"""功能测试：语义匹配准确率、跨库映射、权限过滤、查询流程

测试覆盖：
1. 语义匹配准确率 - 同义异名识别、同名不同义检测、分布验证、决策树四象限
2. 跨库映射 - batch_match、置信度评分、LearningEngine贝叶斯更新
3. 权限过滤流程 - filter_schema、get_row_filters、角色隔离
4. 查询流程 - context构建、行级过滤生成
"""

import numpy as np
import pytest

from kaiwubridge.matching import (
    EmbeddingEngine,
    FieldMatcher,
    LearningEngine,
    cosine_similarity,
    SEMANTIC_HIGH_THRESHOLD,
    DISTRIBUTION_THRESHOLD,
)
from kaiwubridge.models import (
    ColumnInfo,
    RoleConfig,
    SemanticCard,
    TableInfo,
)
from kaiwubridge.permissions import PermissionEngine


# ============ Mock Embedding Engine ============


class MockEmbeddingEngine(EmbeddingEngine):
    """模拟embedding引擎，返回可预测的向量

    通过预设的field_id -> vector映射来控制相似度计算结果。
    不需要加载真实的sentence-transformers模型。
    """

    def __init__(self, vectors: dict[str, np.ndarray] | None = None):
        super().__init__(model_name="mock")
        self._mock_vectors = vectors or {}

    def _load_model(self):
        """跳过模型加载"""
        pass

    def encode(self, text: str, field_id: str | None = None) -> np.ndarray:
        """返回预设向量，或基于文本hash生成确定性向量"""
        if field_id and field_id in self._mock_vectors:
            return self._mock_vectors[field_id]
        # 基于文本生成确定性向量（用于没有预设的情况）
        rng = np.random.RandomState(hash(text) % 2**31)
        vec = rng.randn(768).astype(np.float32)
        vec = vec / np.linalg.norm(vec)  # 归一化
        return vec


# ============ Fixtures ============


@pytest.fixture
def permission_engine():
    """创建包含多角色的权限引擎"""
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
            row_filter={"sales_db.orders": "region = '{user.region}'"},
        ),
        RoleConfig(
            id="sales_manager",
            name="销售经理",
            allowed_databases=["sales_db"],
            allowed_tables={"sales_db": ["orders", "customers", "targets"]},
            denied_columns={},
            row_filter={"sales_db.orders": "department = '{user.department}'"},
        ),
        RoleConfig(
            id="finance_staff",
            name="财务员工",
            allowed_databases=["finance_db", "sales_db"],
            allowed_tables={
                "finance_db": ["invoices", "expenses"],
                "sales_db": ["orders"],
            },
            denied_columns={},
            row_filter={},
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
                ColumnInfo(name="order_id", type="VARCHAR(20)"),
                ColumnInfo(name="cust_name", type="VARCHAR(100)"),
                ColumnInfo(name="sales_amount", type="DECIMAL(12,2)"),
                ColumnInfo(name="cost_price", type="DECIMAL(12,2)"),
                ColumnInfo(name="profit_margin", type="DECIMAL(5,2)"),
                ColumnInfo(name="sales_date", type="DATE"),
                ColumnInfo(name="salesperson", type="VARCHAR(50)"),
                ColumnInfo(name="region", type="VARCHAR(20)"),
                ColumnInfo(name="department", type="VARCHAR(50)"),
                ColumnInfo(name="status", type="VARCHAR(20)"),
            ],
            row_count=40,
        ),
        TableInfo(
            db_id="sales_db",
            table_name="customers",
            columns=[
                ColumnInfo(name="cust_id", type="VARCHAR(20)"),
                ColumnInfo(name="cust_name", type="VARCHAR(100)"),
                ColumnInfo(name="contact_phone", type="VARCHAR(20)"),
                ColumnInfo(name="level", type="ENUM"),
                ColumnInfo(name="region", type="VARCHAR(20)"),
            ],
            row_count=15,
        ),
        TableInfo(
            db_id="sales_db",
            table_name="targets",
            columns=[
                ColumnInfo(name="target_id", type="VARCHAR(20)"),
                ColumnInfo(name="salesperson", type="VARCHAR(50)"),
                ColumnInfo(name="department", type="VARCHAR(50)"),
                ColumnInfo(name="quarter", type="VARCHAR(10)"),
                ColumnInfo(name="target_amount", type="DECIMAL(12,2)"),
                ColumnInfo(name="actual_amount", type="DECIMAL(12,2)"),
            ],
            row_count=8,
        ),
    ]


