"""权限过滤引擎

职责：
- 根据用户角色过滤可见的数据库、表、字段
- 生成行级过滤条件（RLAC WHERE子句）
- 确保LLM永远看不到被禁止的schema信息

安全原则：
- 权限绝对不能交给LLM判断，必须在数据层强制执行
- LLM只看到它被允许看到的schema，其他的在到达LLM之前就已过滤
- 行级过滤在SQL执行层注入，不依赖LLM的"自觉"
"""

from __future__ import annotations

from .models import RoleConfig, TableInfo


class PermissionEngine:
    """权限过滤引擎

    三层过滤：
    1. 数据库级：角色只能看到allowed_databases中的库
    2. 表级：角色只能看到allowed_tables中的表
    3. 字段级：denied_columns中的字段从schema中移除
    4. 行级：row_filter中的条件在SQL执行时强制注入
    """

    def __init__(self, roles: list[RoleConfig]):
        self._roles: dict[str, RoleConfig] = {r.id: r for r in roles}

    def get_role(self, role_id: str) -> RoleConfig | None:
        """获取角色配置"""
        return self._roles.get(role_id)

    def can_access_database(self, role_id: str, db_id: str) -> bool:
        """检查角色是否可以访问指定数据库"""
        role = self._roles.get(role_id)
        if not role:
            return False
        # "*" 表示可访问所有数据库（admin角色）
        if "*" in role.allowed_databases:
            return True
        return db_id in role.allowed_databases

    def filter_schema(self, tables: list[TableInfo], role_id: str) -> list[TableInfo]:
        """根据角色权限过滤schema

        返回该角色可见的表列表，其中每张表的columns已移除被禁止的字段。
        """
        role = self._roles.get(role_id)
        if not role:
            return []

        filtered = []
        for table in tables:
            # 第1层：数据库级过滤
            if not self.can_access_database(role_id, table.db_id):
                continue

            # 第2层：表级过滤
            if not self._can_access_table(role, table.db_id, table.table_name):
                continue

            # 第3层：字段级过滤（移除denied_columns中的字段）
            filtered_table = self._filter_columns(role, table)
            filtered.append(filtered_table)

        return filtered

    def _can_access_table(self, role: RoleConfig, db_id: str, table_name: str) -> bool:
        """检查角色是否可以访问指定表"""
        # 通配符："*"键表示对所有数据库生效
        if "*" in role.allowed_tables:
            allowed = role.allowed_tables["*"]
            if "*" in allowed:
                return True
            return table_name in allowed

        # 按数据库ID查找允许的表列表
        if db_id in role.allowed_tables:
            allowed = role.allowed_tables[db_id]
            if "*" in allowed:
                return True
            return table_name in allowed

        return False

    def _filter_columns(self, role: RoleConfig, table: TableInfo) -> TableInfo:
        """移除被禁止的字段

        denied_columns的key格式："db_id.table_name"
        """
        key = f"{table.db_id}.{table.table_name}"
        denied = role.denied_columns.get(key, [])

        if not denied:
            return table

        # 深拷贝表信息，移除被禁止的列
        filtered_table = table.model_copy(deep=True)
        filtered_table.columns = [
            col for col in filtered_table.columns if col.name not in denied
        ]
        return filtered_table

    def get_row_filters(self, role_id: str, user_attrs: dict) -> dict[str, str]:
        """获取行级过滤条件

        将row_filter模板中的 {user.xxx} 占位符替换为实际用户属性值。

        Args:
            role_id: 角色ID
            user_attrs: 用户业务属性，如 {"region": "华东", "department": "销售一部"}

        Returns:
            {"db_id.table_name": "已替换的WHERE条件"}
        """
        role = self._roles.get(role_id)
        if not role:
            return {}

        filters = {}
        for table_key, filter_template in role.row_filter.items():
            # 替换所有 {user.xxx} 占位符
            resolved = filter_template
            for attr_key, attr_val in user_attrs.items():
                resolved = resolved.replace(f"{{user.{attr_key}}}", str(attr_val))
            filters[table_key] = resolved

        return filters
