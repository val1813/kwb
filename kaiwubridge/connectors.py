"""数据库连接管理模块

职责：
- 根据配置创建SQLAlchemy引擎（连接池）
- 提供统一的SQL执行接口
- 管理多数据库连接的生命周期

安全要求：
- 所有数据库连接应使用只读账号（SELECT权限）
- 物理层面阻止写操作，即使SQL验证被绕过也无法造成破坏
"""

from __future__ import annotations

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from .models import DatabaseConfig


class DatabaseManager:
    """多数据库连接管理器

    每个数据库配置对应一个SQLAlchemy Engine（含连接池）。
    Engine延迟创建，首次访问时初始化。
    """

    def __init__(self, databases: list[DatabaseConfig]):
        self._engines: dict[str, Engine] = {}
        self._configs: dict[str, DatabaseConfig] = {db.id: db for db in databases}

    def _build_url(self, config: DatabaseConfig) -> str:
        """根据数据库类型构建SQLAlchemy连接URL"""
        db_type = config.type.lower()

        if db_type == "sqlite":
            # SQLite直接使用文件路径
            return f"sqlite:///{config.database}"
        elif db_type == "mysql":
            # charset=utf8mb4 确保支持中文和emoji
            return (
                f"mysql+pymysql://{config.username}:{config.password}"
                f"@{config.host}:{config.port}/{config.database}?charset=utf8mb4"
            )
        elif db_type == "postgresql":
            return (
                f"postgresql+psycopg2://{config.username}:{config.password}"
                f"@{config.host}:{config.port}/{config.database}"
            )
        elif db_type == "sqlserver":
            return (
                f"mssql+pyodbc://{config.username}:{config.password}"
                f"@{config.host}:{config.port}/{config.database}"
                f"?driver=ODBC+Driver+17+for+SQL+Server"
            )
        else:
            raise ValueError(f"不支持的数据库类型: {db_type}")

    def get_engine(self, db_id: str) -> Engine:
        """获取或创建数据库引擎（延迟初始化）

        pool_pre_ping=True: 每次从池中取连接前先ping一下，
        避免使用已断开的连接导致查询失败。
        """
        if db_id not in self._engines:
            config = self._configs.get(db_id)
            if not config:
                raise ValueError(f"未找到数据库配置: {db_id}")
            url = self._build_url(config)
            self._engines[db_id] = create_engine(url, pool_pre_ping=True)
        return self._engines[db_id]

    def get_inspector(self, db_id: str):
        """获取SQLAlchemy Inspector，用于schema扫描"""
        engine = self.get_engine(db_id)
        return inspect(engine)

    def test_connection(self, db_id: str) -> bool:
        """测试数据库连接是否可用"""
        try:
            engine = self.get_engine(db_id)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    def execute_sql(self, db_id: str, sql: str) -> list[dict]:
        """执行SQL并返回结果列表

        注意：此方法直接执行传入的SQL字符串。
        调用方必须确保SQL已经过安全验证（SQLValidator）。
        生产环境中数据库账号应只有SELECT权限。
        """
        engine = self.get_engine(db_id)
        with engine.connect() as conn:
            result = conn.execute(text(sql))
            columns = list(result.keys())
            return [dict(zip(columns, row)) for row in result.fetchall()]

    def get_config(self, db_id: str) -> DatabaseConfig | None:
        """获取数据库配置"""
        return self._configs.get(db_id)

    def list_databases(self) -> list[str]:
        """列出所有已配置的数据库ID"""
        return list(self._configs.keys())

    def close_all(self):
        """关闭所有数据库连接，释放连接池资源"""
        for engine in self._engines.values():
            engine.dispose()
        self._engines.clear()
