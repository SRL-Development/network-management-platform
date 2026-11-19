"""Anonymous SMTP settings, manual delivery and alert acknowledgement APIs."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import Field, field_validator
from sqlalchemy.orm import Session

from app.api_models import InventorySelectionRequest, StrictRequest
from app.database import get_db
from app.models import NotificationAlert
from app.services.email_notifications import (
    DEFAULT_TEMPLATES, MODULES, collect_alerts, load_config, run_email_notifications,
    save_config, send_anonymous,
)


router = APIRouter(prefix="/api/notifications", tags=["notifications"])


class TemplateInput(StrictRequest):
    subject: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=10000)


class EmailConfigInput(StrictRequest):
    enabled: bool = False
    smtp_host: str = Field(default="", max_length=253)
    smtp_port: int = Field(default=25, ge=1, le=65535)
    security: Literal["none", "starttls", "ssl"] = "none"
    from_address: str = Field(default="network-manager@localhost", max_length=320)
    recipients: list[str] = Field(default_factory=list, max_length=100)
    repeat_days: int = Field(default=7, ge=1, le=365)
    cmdb_days: int = Field(default=90, ge=1, le=3650)
    backup_coverage_days: int = Field(default=7, ge=1, le=365)
    backup_min_rate: float = Field(default=95, ge=0, le=100)
    modules: dict[str, bool] = Field(default_factory=dict)
    templates: dict[str, TemplateInput] = Field(default_factory=dict)

    @field_validator("smtp_host", "from_address")
    @classmethod
    def no_control_characters(cls, value: str) -> str:
        value = value.strip()
        if any(char in value for char in "\r\n\0"):
            raise ValueError("SMTP 字段不能包含控制字符")
        return value

    @field_validator("recipients")
    @classmethod
    def valid_recipients(cls, values: list[str]) -> list[str]:
        cleaned = []
        for raw in values:
            value = str(raw or "").strip()
            if not value:
                continue
            if "@" not in value or any(char in value for char in "\r\n,;"):
                raise ValueError(f"收件人邮箱格式不正确：{value}")
            if value not in cleaned:
                cleaned.append(value)
        return cleaned


def _normalise(payload: EmailConfigInput) -> dict:
    data = payload.model_dump()
    data["modules"] = {key: bool(data["modules"].get(key)) for key in MODULES}
    data["templates"] = {
        key: data["templates"].get(key) or DEFAULT_TEMPLATES[key]
        for key in MODULES
    }
    if data["enabled"] and (not data["smtp_host"] or not data["recipients"]):
        raise HTTPException(422, "启用邮件提醒前必须填写 SMTP 服务器和收件人")
    return data


@router.get("/config")
def get_config(db: Session = Depends(get_db)):
    return {**load_config(db), "available_modules": MODULES, "anonymous_only": True}


@router.put("/config")
def update_config(payload: EmailConfigInput, request: Request, db: Session = Depends(get_db)):
    data = _normalise(payload)
    save_config(db, data)
    request.state.audit_action = "notifications.config.update"
    request.state.audit_resource_type = "email_notification"
    request.state.audit_detail = {"enabled": data["enabled"], "modules": [key for key, value in data["modules"].items() if value]}
    return {**data, "available_modules": MODULES, "anonymous_only": True}


@router.post("/test")
def test_email(payload: EmailConfigInput):
    data = _normalise(payload)
    send_anonymous(data, "[网络运维平台] SMTP 匿名投递测试", "SMTP 匿名投递测试成功。\n本邮件未使用用户名或密码认证。")
    return {"ok": True, "message": "测试邮件已提交给 SMTP 服务器"}


@router.post("/run")
def run_now(db: Session = Depends(get_db)):
    try:
        task = run_email_notifications([], db, force=True)
    except Exception as exc:
        raise HTTPException(502, f"邮件发送失败：{exc}") from exc
    return {"ok": True, "task_id": task.id, "summary": task.summary}


@router.get("/alerts")
def list_alerts(
    module: str = Query(default="", max_length=50),
    include_acknowledged: bool = False,
    db: Session = Depends(get_db),
):
    config = load_config(db)
    collect_alerts(db, config)
    query = db.query(NotificationAlert).filter(NotificationAlert.is_active.is_(True))
    if module:
        if module not in MODULES:
            raise HTTPException(422, "未知邮件模块")
        query = query.filter(NotificationAlert.module == module)
    if not include_acknowledged:
        query = query.filter(NotificationAlert.acknowledged_at.is_(None))
    return [{
        "id": row.id, "module": row.module, "module_name": MODULES.get(row.module, row.module),
        "title": row.title, "detail": row.detail or {}, "first_seen_at": row.first_seen_at,
        "last_seen_at": row.last_seen_at, "last_sent_at": row.last_sent_at,
        "acknowledged_at": row.acknowledged_at, "acknowledged_by": row.acknowledged_by or "",
    } for row in query.order_by(NotificationAlert.module, NotificationAlert.last_seen_at.desc()).limit(5000)]


@router.post("/alerts/acknowledge")
def acknowledge(payload: InventorySelectionRequest, request: Request, db: Session = Depends(get_db)):
    rows = db.query(NotificationAlert).filter(NotificationAlert.id.in_(payload.ids), NotificationAlert.is_active.is_(True)).all()
    if len(rows) != len(payload.ids):
        raise HTTPException(409, "部分提醒已变化，请刷新后重试")
    user = getattr(request.state, "user", None)
    now = datetime.utcnow()
    for row in rows:
        row.acknowledged_at = now
        row.acknowledged_by = getattr(user, "username", "") or "管理员"
    db.commit()
    request.state.audit_action = "notifications.alert.acknowledge"
    request.state.audit_resource_type = "notification_alert"
    request.state.audit_detail = {"ids": payload.ids, "count": len(rows)}
    return {"ok": True, "acknowledged": len(rows)}
