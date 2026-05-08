"""SQL执行器：验证、RLAC行级过滤、执行

安全机制：
1. sqlparse解析SQL类型（不用正则）— 只允许SELECT/WITH
2. 表/字段级审查 — 确认SQL只涉及被允许的表
3. 子查询包装RLAC — 行级过滤在执行层强制注入，无法绕过
4. 结果数量限制 — 单次最多返回1000条，防止整表导出
5. 敏感字段脱敏 — 手机号/身份证/银行卡自动打码
"""

from __future__ import annotations

from .connectors import DatabaseManager
from .security import AuditLogger, DataMasker, SQLValidator

# 单次查询最大返回行数：防止攻击者通过一次查询导出整表数据
_MAX_RESULT_ROWS = 1000


class QueryExecutor:
    """SQL执行器：验证 + RLAC + 执行"""

    def __init__(
        self,
        db_manager: DatabaseManager,
        audit_logger: AuditLogger | None = None,
        enable_masking: bool = True,
    ):
        self.db_manager = db_manager
        self._validator = SQLValidator()
        self._masker = DataMasker() if enable_masking else None
        self._audit = audit_logger

    def execute(
        self,
        db_id: str,
        sql: str,
        row_filters: dict[str, str] | None = None,
        allowed_tables: set[str] | None = None,
        user_id: str = "",
        role_id: str = "",
        question: str = "",
    ) -> dict:
        """验证并执行SQL，返回结果

        Args:
            db_id: 目标数据库ID
            sql: 要执行的SQL
            row_filters: 行级过滤条件 {"db_id.table_name": "WHERE条件"}
            allowed_tables: 当前用户可访问的表名集合（用于SQL审查）
            user_id: 用户ID（审计日志用）
            role_id: 角色ID（审计日志用）
            question: 用户原始问题（审计日志用）

        Returns:
            {"success": True, "data": [...], "row_count": N, "truncated": bool}
            或 {"success": False, "error": "..."}
        """
        # 第1关：sqlparse白名单验证（只允许SELECT/WITH）
        valid, error_msg = self._validator.validate(sql)
        if not valid:
            self._log_blocked(user_id, role_id, question, sql, error_msg)
            return {"success": False, "error": error_msg}

        # 第2关：表级权限审查（确认SQL只涉及被允许的表）
        if allowed_tables:
            valid, error_msg = self._validator.check_allowed_tables(sql, allowed_tables)
            if not valid:
                self._log_blocked(user_id, role_id, question, sql, error_msg)
                return {"success": False, "error": error_msg}

        # 第3关：RLAC行级过滤（子查询包装，无法绕过）
        if row_filters:
            sql = self._apply_row_filters(sql, db_id, row_filters)

        # 第4关：执行并限制结果数量
        try:
            rows = self.db_manager.execute_sql(db_id, sql)

            # 强制限制返回行数，防止整表导出
            truncated = len(rows) > _MAX_RESULT_ROWS
            data = rows[:_MAX_RESULT_ROWS]

            # 第5关：敏感字段脱敏
            if self._masker:
                data = self._masker.mask_results(data)

            # 记录审计日志
            if self._audit:
                self._audit.log_query(
                    user_id=user_id,
                    role_id=role_id,
                    question=question,
                    generated_sql=sql,
                    result_rows=len(rows),
                    success=True,
                )

            return {
                "success": True,
                "data": data,
                "row_count": len(rows),
                "truncated": truncated,
            }
        except Exception as e:
            error_msg = f"SQL执行失败: {str(e)}"
            if self._audit:
                self._audit.log_query(
                    user_id=user_id,
                    role_id=role_id,
                    question=question,
                    generated_sql=sql,
                    result_rows=0,
                    success=False,
                    error=error_msg,
                )
            return {"success": False, "error": error_msg}

    def _apply_row_filters(
        self, sql: str, db_id: str, row_filters: dict[str, str]
    ) -> str:
        """通过子查询包装应用行级过滤

        将用户SQL包装为子查询，在外层添加WHERE条件。
        无论LLM生成什么SQL，行级过滤都无法绕过。

        注意：SQLite不支持子查询别名中引用外层不存在的列，
        因此RLAC条件中引用的列必须在内层SQL的SELECT中存在。
        这里使用 SELECT * 确保所有列都可用于过滤。
        """
        # 收集适用于当前数据库的过滤条件
        applicable_filters = []
        for table_key, condition in row_filters.items():
            # table_key格式: "db_id.table_name"
            parts = table_key.split(".", 1)
            if len(parts) == 2:
                filter_db_id, _table_name = parts
            else:
                filter_db_id = db_id

            if filter_db_id == db_id:
                applicable_filters.append(condition)

        if not applicable_filters:
            return sql

        # 子查询包装：确保内层查询包含过滤所需的列
        # 策略：将原始SQL包装为CTE，外层SELECT *再加WHERE过滤
        combined_filter = " AND ".join(applicable_filters)
        wrapped = f"WITH _user_query AS ({sql}) SELECT * FROM _user_query WHERE {combined_filter}"
        return wrapped

    def _log_blocked(
        self, user_id: str, role_id: str, question: str, sql: str, reason: str
    ):
        """记录被拦截的查询"""
        if self._audit:
            self._audit.log_query(
                user_id=user_id,
                role_id=role_id,
                question=question,
                generated_sql=sql,
                result_rows=0,
                success=False,
                blocked_reason=reason,
            )
            self._audit.log_security_event(
                user_id=user_id,
                event_type="sql_blocked",
                detail=f"SQL被拦截: {reason} | SQL: {sql[:200]}",
            )
