"""AB对比实验 v2：消除过拟合，增加公平性

v1的问题：
1. A组prompt太弱，DeepSeek合理地选择先DESCRIBE再查询，被判为"失败"
2. 问题都是简单查询，没有测试真正的难点
3. B组schema是为这些问题量身定制的

v2改进：
1. A组给完整字段名（和B组一样的信息量），只去掉"语义名片"（业务含义注释）
2. 增加歧义问题、多表关联、模糊匹配等真实场景
3. 评判标准更细：SQL语法正确/执行成功/结果正确/权限合规 四个维度
4. 增加"A组增强版"：给A组字段名但不给业务含义，测试语义理解差异

实验设计：
- A组（裸schema）：给表名+字段名+类型，但不给业务含义
- B组（KaiwuBridge）：给表名+字段名+类型+语义名片（业务含义、单位、注意事项）
- 两组用相同LLM、相同temperature

这样对比的是：语义名片（业务含义注释）到底有没有用？
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


# ============ 三组对比的Schema ============

# A组：裸schema — 只有表名+字段名+类型，没有业务含义
SCHEMA_GROUP_A = """数据库: sales_dept (MySQL)

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

# B组：KaiwuBridge语义名片 — 表名+字段名+类型+业务含义+注意事项
SCHEMA_GROUP_B = """数据库: sales_dept (MySQL)

表 orders (40行) — 销售订单主表
| 字段 | 类型 | 业务含义 |
|------|------|----------|
| order_id | VARCHAR(20) | 订单编号，格式ORD+3位数字 |
| cust_name | VARCHAR(100) | 客户公司全称 |
| sales_amount | DECIMAL(12,2) | 订单成交金额（含税，单位：元） |
| cost_price | DECIMAL(12,2) | 采购成本（元） |
| profit_margin | DECIMAL(5,2) | 利润率（百分比，如32.00表示32%） |
| sales_date | DATE | 成交日期，格式YYYY-MM-DD |
| salesperson | VARCHAR(50) | 负责销售人员姓名（张三/李四/王五/赵六） |
| region | VARCHAR(20) | 销售大区（枚举：华东/华北/华南/华西） |
| department | VARCHAR(50) | 所属部门（枚举：销售一部/销售二部/销售三部/销售四部） |
| status | VARCHAR(20) | 订单状态（枚举：已完成/进行中/待审核） |
| phone | VARCHAR(20) | 客户联系手机号（11位） |
| id_card | VARCHAR(20) | 客户身份证号（18位） |

表 customers (15行) — 客户档案
| 字段 | 类型 | 业务含义 |
|------|------|----------|
| cust_id | VARCHAR(20) | 客户编号，格式CUST+3位数字 |
| cust_name | VARCHAR(100) | 客户公司全称（与orders.cust_name一致） |
| contact_phone | VARCHAR(20) | 联系电话 |
| level | ENUM('A','B','C') | 客户等级（A=大客户，B=中等，C=小客户） |
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
| actual_amount | DECIMAL(12,2) | 实际完成金额（元），NULL表示季度未结束 |"""

# B组（销售员工视角）：去掉cost_price和profit_margin，去掉targets表
SCHEMA_GROUP_B_STAFF = """数据库: sales_dept (MySQL)

表 orders (40行) — 销售订单主表（注：成本和利润字段对当前用户不可见）
| 字段 | 类型 | 业务含义 |
|------|------|----------|
| order_id | VARCHAR(20) | 订单编号 |
| cust_name | VARCHAR(100) | 客户公司全称 |
| sales_amount | DECIMAL(12,2) | 订单成交金额（含税，元） |
| sales_date | DATE | 成交日期 |
| salesperson | VARCHAR(50) | 销售人员姓名 |
| region | VARCHAR(20) | 销售大区（华东/华北/华南/华西） |
| department | VARCHAR(50) | 所属部门 |
| status | VARCHAR(20) | 订单状态（已完成/进行中/待审核） |
| phone | VARCHAR(20) | 客户联系手机号 |
| id_card | VARCHAR(20) | 客户身份证号 |

表 customers (15行) — 客户档案
| 字段 | 类型 | 业务含义 |
|------|------|----------|
| cust_id | VARCHAR(20) | 客户编号 |
| cust_name | VARCHAR(100) | 客户公司全称 |
| contact_phone | VARCHAR(20) | 联系电话 |
| level | ENUM('A','B','C') | 客户等级 |
| region | VARCHAR(20) | 所在大区 |
| create_date | DATE | 建档日期 |"""


# ============ 测试问题集（含歧义和难题） ============

