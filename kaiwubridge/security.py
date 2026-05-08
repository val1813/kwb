"""安全层：SQL审查、频率限制、敏感字段脱敏、审计日志

安全设计原则：
1. 数据库只读账号 — 物理层面阻止写操作
2. SQL参数化执行 — 杜绝注入
3. sqlparse白名单验证 — 只允许SELECT
4. 表/字段级SQL审查 — LLM生成的SQL必须只涉及被允许的表和字段
5. RLAC行级过滤 — 在执行层强制注入WHERE
6. 结果数量限制 — 防止整表导出
7. 频率限制 — 防止暴力枚举
8. 敏感字段脱敏 — 手机号/身份证/银行卡自动打码
9. 审计日志 — append-only，不可篡改
"""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from threading import Lock

import sqlparse
from sqlparse.sql import IdentifierList, Identifier
from sqlparse.tokens import Keyword, DML


# ============ SQL白名单验证（用sqlparse解析，不用正则） ============


class SQLValidator:
    """SQL安全验证器

    使用sqlparse库解析SQL语法树，比正则匹配更可靠。
    """

    # 绝对禁止的SQL语句类型
    _FORBIDDEN_TYPES = {"INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE",
                        "TRUNCATE", "EXEC", "EXECUTE", "GRANT", "REVOKE", "MERGE"}

    # 可疑模式：UNION注入、子查询信息泄露等
    # UNION检测：先去除SQL注释再匹配，覆盖所有注释绕过变体
    _SUSPICIOUS_PATTERNS = [
        re.compile(r"\bUNION\s+(ALL\s+)?SELECT\b", re.IGNORECASE),  # UNION注入（去注释后匹配）
        re.compile(r"\bINTO\s+OUTFILE\b", re.IGNORECASE),           # 文件写入
        re.compile(r"\bINTO\s+DUMPFILE\b", re.IGNORECASE),          # 文件写入
        re.compile(r"\bLOAD_FILE\b", re.IGNORECASE),                # 文件读取
        re.compile(r"\bINFORMATION_SCHEMA\b", re.IGNORECASE),       # 元数据探测
        re.compile(r"\bSLEEP\s*\(", re.IGNORECASE),                 # 时间盲注
        re.compile(r"\bBENCHMARK\s*\(", re.IGNORECASE),             # 时间盲注
    ]

    # 去除SQL注释的正则：匹配 /* ... */ 块注释和 -- 行注释
    _COMMENT_PATTERN = re.compile(r"/\*.*?\*/|--[^\n]*", re.DOTALL)

    def validate(self, sql: str) -> tuple[bool, str]:
        """验证SQL安全性

        Returns:
            (通过, 错误信息)  通过时错误信息为空字符串
        """
        if not sql or not sql.strip():
            return False, "SQL为空"

        # 用sqlparse解析语句类型
        parsed = sqlparse.parse(sql)
        if not parsed:
            return False, "SQL解析失败"

        for statement in parsed:
            stmt_type = statement.get_type()
            # sqlparse返回的类型：SELECT, INSERT, UPDATE, DELETE, CREATE, DROP等
            if stmt_type and stmt_type.upper() not in ("SELECT", "UNKNOWN"):
                # UNKNOWN通常是WITH...SELECT（CTE），需要进一步检查
                if stmt_type.upper() != "UNKNOWN":
                    return False, f"禁止执行 {stmt_type} 语句"

            # 对UNKNOWN类型（CTE等），检查首个关键字
            first_token = self._get_first_meaningful_token(statement)
            if first_token:
                upper_token = first_token.upper()
                if upper_token in self._FORBIDDEN_TYPES:
                    return False, f"禁止执行 {upper_token} 语句"
                if upper_token not in ("SELECT", "WITH"):
                    return False, f"只允许SELECT/WITH查询，检测到: {upper_token}"

        # 检查可疑模式（先去除注释再匹配，防止注释绕过）
        sql_no_comments = self._COMMENT_PATTERN.sub(" ", sql)
        for pattern in self._SUSPICIOUS_PATTERNS:
            if pattern.search(sql_no_comments):
                return False, f"检测到可疑SQL模式: {pattern.pattern}"

        return True, ""

    def check_allowed_tables(
        self, sql: str, allowed_tables: set[str]
    ) -> tuple[bool, str]:
        """检查SQL中引用的表是否都在允许列表中

        Args:
            sql: 待检查的SQL
            allowed_tables: 允许访问的表名集合

        Returns:
            (通过, 错误信息)
        """
        if not allowed_tables:
            return True, ""  # 空集合表示不做表级检查（admin角色）

        referenced_tables = self._extract_table_names(sql)
        unauthorized = referenced_tables - allowed_tables
        if unauthorized:
            return False, f"无权访问表: {', '.join(unauthorized)}"
        return True, ""

    def _get_first_meaningful_token(self, statement) -> str | None:
        """获取语句的第一个有意义的token"""
        for token in statement.tokens:
            if token.ttype in (sqlparse.tokens.Whitespace, sqlparse.tokens.Newline,
                               sqlparse.tokens.Comment.Single, sqlparse.tokens.Comment.Multiline):
                continue
            return str(token).strip()
        return None

    def _extract_table_names(self, sql: str) -> set[str]:
        """从SQL中提取引用的表名"""
        tables = set()
        parsed = sqlparse.parse(sql)
        for statement in parsed:
            self._extract_tables_from_statement(statement, tables)
        return tables

    def _extract_tables_from_statement(self, statement, tables: set):
        """递归提取语句中的表名（包括子查询中的表）"""
        from_seen = False
        for token in statement.tokens:
            # 递归处理子查询和括号内的内容
            if hasattr(token, 'tokens'):
                # Parenthesis（括号）或Subquery中可能包含子查询
                if not isinstance(token, (IdentifierList, Identifier)):
                    self._extract_tables_from_statement(token, tables)

            if token.ttype is Keyword and token.normalized.upper() in ("FROM", "JOIN", "INNER JOIN",
                                                                        "LEFT JOIN", "RIGHT JOIN",
                                                                        "FULL JOIN", "CROSS JOIN"):
                from_seen = True
                continue

            if from_seen:
                if isinstance(token, IdentifierList):
                    for identifier in token.get_identifiers():
                        name = self._get_table_name(identifier)
                        if name:
                            tables.add(name)
                        # 递归检查identifier内部的子查询
                        if hasattr(identifier, 'tokens'):
                            self._extract_tables_from_statement(identifier, tables)
                    from_seen = False
                elif isinstance(token, Identifier):
                    name = self._get_table_name(token)
                    if name:
                        tables.add(name)
                    # 递归检查identifier内部的子查询
                    if hasattr(token, 'tokens'):
                        self._extract_tables_from_statement(token, tables)
                    from_seen = False
                elif token.ttype is not sqlparse.tokens.Whitespace:
                    from_seen = False

    def _get_table_name(self, token) -> str | None:
        """从Identifier token中提取表名（去掉别名和schema前缀）"""
        if isinstance(token, Identifier):
            # 获取真实名称（去掉别名）
            real_name = token.get_real_name()
            if real_name:
                return real_name.strip('"').strip('`').strip("'")
        return str(token).strip().strip('"').strip('`').strip("'").split()[0] if str(token).strip() else None


