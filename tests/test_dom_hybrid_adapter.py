from __future__ import annotations

import time
import unittest

from chatgpt_orchestrator.actuator_dom_adapter import (
    ActuatorDOMAdapter,
    DOMPreSubmissionError,
)
from chatgpt_orchestrator.adapters import AmbiguousSubmissionError, WorkerAdapter
from chatgpt_orchestrator.dom_hybrid_adapter import DOMUIAHybridAdapter


class FakeAdapter(WorkerAdapter):
    def __init__(self, *, prefix: str, send_result: str = "OK", send_error=None):
        self.prefix = prefix
        self.send_result = send_result
        self.send_error = send_error
        self.created = []
        self.sent = []
        self.closed = []

    def create(self, role: str) -> str:
        session = f"{self.prefix}:{len(self.created)}"
        self.created.append((role, session))
        return session

    def send(
        self,
        session_id: str,
        prompt: str,
        *,
        cancel_event=None,
        timeout_seconds=None,
    ) -> str:
        self.sent.append((session_id, prompt))
        if self.send_error is not None:
            raise self.send_error
        return self.send_result

    def close(self, session_id: str) -> None:
        self.closed.append(session_id)

    def cancel(self, session_id: str) -> bool:
        return True

    def profile_status(self):
        return {"ready": True, "mode": self.prefix}


