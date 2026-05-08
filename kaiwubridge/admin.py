"""管理后台API路由模块

职责：
- 提供管理后台的Web界面（Jinja2渲染）
- 数据库连接状态查看与重新扫描
- 语义名片的查看、编辑、审核
- 跨库映射的查看、确认、拒绝
- 权限配置查看
- 审计日志和安全事件查看
- 系统统计概览
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

# 模板目录
_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

router = APIRouter(prefix="/admin", tags=["管理后台"])


# ============ 请求模型 ============


class CardUpdateRequest(BaseModel):
    """语义名片编辑请求"""
    business_name: str = ""
    description: str = ""
    category: str = ""
    unit: str | None = None
    notes: str = ""


class MappingConfirmRequest(BaseModel):
    """映射确认请求"""
    canonical_name: str = ""


# ============ 页面路由 ============


@router.get("/", response_class=HTMLResponse)
async def admin_page(request: Request):
    """渲染管理后台HTML页面"""
    return templates.TemplateResponse("admin.html", {"request": request})


# ============ 数据库管理 ============


@router.get("/api/databases")
async def list_databases(request: Request):
    """获取数据库列表及连接状态"""
    app = request.app
    db_manager = app.state.db_manager
    config = app.state.config

    databases = []
    for db_config in config.databases:
        connected = db_manager.test_connection(db_config.id)
        databases.append({
            "id": db_config.id,
            "name": db_config.name,
            "type": db_config.type,
            "host": db_config.host,
            "port": db_config.port,
            "database": db_config.database,
            "connected": connected,
        })

    return {"databases": databases}


@router.post("/api/databases/{db_id}/scan")
async def scan_database(db_id: str, request: Request):
    """触发数据库重新扫描"""
    app = request.app
    db_manager = app.state.db_manager
    metadata = app.state.metadata
    config = app.state.config

    # 检查数据库是否存在
    db_config = config.get_database(db_id)
    if not db_config:
        raise HTTPException(status_code=404, detail=f"数据库 {db_id} 不存在")

    # 测试连接
    if not db_manager.test_connection(db_id):
        raise HTTPException(status_code=503, detail=f"数据库 {db_id} 连接失败")

    # 执行扫描
    from .scanner import SchemaScanner
    scanner = SchemaScanner(db_manager)
    try:
        tables = scanner.scan_database(db_id)
        # 保存扫描结果
        for table in tables:
            metadata.save_table_info(table)
        return {
            "success": True,
            "message": f"扫描完成，共发现 {len(tables)} 张表",
            "table_count": len(tables),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"扫描失败: {str(e)}")


# ============ 语义名片管理 ============


@router.get("/api/cards")
async def list_cards(
    request: Request,
    db_id: str = "",
    table_name: str = "",
    page: int = 1,
    page_size: int = 50,
):
    """获取语义名片列表，支持分页和过滤"""
    app = request.app
    metadata = app.state.metadata

    all_cards = metadata.get_all_cards()

    # 过滤
    if db_id:
        all_cards = [c for c in all_cards if c.field_id.startswith(f"{db_id}.")]
    if table_name:
        all_cards = [c for c in all_cards if f".{table_name}." in c.field_id]

    # 分页
    total = len(all_cards)
    start = (page - 1) * page_size
    end = start + page_size
    page_cards = all_cards[start:end]

    return {
        "cards": [
            {
                "field_id": c.field_id,
                "business_name": c.business_name,
                "description": c.description,
                "category": c.category,
                "unit": c.unit,
                "notes": c.notes,
                "confidence": c.confidence,
                "verified": c.verified,
                "updated_at": str(c.updated_at) if c.updated_at else None,
            }
            for c in page_cards
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.put("/api/cards/{field_id:path}")
async def update_card(field_id: str, body: CardUpdateRequest, request: Request):
    """编辑语义名片"""
    app = request.app
    metadata = app.state.metadata

    card = metadata.get_card(field_id)
    if not card:
        raise HTTPException(status_code=404, detail=f"名片 {field_id} 不存在")

    # 更新字段
    card.business_name = body.business_name or card.business_name
    card.description = body.description or card.description
    card.category = body.category or card.category
    card.unit = body.unit if body.unit is not None else card.unit
    card.notes = body.notes or card.notes

    metadata.save_card(card)
    return {"success": True, "message": "名片已更新"}


@router.post("/api/cards/{field_id:path}/verify")
async def verify_card(field_id: str, request: Request):
    """标记语义名片为已审核"""
    app = request.app
    metadata = app.state.metadata

    card = metadata.get_card(field_id)
    if not card:
        raise HTTPException(status_code=404, detail=f"名片 {field_id} 不存在")

    card.verified = True
    card.confidence = 1.0
    metadata.save_card(card)
    return {"success": True, "message": "名片已标记为已审核"}


# ============ 跨库映射管理 ============


@router.get("/api/mappings")
async def list_mappings(request: Request, status: str = ""):
    """获取映射列表，支持按状态过滤"""
    app = request.app
    config = app.state.config

    # 初始化MappingStore
    from .mappings import MappingStore
    mapping_db = str(Path(config.server.metadata_db).parent / "mappings.db")
    store = MappingStore(mapping_db)

    try:
        if status:
            mappings = store.get_all_mappings(status=status)
        else:
            mappings = store.get_all_mappings()
        return {"mappings": mappings, "total": len(mappings)}
    finally:
        store.close()


@router.post("/api/mappings/{mapping_id}/confirm")
async def confirm_mapping(mapping_id: int, request: Request, body: MappingConfirmRequest = None):
    """确认映射"""
    app = request.app
    config = app.state.config

    from .mappings import MappingStore
    mapping_db = str(Path(config.server.metadata_db).parent / "mappings.db")
    store = MappingStore(mapping_db)

    try:
        canonical_name = body.canonical_name if body else ""
        success = store.confirm_mapping(mapping_id, notes=canonical_name)
        if not success:
            raise HTTPException(status_code=400, detail="确认失败：映射不存在或状态非pending")
        return {"success": True, "message": "映射已确认"}
    finally:
        store.close()


@router.post("/api/mappings/{mapping_id}/reject")
async def reject_mapping(mapping_id: int, request: Request):
    """拒绝映射"""
    app = request.app
    config = app.state.config

    from .mappings import MappingStore
    mapping_db = str(Path(config.server.metadata_db).parent / "mappings.db")
    store = MappingStore(mapping_db)

    try:
        success = store.reject_mapping(mapping_id)
        if not success:
            raise HTTPException(status_code=400, detail="拒绝失败：映射不存在或状态非pending")
        return {"success": True, "message": "映射已拒绝"}
    finally:
        store.close()


# ============ 权限管理 ============


@router.get("/api/roles")
async def list_roles(request: Request):
    """获取角色列表"""
    app = request.app
    config = app.state.config

    roles = []
    for role in config.roles:
        roles.append({
            "id": role.id,
            "name": role.name,
            "allowed_databases": role.allowed_databases,
            "allowed_tables": role.allowed_tables,
            "denied_columns": role.denied_columns,
            "row_filter": role.row_filter,
        })
    return {"roles": roles}


@router.get("/api/users")
async def list_users(request: Request):
    """获取用户列表"""
    app = request.app
    config = app.state.config

    users = []
    for user in config.users:
        users.append({
            "id": user.id,
            "name": user.name,
            "role": user.role,
            "attrs": user.attrs,
        })
    return {"users": users}


# ============ 日志查看 ============


@router.get("/api/logs")
async def list_logs(request: Request, limit: int = 100):
    """获取最近的查询日志"""
    app = request.app
    audit_logger = app.state.audit_logger

    log_dir = audit_logger._log_dir
    logs = []

    # 读取最近的日志文件
    log_files = sorted(log_dir.glob("audit_*.jsonl"), reverse=True)
    for log_file in log_files:
        if len(logs) >= limit:
            break
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
            # 倒序读取（最新的在前）
            for line in reversed(lines):
                if len(logs) >= limit:
                    break
                line = line.strip()
                if line:
                    try:
                        logs.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except (IOError, OSError):
            continue

    return {"logs": logs, "total": len(logs)}


@router.get("/api/security-events")
async def list_security_events(request: Request, limit: int = 100):
    """获取安全事件日志"""
    app = request.app
    audit_logger = app.state.audit_logger

    log_dir = audit_logger._log_dir
    events_file = log_dir / "security_events.jsonl"
    events = []

    if events_file.exists():
        try:
            with open(events_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
            for line in reversed(lines):
                if len(events) >= limit:
                    break
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except (IOError, OSError):
            pass

    return {"events": events, "total": len(events)}


# ============ 冲突审核 ============


@router.get("/api/conflicts")
async def list_conflicts(request: Request, unconfirmed_only: bool = False):
    """获取冲突列表"""
    metadata = request.app.state.metadata
    return metadata.get_conflicts(unconfirmed_only=unconfirmed_only)


class ConflictConfirmRequest(BaseModel):
    """冲突确认请求"""
    warning_text: str  # 管理员填写的warning说明


@router.post("/api/conflicts/{conflict_id}/confirm")
async def confirm_conflict(
    conflict_id: int,
    body: ConflictConfirmRequest,
    request: Request,
):
    """
    管理员确认冲突并填写warning说明。
    warning_text示例：
    "注意：sales_db.aaa是销售成本，finance_db.aaa是销售提成，两者完全不同，请勿混用"
    """
    metadata = request.app.state.metadata
    metadata.confirm_conflict(conflict_id, body.warning_text)
    return {"status": "confirmed"}


# ============ 系统统计 ============


@router.get("/api/stats")
async def get_stats(request: Request):
    """获取系统统计概览"""
    app = request.app
    metadata = app.state.metadata
    config = app.state.config

    # 统计数据
    all_cards = metadata.get_all_cards()
    all_tables = metadata.get_all_tables()

    verified_cards = [c for c in all_cards if c.verified]

    # 映射统计
    from .mappings import MappingStore
    mapping_db = str(Path(config.server.metadata_db).parent / "mappings.db")
    store = MappingStore(mapping_db)
    try:
        all_mappings = store.get_all_mappings()
        pending_mappings = [m for m in all_mappings if m["status"] == "pending"]
        confirmed_mappings = [m for m in all_mappings if m["status"] == "confirmed"]
    finally:
        store.close()

    # 今日查询数：统计今天的审计日志条目
    audit_logger = app.state.audit_logger
    today_queries = 0
    today_str = datetime.now().strftime("%Y-%m-%d")
    today_log = audit_logger._log_dir / f"audit_{today_str}.jsonl"
    if today_log.exists():
        try:
            with open(today_log, "r", encoding="utf-8") as f:
                today_queries = sum(1 for line in f if line.strip())
        except (IOError, OSError):
            pass

    return {
        "databases": len(config.databases),
        "tables": len(all_tables),
        "cards_total": len(all_cards),
        "cards_verified": len(verified_cards),
        "cards_unverified": len(all_cards) - len(verified_cards),
        "mappings_total": len(all_mappings),
        "mappings_pending": len(pending_mappings),
        "mappings_confirmed": len(confirmed_mappings),
        "roles": len(config.roles),
        "users": len(config.users),
        "today_queries": today_queries,
    }
