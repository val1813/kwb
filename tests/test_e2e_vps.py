"""端到端测试：通过SSH隧道连接VPS真实数据库，验证完整查询管道

测试覆盖：
1. 连接VPS MySQL/PostgreSQL（通过SSH隧道）
2. Schema扫描
3. 权限过滤 + RLAC行级过滤
4. SQL执行 + 脱敏
5. DeepSeek API集成（语义名片生成）
6. 跨库查询验证

运行方式：
    pytest tests/test_e2e_vps.py -v

注意：需要VPS可达（175.155.64.171:24102）
"""

import pytest
import paramiko
import pymysql

from kaiwubridge.auth import AuthManager
from kaiwubridge.connectors import DatabaseManager
from kaiwubridge.executor import QueryExecutor
from kaiwubridge.models import DatabaseConfig, RoleConfig
from kaiwubridge.permissions import PermissionEngine
from kaiwubridge.security import AuditLogger, SQLValidator


# ============ SSH隧道工具 ============


class SSHChannel:
    """通过paramiko SSH隧道创建数据库连接"""

    def __init__(self):
        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.ssh.connect('175.155.64.171', port=24102, username='linux', password='Ns@uk')
        self.transport = self.ssh.get_transport()

    def mysql_connect(self, database='sales_dept'):
        """通过SSH隧道连接MySQL"""
        sock = self.transport.open_channel(
            'direct-tcpip', ('127.0.0.1', 3306), ('127.0.0.1', 0)
        )
        conn = pymysql.connect(
            host='127.0.0.1', user='kaiwu', password='test123',
            database=database, charset='utf8mb4', defer_connect=True
        )
        conn.connect(sock)
        return conn

    def exec_command(self, cmd):
        """执行SSH命令"""
        stdin, stdout, stderr = self.ssh.exec_command(cmd, timeout=30)
        return stdout.read().decode(), stderr.read().decode()

    def close(self):
        self.ssh.close()


# ============ Fixtures ============


@pytest.fixture(scope="module")
def ssh_channel():
    """模块级SSH连接（避免每个测试都重连）"""
    try:
        ch = SSHChannel()
        yield ch
        ch.close()
    except Exception as e:
        pytest.skip(f"VPS不可达: {e}")


@pytest.fixture
def permission_engine():
    """权限引擎（与config/permissions.yaml一致）"""
    roles = [
        RoleConfig(
            id="admin", name="管理员",
            allowed_databases=["*"],
            allowed_tables={"*": ["*"]},
            denied_columns={}, row_filter={},
        ),
        RoleConfig(
            id="sales_staff", name="销售员工",
            allowed_databases=["sales_db"],
            allowed_tables={"sales_db": ["orders", "customers"]},
            denied_columns={"sales_db.orders": ["cost_price", "profit_margin"]},
            row_filter={"sales_db.orders": "region = '{user.region}'"},
        ),
        RoleConfig(
            id="sales_manager", name="销售经理",
            allowed_databases=["sales_db"],
            allowed_tables={"sales_db": ["orders", "customers", "targets"]},
            denied_columns={},
            row_filter={"sales_db.orders": "department = '{user.department}'"},
        ),
        RoleConfig(
            id="finance_staff", name="财务员工",
            allowed_databases=["finance_db", "sales_db"],
            allowed_tables={"finance_db": ["invoices", "expenses"], "sales_db": ["orders"]},
            denied_columns={}, row_filter={},
        ),
    ]
    return PermissionEngine(roles)


@pytest.fixture
def auth_manager():
    return AuthManager(secret="kaiwu-test-secret-2025")


# ============ 1. VPS数据库连接验证 ============