@pytest.fixture
def finance_tables():
    """财务数据库的表结构"""
    return [
        TableInfo(
            db_id="finance_db",
            table_name="invoices",
            columns=[
                ColumnInfo(name="invoice_no", type="VARCHAR(30)"),
                ColumnInfo(name="client_name", type="VARCHAR(100)"),
                ColumnInfo(name="revenue", type="DECIMAL(12,2)"),
                ColumnInfo(name="invoice_date", type="DATE"),
                ColumnInfo(name="tax_amount", type="DECIMAL(10,2)"),
                ColumnInfo(name="cost", type="DECIMAL(12,2)"),
                ColumnInfo(name="profit", type="DECIMAL(12,2)"),
            ],
            row_count=25,
        ),
        TableInfo(
            db_id="finance_db",
            table_name="expenses",
            columns=[
                ColumnInfo(name="exp_id", type="VARCHAR(20)"),
                ColumnInfo(name="category", type="VARCHAR(50)"),
                ColumnInfo(name="amount", type="DECIMAL(10,2)"),
                ColumnInfo(name="exp_date", type="DATE"),
                ColumnInfo(name="department", type="VARCHAR(50)"),
                ColumnInfo(name="approver", type="VARCHAR(50)"),
            ],
            row_count=15,
        ),
    ]


# ============ 语义匹配准确率测试 ============


