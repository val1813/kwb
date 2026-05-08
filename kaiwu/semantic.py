"""语义名片生成模块：LLM驱动的字段语义分析

职责：
- 调用LLM分析每个字段的业务含义
- 生成结构化的语义名片（business_name, description, category, unit, notes）
- 将结果持久化到MetadataStore

设计决策：
- 逐字段调用LLM：虽然慢，但每个字段都能获得充分的上下文分析
- 失败降级：LLM调用失败时生成基础名片（字段名作为业务名称），不阻塞流程
- 置信度0.85：LLM生成的名片默认置信度，低于阈值的需要人工审核
"""

from __future__ import annotations

from .llm_client import LLMClient
from .metadata import MetadataStore
from .models import ChatMessage, SemanticCard, TableInfo
from .prompts import SEMANTIC_CARD_PROMPT

# LLM生成的语义名片默认置信度：高于0.85可自动采用，低于需人工审核
_DEFAULT_CONFIDENCE = 0.85


class SemanticGenerator:
    """LLM驱动的语义名片生成器

    为数据库中的每个字段生成业务语义描述，帮助LLM理解字段含义。
    """

    def __init__(self, llm_client: LLMClient, metadata: MetadataStore):
        self._llm = llm_client
        self._metadata = metadata

    async def generate_card(self, table: TableInfo, column_name: str) -> SemanticCard:
        """为单个字段生成语义名片

        Args:
            table: 表信息（包含所有列的上下文）
            column_name: 目标列名

        Returns:
            生成的语义名片（已持久化到MetadataStore）
        """
        # 找到目标列
        target_col = None
        for col in table.columns:
            if col.name == column_name:
                target_col = col
                break
        if not target_col:
            raise ValueError(f"字段 {column_name} 不存在于表 {table.table_name}")

        # 构建prompt上下文：其他列名帮助LLM理解表的整体结构
        other_columns = [c.name for c in table.columns if c.name != column_name]

        prompt = SEMANTIC_CARD_PROMPT.format(
            db_name=table.db_id,
            table_name=table.table_name,
            column_name=column_name,
            data_type=target_col.type,
            sample_values=target_col.sample_values[:5],  # 最多传5个样本值，避免prompt过长
            other_columns=", ".join(other_columns),
            table_comment=table.comment,
            column_comment=target_col.comment,
        )

        messages = [ChatMessage(role="user", content=prompt)]
        field_id = f"{table.db_id}.{table.table_name}.{column_name}"

        try:
            result = await self._llm.generate_json(messages)
            card = SemanticCard(
                field_id=field_id,
                business_name=result.get("business_name", column_name),
                description=result.get("description", ""),
                category=result.get("category", "其他"),
                unit=result.get("unit"),
                notes=result.get("notes", ""),
                confidence=_DEFAULT_CONFIDENCE,
            )
        except Exception as e:
            # LLM调用失败时降级：用字段名作为业务名称，置信度设为0
            card = SemanticCard(
                field_id=field_id,
                business_name=column_name,
                description=f"表{table.table_name}的{column_name}字段",
                category="其他",
                confidence=0.0,
                notes=f"自动生成失败: {str(e)[:50]}",
            )

        self._metadata.save_card(card)
        return card

    async def generate_cards_for_table(self, table: TableInfo) -> list[SemanticCard]:
        """为整张表的所有字段生成语义名片"""
        cards = []
        for col in table.columns:
            card = await self.generate_card(table, col.name)
            cards.append(card)
        return cards

    async def generate_cards_for_database(
        self, tables: list[TableInfo]
    ) -> list[SemanticCard]:
        """为整个数据库的所有表生成语义名片"""
        all_cards = []
        for table in tables:
            cards = await self.generate_cards_for_table(table)
            all_cards.extend(cards)
        return all_cards
