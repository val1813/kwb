"""CLI命令行工具：kwb init/serve/scan/status/token"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(help="KaiwuBridge - 企业多源数据库与LLM智能中间层")
console = Console()


def _get_config_dir(config: str) -> Path:
    """解析配置目录路径"""
    p = Path(config)
    if not p.exists():
        console.print(f"[red]配置目录不存在: {p}[/red]")
        raise typer.Exit(1)
    return p


@app.command()
def init(
    config: str = typer.Option("./config", help="配置目录路径"),
):
    """初始化配置文件模板"""
    config_dir = Path(config)
    config_dir.mkdir(parents=True, exist_ok=True)

    # 生成默认配置文件（如果不存在）
    templates = {
        "server.yaml": _SERVER_TEMPLATE,
        "databases.yaml": _DATABASES_TEMPLATE,
        "permissions.yaml": _PERMISSIONS_TEMPLATE,
    }

    for filename, content in templates.items():
        filepath = config_dir / filename
        if filepath.exists():
            console.print(f"[yellow]已存在，跳过: {filepath}[/yellow]")
        else:
            filepath.write_text(content, encoding="utf-8")
            console.print(f"[green]已创建: {filepath}[/green]")

    # 创建data目录
    data_dir = Path("./data")
    data_dir.mkdir(parents=True, exist_ok=True)
    console.print(f"[green]数据目录已就绪: {data_dir}[/green]")
    console.print("\n初始化完成，请编辑配置文件后执行 [bold]kwb scan[/bold] 扫描数据库")


@app.command()
def serve(
    config: str = typer.Option("./config", help="配置目录路径"),
    host: str = typer.Option(None, help="监听地址（覆盖配置文件）"),
    port: int = typer.Option(None, help="监听端口（覆盖配置文件）"),
):
    """启动API服务"""
    import uvicorn

    from .config import AppConfig
    from .server import create_app

    config_dir = _get_config_dir(config)
    app_config = AppConfig.load(config_dir)

    # 命令行参数覆盖配置文件
    listen_host = host or app_config.server.host
    listen_port = port or app_config.server.port

    fastapi_app = create_app(app_config)

    console.print(f"[bold green]开物数据中间层启动中...[/bold green]")
    console.print(f"  地址: http://{listen_host}:{listen_port}")
    console.print(f"  API: http://{listen_host}:{listen_port}/v1/chat/completions")
    console.print(f"  健康检查: http://{listen_host}:{listen_port}/health")
    console.print(f"  LLM后端: {app_config.llm.base_url} ({app_config.llm.model})")

    uvicorn.run(fastapi_app, host=listen_host, port=listen_port, log_level="info")


@app.command()
def scan(
    config: str = typer.Option("./config", help="配置目录路径"),
    db_id: str = typer.Option(None, help="只扫描指定数据库（默认扫描全部）"),
    semantic: bool = typer.Option(True, help="是否生成语义名片（需要LLM）"),
):
    """扫描数据库schema并生成语义名片"""
    from .config import AppConfig
    from .connectors import DatabaseManager
    from .llm_client import LLMClient
    from .metadata import MetadataStore
    from .scanner import SchemaScanner
    from .semantic import SemanticGenerator

    config_dir = _get_config_dir(config)
    app_config = AppConfig.load(config_dir)

    db_manager = DatabaseManager(app_config.databases)
    metadata = MetadataStore(app_config.server.metadata_db)
    scanner = SchemaScanner(db_manager)

    # 确定要扫描的数据库列表
    target_dbs = [db_id] if db_id else db_manager.list_databases()

    for target in target_dbs:
        console.print(f"\n[bold]扫描数据库: {target}[/bold]")

        # 测试连接
        if not db_manager.test_connection(target):
            console.print(f"  [red]连接失败，跳过[/red]")
            continue

        # 扫描schema
        tables = scanner.scan_database(target)
        console.print(f"  发现 {len(tables)} 张表")

        for table in tables:
            metadata.save_table_info(table)
            col_names = [c.name for c in table.columns]
            console.print(f"    {table.table_name}: {len(table.columns)}列, {table.row_count}行")

        # 生成语义名片
        if semantic:
            console.print(f"\n  [bold]生成语义名片（LLM: {app_config.llm.model}）...[/bold]")
            llm = LLMClient(
                base_url=app_config.llm.base_url,
                api_key=app_config.llm.api_key,
                model=app_config.llm.model,
            )
            generator = SemanticGenerator(llm, metadata)

            async def _generate():
                try:
                    cards = await generator.generate_cards_for_database(tables)
                    return cards
                finally:
                    await llm.close()

            cards = asyncio.run(_generate())
            console.print(f"  [green]已生成 {len(cards)} 张语义名片[/green]")

    db_manager.close_all()
    metadata.close()
    console.print("\n[bold green]扫描完成[/bold green]")


@app.command()
def status(
    config: str = typer.Option("./config", help="配置目录路径"),
):
    """查看系统状态"""
    from .config import AppConfig
    from .connectors import DatabaseManager
    from .metadata import MetadataStore

    config_dir = _get_config_dir(config)
    app_config = AppConfig.load(config_dir)

    db_manager = DatabaseManager(app_config.databases)
    metadata = MetadataStore(app_config.server.metadata_db)

    # 数据库连接状态
    console.print("\n[bold]数据库连接状态[/bold]")
    table = Table()
    table.add_column("ID")
    table.add_column("名称")
    table.add_column("类型")
    table.add_column("状态")

    for db_config in app_config.databases:
        connected = db_manager.test_connection(db_config.id)
        status_text = "[green]已连接[/green]" if connected else "[red]连接失败[/red]"
        table.add_row(db_config.id, db_config.name, db_config.type, status_text)
    console.print(table)

    # 元数据统计
    all_tables = metadata.get_all_tables()
    all_cards = metadata.get_all_cards()
    console.print(f"\n[bold]元数据统计[/bold]")
    console.print(f"  已扫描表: {len(all_tables)}")
    console.print(f"  语义名片: {len(all_cards)}")
    verified_count = sum(1 for c in all_cards if c.verified)
    console.print(f"  已审核名片: {verified_count}")

    # LLM配置
    console.print(f"\n[bold]LLM配置[/bold]")
    console.print(f"  端点: {app_config.llm.base_url}")
    console.print(f"  模型: {app_config.llm.model}")

    db_manager.close_all()
    metadata.close()


@app.command()
def token(
    user_id: str = typer.Argument(help="用户ID"),
    config: str = typer.Option("./config", help="配置目录路径"),
    expire: int = typer.Option(8, help="Token有效期（小时）"),
):
    """为指定用户生成JWT Token"""
    from .auth import AuthManager
    from .config import AppConfig

    config_dir = _get_config_dir(config)
    app_config = AppConfig.load(config_dir)

    # 查找用户
    user = app_config.get_user(user_id)
    if not user:
        console.print(f"[red]用户不存在: {user_id}[/red]")
        console.print(f"可用用户: {[u.id for u in app_config.users]}")
        raise typer.Exit(1)

    auth = AuthManager(app_config.server.jwt_secret, expire_hours=expire)
    jwt_token = auth.create_token(user.id, user.role, user.attrs)

    console.print(f"\n[bold]用户: {user.name} ({user.id})[/bold]")
    console.print(f"角色: {user.role}")
    console.print(f"属性: {user.attrs}")
    console.print(f"有效期: {expire}小时")
    console.print(f"\n[bold green]Token:[/bold green]")
    console.print(jwt_token)


# ============ 配置文件模板 ============

_SERVER_TEMPLATE = """server:
  host: 0.0.0.0
  port: 8080
  jwt_secret: please-change-this-secret
  metadata_db: ./data/metadata.db

