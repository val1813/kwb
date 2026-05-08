# KaiwuBridge

企业多源数据库与LLM之间的智能中间层。数据随便传，模型拿到的永远是干净的、权限正确的数据。

---

## 它解决什么问题

企业内部往往有多个数据库（销售用MySQL、财务用PostgreSQL），字段命名混乱、口径不一致、权限分散。直接让LLM访问这些数据库，要么泄露敏感信息，要么生成错误SQL。

KaiwuBridge 在数据库和LLM之间加了一层：

```
企业数据库（任意类型、任意质量）
        ↓
[KaiwuBridge]
  - 接入适配：统一连接各类数据库
  - 语义治理：自动识别字段含义、跨库冲突归一
  - 权限控制：字段级、行级权限，JWT强制执行
  - 路由查询：自然语言翻译成正确的数据库查询
        ↓
任意LLM（DeepSeek/通义/Ollama/自训练模型）
兼容OpenAI API接口
```

不是模型不够强，是企业数据进不去。KaiwuBridge 把脏活全干了。

---

## 快速开始

### 安装

```bash
cd D:/program/qiyeshuju
pip install -e .
```

### 三步启动

```bash
# 1. 初始化配置
kwb init --config ./config

# 2. 扫描数据库（--no-semantic 跳过LLM语义分析，快速验证）
kwb scan --config ./config --no-semantic

# 3. 启动服务
kwb serve --config ./config
```

服务启动后：
- API端点：`http://localhost:8080/v1/chat/completions`
- 管理后台：`http://localhost:8080/admin/`
- 健康检查：`http://localhost:8080/health`

### 环境变量

```bash
export JWT_SECRET=your-32-byte-secret-key-here!
export LLM_BASE_URL=http://localhost:11434/v1   # Ollama默认地址
export LLM_MODEL=qwen2.5:7b
```

---

## CLI命令

| 命令 | 说明 |
|------|------|
| `kwb init` | 初始化配置文件模板 |
| `kwb scan` | 扫描数据库schema，生成语义名片 |
| `kwb serve` | 启动API服务 |
| `kwb status` | 查看数据库连接和元数据状态 |
| `kwb token <user_id>` | 为用户生成JWT Token |

常用参数：

```bash
kwb serve --config ./config --port 9090
kwb scan --config ./config --no-semantic --db-id sales_db
kwb token admin --config ./config --expire 24
```

---

## 配置文件

```
config/
├── server.yaml       # 服务端 + LLM后端配置
├── databases.yaml    # 数据库连接列表
└── permissions.yaml  # 角色权限 + 用户列表
```

### databases.yaml

```yaml
databases:
  - id: sales_db
    name: 销售数据库
    type: mysql
    host: 192.168.1.10
    port: 3306
    database: sales
    username: readonly_user          # 建议使用只读账号
    password: ${SALES_DB_PASSWORD}   # 环境变量，不明文存储

  - id: finance_db
    name: 财务数据库
    type: postgresql
    host: 192.168.1.20
    port: 5432
    database: finance
    username: readonly_user
    password: ${FINANCE_DB_PASSWORD}
```

### permissions.yaml

```yaml
roles:
  - id: sales_staff
    name: 销售员工
    allowed_databases: [sales_db]
    allowed_tables:
      sales_db: [orders, customers]
    denied_columns:
      sales_db.orders: [cost_price, profit_margin]
    row_filter:
      sales_db.orders: "region = '{user.region}'"

users:
  - id: zhangsan
    name: 张三
    role: sales_staff
    attrs:
      region: 华东
      department: 销售一部
```

---

## 使用API

```bash
# 生成token
TOKEN=$(kwb token zhangsan --config ./config 2>/dev/null | tail -1)

# 发起查询
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "华东区上个月销售额是多少？"}]}'
```

响应格式兼容OpenAI，可直接对接任何支持OpenAI API的客户端。

---

## 核心原理

### 语义归一：两层验证

**第一层：语义相似度（bge-m3）**

用本地embedding模型将字段业务描述转为向量，计算跨库字段间的余弦相似度。能识别"销售额"和"营业收入"这类同义异名。

**第二层：数据分布验证（Wasserstein距离）**

对数值型字段比较分布形态。能识别"视角异名"——销售部门的"员工提成"和财务部门的"销售成本"可能指向同一笔钱，纯语义匹配发现不了，但数据分布会暴露关联。

**决策逻辑：**

