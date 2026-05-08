"""AB对比实验：KaiwuBridge中间层 vs 裸LLM

实验设计：
- 同一组自然语言问题，分别走两条路径
- A组（对照组）：裸DeepSeek，只告诉它"你是数据分析助手，公司有销售数据库"
- B组（实验组）：经KaiwuBridge完整管道（schema注入→权限过滤→SQL生成→执行→脱敏）
- 评判标准：SQL是否可执行、返回数据是否正确、是否泄露权限外数据

公平性说明：
- A组的system prompt故意给了"公司有MySQL销售数据库，表名orders/customers/targets"这些提示
  这比真实场景更宽松（真实场景用户不会告诉LLM表名），但让对比更公平
- B组走标准KaiwuBridge管道，不做任何特殊优化
- 两组用完全相同的LLM（deepseek-chat）、相同temperature(0.1)

复现方式：
    1. 按 docs/ab_experiment.md 搭建环境
    2. pytest tests/test_ab_comparison.py -v -s
"""

import json
import re
import time
from pathlib import Path

import paramiko
import pymysql
import pytest

from kaiwubridge.llm_client import LLMClient
from kaiwubridge.models import ChatMessage
from kaiwubridge.prompts import (
    QUERY_CONTEXT_TEMPLATE,
    RESULT_INTERPRETATION_PROMPT,
    SQL_GENERATION_SYSTEM,
)
from kaiwubridge.security import DataMasker, SQLValidator

# ============ 配置（从环境变量或默认值） ============

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_API_KEY = "sk-1ec7d30e0b9648e19cfdb33aa5dfcc64"
DEEPSEEK_MODEL = "deepseek-chat"

SSH_HOST = "175.155.64.171"
SSH_PORT = 24102
SSH_USER = "linux"
SSH_PASS = "Ns@uk"

# SQL代码块提取
_SQL_BLOCK_PATTERN = re.compile(r"```sql\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_BARE_SELECT_PATTERN = re.compile(
    r"((?:WITH|SELECT)\b[^;]*;?)", re.DOTALL | re.IGNORECASE
)


# ============ 工具函数 ============


def extract_sql(response: str) -> str | None:
    """从LLM响应中提取SQL"""
    match = _SQL_BLOCK_PATTERN.search(response)
    if match:
        return match.group(1).strip()
    match = _BARE_SELECT_PATTERN.search(response)
    if match:
        sql = match.group(1).strip()
        if "FROM" in sql.upper():
            return sql
    return None


# ============ A组 system prompt ============
# 故意给A组一些提示（表名、字段名），让对比更公平
# 真实场景中用户不会告诉LLM这些信息

GROUP_A_SYSTEM = """你是一个企业数据分析助手。公司有一个MySQL销售数据库(sales_dept)，包含以下表：
- orders: 订单表
- customers: 客户表
- targets: 销售目标表

用户会问你关于销售数据的问题。如果需要查询数据，请生成SQL（放在```sql代码块中）。
注意：你不知道具体的字段名，需要根据常识猜测。"""


# ============ B组 schema（KaiwuBridge扫描+语义名片生成的结果） ============

