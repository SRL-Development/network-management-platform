import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import CMDBAsset, CMDBEntitlement, NotificationAlert
from app.routers.notifications import router
from app.services.email_notifications import load_config, run_email_notifications, send_anonymous


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        app = FastAPI(); app.include_router(router); app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close(); self.db.close(); self.engine.dispose()

    def config(self):
        return {
            "enabled": True, "smtp_host": "mail.internal", "smtp_port": 25, "security": "none",
            "from_address": "netmgr@example.com", "recipients": ["ops@example.com"],
            "repeat_days": 7, "cmdb_days": 90, "backup_coverage_days": 7, "backup_min_rate": 95,
            "modules": {"cmdb_expiry": True, "backup_coverage": False, "backup_failures": False, "device_offline": False},
            "templates": {"cmdb_expiry": {"subject": "到期 {count}", "body": "{items}"}},
        }

    def test_cmdb_weekly_repeat_and_acknowledgement(self):
        response = self.client.put("/api/notifications/config", json=self.config())
        self.assertEqual(response.status_code, 200, response.text)
        asset = CMDBAsset(name="防火墙", asset_class="硬件", asset_type="防火墙", company="总部")
        asset.entitlements.append(CMDBEntitlement(
            entitlement_type="订阅服务", name="威胁订阅", end_date=date.today() + timedelta(days=30), reminder_days=90,
        ))
        self.db.add(asset); self.db.commit()
        with patch("app.services.email_notifications.send_anonymous") as send:
            first = run_email_notifications([], self.db)
            second = run_email_notifications([], self.db)
        self.assertEqual(first.status, "success")
        self.assertEqual(send.call_count, 1)
        alert = self.db.query(NotificationAlert).one()
        self.assertIsNotNone(alert.last_sent_at)
        result = self.client.post("/api/notifications/alerts/acknowledge", json={"ids": [alert.id]})
        self.assertEqual(result.status_code, 200, result.text)
        alert.last_sent_at = alert.last_sent_at - timedelta(days=8); self.db.commit()
        with patch("app.services.email_notifications.send_anonymous") as send:
            run_email_notifications([], self.db)
        send.assert_not_called()

    def test_smtp_delivery_never_authenticates(self):
        smtp = MagicMock()
        smtp.__enter__.return_value = smtp
        smtp.__exit__.return_value = False
        with patch("app.services.email_notifications.smtplib.SMTP", return_value=smtp):
            send_anonymous(self.config(), "测试", "正文")
        smtp.send_message.assert_called_once()
        self.assertFalse(smtp.login.called)


if __name__ == "__main__":
    unittest.main()
