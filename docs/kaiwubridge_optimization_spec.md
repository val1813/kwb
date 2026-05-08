# KaiwuBridge 优化 Spec v2.0

**基于现有代码的三层优化，串联关系，不是替代关系。**

```
治理层：同名冲突检测 + 语义名片warning标注
    ↓
路由层：SchemaGraphSQL（外键图 + LLM一次调用 + 路径搜索）
    ↓
执行层：执行反馈自纠正 + 结果合理性校验 + 数据溯源
```

---

## 优化一：治理层 — 同名冲突检测

### 问题
同一个表名或字段名在不同库含义完全不同（aaa在销售库是销售成本，在财务库是销售提成），现有代码无法检测，查询时会静默选错。

### 解决思路
扫描阶段自动检测跨库同名冲突，Wasserstein验证分布差异，推送人工确认，写入语义名片warning字段，查询时LLM看到warning主动区分。

### 改动1：scanner.py — 加入同名冲突检测

在现有`scan_database()`完成后，新增`detect_name_conflicts()`：

```python
# kaiwubridge/scanner.py 新增方法

def detect_name_conflicts(
    self,
    db_manager: "DatabaseManager",
    metadata: "MetadataStore",
) -> list[dict]:
    """
    扫描所有已接入数据库，检测跨库同名表/字段冲突。

    返回冲突列表，每条包含：
    - conflict_type: "table" 或 "column"
    - name: 冲突的表名或字段名
    - occurrences: [{db_id, table, sample_values, distribution_stats}]
    - distribution_similar: bool（Wasserstein距离是否接近）
    - severity: "high"（同名异义）或 "low"（同名同义）
    """
    from scipy.stats import wasserstein_distance
    import numpy as np

    all_tables = metadata.get_all_tables()
    conflicts = []

    # 按表名分组
    table_name_map: dict[str, list] = {}
    for table in all_tables:
        table_name_map.setdefault(table.table_name, []).append(table)

    for table_name, tables in table_name_map.items():
        if len(tables) < 2:
            continue

        # 同名表：比较各库中该表的字段集合
        for i in range(len(tables)):
            for j in range(i + 1, len(tables)):
                t_a, t_b = tables[i], tables[j]
                cols_a = {c.name for c in t_a.columns}
                cols_b = {c.name for c in t_b.columns}
                overlap = cols_a & cols_b
                jaccard = len(overlap) / len(cols_a | cols_b) if cols_a | cols_b else 0

                # 字段重叠度低，说明同名但结构不同，高风险冲突
                severity = "low" if jaccard > 0.7 else "high"

                conflicts.append({
                    "conflict_type": "table",
                    "name": table_name,
                    "db_a": t_a.db_id,
                    "db_b": t_b.db_id,
                    "field_overlap_ratio": round(jaccard, 3),
                    "severity": severity,
                    "confirmed": False,
                    "warning_text": "",
                })

    # 按字段名分组（跨库同名字段）
    field_map: dict[str, list] = {}
    for table in all_tables:
        for col in table.columns:
            key = col.name.lower()
            field_map.setdefault(key, []).append({
                "db_id": table.db_id,
                "table": table.table_name,
                "col": col,
                "field_id": f"{table.db_id}.{table.table_name}.{col.name}",
            })

    for field_name, occurrences in field_map.items():
        if len(occurrences) < 2:
            continue

        # 只检测数值型字段的分布差异
        numeric_occs = [
            o for o in occurrences
            if any(k in o["col"].type.lower()
                   for k in ("int", "float", "decimal", "numeric", "double"))
        ]

        distribution_similar = None
        if len(numeric_occs) >= 2 and numeric_occs[0]["col"].sample_values:
            try:
                vals_a = [float(v) for v in numeric_occs[0]["col"].sample_values
                          if v is not None]
                vals_b = [float(v) for v in numeric_occs[1]["col"].sample_values
                          if v is not None]
                if vals_a and vals_b:
                    # 归一化后计算Wasserstein距离
                    range_a = max(vals_a) - min(vals_a) or 1
                    range_b = max(vals_b) - min(vals_b) or 1
                    norm_a = [(v - min(vals_a)) / range_a for v in vals_a]
                    norm_b = [(v - min(vals_b)) / range_b for v in vals_b]
                    dist = wasserstein_distance(norm_a, norm_b)
                    distribution_similar = dist < 0.15
            except Exception:
                pass

        # 分布不相似的同名字段 = 高风险同名异义
        severity = "low"
        if distribution_similar is False:
            severity = "high"

        conflicts.append({
            "conflict_type": "column",
            "name": field_name,
            "occurrences": [
                {"db_id": o["db_id"], "table": o["table"], "field_id": o["field_id"]}
                for o in occurrences
            ],
            "distribution_similar": distribution_similar,
            "severity": severity,
            "confirmed": False,
            "warning_text": "",
        })

    # 写入metadata，推送管理界面待确认
    metadata.store_conflicts(conflicts)
    return conflicts
```

