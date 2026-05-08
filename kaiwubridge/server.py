"""FastAPI服务：OpenAI兼容的/v1/chat/completions端点

核心流程：
JWT验证 → 频率限制 → 加载schema → 权限过滤 → 构建context →
LLM生成SQL → SQL审查 → RLAC行级过滤 → 执行 → 脱敏 → 解释结果
"""

from __future__ import annotations

import re
import time
import uuid
import uuid as _uuid
from datetime import datetime

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from .auth import AuthManager
from .config import AppConfig
from .connectors import DatabaseManager
from .executor import QueryExecutor
from .llm_client import LLMClient
from .metadata import MetadataStore
from .models import (
    ChatChoice,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    TableInfo,
)
from .permissions import PermissionEngine
from .prompts import (
    QUERY_CONTEXT_TEMPLATE,
    RESULT_INTERPRETATION_PROMPT,
    SQL_GENERATION_SYSTEM,
    SQL_EMPTY_RESULT_PROMPT,
    SQL_ERROR_CORRECTION_PROMPT,
)
from .mappings import MappingStore
from .schema_graph import build_schema_graph, link_schema, SchemaNode
from .security import AuditLogger, RateLimiter

# SQL代码块提取：优先匹配```sql代码块，其次匹配裸SELECT语句
_SQL_BLOCK_PATTERN = re.compile(r"```sql\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_BARE_SELECT_PATTERN = re.compile(
    r"((?:WITH|SELECT)\b[^;]*;?)", re.DOTALL | re.IGNORECASE
)

# 发给LLM解释的行数上限，避免超出token限制
_LLM_DISPLAY_ROWS = 20

# 频率限制：每用户每分钟最多50次查询，防止暴力枚举
_RATE_LIMIT_MAX_REQUESTS = 50
_RATE_LIMIT_WINDOW_SECONDS = 60


