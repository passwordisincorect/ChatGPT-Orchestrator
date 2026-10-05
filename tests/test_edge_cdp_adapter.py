from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from chatgpt_orchestrator.adapters import SimulatedAdapter
from chatgpt_orchestrator.edge_cdp_adapter import EdgeCDPAdapter
from chatgpt_orchestrator.models import Settings
from chatgpt_orchestrator.orchestrator import Orchestrator
from chatgpt_orchestrator.store import Store


class EdgeCDPAdapterTests(unittest.TestCase):
    def test_parse_cdp_session(self):
        self.assertEqual(
            EdgeCDPAdapter._parse_session("cdp:ABC123"),
            "ABC123",
        )
        self.assertEqual(
            EdgeCDPAdapter._parse_session("cdp:OWNER123:ABC123"),
            "ABC123",
        )
        with self.assertRaises(ValueError):
            EdgeCDPAdapter._parse_session("edge:ABC123")
        with self.assertRaises(ValueError):
            EdgeCDPAdapter._parse_session("cdp:")

    def test_unknown_session_is_rejected_by_ownership_guard(self):
        adapter = object.__new__(EdgeCDPAdapter)
        adapter._browser_lock = threading.RLock()
        adapter._session_owners = {}
        with self.assertRaisesRegex(RuntimeError, "cdp_target_not_owned"):
            adapter._require_owned_session("cdp:owner:TARGET")

    def test_reusable_target_pool_only_returns_healthy_marked_target(self):
        adapter = object.__new__(EdgeCDPAdapter)
        adapter._browser_lock = threading.RLock()
        adapter._free_targets = ["TARGET"]
        adapter._target_ws_url = lambda _target: "ws://example"
        adapter._evaluate = lambda _sid, _expr: adapter._free_marker("TARGET")
        adapter._snapshot = lambda _sid: {
            "challenge_detected": False,
            "auth_required": False,
            "composer_found": True,
        }
        adapter._close_target_best_effort = lambda _target: None

        self.assertEqual(adapter._take_reusable_target(), "TARGET")
        self.assertEqual(adapter._free_targets, [])

    def test_profile_status_distinguishes_no_chat_page_from_signed_out(self):
        adapter = object.__new__(EdgeCDPAdapter)
        adapter._browser_lock = threading.RLock()
        adapter._port = 9222
        adapter.profile_dir = Path("profile")
        adapter._session_owners = {}
        adapter._free_targets = []
        adapter._ensure_browser = lambda: None
        adapter._targets = lambda: [
            {"id": "BLANK", "type": "page", "url": "about:blank"}
        ]
        adapter._probe_port = lambda _port: True

        status = adapter.profile_status()

        self.assertTrue(status["endpoint_reachable"])
        self.assertEqual(status["chat_page_count"], 0)
        self.assertFalse(status["authenticated"])
        self.assertEqual(status["failure_code"], "cdp_no_chat_page")

    def test_wait_for_composer_rejects_signed_out_profile(self):
        adapter = object.__new__(EdgeCDPAdapter)
        adapter._snapshot = lambda _sid: {
            "auth_required": True,
            "challenge_detected": False,
            "composer_found": True,
            "url": "https://chatgpt.com/",
        }
        with self.assertRaisesRegex(RuntimeError, "not signed in"):
            adapter._wait_for_composer("cdp:test", 0.1)

    def test_wait_for_composer_rejects_challenge(self):
        adapter = object.__new__(EdgeCDPAdapter)
        adapter._snapshot = lambda _sid: {
            "auth_required": False,
            "challenge_detected": True,
            "composer_found": False,
            "url": "https://chatgpt.com/",
        }
        with self.assertRaisesRegex(RuntimeError, "CDP_CHALLENGED"):
            adapter._wait_for_composer("cdp:test", 0.1)

    def test_orchestrator_info_reports_v070_capabilities(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / "info.db")
        settings = Settings("simulated", 3, db)
        core = Orchestrator(settings, Store(db), SimulatedAdapter())
        info = core.orchestrator_info()
        self.assertEqual(info["version"], "0.8.0")
        self.assertEqual(info["backend"], "simulated")
        self.assertTrue(info["capabilities"]["autonomous_planning"])
        self.assertTrue(info["capabilities"]["review_rework_loop"])
        self.assertTrue(info["capabilities"]["safe_restart_recovery"])
        self.assertTrue(info["capabilities"]["soak_test_support"])
        self.assertTrue(info["capabilities"]["persistent_project_state"])
        self.assertTrue(info["capabilities"]["project_context_injection"])
        self.assertTrue(info["capabilities"]["project_outcome_snapshots"])
        self.assertTrue(info["capabilities"]["optimistic_project_updates"])
        self.assertTrue(
            info["capabilities"]["non_retryable_ambiguous_submissions"]
        )
        self.assertFalse(info["capabilities"]["cdp_background"])


if __name__ == "__main__":
    unittest.main()
