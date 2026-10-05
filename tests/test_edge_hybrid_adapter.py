from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from chatgpt_orchestrator.adapters import AmbiguousSubmissionError, WorkerAdapter
from chatgpt_orchestrator.edge_hybrid_adapter import EdgeHybridAdapter


class FakeAdapter(WorkerAdapter):
    def __init__(
        self,
        prefix: str,
        *,
        ready: bool = True,
        create_error: Exception | None = None,
        send_error: Exception | None = None,
        inspect_error: Exception | None = None,
        challenged: bool = False,
    ) -> None:
        self.prefix = prefix
        self.ready = ready
        self.challenged = challenged
        self.create_error = create_error
        self.send_error = send_error
        self.inspect_error = inspect_error
        self.created = 0
        self.sent: list[tuple[str, str]] = []
        self.closed: list[str] = []
        self.cancelled: list[str] = []

    def create(self, role: str) -> str:
        if self.create_error is not None:
            raise self.create_error
        self.created += 1
        return f"{self.prefix}:{role}:{self.created}"

    def send(
        self,
        session_id: str,
        prompt: str,
        *,
        cancel_event: threading.Event | None = None,
        timeout_seconds: float | None = None,
    ) -> str:
        del cancel_event, timeout_seconds
        self.sent.append((session_id, prompt))
        if self.send_error is not None:
            raise self.send_error
        return f"{self.prefix}-result:{prompt}"

    def cancel(self, session_id: str) -> bool:
        self.cancelled.append(session_id)
        return True

    def close(self, session_id: str) -> None:
        self.closed.append(session_id)

    def inspect(self, session_id: str) -> dict:
        del session_id
        if self.inspect_error is not None:
            raise self.inspect_error
        return {
            "authenticated": self.ready,
            "composer_found": self.ready and not self.challenged,
            "challenge_detected": self.challenged,
            "title": "Just a moment..." if self.challenged else "ChatGPT",
            "url": "https://chatgpt.com/",
        }

    def profile_status(self) -> dict:
        return {"ready": self.ready, "mode": self.prefix}