class TestSemanticMatching:
    """语义匹配准确率测试"""

    def _make_high_similarity_engine(self):
        """创建两个字段高度相似的mock引擎"""
        # 两个几乎相同的向量 → 高相似度（噪声极小确保>0.85）
        base = np.random.RandomState(42).randn(768).astype(np.float32)
        base = base / np.linalg.norm(base)
        noise = np.random.RandomState(43).randn(768).astype(np.float32) * 0.01
        similar = base + noise
        similar = similar / np.linalg.norm(similar)

        return MockEmbeddingEngine(vectors={
            "sales_db.orders.sales_amount": base,
            "finance_db.invoices.revenue": similar,
        })

    def _make_low_similarity_engine(self):
        """创建两个字段低相似度的mock引擎"""
        vec_a = np.random.RandomState(100).randn(768).astype(np.float32)
        vec_a = vec_a / np.linalg.norm(vec_a)
        vec_b = np.random.RandomState(200).randn(768).astype(np.float32)
        vec_b = vec_b / np.linalg.norm(vec_b)

        return MockEmbeddingEngine(vectors={
            "sales_db.orders.order_id": vec_a,
            "finance_db.expenses.exp_date": vec_b,
        })

    def test_同义异名_高相似度识别(self):
        """sales_amount vs revenue 应该被识别为高度匹配"""
        engine = self._make_high_similarity_engine()
        matcher = FieldMatcher(engine)

        card_a = SemanticCard(
            field_id="sales_db.orders.sales_amount",
            business_name="销售额",
            description="每笔订单的销售金额",
            category="金额",
            unit="元",
        )
        card_b = SemanticCard(
            field_id="finance_db.invoices.revenue",
            business_name="收入",
            description="发票对应的销售收入金额",
            category="金额",
            unit="元",
        )

        # 提供相似的数值分布
        values_a = [125000, 89000, 156000, 78000, 234000, 67000, 198000]
        values_b = [130000, 92000, 162000, 81000, 243000, 69500, 205000]

        result = matcher.match(card_a, card_b, values_a, values_b)

        assert result["semantic_score"] > 0.8
        assert result["decision"] in ("auto_map", "review_caliber")
        assert result["confidence"] > 0.5

    def test_同名不同义_低相似度(self):
        """两个不相关字段应该被标记为ignore"""
        engine = self._make_low_similarity_engine()
        matcher = FieldMatcher(engine)

        card_a = SemanticCard(
            field_id="sales_db.orders.order_id",
            business_name="订单编号",
            description="订单唯一标识符",
            category="编码",
        )
        card_b = SemanticCard(
            field_id="finance_db.expenses.exp_date",
            business_name="费用日期",
            description="费用发生的日期",
            category="时间",
        )

        result = matcher.match(card_a, card_b)

        assert result["semantic_score"] < SEMANTIC_HIGH_THRESHOLD
        assert result["decision"] == "ignore"

    def test_分布验证_数值相似(self):
        """数值分布相似的字段应该通过分布验证"""
        engine = MockEmbeddingEngine()
        matcher = FieldMatcher(engine)

        card_a = SemanticCard(
            field_id="db1.t1.amount",
            business_name="金额A",
            description="金额字段A",
            category="金额",
            unit="元",
        )
        card_b = SemanticCard(
            field_id="db2.t2.amount",
            business_name="金额B",
            description="金额字段B",
            category="金额",
            unit="元",
        )

        # 几乎相同的分布
        values_a = [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000]
        values_b = [105, 198, 305, 395, 510, 590, 710, 795, 905, 995]

        result = matcher.match(card_a, card_b, values_a, values_b)

        assert result["distribution_matched"] is True
        assert result["distribution_distance"] < DISTRIBUTION_THRESHOLD

    def test_分布验证_数值差异大(self):
        """数值分布差异大的字段应该不通过分布验证"""
        engine = MockEmbeddingEngine()
        matcher = FieldMatcher(engine)

        card_a = SemanticCard(
            field_id="db1.t1.small_amount",
            business_name="小额",
            description="小额交易",
            category="金额",
            unit="元",
        )
        card_b = SemanticCard(
            field_id="db2.t2.large_amount",
            business_name="大额",
            description="大额交易",
            category="金额",
            unit="元",
        )

        # 完全不同的分布
        values_a = [10, 20, 30, 40, 50]
        values_b = [10000, 20000, 30000, 40000, 50000]

        result = matcher.match(card_a, card_b, values_a, values_b)

        assert result["distribution_matched"] is False
        assert result["distribution_distance"] > DISTRIBUTION_THRESHOLD

    def test_决策树_auto_map(self):
        """语义高 + 分布匹配 → auto_map"""
        engine = self._make_high_similarity_engine()
        matcher = FieldMatcher(engine)

        card_a = SemanticCard(
            field_id="sales_db.orders.sales_amount",
            business_name="销售额",
            description="销售金额",
            category="金额",
            unit="元",
        )
        card_b = SemanticCard(
            field_id="finance_db.invoices.revenue",
            business_name="收入",
            description="销售收入",
            category="金额",
            unit="元",
        )

        # 相似分布
        values_a = [100, 200, 300, 400, 500]
        values_b = [102, 198, 305, 398, 503]

        result = matcher.match(card_a, card_b, values_a, values_b)

        assert result["decision"] == "auto_map"

    def test_决策树_review_caliber(self):
        """语义高 + 分布不匹配 → review_caliber（口径差异）"""
        engine = self._make_high_similarity_engine()
        matcher = FieldMatcher(engine)

        card_a = SemanticCard(
            field_id="sales_db.orders.sales_amount",
            business_name="销售额",
            description="回款金额",
            category="金额",
            unit="元",
        )
        card_b = SemanticCard(
            field_id="finance_db.invoices.revenue",
            business_name="收入",
            description="开票金额",
            category="金额",
            unit="元",
        )

        # 系统性偏差的分布（开票比回款高约5-10%）
        values_a = [100, 200, 300, 400, 500]
        values_b = [500, 600, 700, 800, 900]  # 完全不同量级

        result = matcher.match(card_a, card_b, values_a, values_b)

        assert result["decision"] == "review_caliber"

    def test_决策树_ignore(self):
        """语义低 + 分布不匹配 → ignore"""
        engine = self._make_low_similarity_engine()
        matcher = FieldMatcher(engine)

        card_a = SemanticCard(
            field_id="sales_db.orders.order_id",
            business_name="订单编号",
            description="订单唯一标识",
            category="编码",
        )
        card_b = SemanticCard(
            field_id="finance_db.expenses.exp_date",
            business_name="费用日期",
            description="费用发生日期",
            category="时间",
        )

        # 不相关的数值
        values_a = [1, 2, 3, 4, 5]
        values_b = [1000, 2000, 3000, 4000, 5000]

        result = matcher.match(card_a, card_b, values_a, values_b)

        assert result["decision"] == "ignore"

    def test_cosine_similarity_identical(self):
        """相同向量的余弦相似度应为1"""
        vec = np.array([1.0, 2.0, 3.0])
        assert abs(cosine_similarity(vec, vec) - 1.0) < 1e-6

    def test_cosine_similarity_orthogonal(self):
        """正交向量的余弦相似度应为0"""
        vec_a = np.array([1.0, 0.0, 0.0])
        vec_b = np.array([0.0, 1.0, 0.0])
        assert abs(cosine_similarity(vec_a, vec_b)) < 1e-6

    def test_cosine_similarity_opposite(self):
        """反向向量的余弦相似度应为-1"""
        vec_a = np.array([1.0, 2.0, 3.0])
        vec_b = -vec_a
        assert abs(cosine_similarity(vec_a, vec_b) + 1.0) < 1e-6