def create_app(config: AppConfig) -> FastAPI:
    """创建FastAPI应用实例，注入所有依赖"""
    app = FastAPI(title="开物数据中间层", version="0.1.0")

    # 初始化各组件
    db_manager = DatabaseManager(config.databases)
    metadata = MetadataStore(config.server.metadata_db)
    auth = AuthManager(config.server.jwt_secret)
    permissions = PermissionEngine(config.roles)
    llm = LLMClient(
        base_url=config.llm.base_url,
        api_key=config.llm.api_key,
        model=config.llm.model,
    )
    audit_logger = AuditLogger()
    rate_limiter = RateLimiter(
        max_requests=_RATE_LIMIT_MAX_REQUESTS,
        window_seconds=_RATE_LIMIT_WINDOW_SECONDS,
    )
    executor = QueryExecutor(
        db_manager=db_manager,
        audit_logger=audit_logger,
        enable_masking=True,
    )

    # 挂载到app.state，供CLI等外部访问
    app.state.config = config
    app.state.db_manager = db_manager
    app.state.metadata = metadata
    app.state.auth = auth
    app.state.permissions = permissions
    app.state.llm = llm
    app.state.executor = executor
    app.state.audit_logger = audit_logger
    app.state.rate_limiter = rate_limiter

    # 初始化映射存储和schema图
    mapping_store = MappingStore(config.server.metadata_db)
    app.state.mapping_store = mapping_store
    schema_graph = build_schema_graph(db_manager, metadata, mapping_store)
    app.state.schema_graph = schema_graph

    # 挂载管理后台路由
    from .admin import router as admin_router
    app.include_router(admin_router)

    @app.get("/health")
    async def health():
        """健康检查端点"""
        return {"status": "ok", "version": "0.1.0"}

    @app.post("/v1/chat/completions")
    async def chat_completions(
        request: ChatRequest,
        authorization: str = Header(default=""),
    ):
        """OpenAI兼容的聊天补全接口

        安全流程：
        1. JWT身份验证
        2. 频率限制检查
        3. 加载schema + 权限过滤（LLM永远看不到被禁止的字段）
        4. 构建context发给LLM
        5. LLM生成SQL → sqlparse审查 → 表级权限审查
        6. RLAC行级过滤（子查询包装，无法绕过）
        7. 执行 + 结果数量限制 + 敏感字段脱敏
        8. 结果发回LLM做自然语言解释
        """
        # ---- 第1关：JWT身份验证 ----
        if not authorization:
            raise HTTPException(status_code=401, detail="缺少Authorization头")
        try:
            user_ctx = auth.extract_from_header(authorization)
        except Exception as e:
            raise HTTPException(status_code=401, detail=f"Token无效: {str(e)}")

        user_id = user_ctx["user_id"]
        role_id = user_ctx["role_id"]
        user_attrs = user_ctx.get("attrs", {})

        # ---- 第2关：频率限制 ----
        if not rate_limiter.is_allowed(user_id):
            audit_logger.log_security_event(
                user_id=user_id,
                event_type="rate_limit_exceeded",
                detail=f"用户 {user_id} 超出频率限制 ({_RATE_LIMIT_MAX_REQUESTS}次/{_RATE_LIMIT_WINDOW_SECONDS}秒)",
            )
            raise HTTPException(
                status_code=429,
                detail=f"请求过于频繁，每分钟最多{_RATE_LIMIT_MAX_REQUESTS}次查询",
            )

        # ---- 第3关：加载schema + 权限过滤 ----
        all_tables = metadata.get_all_tables()
        if not all_tables:
            return _build_response("系统尚未扫描任何数据库，请先执行 kwb scan", config.llm.model)

        # 权限过滤：移除不可见的表和字段（LLM永远看不到被禁止的内容）
        visible_tables = permissions.filter_schema(all_tables, role_id)
        if not visible_tables:
            return _build_response("您没有任何数据库的访问权限", config.llm.model)

        # 构建行级过滤条件
        row_filters = permissions.get_row_filters(role_id, user_attrs)

        # 构建允许访问的表名集合（用于SQL审查）
        allowed_tables = {t.table_name for t in visible_tables}

        # ---- 第4关：Schema Linking + 构建LLM上下文 ----
        user_question = _extract_last_user_message(request.messages)

        # Schema linking：只给LLM看和问题相关的表，而不是全部visible表
        try:
            linked_nodes = await link_schema(
                question=user_question,
                graph=app.state.schema_graph,
                llm_client=llm,
                max_tables=5,
            )
            # 取权限过滤和schema linking的交集
            final_tables = [
                t for t in visible_tables
                if any(n.table_name == t.table_name and n.db_id == t.db_id
                       for n in linked_nodes)
            ]
            # 如果linking没找到任何表，回退到全量visible
            if not final_tables:
                final_tables = visible_tables
        except Exception:
            # schema linking失败时回退到全量
            final_tables = visible_tables

        schema_desc = _build_schema_description(final_tables, metadata)
        permission_notes = _build_permission_notes(row_filters)

        context = QUERY_CONTEXT_TEMPLATE.format(
            schema_description=schema_desc,
            permission_notes=permission_notes,
            user_question=user_question,
        )

        # ---- 第5关：调用LLM生成回答 ----
        llm_messages = [
            ChatMessage(role="system", content=SQL_GENERATION_SYSTEM),
            ChatMessage(role="user", content=context),
        ]
        llm_response = await llm.chat(
            llm_messages,
            temperature=config.llm.temperature,
            max_tokens=config.llm.max_tokens,
        )

        # ---- 第6关：如果包含SQL，审查并执行 ----
        sql = _extract_sql(llm_response)
        if sql:
            # 推断目标数据库（优先用schema linking缩小后的表集合）
            target_db = _infer_target_db(sql, visible_tables, final_tables)
            if not target_db:
                return _build_response(
                    f"无法确定查询目标数据库。LLM回答：\n\n{llm_response}",
                    config.llm.model,
                )

            # 执行SQL（带自纠正+合理性校验+溯源）
            exec_result = await execute_with_retry(
                sql=sql,
                target_db=target_db,
                user_question=user_question,
                schema_desc=schema_desc,
                executor=executor,
                llm=llm,
                row_filters=row_filters,
                allowed_tables=allowed_tables,
                user_id=user_id,
                role_id=role_id,
                audit_logger=audit_logger,
            )

            if not exec_result["success"]:
                return _build_response(
                    f"查询执行失败，已尝试{exec_result['retry_count']}次自动修正。\n"
                    f"错误：{exec_result.get('error', '未知')}\n"
                    f"（追踪ID：{exec_result['trace_id']}）",
                    config.llm.model,
                )

            # 将结果发回LLM做自然语言解释
            result_text = _format_result(exec_result)
            interpret_prompt = RESULT_INTERPRETATION_PROMPT.format(
                question=user_question,
                sql=exec_result["sql_used"],
                result=result_text,
            )
            final_answer = await llm.chat(
                [ChatMessage(role="user", content=interpret_prompt)]
            )

            # 附加数据来源和异常提示
            source_note = f"\n\n数据来源：{target_db}，查询时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}"
            anomaly_note = ""
            if exec_result.get("anomaly"):
                anomaly_note = f"\n\n注意：{exec_result['anomaly']}，请确认结果是否符合预期。"

            return _build_response(final_answer + source_note + anomaly_note, config.llm.model)
        else:
            # LLM直接回答，无需执行SQL
            audit_logger.log_query(
                user_id=user_id,
                role_id=role_id,
                question=user_question,
                generated_sql=None,
                result_rows=0,
                success=True,
            )
            return _build_response(llm_response, config.llm.model)

    @app.on_event("shutdown")
    async def shutdown():
        """关闭时清理资源"""
        await llm.close()
        db_manager.close_all()
        metadata.close()

    return app