llm:
  # 支持任意OpenAI兼容接口，默认使用本地Ollama
  base_url: http://localhost:11434/v1
  api_key: ""
  model: qwen2.5:7b
  temperature: 0.1
  max_tokens: 2048
"""

_DATABASES_TEMPLATE = """databases:
  # MySQL示例
  # - id: sales_db
  #   name: 销售数据库
  #   type: mysql
  #   host: 192.168.1.10
  #   port: 3306
  #   database: sales
  #   username: readonly_user
  #   password: ${SALES_DB_PASSWORD}

  # PostgreSQL示例
  # - id: finance_db
  #   name: 财务数据库
  #   type: postgresql
  #   host: 192.168.1.20
  #   port: 5432
  #   database: finance
  #   username: readonly_user
  #   password: ${FINANCE_DB_PASSWORD}

  # 本地SQLite（开箱即用，用于测试）
  - id: demo_db
    name: 演示数据库
    type: sqlite
    database: ./data/demo.db
"""

_PERMISSIONS_TEMPLATE = """roles:
  - id: admin
    name: 管理员
    allowed_databases: ["*"]
    allowed_tables:
      "*": ["*"]
    denied_columns: {}
    row_filter: {}

  - id: sales_staff
    name: 销售员工
    allowed_databases: [sales_db]
    allowed_tables:
      sales_db: [orders, customers]
    denied_columns:
      sales_db.orders: [cost_price, profit_margin]
    row_filter:
      sales_db.orders: "region = '{user.region}'"

users:
  - id: admin
    name: 管理员
    role: admin
    attrs: {}

  - id: zhangsan
    name: 张三
    role: sales_staff
    attrs:
      region: 华东
      department: 销售一部
"""


if __name__ == "__main__":
    app()