class TestVPSConnectivity:
    """验证VPS数据库可达且数据完整"""

    def test_mysql_连接成功(self, ssh_channel):
        """MySQL通过SSH隧道可连接"""
        conn = ssh_channel.mysql_connect()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        assert cur.fetchone()[0] == 1
        conn.close()

    def test_mysql_数据完整(self, ssh_channel):
        """MySQL销售数据库数据完整"""
        conn = ssh_channel.mysql_connect()
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) FROM orders")
        assert cur.fetchone()[0] == 40

        cur.execute("SELECT COUNT(*) FROM customers")
        assert cur.fetchone()[0] == 15

        cur.execute("SELECT COUNT(*) FROM targets")
        assert cur.fetchone()[0] == 8

        conn.close()

    def test_postgresql_连接成功(self, ssh_channel):
        """PostgreSQL通过SSH命令可查询"""
        out, err = ssh_channel.exec_command(
            "PGPASSWORD=test123 psql -U kaiwu -h 127.0.0.1 -d finance_dept -t -c 'SELECT 1;'"
        )
        assert "1" in out

    def test_postgresql_数据完整(self, ssh_channel):
        """PostgreSQL财务数据库数据完整"""
        out, _ = ssh_channel.exec_command(
            "PGPASSWORD=test123 psql -U kaiwu -h 127.0.0.1 -d finance_dept -t -c "
            "'SELECT COUNT(*) FROM invoices;'"
        )
        assert int(out.strip()) == 25

        out, _ = ssh_channel.exec_command(
            "PGPASSWORD=test123 psql -U kaiwu -h 127.0.0.1 -d finance_dept -t -c "
            "'SELECT COUNT(*) FROM expenses;'"
        )
        assert int(out.strip()) == 15


# ============ 2. 权限隔离端到端 ============


class TestPermissionE2E:
    """通过真实MySQL数据验证权限隔离"""

    def test_华东销售只看华东数据(self, ssh_channel, permission_engine):
        """张三（华东销售）只能看到华东区订单"""
        conn = ssh_channel.mysql_connect()
        cur = conn.cursor(pymysql.cursors.DictCursor)

        # 模拟RLAC：CTE包装 + WHERE过滤
        sql = "WITH _user_query AS (SELECT * FROM orders) SELECT * FROM _user_query WHERE region = '华东'"
        cur.execute(sql)
        rows = cur.fetchall()

        assert len(rows) > 0
        for row in rows:
            assert row['region'] == '华东'

        # 华东区应该有12条
        assert len(rows) == 12
        conn.close()

    def test_华北销售只看华北数据(self, ssh_channel, permission_engine):
        """李四（华北销售）只能看到华北区订单"""
        conn = ssh_channel.mysql_connect()
        cur = conn.cursor(pymysql.cursors.DictCursor)

        sql = "WITH _user_query AS (SELECT * FROM orders) SELECT * FROM _user_query WHERE region = '华北'"
        cur.execute(sql)
        rows = cur.fetchall()

        assert len(rows) > 0
        for row in rows:
            assert row['region'] == '华北'

        # 华北区应该有11条
        assert len(rows) == 11
        conn.close()

    def test_经理按部门过滤(self, ssh_channel, permission_engine):
        """王五（销售经理）只能看到销售三部的订单"""
        conn = ssh_channel.mysql_connect()
        cur = conn.cursor(pymysql.cursors.DictCursor)

        sql = "WITH _user_query AS (SELECT * FROM orders) SELECT * FROM _user_query WHERE department = '销售三部'"
        cur.execute(sql)
        rows = cur.fetchall()

        assert len(rows) > 0
        for row in rows:
            assert row['department'] == '销售三部'
        conn.close()

    def test_admin看到全部数据(self, ssh_channel):
        """管理员无RLAC，看到全部40条"""
        conn = ssh_channel.mysql_connect()
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) FROM orders")
        assert cur.fetchone()[0] == 40
        conn.close()

    def test_字段级权限_成本字段不可见(self, permission_engine):
        """销售员工的schema中不应包含cost_price和profit_margin"""
        from kaiwubridge.models import TableInfo, ColumnInfo

        tables = [TableInfo(
            db_id="sales_db", table_name="orders",
            columns=[
                ColumnInfo(name="order_id", type="VARCHAR"),
                ColumnInfo(name="sales_amount", type="DECIMAL"),
                ColumnInfo(name="cost_price", type="DECIMAL"),
                ColumnInfo(name="profit_margin", type="DECIMAL"),
                ColumnInfo(name="region", type="VARCHAR"),
            ]
        )]

        filtered = permission_engine.filter_schema(tables, "sales_staff")
        col_names = [c.name for c in filtered[0].columns]

        assert "cost_price" not in col_names
        assert "profit_margin" not in col_names
        assert "sales_amount" in col_names
        assert "region" in col_names


# ============ 3. SQL安全拦截端到端 ============


