# AB对比实验：KaiwuBridge中间层 vs 裸LLM

## 实验目的

验证KaiwuBridge数据中间层的核心价值：接入中间层后LLM能正确查询企业数据，而裸LLM会编造数据或无法生成有效SQL。

## 实验设计

### 两组对比

| | A组（对照组：裸LLM） | B组（实验组：经KaiwuBridge） |
|---|---|---|
| System Prompt | 告知有MySQL数据库和表名（比真实场景更宽松） | KaiwuBridge标准SQL生成prompt |
| Schema信息 | 无字段名、无类型、无业务含义 | 完整字段名+类型+语义名片 |
| 权限控制 | 无 | 字段级+行级过滤 |
| SQL验证 | 无 | sqlparse白名单+注入检测 |
| 数据脱敏 | 无 | 手机号/身份证自动打码 |
| LLM模型 | deepseek-chat | deepseek-chat（相同） |
| Temperature | 0.1 | 0.1（相同） |

### 公平性说明

- A组故意给了表名提示（orders/customers/targets），比真实场景更宽松
- 两组使用完全相同的LLM和参数
- 问题集固定，不针对任何一组优化

### 测试问题集

| ID | 问题 | 类别 | 验证点 |
|----|------|------|--------|
| Q1 | 华东区的总销售额是多少？ | 基础聚合 | SQL正确性、数据准确性 |
| Q2 | 各区域订单数量排名 | 分组排序 | GROUP BY + ORDER BY |
| Q3 | 张三2025Q1销售目标完成率 | 跨表关联 | 需要targets表的精确字段名 |
| Q4 | 我们部门利润率是多少？（销售员工） | 权限隔离 | B组schema无profit字段 |
| Q5 | 华东区TOP3客户联系电话 | 数据脱敏 | B组返回138****5678 |

## 环境搭建

### 前置条件

- Python 3.10+
- VPS: 175.155.64.171:24102 (SSH)
- MySQL 8.0 (VPS上，端口3306，云安全组未开放，需SSH隧道)
- DeepSeek API Key

### 步骤

```bash
# 1. 安装依赖
cd D:\program\qiyeshuju
pip install -e .
pip install pytest pytest-asyncio paramiko pymysql psycopg2-binary sshtunnel

# 2. 确认VPS数据库可达
python -c "
import paramiko
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('175.155.64.171', port=24102, username='linux', password='Ns@uk')
stdin, stdout, _ = ssh.exec_command('sudo mysql -u root -ptest123 -e \"SELECT COUNT(*) FROM sales_dept.orders\"')
print(stdout.read().decode())
ssh.close()
"

# 3. 运行AB对比实验
pytest tests/test_ab_comparison.py -v -s

# 4. 查看结果
cat tests/ab_results.json
```

### 如果需要重建数据库

```bash
# 上传SQL脚本到VPS并执行
python -c "
import paramiko
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('175.155.64.171', port=24102, username='linux', password='Ns@uk')
sftp = ssh.open_sftp()
sftp.put('scripts/setup_mysql.sql', '/tmp/setup_mysql.sql')
sftp.put('scripts/setup_postgres.sql', '/tmp/setup_postgres.sql')
sftp.close()
ssh.exec_command('sudo mysql -u root -ptest123 sales_dept < /tmp/setup_mysql.sql')
ssh.exec_command('sudo -u postgres psql -d finance_dept < /tmp/setup_postgres.sql')
ssh.close()
"
```

## 预期结果

```
┌────────────────────────────────────────────────────────────────────────┐
│              KaiwuBridge AB对比实验报告                                  │
├────────────────────────────────────────────────────────────────────────┤
│ [Q1] 华东区的总销售额是多少？                                           │
│   A组(裸LLM):  ✗ 猜错字段名，SQL执行失败                               │
│   B组(KWB):    ✓ 返回 1,512,000 元                                    │
├────────────────────────────────────────────────────────────────────────┤
│ [Q2] 各区域订单数量排名                                                 │
│   A组(裸LLM):  ✗ 猜测字段名(area/amount)，执行失败                     │
│   B组(KWB):    ✓ 华东12/华北11/华南9/华西8                             │
├────────────────────────────────────────────────────────────────────────┤
│ [Q3] 张三2025Q1销售目标完成率                                           │
│   A组(裸LLM):  ✗ 不知道quarter字段格式是"2025Q1"                       │
│   B组(KWB):    ✓ 86.6%（目标50万，实际43.3万）                         │
├────────────────────────────────────────────────────────────────────────┤
│ [Q4] 利润率是多少？（销售员工身份）                                      │
│   A组(裸LLM):  ✗ 编造"利润率约30%"                                     │
│   B组(KWB):    ✓ 正确拒绝（schema中无利润字段）                         │
├────────────────────────────────────────────────────────────────────────┤
│ [Q5] TOP3客户联系电话                                                   │
│   A组(裸LLM):  ✗ 编造假电话号码                                         │
│   B组(KWB):    ✓ 返回脱敏数据（138****5678）                           │
├────────────────────────────────────────────────────────────────────────┤
│ 结论: A组 0/5 正确 │ B组 5/5 正确                                      │
│ 提升: +5 个问题获得正确答案                                             │
└────────────────────────────────────────────────────────────────────────┘
```

## 结论

1. **SQL生成正确率**：裸LLM不知道字段名，猜测失败率极高；KaiwuBridge提供精确schema后SQL生成正确率接近100%
2. **数据准确性**：裸LLM只能编造数据；KaiwuBridge返回真实查询结果
3. **权限合规**：裸LLM无法做权限控制；KaiwuBridge通过schema过滤从源头阻止越权
4. **数据安全**：裸LLM可能泄露敏感信息；KaiwuBridge自动脱敏

## 文件清单

```
tests/
├── test_ab_comparison.py   # AB对比实验代码
├── ab_results.json         # 实验结果（运行后生成）
scripts/
├── setup_mysql.sql         # MySQL测试数据（40条订单+15客户+8目标）
├── setup_postgres.sql      # PostgreSQL测试数据（25发票+15费用）
docs/
├── ab_experiment.md        # 本文档
config/
├── databases.yaml          # 数据库连接配置
├── permissions.yaml        # 权限配置（5角色5用户）
├── server.yaml             # 服务配置（含DeepSeek API）
```
