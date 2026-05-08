"""跨库映射表管理模块

职责：
- SQLite持久化存储字段映射关系（field_mappings表）
- 映射状态管理（pending/confirmed/rejected）
- 持续学习确认记录存储（confirmations表）
- 提供映射关系的CRUD接口

设计决策：
- 继承MetadataStore的SQLite+WAL模式：零部署、并发读友好
- 独立数据库文件：映射数据与元数据解耦，便于独立备份和迁移
- 状态机设计：pending → confirmed/rejected，不可逆转（审计需求）
- confirmations表独立存储：为持续学习提供训练数据，不污染映射主表
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

# ============ 模块级常量 ============

# 映射状态枚举值
STATUS_PENDING = "pending"        # 待审核
STATUS_CONFIRMED = "confirmed"    # 已确认
STATUS_REJECTED = "rejected"      # 已拒绝

# 映射方法标识
METHOD_AUTO = "auto"              # 自动匹配（语义+分布）
METHOD_MANUAL = "manual"          # 人工指定
METHOD_RULE = "rule"              # 规则匹配（如字段名完全相同）


class MappingStore:
    """跨库字段映射存储

    管理字段之间的映射关系，支持：
    1. 映射CRUD - 创建、查询、更新、删除映射
    2. 状态流转 - pending → confirmed/rejected
    3. 确认记录 - 存储用户反馈，供持续学习使用
    4. 查询上下文 - 在SQL生成时提供跨库字段对应关系

    表结构设计遵循MetadataStore的模式：SQLite + WAL + check_same_thread=False
    """

    def __init__(self, db_path: str):
        """初始化映射存储

        Args:
            db_path: SQLite数据库文件路径，不存在时自动创建
        """
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        # WAL模式：允许并发读取，写入性能更好
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_tables()

    def _init_tables(self):
        """初始化数据库表结构"""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS field_mappings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                field_a_id TEXT NOT NULL,
                field_b_id TEXT NOT NULL,
                canonical_name TEXT DEFAULT '',
                confidence REAL DEFAULT 0.0,
                method TEXT DEFAULT 'auto',
                status TEXT DEFAULT 'pending',
                created_at TEXT NOT NULL,
                verified_at TEXT,
                notes TEXT DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_mappings_field_a
                ON field_mappings(field_a_id);
            CREATE INDEX IF NOT EXISTS idx_mappings_field_b
                ON field_mappings(field_b_id);
            CREATE INDEX IF NOT EXISTS idx_mappings_status
                ON field_mappings(status);

            CREATE TABLE IF NOT EXISTS confirmations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                field_a_id TEXT NOT NULL,
                field_b_id TEXT NOT NULL,
                confirmed INTEGER NOT NULL,
                category_pair TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_confirmations_category
                ON confirmations(category_pair);
        """)
        self._conn.commit()

    # ============ 映射CRUD操作 ============

    def save_mapping(
        self,
        field_a_id: str,
        field_b_id: str,
        canonical_name: str = "",
        confidence: float = 0.0,
        method: str = METHOD_AUTO,
        status: str = STATUS_PENDING,
        notes: str = "",
    ) -> int:
        """保存新的字段映射

        Args:
            field_a_id: 字段A的ID（格式：db_id.table.column）
            field_b_id: 字段B的ID
            canonical_name: 统一业务名称（如"订单金额"）
            confidence: 匹配置信度 0.0~1.0
            method: 匹配方法（auto/manual/rule）
            status: 初始状态（默认pending）
            notes: 备注信息

        Returns:
            新创建的映射ID
        """
        cursor = self._conn.execute(
            """INSERT INTO field_mappings
               (field_a_id, field_b_id, canonical_name, confidence, method, status, created_at, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                field_a_id,
                field_b_id,
                canonical_name,
                confidence,
                method,
                status,
                datetime.now().isoformat(),
                notes,
            ),
        )
        self._conn.commit()
        return cursor.lastrowid

    def get_mapping(self, mapping_id: int) -> dict | None:
        """按ID查询单条映射

        Args:
            mapping_id: 映射记录ID

        Returns:
            映射字典，不存在时返回None
        """
        row = self._conn.execute(
            "SELECT * FROM field_mappings WHERE id = ?", (mapping_id,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_mapping(row)

    def get_mappings_for_field(self, field_id: str) -> list[dict]:
        """查询某个字段参与的所有映射

        同时搜索field_a_id和field_b_id，返回该字段的所有关联映射。

        Args:
            field_id: 字段ID

        Returns:
            映射字典列表
        """
        rows = self._conn.execute(
            """SELECT * FROM field_mappings
               WHERE field_a_id = ? OR field_b_id = ?
               ORDER BY confidence DESC""",
            (field_id, field_id),
        ).fetchall()
        return [self._row_to_mapping(r) for r in rows]

    def get_all_mappings(self, status: str | None = None) -> list[dict]:
        """获取所有映射记录

        Args:
            status: 可选状态过滤（pending/confirmed/rejected）

        Returns:
            映射字典列表，按创建时间降序
        """
        if status:
            rows = self._conn.execute(
                "SELECT * FROM field_mappings WHERE status = ? ORDER BY created_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM field_mappings ORDER BY created_at DESC"
            ).fetchall()
        return [self._row_to_mapping(r) for r in rows]

    def get_pending_mappings(self) -> list[dict]:
        """获取所有待审核的映射

        Returns:
            状态为pending的映射列表，按置信度降序（高置信优先审核）
        """
        rows = self._conn.execute(
            """SELECT * FROM field_mappings
               WHERE status = ?
               ORDER BY confidence DESC""",
            (STATUS_PENDING,),
        ).fetchall()
        return [self._row_to_mapping(r) for r in rows]

    def confirm_mapping(self, mapping_id: int, notes: str = "") -> bool:
        """确认映射（pending → confirmed）

        Args:
            mapping_id: 映射记录ID
            notes: 审核备注

        Returns:
            是否成功（映射不存在或状态非pending时返回False）
        """
        return self._update_status(mapping_id, STATUS_CONFIRMED, notes)

    def reject_mapping(self, mapping_id: int, notes: str = "") -> bool:
        """拒绝映射（pending → rejected）

        Args:
            mapping_id: 映射记录ID
            notes: 拒绝原因

        Returns:
            是否成功
        """
        return self._update_status(mapping_id, STATUS_REJECTED, notes)

    def _update_status(self, mapping_id: int, new_status: str, notes: str) -> bool:
        """更新映射状态（内部方法）

        只允许从pending状态流转，防止已确认/已拒绝的映射被意外修改。
        """
        cursor = self._conn.execute(
            """UPDATE field_mappings
               SET status = ?, verified_at = ?, notes = CASE WHEN ? != '' THEN ? ELSE notes END
               WHERE id = ? AND status = ?""",
            (
                new_status,
                datetime.now().isoformat(),
                notes,
                notes,
                mapping_id,
                STATUS_PENDING,
            ),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    # ============ 确认记录操作（持续学习数据） ============

    def save_confirmation(
        self,
        field_a_id: str,
        field_b_id: str,
        confirmed: bool,
        category_pair: str = "",
    ) -> int:
        """保存用户确认/拒绝记录

        独立于映射状态管理，专门为持续学习引擎提供训练数据。
        category_pair用于按分类统计学习效果（如"金额-金额"、"编码-编码"）。

        Args:
            field_a_id: 字段A的ID
            field_b_id: 字段B的ID
            confirmed: True=确认匹配正确, False=拒绝
            category_pair: 分类对标识（如"金额-金额"）

        Returns:
            新记录ID
        """
        cursor = self._conn.execute(
            """INSERT INTO confirmations
               (field_a_id, field_b_id, confirmed, category_pair, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                field_a_id,
                field_b_id,
                int(confirmed),
                category_pair,
                datetime.now().isoformat(),
            ),
        )
        self._conn.commit()
        return cursor.lastrowid

    def get_confirmations_by_category(self, category_pair: str) -> list[dict]:
        """按分类对查询确认记录

        用于分析特定类型字段的匹配准确率，指导阈值调整。

        Args:
            category_pair: 分类对标识

        Returns:
            确认记录列表
        """
        rows = self._conn.execute(
            """SELECT * FROM confirmations
               WHERE category_pair = ?
               ORDER BY created_at DESC""",
            (category_pair,),
        ).fetchall()
        return [self._row_to_confirmation(r) for r in rows]

    def get_confirmation_stats(self) -> dict:
        """获取确认记录的统计信息

        Returns:
            统计字典：总数、确认数、拒绝数、按分类的准确率
        """
        total = self._conn.execute(
            "SELECT COUNT(*) FROM confirmations"
        ).fetchone()[0]
        confirmed_count = self._conn.execute(
            "SELECT COUNT(*) FROM confirmations WHERE confirmed = 1"
        ).fetchone()[0]
        rejected_count = total - confirmed_count

        # 按分类统计准确率
        category_stats = {}
        rows = self._conn.execute(
            """SELECT category_pair, COUNT(*) as total,
                      SUM(confirmed) as confirmed_count
               FROM confirmations
               WHERE category_pair != ''
               GROUP BY category_pair"""
        ).fetchall()
        for row in rows:
            cat, cat_total, cat_confirmed = row
            category_stats[cat] = {
                "total": cat_total,
                "confirmed": cat_confirmed,
                "accuracy": round(cat_confirmed / cat_total, 4) if cat_total > 0 else 0.0,
            }

        return {
            "total": total,
            "confirmed": confirmed_count,
            "rejected": rejected_count,
            "accuracy": round(confirmed_count / total, 4) if total > 0 else 0.0,
            "by_category": category_stats,
        }

    # ============ 查询上下文接口 ============

    def get_mapping_context(self, field_ids: list[str]) -> str:
        """生成映射关系的文本描述，用于注入LLM查询上下文

        在SQL生成时，将已确认的跨库映射关系作为上下文提供给LLM，
        帮助其理解不同数据库中字段的对应关系。

        Args:
            field_ids: 当前查询涉及的字段ID列表

        Returns:
            格式化的映射关系描述文本
        """
        if not field_ids:
            return ""

        # 查找涉及这些字段的已确认映射
        placeholders = ",".join(["?"] * len(field_ids))
        rows = self._conn.execute(
            f"""SELECT * FROM field_mappings
                WHERE status = ?
                AND (field_a_id IN ({placeholders}) OR field_b_id IN ({placeholders}))""",
            [STATUS_CONFIRMED] + field_ids + field_ids,
        ).fetchall()

        if not rows:
            return ""

        # 构建上下文文本
        lines = ["[跨库字段映射关系]"]
        for row in rows:
            mapping = self._row_to_mapping(row)
            canonical = mapping["canonical_name"] or "未命名"
            lines.append(
                f"- {mapping['field_a_id']} ↔ {mapping['field_b_id']} "
                f"(统一名称: {canonical}, 置信度: {mapping['confidence']:.2f})"
            )
        return "\n".join(lines)

    # ============ 内部工具方法 ============

    def _row_to_mapping(self, row: tuple) -> dict:
        """将数据库行转换为映射字典"""
        return {
            "id": row[0],
            "field_a_id": row[1],
            "field_b_id": row[2],
            "canonical_name": row[3],
            "confidence": row[4],
            "method": row[5],
            "status": row[6],
            "created_at": row[7],
            "verified_at": row[8],
            "notes": row[9],
        }

    def _row_to_confirmation(self, row: tuple) -> dict:
        """将数据库行转换为确认记录字典"""
        return {
            "id": row[0],
            "field_a_id": row[1],
            "field_b_id": row[2],
            "confirmed": bool(row[3]),
            "category_pair": row[4],
            "created_at": row[5],
        }

    def close(self):
        """关闭数据库连接"""
        self._conn.close()
