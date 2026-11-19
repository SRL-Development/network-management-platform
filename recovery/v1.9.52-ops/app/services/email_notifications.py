"""Anonymous SMTP delivery and acknowledgeable modular notification reports."""
from __future__ import annotations

import json
import smtplib
import ssl
from datetime import date, datetime, timedelta
from email.message import EmailMessage

from sqlalchemy.orm import Session, selectinload

from app.models import (
    CMDBAsset, ConfigBackup, Device, NotificationAlert, SystemSetting, TaskLog,
)


CONFIG_KEY = "email_notification_config"
MODULES = {
    "cmdb_expiry": "CMDB 到期提醒",
    "backup_coverage": "设备配置备份覆盖率",
    "backup_failures": "配置备份失败",
    "device_offline": "设备离线",
}

DEFAULT_TEMPLATES = {
    "cmdb_expiry": {
        "subject": "[网络运维] CMDB {days} 天到期提醒（{count} 项）",
        "body": "以下 CMDB 维保、授权或订阅即将在 {days} 天内到期，确认前每 {repeat_days} 天重复提醒：\n\n{items}\n\n生成时间：{generated_at}",
    },
    "backup_coverage": {
        "subject": "[网络运维] 设备配置备份覆盖率 {backup_rate}%",
        "body": "最近 {coverage_days} 天配置备份覆盖率为 {backup_rate}%（{covered}/{total}），低于阈值 {min_rate}%。\n\n{items}\n\n生成时间：{generated_at}",
    },
    "backup_failures": {
        "subject": "[网络运维] 配置备份失败提醒（{count} 项）",
        "body": "最近 {coverage_days} 天发现以下配置备份失败：\n\n{items}\n\n生成时间：{generated_at}",
    },
    "device_offline": {
        "subject": "[网络运维] 设备离线提醒（{count} 台）",
        "body": "当前发现以下启用设备处于离线状态：\n\n{items}\n\n生成时间：{generated_at}",
    },
}


def default_config() -> dict:
    return {
        "enabled": False,
        "smtp_host": "", "smtp_port": 25, "security": "none",
        "from_address": "network-manager@localhost", "recipients": [],
        "repeat_days": 7, "cmdb_days": 90,
        "backup_coverage_days": 7, "backup_min_rate": 95,
        "modules": {key: False for key in MODULES},
        "templates": {key: dict(value) for key, value in DEFAULT_TEMPLATES.items()},
    }


def load_config(db: Session) -> dict:
    result = default_config()
    row = db.get(SystemSetting, CONFIG_KEY)
    if not row or not row.value:
        return result
    try:
        saved = json.loads(row.value)
    except (TypeError, json.JSONDecodeError):
        return result
    if not isinstance(saved, dict):
        return result
    for key in ("enabled", "smtp_host", "smtp_port", "security", "from_address", "recipients", "repeat_days", "cmdb_days", "backup_coverage_days", "backup_min_rate"):
        if key in saved:
            result[key] = saved[key]
    if isinstance(saved.get("modules"), dict):
        result["modules"].update({key: bool(saved["modules"].get(key)) for key in MODULES})
    if isinstance(saved.get("templates"), dict):
        for key in MODULES:
            if isinstance(saved["templates"].get(key), dict):
                result["templates"][key] = {
                    "subject": str(saved["templates"][key].get("subject") or DEFAULT_TEMPLATES[key]["subject"]),
                    "body": str(saved["templates"][key].get("body") or DEFAULT_TEMPLATES[key]["body"]),
                }
    return result


def save_config(db: Session, config: dict) -> None:
    row = db.get(SystemSetting, CONFIG_KEY)
    if row is None:
        row = SystemSetting(key=CONFIG_KEY)
        db.add(row)
    row.value = json.dumps(config, ensure_ascii=False)
    db.commit()


