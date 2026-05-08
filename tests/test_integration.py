"""集成测试：用SQLite模拟数据库跑端到端流程

测试覆盖：
1. 端到端查询流程 - SQL验证 → 表检查 → RLAC → 执行 → 脱敏
2. 权限隔离端到端 - 不同角色看到不同数据
3. RLAC行级过滤端到端 - 同一查询不同用户不同结果
4. 安全拦截端到端 - SQL注入通过完整管道被拦截
5. 审计日志完整性 - 所有操作都被记录
"""

import json
import sqlite3
from pathlib import Path

import pytest

from kaiwubridge.auth import AuthManager
from kaiwubridge.connectors import DatabaseManager
from kaiwubridge.executor import QueryExecutor
from kaiwubridge.models import DatabaseConfig, RoleConfig
from kaiwubridge.permissions import PermissionEngine
from kaiwubridge.security import AuditLogger


# ============ Fixtures ============


@pytest.fixture
def test_db(tmp_path):
    """创建包含模拟企业数据的SQLite测试数据库"""
    db_path = tmp_path / "test_enterprise.db"
    conn = sqlite3.connect(str(db_path))

    # 创建销售订单表
    conn.executescript("""
        CREATE TABLE orders (
            order_id TEXT PRIMARY KEY,
            cust_name TEXT,
            sales_amount REAL,
            cost_price REAL,
            profit_margin REAL,
            sales_date TEXT,
            salesperson TEXT,
            region TEXT,
            department TEXT,
            status TEXT,
            phone TEXT,
            id_card TEXT
        );

        CREATE TABLE customers (
            cust_id TEXT PRIMARY KEY,
            cust_name TEXT,
            contact_phone TEXT,
            level TEXT,
            region TEXT
        );

        CREATE TABLE targets (
            target_id TEXT PRIMARY KEY,
            salesperson TEXT,
            department TEXT,
            quarter TEXT,
            target_amount REAL,
            actual_amount REAL
        );

        -- 插入订单数据（含敏感信息）
        INSERT INTO orders VALUES
        ('ORD001','上海科技有限公司',125000.00,85000.00,32.00,'2025-01-15','张三','华东','销售一部','已完成','13812345678','310101199001011234'),
        ('ORD002','北京贸易股份',89000.00,62000.00,30.34,'2025-01-18','李四','华北','销售二部','已完成','13987654321','110101198805052345'),
        ('ORD003','广州制造集团',156000.00,108000.00,30.77,'2025-01-20','王五','华南','销售三部','已完成','13611112222','440101199203033456'),
        ('ORD004','深圳电子科技',78000.00,54000.00,30.77,'2025-01-22','张三','华东','销售一部','已完成','13722223333','320101199104044567'),
        ('ORD005','杭州网络技术',234000.00,162000.00,30.77,'2025-01-25','张三','华东','销售一部','已完成','13833334444','330101198806065678'),
        ('ORD006','成都软件开发',67000.00,47000.00,29.85,'2025-02-01','赵六','华西','销售四部','已完成','13944445555','510101199507076789'),
        ('ORD007','武汉生物医药',198000.00,138000.00,30.30,'2025-02-03','李四','华北','销售二部','已完成','13655556666','420101199208087890'),
        ('ORD008','南京新材料',145000.00,101000.00,30.34,'2025-02-05','张三','华东','销售一部','已完成','13766667777','320201199309098901'),
        ('ORD009','天津重工业',312000.00,218000.00,30.13,'2025-02-08','李四','华北','销售二部','已完成','13877778888','120101198710109012'),
        ('ORD010','厦门贸易公司',56000.00,39000.00,30.36,'2025-02-10','王五','华南','销售三部','已完成','13988889999','350101199611110123');

        -- 插入客户数据
        INSERT INTO customers VALUES
        ('CUST001','上海科技有限公司','13812345678','A','华东'),
        ('CUST002','北京贸易股份','13987654321','A','华北'),
        ('CUST003','广州制造集团','13611112222','B','华南'),
        ('CUST004','深圳电子科技','13722223333','B','华东'),
        ('CUST005','杭州网络技术','13833334444','A','华东');

        -- 插入目标数据
        INSERT INTO targets VALUES
        ('TGT001','张三','销售一部','2025Q1',500000.00,433000.00),
        ('TGT002','李四','销售二部','2025Q1',600000.00,578000.00),
        ('TGT003','王五','销售三部','2025Q1',400000.00,324000.00),
        ('TGT004','赵六','销售四部','2025Q1',450000.00,389000.00);
    """)
    conn.close()
    return db_path