### 改动2：metadata.py — 新增冲突存储方法

```python
# kaiwubridge/metadata.py 新增

def store_conflicts(self, conflicts: list[dict]):
    """存储冲突检测结果到SQLite"""
    with self._conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_conflicts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conflict_type TEXT,
                name TEXT,
                details_json TEXT,
                severity TEXT,
                confirmed BOOLEAN DEFAULT 0,
                warning_text TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # 清除旧的未确认冲突
        conn.execute("DELETE FROM schema_conflicts WHERE confirmed = 0")
        for c in conflicts:
            import json
            conn.execute(
                """INSERT INTO schema_conflicts
                   (conflict_type, name, details_json, severity)
                   VALUES (?, ?, ?, ?)""",
                (c["conflict_type"], c["name"],
                 json.dumps(c, ensure_ascii=False), c["severity"])
            )

def get_conflicts(self, unconfirmed_only=False) -> list[dict]:
    """获取冲突列表"""
    import json
    sql = "SELECT * FROM schema_conflicts"
    if unconfirmed_only:
        sql += " WHERE confirmed = 0"
    sql += " ORDER BY severity DESC, created_at DESC"
    with self._conn() as conn:
        rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]

def confirm_conflict(self, conflict_id: int, warning_text: str):
    """
    人工确认冲突，写入warning文本。
    warning_text会被注入到相关字段的语义名片里。
    """
    with self._conn() as conn:
        conn.execute(
            """UPDATE schema_conflicts
               SET confirmed=1, warning_text=?
               WHERE id=?""",
            (warning_text, conflict_id)
        )

def get_warnings_for_field(self, field_id: str) -> list[str]:
    """
    查询某个字段关联的所有warning文本，
    供构建LLM context时注入。
    """
    import json
    with self._conn() as conn:
        rows = conn.execute(
            "SELECT warning_text FROM schema_conflicts WHERE confirmed=1"
        ).fetchall()
    warnings = []
    for row in rows:
        if row["warning_text"] and field_id in row["warning_text"]:
            warnings.append(row["warning_text"])
    return warnings
```

### 改动3：admin.py — 新增冲突审核界面API

```python
# kaiwubridge/admin.py 新增端点

@router.get("/api/conflicts")
async def list_conflicts(request: Request, unconfirmed_only: bool = False):
    """获取冲突列表"""
    metadata = request.app.state.metadata
    return metadata.get_conflicts(unconfirmed_only=unconfirmed_only)


class ConflictConfirmRequest(BaseModel):
    warning_text: str  # 管理员填写的warning说明


@router.post("/api/conflicts/{conflict_id}/confirm")
async def confirm_conflict(
    conflict_id: int,
    body: ConflictConfirmRequest,
    request: Request,
):
    """
    管理员确认冲突并填写warning说明。
    warning_text示例：
    "注意：sales_db.aaa是销售成本，finance_db.aaa是销售提成，两者完全不同，请勿混用"
    """
    metadata = request.app.state.metadata
    metadata.confirm_conflict(conflict_id, body.warning_text)
    return {"status": "confirmed"}
```

### 改动4：server.py — 构建context时注入warning

在`_build_schema_description()`里，对每个字段查询关联的warning并注入：

```python
# server.py _build_schema_description() 修改

for col in table.columns:
    card = card_map.get(col.name)
    field_id = f"{db_id}.{table.table_name}.{col.name}"

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

    # 注入warning（同名冲突标注）
    warnings = metadata.get_warnings_for_field(field_id)
    for w in warnings:
        desc += f"\n  ⚠️ {w}"

    parts.append(desc)
```

---

## 优化二：路由层 — SchemaGraphSQL

### 问题
现有`_infer_target_db()`用SQL里的表名字符串匹配来反推数据库，LLM用语义名称生成SQL时会匹配失败，多库场景路由准确率低。

### 解决思路
接入时建外键关系图，查询时用一次LLM调用提取问题实体，在图上用路径搜索找到最小表集合，只把相关表的schema给LLM。零样本、无embedding、BIRD benchmark SOTA。

### 新增文件：schema_graph.py

