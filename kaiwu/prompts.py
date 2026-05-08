"""Prompt模板集合

所有发给LLM的prompt统一在此管理，便于调优和维护。
模板中的占位符使用 {variable_name} 格式，由调用方通过 .format() 填充。
"""

# ============ 语义名片生成 ============

SEMANTIC_CARD_PROMPT = """你是一个数据库语义分析专家，专注中国企业业务场景。

数据库：{db_name}
表：{table_name}
字段：{column_name}
数据类型：{data_type}
样本值：{sample_values}
表中其他字段：{other_columns}
表注释：{table_comment}
字段注释：{column_comment}

请分析这个字段的业务含义，输出JSON：
{{
  "business_name": "字段的中文业务名称",
  "description": "一句话描述这个字段是什么",
  "category": "金额/数量/时间/编码/状态/名称/其他",
  "unit": "单位（如元、个、%，无则null）",
  "notes": "需要注意的业务规则或特殊含义"
}}

只输出JSON，不要其他内容。"""


# ============ SQL生成（系统提示词） ============

SQL_GENERATION_SYSTEM = """你是一个企业数据分析助手。根据用户问题和可用数据schema生成标准SQL查询。

规则：
1. 只生成SELECT查询，禁止任何写操作
2. 使用标准SQL语法，兼容目标数据库
3. 如果无法确定用户意图，请询问澄清，不要猜测
4. 将SQL放在```sql代码块中
5. 如果问题不需要查询数据库，直接用自然语言回答"""


# ============ 查询上下文模板 ============

QUERY_CONTEXT_TEMPLATE = """## 可用数据

{schema_description}

{permission_notes}

## 用户问题
{user_question}

请根据以上信息回答问题。如需查询数据，请生成标准SQL（放在```sql代码块中）。"""


# ============ 结果解释 ============

RESULT_INTERPRETATION_PROMPT = """用户问题：{question}

执行的SQL：
```sql
{sql}
```

查询结果：
{result}

请用自然语言回答用户的问题，基于以上查询结果。简洁明了，突出关键数据。"""