class ActuatorDOMAdapterTests(unittest.TestCase):
    def test_extract_response_uses_latest_assistant_turn_and_strips_mode(self):
        current = (
            "Chia sẻ\nBạn đã nói:\nPROMPT\n"
            "Đã xử lý trong 2s\nChatGPT đã nói:\n\n"
            "ORCH_DOM_OK\n\nCao"
        )
        self.assertEqual(
            ActuatorDOMAdapter._extract_response(current, "", "PROMPT"),
            "ORCH_DOM_OK",
        )

    def test_extract_response_supports_english_accessibility_label(self):
        current = (
            "You said:\nPROMPT\nChatGPT said:\n"
            "First line\nSecond line\nHigh"
        )
        self.assertEqual(
            ActuatorDOMAdapter._extract_response(current, "", "PROMPT"),
            "First line\nSecond line",
        )

    def test_extract_response_strips_guest_footer(self):
        current = (
            "PROMPT\nORCH_BG_EDGE_OK\n\n"
            "ChatGPT is AI and can make mistakes.\n\n"
            "Chat with ChatGPT"
        )
        self.assertEqual(
            ActuatorDOMAdapter._extract_response(current, "", "PROMPT"),
            "ORCH_BG_EDGE_OK",
        )


    def test_extract_response_detects_new_assistant_turn_when_prompt_whitespace_changes(self):
        baseline = (
            "ChatGPT\nNew chat\nLê Đăng Nam\nPlus\n"
        )
        prompt = (
            "You are an independent worker delegated by ChatGPT MAIN.\n"
            "ROLE: solver\n"
            "TASK GOAL:\n"
            "Reply exactly with: ORCH_BG_AUTOCONNECT_OK"
        )
        current = (
            baseline
            + "You said:\n"
            + "You are an independent worker delegated by ChatGPT MAIN. "
            + "ROLE: solver TASK GOAL: Reply exactly with: "
            + "ORCH_BG_AUTOCONNECT_OK\n"
            + "Show more\n"
            + "ChatGPT said:\n\n"
            + "ORCH_BG_AUTOCONNECT_OK\n\n"
            + "ChatGPT can make mistakes. Check important info.\n"
            + "Latest response\n"
            + "High\n"
            + "Response complete\n"
            + "Response complete"
        )
        self.assertEqual(
            ActuatorDOMAdapter._extract_response(current, baseline, prompt),
            "ORCH_BG_AUTOCONNECT_OK",
        )

    def test_extract_response_does_not_reuse_stale_assistant_turn(self):
        baseline = (
            "You said:\nOLD\nChatGPT said:\nOLD_REPLY\n"
            "Response complete"
        )
        current = baseline + "\nSidebar changed"
        self.assertEqual(
            ActuatorDOMAdapter._extract_response(
                current,
                baseline,
                "NEW PROMPT",
            ),
            "",
        )


    def test_tab_pool_refills_in_background_before_first_session(self):
        class Broker:
            def __init__(self):
                self.tabs = []
                self.calls = []

            def call(self, action, **kwargs):
                self.calls.append((action, kwargs))
                if action == "list_tabs":
                    return {"tabs": list(self.tabs)}
                if action == "ensure_tab_pool":
                    self.tabs = [
                        {
                            "tab_ref": f"tab-{i}",
                            "url": "https://chatgpt.com/",
                        }
                        for i in range(kwargs["count"])
                    ]
                    return {
                        "created": kwargs["count"],
                        "available": kwargs["count"],
                    }
                raise AssertionError(action)

            def health(self):
                return {"status": "ok"}

        class Adapter(ActuatorDOMAdapter):
            def _prepare_pooled_tab(self, tab_ref):
                return None

            def _wait_for_composer(self, tab_ref, timeout_seconds):
                return "composer-ref"

        broker = Broker()
        adapter = Adapter(
            broker=broker,
            tab_pool_only=True,
            tab_pool_size=3,
        )
        session = adapter.create("solver")
        self.assertTrue(session.startswith("actdom:"))
        self.assertEqual(len(broker.tabs), 3)
        self.assertTrue(any(call[0] == "ensure_tab_pool" for call in broker.calls))
        adapter.close(session)

    def test_idle_shutdown_runs_only_after_last_session_closes(self):
        class Broker:
            def __init__(self):
                self.shutdown_calls = 0

            def call(self, action, **kwargs):
                if action == "list_tabs":
                    return {
                        "tabs": [
                            {
                                "tab_ref": "tab-0",
                                "url": "https://chatgpt.com/",
                            }
                        ]
                    }
                if action == "shutdown_worker_edge":
                    self.shutdown_calls += 1
                    return {"stopped": True}
                raise AssertionError(action)

            def health(self):
                return {"status": "ok"}

        class Adapter(ActuatorDOMAdapter):
            def _prepare_pooled_tab(self, tab_ref):
                return None

            def _wait_for_composer(self, tab_ref, timeout_seconds):
                return "composer-ref"

        broker = Broker()
        adapter = Adapter(
            broker=broker,
            tab_pool_only=True,
            tab_pool_size=1,
            idle_shutdown_seconds=0.05,
        )
        session = adapter.create("solver")
        adapter.close(session)
        time.sleep(0.12)
        self.assertEqual(broker.shutdown_calls, 1)
        self.assertEqual(adapter._idle_shutdown_count, 1)

    def test_new_session_cancels_pending_idle_shutdown(self):
        class Broker:
            def __init__(self):
                self.shutdown_calls = 0

            def call(self, action, **kwargs):
                if action == "list_tabs":
                    return {
                        "tabs": [
                            {
                                "tab_ref": "tab-0",
                                "url": "https://chatgpt.com/",
                            }
                        ]
                    }
                if action == "shutdown_worker_edge":
                    self.shutdown_calls += 1
                    return {"stopped": True}
                raise AssertionError(action)

            def health(self):
                return {"status": "ok"}

        class Adapter(ActuatorDOMAdapter):
            def _prepare_pooled_tab(self, tab_ref):
                return None

            def _wait_for_composer(self, tab_ref, timeout_seconds):
                return "composer-ref"

        broker = Broker()
        adapter = Adapter(
            broker=broker,
            tab_pool_only=True,
            tab_pool_size=1,
            idle_shutdown_seconds=0.08,
        )
        first = adapter.create("solver")
        adapter.close(first)
        time.sleep(0.02)
        second = adapter.create("critic")
        time.sleep(0.10)
        self.assertEqual(broker.shutdown_calls, 0)
        adapter.close(second)

    def test_resolve_preexisting_new_chat_dialog_before_reset(self):
        class Broker:
            def __init__(self):
                self.dialog_open = True
                self.clicked = []

            def call(self, action, **kwargs):
                if action == "click":
                    self.clicked.append(kwargs["element_ref"])
                    if kwargs["element_ref"] == "clear-ref":
                        self.dialog_open = False
                    return {}
                raise AssertionError(action)

        class Adapter(ActuatorDOMAdapter):
            def _new_chat_dialog(self, tab_ref):
                return "dialog-ref" if self.broker.dialog_open else None

            def _find_first(self, tab_ref, specs):
                names = {
                    str(x.get("text") or x.get("name") or "")
                    for x in specs
                }
                return "clear-ref" if "Clear chat" in names else None

        broker = Broker()
        adapter = Adapter(broker=broker)
        handled = adapter._resolve_new_chat_dialog(
            "tab-ref",
            prefer_clear=True,
            timeout_seconds=0.5,
        )
        self.assertTrue(handled)
        self.assertEqual(broker.clicked, ["clear-ref"])
        self.assertFalse(broker.dialog_open)

    def test_composer_verification_retries_after_hydration_clears_prompt(self):
        class Broker:
            def __init__(self):
                self.set_calls = 0
                self.reads = []

            def call(self, action, **kwargs):
                if action == "set_value":
                    self.set_calls += 1
                    return {}
                if action == "details":
                    # First fill looks correct once, then hydration clears it.
                    # The second fill remains stable for two reads.
                    sequence = ["PROMPT", "", "PROMPT", "PROMPT"]
                    value = sequence[min(len(self.reads), len(sequence) - 1)]
                    self.reads.append(value)
                    return {"value": value}
                raise AssertionError(action)

        class Adapter(ActuatorDOMAdapter):
            def _wait_for_composer(self, tab_ref, timeout_seconds):
                return "composer-ref"

            def _new_chat_dialog(self, tab_ref):
                return None

        broker = Broker()
        adapter = Adapter(broker=broker)
        ref = adapter._set_and_verify_composer(
            "tab-ref",
            "composer-ref",
            "PROMPT",
            10.0,
        )
        self.assertEqual(ref, "composer-ref")
        self.assertEqual(broker.set_calls, 2)
        self.assertEqual(broker.reads[-2:], ["PROMPT", "PROMPT"])


