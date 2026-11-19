"""CMDB asset lifecycle, warranty/licence reminders and bulk operations."""
from __future__ import annotations

import ipaddress
from datetime import date, datetime
import json
from typing import Literal, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import Field, ValidationError, field_validator, model_validator
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from app.api_models import InventorySelectionRequest, StrictRequest
from app.database import get_db
from app.models import (
    CMDBAsset, CMDBEntitlement, CMDBIPAddress, CMDBLifecycleEvent,
    CMDBSerialNumber, DCRack, DCSite, Device, DeviceInfo, ServerAsset, SystemSetting,
)
from app.services.cmdb_csv import decode_csv, export_csv, parse_csv


router = APIRouter(prefix="/api/cmdb", tags=["cmdb"])

LIFECYCLE_STAGES = ("规划", "采购", "到货", "部署", "在用", "维护", "停用", "退役", "处置")
ASSET_CLASSES = ("硬件", "软件", "其他")
ASSET_TYPES = ("防火墙", "交换机", "路由器", "无线 AP", "WLC", "IPT 电话", "VG 网关", "服务器", "存储", "业务软件", "基础软件", "SaaS 服务", "其他")
ENTITLEMENT_TYPES = ("硬件维保", "软件维保", "软件授权", "厂商支持", "订阅服务", "其他")
OPERATIONAL_STATUSES = ("运行", "备用", "维护", "离线", "未知", "退役")
ENVIRONMENTS = ("生产", "测试", "开发", "灾备", "办公", "其他")
SOURCE_TYPES = ("手工维护", "设备管理", "服务器存储", "CSV导入", "平台同步", "其他")


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
    entitlement_type: str = Field(default="硬件维保", min_length=1, max_length=50)
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
    name: str = Field(default="", max_length=200)
    asset_class: Literal["硬件", "软件", "其他"] = "硬件"
    asset_type: str = Field(default="交换机", min_length=1, max_length=50)
    company: str = Field(default="", max_length=100)
    brand: str = Field(default="", max_length=100)
    model: str = Field(default="", max_length=200)
    asset_tag: str = Field(default="", max_length=100)
    linked_device_id: Optional[int] = Field(default=None, gt=0)
    linked_server_id: Optional[int] = Field(default=None, gt=0)
    lifecycle_status: Literal["规划", "采购", "到货", "部署", "在用", "维护", "停用", "退役", "处置"] = "在用"
    operational_status: Literal["运行", "备用", "维护", "离线", "未知", "退役"] = "运行"
    criticality: Literal["低", "一般", "重要", "关键"] = "一般"
    environment: Literal["生产", "测试", "开发", "灾备", "办公", "其他"] = "生产"
    business_service: str = Field(default="", max_length=200)
    source_of_truth: Literal["手工维护", "设备管理", "服务器存储", "CSV导入", "平台同步", "其他"] = "手工维护"
    last_verified_date: Optional[date] = None
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
    sync_linked_fields: bool = True

    @model_validator(mode="after")
    def validate_links_and_dates(self):
        if self.linked_device_id and self.linked_server_id:
            raise ValueError("一个 CMDB 资产只能关联网络设备或服务器/存储之一")
        if not self.name.strip() and not (self.linked_device_id or self.linked_server_id):
            raise ValueError("设备名称不能为空；选择关联设备时可自动带入")
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


class CMDBCatalogRequest(StrictRequest):
    asset_types: list[str] = Field(default_factory=list, max_length=200)
    entitlement_types: list[str] = Field(default_factory=list, max_length=200)
    companies: list[str] = Field(default_factory=list, max_length=500)

    @field_validator("asset_types", "entitlement_types", "companies")
    @classmethod
    def clean_names(cls, values: list[str]) -> list[str]:
        cleaned = []
        for value in values:
            name = str(value or "").strip()
            if name and name not in cleaned:
                cleaned.append(name[:100])
        return cleaned


