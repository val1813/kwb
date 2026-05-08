"""Schema扫描：表结构扫描、数据采样、统计"""

from __future__ import annotations

import random

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from .connectors import DatabaseManager
from .models import ColumnInfo, TableInfo


class SchemaScanner:
    """数据库Schema扫描器"""

    def __init__(self, db_manager: DatabaseManager):
        self.db_manager = db_manager

    def scan_database(self, db_id: str) -> list[TableInfo]:
        """扫描整个数据库的所有表"""
        inspector = self.db_manager.get_inspector(db_id)
        table_names = inspector.get_table_names()
        tables = []
        for table_name in table_names:
            table = self.scan_table(db_id, table_name)
            tables.append(table)
        return tables

    def scan_table(self, db_id: str, table_name: str) -> TableInfo:
        """扫描单张表的结构和统计信息"""
        inspector = self.db_manager.get_inspector(db_id)
        engine = self.db_manager.get_engine(db_id)

        # 获取列信息
        columns_raw = inspector.get_columns(table_name)
        # SQLite不支持get_table_comment，需要兼容处理
        try:
            table_comment = inspector.get_table_comment(table_name).get("text", "") or ""
        except (NotImplementedError, Exception):
            table_comment = ""

        # 获取行数
        row_count = self._get_row_count(engine, table_name)

        # 获取每列的统计信息
        columns = []
        for col in columns_raw:
            col_info = self._scan_column(engine, table_name, col, row_count)
            columns.append(col_info)

        return TableInfo(
            db_id=db_id,
            table_name=table_name,
            columns=columns,
            row_count=row_count,
            comment=table_comment,
        )

    def _get_row_count(self, engine: Engine, table_name: str) -> int:
        """获取表行数"""
        try:
            with engine.connect() as conn:
                result = conn.execute(text(f"SELECT COUNT(*) FROM \"{table_name}\""))
                return result.scalar() or 0
        except Exception:
            return 0

    def _scan_column(
        self, engine: Engine, table_name: str, col: dict, row_count: int
    ) -> ColumnInfo:
        """扫描单列的统计信息"""
        col_name = col["name"]
        col_type = str(col.get("type", ""))
        nullable = col.get("nullable", True)
        comment = col.get("comment", "") or ""

        sample_values = []
        null_ratio = 0.0
        distinct_count = 0

        if row_count > 0:
            try:
                with engine.connect() as conn:
                    # 采样10条非空值
                    result = conn.execute(
                        text(
                            f'SELECT DISTINCT "{col_name}" FROM "{table_name}" '
                            f'WHERE "{col_name}" IS NOT NULL LIMIT 10'
                        )
                    )
                    sample_values = [self._serialize_value(r[0]) for r in result.fetchall()]

                    # null比例
                    result = conn.execute(
                        text(
                            f'SELECT COUNT(*) FROM "{table_name}" '
                            f'WHERE "{col_name}" IS NULL'
                        )
                    )
                    null_count = result.scalar() or 0
                    null_ratio = round(null_count / row_count, 3) if row_count > 0 else 0.0

                    # distinct数量
                    result = conn.execute(
                        text(f'SELECT COUNT(DISTINCT "{col_name}") FROM "{table_name}"')
                    )
                    distinct_count = result.scalar() or 0
            except Exception:
                pass

        return ColumnInfo(
            name=col_name,
            type=col_type,
            nullable=nullable,
            sample_values=sample_values,
            null_ratio=null_ratio,
            distinct_count=distinct_count,
            comment=comment,
        )

    def _serialize_value(self, value) -> str | int | float | None:
        """将数据库值序列化为可JSON化的类型"""
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return value
        return str(value)