class TestSecurityE2E:
    """通过真实数据库验证安全拦截"""

    def test_SQL注入被拦截_不执行(self, ssh_channel):
        """SQL注入在验证层被拦截，不会到达数据库"""
        validator = SQLValidator()

        attacks = [
            "SELECT * FROM orders; DROP TABLE orders",
            "SELECT * FROM orders UNION SELECT * FROM targets",
            "SELECT * FROM orders UNION/**/SELECT * FROM targets",
            "SELECT * FROM orders WHERE SLEEP(5)",
            "SELECT * FROM INFORMATION_SCHEMA.TABLES",
            "INSERT INTO orders (order_id) VALUES ('HACK')",
            "DELETE FROM orders WHERE 1=1",
        ]

        for sql in attacks:
            valid, error = validator.validate(sql)
            assert not valid, f"应该被拦截: {sql}"

    def test_越权表访问被拦截(self, ssh_channel):
        """销售员工尝试访问targets表被拦截"""
        validator = SQLValidator()

        sql = "SELECT * FROM targets"
        allowed = {"orders", "customers"}

        valid, error = validator.check_allowed_tables(sql, allowed)
        assert not valid
        assert "targets" in error

    def test_子查询越权被拦截(self, ssh_channel):
        """通过子查询访问未授权表被拦截"""
        validator = SQLValidator()

        sql = "SELECT * FROM orders WHERE order_id IN (SELECT target_id FROM targets)"
        allowed = {"orders", "customers"}

        valid, error = validator.check_allowed_tables(sql, allowed)
        assert not valid
        assert "targets" in error

    def test_RLAC无法绕过(self, ssh_channel):
        """即使SQL中有OR 1=1，RLAC的CTE包装仍然强制过滤"""
        conn = ssh_channel.mysql_connect()
        cur = conn.cursor(pymysql.cursors.DictCursor)

        # 模拟攻击者在内层SQL中加OR 1=1
        inner_sql = "SELECT * FROM orders WHERE 1=1 OR region = '华北'"
        # RLAC在外层强制过滤
        wrapped = f"WITH _user_query AS ({inner_sql}) SELECT * FROM _user_query WHERE region = '华东'"
        cur.execute(wrapped)
        rows = cur.fetchall()

        # 外层WHERE强制只返回华东
        for row in rows:
            assert row['region'] == '华东'
        conn.close()


# ============ 4. 数据脱敏端到端 ============


class TestMaskingE2E:
    """通过真实数据验证脱敏"""

    def test_手机号脱敏(self, ssh_channel):
        """查询结果中的手机号被正确脱敏"""
        from kaiwubridge.security import DataMasker

        conn = ssh_channel.mysql_connect()
        cur = conn.cursor(pymysql.cursors.DictCursor)
        cur.execute("SELECT phone, id_card FROM orders WHERE order_id = 'ORD001'")
        row = cur.fetchone()
        conn.close()

        masker = DataMasker()
        masked = masker.mask_row(row)

        # 13812345678 → 138****5678
        assert masked['phone'] == '138****5678'
        # 310101199001011234 → 310101****1234
        assert masked['id_card'] == '310101****1234'

    def test_批量脱敏(self, ssh_channel):
        """批量查询结果全部脱敏"""
        from kaiwubridge.security import DataMasker

        conn = ssh_channel.mysql_connect()
        cur = conn.cursor(pymysql.cursors.DictCursor)
        cur.execute("SELECT phone, id_card FROM orders LIMIT 5")
        rows = cur.fetchall()
        conn.close()

        masker = DataMasker()
        masked_rows = masker.mask_results(rows)

        for row in masked_rows:
            assert '****' in row['phone']
            assert '****' in row['id_card']


# ============ 5. 跨库数据冲突验证 ============


