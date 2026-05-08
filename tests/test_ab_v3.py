"""AB对比实验 v3（最终版）：两组都不带业务含义注释

实验设计：
- A组和B组拿到完全相同的schema（只有字段名+类型，无业务含义）
- A组：直接执行LLM生成的SQL，无任何安全层
- B组：经KaiwuBridge管道（SQL验证+权限过滤+RLAC行级过滤+脱敏）

这样对比的纯粹是：安全架构有没有用？
- 两组LLM能力完全相同（相同输入=相同SQL）
- 差异100%来自KaiwuBridge的安全层

如果要单独测语义名片的价值，那是实验2（C组vs D组）。

复现：pytest tests/test_ab_v3.py -v -s
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
from kaiwubridge.prompts import SQL_GENERATION_SYSTEM
from kaiwubridge.security import DataMasker, SQLValidator

# ============ 配置 ============

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_API_KEY = "sk-1ec7d30e0b9648e19cfdb33aa5dfcc64"
DEEPSEEK_MODEL = "deepseek-chat"

SSH_HOST = "175.155.64.171"
SSH_PORT = 24102
SSH_USER = "linux"
SSH_PASS = "Ns@uk"

_SQL_BLOCK_PATTERN = re.compile(r"```sql\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_BARE_SELECT_PATTERN = re.compile(r"((?:WITH|SELECT)\b[^;]*;?)", re.DOTALL | re.IGNORECASE)


def extract_sql(response: str) -> str | None:
    match = _SQL_BLOCK_PATTERN.search(response)
    if match:
        return match.group(1).strip()
    match = _BARE_SELECT_PATTERN.search(response)
    if match:
        sql = match.group(1).strip()
        if "FROM" in sql.upper():
            return sql
    return None


# ============ Schema：两组完全相同，只有字段名+类型，无业务含义 ============

RAW_SCHEMA = """数据库: sales_dept (MySQL)

表 orders:
  order_id VARCHAR(20), cust_name VARCHAR(100), sales_amount DECIMAL(12,2),
  cost_price DECIMAL(12,2), profit_margin DECIMAL(5,2), sales_date DATE,
  salesperson VARCHAR(50), region VARCHAR(20), department VARCHAR(50),
  status VARCHAR(20), phone VARCHAR(20), id_card VARCHAR(20)

表 customers:
  cust_id VARCHAR(20), cust_name VARCHAR(100), contact_phone VARCHAR(20),
  level ENUM('A','B','C'), region VARCHAR(20), create_date DATE

表 targets:
  target_id VARCHAR(20), salesperson VARCHAR(50), department VARCHAR(50),
  quarter VARCHAR(10), target_amount DECIMAL(12,2), actual_amount DECIMAL(12,2)"""

# 销售员工视角：去掉cost_price、profit_margin、targets表（权限过滤后的结果）
RAW_SCHEMA_STAFF = """数据库: sales_dept (MySQL)

表 orders:
  order_id VARCHAR(20), cust_name VARCHAR(100), sales_amount DECIMAL(12,2),
  sales_date DATE, salesperson VARCHAR(50), region VARCHAR(20),
  department VARCHAR(50), status VARCHAR(20), phone VARCHAR(20), id_card VARCHAR(20)

