"""Static regression guards for frontend pieces (no JS harness exists).

Asserts the Phase 2-4 UI contracts are present in static/app.js so refactors can't
silently drop them. Run: python -m unittest tests.test_frontend_static -v
"""
import os
import unittest

APP_JS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "app.js")


class TestFrontendContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(APP_JS, encoding="utf-8") as f:
            cls.src = f.read()

    def assertPresent(self, needle, label):
        self.assertIn(needle, self.src, f"missing frontend contract: {label}")

    def test_share_checklist(self):
        self.assertPresent("sh-checklist", "checklist container")
        self.assertPresent("Before you send", "checklist heading")
        self.assertPresent('id="sh-qa"', "sample-QA checkbox")
        self.assertPresent('$("#sh-qa"', "QA gate in confirm handler")

    def test_standard_naming(self):
        self.assertPresent("Client_Vertical_Geo_", "standard name pattern")
        self.assertPresent('id="file_name" value=', "prefilled sheet-name field")

    def test_pacing_control(self):
        self.assertPresent('id="pacing"', "pacing select")
        self.assertPresent("pacing_preset", "pacing preset API wiring")

    def test_social_form(self):
        self.assertPresent('data-mode="social"', "Social tab")
        self.assertPresent("Est. paid cost", "estimate row")
        self.assertPresent("/api/social/estimate", "estimate fetch")

    def test_connection_transparency(self):
        self.assertPresent('id="conn-banner"', "connections banner")
        self.assertPresent("data-conn-test", "per-service Test button")
        self.assertPresent("conns-test-all", "Test-all button")
        self.assertPresent("social-budget-inr", "budget input")
        self.assertPresent("spend-line", "spend summary line")

    def test_schedules(self):
        self.assertPresent("/api/schedules", "schedules API wiring")
        self.assertPresent("Repeat on schedule", "repeat button")
        self.assertPresent("Schedules", "schedules card")
        self.assertPresent("data-sched-del", "schedule delete")

    def test_org_audit(self):
        self.assertPresent("/api/admin/org-keys", "org-keys API wiring")
        self.assertPresent("Shared keys", "org keys card")
        self.assertPresent("Recent activity", "audit card")
        self.assertPresent("org_keys", "shared-key transparency payload")

    def test_scale(self):
        self.assertPresent("proxy-save", "proxy save button")
        self.assertPresent("Monthly cap (MB)", "proxy cap input")
        self.assertPresent("Company proxy", "proxy section")


if __name__ == "__main__":
    unittest.main()