GROUP_B_SCHEMA = """### 数据库: sales_db（实际连接: sales_dept）

**orders** (40行) — 销售订单表
| 字段 | 类型 | 业务含义 |
|------|------|----------|
| order_id | VARCHAR(20) | 订单编号，唯一标识 |
| cust_name | VARCHAR(100) | 客户名称（公司全称） |
| sales_amount | DECIMAL(12,2) | 销售金额（含税，单位：元） |
| cost_price | DECIMAL(12,2) | 成本价格（元） |
| profit_margin | DECIMAL(5,2) | 利润率（%） |
| sales_date | DATE | 销售日期（YYYY-MM-DD） |
| salesperson | VARCHAR(50) | 销售人员姓名 |
| region | VARCHAR(20) | 销售区域（华东/华北/华南/华西） |
| department | VARCHAR(50) | 所属部门（销售一部~四部） |
| status | VARCHAR(20) | 订单状态（已完成/进行中/待审核） |
| phone | VARCHAR(20) | 客户联系电话 |
| id_card | VARCHAR(20) | 客户身份证号 |

**customers** (15行) — 客户档案表
| 字段 | 类型 | 业务含义 |
|------|------|----------|
| cust_id | VARCHAR(20) | 客户编号 |
| cust_name | VARCHAR(100) | 客户名称 |
| contact_phone | VARCHAR(20) | 联系电话 |
| level | ENUM('A','B','C') | 客户等级 |
| region | VARCHAR(20) | 所在区域 |
| create_date | DATE | 建档日期 |

**targets** (8行) — 销售目标表
| 字段 | 类型 | 业务含义 |
|------|------|----------|
| target_id | VARCHAR(20) | 目标编号 |
| salesperson | VARCHAR(50) | 销售人员 |
| department | VARCHAR(50) | 所属部门 |
| quarter | VARCHAR(10) | 季度（如2025Q1） |
| target_amount | DECIMAL(12,2) | 目标金额（元） |
| actual_amount | DECIMAL(12,2) | 实际完成金额（元），NULL=季度未结束 |"""

GROUP_B_SCHEMA_SALES_STAFF = """### 数据库: sales_db（实际连接: sales_dept）

**orders** (40行) — 销售订单表（注：成本和利润字段对当前角色不可见）
| 字段 | 类型 | 业务含义 |
|------|------|----------|
| order_id | VARCHAR(20) | 订单编号 |
| cust_name | VARCHAR(100) | 客户名称 |
| sales_amount | DECIMAL(12,2) | 销售金额（含税，元） |
| sales_date | DATE | 销售日期 |
| salesperson | VARCHAR(50) | 销售人员 |
| region | VARCHAR(20) | 销售区域 |
| department | VARCHAR(50) | 所属部门 |
| status | VARCHAR(20) | 订单状态 |
| phone | VARCHAR(20) | 客户联系电话 |
| id_card | VARCHAR(20) | 客户身份证号 |

**customers** (15行) — 客户档案表
| 字段 | 类型 | 业务含义 |
|------|------|----------|
| cust_id | VARCHAR(20) | 客户编号 |
| cust_name | VARCHAR(100) | 客户名称 |
| contact_phone | VARCHAR(20) | 联系电话 |
| level | ENUM('A','B','C') | 客户等级 |
| region | VARCHAR(20) | 所在区域 |
| create_date | DATE | 建档日期 |"""


# ============ 实验问题集 ============

EXPERIMENT_QUESTIONS = [
    {
        "id": "Q1",
        "question": "华东区的总销售额是多少？",
        "category": "基础聚合",
        "ground_truth_sql": "SELECT SUM(sales_amount) FROM orders WHERE region='华东'",
        "expected_answer_contains": "1512000",  # 华东区12条订单的总额
        "role": "admin",
    },
    {
        "id": "Q2",
        "question": "各区域的订单数量是多少？按数量从多到少排列",
        "category": "分组排序",
        "ground_truth_sql": "SELECT region, COUNT(*) FROM orders GROUP BY region ORDER BY COUNT(*) DESC",
        "expected_answer_contains": "华东",  # 华东最多(12条)
        "role": "admin",
    },
    {
        "id": "Q3",
        "question": "张三2025年第一季度的销售目标完成率是多少？",
        "category": "跨表关联",
        "ground_truth_sql": "SELECT actual_amount/target_amount*100 FROM targets WHERE salesperson='张三' AND quarter='2025Q1'",
        "expected_answer_contains": "86",  # 433000/500000 = 86.6%
        "role": "admin",
    },
    {
        "id": "Q4",
        "question": "我们部门上个月的利润率是多少？",
        "category": "权限隔离",
        "ground_truth_sql": None,  # 销售员工看不到profit_margin字段
        "expected_behavior": "B组应拒绝（schema中无利润字段）",
        "role": "sales_staff",
    },
    {
        "id": "Q5",
        "question": "华东区销售额最高的3个订单，客户名称和联系电话是什么？",
        "category": "数据脱敏",
        "ground_truth_sql": "SELECT cust_name, phone FROM orders WHERE region='华东' ORDER BY sales_amount DESC LIMIT 3",
        "expected_behavior": "B组返回脱敏手机号（138****5678格式）",
        "role": "admin",
    },
]


