"""FastAPI服务：OpenAI兼容的/v1/chat/completions端点

核心流程：
JWT验证 → 频率限制 → 加载schema → 权限过滤 → 构建context →
LLM生成SQL → SQL审查 → RLAC行级过滤 → 执行 → 脱敏 → 解释结果
"""

from __future__ import annotations

import re
import time
import uuid

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
from .prompts import QUERY_CONTEXT_TEMPLATE, RESULT_INTERPRETATION_PROMPT, SQL_GENERATION_SYSTEM
from .mappings import MappingStore
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
    app.state.mapping_store = MappingStore(config.server.metadata_db)

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

        # ---- 第4关：构建LLM上下文 ----
        user_question = _extract_last_user_message(request.messages)
        schema_desc = _build_schema_description(visible_tables, metadata)
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
            # 推断目标数据库
            target_db = _infer_target_db(sql, visible_tables)
            if not target_db:
                return _build_response(
                    f"无法确定查询目标数据库。LLM回答：\n\n{llm_response}",
                    config.llm.model,
                )

            # 执行SQL（内部会做：sqlparse验证 → 表级审查 → RLAC → 数量限制 → 脱敏）
            result = executor.execute(
                db_id=target_db,
                sql=sql,
                row_filters=row_filters,
                allowed_tables=allowed_tables,
                user_id=user_id,
                role_id=role_id,
                question=user_question,
            )

            if not result["success"]:
                return _build_response(
                    f"查询执行失败：{result['error']}", config.llm.model
                )

            # 将结果发回LLM做自然语言解释
            result_text = _format_result(result)
            interpret_prompt = RESULT_INTERPRETATION_PROMPT.format(
                question=user_question,
                sql=sql,
                result=result_text,
            )
            final_answer = await llm.chat(
                [ChatMessage(role="user", content=interpret_prompt)]
            )
            return _build_response(final_answer, config.llm.model)
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


def _infer_target_db(sql: str, visible_tables: list[TableInfo]) -> str | None:
    """从SQL中推断目标数据库

    策略：
    1. 提取SQL中的表名，匹配到已知表所属的数据库
    2. 如果只有一个数据库可见，直接使用
    """
    sql_upper = sql.upper()
    for table in visible_tables:
        if table.table_name.upper() in sql_upper:
            return table.db_id
    # 只有一个数据库可见时直接使用
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