class AutoLinkRequest(StrictRequest):
    include_devices: bool = True
    include_servers: bool = True
    create_missing: bool = True
    sync_existing: bool = True


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
        "id": asset.id, "name": asset.name, "asset_class": asset.asset_class or "硬件",
        "asset_type": asset.asset_type, "company": asset.company or "",
        "brand": asset.brand or "", "model": asset.model or "", "asset_tag": asset.asset_tag or "",
        "linked_device_id": asset.linked_device_id, "linked_server_id": asset.linked_server_id,
        "lifecycle_status": asset.lifecycle_status, "operational_status": asset.operational_status or "运行",
        "criticality": asset.criticality, "environment": asset.environment or "生产",
        "business_service": asset.business_service or "",
        "source_of_truth": asset.source_of_truth or "手工维护",
        "last_verified_date": asset.last_verified_date,
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
        "name", "asset_class", "asset_type", "company", "brand", "model", "asset_tag", "linked_device_id", "linked_server_id",
        "lifecycle_status", "operational_status", "criticality", "environment", "business_service",
        "source_of_truth", "last_verified_date", "purchase_date", "initial_expiry_date", "vendor",
        "purchase_order", "purchase_cost", "currency", "department", "owner", "location", "notes",
    )
    for field in scalar_fields:
        setattr(asset, field, getattr(payload, field))
    existing_ips = {row.address: row for row in asset.ip_addresses}
    next_ips = []
    for item in payload.ip_addresses:
        row = existing_ips.get(item.address) or CMDBIPAddress()
        row.address, row.purpose, row.is_primary = item.address, item.purpose, item.is_primary
        next_ips.append(row)
    asset.ip_addresses = next_ips

    existing_serials = {row.serial_number.casefold(): row for row in asset.serial_numbers}
    next_serials = []
    for item in payload.serial_numbers:
        row = existing_serials.get(item.serial_number.casefold()) or CMDBSerialNumber()
        row.serial_number, row.kind, row.label = item.serial_number, item.kind, item.label
        next_serials.append(row)
    asset.serial_numbers = next_serials
    asset.entitlements = [CMDBEntitlement(**item.model_dump()) for item in payload.entitlements]


def _clean_ips(values) -> list[str]:
    result = []
    for raw in values:
        try:
            value = str(ipaddress.ip_address((raw or "").strip()))
        except ValueError:
            continue
        if value not in result:
            result.append(value)
    return result


def _device_asset_type(device: Device) -> str:
    marker = f"{device.device_type or ''} {device.function or ''}".casefold()
    if any(term in marker for term in ("asa", "ftd", "fortinet", "palo", "panos", "checkpoint", "firewall")):
        return "防火墙"
    if "wlc" in marker:
        return "WLC"
    if any(term in marker for term in ("wireless", "wifi", "cisco_ap", " access point", "ap ")):
        return "无线 AP"
    if any(term in marker for term in ("router", "路由")):
        return "路由器"
    if any(term in marker for term in ("cucm", "ipt", "iphone", "电话")):
        return "IPT 电话"
    if any(term in marker for term in ("voice gateway", "vg网关", "vg ")):
        return "VG 网关"
    return "交换机"


def _device_snapshot(db: Session, row: Device) -> dict:
    latest = db.query(DeviceInfo).filter(DeviceInfo.device_id == row.id).order_by(DeviceInfo.collected_at.desc()).first()
    serial = (latest.serial_number if latest else "") or ""
    status = {"online": "运行", "offline": "离线", "unknown": "未知"}.get(row.status or "", "未知")
    return {
        "id": row.id, "name": row.name or "", "type": "device",
        "asset_class": "硬件", "asset_type": _device_asset_type(row), "category": _device_asset_type(row),
        "company": row.company or "",
        "brand": (latest.vendor if latest else "") or "",
        "model": row.model or (latest.model if latest else "") or "",
        "asset_tag": "", "serial": serial,
        "serials": [serial] if serial else [],
        "ips": _clean_ips([row.ip_address] + [ip.ip_address for ip in (row.extra_ips or [])]),
        "location": (latest.location if latest else "") or "",
        "owner": (latest.contact if latest else "") or "",
        "operational_status": status, "source_of_truth": "设备管理",
        "last_verified_date": (latest.collected_at.date() if latest and latest.collected_at else None),
    }


