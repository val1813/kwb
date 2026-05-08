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
        confirmed_mappings = mapping_store.get_all_mappings(status="confirmed")
        for mapping in confirmed_mappings:
            # field_id格式：db_id.table.column
            parts_a = mapping["field_a_id"].split(".")
            parts_b = mapping["field_b_id"].split(".")
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