# ============ 辅助函数 ============


def _extract_last_user_message(messages: list[ChatMessage]) -> str:
    """提取最后一条用户消息"""
    for msg in reversed(messages):
        if msg.role == "user":
            return msg.content
    return ""


def _build_schema_description(tables: list[TableInfo], metadata: MetadataStore) -> str:
    """构建发给LLM的schema描述文本，包含语义名片信息"""
    parts = []
    # 按数据库分组
    db_tables: dict[str, list[TableInfo]] = {}
    for t in tables:
        db_tables.setdefault(t.db_id, []).append(t)

    for db_id, db_table_list in db_tables.items():
        parts.append(f"### 数据库: {db_id}")
        for table in db_table_list:
            parts.append(f"\n**{table.table_name}** ({table.row_count}行)")
            # 获取该表的语义名片
            cards = metadata.get_cards_for_table(db_id, table.table_name)
            card_map = {c.field_id.split(".")[-1]: c for c in cards}

            for col in table.columns:
                card = card_map.get(col.name)
                if card and card.business_name:
                    desc = f"- {col.name}: {card.business_name}"
                    if card.description:
                        desc += f"（{card.description}）"
                    if card.unit:
                        desc += f" [{card.unit}]"
                    if card.notes:
                        desc += f" 注：{card.notes}"
                else:
                    desc = f"- {col.name}: {col.type}"

                # 注入冲突warning（治理层同名冲突标注）
                field_id = f"{db_id}.{table.table_name}.{col.name}"
                for w in metadata.get_warnings_for_field(field_id):
                    desc += f"\n  ⚠️ {w}"

                parts.append(desc)

    return "\n".join(parts)


