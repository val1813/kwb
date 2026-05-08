"""数据库连接管理：SQLAlchemy引擎创建、连接测试、SQL执行"""

from __future__ import annotations

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from .models import DatabaseConfig


class DatabaseManager:
    """管理多个数据库连接"""

    def __init__(self, databases: list[DatabaseConfig]):
        self._engines: dict[str, Engine] = {}
        self._configs: dict[str, DatabaseConfig] = {}
        for db in databases:
            self._configs[db.id] = db

    def _build_url(self, config: DatabaseConfig) -> str:
        """构建SQLAlchemy连接URL"""
        db_type = config.type.lower()
        if db_type == "sqlite":
            return f"sqlite:///{config.database}"
        elif db_type == "mysql":
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
        """获取或创建数据库引擎"""
        if db_id not in self._engines:
            config = self._configs.get(db_id)
            if not config:
                raise ValueError(f"未找到数据库配置: {db_id}")
            url = self._build_url(config)
            self._engines[db_id] = create_engine(url, pool_pre_ping=True)
        return self._engines[db_id]

    def get_inspector(self, db_id: str):
        """获取SQLAlchemy Inspector"""
        engine = self.get_engine(db_id)
        return inspect(engine)

    def test_connection(self, db_id: str) -> bool:
        """测试数据库连接"""
        try:
            engine = self.get_engine(db_id)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    def execute_sql(self, db_id: str, sql: str) -> list[dict]:
        """执行SQL并返回结果列表"""
        engine = self.get_engine(db_id)
        with engine.connect() as conn:
            result = conn.execute(text(sql))
            columns = list(result.keys())
            rows = []
            for row in result.fetchall():
                rows.append(dict(zip(columns, row)))
            return rows

    def get_config(self, db_id: str) -> DatabaseConfig | None:
        return self._configs.get(db_id)

    def list_databases(self) -> list[str]:
        return list(self._configs.keys())

    def close_all(self):
        for engine in self._engines.values():
            engine.dispose()
        self._engines.clear()
