"""语义名片生成：LLM驱动的字段语义分析"""

from __future__ import annotations

from .llm_client import LLMClient
from .metadata import MetadataStore
from .models import ChatMessage, SemanticCard, TableInfo
from .prompts import SEMANTIC_CARD_PROMPT


class SemanticGenerator:
    """LLM驱动的语义名片生成器"""

    def __init__(self, llm_client: LLMClient, metadata: MetadataStore):
        self.llm = llm_client
        self.metadata = metadata

    async def generate_card(self, table: TableInfo, column_name: str) -> SemanticCard:
        """为单个字段生成语义名片"""
        # 找到目标列
        target_col = None
        for col in table.columns:
            if col.name == column_name:
                target_col = col
                break
        if not target_col:
            raise ValueError(f"字段 {column_name} 不存在于表 {table.table_name}")

        other_columns = [c.name for c in table.columns if c.name != column_name]

        prompt = SEMANTIC_CARD_PROMPT.format(
            db_name=table.db_id,
            table_name=table.table_name,
            column_name=column_name,
            data_type=target_col.type,
            sample_values=target_col.sample_values[:5],
            other_columns=", ".join(other_columns),
            table_comment=table.comment,
            column_comment=target_col.comment,
        )

        messages = [ChatMessage(role="user", content=prompt)]

        try:
            result = await self.llm.generate_json(messages)
            field_id = f"{table.db_id}.{table.table_name}.{column_name}"
            card = SemanticCard(
                field_id=field_id,
                business_name=result.get("business_name", column_name),
                description=result.get("description", ""),
                category=result.get("category", "其他"),
                unit=result.get("unit"),
                notes=result.get("notes", ""),
                confidence=0.85,
            )
            self.metadata.save_card(card)
            return card
        except Exception as e:
            # LLM调用失败时生成基础名片
            field_id = f"{table.db_id}.{table.table_name}.{column_name}"
            card = SemanticCard(
                field_id=field_id,
                business_name=column_name,
                description=f"表{table.table_name}的{column_name}字段",
                category="其他",
                confidence=0.0,
                notes=f"自动生成失败: {str(e)[:50]}",
            )
            self.metadata.save_card(card)
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
