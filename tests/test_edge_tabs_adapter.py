from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from chatgpt_orchestrator.adapters import AmbiguousSubmissionError
from chatgpt_orchestrator.edge_tabs_adapter import EdgeSharedTabsAdapter


class EdgeSharedTabsAdapterTests(unittest.TestCase):
    def make_fake_adapter(self) -> EdgeSharedTabsAdapter:
        adapter = object.__new__(EdgeSharedTabsAdapter)
        adapter._ui_lock = threading.RLock()
        adapter._send_lock = threading.RLock()
        adapter.chat_url = "https://chatgpt.com/"
        adapter._sessions = {}
        adapter._window_sessions = {}
        adapter._shared_hwnd = None
        adapter._free_slots = set()
        adapter._activate = lambda _session_id: 1
        adapter._helper = SimpleNamespace(
            _get_url=lambda _hwnd: "https://chatgpt.com/",
            _wait_for_composer=lambda _hwnd, timeout_seconds: object(),
            _wait_for_prompt_echo=lambda _hwnd, _prompt, timeout_seconds: True,
        )
        adapter._set_value = lambda _element, _value: None
        adapter._find_send_button = lambda _hwnd: object()
        adapter._invoke = lambda _element: None
        return adapter

    def test_concurrent_sends_are_serialized_for_shared_uia_window(self):
        adapter = self.make_fake_adapter()
        state_lock = threading.Lock()
        active = 0
        peak = 0

        def wait_for_response(
            session_id,
            prompt,
            *,
            cancel_event,
            timeout_seconds,
        ):
            nonlocal active, peak
            del prompt, cancel_event, timeout_seconds
            with state_lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.06)
            with state_lock:
                active -= 1
            return f"done:{session_id}"

        adapter._wait_for_response = wait_for_response

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [
                pool.submit(adapter.send, f"session-{index}", f"prompt-{index}")
                for index in range(3)
            ]
            results = [future.result(timeout=2) for future in futures]

        self.assertEqual(peak, 1)
        self.assertEqual(
            results,
            ["done:session-0", "done:session-1", "done:session-2"],
        )

    def test_wait_for_send_button_retries_until_available(self):
        adapter = self.make_fake_adapter()
        calls = {"count": 0}
        expected = object()

        def find(_hwnd):
            calls["count"] += 1
            if calls["count"] < 3:
                return None
            return expected

        adapter._find_send_button = find
        result = adapter._wait_for_send_button(1, timeout_seconds=1.0)

        self.assertIs(result, expected)
        self.assertGreaterEqual(calls["count"], 3)

    def test_invoke_failure_without_submission_evidence_is_non_retryable(self):
        adapter = self.make_fake_adapter()
        adapter._invoke = lambda _element: (_ for _ in ()).throw(
            RuntimeError("invoke failed")
        )
        adapter._submission_observed_after_invoke_error = (
            lambda *_args, **_kwargs: False
        )

        with self.assertRaisesRegex(AmbiguousSubmissionError, "uia_send_ambiguous"):
            adapter.send("session-1", "prompt")

    def test_invoke_failure_with_submission_evidence_continues_without_replay(self):
        adapter = self.make_fake_adapter()
        calls = {"invoke": 0}

        def invoke(_element):
            calls["invoke"] += 1
            raise RuntimeError("provider returned COM failure after dispatch")

        adapter._invoke = invoke
        adapter._submission_observed_after_invoke_error = (
            lambda *_args, **_kwargs: True
        )
        adapter._wait_for_response = (
            lambda *_args, **_kwargs: "done-after-observed-submit"
        )

        result = adapter.send("session-1", "prompt")

        self.assertEqual(result, "done-after-observed-submit")
        self.assertEqual(calls["invoke"], 1)

    def test_submission_observer_accepts_cleared_composer(self):
        adapter = self.make_fake_adapter()
        adapter._helper._wait_for_prompt_echo = (
            lambda _hwnd, _prompt, timeout_seconds: False
        )
        composer = SimpleNamespace(get_value=lambda: "")

        self.assertTrue(
            adapter._submission_observed_after_invoke_error(
                1,
                composer,
                "prompt",
                timeout_seconds=0.1,
            )
        )

    def test_response_timeout_after_submit_is_non_retryable(self):
        adapter = self.make_fake_adapter()
        adapter._wait_for_response = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            TimeoutError("response timeout")
        )

        with self.assertRaisesRegex(AmbiguousSubmissionError, "uia_send_ambiguous"):
            adapter.send("session-1", "prompt")

    def test_missing_initial_prompt_echo_still_waits_for_response(self):
        adapter = self.make_fake_adapter()
        adapter._helper._wait_for_prompt_echo = (
            lambda _hwnd, _prompt, timeout_seconds: False
        )
        adapter._wait_for_response = (
            lambda *_args, **_kwargs: "done-after-late-echo"
        )

        result = adapter.send("session-1", "prompt")

        self.assertEqual(result, "done-after-late-echo")

    def test_tab_items_deduplicates_duplicate_edge_nodes(self):
        adapter = self.make_fake_adapter()

        class Rect:
            def __init__(self, left, top, right, bottom):
                self.left = left
                self.top = top
                self.right = right
                self.bottom = bottom

            def width(self):
                return self.right - self.left

            def height(self):
                return self.bottom - self.top

        class Node:
            def __init__(self, name, rect):
                self.element_info = SimpleNamespace(
                    class_name="EdgeTab",
                    name=name,
                )
                self._rect = rect

            def rectangle(self):
                return self._rect

        first = Node("A", Rect(0, 100, 50, 140))
        first_duplicate = Node("A duplicate", Rect(0, 100, 50, 140))
        second = Node("B", Rect(0, 145, 50, 185))
        hidden = Node("Hidden", Rect(0, 0, 0, 0))
        window = SimpleNamespace(
            descendants=lambda **_kwargs: [
                first,
                first_duplicate,
                second,
                hidden,
            ]
        )
        adapter._helper = SimpleNamespace(_uia_window=lambda _hwnd: window)

        items = adapter._tab_items(1)

        self.assertEqual(items, [first, second])

    def test_create_uses_dedicated_window_when_tabitems_are_missing(self):
        adapter = self.make_fake_adapter()
        adapter._create_shared_window = lambda: 123
        adapter._tab_items = lambda _hwnd: []

        session_id = adapter.create("solver")
        token = session_id.split(":", 1)[1]

        self.assertEqual(adapter._sessions[token], 0)
        self.assertEqual(adapter._window_sessions[token], 123)
        self.assertIsNone(adapter._shared_hwnd)
    def test_activate_migrates_to_dedicated_window_when_tab_slot_disappears(self):
        adapter = self.make_fake_adapter()
        adapter._activate = EdgeSharedTabsAdapter._activate.__get__(adapter, EdgeSharedTabsAdapter)
        adapter._shared_hwnd = 123
        adapter._sessions = {"abc": 1}
        adapter._window_sessions = {}
        adapter._activate_slot = lambda _hwnd, _slot: (_ for _ in ()).throw(
            RuntimeError("Worker tab slot 1 is unavailable; Edge currently exposes 0 tabs.")
        )
        adapter._create_shared_window = lambda: 456

        with patch(
            "chatgpt_orchestrator.edge_tabs_adapter.win32gui.IsWindow",
            return_value=True,
        ):
            hwnd = adapter._activate("edgetab:abc")

        self.assertEqual(hwnd, 456)
        self.assertEqual(adapter._sessions["abc"], 0)
        self.assertEqual(adapter._window_sessions["abc"], 456)
    def test_profile_status_reports_serialized_single_lane(self):
        adapter = self.make_fake_adapter()
        adapter._sessions = {"a": 1, "b": 2}

        status = adapter.profile_status()

        self.assertTrue(status["ready"])
        self.assertTrue(status["serialized_sends"])
        self.assertEqual(status["max_parallel_sends"], 1)
        self.assertEqual(status["active_sessions"], 2)
        self.assertFalse(status["window_ready"])


if __name__ == "__main__":
    unittest.main()
