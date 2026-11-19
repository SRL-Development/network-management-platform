"""CMDB asset lifecycle, warranty/licence reminders and bulk operations."""
from __future__ import annotations

import ipaddress
from datetime import date, datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import Field, field_validator, model_validator
from sqlalchemy.orm import Session, selectinload

from app.api_models import InventorySelectionRequest, StrictRequest
from app.database import get_db
from app.models import (
    CMDBAsset, CMDBEntitlement, CMDBIPAddress, CMDBLifecycleEvent,
    CMDBSerialNumber, Device, DeviceInfo, ServerAsset,
)


router = APIRouter(prefix="/api/cmdb", tags=["cmdb"])

LIFECYCLE_STAGES = ("规划", "采购", "到货", "部署", "在用", "维护", "停用", "退役", "处置")
ASSET_TYPES = ("网络设备", "服务器", "存储", "安全设备", "无线设备", "其他")
ENTITLEMENT_TYPES = ("硬件维保", "软件授权", "厂商支持", "订阅服务", "其他")


class IPInput(StrictRequest):
    address: str = Field(min_length=1, max_length=45)
    purpose: str = Field(default="管理", max_length=100)
    is_primary: bool = False

    @field_validator("address")
    @classmethod
    def valid_ip(cls, value: str) -> str:
        return str(ipaddress.ip_address(value.strip()))


class SerialInput(StrictRequest):
    serial_number: str = Field(min_length=1, max_length=200)
    kind: str = Field(default="主机序列号", max_length=50)
    label: str = Field(default="", max_length=100)

    @field_validator("serial_number")
    @classmethod
    def clean_serial(cls, value: str) -> str:
        return value.strip()


