"""配置加载模块：YAML解析 + 环境变量替换

职责：
- 从config目录加载server.yaml、databases.yaml、permissions.yaml
- 递归替换配置值中的 ${ENV_VAR} 为实际环境变量
- 校验并组装为AppConfig对象供全局使用
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from .models import DatabaseConfig, LLMConfig, RoleConfig, ServerConfig, UserConfig

# 环境变量占位符正则：匹配 ${VAR_NAME} 格式
_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


def resolve_env_vars(value: Any) -> Any:
    """递归替换字符串中的 ${ENV_VAR} 为环境变量值

    未设置的环境变量替换为空字符串，不会报错。
    支持嵌套在dict和list中的字符串。
    """
    if isinstance(value, str):
        def _replacer(match):
            env_name = match.group(1)
            return os.environ.get(env_name, "")
        return _ENV_VAR_PATTERN.sub(_replacer, value)
    elif isinstance(value, dict):
        return {k: resolve_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [resolve_env_vars(item) for item in value]
    return value


def load_yaml(path: Path) -> dict:
    """加载YAML文件并自动替换环境变量"""
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return resolve_env_vars(data)


class AppConfig:
    """应用全局配置，聚合所有配置文件内容"""

    def __init__(
        self,
        server: ServerConfig,
        llm: LLMConfig,
        databases: list[DatabaseConfig],
        roles: list[RoleConfig],
        users: list[UserConfig],
    ):
        self.server = server
        self.llm = llm
        self.databases = databases
        self.roles = roles
        self.users = users

    @classmethod
    def load(cls, config_dir: Path) -> "AppConfig":
        """从配置目录加载所有配置文件

        目录结构要求：
        - server.yaml: 服务端和LLM配置
        - databases.yaml: 数据库连接列表
        - permissions.yaml: 角色权限和用户列表
        """
        config_dir = Path(config_dir)

        # 加载服务端配置
        server_data = load_yaml(config_dir / "server.yaml")
        server = ServerConfig(**(server_data.get("server", {})))
        llm = LLMConfig(**(server_data.get("llm", {})))

        # 加载数据库连接配置
        db_data = load_yaml(config_dir / "databases.yaml")
        databases = [DatabaseConfig(**db) for db in db_data.get("databases", [])]

        # 加载权限配置
        perm_data = load_yaml(config_dir / "permissions.yaml")
        roles = [RoleConfig(**r) for r in perm_data.get("roles", [])]
        users = [UserConfig(**u) for u in perm_data.get("users", [])]

        return cls(
            server=server,
            llm=llm,
            databases=databases,
            roles=roles,
            users=users,
        )

    def get_database(self, db_id: str) -> DatabaseConfig | None:
        """按ID查找数据库配置"""
        for db in self.databases:
            if db.id == db_id:
                return db
        return None

    def get_role(self, role_id: str) -> RoleConfig | None:
        """按ID查找角色配置"""
        for role in self.roles:
            if role.id == role_id:
                return role
        return None

    def get_user(self, user_id: str) -> UserConfig | None:
        """按ID查找用户配置"""
        for user in self.users:
            if user.id == user_id:
                return user
        return None