class _SafeFormat(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def _format(template: str, context: dict) -> str:
    try:
        return template.format_map(_SafeFormat(context))
    except (ValueError, KeyError):
        return template


def send_anonymous(config: dict, subject: str, body: str) -> None:
    host = str(config.get("smtp_host") or "").strip()
    recipients = [str(value).strip() for value in config.get("recipients", []) if str(value).strip()]
    if not host or not recipients:
        raise ValueError("请填写 SMTP 服务器和至少一个收件人")
    message = EmailMessage()
    message["Subject"] = subject.replace("\r", " ").replace("\n", " ")[:300]
    message["From"] = str(config.get("from_address") or "network-manager@localhost").strip()
    message["To"] = ", ".join(recipients)
    message.set_content(body)
    port = int(config.get("smtp_port") or 25)
    security = config.get("security") or "none"
    context = ssl.create_default_context()
    client_class = smtplib.SMTP_SSL if security == "ssl" else smtplib.SMTP
    kwargs = {"host": host, "port": port, "timeout": 15}
    if security == "ssl":
        kwargs["context"] = context
    with client_class(**kwargs) as smtp:
        if security == "starttls":
            smtp.starttls(context=context)
        # Deliberately no smtp.login(): this feature is anonymous SMTP only.
        smtp.send_message(message)


def _upsert_alert(db: Session, module: str, event_key: str, title: str, detail: dict, now: datetime) -> NotificationAlert:
    row = db.query(NotificationAlert).filter_by(module=module, event_key=event_key).first()
    if row is None:
        row = NotificationAlert(module=module, event_key=event_key, title=title, detail=detail, first_seen_at=now)
        db.add(row)
    elif not row.is_active:
        row.first_seen_at = now
        row.acknowledged_at = None
        row.acknowledged_by = ""
        row.last_sent_at = None
    row.title, row.detail, row.last_seen_at, row.is_active = title, detail, now, True
    return row


def collect_alerts(db: Session, config: dict) -> dict[str, list[NotificationAlert]]:
    now, today = datetime.utcnow(), date.today()
    active_keys: dict[str, set[str]] = {key: set() for key in MODULES}
    days = int(config.get("cmdb_days") or 90)
    assets = db.query(CMDBAsset).options(selectinload(CMDBAsset.entitlements)).all()
    for asset in assets:
        candidates = []
        if asset.initial_expiry_date:
            candidates.append(("initial", asset.initial_expiry_date, "首次购买到期"))
        candidates.extend((f"entitlement:{item.id}", item.end_date, item.name or item.entitlement_type)
                          for item in asset.entitlements if item.end_date)
        for source, expiry, label in candidates:
            remaining = (expiry - today).days
            if remaining > days:
                continue
            key = f"{asset.id}:{source}:{expiry.isoformat()}"
            active_keys["cmdb_expiry"].add(key)
            _upsert_alert(db, "cmdb_expiry", key, f"{asset.name} · {label}", {
                "asset_id": asset.id, "asset": asset.name, "company": asset.company or "",
                "item": label, "expiry_date": expiry.isoformat(), "days_remaining": remaining,
            }, now)

    active_devices = db.query(Device).filter(Device.is_active.is_(True)).all()
    coverage_days = int(config.get("backup_coverage_days") or 7)
    since = now - timedelta(days=coverage_days)
    covered_ids = {value for (value,) in db.query(ConfigBackup.device_id).filter(ConfigBackup.backup_time >= since).distinct()}
    total, covered = len(active_devices), sum(1 for row in active_devices if row.id in covered_ids)
    rate = round(covered * 100 / total, 1) if total else 100.0
    if rate < float(config.get("backup_min_rate") or 95):
        key = "backup_coverage"
        active_keys["backup_coverage"].add(key)
        missing = [f"{row.name} ({row.ip_address})" for row in active_devices if row.id not in covered_ids]
        _upsert_alert(db, "backup_coverage", key, f"配置备份覆盖率 {rate}%", {
            "backup_rate": rate, "covered": covered, "total": total,
            "coverage_days": coverage_days, "min_rate": config.get("backup_min_rate", 95),
            "missing": missing[:500],
        }, now)

    failed_tasks = db.query(TaskLog).filter(
        TaskLog.task_type == "backup", TaskLog.status.in_(("failed", "partial")), TaskLog.started_at >= since,
    ).order_by(TaskLog.started_at.desc()).limit(100).all()
    for task in failed_tasks:
        key = f"task:{task.id}"
        active_keys["backup_failures"].add(key)
        _upsert_alert(db, "backup_failures", key, task.summary or f"备份任务 #{task.id} 失败", {
            "task_id": task.id, "time": task.started_at.isoformat(), "failed_count": task.failed_count,
        }, now)

    for device in active_devices:
        if device.status != "offline":
            continue
        key = f"device:{device.id}:{device.last_seen.isoformat() if device.last_seen else 'never'}"
        active_keys["device_offline"].add(key)
        _upsert_alert(db, "device_offline", key, f"{device.name} 离线", {
            "device_id": device.id, "device": device.name, "ip": device.ip_address,
            "company": device.company or "", "last_seen": device.last_seen.isoformat() if device.last_seen else "",
        }, now)

    for module in MODULES:
        for row in db.query(NotificationAlert).filter_by(module=module, is_active=True).all():
            if row.event_key not in active_keys[module]:
                row.is_active = False
    db.commit()
    return {module: db.query(NotificationAlert).filter_by(module=module, is_active=True).all() for module in MODULES}


def _context(module: str, alerts: list[NotificationAlert], config: dict) -> dict:
    lines = []
    for index, row in enumerate(alerts, 1):
        detail = row.detail or {}
        if module == "cmdb_expiry":
            lines.append(f"{index}. {row.title}｜公司：{detail.get('company') or '-'}｜到期：{detail.get('expiry_date')}｜剩余：{detail.get('days_remaining')} 天")
        elif module == "backup_coverage":
            lines.extend(f"{index + offset}. 未覆盖：{item}" for offset, item in enumerate(detail.get("missing", [])))
        elif module == "device_offline":
            lines.append(f"{index}. {detail.get('device')} ({detail.get('ip')})｜公司：{detail.get('company') or '-'}")
        else:
            lines.append(f"{index}. {row.title}")
    coverage = next((row.detail or {} for row in alerts if row.module == "backup_coverage"), {})
    return {
        "module": MODULES[module], "count": len(alerts), "items": "\n".join(lines) or "无明细",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "days": config.get("cmdb_days", 90), "repeat_days": config.get("repeat_days", 7),
        "coverage_days": config.get("backup_coverage_days", 7), "min_rate": config.get("backup_min_rate", 95),
        "backup_rate": coverage.get("backup_rate", "-"), "covered": coverage.get("covered", 0), "total": coverage.get("total", 0),
    }


def run_email_notifications(_device_ids, db: Session, *, force: bool = False) -> TaskLog:
    config = load_config(db)
    task = TaskLog(task_type="email_notify", status="running", started_at=datetime.utcnow())
    db.add(task); db.commit()
    try:
        if not config.get("enabled") and not force:
            task.status, task.summary = "success", "邮件提醒未启用，已跳过"
            task.completed_at = datetime.utcnow(); db.commit(); return task
        collected = collect_alerts(db, config)
        cutoff = datetime.utcnow() - timedelta(days=int(config.get("repeat_days") or 7))
        sent_modules, sent_alerts = [], 0
        for module, enabled in config.get("modules", {}).items():
            if module not in MODULES or not enabled:
                continue
            rows = [row for row in collected[module] if row.acknowledged_at is None and (force or row.last_sent_at is None or row.last_sent_at <= cutoff)]
            if not rows:
                continue
            context = _context(module, rows, config)
            template = config["templates"][module]
            send_anonymous(config, _format(template["subject"], context), _format(template["body"], context))
            sent_at = datetime.utcnow()
            for row in rows:
                row.last_sent_at = sent_at
            sent_modules.append(module); sent_alerts += len(rows)
        task.status, task.completed_at = "success", datetime.utcnow()
        task.total_devices, task.success_count = sent_alerts, sent_alerts
        task.summary = f"邮件提醒完成：发送 {len(sent_modules)} 个模块、{sent_alerts} 条待确认事项" if sent_modules else "没有达到发送条件的待确认事项"
        db.commit(); return task
    except Exception as exc:
        db.rollback()
        task = db.get(TaskLog, task.id)
        task.status, task.completed_at, task.failed_count = "failed", datetime.utcnow(), 1
        task.summary, task.error_details = f"邮件提醒失败：{exc}", [{"error": str(exc)[:1000]}]
        db.commit()
        raise