```python
# kaiwubridge/schema_graph.py（新文件，约150行）

"""
SchemaGraphSQL实现：
基于外键关系图 + LLM实体提取 + 路径搜索的schema linking。
零样本、无embedding模型、无需训练。
参考：SchemaGraphSQL (ACL ARR 2025 May)
"""

from __future__ import annotations
import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class SchemaNode:
    db_id: str
    table_name: str
    columns: list[str] = field(default_factory=list)
    business_names: dict[str, str] = field(default_factory=dict)  # col -> business_name

    @property
    def node_id(self) -> str:
        return f"{self.db_id}.{self.table_name}"


class SchemaGraph:
    """
    多库schema图。
    节点：(db_id, table_name)
    边：外键关系（跨表JOIN路径）+ 跨库语义映射（来自mapping_store）
    """

    def __init__(self):
        self._nodes: dict[str, SchemaNode] = {}
        self._edges: dict[str, set[str]] = {}  # node_id -> {neighbor_node_id}

    def add_node(self, node: SchemaNode):
        self._nodes[node.node_id] = node
        if node.node_id not in self._edges:
            self._edges[node.node_id] = set()

    def add_edge(self, node_a_id: str, node_b_id: str):
        self._edges.setdefault(node_a_id, set()).add(node_b_id)
        self._edges.setdefault(node_b_id, set()).add(node_a_id)

    def get_node(self, node_id: str) -> SchemaNode | None:
        return self._nodes.get(node_id)

    def all_nodes(self) -> list[SchemaNode]:
        return list(self._nodes.values())

    def bfs_subgraph(
        self, start_node_ids: list[str], max_hops: int = 2
    ) -> list[SchemaNode]:
        """
        从起始节点出发BFS，返回max_hops跳内的所有节点。
        用于在找到相关表后，把JOIN所需的关联表也带上。
        """
        visited = set(start_node_ids)
        queue = list(start_node_ids)
        result = []

        for _ in range(max_hops):
            next_queue = []
            for node_id in queue:
                for neighbor in self._edges.get(node_id, set()):
                    if neighbor not in visited:
                        visited.add(neighbor)
                        next_queue.append(neighbor)
            queue = next_queue

        for node_id in visited:
            node = self._nodes.get(node_id)
            if node:
                result.append(node)

        return result


def build_schema_graph(
    db_manager, metadata, mapping_store
) -> SchemaGraph:
    """
    构建多库schema图。
    节点来源：metadata里的所有表。
    边来源：
      1. 外键关系（SQLAlchemy inspector）
      2. 跨库语义映射（mapping_store里已确认的映射）
    """
    graph = SchemaGraph()
    all_tables = metadata.get_all_tables()

    # 添加节点
    for table in all_tables:
        cards = metadata.get_cards_for_table(table.db_id, table.table_name)
        business_names = {
            c.field_id.split(".")[-1]: c.business_name
            for c in cards if c.business_name
        }
        node = SchemaNode(
            db_id=table.db_id,
            table_name=table.table_name,
            columns=[col.name for col in table.columns],
            business_names=business_names,
        )
        graph.add_node(node)

    # 添加外键边
    for db_id in db_manager.list_databases():
        try:
            inspector = db_manager.get_inspector(db_id)
            table_names = inspector.get_table_names()
            for table_name in table_names:
                fks = inspector.get_foreign_keys(table_name)
                for fk in fks:
                    referred_table = fk.get("referred_table")
                    if referred_table:
                        node_a = f"{db_id}.{table_name}"
                        node_b = f"{db_id}.{referred_table}"
                        if graph.get_node(node_a) and graph.get_node(node_b):
                            graph.add_edge(node_a, node_b)
        except Exception as e:
            logger.warning("获取%s外键关系失败: %s", db_id, e)

    # 添加跨库语义映射边（已确认的映射关系）
    try:
        confirmed_mappings = mapping_store.get_mappings(status="confirmed")
        for mapping in confirmed_mappings:
            # field_id格式：db_id.table.column
            parts_a = mapping.field_a_id.split(".")
            parts_b = mapping.field_b_id.split(".")
            if len(parts_a) >= 2 and len(parts_b) >= 2:
                node_a = f"{parts_a[0]}.{parts_a[1]}"
                node_b = f"{parts_b[0]}.{parts_b[1]}"
                if graph.get_node(node_a) and graph.get_node(node_b):
                    graph.add_edge(node_a, node_b)
    except Exception as e:
        logger.warning("加载跨库映射边失败: %s", e)

    return graph


ENTITY_EXTRACTION_PROMPT = """你是一个数据库schema分析专家。

用户问题：{question}

可用的数据库表（格式：db_id.table_name: 业务描述）：
{table_list}

请从用户问题中提取关键实体，找出最相关的1-3张表。

只输出JSON，格式：
{{
  "relevant_tables": ["db_id.table_name", ...],
  "reasoning": "一句话说明为什么选这些表"
}}"""


async def link_schema(
    question: str,
    graph: SchemaGraph,
    llm_client,
    max_tables: int = 5,
) -> list[SchemaNode]:
    """
    SchemaGraphSQL核心：
    1. 用一次LLM调用提取问题实体，映射到相关表
    2. 在schema图上BFS扩展，找到JOIN所需的关联表
    3. 返回精选的表子集（最多max_tables张）

    不需要embedding，不需要训练，零样本。
    """
    all_nodes = graph.all_nodes()
    if not all_nodes:
        return []

    # 构建表列表描述（包含业务名称）
    table_list_lines = []
    for node in all_nodes:
        business_names = list(node.business_names.values())[:3]
        desc = "、".join(business_names) if business_names else node.table_name
        table_list_lines.append(f"{node.node_id}: {desc}")
    table_list = "\n".join(table_list_lines)

    # 一次LLM调用提取相关表
    from .models import ChatMessage
    prompt = ENTITY_EXTRACTION_PROMPT.format(
        question=question,
        table_list=table_list,
    )
    try:
        response = await llm_client.chat(
            [ChatMessage(role="user", content=prompt)],
            temperature=0,
            max_tokens=300,
        )
        # 提取JSON
        import re
        json_match = re.search(r'\{.*\}', response, re.DOTALL)
        if json_match:
            result = json.loads(json_match.group())
            relevant_table_ids = result.get("relevant_tables", [])
        else:
            relevant_table_ids = []
    except Exception as e:
        logger.warning("Schema linking LLM调用失败: %s，回退到全量schema", e)
        return all_nodes[:max_tables]

    # 验证返回的表名是否存在
    valid_ids = [
        tid for tid in relevant_table_ids
        if graph.get_node(tid) is not None
    ]

    if not valid_ids:
        # LLM没找到相关表，返回所有表（回退策略）
        logger.warning("Schema linking未找到相关表，使用全量schema")
        return all_nodes

    # BFS扩展：把JOIN需要的关联表也带上
    subgraph_nodes = graph.bfs_subgraph(valid_ids, max_hops=1)

    # 控制数量，优先保留LLM直接选中的表
    priority_nodes = [graph.get_node(tid) for tid in valid_ids if graph.get_node(tid)]
    other_nodes = [n for n in subgraph_nodes if n.node_id not in valid_ids]

    result_nodes = priority_nodes + other_nodes
    return result_nodes[:max_tables]
```

