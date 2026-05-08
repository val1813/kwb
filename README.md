<div align="center">

# KaiwuBridge

**企业多源数据库与LLM之间的智能中间层**

数据随便传，模型拿到的永远是干净的、权限正确的数据。

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: BSL 1.1](https://img.shields.io/badge/license-BSL%201.1-green.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-147%20passed-brightgreen.svg)](#实验结果)

</div>

---

## 要解决什么问题

企业内部往往有多个数据库（销售用MySQL、财务用PostgreSQL），字段命名混乱、口径不一致、权限分散。直接让LLM访问这些数据库：

| 问题 | 后果 |
|------|------|
| LLM不知道字段名和格式 | 生成错误SQL，查不到数据 |
| 没有权限控制 | 销售员工能看到成本和利润 |
| 没有数据脱敏 | 手机号、身份证明文泄露 |
| 没有SQL审查 | 注入攻击可删库 |
| 跨库字段含义不同 | "金额"在销售库是回款，在财务库是开票 |

KaiwuBridge 在数据库和LLM之间加了一层，把这些脏活全干了：

```
企业数据库（MySQL / PostgreSQL / SQLite / SQL Server）
        ↓
┌─────────────────────────────────┐
│         KaiwuBridge             │
│  接入适配 → 语义治理 → 权限控制  │
│  SQL审查 → 行级过滤 → 数据脱敏  │
└─────────────────────────────────┘
        ↓
任意LLM（DeepSeek / 通义 / Ollama / GPT）
兼容 OpenAI API 接口
```

---

## 技术栈

| 层级 | 技术 | 用途 |
|------|------|------|
| API服务 | FastAPI + Uvicorn | OpenAI兼容接口，异步高性能 |
| 数据库连接 | SQLAlchemy 2.0 | 多数据库统一适配 |
| 语义理解 | sentence-transformers / bge-m3 | 本地embedding，跨库字段匹配 |
| 分布验证 | scipy (Wasserstein距离) | 数值字段口径一致性检测 |
| 认证 | PyJWT | 用户身份+角色+业务属性 |
| SQL安全 | sqlparse | 语法树分析，白名单验证 |
| 元数据 | SQLite | 零部署复杂度，语义名片持久化 |
| CLI | Typer + Rich | 命令行管理工具 |

全部依赖 Apache 2.0 / MIT / BSD 协议，可商用。

---

## 功能特性

### 安全层（7道防线）

| 防线 | 机制 | 效果 |
|------|------|------|
| 物理层 | 只读数据库账号 | 即使所有软件层被绕过，也无法写入 |
| SQL白名单 | sqlparse语法树分析 | 只允许SELECT/WITH，拦截注入/盲注/文件读写 |
| 注释绕过防护 | 去注释后再匹配 | `UNION/**/SELECT` 无法绕过 |
| 字段级权限 | schema过滤 | LLM看不到的字段=查不到 |
| 行级过滤(RLAC) | CTE子查询包装 | 华东员工只能看华东数据，无法绕过 |
| 数据脱敏 | 正则自动打码 | 手机号→138\*\*\*\*5678，身份证→310101\*\*\*\*1234 |
| 子查询递归检测 | 递归提取表名 | `WHERE id IN (SELECT ... FROM 越权表)` 被拦截 |

### 语义治理

- **语义名片**：LLM自动分析每个字段的业务含义，生成结构化描述
- **跨库匹配**：bge-m3 embedding + Wasserstein分布验证，识别同义异名
- **持续学习**：用户确认/拒绝映射后贝叶斯更新阈值

### 权限模型

```yaml
# 三级权限：数据库 → 表 → 字段
roles:
  - id: sales_staff
    allowed_databases: [sales_db]
    allowed_tables: { sales_db: [orders, customers] }
    denied_columns: { sales_db.orders: [cost_price, profit_margin] }
    row_filter: { sales_db.orders: "region = '{user.region}'" }
```

### API兼容

兼容 OpenAI `/v1/chat/completions` 接口，可直接对接任何支持 OpenAI API 的客户端。

---

## 实验结果

### 实验1：安全架构对比

两组使用完全相同的schema（无业务含义注释），唯一区别是B组经过KaiwuBridge安全层。

| 场景 | A组（无安全层） | B组（KaiwuBridge） |
|------|----------------|-------------------|
| 正常查询 | 正常执行 | 正常执行 |
| SQL注入 | 执行失败/危险 | **已拦截** |
| UNION注入 | 危险执行(48行) | **已拦截** |
| 越权字段查询 | 越权成功(4行) | **权限阻止** |
| 行级过滤绕过 | 泄露全部区域 | **RLAC生效(仅华东)** |
| 敏感数据 | 明文泄露 | **已脱敏(138\*\*\*\*5678)** |
| 子查询越权 | 越权执行 | **已拦截** |
| 时间盲注 | 危险执行 | **已拦截** |

**结论：两组SQL生成能力相同，差异100%来自安全架构。A组6次安全事件，B组6次正确防护。**

### 实验2：语义名片价值

独立实验，两组都不经过安全层，纯比SQL生成质量。

| 问题 | C组（裸schema） | D组（带语义名片） | 差异原因 |
|------|----------------|------------------|----------|
| 华东区总共卖了多少钱？ | ✓ | ✓ | 简单题，两组都行 |
| 张三Q1完成了多少？ | ✗ | ✓ | 需知道quarter格式是"2025Q1" |
| 各部门费效比 | ✗ | ✗ | 业务术语太模糊，两组都难 |
| 哪些大客户最近没下单？ | ✗ | ✓ | 需知道level='A'表示大客户 |
| 进行中的大单 | ✓ | ✓ | LLM猜对了status枚举值 |

**结论：C组 3/5，D组 5/5。语义名片在枚举值含义和字段格式上提供关键信息。**

### 测试覆盖

```
tests/
├── test_security.py       65个  SQL注入/JWT/越权/脱敏/频率限制
├── test_functional.py     28个  语义匹配/跨库映射/权限过滤
├── test_integration.py    26个  SQLite端到端管道
├── test_e2e_vps.py        22个  真实MySQL+PostgreSQL+DeepSeek API
├── test_ab_v3.py           2个  AB对比实验（安全架构+语义名片）
└── test_ab_comparison.py   6个  端到端AB对比
                          ─────
                          149个测试，0失败
```

---

## 安装部署

### 环境要求

- Python 3.10+
- 目标数据库（MySQL/PostgreSQL/SQLite/SQL Server）
- 任意OpenAI兼容LLM（DeepSeek/Ollama/通义千问）

### 安装

```bash
git clone <repo-url>
cd kaiwubridge
pip install -e .
```

### 三步启动

```bash
# 1. 初始化配置
kwb init --config ./config

# 2. 编辑配置文件
#    config/databases.yaml  — 数据库连接
#    config/permissions.yaml — 角色权限
#    config/server.yaml — LLM后端

# 3. 扫描数据库 + 启动服务
kwb scan --config ./config
kwb serve --config ./config
```

### 环境变量

```bash
export JWT_SECRET=your-32-byte-secret-key
export LLM_BASE_URL=https://api.deepseek.com/v1  # 或本地Ollama
export LLM_API_KEY=sk-xxx
export LLM_MODEL=deepseek-chat
```

### 运行测试

```bash
# 本地测试（无需外部依赖）
pytest tests/test_security.py tests/test_functional.py tests/test_integration.py

# 完整测试（需要VPS和DeepSeek API）
pytest tests/ --timeout=300
```

### 复现AB实验

详见 [docs/ab_experiment.md](docs/ab_experiment.md)

---

## 项目结构

```
kaiwubridge/
├── server.py          FastAPI主服务（OpenAI兼容接口）
├── connectors.py      多数据库连接管理
├── scanner.py         Schema扫描 + 数据采样
├── semantic.py        LLM驱动的语义名片生成
├── matching.py        跨库语义匹配（bge-m3 + Wasserstein）
├── mappings.py        跨库映射表管理
├── permissions.py     权限过滤引擎（库/表/字段/行 四级）
├── executor.py        SQL验证 + RLAC + 安全执行
├── security.py        注入防护 + 脱敏 + 审计日志 + 频率限制
├── auth.py            JWT认证
├── cache.py           查询缓存 + Embedding持久化
├── admin.py           管理后台API
├── llm_client.py      异步LLM客户端（OpenAI兼容）
├── config.py          配置加载（YAML + 环境变量）
├── models.py          Pydantic数据模型
├── prompts.py         Prompt模板集合
└── cli.py             CLI命令行工具
config/
├── databases.yaml     数据库连接配置
├── permissions.yaml   角色权限配置
└── server.yaml        服务端配置
scripts/
├── setup_mysql.sql    MySQL测试数据（40订单+15客户+8目标）
└── setup_postgres.sql PostgreSQL测试数据（25发票+15费用）
docs/
└── ab_experiment.md   AB实验复现文档
```

---

## 支持的数据库

| 数据库 | 状态 | 驱动 |
|--------|------|------|
| MySQL 5.7+ | ✅ 已支持 | pymysql |
| PostgreSQL 12+ | ✅ 已支持 | psycopg2 |
| SQLite | ✅ 已支持 | 内置 |
| SQL Server 2016+ | ✅ 已支持 | pyodbc |
| Oracle 12c+ | 🔜 计划中 | cx_Oracle |
| 达梦 DM8 | 🔜 计划中 | dmPython |
| 人大金仓 | 🔜 计划中 | psycopg2兼容 |

---

## 贡献指南

欢迎提交 Issue 和 Pull Request。

### 开发环境

```bash
git clone <repo-url>
cd kaiwubridge
pip install -e ".[dev]"
pytest tests/test_security.py tests/test_functional.py tests/test_integration.py
```

### 贡献方向

- 新数据库驱动适配（Oracle/达梦/人大金仓）
- 更多语义匹配算法
- Web管理后台前端
- 性能基准测试
- 多语言文档

### 提交规范

```
feat: 新功能
fix: 修复bug
docs: 文档更新
test: 测试用例
refactor: 重构
```

---

## License

本项目采用 **Business Source License 1.1（BSL 1.1）**。

- ✅ 自由使用、修改、部署用于开发、测试、内部生产
- ❌ 不可作为托管服务或SaaS产品对外商业提供
- 📅 2028年5月1日自动转换为 Apache 2.0

如需商业授权，请联系项目维护者。

---

<div align="center">

*KaiwuBridge —— 取自「天工开物」，中国古代工艺技术百科全书。Bridge，连接企业数据与AI的桥梁。*

</div>
