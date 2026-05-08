"""配置加载：YAML解析 + 环境变量替换"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from .models import DatabaseConfig, LLMConfig, RoleConfig, ServerConfig, UserConfig


def resolve_env_vars(value: Any) -> Any:
    """递归替换字符串中的 ${ENV_VAR} 为环境变量值"""
    if isinstance(value, str):
        pattern = re.compile(r"\$\{([^}]+)\}")
        def replacer(match):
            env_name = match.group(1)
            return os.environ.get(env_name, "")
        return pattern.sub(replacer, value)
    elif isinstance(value, dict):
        return {k: resolve_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [resolve_env_vars(item) for item in value]
    return value


def load_yaml(path: Path) -> dict:
    """加载YAML文件，自动替换环境变量"""
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return resolve_env_vars(data)


class AppConfig:
    """应用全局配置"""

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
        """从配置目录加载所有配置"""
        config_dir = Path(config_dir)

        # server.yaml
        server_data = load_yaml(config_dir / "server.yaml")
        server = ServerConfig(**(server_data.get("server", {})))
        llm = LLMConfig(**(server_data.get("llm", {})))

        # databases.yaml
        db_data = load_yaml(config_dir / "databases.yaml")
        databases = [DatabaseConfig(**db) for db in db_data.get("databases", [])]

        # permissions.yaml
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
        for db in self.databases:
            if db.id == db_id:
                return db
        return None

    def get_role(self, role_id: str) -> RoleConfig | None:
        for role in self.roles:
            if role.id == role_id:
                return role
        return None

    def get_user(self, user_id: str) -> UserConfig | None:
        for user in self.users:
            if user.id == user_id:
                return user
        return None
