"""CSV interchange for CMDB configuration items.

The flat columns stay friendly to Excel while JSON columns preserve nested IP,
serial and entitlement metadata for a lossless export/import round trip.
"""
from __future__ import annotations

import csv
import io
import json
import re
from typing import Any


CSV_COLUMNS = (
    "name", "asset_class", "asset_type", "company", "brand", "model", "asset_tag",
    "lifecycle_status", "operational_status", "criticality", "environment",
    "business_service", "source_of_truth", "last_verified_date",
    "purchase_date", "initial_expiry_date", "vendor", "purchase_order",
    "purchase_cost", "currency", "department", "owner", "location",
    "linked_device_id", "linked_device_name", "linked_server_id", "linked_server_name",
    "primary_ip", "additional_ips", "serial_numbers",
    "ip_addresses_json", "serial_numbers_json", "entitlements_json", "notes",
)

HEADER_ALIASES = {
    "设备名称": "name", "资产大类": "asset_class", "资产类型": "asset_type", "所属公司": "company", "公司": "company", "品牌": "brand", "型号": "model",
    "资产编号": "asset_tag", "生命周期": "lifecycle_status", "运行状态": "operational_status",
    "重要程度": "criticality", "环境": "environment", "业务服务": "business_service",
    "权威数据源": "source_of_truth", "最近核验日期": "last_verified_date",
    "购买日期": "purchase_date", "首次到期日": "initial_expiry_date", "供应商": "vendor",
    "采购单": "purchase_order", "采购金额": "purchase_cost", "币种": "currency",
    "使用部门": "department", "责任人": "owner", "位置": "location",
    "关联网络设备ID": "linked_device_id", "关联网络设备": "linked_device_name",
    "关联服务器ID": "linked_server_id", "关联服务器": "linked_server_name",
    "主管理IP": "primary_ip", "其他IP": "additional_ips", "序列号": "serial_numbers",
    "备注": "notes",
}


def decode_csv(content: bytes) -> str:
    if len(content) > 10_000_000:
        raise ValueError("CSV 文件不能超过 10 MB")
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("无法识别 CSV 编码，请使用 UTF-8 或 GB18030")