# ============ 跨库映射测试 ============


class TestCrossDBMapping:
    """跨库映射测试"""

    def test_batch_match_过滤ignore(self):
        """batch_match应该过滤掉decision=ignore的结果"""
        engine = MockEmbeddingEngine()
        matcher = FieldMatcher(engine)

        cards_a = [
            SemanticCard(field_id="db1.t1.f1", business_name="字段1", category="其他"),
            SemanticCard(field_id="db1.t1.f2", business_name="字段2", category="其他"),
        ]
        cards_b = [
            SemanticCard(field_id="db2.t2.f1", business_name="字段3", category="其他"),
            SemanticCard(field_id="db2.t2.f2", business_name="字段4", category="其他"),
        ]

        results = matcher.batch_match(cards_a, cards_b)

        # 所有结果的decision都不应该是ignore
        for r in results:
            assert r["decision"] != "ignore"

    def test_batch_match_按置信度排序(self):
        """batch_match结果应按confidence降序排列"""
        # 构造一个能产生不同相似度的引擎
        base = np.random.RandomState(42).randn(768).astype(np.float32)
        base = base / np.linalg.norm(base)
        similar = base + np.random.RandomState(43).randn(768).astype(np.float32) * 0.02
        similar = similar / np.linalg.norm(similar)
        different = np.random.RandomState(99).randn(768).astype(np.float32)
        different = different / np.linalg.norm(different)

        engine = MockEmbeddingEngine(vectors={
            "db1.t1.amount": base,
            "db2.t2.revenue": similar,
            "db2.t2.date": different,
        })
        matcher = FieldMatcher(engine)

        cards_a = [
            SemanticCard(
                field_id="db1.t1.amount",
                business_name="金额",
                description="交易金额",
                category="金额",
            ),
        ]
        cards_b = [
            SemanticCard(
                field_id="db2.t2.revenue",
                business_name="收入",
                description="销售收入",
                category="金额",
            ),
            SemanticCard(
                field_id="db2.t2.date",
                business_name="日期",
                description="交易日期",
                category="时间",
            ),
        ]

        results = matcher.batch_match(cards_a, cards_b)

        # 验证按confidence降序
        for i in range(len(results) - 1):
            assert results[i]["confidence"] >= results[i + 1]["confidence"]

    def test_learning_engine_正例降低阈值(self):
        """用户确认匹配后，语义阈值应该降低（更宽松）"""
        engine = MockEmbeddingEngine()
        matcher = FieldMatcher(engine)
        learner = LearningEngine(matcher)

        initial_threshold = matcher.semantic_threshold

        # 模拟用户确认了多个匹配
        for _ in range(5):
            learner.record_feedback(
                semantic_score=0.82,  # 略低于阈值
                distribution_distance=0.10,
                confirmed=True,
            )

        # 阈值应该降低
        assert matcher.semantic_threshold < initial_threshold

    def test_learning_engine_负例升高阈值(self):
        """用户拒绝匹配后，语义阈值应该升高（更严格）"""
        engine = MockEmbeddingEngine()
        matcher = FieldMatcher(engine)
        learner = LearningEngine(matcher)

        initial_threshold = matcher.semantic_threshold

        # 模拟用户拒绝了多个匹配
        for _ in range(5):
            learner.record_feedback(
                semantic_score=0.88,  # 高于阈值但用户拒绝
                distribution_distance=0.20,
                confirmed=False,
            )

        # 阈值应该升高
        assert matcher.semantic_threshold > initial_threshold

    def test_learning_engine_stats(self):
        """LearningEngine应该正确报告统计信息"""
        engine = MockEmbeddingEngine()
        matcher = FieldMatcher(engine)
        learner = LearningEngine(matcher)

        learner.record_feedback(0.9, 0.1, True)
        learner.record_feedback(0.8, 0.2, False)

        stats = learner.get_stats()
        assert stats["total_feedbacks"] == 2
        assert "semantic_threshold" in stats
        assert "distribution_threshold" in stats