### 改动5：server.py — 集成schema_graph路由

```python
# server.py create_app() 初始化时添加

from .schema_graph import build_schema_graph, link_schema

# 初始化schema图（启动时建一次，scan后重建）
schema_graph = build_schema_graph(db_manager, metadata, mapping_store)
app.state.schema_graph = schema_graph

# chat_completions端点里，替换原有的权限过滤和schema构建逻辑：

# 原来：
# visible_tables = permissions.filter_schema(all_tables, role_id)
# schema_desc = _build_schema_description(visible_tables, metadata)

# 改为：
# 第一步：权限过滤（保持不变）
visible_tables = permissions.filter_schema(all_tables, role_id)

# 第二步：schema linking（新增）
# 只把和问题相关的表给LLM，而不是所有visible表
linked_tables = await link_schema(
    question=user_question,
    graph=app.state.schema_graph,
    llm_client=llm,
    max_tables=5,
)

# 取权限过滤和schema linking的交集
visible_ids = {t.table_name for t in visible_tables}
final_tables = [t for t in linked_tables if t.table_name in visible_ids] or visible_tables

# 构建schema描述（用final_tables而不是visible_tables）
schema_desc = _build_schema_description_from_nodes(final_tables, metadata)
```

### 改动6：server.py — 替换_infer_target_db

```python
# server.py 修改_infer_target_db

def _infer_target_db(
    sql: str,
    visible_tables: list,
    linked_tables: list | None = None,
) -> str | None:
    """
    推断目标数据库。
    优先用schema linking结果（linked_tables），
    linked_tables通常只有1-2个库，歧义少。
    """
    # 优先从linked_tables推断（更精准）
    candidates = linked_tables or visible_tables
    sql_upper = sql.upper()

    for table in candidates:
        if table.table_name.upper() in sql_upper:
            return table.db_id if hasattr(table, 'db_id') else table.get('db_id')

    # 回退：如果linked只有一个库
    db_ids = list(set(
        t.db_id if hasattr(t, 'db_id') else t.get('db_id')
        for t in candidates
    ))
    if len(db_ids) == 1:
        return db_ids[0]

    # 最终回退：从所有visible_tables推断
    for table in visible_tables:
        if table.table_name.upper() in sql_upper:
            return table.db_id
    return None
```

