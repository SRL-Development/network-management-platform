import unittest
from datetime import date, timedelta
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import CMDBAsset, Device, DeviceInfo, ScheduleConfig
from app.routers.cmdb import router as cmdb_router
from app.routers.integrations import router as integrations_router


class CMDBTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.device = Device(name="core-sw", ip_address="10.0.0.1", device_type="cisco_ios")
        self.db.add(self.device)
        self.db.commit()
        app = FastAPI()
        app.include_router(cmdb_router)
        app.include_router(integrations_router)
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.db.close()
        self.engine.dispose()

    def payload(self):
        return {
            "name": "核心交换机 A", "asset_type": "网络设备", "brand": "Cisco",
            "model": "C9500", "asset_tag": "NET-001", "linked_device_id": self.device.id,
            "lifecycle_status": "在用", "criticality": "关键",
            "purchase_date": str(date.today() - timedelta(days=600)),
            "initial_expiry_date": str(date.today() + timedelta(days=75)),
            "vendor": "供应商A", "purchase_order": "PO-001", "department": "网络部",
            "owner": "张三", "location": "总部机房", "notes": "生产核心",
            "ip_addresses": [
                {"address": "10.0.0.1", "purpose": "管理", "is_primary": True},
                {"address": "10.0.0.2", "purpose": "带外", "is_primary": False},
            ],
            "serial_numbers": [
                {"serial_number": "FOC-A", "kind": "主机序列号", "label": "机框"},
                {"serial_number": "PSU-B", "kind": "电源序列号", "label": "电源1"},
            ],
            "entitlements": [
                {"entitlement_type": "硬件维保", "name": "SmartNet", "vendor": "Cisco", "end_date": str(date.today() + timedelta(days=45)), "reminder_days": 90},
                {"entitlement_type": "软件授权", "name": "DNA", "vendor": "Cisco", "end_date": str(date.today() + timedelta(days=365)), "reminder_days": 90},
            ],
        }

    def test_multi_value_asset_expiry_search_and_lifecycle(self):
        response = self.client.post("/api/cmdb/assets", json=self.payload())
        self.assertEqual(response.status_code, 201, response.text)
        row = response.json()
        self.assertEqual(len(row["ip_addresses"]), 2)
        self.assertEqual(len(row["serial_numbers"]), 2)
        self.assertEqual(len(row["entitlements"]), 2)
        self.assertEqual(row["days_remaining"], 45)
        self.assertEqual(row["next_expiry_name"], "SmartNet")
        self.assertEqual(len(row["lifecycle_events"]), 1)

        by_ip = self.client.get("/api/cmdb/assets?search=10.0.0.2")
        by_serial = self.client.get("/api/cmdb/assets?search=PSU-B")
        self.assertEqual([item["id"] for item in by_ip.json()], [row["id"]])
        self.assertEqual([item["id"] for item in by_serial.json()], [row["id"]])
        summary = self.client.get("/api/cmdb/summary?days=90").json()
        self.assertEqual(summary["total"], 1)
        self.assertEqual(summary["expiring_90"], 1)

        event = self.client.post(f"/api/cmdb/assets/{row['id']}/events", json={
            "stage": "维护", "event_date": str(date.today()), "operator": "李四",
            "notes": "更换电源", "update_current_status": True,
        })
        self.assertEqual(event.status_code, 201, event.text)
        self.assertEqual(event.json()["lifecycle_status"], "维护")
        self.assertEqual(len(event.json()["lifecycle_events"]), 2)

    def test_validation_and_bulk_operations(self):
        invalid = self.payload()
        invalid["ip_addresses"][1]["address"] = "10.0.0.1"
        self.assertEqual(self.client.post("/api/cmdb/assets", json=invalid).status_code, 422)
        created = self.client.post("/api/cmdb/assets", json=self.payload()).json()
        result = self.client.post("/api/cmdb/batch-status", json={
            "ids": [created["id"]], "lifecycle_status": "退役", "notes": "替换设备",
        })
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.client.get(f"/api/cmdb/assets/{created['id']}").json()["lifecycle_status"], "退役")
        result = self.client.post("/api/cmdb/batch-delete", json={"ids": [created["id"]]})
        self.assertEqual(result.json()["deleted"], 1)
        self.assertEqual(self.db.query(CMDBAsset).count(), 0)

    def test_linked_device_automatically_hydrates_identity(self):
        self.device.model = "C9500-48Y4C"
        self.device.status = "online"
        self.db.add(DeviceInfo(
            device_id=self.device.id, vendor="Cisco", model="C9500-48Y4C",
            serial_number="FOC-LINKED-1", location="总部机房", contact="网络组",
        ))
        self.db.commit()
        response = self.client.post("/api/cmdb/assets", json={
            "name": "", "linked_device_id": self.device.id,
            "asset_type": "网络设备", "lifecycle_status": "在用",
        })
        self.assertEqual(response.status_code, 201, response.text)
        row = response.json()
        self.assertEqual(row["name"], "core-sw")
        self.assertEqual(row["brand"], "Cisco")
        self.assertEqual(row["model"], "C9500-48Y4C")
        self.assertEqual(row["source_of_truth"], "设备管理")
        self.assertEqual(row["operational_status"], "运行")
        self.assertEqual([item["address"] for item in row["ip_addresses"]], ["10.0.0.1"])
        self.assertEqual([item["serial_number"] for item in row["serial_numbers"]], ["FOC-LINKED-1"])

    def test_csv_template_import_dedup_overwrite_and_export(self):
        raw = (
            "name,asset_type,asset_tag,lifecycle_status,operational_status,environment,"
            "business_service,primary_ip,serial_numbers,owner\n"
            "边界防火墙,安全设备,SEC-001,在用,运行,生产,互联网出口,192.0.2.10,PA-001,安全组\n"
        ).encode("utf-8-sig")
        response = self.client.post(
            "/api/cmdb/import?mode=skip", files={"file": ("cmdb.csv", raw, "text/csv")},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["created"], 1)
        repeated = self.client.post(
            "/api/cmdb/import?mode=skip", files={"file": ("cmdb.csv", raw, "text/csv")},
        ).json()
        self.assertEqual((repeated["created"], repeated["skipped"]), (0, 1))
        overwrite = raw.replace("安全组".encode(), "SOC".encode())
        updated = self.client.post(
            "/api/cmdb/import?mode=overwrite", files={"file": ("cmdb.csv", overwrite, "text/csv")},
        ).json()
        self.assertEqual(updated["updated"], 1)
        self.assertEqual(self.client.get("/api/cmdb/assets").json()[0]["owner"], "SOC")

        template = self.client.get("/api/cmdb/export?template=true")
        exported = self.client.get("/api/cmdb/export")
        self.assertEqual(template.status_code, 200)
        self.assertTrue(template.content.startswith(b"\xef\xbb\xbf"))
        text = exported.content.decode("utf-8-sig")
        self.assertIn("ip_addresses_json", text)
        self.assertIn("SEC-001", text)

    def test_asset_classes_company_and_custom_catalogs(self):
        saved = self.client.put("/api/cmdb/catalog", json={
            "asset_types": ["防火墙", "无线 AP", "财务软件"],
            "entitlement_types": ["硬件维保", "软件维保", "安全订阅"],
            "companies": ["集团总部", "子公司 A"],
        })
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertIn("财务软件", saved.json()["asset_types"])

        firewall = self.client.post("/api/cmdb/assets", json={
            "name": "边界防火墙", "asset_class": "硬件", "asset_type": "防火墙",
            "company": "集团总部", "purchase_date": "2026-01-01",
            "entitlements": [
                {"entitlement_type": "硬件维保", "name": "整机维保"},
                {"entitlement_type": "安全订阅", "name": "威胁防护订阅"},
            ],
        })
        software = self.client.post("/api/cmdb/assets", json={
            "name": "财务系统", "asset_class": "软件", "asset_type": "财务软件",
            "company": "子公司 A", "entitlements": [
                {"entitlement_type": "软件维保", "name": "年度升级服务"},
            ],
        })
        ap = self.client.post("/api/cmdb/assets", json={
            "name": "AP-01", "asset_class": "硬件", "asset_type": "无线 AP",
            "company": "集团总部", "purchase_date": "2025-06-01", "entitlements": [],
        })
        self.assertEqual((firewall.status_code, software.status_code, ap.status_code), (201, 201, 201))
        self.assertEqual(len(firewall.json()["entitlements"]), 2)
        self.assertEqual(ap.json()["entitlements"], [])
        rows = self.client.get("/api/cmdb/assets?asset_class=软件&company=子公司%20A").json()
        self.assertEqual([row["name"] for row in rows], ["财务系统"])

        blocked = self.client.put("/api/cmdb/catalog", json={
            "asset_types": ["防火墙", "无线 AP"],
            "entitlement_types": ["硬件维保", "安全订阅"],
            "companies": ["集团总部"],
        })
        self.assertEqual(blocked.status_code, 409)

    def test_vcenter_autosync_configuration(self):
        with patch("app.scheduler.tasks.reload_scheduler"):
            response = self.client.put("/api/integrations/vcenter-autosync", json={
                "enabled": True, "interval_minutes": 360,
            })
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertTrue(data["enabled"])
        self.assertEqual(data["interval_minutes"], 360)
        schedule = self.db.query(ScheduleConfig).filter_by(task_type="vcenter_sync").one()
        self.assertEqual(schedule.cron_expression, "30 */6 * * *")
        self.assertEqual(self.client.put("/api/integrations/vcenter-autosync", json={"enabled": True, "interval_minutes": 10}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