def _server_snapshot(db: Session, row: ServerAsset) -> dict:
    rack = db.get(DCRack, row.rack_id) if row.rack_id else None
    site = db.get(DCSite, rack.site_id) if rack else None
    location_parts = [site.name if site else "", rack.rack_number if rack else ""]
    if rack:
        location_parts.append(f"U{row.u_start}–U{row.u_start + max(row.u_size or 1, 1) - 1}")
    status = {"在用": "运行", "备用": "备用", "停用": "离线", "报废": "退役"}.get(row.status or "", "未知")
    serial = row.serial or ""
    return {
        "id": row.id, "name": row.name or "", "type": "server",
        "asset_class": "硬件",
        "asset_type": "存储" if "存储" in (row.category or "") else "服务器",
        "company": (site.company if site else "") or "",
        "category": row.category or "服务器", "brand": row.brand or "", "model": row.model or "",
        "asset_tag": row.asset_tag or "", "serial": serial, "serials": [serial] if serial else [],
        "ips": _clean_ips([row.management_ip] + [ip.ip_address for ip in (row.extra_ips or [])]),
        "location": " / ".join(part for part in location_parts if part), "owner": row.owner or "",
        "operational_status": status, "source_of_truth": "服务器存储",
        "last_verified_date": row.updated_at.date() if row.updated_at else None,
    }


def _linked_snapshot(db: Session, linked_device_id: int | None, linked_server_id: int | None) -> dict | None:
    if linked_device_id:
        row = db.get(Device, linked_device_id)
        return _device_snapshot(db, row) if row else None
    if linked_server_id:
        row = db.get(ServerAsset, linked_server_id)
        return _server_snapshot(db, row) if row else None
    return None


def _hydrate_linked(db: Session, payload: CMDBAssetRequest, *, overwrite: bool = False) -> CMDBAssetRequest:
    if not payload.sync_linked_fields:
        return payload
    source = _linked_snapshot(db, payload.linked_device_id, payload.linked_server_id)
    if not source:
        return payload
    updates = {}
    for field in ("name", "company", "brand", "model", "asset_tag", "location", "owner"):
        incoming = source.get(field) or ""
        if incoming and (overwrite or not getattr(payload, field)):
            updates[field] = incoming
    if overwrite or payload.asset_type in {"", "网络设备", "交换机"}:
        updates["asset_type"] = source["asset_type"]
    updates["asset_class"] = source["asset_class"]
    updates["source_of_truth"] = source["source_of_truth"]
    updates["operational_status"] = source["operational_status"]
    if source.get("last_verified_date"):
        updates["last_verified_date"] = source["last_verified_date"]

    existing_ips = {item.address for item in payload.ip_addresses}
    ip_rows = list(payload.ip_addresses)
    has_primary = any(item.is_primary for item in ip_rows)
    if not has_primary and source["ips"] and source["ips"][0] in existing_ips:
        ip_rows = [item.model_copy(update={"is_primary": item.address == source["ips"][0]}) for item in ip_rows]
        has_primary = True
    for index, address in enumerate(source["ips"]):
        if address not in existing_ips:
            ip_rows.append(IPInput(address=address, purpose="管理", is_primary=not has_primary and index == 0))
            existing_ips.add(address)
            if not has_primary and index == 0:
                has_primary = True
    updates["ip_addresses"] = ip_rows

    existing_serials = {item.serial_number.casefold() for item in payload.serial_numbers}
    serial_rows = list(payload.serial_numbers)
    for serial in source["serials"]:
        if serial.casefold() not in existing_serials:
            serial_rows.append(SerialInput(serial_number=serial, kind="主机序列号", label=source["name"]))
            existing_serials.add(serial.casefold())
    updates["serial_numbers"] = serial_rows
    return payload.model_copy(update=updates)