@pytest.fixture
def db_manager(test_db):
    """创建数据库管理器"""
    config = DatabaseConfig(
        id="sales_db",
        name="销售数据库",
        type="sqlite",
        database=str(test_db),
    )
    manager = DatabaseManager([config])
    yield manager
    manager.close_all()


@pytest.fixture
def permission_engine():
    """创建权限引擎"""
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
            id="sales_staff",
            name="销售员工",
            allowed_databases=["sales_db"],
            allowed_tables={"sales_db": ["orders", "customers"]},
            denied_columns={"sales_db.orders": ["cost_price", "profit_margin"]},
            row_filter={"sales_db.orders": "region = '华东'"},
        ),
        RoleConfig(
            id="sales_staff_north",
            name="华北销售员工",
            allowed_databases=["sales_db"],
            allowed_tables={"sales_db": ["orders", "customers"]},
            denied_columns={"sales_db.orders": ["cost_price", "profit_margin"]},
            row_filter={"sales_db.orders": "region = '华北'"},
        ),
        RoleConfig(
            id="sales_manager",
            name="销售经理",
            allowed_databases=["sales_db"],
            allowed_tables={"sales_db": ["orders", "customers", "targets"]},
            denied_columns={},
            row_filter={"sales_db.orders": "department = '销售一部'"},
        ),
    ]
    return PermissionEngine(roles)


@pytest.fixture
def auth_manager():
    """创建认证管理器"""
    return AuthManager(secret="test-secret-key-2025")


@pytest.fixture
def audit_logger(tmp_path):
    """创建审计日志器"""
    return AuditLogger(log_dir=str(tmp_path / "audit"))


@pytest.fixture
def executor(db_manager, audit_logger):
    """创建查询执行器"""
    return QueryExecutor(
        db_manager=db_manager,
        audit_logger=audit_logger,
        enable_masking=True,
    )


# ============ 端到端查询流程测试 ============


class TestEndToEndQuery:
    """端到端查询流程"""

    def test_简单查询成功(self, executor):
        """正常SELECT查询应该成功执行"""
        result = executor.execute(
            db_id="sales_db",
            sql="SELECT order_id, cust_name, sales_amount FROM orders WHERE region = '华东'",
            user_id="zhangsan",
            role_id="sales_staff",
            question="华东区的订单",
        )

        assert result["success"] is True
        assert result["row_count"] > 0
        # 华东区有4条数据（ORD001, ORD004, ORD005, ORD008）
        assert result["row_count"] == 4

    def test_聚合查询(self, executor):
        """聚合查询应该正常工作"""
        result = executor.execute(
            db_id="sales_db",
            sql="SELECT region, COUNT(*) as cnt, SUM(sales_amount) as total FROM orders GROUP BY region",
            user_id="admin",
            role_id="admin",
            question="各区域销售汇总",
        )

        assert result["success"] is True
        assert result["row_count"] == 4  # 华东、华北、华南、华西

    def test_结果脱敏_手机号(self, executor):
        """查询结果中的手机号应该被脱敏"""
        result = executor.execute(
            db_id="sales_db",
            sql="SELECT order_id, phone, id_card FROM orders WHERE order_id = 'ORD001'",
            user_id="admin",
            role_id="admin",
            question="查看订单详情",
        )

        assert result["success"] is True
        row = result["data"][0]
        # 手机号应该被脱敏：13812345678 → 138****5678
        assert "****" in row["phone"]
        assert row["phone"] == "138****5678"
        # 身份证应该被脱敏：310101199001011234 → 310101****1234
        assert "****" in row["id_card"]
        assert row["id_card"] == "310101****1234"

    def test_空结果查询(self, executor):
        """查询无结果时应该返回空列表"""
        result = executor.execute(
            db_id="sales_db",
            sql="SELECT * FROM orders WHERE region = '不存在的区域'",
            user_id="admin",
            role_id="admin",
            question="查询不存在的区域",
        )

        assert result["success"] is True
        assert result["row_count"] == 0
        assert result["data"] == []


