"""Pydantic数据模型"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ============ 配置模型 ============

class DatabaseConfig(BaseModel):
    id: str
    name: str
    type: str  # mysql, postgresql, sqlite
    host: str = "localhost"
    port: int = 3306
    database: str = ""
    username: str = ""
    password: str = ""


class LLMConfig(BaseModel):
    base_url: str = "http://localhost:11434/v1"
    api_key: str = ""
    model: str = "qwen2.5:7b"
    temperature: float = 0.1
    max_tokens: int = 2048


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080
    jwt_secret: str = "change-me"
    metadata_db: str = "./data/metadata.db"


class RoleConfig(BaseModel):
    id: str
    name: str
    allowed_databases: list[str] = Field(default_factory=list)
    allowed_tables: dict[str, list[str]] = Field(default_factory=dict)
    denied_columns: dict[str, list[str]] = Field(default_factory=dict)
    row_filter: dict[str, str] = Field(default_factory=dict)


class UserConfig(BaseModel):
    id: str
    name: str
    role: str
    attrs: dict[str, str] = Field(default_factory=dict)


# ============ Schema模型 ============

class ColumnInfo(BaseModel):
    name: str
    type: str
    nullable: bool = True
    sample_values: list[Any] = Field(default_factory=list)
    null_ratio: float = 0.0
    distinct_count: int = 0
    comment: str = ""


class TableInfo(BaseModel):
    db_id: str
    table_name: str
    columns: list[ColumnInfo] = Field(default_factory=list)
    row_count: int = 0
    comment: str = ""


# ============ 语义名片模型 ============

class SemanticCard(BaseModel):
    field_id: str  # "db_id.table.column"
    business_name: str = ""
    description: str = ""
    category: str = ""  # 金额/数量/时间/编码/状态/名称/其他
    unit: str | None = None
    notes: str = ""
    confidence: float = 0.0
    verified: bool = False
    updated_at: datetime | None = None


# ============ API模型（OpenAI兼容） ============

class ChatMessage(BaseModel):
    role: str  # system, user, assistant
    content: str


class ChatRequest(BaseModel):
    model: str = ""
    messages: list[ChatMessage]
    temperature: float | None = None
    max_tokens: int | None = None
    stream: bool = False


class ChatChoice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: str = "stop"


class ChatUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatResponse(BaseModel):
    id: str = ""
    object: str = "chat.completion"
    created: int = 0
    model: str = ""
    choices: list[ChatChoice] = Field(default_factory=list)
    usage: ChatUsage = Field(default_factory=ChatUsage)