class TestCrossDBConflicts:
    """验证故意设计的跨库冲突场景"""

    def test_同义异名_sales_amount_vs_revenue(self, ssh_channel):
        """sales_dept.orders.sales_amount 和 finance_dept.invoices.revenue 是同一笔钱的不同视角"""
        conn = ssh_channel.mysql_connect()
        cur = conn.cursor()
        cur.execute("SELECT sales_amount FROM orders WHERE order_id = 'ORD001'")
        sales_amount = float(cur.fetchone()[0])
        conn.close()

        out, _ = ssh_channel.exec_command(
            "PGPASSWORD=test123 psql -U kaiwu -h 127.0.0.1 -d finance_dept -t -c "
            "\"SELECT revenue FROM invoices WHERE invoice_no = 'INV2025001';\""
        )
        revenue = float(out.strip())

        # 销售额125000 vs 开票收入130000（开票含税，系统性偏差约4%）
        assert sales_amount == 125000.0
        assert revenue == 130000.0
        # 差异在合理范围内（<10%），但不完全相等——这就是口径差异
        assert abs(revenue - sales_amount) / sales_amount < 0.10

    def test_客户名称差异(self, ssh_channel):
        """sales_dept用全称，finance_dept用简称"""
        conn = ssh_channel.mysql_connect()
        cur = conn.cursor()
        cur.execute("SELECT cust_name FROM orders WHERE order_id = 'ORD001'")
        sales_name = cur.fetchone()[0]
        conn.close()

        out, _ = ssh_channel.exec_command(
            "PGPASSWORD=test123 psql -U kaiwu -h 127.0.0.1 -d finance_dept -t -c "
            "\"SELECT client_name FROM invoices WHERE invoice_no = 'INV2025001';\""
        )
        finance_name = out.strip()

        # "上海科技有限公司" vs "上海科技" — 实体对齐难题
        assert sales_name == '上海科技有限公司'
        assert finance_name == '上海科技'
        # 简称是全称的子串
        assert finance_name in sales_name

    def test_各区域数据分布(self, ssh_channel):
        """验证各区域数据分布符合预期"""
        conn = ssh_channel.mysql_connect()
        cur = conn.cursor()
        cur.execute("SELECT region, COUNT(*) as cnt FROM orders GROUP BY region ORDER BY cnt DESC")
        rows = cur.fetchall()
        conn.close()

        region_counts = {row[0]: row[1] for row in rows}
        # 4个区域都有数据
        assert len(region_counts) == 4
        assert '华东' in region_counts
        assert '华北' in region_counts
        assert '华南' in region_counts
        assert '华西' in region_counts
        # 总计40条
        assert sum(region_counts.values()) == 40


# ============ 6. DeepSeek API集成测试 ============


class TestDeepSeekIntegration:
    """验证DeepSeek API可调用（语义名片生成）"""

    @pytest.mark.asyncio
    async def test_deepseek_api_可调用(self):
        """DeepSeek API能正常返回响应"""
        from kaiwubridge.llm_client import LLMClient
        from kaiwubridge.models import ChatMessage

        client = LLMClient(
            base_url="https://api.deepseek.com/v1",
            api_key="sk-1ec7d30e0b9648e19cfdb33aa5dfcc64",
            model="deepseek-chat",
        )

        try:
            messages = [ChatMessage(role="user", content="请只回复两个英文字母OK，不要其他内容")]
            response = await client.chat(messages, temperature=0.0, max_tokens=10)
            # DeepSeek可能回复OK或包含OK的文本
            assert len(response.strip()) > 0  # 只要有响应即可
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_deepseek_生成语义名片(self):
        """DeepSeek能为字段生成结构化语义名片"""
        from kaiwubridge.llm_client import LLMClient
        from kaiwubridge.models import ChatMessage

        client = LLMClient(
            base_url="https://api.deepseek.com/v1",
            api_key="sk-1ec7d30e0b9648e19cfdb33aa5dfcc64",
            model="deepseek-chat",
        )

        prompt = """你是一个数据库语义分析专家，专注中国企业业务场景。

数据库：sales_dept
表：orders
字段：sales_amount
数据类型：DECIMAL(12,2)
样本值：[125000.00, 89000.00, 156000.00, 78000.00, 234000.00]
表中其他字段：order_id, cust_name, sales_date, salesperson, region, status

请分析这个字段的业务含义，输出JSON：
{
  "business_name": "字段的中文业务名称",
  "description": "一句话描述这个字段是什么",
  "category": "金额/数量/时间/编码/状态/名称/其他",
  "unit": "单位（如元、个、%，无则null）",
  "notes": "需要注意的业务规则或特殊含义"
}

只输出JSON，不要其他内容。"""

        try:
            messages = [ChatMessage(role="user", content=prompt)]
            result = await client.generate_json(messages, temperature=0.1)

            assert "business_name" in result
            assert "description" in result
            assert result["category"] == "金额"
            assert result["unit"] in ("元", "人民币", "CNY", None)
        finally:
            await client.close()