# ============ 权限隔离端到端测试 ============


class TestPermissionIsolation:
    """权限隔离端到端测试"""

    def test_RLAC_华东销售只看华东数据(self, executor):
        """华东销售员工通过RLAC只能看到华东区数据"""
        result = executor.execute(
            db_id="sales_db",
            sql="SELECT * FROM orders",
            row_filters={"sales_db.orders": "region = '华东'"},
            allowed_tables={"orders", "customers"},
            user_id="zhangsan",
            role_id="sales_staff",
            question="查看所有订单",
        )

        assert result["success"] is True
        # 所有返回的数据都应该是华东区的
        for row in result["data"]:
            assert row["region"] == "华东"
        # 华东区有4条
        assert result["row_count"] == 4

    def test_RLAC_华北销售只看华北数据(self, executor):
        """华北销售员工通过RLAC只能看到华北区数据"""
        result = executor.execute(
            db_id="sales_db",
            sql="SELECT * FROM orders",
            row_filters={"sales_db.orders": "region = '华北'"},
            allowed_tables={"orders", "customers"},
            user_id="lisi",
            role_id="sales_staff_north",
            question="查看所有订单",
        )

        assert result["success"] is True
        for row in result["data"]:
            assert row["region"] == "华北"
        # 华北区有3条（ORD002, ORD007, ORD009）
        assert result["row_count"] == 3

    def test_RLAC_经理按部门过滤(self, executor):
        """销售经理通过RLAC只能看到自己部门的数据"""
        result = executor.execute(
            db_id="sales_db",
            sql="SELECT * FROM orders",
            row_filters={"sales_db.orders": "department = '销售一部'"},
            allowed_tables={"orders", "customers", "targets"},
            user_id="wangwu",
            role_id="sales_manager",
            question="查看部门订单",
        )

        assert result["success"] is True
        for row in result["data"]:
            assert row["department"] == "销售一部"

    def test_admin无RLAC看到所有数据(self, executor):
        """管理员没有RLAC，能看到所有数据"""
        result = executor.execute(
            db_id="sales_db",
            sql="SELECT * FROM orders",
            row_filters=None,  # admin没有行级过滤
            user_id="admin",
            role_id="admin",
            question="查看所有订单",
        )

        assert result["success"] is True
        assert result["row_count"] == 10  # 全部10条

    def test_表级权限_销售员不能访问targets(self, executor):
        """销售员工不能访问targets表"""
        result = executor.execute(
            db_id="sales_db",
            sql="SELECT * FROM targets",
            allowed_tables={"orders", "customers"},  # 不包含targets
            user_id="zhangsan",
            role_id="sales_staff",
            question="查看销售目标",
        )

        assert result["success"] is False
        assert "无权访问表" in result["error"]


# ============ 安全拦截端到端测试 ============