def _catalog_names(db: Session, key: str, defaults) -> list[str]:
    row = db.get(SystemSetting, key)
    try:
        values = json.loads(row.value) if row and row.value else list(defaults)
    except (TypeError, json.JSONDecodeError):
        values = list(defaults)
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _company_names(db: Session) -> list[str]:
    configured = _catalog_names(db, "device_company_catalog", ())
    used = [value for (value,) in db.query(Device.company).distinct() if value]
    used.extend(value for (value,) in db.query(CMDBAsset.company).distinct() if value)
    return sorted(set(configured + used))


@router.get("/catalog")
def catalog(db: Session = Depends(get_db)):
    devices = db.query(Device).order_by(Device.name).all()
    servers = db.query(ServerAsset).order_by(ServerAsset.name).all()
    asset_types = _catalog_names(db, "cmdb_asset_type_catalog", ASSET_TYPES)
    asset_types.extend(value for (value,) in db.query(CMDBAsset.asset_type).distinct() if value and value not in asset_types)
    entitlement_types = _catalog_names(db, "cmdb_entitlement_type_catalog", ENTITLEMENT_TYPES)
    entitlement_types.extend(value for (value,) in db.query(CMDBEntitlement.entitlement_type).distinct() if value and value not in entitlement_types)
    return {
        "asset_classes": ASSET_CLASSES,
        "asset_types": asset_types,
        "companies": _company_names(db), "lifecycle_stages": LIFECYCLE_STAGES,
        "operational_statuses": OPERATIONAL_STATUSES, "environments": ENVIRONMENTS,
        "source_types": SOURCE_TYPES,
        "entitlement_types": entitlement_types,
        "devices": [_device_snapshot(db, row) for row in devices],
        "servers": [_server_snapshot(db, row) for row in servers],
    }


@router.put("/catalog")
def save_catalog(payload: CMDBCatalogRequest, request: Request, db: Session = Depends(get_db)):
    asset_types = payload.asset_types or list(ASSET_TYPES)
    entitlement_types = payload.entitlement_types or list(ENTITLEMENT_TYPES)
    used_asset_types = {value for (value,) in db.query(CMDBAsset.asset_type).distinct() if value}
    used_entitlement_types = {value for (value,) in db.query(CMDBEntitlement.entitlement_type).distinct() if value}
    used_companies = {value for (value,) in db.query(Device.company).distinct() if value}
    used_companies.update(value for (value,) in db.query(CMDBAsset.company).distinct() if value)
    if used_asset_types - set(asset_types):
        raise HTTPException(409, "不能删除仍被 CMDB 资产使用的资产类型")
    if used_entitlement_types - set(entitlement_types):
        raise HTTPException(409, "不能删除仍被维保/授权记录使用的类型")
    if used_companies - set(payload.companies):
        raise HTTPException(409, "不能删除仍被设备或 CMDB 资产使用的公司")
    for key, values in (
        ("cmdb_asset_type_catalog", asset_types),
        ("cmdb_entitlement_type_catalog", entitlement_types),
        ("device_company_catalog", payload.companies),
    ):
        row = db.get(SystemSetting, key)
        if row is None:
            row = SystemSetting(key=key)
            db.add(row)
        row.value = json.dumps(values, ensure_ascii=False)
    db.commit()
    _audit(request, "cmdb.catalog.update", detail={"asset_types": len(asset_types), "entitlement_types": len(entitlement_types), "companies": len(payload.companies)})
    return catalog(db)


