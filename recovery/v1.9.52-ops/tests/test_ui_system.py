"""Static contract tests for the shared v1.9.52.12 UI system."""
import re
import unittest
from pathlib import Path

from app.auth import module_for_path


ROOT = Path(__file__).resolve().parents[1]


class SharedUiSystemTests(unittest.TestCase):
    def test_all_business_pages_use_shared_shell(self):
        routed = {
            "dashboard.html", "devices.html", "device_detail.html", "topology.html",
            "backups.html", "changes.html", "settings.html", "schedule.html",
            "ip_inventory.html", "ipam.html", "isp.html", "dc.html", "assets.html",
            "servers.html", "accounts.html", "vms.html", "integrations.html",
            "cmdb.html", "notifications.html",
        }
        for name in routed:
            source = (ROOT / "app" / "templates" / name).read_text(encoding="utf-8")
            self.assertIn('{% extends "base.html" %}', source, name)

    def test_shell_has_navigation_search_mobile_controls_and_live_feedback(self):
        source = (ROOT / "app" / "templates" / "base.html").read_text(encoding="utf-8")
        for marker in (
            'id="appSidebar"', 'id="navSearch"', 'id="sidebarToggle"',
            'id="mobileScrim"', 'id="currentPageTitle"', 'aria-live="polite"',
            'class="nav-section-label"', 'id="mainContent"',
        ):
            self.assertIn(marker, source)

    def test_design_system_covers_core_components_and_responsive_modes(self):
        source = (ROOT / "app" / "static" / "css" / "style.css").read_text(encoding="utf-8")
        for marker in (
            "Cisco Network Manager design system · v1.9.52.12", ".topbar {",
            "table.data-table th {", ".modal-overlay.active", ".login-shell {",
            ".module-overview {", ".section-jumpbar {", ".table-summary {",
            "@media (max-width: 900px)", "@media (max-width: 720px)",
            "@media (prefers-reduced-motion: reduce)", "@media print",
        ):
            self.assertIn(marker, source)

    def test_shared_script_supports_navigation_keyboard_and_accessibility(self):
        source = (ROOT / "app" / "static" / "js" / "app.js").read_text(encoding="utf-8")
        for marker in (
            "initApplicationShell", "initModuleWorkspace", "MODULE_PRESENTATION",
            "function enhanceUi", "table-summary", "aria-labelledby",
            "event.key === '/'", "event.key === 'Escape'", "nav-logs",
        ):
            self.assertIn(marker, source)
        self.assertRegex(source, re.compile(r"table\.querySelectorAll\('thead th'\)"))

    def test_cmdb_and_vcenter_autosync_ui_contracts(self):
        self.assertEqual(module_for_path("/cmdb"), "cmdb")
        self.assertEqual(module_for_path("/api/cmdb/assets"), "cmdb")
        cmdb = (ROOT / "app" / "templates" / "cmdb.html").read_text(encoding="utf-8")
        cmdb_js = (ROOT / "app" / "static" / "js" / "cmdb.js").read_text(encoding="utf-8")
        integration = (ROOT / "app" / "templates" / "integrations.html").read_text(encoding="utf-8")
        integration_js = (ROOT / "app" / "static" / "js" / "integrations.js").read_text(encoding="utf-8")
        for marker in ('id="cmdb-select-all"', 'id="cmdb-ip-list"', 'id="cmdb-serial-list"', 'id="cmdb-entitlement-list"', 'id="cmdb-event-list"', 'id="cmdb-import"', 'id="cmdb-export"', 'id="cmdb-operational-status"', 'id="cmdb-source-of-truth"', 'id="cmdb-asset-class"', 'id="cmdb-company"', 'id="cmdb-manage-catalog"'):
            self.assertIn(marker, cmdb)
        for marker in ("/api/cmdb/assets", "/api/cmdb/batch-delete", "/api/cmdb/batch-status", "/api/cmdb/import", "/api/cmdb/export", "/api/cmdb/catalog", "/api/cmdb/auto-link", "importLinked(false,true)"):
            self.assertIn(marker, cmdb_js)
        self.assertIn('id="vcenter-autosync-enabled"', integration)
        self.assertIn('/api/integrations/vcenter-autosync', integration_js)

    def test_notification_center_contract(self):
        self.assertEqual(module_for_path("/notifications"), "notifications")
        self.assertEqual(module_for_path("/api/notifications/config"), "notifications")
        page = (ROOT / "app" / "templates" / "notifications.html").read_text(encoding="utf-8")
        script = (ROOT / "app" / "static" / "js" / "notifications.js").read_text(encoding="utf-8")
        for marker in ('id="mail-host"', 'id="mail-recipients"', 'id="mail-modules"', 'id="mail-alert-rows"', '匿名 SMTP'):
            self.assertIn(marker, page)
        for marker in ('/api/notifications/config', '/api/notifications/test', '/api/notifications/run', '/api/notifications/alerts/acknowledge'):
            self.assertIn(marker, script)


if __name__ == "__main__":
    unittest.main()