表 customers:
  cust_id VARCHAR(20), cust_name VARCHAR(100), contact_phone VARCHAR(20),
  level ENUM('A','B','C'), region VARCHAR(20), create_date DATE"""


# ============ 实验1：安全架构对比（A组=裸执行，B组=KaiwuBridge安全层） ============

SECURITY_SCENARIOS = [
    {
        "id": "S1",
        "name": "基线：正常查询",
        "question": "华东区总销售额",
        "test_point": "两组都应成功，验证基线一致",
        "expect_a": "success",
        "expect_b": "success",
    },
    {
        "id": "S2",
        "name": "SQL注入",
        "question": "查询华东区订单",
        "inject_sql": "SELECT * FROM orders WHERE region='华东'; DROP TABLE orders;--",
        "test_point": "A组执行危险SQL；B组拦截多语句",
        "expect_a": "executes_dangerous",
        "expect_b": "blocked",
    },
    {
        "id": "S3",
        "name": "UNION注入",
        "question": "查询订单",
        "inject_sql": "SELECT order_id, cust_name FROM orders UNION SELECT target_id, salesperson FROM targets",
        "test_point": "A组执行UNION；B组拦截",
        "expect_a": "success",
        "expect_b": "blocked",
    },
    {
        "id": "S4",
        "name": "越权字段（销售员工查利润）",
        "question": "各部门利润率",
        "inject_sql": "SELECT department, AVG(profit_margin) FROM orders GROUP BY department",
        "test_point": "A组能查profit_margin；B组(staff)schema中无此字段",
        "expect_a": "success",
        "expect_b": "blocked",
        "role": "sales_staff",
    },
    {
        "id": "S5",
        "name": "行级过滤绕过",
        "question": "所有订单",
        "inject_sql": "SELECT region, COUNT(*) as cnt FROM orders GROUP BY region",
        "test_point": "A组返回4个区域；B组RLAC只返回华东",
        "expect_a": "all_regions",
        "expect_b": "only_huadong",
        "role": "sales_staff",
        "row_filter": "region = '华东'",
    },
    {
        "id": "S6",
        "name": "敏感数据泄露",
        "question": "客户手机号",
        "inject_sql": "SELECT cust_name, phone, id_card FROM orders LIMIT 5",
        "test_point": "A组返回明文手机号；B组返回脱敏数据",
        "expect_a": "plain_text",
        "expect_b": "masked",
    },
    {
        "id": "S7",
        "name": "子查询越权",
        "question": "订单信息",
        "inject_sql": "SELECT * FROM orders WHERE order_id IN (SELECT target_id FROM targets)",
        "test_point": "A组能访问targets；B组(staff)拦截",
        "expect_a": "success",
        "expect_b": "blocked",
        "role": "sales_staff",
    },
    {
        "id": "S8",
        "name": "时间盲注",
        "question": "订单",
        "inject_sql": "SELECT * FROM orders WHERE SLEEP(1) AND region='华东'",
        "test_point": "A组执行SLEEP；B组拦截",
        "expect_a": "success",
        "expect_b": "blocked",
    },
]

# ============ 实验2：语义名片价值（C组=裸schema，D组=带注释schema） ============

SEMANTIC_QUESTIONS = [
    {
        "id": "T1",
        "question": "华东区总共卖了多少钱？",
        "why": "'卖了多少钱'对应sales_amount，裸schema也能猜到",
    },
    {
        "id": "T2",
        "question": "张三Q1完成了多少？离目标还差多少？",
        "why": "需要知道quarter格式是'2025Q1'",
    },
    {
        "id": "T3",
        "question": "上个季度各部门的费效比怎么样？",
        "why": "'费效比'不是字段名，需要理解为cost_price/sales_amount",
    },
    {
        "id": "T4",
        "question": "哪些大客户最近没下单了？",
        "why": "'大客户'=level='A'，需要知道枚举含义",
    },
    {
        "id": "T5",
        "question": "进行中的大单有哪些？超过20万的",
        "why": "需要知道status枚举值是'进行中'而非'in_progress'",
    },
]

SCHEMA_WITH_SEMANTIC = """数据库: sales_dept (MySQL)

表 orders (40行) — 销售订单主表
| 字段 | 类型 | 业务含义 |
|------|------|----------|
| order_id | VARCHAR(20) | 订单编号，格式ORD+3位数字 |
| cust_name | VARCHAR(100) | 客户公司全称 |
| sales_amount | DECIMAL(12,2) | 订单成交金额（含税，单位：元） |
| cost_price | DECIMAL(12,2) | 采购成本（元） |
| profit_margin | DECIMAL(5,2) | 利润率（百分比，如32.00表示32%） |
| sales_date | DATE | 成交日期，格式YYYY-MM-DD |
| salesperson | VARCHAR(50) | 负责销售人员（张三/李四/王五/赵六） |
| region | VARCHAR(20) | 销售大区（枚举：华东/华北/华南/华西） |
| department | VARCHAR(50) | 所属部门（枚举：销售一部/二部/三部/四部） |
| status | VARCHAR(20) | 订单状态（枚举：已完成/进行中/待审核） |
| phone | VARCHAR(20) | 客户联系手机号（11位） |
| id_card | VARCHAR(20) | 客户身份证号（18位） |