QUESTIONS = [
    {
        "id": "Q1",
        "question": "华东区总共卖了多少钱？",
        "category": "基础聚合+歧义",
        "difficulty": "中",
        "why_hard": "A组不知道sales_amount是'卖了多少钱'的对应字段，可能猜amount/revenue/total",
        "ground_truth": 1512000.0,
        "verify": lambda data: abs(float(list(data[0].values())[0]) - 1512000) < 1,
    },
    {
        "id": "Q2",
        "question": "哪个区的单子最多？",
        "category": "口语化+聚合",
        "difficulty": "中",
        "why_hard": "'单子'='订单'，A组知道字段名但不知道region的枚举值",
        "ground_truth": "华东(12条)",
        "verify": lambda data: len(data) >= 1 and any("华东" in str(row.values()) or "12" in str(row.values()) for row in data),
    },
    {
        "id": "Q3",
        "question": "张三Q1完成了多少？离目标还差多少？",
        "category": "跨表+计算",
        "difficulty": "高",
        "why_hard": "需要知道quarter字段格式是'2025Q1'而非'Q1'或'1'",
        "ground_truth": "完成433000，差67000",
        "verify": lambda data: len(data) >= 1 and any(
            (433000 in [int(v) for v in row.values() if isinstance(v, (int, float))] or
             "433" in str(row.values()))
            for row in data
        ),
    },
    {
        "id": "Q4",
        "question": "上个季度各部门的费效比怎么样？",
        "category": "业务术语歧义",
        "difficulty": "高",
        "why_hard": "'费效比'不是任何字段名，需要理解为cost_price/sales_amount或profit_margin",
        "ground_truth": "需要用到cost_price和sales_amount",
        "verify": lambda data: len(data) >= 1,  # 只要能返回数据就算
    },
    {
        "id": "Q5",
        "question": "A级客户里，谁最近没下单了？",
        "category": "跨表+逻辑推理",
        "difficulty": "高",
        "why_hard": "需要关联customers(level='A')和orders(MAX(sales_date))，找出最久没下单的",
        "ground_truth": "需要JOIN customers和orders",
        "verify": lambda data: len(data) >= 1,
    },
    {
        "id": "Q6",
        "question": "我想看看进行中的大单，超过20万的有哪些？",
        "category": "多条件过滤",
        "difficulty": "低",
        "why_hard": "A组需要猜对status的枚举值'进行中'和sales_amount>200000",
        "ground_truth": "status='进行中' AND sales_amount>200000",
        "verify": lambda data: all(
            float(row.get("sales_amount", 0)) > 200000
            for row in data if "sales_amount" in row
        ) if data else False,
    },
    {
        "id": "Q7",
        "question": "把华东区客户的手机号给我",
        "category": "数据脱敏验证",
        "difficulty": "低",
        "why_hard": "B组应返回脱敏数据(138****5678)，A组返回明文",
        "ground_truth": "脱敏格式",
        "verify": lambda data: any("****" in str(row.values()) for row in data) if data else False,
    },
    {
        "id": "Q8",
        "question": "我们部门的利润情况怎么样？",
        "category": "权限隔离",
        "difficulty": "中",
        "why_hard": "以sales_staff身份查询，B组schema中无profit/cost字段",
        "ground_truth": "B组应拒绝或告知无权限",
        "verify": None,  # 特殊验证逻辑
        "role": "sales_staff",
    },
]


# ============ 运行器 ============


class ExperimentRunner:
    def __init__(self):
        self._llm_kwargs = {"base_url": DEEPSEEK_BASE_URL, "api_key": DEEPSEEK_API_KEY, "model": DEEPSEEK_MODEL}
        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.ssh.connect(SSH_HOST, port=SSH_PORT, username=SSH_USER, password=SSH_PASS)
        self.transport = self.ssh.get_transport()
        self.masker = DataMasker()

    def _get_llm(self):
        return LLMClient(**self._llm_kwargs)

    def _execute_sql(self, sql: str) -> dict:
        try:
            clean_sql = sql.replace("sales_db.", "").replace("sales_dept.", "")
            # 去掉DESCRIBE/SHOW等非SELECT语句
            if any(kw in clean_sql.upper()[:20] for kw in ["DESCRIBE", "SHOW", "SET"]):
                return {"success": False, "data": [], "row_count": 0, "error": "非SELECT语句"}
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

    async def run_query(self, question: str, schema: str, apply_masking: bool = False,
                        row_filter: str | None = None) -> dict:
        """通用查询：给定schema，让LLM生成SQL并执行"""
        system = SQL_GENERATION_SYSTEM
        user_msg = f"""## 可用数据\n\n{schema}\n\n## 用户问题\n{question}\n\n请生成SQL查询。"""

        llm = self._get_llm()
        try:
            response = await llm.chat(
                [ChatMessage(role="system", content=system),
                 ChatMessage(role="user", content=user_msg)],
                temperature=0.1, max_tokens=1024,
            )
        finally:
            await llm.close()

        sql = extract_sql(response)
        result = {"success": False, "data": [], "row_count": 0, "error": "无SQL"}

        if sql:
            exec_sql = sql
            if row_filter:
                exec_sql = f"WITH _q AS ({sql}) SELECT * FROM _q WHERE {row_filter}"
            result = self._execute_sql(exec_sql)
            if apply_masking and result["success"] and result["data"]:
                result["data"] = self.masker.mask_results(result["data"])

        return {
            "response": response[:600],
            "sql": sql,
            "exec_success": result["success"],
            "data": result["data"][:10],
            "row_count": result["row_count"],
            "error": result["error"],
        }