class TestSecurityBlocking:
    """安全拦截端到端测试"""

    def test_SQL注入_UNION_SELECT(self, executor):
        """UNION SELECT注入应该被拦截"""
        result = executor.execute(
            db_id="sales_db",
            sql="SELECT * FROM orders UNION SELECT * FROM targets",
            allowed_tables={"orders"},
            user_id="attacker",
            role_id="sales_staff",
            question="尝试注入",
        )

        assert result["success"] is False
        assert "可疑SQL模式" in result["error"] or "无权访问表" in result["error"]

    def test_SQL注入_DROP_TABLE(self, executor):
        """DROP TABLE应该被拦截"""
        result = executor.execute(
            db_id="sales_db",
            sql="DROP TABLE orders",
            user_id="attacker",
            role_id="sales_staff",
            question="尝试删表",
        )

        assert result["success"] is False
        assert "禁止执行" in result["error"]

    def test_SQL注入_INSERT(self, executor):
        """INSERT应该被拦截"""
        result = executor.execute(
            db_id="sales_db",
            sql="INSERT INTO orders (order_id) VALUES ('HACK001')",
            user_id="attacker",
            role_id="sales_staff",
            question="尝试插入",
        )

        assert result["success"] is False
        assert "禁止执行" in result["error"]

    def test_SQL注入_UPDATE(self, executor):
        """UPDATE应该被拦截"""
        result = executor.execute(
            db_id="sales_db",
            sql="UPDATE orders SET sales_amount = 0",
            user_id="attacker",
            role_id="sales_staff",
            question="尝试修改",
        )

        assert result["success"] is False
        assert "禁止执行" in result["error"]

    def test_SQL注入_DELETE(self, executor):
        """DELETE应该被拦截"""
        result = executor.execute(
            db_id="sales_db",
            sql="DELETE FROM orders",
            user_id="attacker",
            role_id="sales_staff",
            question="尝试删除",
        )

        assert result["success"] is False
        assert "禁止执行" in result["error"]

    def test_SQL注入_SLEEP(self, executor):
        """SLEEP时间盲注应该被拦截"""
        result = executor.execute(
            db_id="sales_db",
            sql="SELECT * FROM orders WHERE SLEEP(5)",
            allowed_tables={"orders"},
            user_id="attacker",
            role_id="sales_staff",
            question="时间盲注",
        )

        assert result["success"] is False
        assert "可疑SQL模式" in result["error"]

    def test_SQL注入_INFORMATION_SCHEMA(self, executor):
        """INFORMATION_SCHEMA探测应该被拦截"""
        result = executor.execute(
            db_id="sales_db",
            sql="SELECT * FROM INFORMATION_SCHEMA.TABLES",
            allowed_tables={"orders"},
            user_id="attacker",
            role_id="sales_staff",
            question="元数据探测",
        )

        assert result["success"] is False

    def test_越权访问_未授权表(self, executor):
        """访问未授权的表应该被拦截"""
        result = executor.execute(
            db_id="sales_db",
            sql="SELECT * FROM targets",
            allowed_tables={"orders", "customers"},
            user_id="zhangsan",
            role_id="sales_staff",
            question="尝试越权",
        )

        assert result["success"] is False
        assert "无权访问表" in result["error"]

    def test_RLAC_无法绕过_OR_1_1(self, executor):
        """即使SQL中有OR 1=1，RLAC仍然生效"""
        # RLAC通过CTE包装，外层WHERE强制过滤
        result = executor.execute(
            db_id="sales_db",
            sql="SELECT * FROM orders WHERE 1=1 OR region = '华北'",
            row_filters={"sales_db.orders": "region = '华东'"},
            allowed_tables={"orders"},
            user_id="zhangsan",
            role_id="sales_staff",
            question="尝试绕过RLAC",
        )

        assert result["success"] is True
        # RLAC在外层强制过滤，只返回华东数据
        for row in result["data"]:
            assert row["region"] == "华东"


# ============ 审计日志完整性测试 ============