@router.get("/summary")
def summary(days: int = Query(default=90, ge=1, le=3650), db: Session = Depends(get_db)):
    assets = _query(db).all()
    rows = [_asset_response(asset) for asset in assets]
    upcoming = sorted(
        [row for row in rows if row["days_remaining"] is not None and row["days_remaining"] <= days],
        key=lambda row: row["days_remaining"],
    )
    lifecycle = {stage: 0 for stage in LIFECYCLE_STAGES}
    operational = {status: 0 for status in OPERATIONAL_STATUSES}
    environments = {environment: 0 for environment in ENVIRONMENTS}
    classes = {asset_class: 0 for asset_class in ASSET_CLASSES}
    companies = {}
    for row in rows:
        lifecycle[row["lifecycle_status"]] = lifecycle.get(row["lifecycle_status"], 0) + 1
        operational[row["operational_status"]] = operational.get(row["operational_status"], 0) + 1
        environments[row["environment"]] = environments.get(row["environment"], 0) + 1
        classes[row["asset_class"]] = classes.get(row["asset_class"], 0) + 1
        if row["company"]:
            companies[row["company"]] = companies.get(row["company"], 0) + 1
    return {
        "total": len(rows), "in_service": lifecycle.get("在用", 0),
        "expired": sum(1 for row in rows if row["days_remaining"] is not None and row["days_remaining"] < 0),
        "expiring_30": sum(1 for row in rows if row["days_remaining"] is not None and 0 <= row["days_remaining"] <= 30),
        "expiring_90": sum(1 for row in rows if row["days_remaining"] is not None and 0 <= row["days_remaining"] <= 90),
        "lifecycle": lifecycle, "operational": operational, "environments": environments,
        "asset_classes": classes, "companies": companies,
        "upcoming": upcoming[:20], "window_days": days,
    }