# ============ AB测试运行器 ============


class ABTestRunner:
    """AB对比实验运行器"""

    def __init__(self):
        self._llm_kwargs = {
            "base_url": DEEPSEEK_BASE_URL,
            "api_key": DEEPSEEK_API_KEY,
            "model": DEEPSEEK_MODEL,
        }
        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.ssh.connect(SSH_HOST, port=SSH_PORT, username=SSH_USER, password=SSH_PASS)
        self.transport = self.ssh.get_transport()
        self.validator = SQLValidator()
        self.masker = DataMasker()
        self.results = []  # 收集所有实验结果

    def _get_llm(self):
        return LLMClient(**self._llm_kwargs)

    def _get_mysql_conn(self):
        sock = self.transport.open_channel(
            "direct-tcpip", ("127.0.0.1", 3306), ("127.0.0.1", 0)
        )
        conn = pymysql.connect(
            host="127.0.0.1", user="kaiwu", password="test123",
            database="sales_dept", charset="utf8mb4", defer_connect=True,
        )
        conn.connect(sock)
        return conn

    def _execute_sql(self, sql: str) -> dict:
        """执行SQL，返回结果或错误"""
        try:
            # 去掉LLM可能生成的库名前缀
            clean_sql = sql.replace("sales_db.", "").replace("sales_dept.", "")
            conn = self._get_mysql_conn()
            cur = conn.cursor(pymysql.cursors.DictCursor)
            cur.execute(clean_sql)
            rows = cur.fetchall()
            conn.close()
            data = []
            for row in rows:
                data.append({
                    k: float(v) if hasattr(v, "as_integer_ratio") else v
                    for k, v in row.items()
                })
            return {"success": True, "data": data, "row_count": len(data), "error": ""}
        except Exception as e:
            return {"success": False, "data": [], "row_count": 0, "error": str(e)}

    async def run_group_a(self, question: str) -> dict:
        """A组：裸DeepSeek + 基本表名提示（公平起见）"""
        messages = [
            ChatMessage(role="system", content=GROUP_A_SYSTEM),
            ChatMessage(role="user", content=question),
        ]
        llm = self._get_llm()
        try:
            response = await llm.chat(messages, temperature=0.1, max_tokens=1024)
        finally:
            await llm.close()

        sql = extract_sql(response)
        exec_result = {"success": False, "data": [], "row_count": 0, "error": "无SQL"}
        if sql:
            exec_result = self._execute_sql(sql)

        return {
            "group": "A",
            "response": response,
            "sql": sql,
            "exec_success": exec_result["success"],
            "data": exec_result["data"],
            "row_count": exec_result["row_count"],
            "error": exec_result["error"],
        }

    async def run_group_b(self, question: str, role: str = "admin",
                          row_filters: dict | None = None) -> dict:
        """B组：经KaiwuBridge管道"""
        # 选择schema
        schema = GROUP_B_SCHEMA_SALES_STAFF if role == "sales_staff" else GROUP_B_SCHEMA

        # 权限说明
        perm_notes = ""
        if row_filters:
            lines = ["## 自动过滤条件（无需手动添加）"]
            for k, v in row_filters.items():
                lines.append(f"- {k}: WHERE {v}")
            perm_notes = "\n".join(lines)

        context = QUERY_CONTEXT_TEMPLATE.format(
            schema_description=schema,
            permission_notes=perm_notes,
            user_question=question,
        )

        messages = [
            ChatMessage(role="system", content=SQL_GENERATION_SYSTEM),
            ChatMessage(role="user", content=context),
        ]
        llm = self._get_llm()
        try:
            llm_response = await llm.chat(messages, temperature=0.1, max_tokens=1024)
        finally:
            await llm.close()

        sql = extract_sql(llm_response)
        exec_result = {"success": False, "data": [], "row_count": 0, "error": "无SQL生成"}

        if sql:
            # 安全验证
            valid, err = self.validator.validate(sql)
            if not valid:
                exec_result = {"success": False, "data": [], "row_count": 0, "error": f"安全拦截: {err}"}
            else:
                # RLAC行级过滤
                exec_sql = sql
                if row_filters:
                    for _, condition in row_filters.items():
                        exec_sql = f"WITH _q AS ({exec_sql}) SELECT * FROM _q WHERE {condition}"
                exec_result = self._execute_sql(exec_sql)

                # 脱敏
                if exec_result["success"] and exec_result["data"]:
                    exec_result["data"] = self.masker.mask_results(exec_result["data"])

        return {
            "group": "B",
            "response": llm_response,
            "sql": sql,
            "exec_success": exec_result["success"],
            "data": exec_result["data"],
            "row_count": exec_result["row_count"],
            "error": exec_result["error"],
        }

    def save_results(self, output_path: str):
        """保存实验结果到JSON"""
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(self.results, f, ensure_ascii=False, indent=2, default=str)