---

## 优化三：执行层 — 自纠正 + 合理性校验 + 溯源

### 问题说明
- 自纠正解决：SQL语法错误、字段不存在等**执行层技术错误**（有报错信号）
- 合理性校验解决：SQL成功但结果异常（空结果/数量级异常）的**静默失败**
- 溯源解决：用户事后反馈时能重建当时的查询context

**注意**：语义错误（选错库、口径混淆）由治理层解决，执行层无法发现语义错误。

### 改动7：prompts.py — 新增纠正prompt

```python
# kaiwubridge/prompts.py 新增

SQL_ERROR_CORRECTION_PROMPT = """用户问题：{question}

你刚才生成的SQL执行失败了：
```sql
{sql}
```

错误信息：{error}

可用的schema：
{schema_description}

请根据错误信息修正SQL。只输出修正后的SQL，放在```sql代码块中，不要其他内容。"""


SQL_EMPTY_RESULT_PROMPT = """用户问题：{question}

你生成的SQL执行成功但返回了空结果：
```sql
{sql}
```

可用的schema：
{schema_description}

可能的原因：
1. 筛选条件过严（时间范围、状态值等）
2. 表名或字段理解有偏差
3. 确实没有数据

请重新分析，尝试放宽条件或调整查询逻辑。
如果确认应该有数据，输出修正后的SQL（```sql代码块）。
如果合理判断就是没有数据，回答"该条件下确实无数据：[说明原因]"。"""
```

### 改动8：server.py — 执行层完整流程

用一个独立函数封装执行+自纠正+校验+溯源：

```python
# server.py 新增函数

import uuid as _uuid

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
                from .models import ChatMessage
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
            from .models import ChatMessage
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
    import re
    match = re.search(r"```sql\s*\n(.*?)```", response, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return None
```

### 改动9：回答用户时加数据来源和异常提示

```python
# server.py chat_completions端点里，替换原有的execute+interpret逻辑

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
        f"建议：请尝试换一种表达方式，或联系数据管理员。\n"
        f"（追踪ID：{exec_result['trace_id']}）",
        config.llm.model,
    )

# 构建解释prompt（包含数据来源）
result_text = _format_result(exec_result)
source_note = f"\n\n数据来源：{target_db}，查询时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}"

# 如果有合理性异常，提示用户确认
anomaly_note = ""
if exec_result.get("anomaly"):
    anomaly_note = f"\n\n⚠️ 注意：{exec_result['anomaly']}，请确认结果是否符合预期。"

interpret_prompt = RESULT_INTERPRETATION_PROMPT.format(
    question=user_question,
    sql=exec_result["sql_used"],
    result=result_text,
)
final_answer = await llm.chat(
    [ChatMessage(role="user", content=interpret_prompt)]
)
final_answer += source_note + anomaly_note

return _build_response(final_answer, config.llm.model)
```

---

## 改动汇总

| 文件 | 改动 | 代码量 |
|------|------|--------|
| `scanner.py` | 新增`detect_name_conflicts()` | +80行 |
| `metadata.py` | 新增冲突存储/查询/warning方法 | +60行 |
| `admin.py` | 新增冲突审核API端点 | +25行 |
| `schema_graph.py` | 新文件，SchemaGraphSQL完整实现 | +150行 |
| `prompts.py` | 新增纠正prompt模板 | +20行 |
| `server.py` | 集成schema_graph路由+执行重试+溯源 | +120行 |
| **合计** | | **约455行** |

---

## 执行顺序

**第一步**：实现`schema_graph.py`和`server.py`的路由改动。
这是独立的，不依赖其他改动，可以立刻提升复杂问题的路由准确率。

**第二步**：实现治理层的冲突检测（`scanner.py` + `metadata.py` + `admin.py`）。
需要重新跑`kwb scan`触发冲突检测，然后在管理界面确认冲突。

**第三步**：实现执行层的自纠正+校验+溯源（`server.py` + `prompts.py`）。
在前两步稳定后加入，避免掩盖路由和治理层的问题。

---

## 验证方法

按spec里测试用例跑：
- 同名不同义字段查询：验证warning是否出现在context里
- 跨库复杂查询：验证schema linking是否选对表（不超过5张）
- SQL语法错误：验证自纠正是否在2次内修正
- 空结果查询：验证是否触发重试或合理解释
- 审计日志：验证trace_id是否可追溯
