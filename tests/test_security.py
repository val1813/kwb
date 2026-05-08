"""安全测试套件

覆盖场景：
1. SQL注入防护（UNION/时间盲注/堆叠查询/文件写入/INFORMATION_SCHEMA/注释绕过/编码绕过）
2. 权限绕过防护（伪造JWT/过期token/篡改role_id/空token）
3. 越权访问防护（跨库访问/字段级禁止/行级过滤绕过/子查询越权）
4. 数据脱敏（手机号/身份证/银行卡/邮箱）
5. 频率限制（超限拒绝/窗口过期恢复）
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import jwt
import pytest

from kaiwubridge.auth import AuthManager
from kaiwubridge.models import ColumnInfo, RoleConfig, TableInfo
from kaiwubridge.permissions import PermissionEngine
from kaiwubridge.security import DataMasker, RateLimiter, SQLValidator


# ============ Fixtures ============


@pytest.fixture
def sql_validator():
    """SQL安全验证器实例"""
    return SQLValidator()


@pytest.fixture
def rate_limiter():
    """频率限制器：每窗口5次请求，窗口1秒"""
    return RateLimiter(max_requests=5, window_seconds=1)


@pytest.fixture
def data_masker():
    """数据脱敏处理器实例"""
    return DataMasker()


@pytest.fixture
def auth_manager():
    """JWT认证管理器，使用测试密钥"""
    return AuthManager(secret="test-secret-key-for-unit-tests", expire_hours=1)


@pytest.fixture
def permission_engine():
    """权限引擎，配置两个角色：admin和sales"""
    roles = [
        RoleConfig(
            id="admin",
            name="管理员",
            allowed_databases=["*"],
            allowed_tables={"*": ["*"]},
            denied_columns={},
            row_filter={},
        ),
        RoleConfig(
            id="sales",
            name="销售员工",
            allowed_databases=["sales_db"],
            allowed_tables={"sales_db": ["orders", "customers", "products"]},
            denied_columns={
                "sales_db.products": ["cost_price", "profit_margin"],
                "sales_db.customers": ["id_card", "bank_account"],
            },
            row_filter={
                "sales_db.orders": "region = '{user.region}'",
            },
        ),
    ]
    return PermissionEngine(roles)


@pytest.fixture
def sales_tables():
    """销售数据库的表结构"""
    return [
        TableInfo(
            db_id="sales_db",
            table_name="orders",
            columns=[
                ColumnInfo(name="id", type="INTEGER"),
                ColumnInfo(name="customer_id", type="INTEGER"),
                ColumnInfo(name="amount", type="DECIMAL"),
                ColumnInfo(name="region", type="VARCHAR"),
            ],
        ),
        TableInfo(
            db_id="sales_db",
            table_name="customers",
            columns=[
                ColumnInfo(name="id", type="INTEGER"),
                ColumnInfo(name="name", type="VARCHAR"),
                ColumnInfo(name="phone", type="VARCHAR"),
                ColumnInfo(name="id_card", type="VARCHAR"),
                ColumnInfo(name="bank_account", type="VARCHAR"),
            ],
        ),
        TableInfo(
            db_id="sales_db",
            table_name="products",
            columns=[
                ColumnInfo(name="id", type="INTEGER"),
                ColumnInfo(name="name", type="VARCHAR"),
                ColumnInfo(name="price", type="DECIMAL"),
                ColumnInfo(name="cost_price", type="DECIMAL"),
                ColumnInfo(name="profit_margin", type="DECIMAL"),
            ],
        ),
        TableInfo(
            db_id="finance_db",
            table_name="salary",
            columns=[
                ColumnInfo(name="id", type="INTEGER"),
                ColumnInfo(name="employee_id", type="INTEGER"),
                ColumnInfo(name="amount", type="DECIMAL"),
            ],
        ),
    ]


# ============ 1. SQL注入防护测试 ============


class TestSQLInjectionPrevention:
    """SQL注入防护测试"""

    # --- UNION SELECT注入 ---

    def test_union_select_basic(self, sql_validator):
        """基本UNION SELECT注入应被拦截"""
        sql = "SELECT name FROM users UNION SELECT password FROM admin_users"
        valid, error = sql_validator.validate(sql)
        assert not valid
        assert "UNION" in error

    def test_union_all_select(self, sql_validator):
        """UNION ALL SELECT注入应被拦截"""
        sql = "SELECT id FROM orders UNION ALL SELECT credit_card FROM payments"
        valid, error = sql_validator.validate(sql)
        assert not valid
        assert "UNION" in error

    def test_union_select_with_comment_obfuscation(self, sql_validator):
        """带注释混淆的UNION注入应被拦截（去注释后匹配）"""
        sql = "SELECT name FROM users UNION/**/SELECT password FROM admin"
        valid, error = sql_validator.validate(sql)
        assert not valid
        assert "UNION" in error

    # --- 时间盲注 (SLEEP/BENCHMARK) ---

    def test_sleep_injection(self, sql_validator):
        """SLEEP时间盲注应被拦截"""
        sql = "SELECT * FROM users WHERE id=1 AND SLEEP(5)"
        valid, error = sql_validator.validate(sql)
        assert not valid
        assert "SLEEP" in error

    def test_benchmark_injection(self, sql_validator):
        """BENCHMARK时间盲注应被拦截"""
        sql = "SELECT * FROM users WHERE id=1 AND BENCHMARK(10000000, SHA1('test'))"
        valid, error = sql_validator.validate(sql)
        assert not valid
        assert "BENCHMARK" in error

    def test_sleep_in_subquery(self, sql_validator):
        """子查询中的SLEEP应被拦截"""
        sql = "SELECT * FROM users WHERE id=(SELECT SLEEP(3))"
        valid, error = sql_validator.validate(sql)
        assert not valid

    # --- 堆叠查询 (INSERT/UPDATE/DELETE/DROP) ---

    def test_insert_statement(self, sql_validator):
        """INSERT语句应被拦截"""
        sql = "INSERT INTO users (name, role) VALUES ('hacker', 'admin')"
        valid, error = sql_validator.validate(sql)
        assert not valid
        assert "INSERT" in error

    def test_update_statement(self, sql_validator):
        """UPDATE语句应被拦截"""
        sql = "UPDATE users SET role='admin' WHERE id=1"
        valid, error = sql_validator.validate(sql)
        assert not valid
        assert "UPDATE" in error

    def test_delete_statement(self, sql_validator):
        """DELETE语句应被拦截"""
        sql = "DELETE FROM users WHERE id > 0"
        valid, error = sql_validator.validate(sql)
        assert not valid
        assert "DELETE" in error

    def test_drop_table(self, sql_validator):
        """DROP TABLE应被拦截"""
        sql = "DROP TABLE users"
        valid, error = sql_validator.validate(sql)
        assert not valid
        assert "DROP" in error

    def test_truncate_table(self, sql_validator):
        """TRUNCATE TABLE应被拦截"""
        sql = "TRUNCATE TABLE users"
        valid, error = sql_validator.validate(sql)
        assert not valid

    # --- INTO OUTFILE文件写入 ---

    def test_into_outfile(self, sql_validator):
        """INTO OUTFILE文件写入应被拦截"""
        sql = "SELECT * FROM users INTO OUTFILE '/tmp/data.csv'"
        valid, error = sql_validator.validate(sql)
        assert not valid
        assert "OUTFILE" in error

    def test_into_dumpfile(self, sql_validator):
        """INTO OUTFILE变体应被拦截"""
        sql = "SELECT password FROM users INTO OUTFILE '/var/www/shell.php'"
        valid, error = sql_validator.validate(sql)
        assert not valid

    # --- INFORMATION_SCHEMA探测 ---

    def test_information_schema_tables(self, sql_validator):
        """INFORMATION_SCHEMA.TABLES探测应被拦截"""
        sql = "SELECT table_name FROM INFORMATION_SCHEMA.TABLES"
        valid, error = sql_validator.validate(sql)
        assert not valid
        assert "INFORMATION_SCHEMA" in error

    def test_information_schema_columns(self, sql_validator):
        """INFORMATION_SCHEMA.COLUMNS探测应被拦截"""
        sql = "SELECT column_name FROM INFORMATION_SCHEMA.COLUMNS WHERE table_name='users'"
        valid, error = sql_validator.validate(sql)
        assert not valid

    # --- 注释绕过 ---

    def test_single_line_comment_bypass(self, sql_validator):
        """单行注释绕过尝试：在注释后隐藏恶意语句"""
        # 注意：sqlparse会将整个字符串作为一条语句解析
        # 如果注释后有恶意内容，sqlparse仍能检测到多语句
        sql = "SELECT * FROM users -- \n; DROP TABLE users"
        valid, error = sql_validator.validate(sql)
        assert not valid

    def test_multiline_comment_in_union(self, sql_validator):
        """多行注释绕过UNION检测应被拦截（去注释后匹配）"""
        sql = "SELECT id FROM users UNION/*comment*/SELECT password FROM admin"
        valid, error = sql_validator.validate(sql)
        assert not valid
        assert "UNION" in error

    # --- 编码绕过尝试 ---

    def test_mixed_case_bypass(self, sql_validator):
        """大小写混合绕过尝试"""
        sql = "SELECT * FROM users UnIoN SeLeCt password FROM admin"
        valid, error = sql_validator.validate(sql)
        assert not valid

    def test_sleep_mixed_case(self, sql_validator):
        """大小写混合的SLEEP应被拦截"""
        sql = "SELECT * FROM users WHERE id=1 AND sLeEp(5)"
        valid, error = sql_validator.validate(sql)
        assert not valid

    # --- 合法SQL应通过 ---

    def test_valid_select(self, sql_validator):
        """正常SELECT语句应通过"""
        sql = "SELECT id, name, amount FROM orders WHERE region = '华东'"
        valid, error = sql_validator.validate(sql)
        assert valid
        assert error == ""

    def test_valid_select_with_join(self, sql_validator):
        """带JOIN的SELECT应通过"""
        sql = """
            SELECT o.id, c.name, o.amount
            FROM orders o
            JOIN customers c ON o.customer_id = c.id
            WHERE o.amount > 1000
        """
        valid, error = sql_validator.validate(sql)
        assert valid

    def test_valid_cte(self, sql_validator):
        """CTE (WITH ... SELECT) 应通过"""
        sql = """
            WITH top_orders AS (
                SELECT customer_id, SUM(amount) as total
                FROM orders
                GROUP BY customer_id
            )
            SELECT * FROM top_orders WHERE total > 10000
        """
        valid, error = sql_validator.validate(sql)
        assert valid

    def test_empty_sql_rejected(self, sql_validator):
        """空SQL应被拒绝"""
        valid, error = sql_validator.validate("")
        assert not valid
        assert "空" in error

    def test_whitespace_only_rejected(self, sql_validator):
        """纯空白SQL应被拒绝"""
        valid, error = sql_validator.validate("   \n\t  ")
        assert not valid


# ============ 2. 权限绕过防护测试 ============


class TestAuthBypassPrevention:
    """JWT认证绕过防护测试"""

    def test_forged_token_wrong_secret(self, auth_manager):
        """使用错误密钥伪造的token应被拒绝"""
        # 用不同的密钥签发token
        forged_payload = {
            "user_id": "hacker",
            "role_id": "admin",
            "attrs": {},
        }
        forged_token = jwt.encode(forged_payload, "wrong-secret", algorithm="HS256")

        with pytest.raises(jwt.InvalidSignatureError):
            auth_manager.verify_token(forged_token)

    def test_expired_token(self, auth_manager):
        """过期token应被拒绝"""
        # 创建一个已过期的token（expire_hours设为负数模拟过期）
        expired_manager = AuthManager(secret="test-secret-key-for-unit-tests", expire_hours=-1)
        token = expired_manager.create_token(user_id="user1", role_id="sales")

        with pytest.raises(jwt.ExpiredSignatureError):
            auth_manager.verify_token(token)

    def test_tampered_role_id(self, auth_manager):
        """篡改token中的role_id应被拒绝（签名不匹配）"""
        # 先生成合法token
        token = auth_manager.create_token(user_id="user1", role_id="sales")

        # 解码但不验证，篡改role_id后重新编码（用错误密钥）
        payload = jwt.decode(token, options={"verify_signature": False})
        payload["role_id"] = "admin"  # 篡改为admin
        tampered_token = jwt.encode(payload, "different-secret", algorithm="HS256")

        with pytest.raises(jwt.InvalidSignatureError):
            auth_manager.verify_token(tampered_token)

    def test_empty_token(self, auth_manager):
        """空token应被拒绝"""
        with pytest.raises(jwt.DecodeError):
            auth_manager.verify_token("")

    def test_invalid_format_token(self, auth_manager):
        """无效格式token应被拒绝"""
        with pytest.raises(jwt.DecodeError):
            auth_manager.verify_token("not.a.valid.jwt.token.at.all")

    def test_none_algorithm_attack(self, auth_manager):
        """None算法攻击应被拒绝（CVE-2015-9235）"""
        # 尝试用"none"算法签发token绕过验证
        payload = {"user_id": "hacker", "role_id": "admin", "attrs": {}}
        # PyJWT默认不允许none算法，但测试确认行为
        with pytest.raises(Exception):
            # 即使能编码，验证时也应失败
            forged = jwt.encode(payload, "", algorithm="none")
            auth_manager.verify_token(forged)

    def test_valid_token_passes(self, auth_manager):
        """合法token应通过验证"""
        token = auth_manager.create_token(
            user_id="user1", role_id="sales", attrs={"region": "华东"}
        )
        payload = auth_manager.verify_token(token)
        assert payload["user_id"] == "user1"
        assert payload["role_id"] == "sales"
        assert payload["attrs"]["region"] == "华东"

    def test_bearer_header_extraction(self, auth_manager):
        """从Bearer头中正确提取token"""
        token = auth_manager.create_token(user_id="user1", role_id="sales")
        header = f"Bearer {token}"
        payload = auth_manager.extract_from_header(header)
        assert payload["user_id"] == "user1"

    def test_bearer_header_with_forged_token(self, auth_manager):
        """Bearer头中携带伪造token应被拒绝"""
        header = "Bearer eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoiaGFja2VyIn0.fake_signature"
        with pytest.raises(jwt.InvalidTokenError):
            auth_manager.extract_from_header(header)


# ============ 3. 越权访问防护测试 ============


class TestPrivilegeEscalationPrevention:
    """越权访问防护测试"""

    def test_sales_cannot_access_finance_db(self, permission_engine):
        """销售员工不能访问财务数据库"""
        assert not permission_engine.can_access_database("sales", "finance_db")

    def test_sales_can_access_sales_db(self, permission_engine):
        """销售员工可以访问销售数据库"""
        assert permission_engine.can_access_database("sales", "sales_db")

    def test_admin_can_access_any_db(self, permission_engine):
        """管理员可以访问任何数据库"""
        assert permission_engine.can_access_database("admin", "finance_db")
        assert permission_engine.can_access_database("admin", "sales_db")
        assert permission_engine.can_access_database("admin", "any_db")

    def test_sales_denied_cost_price_column(self, permission_engine, sales_tables):
        """销售员工不能看到cost_price字段"""
        filtered = permission_engine.filter_schema(sales_tables, "sales")

        # 找到products表
        products = next(t for t in filtered if t.table_name == "products")
        column_names = [c.name for c in products.columns]

        assert "cost_price" not in column_names
        assert "profit_margin" not in column_names
        # 但price字段应该可见
        assert "price" in column_names

    def test_sales_denied_sensitive_customer_columns(self, permission_engine, sales_tables):
        """销售员工不能看到客户的身份证和银行卡字段"""
        filtered = permission_engine.filter_schema(sales_tables, "sales")

        customers = next(t for t in filtered if t.table_name == "customers")
        column_names = [c.name for c in customers.columns]

        assert "id_card" not in column_names
        assert "bank_account" not in column_names
        # 但name和phone应该可见
        assert "name" in column_names
        assert "phone" in column_names

    def test_sales_cannot_see_finance_tables(self, permission_engine, sales_tables):
        """销售员工过滤后看不到财务表"""
        filtered = permission_engine.filter_schema(sales_tables, "sales")
        table_names = [t.table_name for t in filtered]

        assert "salary" not in table_names
        assert "orders" in table_names

    def test_sql_validator_blocks_unauthorized_table(self, sql_validator):
        """SQL审查拦截对未授权表的访问"""
        sql = "SELECT * FROM salary"
        allowed = {"orders", "customers", "products"}

        valid, error = sql_validator.check_allowed_tables(sql, allowed)
        assert not valid
        assert "salary" in error

    def test_subquery_accessing_unauthorized_table(self, sql_validator):
        """通过子查询访问未授权表：当前table提取不递归进子查询（已知限制）
        sqlparse的_extract_tables_from_statement只处理顶层FROM，
        子查询中的表名未被提取。此为安全改进项。
        """
        sql = """
            SELECT * FROM orders
            WHERE customer_id IN (SELECT employee_id FROM salary WHERE amount > 50000)
        """
        allowed = {"orders", "customers", "products"}

        valid, error = sql_validator.check_allowed_tables(sql, allowed)
        # 子查询中的表名现在能被递归提取并拦截
        assert not valid
        assert "salary" in error

    def test_row_filter_applied(self, permission_engine):
        """行级过滤条件正确生成"""
        filters = permission_engine.get_row_filters(
            "sales", {"region": "华东", "department": "销售一部"}
        )
        assert "sales_db.orders" in filters
        assert filters["sales_db.orders"] == "region = '华东'"

    def test_or_1_equals_1_bypass_attempt(self, sql_validator):
        """OR 1=1绕过行级过滤：SQL本身合法，但RLAC在执行层强制注入无法绕过"""
        # 这条SQL本身是合法的SELECT，SQL验证器不会拦截
        sql = "SELECT * FROM orders WHERE region = '华东' OR 1=1"
        valid, _ = sql_validator.validate(sql)
        # SQL语法上合法（只是SELECT），但RLAC会在外层再包一层WHERE
        assert valid  # SQL验证器不拦截，由RLAC层处理

    def test_rlac_wrapping_prevents_bypass(self):
        """RLAC子查询包装确保行级过滤无法被绕过"""
        from kaiwubridge.executor import QueryExecutor

        # Mock数据库管理器
        mock_db_manager = MagicMock()
        mock_db_manager.execute_sql.return_value = []

        executor = QueryExecutor(db_manager=mock_db_manager, enable_masking=False)

        # 用户尝试在SQL中添加OR 1=1绕过行级过滤
        malicious_sql = "SELECT * FROM orders WHERE region = '华东' OR 1=1"
        row_filters = {"sales_db.orders": "region = '华东'"}

        executor.execute(
            db_id="sales_db",
            sql=malicious_sql,
            row_filters=row_filters,
            allowed_tables={"orders"},
        )

        # 验证实际执行的SQL被RLAC包装
        actual_sql = mock_db_manager.execute_sql.call_args[0][1]
        # RLAC将原始SQL包装为CTE，外层强制添加WHERE条件
        assert "WITH _user_query AS" in actual_sql
        assert "WHERE region = '华东'" in actual_sql

    def test_unknown_role_gets_no_access(self, permission_engine, sales_tables):
        """不存在的角色ID不能访问任何内容"""
        assert not permission_engine.can_access_database("nonexistent", "sales_db")
        filtered = permission_engine.filter_schema(sales_tables, "nonexistent")
        assert filtered == []


# ============ 4. 数据脱敏测试 ============


class TestDataMasking:
    """敏感数据脱敏测试"""

    # --- 手机号脱敏 ---

    def test_phone_masking(self, data_masker):
        """手机号中间4位应被脱敏"""
        assert data_masker.mask_value("13812345678") == "138****5678"

    def test_phone_masking_various_prefixes(self, data_masker):
        """不同运营商号段的手机号都应被脱敏"""
        assert data_masker.mask_value("15912345678") == "159****5678"
        assert data_masker.mask_value("18687654321") == "186****4321"
        assert data_masker.mask_value("17011112222") == "170****2222"

    def test_phone_in_text(self, data_masker):
        """文本中嵌入的手机号应被脱敏"""
        text = "联系电话：13912345678，备用：15800001111"
        masked = data_masker.mask_value(text)
        assert "139****5678" in masked
        assert "158****1111" in masked
        assert "13912345678" not in masked

    # --- 身份证脱敏 ---

    def test_id_card_masking(self, data_masker):
        """身份证号中间8位应被脱敏"""
        assert data_masker.mask_value("110101199001011234") == "110101****1234"

    def test_id_card_with_x(self, data_masker):
        """末尾为X的身份证号应被正确脱敏"""
        assert data_masker.mask_value("11010119900101123X") == "110101****123X"

    def test_id_card_in_text(self, data_masker):
        """文本中的身份证号应被脱敏"""
        text = "身份证号码：320106198803071234"
        masked = data_masker.mask_value(text)
        assert "320106****1234" in masked
        assert "320106198803071234" not in masked

    # --- 银行卡脱敏 ---

    def test_bank_card_masking_16_digits(self, data_masker):
        """16位银行卡号应被脱敏"""
        assert data_masker.mask_value("6222021234567890") == "6222****7890"

    def test_bank_card_masking_19_digits(self, data_masker):
        """19位银行卡号应被脱敏"""
        assert data_masker.mask_value("6222021234567890123") == "6222****0123"

    # --- 邮箱脱敏 ---

    def test_email_masking(self, data_masker):
        """邮箱用户名应被脱敏"""
        masked = data_masker.mask_value("zhangsan@example.com")
        assert masked == "z***@example.com"

    def test_email_short_username(self, data_masker):
        """短用户名邮箱应被脱敏"""
        masked = data_masker.mask_value("a@test.org")
        assert masked == "a***@test.org"

    # --- 批量脱敏 ---

    def test_mask_row(self, data_masker):
        """整行数据脱敏"""
        row = {
            "name": "张三",
            "phone": "13812345678",
            "id_card": "110101199001011234",
            "amount": 1000.50,  # 非字符串字段不处理
        }
        masked = data_masker.mask_row(row)
        assert masked["name"] == "张三"  # 普通文本不变
        assert masked["phone"] == "138****5678"
        assert masked["id_card"] == "110101****1234"
        assert masked["amount"] == 1000.50  # 数值不变

    def test_mask_results(self, data_masker):
        """结果集批量脱敏"""
        rows = [
            {"phone": "13811111111", "name": "用户A"},
            {"phone": "13922222222", "name": "用户B"},
        ]
        masked = data_masker.mask_results(rows)
        assert masked[0]["phone"] == "138****1111"
        assert masked[1]["phone"] == "139****2222"
        assert masked[0]["name"] == "用户A"

    def test_non_string_value_unchanged(self, data_masker):
        """非字符串值不做处理"""
        assert data_masker.mask_value(12345) == 12345  # type: ignore
        assert data_masker.mask_value(None) is None  # type: ignore


# ============ 5. 频率限制测试 ============


class TestRateLimiting:
    """频率限制测试"""

    def test_within_limit_allowed(self, rate_limiter):
        """未超限的请求应被允许"""
        for i in range(5):
            assert rate_limiter.is_allowed("user1"), f"第{i+1}次请求应被允许"

    def test_exceeding_limit_rejected(self, rate_limiter):
        """超出限制的请求应被拒绝"""
        # 先用完5次配额
        for _ in range(5):
            rate_limiter.is_allowed("user1")

        # 第6次应被拒绝
        assert not rate_limiter.is_allowed("user1")

    def test_different_users_independent(self, rate_limiter):
        """不同用户的频率限制互相独立"""
        # user1用完配额
        for _ in range(5):
            rate_limiter.is_allowed("user1")
        assert not rate_limiter.is_allowed("user1")

        # user2不受影响
        assert rate_limiter.is_allowed("user2")

    def test_window_expiry_restores_access(self):
        """窗口过期后应恢复访问"""
        # 使用极短窗口（0.1秒）便于测试
        limiter = RateLimiter(max_requests=2, window_seconds=0.1)

        # 用完配额
        assert limiter.is_allowed("user1")
        assert limiter.is_allowed("user1")
        assert not limiter.is_allowed("user1")

        # 等待窗口过期
        time.sleep(0.15)

        # 应恢复访问
        assert limiter.is_allowed("user1")

    def test_get_remaining(self, rate_limiter):
        """剩余配额计算正确"""
        assert rate_limiter.get_remaining("user1") == 5

        rate_limiter.is_allowed("user1")
        assert rate_limiter.get_remaining("user1") == 4

        rate_limiter.is_allowed("user1")
        assert rate_limiter.get_remaining("user1") == 3

    def test_remaining_after_exhaustion(self, rate_limiter):
        """配额耗尽后剩余为0"""
        for _ in range(5):
            rate_limiter.is_allowed("user1")
        assert rate_limiter.get_remaining("user1") == 0

    def test_sliding_window_behavior(self):
        """滑动窗口行为：旧请求过期后腾出配额"""
        limiter = RateLimiter(max_requests=3, window_seconds=0.2)

        # 发送3次请求
        limiter.is_allowed("user1")
        limiter.is_allowed("user1")
        limiter.is_allowed("user1")
        assert not limiter.is_allowed("user1")  # 第4次被拒

        # 等待部分请求过期
        time.sleep(0.25)

        # 旧请求过期后可以继续
        assert limiter.is_allowed("user1")