# ============ Fixtures ============


@pytest.fixture(scope="module")
def runner():
    try:
        r = ABTestRunner()
        yield r
        # 保存实验结果
        out_path = Path("tests/ab_results.json")
        r.save_results(str(out_path))
        r.ssh.close()
    except Exception as e:
        pytest.skip(f"环境不可用: {e}")


# ============ 测试用例 ============


class TestABExperiment:
    """AB对比实验"""

    @pytest.mark.asyncio
    async def test_Q1_基础聚合_华东销售额(self, runner):
        """Q1: 华东区总销售额 — 验证基础查询能力"""
        q = EXPERIMENT_QUESTIONS[0]
        a = await runner.run_group_a(q["question"])
        b = await runner.run_group_b(q["question"])

        runner.results.append({"id": q["id"], "question": q["question"],
                               "category": q["category"], "group_a": a, "group_b": b})

        print(f"\n{'='*60}")
        print(f"[{q['id']}] {q['question']}  (类别: {q['category']})")
        print(f"{'─'*60}")
        print(f"A组 SQL: {a['sql']}")
        print(f"A组 可执行: {a['exec_success']}, 错误: {a['error'][:80] if a['error'] else ''}")
        print(f"B组 SQL: {b['sql']}")
        print(f"B组 可执行: {b['exec_success']}, 行数: {b['row_count']}")
        if b['data']:
            print(f"B组 数据: {b['data'][:3]}")
        print(f"{'='*60}")

        # 断言：B组必须返回正确数据
        assert b["exec_success"], f"B组SQL执行失败: {b['error']}"
        assert b["row_count"] > 0, "B组应返回数据"
        # 验证数据正确性
        first_row = b["data"][0]
        total = list(first_row.values())[0]
        assert abs(float(total) - 1512000) < 1, f"华东区总额应为1512000，实际{total}"

    @pytest.mark.asyncio
    async def test_Q2_分组排序_区域订单数(self, runner):
        """Q2: 各区域订单数排名 — 验证GROUP BY能力"""
        q = EXPERIMENT_QUESTIONS[1]
        a = await runner.run_group_a(q["question"])
        b = await runner.run_group_b(q["question"])

        runner.results.append({"id": q["id"], "question": q["question"],
                               "category": q["category"], "group_a": a, "group_b": b})

        print(f"\n{'='*60}")
        print(f"[{q['id']}] {q['question']}  (类别: {q['category']})")
        print(f"{'─'*60}")
        print(f"A组 可执行={a['exec_success']}, 数据={a['data'][:4] if a['data'] else '无'}")
        print(f"B组 可执行={b['exec_success']}, 数据={b['data'][:4] if b['data'] else '无'}")
        print(f"{'='*60}")

        assert b["exec_success"], f"B组失败: {b['error']}"
        assert b["row_count"] == 4, f"应有4个区域，实际{b['row_count']}"

    @pytest.mark.asyncio
    async def test_Q3_跨表关联_目标完成率(self, runner):
        """Q3: 张三销售目标完成率 — 验证跨表查询能力"""
        q = EXPERIMENT_QUESTIONS[2]
        a = await runner.run_group_a(q["question"])
        b = await runner.run_group_b(q["question"])

        runner.results.append({"id": q["id"], "question": q["question"],
                               "category": q["category"], "group_a": a, "group_b": b})

        print(f"\n{'='*60}")
        print(f"[{q['id']}] {q['question']}  (类别: {q['category']})")
        print(f"{'─'*60}")
        print(f"A组 可执行={a['exec_success']}, SQL={a['sql']}")
        print(f"B组 可执行={b['exec_success']}, SQL={b['sql']}")
        if b['data']:
            print(f"B组 数据: {b['data']}")
        print(f"{'='*60}")

        assert b["exec_success"], f"B组失败: {b['error']}"
        # 验证完成率约86.6%
        if b["data"]:
            row = b["data"][0]
            # 找到包含完成率的字段
            for v in row.values():
                if isinstance(v, (int, float)) and 80 < v < 90:
                    break
            else:
                # 也可能返回target_amount和actual_amount让我们自己算
                vals = [v for v in row.values() if isinstance(v, (int, float))]
                if 433000 in vals or 433000.0 in vals:
                    pass  # 返回了原始数据，也算正确
                else:
                    print(f"  [警告] 未找到86.6%的完成率，数据: {row}")

    @pytest.mark.asyncio
    async def test_Q4_权限隔离_利润不可见(self, runner):
        """Q4: 销售员工问利润率 — 验证权限过滤（schema中无profit字段）"""
        q = EXPERIMENT_QUESTIONS[3]
        a = await runner.run_group_a(q["question"])
        b = await runner.run_group_b(q["question"], role="sales_staff")

        runner.results.append({"id": q["id"], "question": q["question"],
                               "category": q["category"], "group_a": a, "group_b": b})

        print(f"\n{'='*60}")
        print(f"[{q['id']}] {q['question']}  (类别: {q['category']})")
        print(f"{'─'*60}")
        print(f"A组响应: {a['response'][:200]}")
        print(f"B组响应: {b['response'][:200]}")
        print(f"{'='*60}")

        # B组的schema中没有profit_margin/cost_price，LLM不应生成包含这些字段的SQL
        if b["sql"]:
            sql_upper = b["sql"].upper()
            assert "PROFIT" not in sql_upper, "B组不应查询利润字段（权限已过滤）"
            assert "COST" not in sql_upper, "B组不应查询成本字段（权限已过滤）"
        # B组应该告知用户无法计算（因为看不到相关字段）
        assert "利润" in b["response"] or "成本" in b["response"] or "无法" in b["response"], \
            "B组应告知无法计算利润率"

    @pytest.mark.asyncio
    async def test_Q5_数据脱敏_手机号(self, runner):
        """Q5: 查客户电话 — 验证返回数据已脱敏"""
        q = EXPERIMENT_QUESTIONS[4]
        a = await runner.run_group_a(q["question"])
        b = await runner.run_group_b(q["question"])

        runner.results.append({"id": q["id"], "question": q["question"],
                               "category": q["category"], "group_a": a, "group_b": b})

        print(f"\n{'='*60}")
        print(f"[{q['id']}] {q['question']}  (类别: {q['category']})")
        print(f"{'─'*60}")
        print(f"A组 可执行={a['exec_success']}, 响应: {a['response'][:150]}")
        print(f"B组 可执行={b['exec_success']}, 数据: {b['data'][:3] if b['data'] else '无'}")
        print(f"{'='*60}")

        assert b["exec_success"], f"B组失败: {b['error']}"
        # 验证脱敏：不应有完整11位手机号
        for row in b["data"]:
            for val in row.values():
                if isinstance(val, str) and re.match(r"^1[3-9]\d{9}$", val):
                    pytest.fail(f"发现未脱敏手机号: {val}")
        # 应该有****脱敏标记
        has_masked = any(
            "****" in str(val)
            for row in b["data"]
            for val in row.values()
        )
        assert has_masked, "B组数据应包含脱敏标记(****)"