class EdgeHybridAdapterTests(unittest.TestCase):
    def make_adapter(
        self,
        cdp: FakeAdapter,
        fallback: FakeAdapter,
    ) -> EdgeHybridAdapter:
        return EdgeHybridAdapter(
            executable="unused",
            profile_dir="unused",
            cdp_adapter=cdp,
            fallback_adapter=fallback,
        )

    def test_create_prefers_ready_cdp(self):
        cdp = FakeAdapter("cdp", ready=True)
        fallback = FakeAdapter("uia", ready=True)
        adapter = self.make_adapter(cdp, fallback)

        session = adapter.create("solver")
        details = adapter.inspect(session)

        self.assertEqual(details["transport"], "cdp")
        self.assertTrue(details["background"])
        self.assertEqual(cdp.created, 1)
        self.assertEqual(fallback.created, 0)

    def test_create_falls_back_when_cdp_is_not_ready(self):
        cdp = FakeAdapter("cdp", ready=False)
        fallback = FakeAdapter("uia", ready=True)
        adapter = self.make_adapter(cdp, fallback)

        session = adapter.create("solver")
        details = adapter.inspect(session)

        self.assertEqual(details["transport"], "uia")
        self.assertTrue(details["fallback_active"])
        self.assertEqual(fallback.created, 1)
        self.assertEqual(len(cdp.closed), 1)

    def test_send_migrates_before_submission_when_cdp_loses_readiness(self):
        cdp = FakeAdapter("cdp", ready=True)
        fallback = FakeAdapter("uia", ready=True)
        adapter = self.make_adapter(cdp, fallback)
        session = adapter.create("solver")

        cdp.ready = False
        result = adapter.send(session, "HELLO")

        self.assertEqual(result, "uia-result:HELLO")
        self.assertEqual(cdp.sent, [])
        self.assertEqual(len(cdp.closed), 1)
        self.assertEqual(len(fallback.sent), 1)
        self.assertEqual(adapter.inspect(session)["transport"], "uia")

    def test_cdp_send_failure_is_non_retryable_and_not_replayed(self):
        cdp = FakeAdapter(
            "cdp",
            ready=True,
            send_error=RuntimeError("transport failed after submit began"),
        )
        fallback = FakeAdapter("uia", ready=True)
        adapter = self.make_adapter(cdp, fallback)
        session = adapter.create("solver")

        with self.assertRaisesRegex(
            AmbiguousSubmissionError,
            "cdp_send_ambiguous",
        ):
            adapter.send(session, "SIDE_EFFECTING_PROMPT")

        self.assertEqual(len(cdp.sent), 1)
        self.assertEqual(fallback.sent, [])
        status = adapter.profile_status()
        self.assertEqual(status["last_transition"]["code"], "cdp_send_ambiguous")

    def test_cdp_timeout_after_send_start_is_non_retryable(self):
        cdp = FakeAdapter(
            "cdp",
            ready=True,
            send_error=TimeoutError("response timeout after submit"),
        )
        fallback = FakeAdapter("uia", ready=True)
        adapter = self.make_adapter(cdp, fallback)
        session = adapter.create("solver")

        with self.assertRaises(AmbiguousSubmissionError):
            adapter.send(session, "DO_NOT_DUPLICATE")

        self.assertEqual(len(cdp.sent), 1)
        self.assertEqual(fallback.sent, [])

    def test_target_missing_before_send_migrates_to_fallback(self):
        cdp = FakeAdapter("cdp", ready=True)
        fallback = FakeAdapter("uia", ready=True)
        adapter = self.make_adapter(cdp, fallback)
        session = adapter.create("solver")

        cdp.inspect_error = RuntimeError(
            "cdp_target_missing: target is no longer available"
        )
        result = adapter.send(session, "RECOVER_BEFORE_SUBMIT")

        self.assertEqual(result, "uia-result:RECOVER_BEFORE_SUBMIT")
        self.assertEqual(cdp.sent, [])
        self.assertEqual(len(fallback.sent), 1)
        self.assertEqual(adapter.inspect(session)["transport"], "uia")

    def test_preflight_failure_and_uia_creation_failure_never_sends(self):
        cdp = FakeAdapter("cdp", ready=True)
        fallback = FakeAdapter("uia", ready=True)
        adapter = self.make_adapter(cdp, fallback)
        session = adapter.create("solver")

        cdp.ready = False
        fallback.create_error = RuntimeError("UIA creation failed")

        with self.assertRaisesRegex(RuntimeError, "all_backends_failed"):
            adapter.send(session, "NEVER_SUBMIT")

        self.assertEqual(cdp.sent, [])
        self.assertEqual(fallback.sent, [])

    def test_fallback_creation_failure_reports_all_backends_failed(self):
        cdp = FakeAdapter("cdp", ready=False)
        fallback = FakeAdapter(
            "uia",
            ready=True,
            create_error=RuntimeError("UIA unavailable"),
        )
        adapter = self.make_adapter(cdp, fallback)

        with self.assertRaisesRegex(RuntimeError, "all_backends_failed"):
            adapter.create("solver")

        status = adapter.profile_status()
        self.assertEqual(
            status["last_transition"]["code"],
            "all_backends_failed",
        )

    def test_three_concurrent_workers_keep_unique_sessions(self):
        cdp = FakeAdapter("cdp", ready=True)
        fallback = FakeAdapter("uia", ready=True)
        adapter = self.make_adapter(cdp, fallback)

        with ThreadPoolExecutor(max_workers=3) as pool:
            sessions = list(
                pool.map(adapter.create, ["solver", "critic", "implementer"])
            )

        self.assertEqual(len(set(sessions)), 3)
        self.assertEqual(cdp.created, 3)
        self.assertTrue(
            all(adapter.inspect(s)["transport"] == "cdp" for s in sessions)
        )

    def test_challenge_fails_closed_to_uia_before_send(self):
        cdp = FakeAdapter("cdp", ready=True, challenged=True)
        fallback = FakeAdapter("uia", ready=True)
        adapter = self.make_adapter(cdp, fallback)

        session = adapter.create("critic")
        details = adapter.inspect(session)

        self.assertEqual(details["transport"], "uia")
        self.assertEqual(details["last_cdp_error_code"], "cdp_challenged")
        self.assertEqual(cdp.sent, [])
        self.assertEqual(fallback.created, 1)

    def test_cdp_can_be_disabled_by_configuration(self):
        cdp = FakeAdapter("cdp", ready=True)
        fallback = FakeAdapter("uia", ready=True)
        adapter = EdgeHybridAdapter(
            executable="unused",
            profile_dir="unused",
            cdp_enabled=False,
            cdp_adapter=cdp,
            fallback_adapter=fallback,
        )

        session = adapter.create("solver")
        status = adapter.profile_status()

        self.assertEqual(adapter.inspect(session)["transport"], "uia")
        self.assertEqual(cdp.created, 0)
        self.assertFalse(status["cdp_configured"])
        self.assertEqual(status["last_cdp_error_code"], "cdp_disabled")

    def test_two_workers_keep_isolated_inner_sessions(self):
        cdp = FakeAdapter("cdp", ready=True)
        fallback = FakeAdapter("uia", ready=True)
        adapter = self.make_adapter(cdp, fallback)

        one = adapter.create("solver")
        two = adapter.create("critic")

        first = adapter.inspect(one)["inner"]["url"]
        second = adapter.inspect(two)["inner"]["url"]
        self.assertNotEqual(one, two)
        self.assertEqual(first, second)
        self.assertEqual(cdp.created, 2)

    def test_idle_status_selects_uia_when_cdp_not_ready(self):
        cdp = FakeAdapter("cdp", ready=False)
        fallback = FakeAdapter("uia", ready=True)
        adapter = self.make_adapter(cdp, fallback)

        status = adapter.profile_status()

        self.assertEqual(status["selected_backend"], "uia_fallback")
        self.assertEqual(status["effective_backend"], "uia_fallback")
        self.assertTrue(status["degraded"])
        self.assertIsNotNone(status["degraded_reason"])

    def test_fallback_transition_is_structured_and_not_duplicated(self):
        cdp = FakeAdapter("cdp", ready=False)
        fallback = FakeAdapter("uia", ready=True)
        adapter = self.make_adapter(cdp, fallback)

        session = adapter.create("solver")
        status = adapter.profile_status()
        fallback_events = [
            event for event in status["recent_transitions"]
            if event["code"] == "fallback_uia_active"
        ]

        self.assertEqual(len(fallback_events), 1)
        event = fallback_events[0]
        self.assertEqual(event["session_id"], session)
        self.assertEqual(event["from_backend"], "none")
        self.assertEqual(event["to_backend"], "uia_fallback")

    def test_soak_repeated_fallback_cycles_leave_clean_idle_state(self):
        cdp = FakeAdapter("cdp", ready=False)
        fallback = FakeAdapter("uia", ready=True)
        adapter = self.make_adapter(cdp, fallback)

        for index in range(25):
            session = adapter.create(f"worker-{index}")
            result = adapter.send(session, f"PING-{index}")
            self.assertEqual(result, f"uia-result:PING-{index}")
            adapter.close(session)

        status = adapter.profile_status()
        metrics = status["runtime_metrics"]

        self.assertTrue(status["stability"]["clean_idle"])
        self.assertEqual(status["active_cdp_sessions"], 0)
        self.assertEqual(status["active_uia_sessions"], 0)
        self.assertEqual(metrics["sessions_created_total"], 25)
        self.assertEqual(metrics["uia_fallback_sessions_total"], 25)
        self.assertEqual(metrics["sessions_closed_total"], 25)
        self.assertEqual(metrics["send_success_total"], 25)
        self.assertEqual(metrics["send_failure_total"], 0)
        self.assertEqual(metrics["cdp_preflight_failure_total"], 25)
        self.assertEqual(status["stability"]["send_success_rate"], 1.0)

    def test_runtime_metrics_count_ambiguous_send_once(self):
        cdp = FakeAdapter(
            "cdp",
            ready=True,
            send_error=RuntimeError("transport vanished after submit"),
        )
        fallback = FakeAdapter("uia", ready=True)
        adapter = self.make_adapter(cdp, fallback)
        session = adapter.create("solver")

        with self.assertRaises(AmbiguousSubmissionError):
            adapter.send(session, "ONCE_ONLY")

        adapter.close(session)
        metrics = adapter.profile_status()["runtime_metrics"]

        self.assertEqual(metrics["sessions_created_total"], 1)
        self.assertEqual(metrics["cdp_sessions_total"], 1)
        self.assertEqual(metrics["sessions_closed_total"], 1)
        self.assertEqual(metrics["send_success_total"], 0)
        self.assertEqual(metrics["send_failure_total"], 1)
        self.assertEqual(metrics["ambiguous_send_failure_total"], 1)

    def test_close_is_idempotent_for_metrics(self):
        cdp = FakeAdapter("cdp", ready=True)
        fallback = FakeAdapter("uia", ready=True)
        adapter = self.make_adapter(cdp, fallback)
        session = adapter.create("solver")

        adapter.close(session)
        adapter.close(session)

        metrics = adapter.profile_status()["runtime_metrics"]
        self.assertEqual(metrics["sessions_closed_total"], 1)
        self.assertTrue(adapter.profile_status()["stability"]["clean_idle"])

    def test_profile_status_exposes_primary_and_fallback(self):
        cdp = FakeAdapter("cdp", ready=False)
        fallback = FakeAdapter("uia", ready=True)
        adapter = self.make_adapter(cdp, fallback)

        status = adapter.profile_status()

        self.assertEqual(status["mode"], "hybrid_background_first")
        self.assertEqual(status["primary"], "cdp_background")
        self.assertEqual(status["fallback"], "shared_tabs_uia")
        self.assertFalse(status["cdp_ready"])
        self.assertFalse(status["replay_on_cdp_send_failure"])


if __name__ == "__main__":
    unittest.main()