def _build_permission_notes(row_filters: dict[str, str]) -> str:
    """构建权限说明文本，告知LLM哪些过滤条件会自动执行"""
    if not row_filters:
        return ""
    notes = ["## 权限说明", "以下过滤条件会自动执行，无需在SQL中手动添加："]
    for table_key, condition in row_filters.items():
        notes.append(f"- {table_key}: {condition}")
    return "\n".join(notes)


def _extract_sql(response: str) -> str | None:
    """从LLM响应中提取SQL语句

    提取策略（按优先级）：
    1. ```sql代码块中的内容
    2. 裸SELECT/WITH语句（必须包含FROM才算有效）
    """
    # 优先匹配```sql代码块
    match = _SQL_BLOCK_PATTERN.search(response)
    if match:
        return match.group(1).strip()

    # 其次匹配裸SELECT/WITH语句
    match = _BARE_SELECT_PATTERN.search(response)
    if match:
        sql = match.group(1).strip()
        if "FROM" in sql.upper():
            return sql

    return None


def _infer_target_db(sql: str, visible_tables: list[TableInfo], final_tables: list[TableInfo] | None = None) -> str | None:
    """从SQL中推断目标数据库

    策略：
    1. 优先从final_tables（schema linking缩小后的表集合）推断
    2. 回退到visible_tables全量匹配
    3. 如果只有一个数据库可见，直接使用
    """
    sql_upper = sql.upper()

    # 优先用schema linking结果（更精准，歧义少）
    candidates = final_tables if final_tables else visible_tables
    for table in candidates:
        if table.table_name.upper() in sql_upper:
            return table.db_id

    # 如果linked只有一个库，直接用
    db_ids = list(set(t.db_id for t in candidates))
    if len(db_ids) == 1:
        return db_ids[0]

    # 回退到全量visible_tables
    if final_tables:
        for table in visible_tables:
            if table.table_name.upper() in sql_upper:
                return table.db_id
        db_ids = list(set(t.db_id for t in visible_tables))
        if len(db_ids) == 1:
            return db_ids[0]

    return None


def _format_result(result: dict) -> str:
    """格式化查询结果为文本，用于发给LLM解释"""
    data = result.get("data", [])
    if not data:
        return "查询结果为空"

    lines = []
    headers = list(data[0].keys())
    lines.append(" | ".join(headers))
    lines.append("-" * len(lines[0]))

    for row in data[:_LLM_DISPLAY_ROWS]:
        values = [str(row.get(h, "")) for h in headers]
        lines.append(" | ".join(values))

    if result.get("truncated") or len(data) > _LLM_DISPLAY_ROWS:
        lines.append(f"\n... 共{result['row_count']}行，仅显示前{_LLM_DISPLAY_ROWS}行")

    return "\n".join(lines)


def _build_response(content: str, model: str) -> JSONResponse:
    """构建OpenAI格式的响应"""
    response = ChatResponse(
        id=f"chatcmpl-{uuid.uuid4().hex[:12]}",
        object="chat.completion",
        created=int(time.time()),
        model=model,
        choices=[
            ChatChoice(
                index=0,
                message=ChatMessage(role="assistant", content=content),
                finish_reason="stop",
            )
        ],
    )
    return JSONResponse(content=response.model_dump())


# ============ 执行层：自纠正 + 合理性校验 + 溯源 ============