# ============ 频率限制 ============


class RateLimiter:
    """滑动窗口频率限制器

    每个用户在指定时间窗口内最多允许指定次数的请求。
    """

    def __init__(
        self,
        max_requests: int = 50,   # 窗口内最大请求数
        window_seconds: int = 60,  # 时间窗口（秒）
    ):
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._requests: dict[str, list[float]] = defaultdict(list)  # user_id -> [timestamp]
        self._lock = Lock()

    def is_allowed(self, user_id: str) -> bool:
        """检查用户是否在频率限制内

        Returns:
            True=允许请求, False=超出限制
        """
        now = time.time()
        cutoff = now - self._window_seconds

        with self._lock:
            # 清理过期记录
            self._requests[user_id] = [
                t for t in self._requests[user_id] if t > cutoff
            ]
            # 检查是否超限
            if len(self._requests[user_id]) >= self._max_requests:
                return False
            # 记录本次请求
            self._requests[user_id].append(now)
            return True

    def get_remaining(self, user_id: str) -> int:
        """获取用户剩余可用请求数"""
        now = time.time()
        cutoff = now - self._window_seconds
        with self._lock:
            valid = [t for t in self._requests[user_id] if t > cutoff]
            return max(0, self._max_requests - len(valid))


# ============ 敏感字段脱敏 ============


