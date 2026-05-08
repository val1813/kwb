"""SQLite元数据存储模块

职责：
- 持久化存储语义名片（semantic_cards表）
- 缓存schema扫描结果（table_schemas表）
- 提供按数据库/表/字段的查询接口

设计决策：
- 使用SQLite：零部署复杂度，单文件即可
- WAL模式：支持并发读取，写入不阻塞读取
- check_same_thread=False：FastAPI多线程环境下安全使用
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .models import ColumnInfo, SemanticCard, TableInfo


class MetadataStore:
    """SQLite元数据存储

    存储两类数据：
    1. 语义名片 - 每个字段的业务语义描述
    2. Schema缓存 - 数据库表结构的快照
    """

    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        # WAL模式：允许并发读取，写入性能更好
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_tables()

    def _init_tables(self):
        """初始化数据库表结构"""
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS semantic_cards (
                field_id TEXT PRIMARY KEY,
                business_name TEXT DEFAULT '',
                description TEXT DEFAULT '',
                category TEXT DEFAULT '',
                unit TEXT,
                notes TEXT DEFAULT '',
                confidence REAL DEFAULT 0.0,
                verified INTEGER DEFAULT 0,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS table_schemas (
                db_id TEXT NOT NULL,
                table_name TEXT NOT NULL,
                columns_json TEXT NOT NULL,
                row_count INTEGER DEFAULT 0,
                comment TEXT DEFAULT '',
                scanned_at TEXT,
                PRIMARY KEY (db_id, table_name)
            );
        """)
        self.conn.commit()

    # ---- 语义名片操作 ----

    def save_card(self, card: SemanticCard):
        """保存或更新语义名片（INSERT OR REPLACE）"""
        self.conn.execute(
            """INSERT OR REPLACE INTO semantic_cards
               (field_id, business_name, description, category, unit, notes, confidence, verified, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                card.field_id,
                card.business_name,
                card.description,
                card.category,
                card.unit,
                card.notes,
                card.confidence,
                int(card.verified),
                datetime.now().isoformat(),
            ),
        )
        self.conn.commit()

    def get_card(self, field_id: str) -> SemanticCard | None:
        """按field_id查询单张语义名片"""
        row = self.conn.execute(
            "SELECT * FROM semantic_cards WHERE field_id = ?", (field_id,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_card(row)

    def get_cards_for_table(self, db_id: str, table_name: str) -> list[SemanticCard]:
        """获取指定表的所有语义名片"""
        prefix = f"{db_id}.{table_name}."
        rows = self.conn.execute(
            "SELECT * FROM semantic_cards WHERE field_id LIKE ?", (prefix + "%",)
        ).fetchall()
        return [self._row_to_card(r) for r in rows]

    def get_all_cards(self) -> list[SemanticCard]:
        """获取所有语义名片"""
        rows = self.conn.execute("SELECT * FROM semantic_cards").fetchall()
        return [self._row_to_card(r) for r in rows]

    def _row_to_card(self, row: tuple) -> SemanticCard:
        """将数据库行转换为SemanticCard对象"""
        return SemanticCard(
            field_id=row[0],
            business_name=row[1],
            description=row[2],
            category=row[3],
            unit=row[4],
            notes=row[5],
            confidence=row[6],
            verified=bool(row[7]),
            updated_at=row[8],
        )

    # ---- Schema缓存操作 ----

    def save_table_info(self, table: TableInfo):
        """保存表结构扫描结果"""
        columns_json = json.dumps(
            [col.model_dump() for col in table.columns], ensure_ascii=False
        )
        self.conn.execute(
            """INSERT OR REPLACE INTO table_schemas
               (db_id, table_name, columns_json, row_count, comment, scanned_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                table.db_id,
                table.table_name,
                columns_json,
                table.row_count,
                table.comment,
                datetime.now().isoformat(),
            ),
        )
        self.conn.commit()

    def get_table_info(self, db_id: str, table_name: str) -> TableInfo | None:
        """查询单张表的schema缓存"""
        row = self.conn.execute(
            "SELECT * FROM table_schemas WHERE db_id = ? AND table_name = ?",
            (db_id, table_name),
        ).fetchone()
        if not row:
            return None
        return self._row_to_table(row)

    def get_all_tables(self, db_id: str | None = None) -> list[TableInfo]:
        """获取所有已扫描的表信息

        Args:
            db_id: 可选，指定只返回某个数据库的表
        """
        if db_id:
            rows = self.conn.execute(
                "SELECT * FROM table_schemas WHERE db_id = ?", (db_id,)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM table_schemas").fetchall()
        return [self._row_to_table(row) for row in rows]

    def _row_to_table(self, row: tuple) -> TableInfo:
        """将数据库行转换为TableInfo对象"""
        columns = [ColumnInfo(**c) for c in json.loads(row[2])]
        return TableInfo(
            db_id=row[0],
            table_name=row[1],
            columns=columns,
            row_count=row[3],
            comment=row[4],
        )

    def close(self):
        """关闭数据库连接"""
        self.conn.close()

    # ---- 冲突检测存储操作 ----

    def store_conflicts(self, conflicts: list[dict]):
        """存储冲突检测结果到SQLite"""
        self.conn.execute("""
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
        self.conn.execute("DELETE FROM schema_conflicts WHERE confirmed = 0")
        for c in conflicts:
            self.conn.execute(
                """INSERT INTO schema_conflicts
                   (conflict_type, name, details_json, severity)
                   VALUES (?, ?, ?, ?)""",
                (c["conflict_type"], c["name"],
                 json.dumps(c, ensure_ascii=False), c["severity"])
            )
        self.conn.commit()

    def get_conflicts(self, unconfirmed_only=False) -> list[dict]:
        """获取冲突列表"""
        self.conn.row_factory = sqlite3.Row
        sql = "SELECT * FROM schema_conflicts"
        if unconfirmed_only:
            sql += " WHERE confirmed = 0"
        sql += " ORDER BY severity DESC, created_at DESC"
        try:
            rows = self.conn.execute(sql).fetchall()
            result = [dict(r) for r in rows]
        except sqlite3.OperationalError:
            # 表不存在时返回空列表
            result = []
        finally:
            self.conn.row_factory = None
        return result

    def confirm_conflict(self, conflict_id: int, warning_text: str):
        """
        人工确认冲突，写入warning文本。
        warning_text会被注入到相关字段的语义名片里。
        """
        self.conn.execute(
            """UPDATE schema_conflicts
               SET confirmed=1, warning_text=?
               WHERE id=?""",
            (warning_text, conflict_id)
        )
        self.conn.commit()

    def get_warnings_for_field(self, field_id: str) -> list[str]:
        """
        查询某个字段关联的所有warning文本，
        供构建LLM context时注入。
        """
        try:
            self.conn.row_factory = sqlite3.Row
            rows = self.conn.execute(
                "SELECT warning_text, details_json FROM schema_conflicts WHERE confirmed=1"
            ).fetchall()
            self.conn.row_factory = None
        except sqlite3.OperationalError:
            return []
        warnings = []
        for row in rows:
            warning_text = row["warning_text"]
            if warning_text and field_id in (row["details_json"] or ""):
                warnings.append(warning_text)
        return warnings