async def execute_with_retry(
    sql: str,
    target_db: str,
    user_question: str,
    schema_desc: str,
    executor: "QueryExecutor",
    llm: "LLMClient",
    row_filters: dict,
    allowed_tables: set,
    user_id: str,
    role_id: str,
    audit_logger: "AuditLogger",
    max_retries: int = 2,
) -> dict:
    """
    执行SQL，失败时自动纠正重试。
    返回：{
      "success": bool,
      "data": [...],
      "sql_used": str,
      "retry_count": int,
      "trace_id": str,       # 溯源ID，写入audit_log
      "anomaly": str | None, # 合理性校验异常描述
    }
    """
    trace_id = _uuid.uuid4().hex[:12]
    current_sql = sql
    last_error = None

    for attempt in range(max_retries + 1):
        result = executor.execute(
            db_id=target_db,
            sql=current_sql,
            row_filters=row_filters,
            allowed_tables=allowed_tables,
            user_id=user_id,
            role_id=role_id,
            question=user_question,
        )

        if result["success"]:
            # ---- 合理性校验 ----
            anomaly = _check_result_anomaly(result, user_question)

            # 空结果且还有重试机会：尝试放宽查询
            if not result["data"] and attempt < max_retries:
                correction_prompt = SQL_EMPTY_RESULT_PROMPT.format(
                    question=user_question,
                    sql=current_sql,
                    schema_description=schema_desc,
                )
                corrected = await llm.chat(
                    [ChatMessage(role="user", content=correction_prompt)],
                    temperature=0,
                    max_tokens=500,
                )
                # 如果LLM说确实无数据，接受空结果
                if "确实无数据" in corrected:
                    break
                new_sql = _extract_sql_from_response(corrected)
                if new_sql and new_sql != current_sql:
                    current_sql = new_sql
                    continue

            # 记录溯源信息到audit_log（包含trace_id）
            audit_logger.log_query(
                user_id=user_id,
                role_id=role_id,
                question=user_question,
                generated_sql=current_sql,
                result_rows=result.get("row_count", 0),
                success=True,
                extra={"trace_id": trace_id, "retry_count": attempt},
            )

            return {
                "success": True,
                "data": result.get("data", []),
                "row_count": result.get("row_count", 0),
                "truncated": result.get("truncated", False),
                "sql_used": current_sql,
                "retry_count": attempt,
                "trace_id": trace_id,
                "anomaly": anomaly,
            }

        # 执行失败：尝试LLM纠正
        last_error = result.get("error", "未知错误")

        if attempt < max_retries:
            correction_prompt = SQL_ERROR_CORRECTION_PROMPT.format(
                question=user_question,
                sql=current_sql,
                error=last_error,
                schema_description=schema_desc,
            )
            corrected = await llm.chat(
                [ChatMessage(role="user", content=correction_prompt)],
                temperature=0,
                max_tokens=500,
            )
            new_sql = _extract_sql_from_response(corrected)
            if new_sql:
                current_sql = new_sql
            else:
                break  # LLM无法修正，放弃

    # 所有重试失败
    audit_logger.log_query(
        user_id=user_id,
        role_id=role_id,
        question=user_question,
        generated_sql=current_sql,
        result_rows=0,
        success=False,
        extra={"trace_id": trace_id, "error": last_error},
    )
    return {
        "success": False,
        "error": last_error,
        "sql_used": current_sql,
        "retry_count": max_retries,
        "trace_id": trace_id,
        "anomaly": None,
    }


def _check_result_anomaly(result: dict, question: str) -> str | None:
    """
    合理性校验：检测结果是否异常。
    注意：只能检测统计异常，无法检测语义错误（选错库等）。
    语义错误由治理层的冲突检测+warning机制处理。
    """
    data = result.get("data", [])
    row_count = result.get("row_count", 0)

    # 异常1：问聚合问题但返回大量明细
    aggregate_keywords = ["总", "合计", "汇总", "平均", "最大", "最小", "多少"]
    if any(kw in question for kw in aggregate_keywords) and row_count > 100:
        return f"问题看起来需要聚合结果，但返回了{row_count}行明细数据，可能缺少GROUP BY"

    # 异常2：单值问题返回多行
    single_keywords = ["是多少", "有多少", "共有", "一共"]
    if any(kw in question for kw in single_keywords) and row_count > 10:
        return f"问题期望单个数值，但返回了{row_count}行"

    return None  # 无异常


def _extract_sql_from_response(response: str) -> str | None:
    """从LLM响应中提取SQL"""
    match = re.search(r"```sql\s*\n(.*?)```", response, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return None