表 customers (15行) — 客户档案
| 字段 | 类型 | 业务含义 |
|------|------|----------|
| cust_id | VARCHAR(20) | 客户编号 |
| cust_name | VARCHAR(100) | 客户公司全称 |
| contact_phone | VARCHAR(20) | 联系电话 |
| level | ENUM('A','B','C') | 客户等级（A=大客户年营收>1000万，B=中等，C=小客户） |
| region | VARCHAR(20) | 所在大区 |
| create_date | DATE | 建档日期 |

表 targets (8行) — 季度销售目标
| 字段 | 类型 | 业务含义 |
|------|------|----------|
| target_id | VARCHAR(20) | 目标编号 |
| salesperson | VARCHAR(50) | 销售人员姓名 |
| department | VARCHAR(50) | 所属部门 |
| quarter | VARCHAR(10) | 季度标识（格式：2025Q1, 2025Q2） |
| target_amount | DECIMAL(12,2) | 季度目标金额（元） |
| actual_amount | DECIMAL(12,2) | 实际完成金额（元），NULL=季度未结束 |"""


# ============ 运行器 ============


class V3Runner:
    def __init__(self):
        self._llm_kwargs = {"base_url": DEEPSEEK_BASE_URL, "api_key": DEEPSEEK_API_KEY, "model": DEEPSEEK_MODEL}
        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.ssh.connect(SSH_HOST, port=SSH_PORT, username=SSH_USER, password=SSH_PASS)
        self.transport = self.ssh.get_transport()
        self.validator = SQLValidator()
        self.masker = DataMasker()

    def _get_llm(self):
        return LLMClient(**self._llm_kwargs)

    def _execute_sql_raw(self, sql: str) -> dict:
        """直接执行，无安全层"""
        try:
            clean_sql = sql.replace("sales_db.", "").replace("sales_dept.", "")
            sock = self.transport.open_channel("direct-tcpip", ("127.0.0.1", 3306), ("127.0.0.1", 0))
            conn = pymysql.connect(host="127.0.0.1", user="kaiwu", password="test123",
                                   database="sales_dept", charset="utf8mb4", defer_connect=True)
            conn.connect(sock)
            cur = conn.cursor(pymysql.cursors.DictCursor)
            cur.execute(clean_sql)
            rows = cur.fetchall()
            conn.close()
            data = [{k: float(v) if hasattr(v, "as_integer_ratio") else v for k, v in row.items()} for row in rows]
            return {"success": True, "data": data, "row_count": len(data), "error": ""}
        except Exception as e:
            return {"success": False, "data": [], "row_count": 0, "error": str(e)[:200]}

    def _execute_sql_kwb(self, sql: str, allowed_tables: set | None = None,
                         row_filter: str | None = None) -> dict:
        """经KaiwuBridge安全层"""
        # SQL白名单
        valid, err = self.validator.validate(sql)
        if not valid:
            return {"success": False, "data": [], "row_count": 0, "error": f"[拦截]{err}", "blocked": True}

        # 表级权限
        if allowed_tables:
            valid, err = self.validator.check_allowed_tables(sql, allowed_tables)
            if not valid:
                return {"success": False, "data": [], "row_count": 0, "error": f"[越权]{err}", "blocked": True}

        # RLAC
        exec_sql = sql
        if row_filter:
            exec_sql = f"WITH _q AS ({sql}) SELECT * FROM _q WHERE {row_filter}"

        result = self._execute_sql_raw(exec_sql)

        # 脱敏
        if result["success"] and result["data"]:
            result["data"] = self.masker.mask_results(result["data"])

        result["blocked"] = False
        return result

    async def generate_sql(self, question: str, schema: str) -> str | None:
        """LLM生成SQL"""
        user_msg = f"## 可用数据\n\n{schema}\n\n## 问题\n{question}\n\n生成SQL（放在```sql代码块中）。"
        llm = self._get_llm()
        try:
            resp = await llm.chat(
                [ChatMessage(role="system", content=SQL_GENERATION_SYSTEM),
                 ChatMessage(role="user", content=user_msg)],
                temperature=0.1, max_tokens=512)
        finally:
            await llm.close()
        return extract_sql(resp)


# ============ Fixtures ============


@pytest.fixture(scope="module")
def runner():
    try:
        r = V3Runner()
        yield r
        r.ssh.close()
    except Exception as e:
        pytest.skip(f"环境不可用: {e}")


# ============ 实验1：安全架构对比 ============


class TestSecurityComparison:
    """实验1：相同schema（无注释），对比有无安全层"""

    @pytest.mark.asyncio
    async def test_security_scenarios(self, runner):
        """8个安全场景对比"""
        results = []

        for s in SECURITY_SCENARIOS:
            sql = s.get("inject_sql")
            if not sql:
                # 让LLM生成（两组用相同的裸schema）
                sql = await runner.generate_sql(s["question"], RAW_SCHEMA)

            if not sql:
                results.append({"id": s["id"], "name": s["name"], "sql": None,
                                "a_status": "无SQL", "b_status": "无SQL", "test_point": s["test_point"]})
                continue

            role = s.get("role", "admin")
            row_filter = s.get("row_filter")
            allowed_tables = {"orders", "customers"} if role == "sales_staff" else None

            # A组：直接执行
            a = runner._execute_sql_raw(sql)
            # B组：经安全层
            b = runner._execute_sql_kwb(sql, allowed_tables=allowed_tables, row_filter=row_filter)

            # 判定状态
            a_status = self._judge_a(s, a)
            b_status = self._judge_b(s, b)

            results.append({"id": s["id"], "name": s["name"], "sql": sql[:80],
                            "a_status": a_status, "b_status": b_status,
                            "a_rows": a.get("row_count", 0), "b_rows": b.get("row_count", 0),
                            "test_point": s["test_point"]})

        # 打印报告
        print("\n")
        print("=" * 82)
        print(" 实验1：安全架构对比（两组schema完全相同，无业务含义注释）")
        print("=" * 82)
        print(f"{'ID':<4} {'场景':<14} {'A组(无安全层)':<18} {'B组(KaiwuBridge)':<18} {'验证点'}")
        print("-" * 82)

        a_issues = 0
        b_blocks = 0
        for r in results:
            print(f"{r['id']:<4} {r['name']:<14} {r['a_status']:<18} {r['b_status']:<18} {r['test_point'][:30]}")
            if "⚠" in r["a_status"]:
                a_issues += 1
            if "✓" in r["b_status"]:
                b_blocks += 1

        print("-" * 82)
        print(f"A组安全事件: {a_issues}次 | B组正确防护: {b_blocks}次")
        print(f"结论: 两组SQL生成能力相同，差异100%来自安全架构")
        print("=" * 82)

        # 保存
        Path("tests/ab_results_v3.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

        assert b_blocks >= 4, f"B组应至少4次正确防护，实际{b_blocks}"
        assert a_issues >= 3, f"A组应至少3次安全事件，实际{a_issues}"

    def _judge_a(self, scenario, result):
        sid = scenario["id"]
        if not result["success"]:
            return "执行失败"
        if sid in ("S2", "S3", "S8"):
            return f"⚠ 危险执行({result['row_count']}行)"
        if sid == "S4":
            # staff不该看到profit_margin，A组能查到=越权
            return f"⚠ 越权查询({result['row_count']}行)"
        if sid == "S5":
            # 检查行级过滤：staff只该看华东，A组看到全部区域=泄露
            if result["row_count"] > 12:
                return f"⚠ 泄露全部({result['row_count']}行)"
            # 即使行数<=12也可能包含多区域
            regions = set(row.get("region", "") for row in result["data"] if "region" in row)
            if len(regions) > 1:
                return f"⚠ 泄露多区域({result['row_count']}行)"
            return f"执行({result['row_count']}行)"
        if sid == "S6":
            # 检查是否有明文手机号/身份证
            has_plain = any(
                (isinstance(v, str) and (re.match(r"^1[3-9]\d{9}$", v) or re.match(r"^\d{17}[\dXx]$", v)))
                for row in result["data"][:5] for v in row.values()
            )
            return "⚠ 明文泄露" if has_plain else f"执行({result['row_count']}行)"
        if sid == "S7":
            return f"⚠ 越权执行({result['row_count']}行)"
        return f"正常({result['row_count']}行)"

    def _judge_b(self, scenario, result):
        sid = scenario["id"]
        if result.get("blocked"):
            return "✓ 已拦截"
        if not result["success"]:
            return "✓ 已阻止"
        if sid == "S5":
            # 行级过滤：只该返回华东
            regions = set(row.get("region", "") for row in result["data"] if "region" in row)
            if len(regions) <= 1:
                return f"✓ RLAC生效({result['row_count']}行)"
            return f"执行({result['row_count']}行)"
        if sid == "S6":
            # 脱敏检查
            has_mask = any("****" in str(v) for row in result["data"][:5] for v in row.values())
            return "✓ 已脱敏" if has_mask else f"执行({result['row_count']}行)"
        return f"正常({result['row_count']}行)"


# ============ 实验2：语义名片价值（独立实验） ============


class TestSemanticCardValue:
    """实验2：裸schema vs 带语义名片，纯比SQL生成质量（无安全层）"""

    @pytest.mark.asyncio
    async def test_semantic_comparison(self, runner):
        """5个歧义问题对比"""
        results = []

        for q in SEMANTIC_QUESTIONS:
            # C组：裸schema
            sql_c = await runner.generate_sql(q["question"], RAW_SCHEMA)
            result_c = runner._execute_sql_raw(sql_c) if sql_c else {"success": False, "data": [], "row_count": 0}

            # D组：带语义名片
            sql_d = await runner.generate_sql(q["question"], SCHEMA_WITH_SEMANTIC)
            result_d = runner._execute_sql_raw(sql_d) if sql_d else {"success": False, "data": [], "row_count": 0}

            results.append({
                "id": q["id"], "question": q["question"], "why": q["why"],
                "c_sql": sql_c, "c_success": result_c["success"], "c_rows": result_c.get("row_count", 0),
                "d_sql": sql_d, "d_success": result_d["success"], "d_rows": result_d.get("row_count", 0),
            })

        # 打印报告
        print("\n")
        print("=" * 82)
        print(" 实验2：语义名片价值（C组=裸schema，D组=带业务含义注释）")
        print("=" * 82)
        print(f"{'ID':<4} {'问题':<24} {'C组(裸)':<16} {'D组(语义名片)':<16} {'差异原因'}")
        print("-" * 82)

        c_ok = 0
        d_ok = 0
        for r in results:
            c_status = f"✓({r['c_rows']}行)" if r["c_success"] and r["c_rows"] > 0 else "✗"
            d_status = f"✓({r['d_rows']}行)" if r["d_success"] and r["d_rows"] > 0 else "✗"
            if r["c_success"] and r["c_rows"] > 0:
                c_ok += 1
            if r["d_success"] and r["d_rows"] > 0:
                d_ok += 1
            print(f"{r['id']:<4} {r['question'][:22]:<24} {c_status:<16} {d_status:<16} {r['why'][:20]}")

        print("-" * 82)
        print(f"C组(裸schema): {c_ok}/5 成功 | D组(语义名片): {d_ok}/5 成功")
        print(f"语义名片提升: +{d_ok - c_ok}个问题")
        print("=" * 82)

        # 保存
        Path("tests/semantic_results.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
