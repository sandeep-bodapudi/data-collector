"""Phase 2 cascade tests — mocked, no network. Run: python -m unittest tests.test_phase2_cascade -v"""
import os
import sys
import tempfile as _tf
import unittest

sys.path.insert(0, r"D:\Onebridge projects\scrapper")
os.environ.setdefault("JOB_STORE_DIR", os.path.join(_tf.mkdtemp(), "jobs"))  # keep job payloads out of the real dir

from scraper import jobs as J
from scraper.config import effective_services
from scraper.connection_health import HEALTH_CACHE, status_string


def _getter(mapping):
    return lambda k, d="1": mapping.get(k, d)


class TestEffectiveServices(unittest.TestCase):
    def test_disabled_filtered(self):
        healthy = {"brave": True, "google_cse": True, "ddgs": True}
        out = effective_services(["brave", "google_cse", "ddgs"], healthy,
                                 _getter({"conn_brave": "0"}))
        self.assertEqual(out, ["google_cse", "ddgs"])

    def test_unhealthy_filtered(self):
        healthy = {"brave": False, "ddgs": True}
        out = effective_services(["brave", "ddgs"], healthy, _getter({}))
        self.assertEqual(out, ["ddgs"])


class TestPreflight(unittest.TestCase):
    def test_all_search_disabled_fails(self):
        import scraper.config as cfg

        orig = cfg.is_enabled
        cfg.is_enabled = lambda name, sg=None: False
        try:
            eff = J.preflight({"mode": "web", "brave_key": "", "custom_fields": []})
            self.assertEqual(eff["search"], [])
        finally:
            cfg.is_enabled = orig

    def test_ddgs_only_when_no_keys(self):
        import scraper.config as cfg

        orig = cfg.is_enabled
        cfg.is_enabled = lambda name, sg=None: True
        try:
            # ensure cache does not mark ddgs unhealthy
            HEALTH_CACHE.pop("ddgs", None)
            eff = J.preflight({"mode": "web", "brave_key": "", "custom_fields": []})
            self.assertIn("ddgs", eff["search"])
            self.assertNotIn("brave", eff["search"])
        finally:
            cfg.is_enabled = orig

    def test_ai_off_continues(self):
        eff = J.preflight({"mode": "web", "brave_key": "", "custom_fields": ["X"], "ai": {}})
        self.assertFalse(eff["ai_ok"])
        self.assertTrue(any("AI" in n for n in eff["notes"]))


class TestStatusString(unittest.TestCase):
    def test_off_shown(self):
        s = status_string({"brave": {"healthy": True, "error": ""}}, {"brave": False})
        self.assertIn("Brave OFF", s)

    def test_ok_and_fail(self):
        s = status_string({"brave": {"healthy": True}, "ddgs": {"healthy": False, "error": "x"}})
        self.assertIn("Brave OK", s)
        self.assertIn("DDGS FAIL", s)


class TestJobFailFast(unittest.TestCase):
    def test_web_no_search_errors(self):
        import scraper.config as cfg

        orig = cfg.is_enabled
        cfg.is_enabled = lambda name, sg=None: False
        try:
            job = J.Job({"mode": "web", "queries": ["x"], "max_results": 5, "custom_fields": [],
                         "fields": [], "platforms": ["web"], "ai": {}}, 1)
            J._run(job)
            self.assertEqual(job.status, "error")
            self.assertIn("Admin", job.error)
        finally:
            cfg.is_enabled = orig


if __name__ == "__main__":
    unittest.main()