class TestAuditLog:
    """审计日志完整性测试"""

    def test_成功查询被记录(self, executor, tmp_path):
        """成功的查询应该被记录到审计日志"""
        executor.execute(
            db_id="sales_db",
            sql="SELECT COUNT(*) as cnt FROM orders",
            user_id="admin",
            role_id="admin",
            question="统计订单数",
        )

        # 读取审计日志
        audit_dir = tmp_path / "audit"
        log_files = list(audit_dir.glob("audit_*.jsonl"))
        assert len(log_files) > 0

        with open(log_files[0], "r", encoding="utf-8") as f:
            lines = f.readlines()

        assert len(lines) >= 1
        record = json.loads(lines[-1])
        assert record["user_id"] == "admin"
        assert record["success"] is True
        assert record["result_rows"] == 1

    def test_被拦截查询被记录(self, executor, tmp_path):
        """被拦截的查询应该被记录到审计日志"""
        executor.execute(
            db_id="sales_db",
            sql="DROP TABLE orders",
            user_id="attacker",
            role_id="sales_staff",
            question="尝试删表",
        )

        # 读取审计日志
        audit_dir = tmp_path / "audit"
        log_files = list(audit_dir.glob("audit_*.jsonl"))
        assert len(log_files) > 0

        with open(log_files[0], "r", encoding="utf-8") as f:
            lines = f.readlines()

        # 找到被拦截的记录
        blocked_records = [
            json.loads(line) for line in lines
            if json.loads(line).get("blocked_reason")
        ]
        assert len(blocked_records) >= 1
        assert blocked_records[0]["user_id"] == "attacker"
        assert blocked_records[0]["success"] is False

    def test_安全事件被记录(self, executor, tmp_path):
        """安全事件应该被记录到security_events.jsonl"""
        executor.execute(
            db_id="sales_db",
            sql="SELECT * FROM orders UNION ALL SELECT * FROM targets",
            allowed_tables={"orders"},
            user_id="attacker",
            role_id="sales_staff",
            question="UNION注入尝试",
        )

        # 读取安全事件日志
        security_log = tmp_path / "audit" / "security_events.jsonl"
        assert security_log.exists()

        with open(security_log, "r", encoding="utf-8") as f:
            lines = f.readlines()

        assert len(lines) >= 1
        event = json.loads(lines[-1])
        assert event["event_type"] == "sql_blocked"
        assert event["user_id"] == "attacker"
        assert event["level"] == "WARNING"

    def test_多次操作审计完整性(self, executor, tmp_path):
        """多次操作后审计日志应该完整记录所有事件"""
        # 执行多种操作
        executor.execute(
            db_id="sales_db",
            sql="SELECT * FROM orders",
            user_id="admin",
            role_id="admin",
            question="查询1",
        )
        executor.execute(
            db_id="sales_db",
            sql="SELECT * FROM customers",
            user_id="zhangsan",
            role_id="sales_staff",
            question="查询2",
        )
        executor.execute(
            db_id="sales_db",
            sql="DROP TABLE orders",
            user_id="attacker",
            role_id="sales_staff",
            question="攻击尝试",
        )

        # 验证审计日志
        audit_dir = tmp_path / "audit"
        log_files = list(audit_dir.glob("audit_*.jsonl"))
        assert len(log_files) > 0

        with open(log_files[0], "r", encoding="utf-8") as f:
            lines = f.readlines()

        # 应该有3条记录
        assert len(lines) >= 3

        records = [json.loads(line) for line in lines]
        # 验证记录顺序和内容
        assert records[0]["user_id"] == "admin"
        assert records[0]["success"] is True
        assert records[1]["user_id"] == "zhangsan"
        assert records[1]["success"] is True
        assert records[2]["user_id"] == "attacker"
        assert records[2]["success"] is False


# ============ JWT认证集成测试 ============


class TestAuthIntegration:
    """JWT认证集成测试"""

    def test_token生成和验证(self, auth_manager):
        """生成的token应该能被正确验证"""
        token = auth_manager.create_token(
            user_id="zhangsan",
            role_id="sales_staff",
            attrs={"region": "华东", "department": "销售一部"},
        )

        payload = auth_manager.verify_token(token)
        assert payload["user_id"] == "zhangsan"
        assert payload["role_id"] == "sales_staff"
        assert payload["attrs"]["region"] == "华东"

    def test_Bearer_header提取(self, auth_manager):
        """从Bearer header中提取token"""
        token = auth_manager.create_token("admin", "admin")
        header = f"Bearer {token}"

        payload = auth_manager.extract_from_header(header)
        assert payload["user_id"] == "admin"

    def test_过期token被拒绝(self, auth_manager):
        """过期的token应该被拒绝"""
        import jwt as pyjwt

        # 创建一个已过期的token
        expired_manager = AuthManager(secret="test-secret-key-2025", expire_hours=-1)
        token = expired_manager.create_token("user", "role")

        with pytest.raises(pyjwt.ExpiredSignatureError):
            auth_manager.verify_token(token)

    def test_伪造token被拒绝(self, auth_manager):
        """用错误密钥签名的token应该被拒绝"""
        import jwt as pyjwt

        fake_manager = AuthManager(secret="wrong-secret")
        token = fake_manager.create_token("admin", "admin")

        with pytest.raises(pyjwt.InvalidSignatureError):
            auth_manager.verify_token(token)