class DOMUIAHybridAdapterTests(unittest.TestCase):
    def make_adapter(self, dom, uia, *, uia_fallback_enabled=True):
        return DOMUIAHybridAdapter(
            executable="unused.exe",
            dom_adapter=dom,
            fallback_adapter=uia,
            uia_fallback_enabled=uia_fallback_enabled,
        )

    def test_pre_submission_dom_failure_migrates_once_to_uia(self):
        dom = FakeAdapter(
            prefix="dom",
            send_error=DOMPreSubmissionError("composer unavailable"),
        )
        uia = FakeAdapter(prefix="uia", send_result="UIA_OK")
        adapter = self.make_adapter(dom, uia)

        session = adapter.create("solver")
        result = adapter.send(session, "PROMPT")

        self.assertEqual(result, "UIA_OK")
        self.assertEqual(len(dom.sent), 1)
        self.assertEqual(len(uia.sent), 1)
        self.assertEqual(len(uia.created), 1)
        self.assertEqual(len(dom.closed), 1)
        self.assertEqual(adapter.inspect(session)["transport"], "uia")

    def test_ambiguous_dom_failure_never_replays_through_uia(self):
        dom = FakeAdapter(
            prefix="dom",
            send_error=AmbiguousSubmissionError("maybe submitted"),
        )
        uia = FakeAdapter(prefix="uia")
        adapter = self.make_adapter(dom, uia)

        session = adapter.create("solver")
        with self.assertRaises(AmbiguousSubmissionError):
            adapter.send(session, "PROMPT")

        self.assertEqual(len(dom.sent), 1)
        self.assertEqual(len(uia.sent), 0)
        self.assertEqual(len(uia.created), 0)

    def test_create_falls_back_when_dom_broker_is_unavailable(self):
        class CreateFailAdapter(FakeAdapter):
            def create(self, role: str) -> str:
                raise RuntimeError("broker down")

        dom = CreateFailAdapter(prefix="dom")
        uia = FakeAdapter(prefix="uia")
        adapter = self.make_adapter(dom, uia)

        session = adapter.create("critic")

        self.assertEqual(adapter.inspect(session)["transport"], "uia")
        self.assertEqual(len(uia.created), 1)

    def test_strict_background_create_never_falls_back_to_uia(self):
        class CreateFailAdapter(FakeAdapter):
            def create(self, role: str) -> str:
                raise RuntimeError("broker down")

        dom = CreateFailAdapter(prefix="dom")
        uia = FakeAdapter(prefix="uia")
        adapter = self.make_adapter(
            dom,
            uia,
            uia_fallback_enabled=False,
        )

        with self.assertRaisesRegex(RuntimeError, "dom_primary_required"):
            adapter.create("solver")

        self.assertEqual(len(uia.created), 0)
        status = adapter.profile_status()
        self.assertFalse(status["uia_fallback_enabled"])
        self.assertFalse(status["uia_fallback_available"])
        self.assertEqual(status["fallback"], "disabled")

    def test_strict_background_send_never_falls_back_to_uia(self):
        dom = FakeAdapter(
            prefix="dom",
            send_error=DOMPreSubmissionError("composer unavailable"),
        )
        uia = FakeAdapter(prefix="uia")
        adapter = self.make_adapter(
            dom,
            uia,
            uia_fallback_enabled=False,
        )

        session = adapter.create("solver")
        with self.assertRaises(DOMPreSubmissionError):
            adapter.send(session, "PROMPT")

        self.assertEqual(len(dom.sent), 1)
        self.assertEqual(len(uia.sent), 0)
        self.assertEqual(len(uia.created), 0)
        metrics = adapter.profile_status()["runtime_metrics"]
        self.assertEqual(metrics["uia_fallback_blocked_total"], 1)


if __name__ == "__main__":
    unittest.main()