def _normalise_row(row: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in row.items():
        clean_key = (key or "").strip().lstrip("\ufeff")
        canonical = HEADER_ALIASES.get(clean_key, clean_key)
        result[canonical] = (value or "").strip()
    return result


def _split(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[;；|\n]+", value or "") if item.strip()]


def _json_list(value: str, label: str) -> list[dict]:
    if not value:
        return []
    try:
        result = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} JSON 格式错误：{exc.msg}") from exc
    if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
        raise ValueError(f"{label} 必须是 JSON 对象数组")
    return result


def _integer(value: str, label: str) -> int | None:
    if not value:
        return None
    try:
        number = int(value)
    except ValueError as exc:
        raise ValueError(f"{label} 必须是整数") from exc
    if number <= 0:
        raise ValueError(f"{label} 必须大于 0")
    return number


def _ip_rows(row: dict[str, str]) -> list[dict]:
    detailed = _json_list(row.get("ip_addresses_json", ""), "IP 地址")
    if detailed:
        return detailed
    primary = row.get("primary_ip", "")
    values = _split(row.get("additional_ips", ""))
    if row.get("management_ips"):
        values = _split(row["management_ips"])
        primary = primary or (values[0] if values else "")
    ordered = list(dict.fromkeys(([primary] if primary else []) + values))
    return [{"address": value, "purpose": "管理", "is_primary": value == primary} for value in ordered]


def _serial_rows(row: dict[str, str]) -> list[dict]:
    detailed = _json_list(row.get("serial_numbers_json", ""), "序列号")
    if detailed:
        return detailed
    return [{"serial_number": value, "kind": "主机序列号", "label": ""}
            for value in _split(row.get("serial_numbers", ""))]


def parse_csv(raw: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(raw))
    if not reader.fieldnames:
        raise ValueError("CSV 缺少表头")
    rows: list[dict] = []
    for line_number, source in enumerate(reader, start=2):
        row = _normalise_row(source)
        if not any(row.values()):
            continue
        payload = {
            "name": row.get("name", ""),
            "asset_class": row.get("asset_class") or "硬件",
            "asset_type": row.get("asset_type") or "交换机",
            "company": row.get("company", ""),
            "brand": row.get("brand", ""), "model": row.get("model", ""),
            "asset_tag": row.get("asset_tag", ""),
            "lifecycle_status": row.get("lifecycle_status") or "在用",
            "operational_status": row.get("operational_status") or "运行",
            "criticality": row.get("criticality") or "一般",
            "environment": row.get("environment") or "生产",
            "business_service": row.get("business_service", ""),
            "source_of_truth": row.get("source_of_truth") or "CSV导入",
            "last_verified_date": row.get("last_verified_date") or None,
            "purchase_date": row.get("purchase_date") or None,
            "initial_expiry_date": row.get("initial_expiry_date") or None,
            "vendor": row.get("vendor", ""), "purchase_order": row.get("purchase_order", ""),
            "purchase_cost": row.get("purchase_cost") or None,
            "currency": row.get("currency") or "CNY", "department": row.get("department", ""),
            "owner": row.get("owner", ""), "location": row.get("location", ""),
            "linked_device_id": _integer(row.get("linked_device_id", ""), "linked_device_id"),
            "linked_server_id": _integer(row.get("linked_server_id", ""), "linked_server_id"),
            "ip_addresses": _ip_rows(row), "serial_numbers": _serial_rows(row),
            "entitlements": _json_list(row.get("entitlements_json", ""), "维保与授权"),
            "notes": row.get("notes", ""),
        }
        payload["_linked_device_name"] = row.get("linked_device_name", "")
        payload["_linked_server_name"] = row.get("linked_server_name", "")
        payload["_line_number"] = line_number
        rows.append(payload)
    if not rows:
        raise ValueError("CSV 中没有可导入的数据")
    if len(rows) > 5000:
        raise ValueError("单次最多导入 5000 条 CMDB 资产")
    return rows


def _json(value: Any) -> str:
    return json.dumps(value or [], ensure_ascii=False, separators=(",", ":"))


def export_csv(rows: list[dict], template: bool = False) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    if template:
        writer.writerow({
            "name": "核心交换机-A", "asset_class": "硬件", "asset_type": "交换机", "company": "集团总部", "brand": "Cisco",
            "model": "C9500-48Y4C", "asset_tag": "NET-001", "lifecycle_status": "在用",
            "operational_status": "运行", "criticality": "关键", "environment": "生产",
            "business_service": "园区网核心", "source_of_truth": "CSV导入",
            "last_verified_date": "2026-11-19", "department": "网络部", "owner": "网络管理员",
            "location": "总部机房 A01 机柜", "primary_ip": "10.0.0.1",
            "additional_ips": "10.0.0.2;10.0.0.3", "serial_numbers": "FOC123;FOC456",
            "notes": "模板示例，导入前请删除或修改",
        })
    else:
        for row in rows:
            ips = row.get("ip_addresses", [])
            serials = row.get("serial_numbers", [])
            primary = next((item.get("address", "") for item in ips if item.get("is_primary")), "")
            additional = [item.get("address", "") for item in ips if item.get("address") and item.get("address") != primary]
            writer.writerow({
                **row,
                "last_verified_date": row.get("last_verified_date") or "",
                "purchase_date": row.get("purchase_date") or "",
                "initial_expiry_date": row.get("initial_expiry_date") or "",
                "primary_ip": primary,
                "additional_ips": ";".join(additional),
                "serial_numbers": ";".join(item.get("serial_number", "") for item in serials if item.get("serial_number")),
                "ip_addresses_json": _json(ips),
                "serial_numbers_json": _json(serials),
                "entitlements_json": _json(row.get("entitlements", [])),
            })
    return "\ufeff" + output.getvalue()