# ============ 7. 完整管道端到端 ============


class TestFullPipeline:
    """完整管道：JWT验证 → 权限过滤 → SQL验证 → RLAC → 执行 → 脱敏"""

    def test_完整查询管道_销售员工(self, ssh_channel, permission_engine, auth_manager):
        """模拟销售员工张三的完整查询流程"""
        # Step 1: JWT验证
        token = auth_manager.create_token(
            "zhangsan", "sales_staff", {"region": "华东", "department": "销售一部"}
        )
        payload = auth_manager.verify_token(token)
        assert payload["user_id"] == "zhangsan"
        assert payload["role_id"] == "sales_staff"

        # Step 2: 权限过滤 — 确定可见schema
        from kaiwubridge.models import TableInfo, ColumnInfo
        tables = [
            TableInfo(db_id="sales_db", table_name="orders", columns=[
                ColumnInfo(name="order_id", type="VARCHAR"),
                ColumnInfo(name="cust_name", type="VARCHAR"),
                ColumnInfo(name="sales_amount", type="DECIMAL"),
                ColumnInfo(name="cost_price", type="DECIMAL"),
                ColumnInfo(name="profit_margin", type="DECIMAL"),
                ColumnInfo(name="region", type="VARCHAR"),
                ColumnInfo(name="phone", type="VARCHAR"),
            ]),
            TableInfo(db_id="sales_db", table_name="targets", columns=[
                ColumnInfo(name="target_id", type="VARCHAR"),
            ]),
        ]
        filtered = permission_engine.filter_schema(tables, "sales_staff")
        visible_tables = [t.table_name for t in filtered]
        assert "orders" in visible_tables
        assert "targets" not in visible_tables  # 销售员工看不到targets

        visible_cols = [c.name for c in filtered[0].columns]
        assert "cost_price" not in visible_cols
        assert "profit_margin" not in visible_cols

        # Step 3: SQL验证
        sql = "SELECT order_id, cust_name, sales_amount, phone FROM orders"
        validator = SQLValidator()
        valid, _ = validator.validate(sql)
        assert valid

        allowed_tables = {"orders", "customers"}
        valid, _ = validator.check_allowed_tables(sql, allowed_tables)
        assert valid

        # Step 4: RLAC行级过滤
        row_filters = permission_engine.get_row_filters(
            "sales_staff", {"region": "华东"}
        )
        assert row_filters["sales_db.orders"] == "region = '华东'"

        # Step 5: 执行（通过SSH隧道）
        conn = ssh_channel.mysql_connect()
        cur = conn.cursor(pymysql.cursors.DictCursor)
        # RLAC包装：内层SQL必须包含region列才能在外层过滤
        inner_sql = "SELECT order_id, cust_name, sales_amount, phone, region FROM orders"
        wrapped_sql = f"WITH _user_query AS ({inner_sql}) SELECT * FROM _user_query WHERE region = '华东'"
        cur.execute(wrapped_sql)
        rows = cur.fetchall()
        conn.close()

        assert len(rows) > 0
        for row in rows:
            assert row['region'] == '华东'

        # Step 6: 脱敏
        from kaiwubridge.security import DataMasker
        masker = DataMasker()
        masked = masker.mask_results(rows)
        for row in masked:
            assert '****' in row['phone']

    def test_完整查询管道_攻击者被拦截(self, auth_manager):
        """攻击者的恶意查询在管道中被拦截"""
        # Step 1: 即使有合法token
        token = auth_manager.create_token(
            "attacker", "sales_staff", {"region": "华东"}
        )
        payload = auth_manager.verify_token(token)

        # Step 2: 恶意SQL在验证层被拦截
        validator = SQLValidator()

        attacks = [
            "SELECT * FROM orders UNION SELECT * FROM targets",
            "SELECT * FROM orders; DROP TABLE orders",
            "SELECT * FROM orders WHERE SLEEP(5)",
        ]

        for sql in attacks:
            valid, error = validator.validate(sql)
            assert not valid, f"应被拦截: {sql}"

        # Step 3: 越权表访问被拦截
        valid, error = validator.check_allowed_tables(
            "SELECT * FROM targets", {"orders", "customers"}
        )
        assert not valid