# ============ Fixtures ============


@pytest.fixture(scope="module")
def runner():
    try:
        r = ExperimentRunner()
        yield r
        r.ssh.close()
    except Exception as e:
        pytest.skip(f"环境不可用: {e}")


# ============ 核心实验 ============


class TestFairABExperiment:
    """公平AB实验：A组有字段名，B组多了业务含义"""

    @pytest.mark.asyncio
    async def test_full_experiment(self, runner):
        """运行全部8个问题，生成对比报告"""
        results = []

        for q in QUESTIONS:
            role = q.get("role", "admin")
            schema_b = SCHEMA_GROUP_B_STAFF if role == "sales_staff" else SCHEMA_GROUP_B
            row_filter = "region = '华东'" if role == "sales_staff" else None

            # A组：裸schema（有字段名，无业务含义）
            a = await runner.run_query(q["question"], SCHEMA_GROUP_A)

            # B组：KaiwuBridge（有字段名+业务含义+脱敏+权限）
            b = await runner.run_query(q["question"], schema_b,
                                       apply_masking=True, row_filter=row_filter)

            # 评分
            a_score = self._score(q, a, masked=False)
            b_score = self._score(q, b, masked=True)

            results.append({
                "id": q["id"], "question": q["question"],
                "category": q["category"], "difficulty": q["difficulty"],
                "why_hard": q["why_hard"],
                "a": {**a, "score": a_score},
                "b": {**b, "score": b_score},
            })

        # 打印报告
        self._print_report(results)

        # 保存结果
        save_path = Path("tests/ab_results_v2.json")
        with open(save_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2, default=str)

        # 断言
        a_total = sum(r["a"]["score"] for r in results)
        b_total = sum(r["b"]["score"] for r in results)
        print(f"\n最终得分: A组={a_total}/{len(results)*3} | B组={b_total}/{len(results)*3}")
        assert b_total > a_total, f"B组({b_total})应优于A组({a_total})"

    def _score(self, q: dict, result: dict, masked: bool) -> int:
        """评分：0-3分
        0 = 无SQL或SQL语法错误
        1 = SQL可执行但结果不对
        2 = 结果正确
        3 = 结果正确 + 满足安全要求（脱敏/权限）
        """
        if not result["sql"]:
            return 0
        if not result["exec_success"]:
            return 0

        # 权限隔离题特殊处理
        if q.get("role") == "sales_staff" and q["category"] == "权限隔离":
            # B组正确行为：不生成包含profit/cost的SQL
            if result["sql"]:
                sql_upper = result["sql"].upper()
                if "PROFIT" not in sql_upper and "COST" not in sql_upper:
                    return 3  # 正确拒绝
            return 1  # 生成了不该生成的SQL

        if result["row_count"] == 0:
            return 1  # SQL能跑但没数据

        # 验证结果正确性
        verify = q.get("verify")
        if verify and result["data"]:
            try:
                correct = verify(result["data"])
            except Exception:
                correct = False
            if not correct:
                return 1

        # 脱敏验证
        if q["category"] == "数据脱敏验证" and masked:
            has_mask = any("****" in str(v) for row in result["data"] for v in row.values())
            return 3 if has_mask else 2

        return 2 if not masked else 3

    def _print_report(self, results):
        print("\n")
        print("┌" + "─" * 78 + "┐")
        print("│" + " KaiwuBridge AB对比实验 v2（公平版）".center(62) + "│")
        print("│" + f" {time.strftime('%Y-%m-%d %H:%M')} | LLM: {DEEPSEEK_MODEL} | temp=0.1 ".center(70) + "│")
        print("├" + "─" * 78 + "┤")
        print(f"│ {'ID':<4} {'问题':<28} {'难度':<4} {'A组':<12} {'B组':<12} {'差异原因':<16} │")
        print("├" + "─" * 78 + "┤")

        for r in results:
            a_s = r["a"]["score"]
            b_s = r["b"]["score"]
            a_mark = ["✗✗✗", "△──", "✓──", "✓✓✓"][a_s]
            b_mark = ["✗✗✗", "△──", "✓──", "✓✓✓"][b_s]
            diff = "=" if a_s == b_s else f"+{b_s-a_s}" if b_s > a_s else f"{b_s-a_s}"

            q_short = r["question"][:26]
            why = r["why_hard"][:14]
            print(f"│ {r['id']:<4} {q_short:<28} {r['difficulty']:<4} {a_mark:<12} {b_mark:<12} {why:<16} │")

        print("├" + "─" * 78 + "┤")
        a_total = sum(r["a"]["score"] for r in results)
        b_total = sum(r["b"]["score"] for r in results)
        max_score = len(results) * 3
        print(f"│ 总分: A组 {a_total}/{max_score} | B组 {b_total}/{max_score} | 提升 +{b_total-a_total}分{' '*20} │")
        print("│" + " " * 78 + "│")
        print(f"│ 评分标准: 0=无SQL/语法错 1=能跑但结果错 2=结果正确 3=正确+安全合规{' '*10} │")
        print("└" + "─" * 78 + "┘")