```
语义相似 + 分布相似  →  高置信，自动建立映射
语义相似 + 分布不同  →  口径可能不同，标记警告，人工确认
语义不同 + 分布相似  →  视角异名，放入待审核队列
语义不同 + 分布不同  →  无关字段，忽略
```

**持续学习：** 用户确认/拒绝映射后，贝叶斯更新阈值。用得越久，需要人工干预越少。

### 权限控制：在数据层强制执行

权限绝对不交给LLM判断。LLM可以被prompt操控，真正安全的权限必须在数据执行层强制生效：

1. JWT token包含角色和业务属性
2. 查询前过滤不可见的库/表/字段——LLM收到的schema里根本没有这些字段
3. SQL执行时强制注入WHERE条件（CTE子查询包装，无法绕过）
4. 数据库连接使用只读账号，物理上无法写入

---

## 安全机制

| 层级 | 机制 | 说明 |
|------|------|------|
| 物理层 | 只读账号 | 数据库连接只用SELECT权限 |
| 验证层 | sqlparse白名单 | 只允许SELECT/WITH，拦截DROP/DELETE/UNION注入/时间盲注 |
| 权限层 | 字段级过滤 | LLM永远看不到被禁止的字段 |
| 执行层 | RLAC行级过滤 | CTE子查询包装，无法绕过 |
| 输出层 | 敏感数据脱敏 | 手机号/身份证/银行卡自动打码 |
| 限流层 | 频率限制 | 50次/分钟，防暴力枚举 |
| 审计层 | append-only日志 | 所有查询记录不可篡改，按日期分割 |

---

## 系统架构

```
┌─────────────────────────────────┐
│         管理层 (Web UI)          │  /admin/ 配置、审核、日志
├─────────────────────────────────┤
│         代理层 (CLI Server)      │  FastAPI，OpenAI兼容接口
│    库路由 → 权限过滤 → SQL执行   │
├─────────────────────────────────┤
│         治理层 (Core Engine)     │  语义匹配 + 分布验证 + 映射表
├─────────────────────────────────┤
│         接入层 (Connectors)      │  SQLAlchemy 多数据库适配
└─────────────────────────────────┘
```

核心模块：

| 模块 | 职责 |
|------|------|
| `connectors.py` | 多数据库连接管理（MySQL/PG/SQLite/MSSQL） |
| `scanner.py` | Schema扫描 + 数据采样 |
| `semantic.py` | LLM驱动的语义名片生成 |
| `matching.py` | 跨库语义匹配（bge-m3 + Wasserstein） |
| `mappings.py` | 跨库映射表管理 |
| `permissions.py` | 权限过滤引擎（表/字段/行三级） |
| `executor.py` | SQL验证 + RLAC + 安全执行 |
| `security.py` | 频率限制 + 脱敏 + 审计日志 |
| `cache.py` | 查询缓存 + Schema缓存 + Embedding持久化 |
| `server.py` | FastAPI主服务（OpenAI兼容接口） |
| `admin.py` | 管理后台API（14个端点） |

---

## 支持的数据库

| 数据库 | 状态 | 驱动 |
|--------|------|------|
| MySQL 5.7+ | ✅ 已支持 | pymysql |
| PostgreSQL 12+ | ✅ 已支持 | psycopg2 |
| SQLite | ✅ 已支持 | 内置 |
| SQL Server 2016+ | ✅ 已支持 | pyodbc |
| Oracle 12c+ | 🔜 待接入 | cx_Oracle |
| 达梦 DM8 | 🔜 待接入 | dmPython |
| 人大金仓 | 🔜 待接入 | psycopg2兼容 |

---

## 技术栈

- Python 3.10+
- FastAPI + Uvicorn（API服务）
- SQLAlchemy 2.0（数据库连接）
- sentence-transformers / bge-m3（本地语义embedding）
- scipy（Wasserstein分布计算）
- PyJWT（认证）
- SQLite（元数据存储，零部署复杂度）
- sqlparse（SQL安全验证）

全部依赖 Apache 2.0 / MIT / BSD 协议，可商用，无侵权风险。

---

## 许可证

本项目采用 **Business Source License 1.1（BSL 1.1）**。

- ✅ 可自由使用、修改、部署用于开发、测试、研究
- ✅ 可在内部生产环境使用（不对外提供服务）
- ❌ 不可将本软件作为托管服务或SaaS产品对外商业提供
- 📅 2028年5月1日自动转换为 Apache 2.0

如需商业授权，请联系项目维护者。

---

*KaiwuBridge——取自「天工开物」，中国古代最重要的工艺技术百科全书。Bridge，连接企业数据与AI的桥梁。*
