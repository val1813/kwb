"""权限过滤引擎：表级/字段级权限 + 行级过滤模板"""

from __future__ import annotations

import copy

from .models import RoleConfig, TableInfo


class PermissionEngine:
    """权限过滤引擎"""

    def __init__(self, roles: list[RoleConfig]):
        self._roles: dict[str, RoleConfig] = {r.id: r for r in roles}

    def get_role(self, role_id: str) -> RoleConfig | None:
        return self._roles.get(role_id)

    def can_access_database(self, role_id: str, db_id: str) -> bool:
        """检查角色是否可以访问指定数据库"""
        role = self._roles.get(role_id)
        if not role:
            return False
        if "*" in role.allowed_databases:
            return True
        return db_id in role.allowed_databases

    def filter_schema(self, tables: list[TableInfo], role_id: str) -> list[TableInfo]:
        """根据角色权限过滤schema，移除不可见的表和字段"""
        role = self._roles.get(role_id)
        if not role:
            return []

        filtered = []
        for table in tables:
            # 检查数据库访问权限
            if not self.can_access_database(role_id, table.db_id):
                continue

            # 检查表访问权限
            if not self._can_access_table(role, table.db_id, table.table_name):
                continue

            # 过滤字段
            filtered_table = self._filter_columns(role, table)
            filtered.append(filtered_table)

        return filtered

    def _can_access_table(self, role: RoleConfig, db_id: str, table_name: str) -> bool:
        """检查角色是否可以访问指定表"""
        # 通配符
        if "*" in role.allowed_tables:
            allowed = role.allowed_tables["*"]
            if "*" in allowed:
                return True
            return table_name in allowed

        # 指定数据库的表列表
        if db_id in role.allowed_tables:
            allowed = role.allowed_tables[db_id]
            if "*" in allowed:
                return True
            return table_name in allowed

        return False

    def _filter_columns(self, role: RoleConfig, table: TableInfo) -> TableInfo:
        """移除被禁止的字段"""
        key = f"{table.db_id}.{table.table_name}"
        denied = role.denied_columns.get(key, [])

        if not denied:
            return table

        # 深拷贝并过滤
        filtered_table = table.model_copy(deep=True)
        filtered_table.columns = [
            col for col in filtered_table.columns if col.name not in denied
        ]
        return filtered_table

    def get_row_filters(self, role_id: str, user_attrs: dict) -> dict[str, str]:
        """获取行级过滤条件，替换用户属性占位符

        返回: {db_id.table_name: "WHERE条件"}
        """
        role = self._roles.get(role_id)
        if not role:
            return {}

        filters = {}
        for table_key, filter_template in role.row_filter.items():
            # 替换 {user.xxx} 占位符
            resolved = filter_template
            for attr_key, attr_val in user_attrs.items():
                resolved = resolved.replace(f"{{user.{attr_key}}}", str(attr_val))
            filters[table_key] = resolved

        return filters