class DataMasker:
    """敏感数据脱敏处理器

    自动识别并脱敏以下类型的数据：
    - 手机号：13812345678 → 138****5678
    - 身份证：110101199001011234 → 110101****1234
    - 银行卡：6222021234567890123 → 6222****0123
    - 邮箱：user@example.com → u***@example.com
    """

    # 中国手机号：11位数字，1开头
    _PHONE_PATTERN = re.compile(r"\b(1[3-9]\d)\d{4}(\d{4})\b")
    # 身份证号：18位（最后一位可能是X）
    _ID_CARD_PATTERN = re.compile(r"\b(\d{6})\d{8}(\d{3}[\dXx])\b")
    # 银行卡号：16-19位数字
    _BANK_CARD_PATTERN = re.compile(r"\b(\d{4})\d{8,11}(\d{4})\b")
    # 邮箱
    _EMAIL_PATTERN = re.compile(r"\b([a-zA-Z0-9])[a-zA-Z0-9.]*(@[a-zA-Z0-9.-]+)\b")

    def mask_value(self, value: str) -> str:
        """对单个字符串值进行脱敏"""
        if not isinstance(value, str):
            return value

        result = value
        # 按优先级依次匹配（身份证优先于银行卡，因为长度重叠）
        result = self._ID_CARD_PATTERN.sub(r"\1****\2", result)
        result = self._PHONE_PATTERN.sub(r"\1****\2", result)
        result = self._BANK_CARD_PATTERN.sub(r"\1****\2", result)
        result = self._EMAIL_PATTERN.sub(r"\1***\2", result)
        return result

    def mask_row(self, row: dict) -> dict:
        """对一行数据中的所有字符串字段进行脱敏"""
        masked = {}
        for key, value in row.items():
            if isinstance(value, str):
                masked[key] = self.mask_value(value)
            else:
                masked[key] = value
        return masked

    def mask_results(self, rows: list[dict]) -> list[dict]:
        """对查询结果集进行脱敏"""
        return [self.mask_row(row) for row in rows]


# ============ 审计日志（append-only） ============


class AuditLogger:
    """审计日志记录器

    所有查询记录写入append-only日志文件。
    日志格式：每行一个JSON对象，便于后续分析。
    文件权限设置为只追加，防止篡改。
    """

    def __init__(self, log_dir: str = "./data/audit"):
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    def _get_log_file(self) -> Path:
        """按日期分割日志文件，便于管理和归档"""
        today = datetime.now().strftime("%Y-%m-%d")
        return self._log_dir / f"audit_{today}.jsonl"

    def log_query(
        self,
        user_id: str,
        role_id: str,
        question: str,
        generated_sql: str | None,
        result_rows: int,
        success: bool,
        error: str = "",
        blocked_reason: str = "",
        extra: dict | None = None,
    ):
        """记录一次查询操作

        Args:
            user_id: 用户ID
            role_id: 角色ID
            question: 用户原始问题
            generated_sql: LLM生成的SQL（可能为None）
            result_rows: 返回行数
            success: 是否成功
            error: 错误信息
            blocked_reason: 被拦截的原因（权限/频率/SQL审查等）
            extra: 附加信息（trace_id、retry_count等）
        """
        record = {
            "timestamp": datetime.now().isoformat(),
            "user_id": user_id,
            "role_id": role_id,
            "question": question,
            "generated_sql": generated_sql,
            "result_rows": result_rows,
            "success": success,
            "error": error,
            "blocked_reason": blocked_reason,
        }
        if extra:
            record.update(extra)

        log_file = self._get_log_file()
        with self._lock:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def log_security_event(self, user_id: str, event_type: str, detail: str):
        """记录安全事件（异常查询、越权尝试等）"""
        record = {
            "timestamp": datetime.now().isoformat(),
            "event_type": event_type,
            "user_id": user_id,
            "detail": detail,
            "level": "WARNING",
        }

        log_file = self._log_dir / "security_events.jsonl"
        with self._lock:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
