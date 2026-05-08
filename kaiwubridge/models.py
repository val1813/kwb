"""Pydantic数据模型定义

所有模块共享的数据结构，分为四组：
1. 配置模型 - 对应YAML配置文件
2. Schema模型 - 数据库结构描述
3. 语义名片模型 - 字段业务语义
4. API模型 - OpenAI兼容的请求/响应格式
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ============ 配置模型（对应YAML配置文件） ============


class DatabaseConfig(BaseModel):
    """数据库连接配置"""
    id: str                          # 数据库唯一标识，如 "sales_db"
    name: str                        # 中文显示名称，如 "销售数据库"
    type: str                        # 数据库类型：mysql / postgresql / sqlite
    host: str = "localhost"
    port: int = 3306                 # 默认MySQL端口，PostgreSQL需改为5432
    database: str = ""               # 数据库名或SQLite文件路径
    username: str = ""
    password: str = ""               # 支持 ${ENV_VAR} 语法，运行时从环境变量读取


class LLMConfig(BaseModel):
    """LLM后端配置，兼容任意OpenAI接口"""
    base_url: str = "http://localhost:11434/v1"  # 默认本地Ollama
    api_key: str = ""                            # Ollama无需API Key
    model: str = "qwen2.5:7b"                    # 默认模型
    temperature: float = 0.1                     # 低温度保证SQL生成稳定性
    max_tokens: int = 2048                       # 单次响应最大token数


class ServerConfig(BaseModel):
    """服务端配置"""
    host: str = "0.0.0.0"
    port: int = 8080
    jwt_secret: str = "change-me"                # 生产环境必须通过环境变量设置
    metadata_db: str = "./data/metadata.db"      # SQLite元数据库路径


class RoleConfig(BaseModel):
    """角色权限配置

    权限模型：
    - allowed_databases: 可访问的数据库列表，"*"表示全部
    - allowed_tables: {db_id: [table_list]}，"*"表示全部
    - denied_columns: {db_id.table_name: [column_list]}，黑名单
    - row_filter: {db_id.table_name: "WHERE条件模板"}，支持{user.xxx}占位符
    """
    id: str
    name: str
    allowed_databases: list[str] = Field(default_factory=list)
    allowed_tables: dict[str, list[str]] = Field(default_factory=dict)
    denied_columns: dict[str, list[str]] = Field(default_factory=dict)
    row_filter: dict[str, str] = Field(default_factory=dict)


class UserConfig(BaseModel):
    """用户配置"""
    id: str                                      # 用户唯一标识
    name: str                                    # 显示名称
    role: str                                    # 关联的角色ID
    attrs: dict[str, str] = Field(default_factory=dict)  # 业务属性，用于行级过滤


# ============ Schema模型（数据库结构描述） ============


class ColumnInfo(BaseModel):
    """列信息（schema扫描结果）"""
    name: str
    type: str                                    # SQLAlchemy返回的类型字符串
    nullable: bool = True
    sample_values: list[Any] = Field(default_factory=list)  # 随机采样的非空值（最多10条）
    null_ratio: float = 0.0                      # 空值比例 0.0~1.0
    distinct_count: int = 0                      # 去重后的值数量
    comment: str = ""                            # 数据库中的列注释


class TableInfo(BaseModel):
    """表信息（schema扫描结果）"""
    db_id: str                                   # 所属数据库ID
    table_name: str
    columns: list[ColumnInfo] = Field(default_factory=list)
    row_count: int = 0
    comment: str = ""                            # 数据库中的表注释


# ============ 语义名片模型 ============


class SemanticCard(BaseModel):
    """字段语义名片（LLM生成 + 人工审核）

    每个字段一张名片，描述其业务含义。
    field_id格式："db_id.table_name.column_name"
    """
    field_id: str
    business_name: str = ""                      # 中文业务名称，如"订单金额"
    description: str = ""                        # 一句话描述
    category: str = ""                           # 分类：金额/数量/时间/编码/状态/名称/其他
    unit: str | None = None                      # 单位：元、个、%等
    notes: str = ""                              # 业务规则备注
    confidence: float = 0.0                      # LLM生成的置信度 0.0~1.0
    verified: bool = False                       # 是否已人工审核
    updated_at: datetime | None = None


# ============ API模型（OpenAI兼容格式） ============


class ChatMessage(BaseModel):
    """聊天消息"""
    role: str                                    # system / user / assistant
    content: str


class ChatRequest(BaseModel):
    """聊天请求（兼容OpenAI /v1/chat/completions）"""
    model: str = ""
    messages: list[ChatMessage]
    temperature: float | None = None
    max_tokens: int | None = None
    stream: bool = False


class ChatChoice(BaseModel):
    """响应中的单个选择"""
    index: int = 0
    message: ChatMessage
    finish_reason: str = "stop"


class ChatUsage(BaseModel):
    """Token使用统计"""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatResponse(BaseModel):
    """聊天响应（兼容OpenAI格式）"""
    id: str = ""
    object: str = "chat.completion"
    created: int = 0
    model: str = ""
    choices: list[ChatChoice] = Field(default_factory=list)
    usage: ChatUsage = Field(default_factory=ChatUsage)