@router.get("/assets")
def list_assets(
    search: str = Query(default="", max_length=200),
    asset_class: str = Query(default="", max_length=20),
    asset_type: str = Query(default="", max_length=50),
    company: str = Query(default="", max_length=100),
    lifecycle_status: str = Query(default="", max_length=30),
    operational_status: str = Query(default="", max_length=30),
    environment: str = Query(default="", max_length=30),
    expiry: Literal["", "expired", "30", "90", "none"] = "",
    db: Session = Depends(get_db),
):
    query = _query(db)
    if asset_class:
        query = query.filter(CMDBAsset.asset_class == asset_class)
    if asset_type:
        query = query.filter(CMDBAsset.asset_type == asset_type)
    if company:
        query = query.filter(CMDBAsset.company == company)
    if lifecycle_status:
        query = query.filter(CMDBAsset.lifecycle_status == lifecycle_status)
    if operational_status:
        query = query.filter(CMDBAsset.operational_status == operational_status)
    if environment:
        query = query.filter(CMDBAsset.environment == environment)
    rows = [_asset_response(asset) for asset in query.order_by(CMDBAsset.name).limit(5000)]
    if search.strip():
        term = search.strip().casefold()
        rows = [row for row in rows if term in " ".join(
            [row["name"], row["asset_class"], row["asset_type"], row["company"], row["model"], row["asset_tag"], row["owner"], row["department"],
             row["business_service"], row["source_of_truth"]]
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


def _normal_key(value) -> str:
    return str(value or "").strip().casefold()


def _import_indexes(rows: list[CMDBAsset]):
    indexes = {name: {} for name in ("device", "server", "tag", "serial", "ip", "name_type")}
    by_id = {}

    def add(bucket: str, key, asset: CMDBAsset):
        key = _normal_key(key)
        if key:
            indexes[bucket].setdefault(key, set()).add(asset.id)

    def add_asset(asset: CMDBAsset):
        by_id[asset.id] = asset
        add("device", asset.linked_device_id, asset)
        add("server", asset.linked_server_id, asset)
        add("tag", asset.asset_tag, asset)
        add("name_type", f"{asset.company}\0{asset.asset_class}\0{asset.asset_type}\0{asset.name}", asset)
        for item in asset.serial_numbers:
            add("serial", item.serial_number, asset)
        for item in asset.ip_addresses:
            add("ip", item.address, asset)

    for row in rows:
        add_asset(row)
    return indexes, by_id, add_asset


def _match_import(payload: CMDBAssetRequest, indexes, by_id) -> CMDBAsset | None:
    stages = []
    if payload.linked_device_id:
        stages.append(("关联网络设备", "device", [payload.linked_device_id]))
    if payload.linked_server_id:
        stages.append(("关联服务器", "server", [payload.linked_server_id]))
    if payload.asset_tag:
        stages.append(("资产编号", "tag", [payload.asset_tag]))
    if payload.serial_numbers:
        stages.append(("序列号", "serial", [item.serial_number for item in payload.serial_numbers]))
    if payload.ip_addresses:
        stages.append(("IP 地址", "ip", [item.address for item in payload.ip_addresses]))
    stages.append(("公司+名称+类型", "name_type", [f"{payload.company}\0{payload.asset_class}\0{payload.asset_type}\0{payload.name}"]))
    for label, bucket, keys in stages:
        ids = set()
        for key in keys:
            ids.update(indexes[bucket].get(_normal_key(key), set()))
        if len(ids) > 1:
            raise ValueError(f"{label}匹配到多条现有 CMDB 资产，请先清理重复数据")
        if ids:
            return by_id[next(iter(ids))]
    return None


def _resolve_link_names(db: Session, data: dict):
    device_name = data.pop("_linked_device_name", "")
    server_name = data.pop("_linked_server_name", "")
    if not data.get("linked_device_id") and device_name:
        matches = [row for row in db.query(Device).all() if _normal_key(row.name) == _normal_key(device_name)]
        if len(matches) != 1:
            raise ValueError(f"关联网络设备名称“{device_name}”匹配数量为 {len(matches)}")
        data["linked_device_id"] = matches[0].id
    if not data.get("linked_server_id") and server_name:
        matches = [row for row in db.query(ServerAsset).all() if _normal_key(row.name) == _normal_key(server_name)]
        if len(matches) != 1:
            raise ValueError(f"关联服务器/存储名称“{server_name}”匹配数量为 {len(matches)}")
        data["linked_server_id"] = matches[0].id


def _request_from_asset(asset: CMDBAsset, **updates) -> CMDBAssetRequest:
    data = {
        "name": asset.name, "asset_class": asset.asset_class or "硬件", "asset_type": asset.asset_type,
        "company": asset.company or "", "brand": asset.brand or "", "model": asset.model or "",
        "asset_tag": asset.asset_tag or "", "linked_device_id": asset.linked_device_id,
        "linked_server_id": asset.linked_server_id, "lifecycle_status": asset.lifecycle_status,
        "operational_status": asset.operational_status or "运行", "criticality": asset.criticality,
        "environment": asset.environment or "生产", "business_service": asset.business_service or "",
        "source_of_truth": asset.source_of_truth or "手工维护", "last_verified_date": asset.last_verified_date,
        "purchase_date": asset.purchase_date, "initial_expiry_date": asset.initial_expiry_date,
        "vendor": asset.vendor or "", "purchase_order": asset.purchase_order or "",
        "purchase_cost": asset.purchase_cost, "currency": asset.currency or "CNY",
        "department": asset.department or "", "owner": asset.owner or "", "location": asset.location or "",
        "notes": asset.notes or "",
        "ip_addresses": [{"address": row.address, "purpose": row.purpose or "管理", "is_primary": row.is_primary} for row in asset.ip_addresses],
        "serial_numbers": [{"serial_number": row.serial_number, "kind": row.kind or "主机序列号", "label": row.label or ""} for row in asset.serial_numbers],
        "entitlements": [{
            "entitlement_type": row.entitlement_type, "name": row.name, "vendor": row.vendor or "",
            "contract_number": row.contract_number or "", "start_date": row.start_date, "end_date": row.end_date,
            "reminder_days": row.reminder_days, "notes": row.notes or "",
        } for row in asset.entitlements],
    }
    data.update(updates)
    return CMDBAssetRequest.model_validate(data)


def _request_from_snapshot(source: dict, kind: str) -> CMDBAssetRequest:
    return CMDBAssetRequest(
        name=source["name"], asset_class=source["asset_class"], asset_type=source["asset_type"],
        company=source.get("company", ""), brand=source.get("brand", ""), model=source.get("model", ""),
        asset_tag=source.get("asset_tag", ""), linked_device_id=source["id"] if kind == "device" else None,
        linked_server_id=source["id"] if kind == "server" else None,
        operational_status=source.get("operational_status", "未知"), source_of_truth=source["source_of_truth"],
        last_verified_date=source.get("last_verified_date"), location=source.get("location", ""),
        owner=source.get("owner", ""),
        ip_addresses=[IPInput(address=value, purpose="管理", is_primary=index == 0) for index, value in enumerate(source.get("ips", []))],
        serial_numbers=[SerialInput(serial_number=value, kind="主机序列号", label=source["name"]) for value in source.get("serials", [])],
    )


@router.get("/auto-link/preview")
def preview_auto_link(db: Session = Depends(get_db)):
    device_total = db.query(Device).count()
    server_total = db.query(ServerAsset).count()
    linked_devices = db.query(CMDBAsset.linked_device_id).filter(CMDBAsset.linked_device_id.is_not(None)).distinct().count()
    linked_servers = db.query(CMDBAsset.linked_server_id).filter(CMDBAsset.linked_server_id.is_not(None)).distinct().count()
    return {
        "devices": {"total": device_total, "linked": linked_devices, "unlinked": max(device_total - linked_devices, 0)},
        "servers": {"total": server_total, "linked": linked_servers, "unlinked": max(server_total - linked_servers, 0)},
    }


@router.post("/auto-link")
def auto_link(payload: AutoLinkRequest, request: Request, db: Session = Depends(get_db)):
    assets = _query(db).all()
    indexes, by_id, add_index = _import_indexes(assets)
    sources = []
    if payload.include_devices:
        sources.extend(("device", _device_snapshot(db, row)) for row in db.query(Device).order_by(Device.id).all())
    if payload.include_servers:
        sources.extend(("server", _server_snapshot(db, row)) for row in db.query(ServerAsset).order_by(ServerAsset.id).all())
    created = updated = skipped = ambiguous = 0
    errors = []
    for kind, source in sources:
        try:
            source_request = _request_from_snapshot(source, kind)
            asset = _match_import(source_request, indexes, by_id)
            if asset is None:
                name_matches = [row for row in by_id.values()
                                if not row.linked_device_id and not row.linked_server_id
                                and _normal_key(row.name) == _normal_key(source["name"])
                                and _normal_key(row.company) == _normal_key(source.get("company", ""))]
                if len(name_matches) > 1:
                    raise ValueError("公司+名称匹配到多条未关联 CMDB 资产")
                asset = name_matches[0] if name_matches else None
            if asset:
                conflict = (
                    kind == "device" and asset.linked_device_id not in (None, source["id"])
                ) or (
                    kind == "server" and asset.linked_server_id not in (None, source["id"])
                ) or (kind == "device" and asset.linked_server_id is not None) or (kind == "server" and asset.linked_device_id is not None)
                if conflict:
                    skipped += 1
                    errors.append({"source": source["name"], "error": "稳定标识匹配到已关联其他来源的 CMDB 资产"})
                    continue
                if not payload.sync_existing:
                    skipped += 1
                    continue
                updates = {"linked_device_id": source["id"]} if kind == "device" else {"linked_server_id": source["id"]}
                merged = _hydrate_linked(db, _request_from_asset(asset, **updates), overwrite=True)
                _apply(asset, merged)
                updated += 1
            elif payload.create_missing:
                asset = CMDBAsset()
                _apply(asset, source_request)
                asset.lifecycle_events.append(CMDBLifecycleEvent(stage="在用", event_date=date.today(), notes="一键关联自动创建"))
                db.add(asset); db.flush(); created += 1
            else:
                skipped += 1
                continue
            db.flush(); add_index(asset)
        except ValueError as exc:
            ambiguous += 1
            errors.append({"source": source["name"], "error": str(exc)[:300]})
    db.commit()
    _audit(request, "cmdb.asset.auto_link", detail={"created": created, "updated": updated, "skipped": skipped, "ambiguous": ambiguous})
    return {"ok": not errors, "total": len(sources), "created": created, "updated": updated, "skipped": skipped, "ambiguous": ambiguous, "errors": errors[:100]}


@router.get("/export")
def export_assets(
    template: bool = False,
    ids: str = Query(default="", max_length=50000),
    db: Session = Depends(get_db),
):
    assets = [] if template else _query(db).order_by(CMDBAsset.name).all()
    if ids and not template:
        try:
            selected = {int(value) for value in ids.split(",") if value.strip()}
        except ValueError:
            raise HTTPException(status_code=422, detail="ids 必须是逗号分隔的整数") from None
        assets = [row for row in assets if row.id in selected]
    device_names = {row.id: row.name for row in db.query(Device).all()}
    server_names = {row.id: row.name for row in db.query(ServerAsset).all()}
    rows = []
    for asset in assets:
        item = _asset_response(asset)
        item["linked_device_name"] = device_names.get(asset.linked_device_id, "")
        item["linked_server_name"] = server_names.get(asset.linked_server_id, "")
        rows.append(item)
    filename = "cmdb_template.csv" if template else "cmdb_assets.csv"
    return Response(
        content=export_csv(rows, template=template), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/import")
async def import_assets(
    request: Request,
    file: UploadFile = File(...),
    mode: Literal["skip", "overwrite"] = Query(default="skip"),
    db: Session = Depends(get_db),
):
    try:
        parsed = parse_csv(decode_csv(await file.read(10_000_001)))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    existing = _query(db).all()
    indexes, by_id, add_index = _import_indexes(existing)
    created = updated = skipped = 0
    errors = []
    for source in parsed:
        line_number = source.pop("_line_number")
        try:
            with db.begin_nested():
                _resolve_link_names(db, source)
                payload = CMDBAssetRequest.model_validate(source)
                _validate_links(db, payload)
                payload = _hydrate_linked(db, payload)
                if not payload.name.strip():
                    raise ValueError("设备名称不能为空")
                asset = _match_import(payload, indexes, by_id)
                if asset and mode == "skip":
                    skipped += 1
                    continue
                if asset:
                    old_status = asset.lifecycle_status
                    _apply(asset, payload)
                    if old_status != payload.lifecycle_status:
                        asset.lifecycle_events.append(CMDBLifecycleEvent(
                            stage=payload.lifecycle_status, event_date=date.today(),
                            notes=f"CSV 导入覆盖：{old_status} → {payload.lifecycle_status}",
                        ))
                    updated += 1
                else:
                    asset = CMDBAsset()
                    _apply(asset, payload)
                    asset.lifecycle_events.append(CMDBLifecycleEvent(
                        stage=payload.lifecycle_status, event_date=date.today(), notes="CSV 导入创建",
                    ))
                    db.add(asset)
                    created += 1
                db.flush()
                add_index(asset)
        except (ValidationError, ValueError, HTTPException, SQLAlchemyError) as exc:
            if isinstance(exc, ValidationError):
                message = "; ".join(error["msg"] for error in exc.errors())
            elif isinstance(exc, HTTPException):
                message = str(exc.detail)
            elif isinstance(exc, SQLAlchemyError):
                message = "数据库约束冲突，请检查资产编号、IP、序列号或关联对象"
            else:
                message = str(exc)
            errors.append({"line": line_number, "error": message[:500]})
    db.commit()
    request.state.audit_action = "cmdb.asset.import"
    request.state.audit_resource_type = "cmdb_asset"
    request.state.audit_detail = {
        "mode": mode, "created": created, "updated": updated,
        "skipped": skipped, "errors": len(errors), "file": (file.filename or "")[:200],
    }
    return {
        "ok": not errors, "mode": mode, "created": created, "updated": updated,
        "skipped": skipped, "error_count": len(errors), "errors": errors[:100],
    }


@router.get("/assets/{asset_id}")
def get_asset(asset_id: int, db: Session = Depends(get_db)):
    return _asset_response(_get_asset(db, asset_id), include_events=True)


@router.post("/assets", status_code=201)
def create_asset(payload: CMDBAssetRequest, request: Request, db: Session = Depends(get_db)):
    _validate_links(db, payload)
    payload = _hydrate_linked(db, payload)
    if not payload.name.strip():
        raise HTTPException(status_code=422, detail="关联对象没有可用名称，请手工填写")
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
    payload = _hydrate_linked(db, payload)
    if not payload.name.strip():
        raise HTTPException(status_code=422, detail="CMDB 资产名称不能为空")
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