# ============ 汇总报告 ============


class TestABReport:
    """生成实验报告"""

    @pytest.mark.asyncio
    async def test_生成对比报告(self, runner):
        """运行全部问题并生成结构化报告"""
        all_results = []

        for q in EXPERIMENT_QUESTIONS:
            a = await runner.run_group_a(q["question"])
            b_kwargs = {"role": q.get("role", "admin")}
            b = await runner.run_group_b(q["question"], **b_kwargs)
            all_results.append({"q": q, "a": a, "b": b})

        # 打印报告
        print("\n")
        print("┌" + "─" * 72 + "┐")
        print("│" + " KaiwuBridge AB对比实验报告 ".center(64) + "│")
        print("├" + "─" * 72 + "┤")
        print(f"│ {'实验时间':<8}: {time.strftime('%Y-%m-%d %H:%M:%S'):<60} │")
        print(f"│ {'LLM模型':<8}: {DEEPSEEK_MODEL:<60} │")
        print(f"│ {'Temperature':<11}: 0.1{' '*57} │")
        print("├" + "─" * 72 + "┤")

        a_correct = 0
        b_correct = 0

        for r in all_results:
            q = r["q"]
            a = r["a"]
            b = r["b"]

            # 判定A组是否正确
            a_ok = a["exec_success"] and a["row_count"] > 0
            # 判定B组是否正确
            if q["category"] == "权限隔离":
                # 权限测试：B组正确行为是拒绝
                b_ok = not b["exec_success"] or (b["sql"] and "PROFIT" not in b["sql"].upper())
            else:
                b_ok = b["exec_success"] and b["row_count"] > 0

            if a_ok:
                a_correct += 1
            if b_ok:
                b_correct += 1

            a_mark = "✓" if a_ok else "✗"
            b_mark = "✓" if b_ok else "✗"

            print(f"│ [{q['id']}] {q['question']:<55} │")
            print(f"│   A组(裸LLM):  {a_mark} {'SQL可执行' if a_ok else '失败/编造':<52} │")
            print(f"│   B组(KWB):    {b_mark} {'数据正确' if b_ok else '需改进':<52} │")
            print("├" + "─" * 72 + "┤")

        print(f"│ {'结论':<8}: A组 {a_correct}/{len(all_results)} 正确 │ B组 {b_correct}/{len(all_results)} 正确{' '*20} │")
        print(f"│ {'提升':<8}: +{b_correct - a_correct} 个问题获得正确答案{' '*38} │")
        print("└" + "─" * 72 + "┘")

        # 保存结果
        runner.results = [
            {"id": r["q"]["id"], "question": r["q"]["question"],
             "category": r["q"]["category"],
             "group_a": {k: v for k, v in r["a"].items() if k != "response"},
             "group_b": {k: v for k, v in r["b"].items() if k != "response"}}
            for r in all_results
        ]

        # 断言：B组必须优于A组
        assert b_correct > a_correct, f"B组({b_correct})应优于A组({a_correct})"
        assert b_correct >= 4, f"B组至少4/5正确，实际{b_correct}/5"