class EntitlementInput(StrictRequest):
    entitlement_type: Literal["硬件维保", "软件授权", "厂商支持", "订阅服务", "其他"] = "硬件维保"
    name: str = Field(min_length=1, max_length=200)
    vendor: str = Field(default="", max_length=200)
    contract_number: str = Field(default="", max_length=100)
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    reminder_days: int = Field(default=90, ge=1, le=1095)
    notes: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def dates_in_order(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("维保/授权结束日期不能早于开始日期")
        return self


class CMDBAssetRequest(StrictRequest):
    name: str = Field(min_length=1, max_length=200)
    asset_type: Literal["网络设备", "服务器", "存储", "安全设备", "无线设备", "其他"] = "网络设备"
    brand: str = Field(default="", max_length=100)
    model: str = Field(default="", max_length=200)
    asset_tag: str = Field(default="", max_length=100)
    linked_device_id: Optional[int] = Field(default=None, gt=0)
    linked_server_id: Optional[int] = Field(default=None, gt=0)
    lifecycle_status: Literal["规划", "采购", "到货", "部署", "在用", "维护", "停用", "退役", "处置"] = "在用"
    criticality: Literal["低", "一般", "重要", "关键"] = "一般"
    purchase_date: Optional[date] = None
    initial_expiry_date: Optional[date] = None
    vendor: str = Field(default="", max_length=200)
    purchase_order: str = Field(default="", max_length=100)
    purchase_cost: Optional[float] = Field(default=None, ge=0)
    currency: str = Field(default="CNY", min_length=3, max_length=10)
    department: str = Field(default="", max_length=100)
    owner: str = Field(default="", max_length=100)
    location: str = Field(default="", max_length=300)
    notes: str = Field(default="", max_length=4000)
    ip_addresses: list[IPInput] = Field(default_factory=list, max_length=50)
    serial_numbers: list[SerialInput] = Field(default_factory=list, max_length=50)
    entitlements: list[EntitlementInput] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_links_and_dates(self):
        if self.linked_device_id and self.linked_server_id:
            raise ValueError("一个 CMDB 资产只能关联网络设备或服务器/存储之一")
        if self.purchase_date and self.initial_expiry_date and self.initial_expiry_date < self.purchase_date:
            raise ValueError("首次到期日不能早于购买日期")
        addresses = [item.address for item in self.ip_addresses]
        if len(addresses) != len(set(addresses)):
            raise ValueError("同一资产不能录入重复 IP")
        serials = [item.serial_number.casefold() for item in self.serial_numbers]
        if len(serials) != len(set(serials)):
            raise ValueError("同一资产不能录入重复序列号")
        if sum(1 for item in self.ip_addresses if item.is_primary) > 1:
            raise ValueError("只能设置一个主 IP")
        return self


class LifecycleEventRequest(StrictRequest):
    stage: Literal["规划", "采购", "到货", "部署", "在用", "维护", "停用", "退役", "处置"]
    event_date: date = Field(default_factory=date.today)
    operator: str = Field(default="", max_length=100)
    notes: str = Field(default="", max_length=2000)
    update_current_status: bool = True


class BatchStatusRequest(InventorySelectionRequest):
    lifecycle_status: Literal["规划", "采购", "到货", "部署", "在用", "维护", "停用", "退役", "处置"]
    operator: str = Field(default="", max_length=100)
    notes: str = Field(default="批量状态调整", max_length=1000)


def _audit(request: Request, action: str, resource_id: str = "", detail: Optional[dict] = None):
    request.state.audit_action = action
    request.state.audit_resource_type = "cmdb_asset"
    request.state.audit_resource_id = resource_id
    request.state.audit_detail = detail or {}


def _expiry(asset: CMDBAsset) -> dict:
    candidates: list[tuple[date, str]] = []
    if asset.initial_expiry_date:
        candidates.append((asset.initial_expiry_date, "首次购买到期"))
    for item in asset.entitlements:
        if item.end_date:
            candidates.append((item.end_date, item.name or item.entitlement_type))
    if not candidates:
        return {"next_expiry_date": None, "next_expiry_name": "", "days_remaining": None, "expiry_status": "none"}
    expiry_date, name = min(candidates, key=lambda item: item[0])
    days = (expiry_date - date.today()).days
    status = "expired" if days < 0 else "urgent" if days <= 30 else "warning" if days <= 90 else "normal"
    return {"next_expiry_date": expiry_date, "next_expiry_name": name, "days_remaining": days, "expiry_status": status}


def _entitlement_response(item: CMDBEntitlement) -> dict:
    days = (item.end_date - date.today()).days if item.end_date else None
    status = "none" if days is None else "expired" if days < 0 else "warning" if days <= item.reminder_days else "normal"
    return {
        "id": item.id, "entitlement_type": item.entitlement_type, "name": item.name,
        "vendor": item.vendor or "", "contract_number": item.contract_number or "",
        "start_date": item.start_date, "end_date": item.end_date,
        "reminder_days": item.reminder_days, "days_remaining": days,
        "reminder_status": status, "notes": item.notes or "",
    }


def _asset_response(asset: CMDBAsset, include_events: bool = False) -> dict:
    result = {
        "id": asset.id, "name": asset.name, "asset_type": asset.asset_type,
        "brand": asset.brand or "", "model": asset.model or "", "asset_tag": asset.asset_tag or "",
        "linked_device_id": asset.linked_device_id, "linked_server_id": asset.linked_server_id,
        "lifecycle_status": asset.lifecycle_status, "criticality": asset.criticality,
        "purchase_date": asset.purchase_date, "initial_expiry_date": asset.initial_expiry_date,
        "vendor": asset.vendor or "", "purchase_order": asset.purchase_order or "",
        "purchase_cost": asset.purchase_cost, "currency": asset.currency or "CNY",
        "department": asset.department or "", "owner": asset.owner or "",
        "location": asset.location or "", "notes": asset.notes or "",
        "ip_addresses": [{"id": row.id, "address": row.address, "purpose": row.purpose or "", "is_primary": row.is_primary} for row in asset.ip_addresses],
        "serial_numbers": [{"id": row.id, "serial_number": row.serial_number, "kind": row.kind or "", "label": row.label or ""} for row in asset.serial_numbers],
        "entitlements": [_entitlement_response(row) for row in asset.entitlements],
        "created_at": asset.created_at, "updated_at": asset.updated_at,
        **_expiry(asset),
    }
    if include_events:
        result["lifecycle_events"] = [{
            "id": row.id, "stage": row.stage, "event_date": row.event_date,
            "operator": row.operator or "", "notes": row.notes or "", "created_at": row.created_at,
        } for row in sorted(asset.lifecycle_events, key=lambda row: (row.event_date, row.id), reverse=True)]
    return result


def _query(db: Session):
    return db.query(CMDBAsset).options(
        selectinload(CMDBAsset.ip_addresses), selectinload(CMDBAsset.serial_numbers),
        selectinload(CMDBAsset.entitlements), selectinload(CMDBAsset.lifecycle_events),
    )


def _get_asset(db: Session, asset_id: int) -> CMDBAsset:
    asset = _query(db).filter(CMDBAsset.id == asset_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="CMDB 资产不存在")
    return asset


def _validate_links(db: Session, payload: CMDBAssetRequest):
    if payload.linked_device_id and not db.get(Device, payload.linked_device_id):
        raise HTTPException(status_code=422, detail="关联的网络设备不存在")
    if payload.linked_server_id and not db.get(ServerAsset, payload.linked_server_id):
        raise HTTPException(status_code=422, detail="关联的服务器/存储不存在")


def _apply(asset: CMDBAsset, payload: CMDBAssetRequest):
    scalar_fields = (
        "name", "asset_type", "brand", "model", "asset_tag", "linked_device_id", "linked_server_id",
        "lifecycle_status", "criticality", "purchase_date", "initial_expiry_date", "vendor",
        "purchase_order", "purchase_cost", "currency", "department", "owner", "location", "notes",
    )
    for field in scalar_fields:
        setattr(asset, field, getattr(payload, field))
    asset.ip_addresses = [CMDBIPAddress(**item.model_dump()) for item in payload.ip_addresses]
    asset.serial_numbers = [CMDBSerialNumber(**item.model_dump()) for item in payload.serial_numbers]
    asset.entitlements = [CMDBEntitlement(**item.model_dump()) for item in payload.entitlements]


@router.get("/catalog")
def catalog(db: Session = Depends(get_db)):
    devices = db.query(Device).order_by(Device.name).all()
    servers = db.query(ServerAsset).order_by(ServerAsset.name).all()
    device_rows = []
    for row in devices:
        latest = db.query(DeviceInfo).filter(DeviceInfo.device_id == row.id).order_by(DeviceInfo.collected_at.desc()).first()
        device_rows.append({
            "id": row.id, "name": row.name, "type": "device", "model": row.model or (latest.model if latest else ""),
            "serial": latest.serial_number if latest else "",
            "ips": list(dict.fromkeys([value for value in [row.ip_address] + [ip.ip_address for ip in getattr(row, "extra_ips", [])] if value])),
        })
    return {
        "asset_types": ASSET_TYPES, "lifecycle_stages": LIFECYCLE_STAGES,
        "entitlement_types": ENTITLEMENT_TYPES,
        "devices": device_rows,
        "servers": [{
            "id": row.id, "name": row.name, "type": "server",
            "ips": list(dict.fromkeys([value for value in [row.management_ip] + [ip.ip_address for ip in row.extra_ips] if value])),
            "serial": row.serial or "", "model": row.model or "", "category": row.category or "服务器",
        } for row in servers],
    }


@router.get("/summary")
def summary(days: int = Query(default=90, ge=1, le=3650), db: Session = Depends(get_db)):
    assets = _query(db).all()
    rows = [_asset_response(asset) for asset in assets]
    upcoming = sorted(
        [row for row in rows if row["days_remaining"] is not None and row["days_remaining"] <= days],
        key=lambda row: row["days_remaining"],
    )
    lifecycle = {stage: 0 for stage in LIFECYCLE_STAGES}
    for row in rows:
        lifecycle[row["lifecycle_status"]] = lifecycle.get(row["lifecycle_status"], 0) + 1
    return {
        "total": len(rows), "in_service": lifecycle.get("在用", 0),
        "expired": sum(1 for row in rows if row["days_remaining"] is not None and row["days_remaining"] < 0),
        "expiring_30": sum(1 for row in rows if row["days_remaining"] is not None and 0 <= row["days_remaining"] <= 30),
        "expiring_90": sum(1 for row in rows if row["days_remaining"] is not None and 0 <= row["days_remaining"] <= 90),
        "lifecycle": lifecycle, "upcoming": upcoming[:20], "window_days": days,
    }


@router.get("/assets")
def list_assets(
    search: str = Query(default="", max_length=200),
    asset_type: str = Query(default="", max_length=50),
    lifecycle_status: str = Query(default="", max_length=30),
    expiry: Literal["", "expired", "30", "90", "none"] = "",
    db: Session = Depends(get_db),
):
    query = _query(db)
    if asset_type:
        query = query.filter(CMDBAsset.asset_type == asset_type)
    if lifecycle_status:
        query = query.filter(CMDBAsset.lifecycle_status == lifecycle_status)
    rows = [_asset_response(asset) for asset in query.order_by(CMDBAsset.name).limit(5000)]
    if search.strip():
        term = search.strip().casefold()
        rows = [row for row in rows if term in " ".join(
            [row["name"], row["model"], row["asset_tag"], row["owner"], row["department"]]
            + [ip["address"] for ip in row["ip_addresses"]]
            + [serial["serial_number"] for serial in row["serial_numbers"]]
        ).casefold()]
    if expiry == "expired":
        rows = [row for row in rows if row["days_remaining"] is not None and row["days_remaining"] < 0]
    elif expiry in {"30", "90"}:
        window = int(expiry)
        rows = [row for row in rows if row["days_remaining"] is not None and 0 <= row["days_remaining"] <= window]
    elif expiry == "none":
        rows = [row for row in rows if row["days_remaining"] is None]
    return rows


@router.get("/assets/{asset_id}")
def get_asset(asset_id: int, db: Session = Depends(get_db)):
    return _asset_response(_get_asset(db, asset_id), include_events=True)


@router.post("/assets", status_code=201)
def create_asset(payload: CMDBAssetRequest, request: Request, db: Session = Depends(get_db)):
    _validate_links(db, payload)
    asset = CMDBAsset()
    _apply(asset, payload)
    asset.lifecycle_events.append(CMDBLifecycleEvent(
        stage=payload.lifecycle_status, event_date=date.today(), notes="创建 CMDB 资产",
    ))
    db.add(asset)
    db.commit()
    db.refresh(asset)
    _audit(request, "cmdb.asset.create", str(asset.id), {"name": asset.name, "type": asset.asset_type})
    return _asset_response(_get_asset(db, asset.id), include_events=True)


@router.put("/assets/{asset_id}")
def update_asset(asset_id: int, payload: CMDBAssetRequest, request: Request, db: Session = Depends(get_db)):
    asset = _get_asset(db, asset_id)
    _validate_links(db, payload)
    old_status = asset.lifecycle_status
    _apply(asset, payload)
    if old_status != payload.lifecycle_status:
        asset.lifecycle_events.append(CMDBLifecycleEvent(
            stage=payload.lifecycle_status, event_date=date.today(), notes=f"状态由 {old_status} 调整为 {payload.lifecycle_status}",
        ))
    db.commit()
    _audit(request, "cmdb.asset.update", str(asset_id), {"status": payload.lifecycle_status})
    return _asset_response(_get_asset(db, asset_id), include_events=True)


@router.delete("/assets/{asset_id}")
def delete_asset(asset_id: int, request: Request, db: Session = Depends(get_db)):
    asset = _get_asset(db, asset_id)
    name = asset.name
    db.delete(asset)
    db.commit()
    _audit(request, "cmdb.asset.delete", str(asset_id), {"name": name})
    return {"ok": True, "message": "CMDB 资产已删除"}


@router.post("/batch-delete")
def batch_delete(payload: InventorySelectionRequest, request: Request, db: Session = Depends(get_db)):
    rows = db.query(CMDBAsset).filter(CMDBAsset.id.in_(payload.ids)).all()
    for row in rows:
        db.delete(row)
    db.commit()
    _audit(request, "cmdb.asset.batch_delete", detail={"requested": len(payload.ids), "deleted": len(rows)})
    return {"ok": True, "deleted": len(rows), "message": f"已删除 {len(rows)} 条 CMDB 资产"}


@router.post("/batch-status")
def batch_status(payload: BatchStatusRequest, request: Request, db: Session = Depends(get_db)):
    rows = db.query(CMDBAsset).filter(CMDBAsset.id.in_(payload.ids)).all()
    for row in rows:
        old = row.lifecycle_status
        row.lifecycle_status = payload.lifecycle_status
        if old != payload.lifecycle_status:
            row.lifecycle_events.append(CMDBLifecycleEvent(
                stage=payload.lifecycle_status, event_date=date.today(), operator=payload.operator,
                notes=payload.notes or f"批量状态由 {old} 调整为 {payload.lifecycle_status}",
            ))
    db.commit()
    _audit(request, "cmdb.asset.batch_status", detail={"updated": len(rows), "status": payload.lifecycle_status})
    return {"ok": True, "updated": len(rows), "message": f"已更新 {len(rows)} 条资产状态"}


@router.post("/assets/{asset_id}/events", status_code=201)
def add_event(asset_id: int, payload: LifecycleEventRequest, request: Request, db: Session = Depends(get_db)):
    asset = _get_asset(db, asset_id)
    event = CMDBLifecycleEvent(
        stage=payload.stage, event_date=payload.event_date,
        operator=payload.operator, notes=payload.notes,
    )
    asset.lifecycle_events.append(event)
    if payload.update_current_status:
        asset.lifecycle_status = payload.stage
    db.commit()
    _audit(request, "cmdb.lifecycle.add", str(asset_id), {"stage": payload.stage})
    return _asset_response(_get_asset(db, asset_id), include_events=True)
