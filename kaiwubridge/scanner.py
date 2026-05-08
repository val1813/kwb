"""Schema扫描模块：表结构扫描、数据采样、统计

职责：
- 通过SQLAlchemy Inspector获取数据库的表和列信息
- 对每列进行数据采样（随机取10条非空值）
- 统计null比例和distinct数量
- 结果存入MetadataStore供后续使用

设计决策：
- 采样10条：足够让LLM理解字段含义，又不会泄露过多数据
- 使用LIMIT而非全表扫描：大表也能快速完成
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .connectors import DatabaseManager
from .models import ColumnInfo, TableInfo

# 每列采样的非空值数量：10条足够让LLM理解字段含义
_SAMPLE_SIZE = 10


class SchemaScanner:
    """数据库Schema扫描器

    扫描数据库中所有表的结构信息，包括：
    - 列名、类型、是否可空
    - 样本值（随机采样）
    - 空值比例、去重数量
    - 表/列注释（如果数据库支持）
    """

    def __init__(self, db_manager: DatabaseManager):
        self._db_manager = db_manager

    def scan_database(self, db_id: str) -> list[TableInfo]:
        """扫描整个数据库的所有表"""
        inspector = self._db_manager.get_inspector(db_id)
        table_names = inspector.get_table_names()
        tables = []
        for table_name in table_names:
            table = self.scan_table(db_id, table_name)
            tables.append(table)
        return tables

    def scan_table(self, db_id: str, table_name: str) -> TableInfo:
        """扫描单张表的结构和统计信息"""
        inspector = self._db_manager.get_inspector(db_id)
        engine = self._db_manager.get_engine(db_id)

        # 获取列信息
        columns_raw = inspector.get_columns(table_name)

        # 获取表注释（SQLite等不支持的数据库会抛NotImplementedError）
        try:
            table_comment = inspector.get_table_comment(table_name).get("text", "") or ""
        except (NotImplementedError, Exception):
            table_comment = ""

        # 获取行数
        row_count = self._get_row_count(engine, table_name)

        # 扫描每列的统计信息
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
        """获取表总行数"""
        try:
            with engine.connect() as conn:
                result = conn.execute(text(f'SELECT COUNT(*) FROM "{table_name}"'))
                return result.scalar() or 0
        except Exception:
            return 0

    def _scan_column(
        self, engine: Engine, table_name: str, col: dict, row_count: int
    ) -> ColumnInfo:
        """扫描单列的统计信息

        包括：样本值、空值比例、去重数量
        """
        col_name = col["name"]
        col_type = str(col.get("type", ""))
        nullable = col.get("nullable", True)
        comment = col.get("comment", "") or ""

        sample_values: list = []
        null_ratio = 0.0
        distinct_count = 0

        if row_count > 0:
            try:
                with engine.connect() as conn:
                    # 采样：取最多_SAMPLE_SIZE条非空的不重复值
                    result = conn.execute(
                        text(
                            f'SELECT DISTINCT "{col_name}" FROM "{table_name}" '
                            f'WHERE "{col_name}" IS NOT NULL LIMIT {_SAMPLE_SIZE}'
                        )
                    )
                    sample_values = [self._serialize_value(r[0]) for r in result.fetchall()]

                    # 统计空值数量，计算空值比例
                    result = conn.execute(
                        text(
                            f'SELECT COUNT(*) FROM "{table_name}" '
                            f'WHERE "{col_name}" IS NULL'
                        )
                    )
                    null_count = result.scalar() or 0
                    null_ratio = round(null_count / row_count, 3)

                    # 统计去重数量
                    result = conn.execute(
                        text(f'SELECT COUNT(DISTINCT "{col_name}") FROM "{table_name}"')
                    )
                    distinct_count = result.scalar() or 0
            except Exception:
                # 某些列类型可能不支持DISTINCT等操作，跳过
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

    def _serialize_value(self, value) -> str | int | float | None:
        """将数据库值序列化为可JSON化的基础类型"""
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return value
        return str(value)
