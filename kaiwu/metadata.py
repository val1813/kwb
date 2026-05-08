"""SQLite元数据存储：语义名片、schema缓存"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .models import ColumnInfo, SemanticCard, TableInfo


class MetadataStore:
    """SQLite元数据存储"""

    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_tables()

    def _init_tables(self):
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

    # ---- 语义名片 ----

    def save_card(self, card: SemanticCard):
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
        row = self.conn.execute(
            "SELECT * FROM semantic_cards WHERE field_id = ?", (field_id,)
        ).fetchone()
        if not row:
            return None
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

    def get_cards_for_table(self, db_id: str, table_name: str) -> list[SemanticCard]:
        prefix = f"{db_id}.{table_name}."
        rows = self.conn.execute(
            "SELECT * FROM semantic_cards WHERE field_id LIKE ?", (prefix + "%",)
        ).fetchall()
        return [
            SemanticCard(
                field_id=r[0],
                business_name=r[1],
                description=r[2],
                category=r[3],
                unit=r[4],
                notes=r[5],
                confidence=r[6],
                verified=bool(r[7]),
                updated_at=r[8],
            )
            for r in rows
        ]

    def get_all_cards(self) -> list[SemanticCard]:
        rows = self.conn.execute("SELECT * FROM semantic_cards").fetchall()
        return [
            SemanticCard(
                field_id=r[0],
                business_name=r[1],
                description=r[2],
                category=r[3],
                unit=r[4],
                notes=r[5],
                confidence=r[6],
                verified=bool(r[7]),
                updated_at=r[8],
            )
            for r in rows
        ]

    # ---- Schema缓存 ----

    def save_table_info(self, table: TableInfo):
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
        row = self.conn.execute(
            "SELECT * FROM table_schemas WHERE db_id = ? AND table_name = ?",
            (db_id, table_name),
        ).fetchone()
        if not row:
            return None
        columns = [ColumnInfo(**c) for c in json.loads(row[2])]
        return TableInfo(
            db_id=row[0],
            table_name=row[1],
            columns=columns,
            row_count=row[3],
            comment=row[4],
        )

    def get_all_tables(self, db_id: str | None = None) -> list[TableInfo]:
        if db_id:
            rows = self.conn.execute(
                "SELECT * FROM table_schemas WHERE db_id = ?", (db_id,)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM table_schemas").fetchall()
        results = []
        for row in rows:
            columns = [ColumnInfo(**c) for c in json.loads(row[2])]
            results.append(
                TableInfo(
                    db_id=row[0],
                    table_name=row[1],
                    columns=columns,
                    row_count=row[3],
                    comment=row[4],
                )
            )
        return results

    def close(self):
        self.conn.close()