# ============ 权限过滤流程测试 ============


class TestPermissionFiltering:
    """权限过滤流程测试"""

    def test_admin_看到所有表(self, permission_engine, sales_tables, finance_tables):
        """管理员应该能看到所有数据库的所有表"""
        all_tables = sales_tables + finance_tables
        filtered = permission_engine.filter_schema(all_tables, "admin")

        assert len(filtered) == len(all_tables)

    def test_sales_staff_只看到销售库(self, permission_engine, sales_tables, finance_tables):
        """销售员工只能看到sales_db的orders和customers"""
        all_tables = sales_tables + finance_tables
        filtered = permission_engine.filter_schema(all_tables, "sales_staff")

        # 只能看到orders和customers，不能看到targets和finance_db的表
        table_names = [(t.db_id, t.table_name) for t in filtered]
        assert ("sales_db", "orders") in table_names
        assert ("sales_db", "customers") in table_names
        assert ("sales_db", "targets") not in table_names
        assert ("finance_db", "invoices") not in table_names
        assert ("finance_db", "expenses") not in table_names

    def test_sales_staff_看不到成本字段(self, permission_engine, sales_tables):
        """销售员工看不到cost_price和profit_margin字段"""
        filtered = permission_engine.filter_schema(sales_tables, "sales_staff")

        orders_table = next(t for t in filtered if t.table_name == "orders")
        col_names = [c.name for c in orders_table.columns]

        assert "cost_price" not in col_names
        assert "profit_margin" not in col_names
        # 其他字段应该还在
        assert "order_id" in col_names
        assert "sales_amount" in col_names
        assert "region" in col_names

    def test_sales_manager_能看到targets表(self, permission_engine, sales_tables):
        """销售经理能看到targets表"""
        filtered = permission_engine.filter_schema(sales_tables, "sales_manager")

        table_names = [t.table_name for t in filtered]
        assert "targets" in table_names
        assert "orders" in table_names
        assert "customers" in table_names

    def test_sales_manager_能看到成本字段(self, permission_engine, sales_tables):
        """销售经理没有denied_columns，能看到所有字段"""
        filtered = permission_engine.filter_schema(sales_tables, "sales_manager")

        orders_table = next(t for t in filtered if t.table_name == "orders")
        col_names = [c.name for c in orders_table.columns]

        assert "cost_price" in col_names
        assert "profit_margin" in col_names

    def test_finance_staff_跨库访问(self, permission_engine, sales_tables, finance_tables):
        """财务员工能看到finance_db和sales_db.orders"""
        all_tables = sales_tables + finance_tables
        filtered = permission_engine.filter_schema(all_tables, "finance_staff")

        table_keys = [(t.db_id, t.table_name) for t in filtered]
        assert ("finance_db", "invoices") in table_keys
        assert ("finance_db", "expenses") in table_keys
        assert ("sales_db", "orders") in table_keys
        # 不能看到customers和targets
        assert ("sales_db", "customers") not in table_keys
        assert ("sales_db", "targets") not in table_keys

    def test_unknown_role_看不到任何表(self, permission_engine, sales_tables):
        """未知角色不应该看到任何表"""
        filtered = permission_engine.filter_schema(sales_tables, "nonexistent_role")
        assert len(filtered) == 0

    def test_row_filter_替换用户属性(self, permission_engine):
        """行级过滤应该正确替换{user.xxx}占位符"""
        filters = permission_engine.get_row_filters(
            "sales_staff",
            {"region": "华东", "department": "销售一部"},
        )

        assert "sales_db.orders" in filters
        assert filters["sales_db.orders"] == "region = '华东'"

    def test_row_filter_sales_manager(self, permission_engine):
        """销售经理的行级过滤按department"""
        filters = permission_engine.get_row_filters(
            "sales_manager",
            {"department": "销售三部"},
        )

        assert "sales_db.orders" in filters
        assert filters["sales_db.orders"] == "department = '销售三部'"

    def test_admin_无行级过滤(self, permission_engine):
        """管理员没有行级过滤"""
        filters = permission_engine.get_row_filters("admin", {})
        assert len(filters) == 0

    def test_can_access_database(self, permission_engine):
        """数据库级访问控制"""
        # admin可以访问任何库
        assert permission_engine.can_access_database("admin", "sales_db") is True
        assert permission_engine.can_access_database("admin", "finance_db") is True
        assert permission_engine.can_access_database("admin", "any_db") is True

        # sales_staff只能访问sales_db
        assert permission_engine.can_access_database("sales_staff", "sales_db") is True
        assert permission_engine.can_access_database("sales_staff", "finance_db") is False

        # finance_staff可以访问两个库
        assert permission_engine.can_access_database("finance_staff", "sales_db") is True
        assert permission_engine.can_access_database("finance_staff", "finance_db") is True


# ============ 查询流程测试 ============


class TestQueryFlow:
    """查询流程测试（不涉及LLM调用）"""

    def test_context只包含允许的schema(self, permission_engine, sales_tables):
        """构建给LLM的context时，只应包含用户有权看到的schema"""
        # 模拟为sales_staff构建context
        filtered = permission_engine.filter_schema(sales_tables, "sales_staff")

        # 验证过滤后的schema不包含敏感信息
        for table in filtered:
            col_names = [c.name for c in table.columns]
            assert "cost_price" not in col_names
            assert "profit_margin" not in col_names

        # 验证只有允许的表
        table_names = [t.table_name for t in filtered]
        assert "targets" not in table_names

    def test_不同用户生成不同的row_filters(self, permission_engine):
        """不同用户应该生成不同的行级过滤条件"""
        filters_zhangsan = permission_engine.get_row_filters(
            "sales_staff", {"region": "华东", "department": "销售一部"}
        )
        filters_lisi = permission_engine.get_row_filters(
            "sales_staff", {"region": "华北", "department": "销售二部"}
        )

        assert filters_zhangsan["sales_db.orders"] == "region = '华东'"
        assert filters_lisi["sales_db.orders"] == "region = '华北'"
        assert filters_zhangsan != filters_lisi
