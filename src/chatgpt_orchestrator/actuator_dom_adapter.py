from __future__ import annotations

import json
from pathlib import Path
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from .adapters import AmbiguousSubmissionError, WorkerAdapter


class DOMPreSubmissionError(RuntimeError):
    """DOM failed before any submission attempt, so UIA fallback is safe."""


class DOMBrokerClient:
    def __init__(
        self,
        endpoint: str = "http://127.0.0.1:8765/api/dom",
        token_file: str = r"D:\MCP-Test\.chatgpt-dom-broker-token",
        timeout_seconds: float = 40.0,
    ) -> None:
        self.endpoint = str(endpoint).rstrip("/")
        self.token_file = Path(token_file)
        self.timeout_seconds = float(timeout_seconds)

    def _token(self) -> str:
        try:
            token = self.token_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Actuator DOM broker token file not found: {self.token_file}"
            ) from exc
        if not token:
            raise RuntimeError("Actuator DOM broker token is empty.")
        return token

    def call(self, action: str, **args) -> dict:
        payload = json.dumps(
            {"action": str(action), "args": args},
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(
            self.endpoint,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
                "X-Actuator-DOM-Token": self._token(),
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                body = json.loads(raw)
                detail = str(body.get("error") or raw)
            except Exception:
                detail = raw
            raise RuntimeError(
                f"Actuator DOM broker HTTP {exc.code}: {detail}"
            ) from exc
        except URLError as exc:
            raise RuntimeError(
                f"Actuator DOM broker is unavailable: {exc.reason}"
            ) from exc

        if not isinstance(body, dict) or not body.get("ok"):
            raise RuntimeError(
                str(body.get("error") if isinstance(body, dict) else body)
            )
        result = body.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("Actuator DOM broker returned an invalid result.")
        return result

    def health(self) -> dict:
        endpoint = self.endpoint.rsplit("/api/dom", 1)[0] + "/dom/healthz"
        request = Request(endpoint, method="GET", headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=min(self.timeout_seconds, 3.0)) as response:
                value = json.loads(response.read().decode("utf-8"))
            return dict(value) if isinstance(value, dict) else {"status": "invalid"}
        except Exception as exc:
            return {
                "status": "unavailable",
                "error": f"{type(exc).__name__}: {exc}",
            }


class ActuatorDOMAdapter(WorkerAdapter):
    """ChatGPT worker transport using the Actuator-owned shared DOM/CDP broker."""

    def __init__(
        self,
        chat_url: str = "https://chatgpt.com/",
        create_timeout_seconds: float = 20.0,
        send_timeout_seconds: float = 120.0,
        stable_seconds: float = 3.0,
        *,
        broker_endpoint: str = "http://127.0.0.1:8765/api/dom",
        broker_token_file: str = r"D:\MCP-Test\.chatgpt-dom-broker-token",
        broker: DOMBrokerClient | None = None,
        tab_pool_only: bool = False,
        tab_pool_size: int = 5,
        idle_shutdown_seconds: float = 0.0,
    ) -> None:
        self.chat_url = str(chat_url)
        self.create_timeout_seconds = float(create_timeout_seconds)
        self.send_timeout_seconds = float(send_timeout_seconds)
        self.stable_seconds = max(0.2, float(stable_seconds))
        self.broker = broker or DOMBrokerClient(
            endpoint=broker_endpoint,
            token_file=broker_token_file,
            timeout_seconds=max(self.send_timeout_seconds, 40.0),
        )
        self._lock = threading.RLock()
        self._sessions: dict[str, dict[str, str]] = {}
        self.tab_pool_only = bool(tab_pool_only)
        self.tab_pool_size = min(max(1, int(tab_pool_size)), 20)
        self.idle_shutdown_seconds = max(0.0, float(idle_shutdown_seconds))
        self._idle_shutdown_timer: threading.Timer | None = None
        self._idle_shutdown_count = 0
        self._last_idle_shutdown_error: str | None = None
        self._last_idle_shutdown_result: dict[str, Any] | None = None

    def create(self, role: str) -> str:
        token = uuid4().hex[:16]
        session_id = f"actdom:{token}"

        if self.tab_pool_only:
            with self._lock:
                self._cancel_idle_shutdown_locked()
                used = {
                    str(route.get("tab_ref") or "")
                    for route in self._sessions.values()
                }
                listed = self.broker.call("list_tabs")
                tabs = listed.get("tabs") or []
                pool_tabs = [
                    tab
                    for tab in tabs
                    if str(tab.get("url") or "").startswith("https://chatgpt.com")
                ]
                if len(pool_tabs) < self.tab_pool_size:
                    self.broker.call(
                        "ensure_tab_pool",
                        url=self.chat_url,
                        count=self.tab_pool_size,
                        timeout_ms=max(
                            1000,
                            int(self.create_timeout_seconds * 1000),
                        ),
                    )
                    listed = self.broker.call("list_tabs")
                    tabs = listed.get("tabs") or []
                    pool_tabs = [
                        tab
                        for tab in tabs
                        if str(tab.get("url") or "").startswith("https://chatgpt.com")
                    ]

                tab_ref = ""
                for tab in pool_tabs:
                    candidate = str(tab.get("tab_ref") or "")
                    if candidate and candidate not in used:
                        tab_ref = candidate
                        break
                if not tab_ref:
                    raise RuntimeError(
                        "dom_tab_pool_exhausted: no free ChatGPT worker tab "
                        "is available after background pool refill."
                    )
                self._sessions[token] = {
                    "role": str(role),
                    "tab_ref": tab_ref,
                    "pooled": "true",
                }

            try:
                self._prepare_pooled_tab(tab_ref)
                self._wait_for_composer(tab_ref, self.create_timeout_seconds)
            except Exception:
                with self._lock:
                    self._sessions.pop(token, None)
                    if not self._sessions:
                        self._schedule_idle_shutdown_locked()
                raise
            return session_id

        opened = self.broker.call(
            "open_tab",
            url=self.chat_url,
            timeout_ms=max(1000, int(self.create_timeout_seconds * 1000)),
        )
        tab_ref = str(opened.get("tab_ref") or "")
        if not tab_ref:
            raise RuntimeError("DOM broker did not return a tab_ref.")

        try:
            self._wait_for_composer(tab_ref, self.create_timeout_seconds)
        except Exception:
            try:
                self.broker.call("close_tab", tab_ref=tab_ref)
            except Exception:
                pass
            raise

        with self._lock:
            self._sessions[token] = {
                "role": str(role),
                "tab_ref": tab_ref,
                "pooled": "false",
            }
        return session_id

    def send(
        self,
        session_id: str,
        prompt: str,
        *,
        cancel_event: threading.Event | None = None,
        timeout_seconds: float | None = None,
    ) -> str:
        route = self._route(session_id)
        tab_ref = route["tab_ref"]
        timeout = (
            self.send_timeout_seconds
            if timeout_seconds is None
            else max(0.1, float(timeout_seconds))
        )

        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("Worker job was cancelled.")

        try:
            baseline = self._main_text(tab_ref)
            composer = self._wait_for_composer(
                tab_ref,
                min(self.create_timeout_seconds, timeout),
            )
            composer = self._set_and_verify_composer(
                tab_ref,
                composer,
                prompt,
                timeout,
            )
            if cancel_event is not None and cancel_event.is_set():
                try:
                    self.broker.call(
                        "set_value",
                        element_ref=composer,
                        value="",
                        timeout_ms=3000,
                    )
                except Exception:
                    pass
                raise InterruptedError("Worker job was cancelled.")
            send_button = self._wait_for_send_button(
                tab_ref,
                min(8.0, timeout),
            )
        except InterruptedError:
            raise
        except DOMPreSubmissionError:
            raise
        except Exception as exc:
            raise DOMPreSubmissionError(
                f"DOM failed before submission: {type(exc).__name__}: {exc}"
            ) from exc

        try:
            self.broker.call(
                "click",
                element_ref=send_button,
                timeout_ms=max(1000, int(min(timeout, 30.0) * 1000)),
            )
        except Exception as exc:
            if not self._submission_observed(tab_ref, composer, prompt, baseline):
                raise AmbiguousSubmissionError(
                    "dom_send_ambiguous: DOM click raised and submission could "
                    "not be confirmed; automatic retry/replay is disabled. "
                    f"{type(exc).__name__}: {exc}"
                ) from exc

        try:
            return self._wait_for_response(
                tab_ref,
                prompt,
                baseline,
                timeout,
                cancel_event=cancel_event,
            )
        except InterruptedError:
            raise
        except AmbiguousSubmissionError:
            raise
        except Exception as exc:
            raise AmbiguousSubmissionError(
                "dom_response_ambiguous: prompt was submitted but response "
                "collection failed; automatic replay is disabled. "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    def cancel(self, session_id: str) -> bool:
        route = self._route(session_id)
        button = self._find_first(
            route["tab_ref"],
            [
                {"selector": "button[data-testid='stop-button']"},
                {"selector": "button[aria-label='Dừng tạo']"},
                {"selector": "button[aria-label='Stop generating']"},
                {"role": "button", "name": "Dừng tạo", "exact": True},
                {"role": "button", "name": "Stop generating", "exact": True},
            ],
        )
        if not button:
            return False
        try:
            self.broker.call("click", element_ref=button, timeout_ms=3000)
            return True
        except Exception:
            return False

    def close(self, session_id: str) -> None:
        token = self._parse_session(session_id)
        with self._lock:
            route = self._sessions.pop(token, None)
        if route is None:
            return
        if route.get("pooled") != "true":
            try:
                self.broker.call("close_tab", tab_ref=route["tab_ref"])
            except Exception:
                pass
        with self._lock:
            if not self._sessions:
                self._schedule_idle_shutdown_locked()

    def _cancel_idle_shutdown_locked(self) -> None:
        timer = self._idle_shutdown_timer
        self._idle_shutdown_timer = None
        if timer is not None:
            timer.cancel()

    def _schedule_idle_shutdown_locked(self) -> None:
        self._cancel_idle_shutdown_locked()
        if self.idle_shutdown_seconds <= 0:
            return
        timer = threading.Timer(
            self.idle_shutdown_seconds,
            self._shutdown_if_idle,
        )
        timer.daemon = True
        self._idle_shutdown_timer = timer
        timer.start()

    def _shutdown_if_idle(self) -> None:
        with self._lock:
            self._idle_shutdown_timer = None
            if self._sessions or self.idle_shutdown_seconds <= 0:
                return
            try:
                result = self.broker.call(
                    "shutdown_worker_edge",
                    timeout_ms=8000,
                )
            except Exception as exc:
                self._last_idle_shutdown_error = f"{type(exc).__name__}: {exc}"
                return
            self._idle_shutdown_count += 1
            self._last_idle_shutdown_error = None
            self._last_idle_shutdown_result = dict(result or {})

    def inspect(self, session_id: str) -> dict[str, Any]:
        route = self._route(session_id)
        composer = self._find_first(
            route["tab_ref"],
            [
                {"selector": "[contenteditable='true'][data-virtualkeyboard='true']"},
                {"selector": "div.ProseMirror[contenteditable='true']"},
                {"selector": "textarea"},
            ],
        )
        return {
            "session_id": session_id,
            "mode": "actuator_dom_broker",
            "transport": "dom_broker",
            "tab_ref": route["tab_ref"],
            "composer_found": bool(composer),
            "background": True,
        }

    def profile_status(self) -> dict[str, Any]:
        health = self.broker.health()
        with self._lock:
            active = len(self._sessions)
            used_tabs = {
                str(route.get("tab_ref") or "")
                for route in self._sessions.values()
            }
        pool_total = None
        pool_free = None
        worker_edge_running = None
        worker_edge_connected = None
        if self.tab_pool_only and health.get("status") == "ok":
            try:
                listed = self.broker.call("peek_tabs")
                worker_edge_running = bool(listed.get("running"))
                worker_edge_connected = bool(listed.get("connected"))
                pool_tabs = [
                    tab
                    for tab in (listed.get("tabs") or [])
                    if str(tab.get("url") or "").startswith("https://chatgpt.com")
                ]
                pool_total = len(pool_tabs)
                pool_free = sum(
                    1
                    for tab in pool_tabs
                    if str(tab.get("tab_ref") or "") not in used_tabs
                )
            except Exception:
                pass
        with self._lock:
            idle_shutdown_pending = self._idle_shutdown_timer is not None
            idle_shutdown_count = self._idle_shutdown_count
            last_idle_shutdown_error = self._last_idle_shutdown_error
            last_idle_shutdown_result = self._last_idle_shutdown_result
        ready = health.get("status") == "ok"
        return {
            "mode": "actuator_dom_broker",
            "ready": ready,
            "primary": "actuator_dom_broker",
            "effective_backend": "actuator_dom_broker" if ready else "unavailable",
            "selected_backend": "actuator_dom_broker" if ready else "unavailable",
            "active_dom_sessions": active,
            "shared_cdp_owner": "ChatGPT-Actuator",
            "tab_pool_only": self.tab_pool_only,
            "tab_pool_size": self.tab_pool_size,
            "tab_pool_total": pool_total,
            "tab_pool_free": pool_free,
            "worker_edge_running": worker_edge_running,
            "worker_edge_connected": worker_edge_connected,
            "idle_shutdown_seconds": self.idle_shutdown_seconds,
            "idle_shutdown_pending": idle_shutdown_pending,
            "idle_shutdown_count": idle_shutdown_count,
            "last_idle_shutdown_error": last_idle_shutdown_error,
            "last_idle_shutdown_result": last_idle_shutdown_result,
            "broker_health": health,
            "replay_on_send_failure": False,
        }

    def _current_tab_url(self, tab_ref: str) -> str:
        try:
            listed = self.broker.call("list_tabs")
        except Exception:
            return ""
        for tab in listed.get("tabs") or []:
            if str(tab.get("tab_ref") or "") == tab_ref:
                return str(tab.get("url") or "")
        return ""

    def _new_chat_dialog(self, tab_ref: str) -> str | None:
        return self._find_first(
            tab_ref,
            [
                {"selector": "dialog#mobile-new-chat-dialog[open]"},
                {"selector": "#mobile-new-chat-dialog[open]"},
                {"selector": "dialog[data-bottom-sheet][open]"},
            ],
        )

    def _resolve_new_chat_dialog(
        self,
        tab_ref: str,
        *,
        prefer_clear: bool,
        timeout_seconds: float = 4.0,
    ) -> bool:
        """Resolve an already-open mobile New chat sheet without OS focus.

        A stale/open sheet intercepts pointer events and can make the next
        New-chat click time out. Prefer the destructive Clear chat action when
        the caller is resetting a pooled worker tab; otherwise dismiss safely.
        """
        deadline = time.monotonic() + max(0.2, float(timeout_seconds))
        handled = False
        while time.monotonic() < deadline:
            dialog = self._new_chat_dialog(tab_ref)
            if not dialog:
                return handled

            clear_specs = [
                {"text": "Clear chat", "exact": True},
                {"role": "button", "name": "Clear chat", "exact": True},
                {"role": "link", "name": "Clear chat", "exact": True},
                {"text": "Clear current chat", "exact": True},
                {"role": "button", "name": "Clear current chat", "exact": True},
                {"text": "Xóa cuộc trò chuyện", "exact": True},
                {"role": "button", "name": "Xóa cuộc trò chuyện", "exact": True},
                {"text": "Xóa đoạn chat", "exact": True},
                {"role": "button", "name": "Xóa đoạn chat", "exact": True},
            ]
            dismiss_specs = [
                {"selector": "button[aria-label='Close']"},
                {"selector": "button[aria-label='Đóng']"},
                {"text": "Cancel", "exact": True},
                {"role": "button", "name": "Cancel", "exact": True},
                {"text": "Hủy", "exact": True},
                {"role": "button", "name": "Hủy", "exact": True},
                {"text": "Close", "exact": True},
                {"role": "button", "name": "Close", "exact": True},
            ]

            action = self._find_first(
                tab_ref,
                clear_specs if prefer_clear else dismiss_specs + clear_specs,
            )
            if not action and prefer_clear:
                action = self._find_first(tab_ref, dismiss_specs)
            if not action:
                time.sleep(0.1)
                continue

            self.broker.call(
                "click",
                element_ref=action,
                timeout_ms=3000,
            )
            handled = True
            close_deadline = time.monotonic() + 2.0
            while time.monotonic() < close_deadline:
                if not self._new_chat_dialog(tab_ref):
                    break
                time.sleep(0.08)
            if not self._new_chat_dialog(tab_ref):
                return handled

        if self._new_chat_dialog(tab_ref):
            raise DOMPreSubmissionError(
                "ChatGPT New chat dialog remained open and would intercept "
                "background DOM clicks."
            )
        return handled

    def _composer_text(self, composer_ref: str) -> str:
        details = self.broker.call(
            "details",
            element_ref=composer_ref,
        )
        current_value = details.get("value")
        if current_value is not None:
            return str(current_value)
        return self._read_text(composer_ref)

    def _prepare_pooled_tab(self, tab_ref: str) -> None:
        """Reset a pooled ChatGPT tab without navigation or foreground focus."""
        # Clean up a stale mobile sheet left by a prior interrupted reset.
        self._resolve_new_chat_dialog(
            tab_ref,
            prefer_clear=True,
            timeout_seconds=min(4.0, self.create_timeout_seconds),
        )

        current_url = self._current_tab_url(tab_ref)
        composer = self._wait_for_composer(
            tab_ref,
            self.create_timeout_seconds,
        )
        try:
            composer_empty = not self._composer_text(composer).strip()
        except Exception:
            composer_empty = False

        already_fresh = (
            current_url.startswith("https://chatgpt.com")
            and "/c/" not in current_url
            and "/uc/" not in current_url
            and composer_empty
        )

        if not already_fresh:
            new_chat = self._find_first(
                tab_ref,
                [
                    {"selector": "a[data-testid='create-new-chat-button']"},
                    {"selector": "a[aria-label='New chat']"},
                    {"selector": "button[aria-label='New chat']"},
                    {"selector": "a[aria-label='Cuộc trò chuyện mới']"},
                    {"selector": "button[aria-label='Cuộc trò chuyện mới']"},
                    {"text": "New chat", "exact": True},
                    {"text": "Cuộc trò chuyện mới", "exact": True},
                    {"role": "link", "name": "New chat", "exact": True},
                    {"role": "button", "name": "New chat", "exact": True},
                    {"role": "link", "name": "Cuộc trò chuyện mới", "exact": True},
                    {"role": "button", "name": "Cuộc trò chuyện mới", "exact": True},
                ],
            )
            if not new_chat:
                raise DOMPreSubmissionError(
                    "ChatGPT New chat control was not found for pooled-tab reset."
                )

            # Re-check immediately before clicking: hydration can create the
            # mobile sheet between discovery and activation.
            self._resolve_new_chat_dialog(
                tab_ref,
                prefer_clear=True,
                timeout_seconds=1.5,
            )
            self.broker.call(
                "click",
                element_ref=new_chat,
                timeout_ms=max(
                    1000,
                    int(min(self.create_timeout_seconds, 10.0) * 1000),
                ),
            )
            self._resolve_new_chat_dialog(
                tab_ref,
                prefer_clear=True,
                timeout_seconds=min(5.0, self.create_timeout_seconds),
            )

        # Require a fresh URL plus a live composer. A visible old composer can
        # survive briefly while React swaps the conversation route.
        deadline = time.monotonic() + min(
            max(3.0, self.create_timeout_seconds),
            10.0,
        )
        fresh_ref = ""
        consecutive_ready = 0
        while time.monotonic() < deadline:
            if self._new_chat_dialog(tab_ref):
                self._resolve_new_chat_dialog(
                    tab_ref,
                    prefer_clear=True,
                    timeout_seconds=2.0,
                )
                consecutive_ready = 0
                continue

            current_url = self._current_tab_url(tab_ref)
            try:
                fresh_ref = self._wait_for_composer(tab_ref, 1.0)
                text = self._composer_text(fresh_ref).strip()
            except Exception:
                consecutive_ready = 0
                time.sleep(0.1)
                continue

            fresh_url = (
                current_url.startswith("https://chatgpt.com")
                and "/c/" not in current_url
                and "/uc/" not in current_url
            )
            if fresh_url and not text:
                consecutive_ready += 1
                if consecutive_ready >= 2:
                    return
            else:
                consecutive_ready = 0
                if text:
                    try:
                        self.broker.call(
                            "set_value",
                            element_ref=fresh_ref,
                            value="",
                            timeout_ms=3000,
                        )
                    except Exception:
                        pass
            time.sleep(0.2)

        raise DOMPreSubmissionError(
            "Pooled ChatGPT tab did not settle on a fresh hydrated composer."
        )

    def _set_and_verify_composer(
        self,
        tab_ref: str,
        composer_ref: str,
        prompt: str,
        timeout_seconds: float,
    ) -> str:
        """Fill only after hydration and require the prompt to remain stable."""
        last_error: Exception | None = None
        current_ref = composer_ref
        attempt_timeout_ms = max(
            1000,
            int(min(max(0.1, float(timeout_seconds)), 5.0) * 1000),
        )
        expected = " ".join(prompt.split())
        attempts = 5

        for attempt in range(attempts):
            if attempt:
                # React may replace the composer node after route hydration.
                time.sleep(min(0.2 * (attempt + 1), 0.8))
                current_ref = self._wait_for_composer(
                    tab_ref,
                    min(5.0, timeout_seconds),
                )

            if self._new_chat_dialog(tab_ref):
                try:
                    self._resolve_new_chat_dialog(
                        tab_ref,
                        prefer_clear=True,
                        timeout_seconds=2.0,
                    )
                    current_ref = self._wait_for_composer(
                        tab_ref,
                        min(5.0, timeout_seconds),
                    )
                except Exception as exc:
                    last_error = exc
                    continue

            try:
                self.broker.call(
                    "set_value",
                    element_ref=current_ref,
                    value=prompt,
                    timeout_ms=attempt_timeout_ms,
                )
            except Exception as exc:
                last_error = exc
                continue

            # A single successful read is insufficient: ChatGPT hydration can
            # clear the field a moment later. Require two stable reads after
            # re-resolving the composer each time.
            stable_reads = 0
            verify_deadline = time.monotonic() + 3.0
            while time.monotonic() < verify_deadline:
                try:
                    if self._new_chat_dialog(tab_ref):
                        stable_reads = 0
                        break
                    current_ref = self._wait_for_composer(tab_ref, 1.0)
                    typed = self._composer_text(current_ref)
                    actual = " ".join(typed.split())
                    if not expected or expected == actual or expected in actual:
                        stable_reads += 1
                        if stable_reads >= 2:
                            return current_ref
                    else:
                        stable_reads = 0
                        if not actual:
                            # Hydration cleared the prompt; refill on the next
                            # outer attempt rather than submitting uncertainly.
                            break
                except Exception as exc:
                    last_error = exc
                    stable_reads = 0
                time.sleep(0.2)

        detail = (
            f"{type(last_error).__name__}: {last_error}"
            if last_error is not None
            else "composer did not retain the prompt across hydration"
        )
        raise DOMPreSubmissionError(
            f"DOM composer verification failed before submission: {detail}"
        )

    def _wait_for_composer(self, tab_ref: str, timeout_seconds: float) -> str:
        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        while time.monotonic() < deadline:
            ref = self._find_first(
                tab_ref,
                [
                    {"selector": "[contenteditable='true'][data-virtualkeyboard='true']"},
                    {"selector": "div.ProseMirror[contenteditable='true']"},
                    {"selector": "textarea"},
                ],
            )
            if ref:
                return ref
            time.sleep(0.15)
        raise DOMPreSubmissionError("ChatGPT DOM composer was not found.")

    def _wait_for_send_button(self, tab_ref: str, timeout_seconds: float) -> str:
        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        specs = [
            {"selector": "button[data-testid='send-button']"},
            {"selector": "[data-testid='send-button']"},
            {"selector": "button[data-composer-submit]"},
            {"selector": "button[aria-label='Send message']"},
            {"selector": "button[aria-label='Gửi tin nhắn']"},
            {"selector": "button[aria-label='Gửi']"},
            {"selector": "button[aria-label='Send']"},
            {"role": "button", "name": "Send message", "exact": True},
            {"role": "button", "name": "Gửi tin nhắn", "exact": True},
            {"role": "button", "name": "Gửi", "exact": True},
            {"role": "button", "name": "Send", "exact": True},
        ]
        while time.monotonic() < deadline:
            ref = self._find_first(tab_ref, specs)
            if ref:
                return ref
            time.sleep(0.1)
        raise DOMPreSubmissionError("ChatGPT DOM Send button was not found.")

    def _submission_observed(
        self,
        tab_ref: str,
        composer_ref: str,
        prompt: str,
        baseline: str,
    ) -> bool:
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            try:
                if self._read_text(composer_ref).strip() == "":
                    return True
            except Exception:
                pass
            try:
                current = self._main_text(tab_ref)
                if prompt.strip() and prompt.strip() in current:
                    if current != baseline:
                        return True
            except Exception:
                pass
            time.sleep(0.08)
        return False

    def _wait_for_response(
        self,
        tab_ref: str,
        prompt: str,
        baseline: str,
        timeout_seconds: float,
        *,
        cancel_event: threading.Event | None,
    ) -> str:
        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        last_response = ""
        stable_since: float | None = None

        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                self.cancel_by_tab(tab_ref)
                raise InterruptedError("Worker job was cancelled.")

            current = self._main_text(tab_ref)
            response = self._extract_response(current, baseline, prompt)
            generating = self._is_generating(tab_ref)

            if response:
                if response == last_response:
                    if stable_since is None:
                        stable_since = time.monotonic()
                else:
                    last_response = response
                    stable_since = time.monotonic()

                if (
                    not generating
                    and stable_since is not None
                    and time.monotonic() - stable_since >= self.stable_seconds
                ):
                    return response
            time.sleep(0.2)

        raise AmbiguousSubmissionError(
            "dom_response_timeout: prompt was submitted but a stable assistant "
            "response was not observed; automatic replay is disabled."
        )

    def cancel_by_tab(self, tab_ref: str) -> bool:
        button = self._find_first(
            tab_ref,
            [
                {"selector": "button[data-testid='stop-button']"},
                {"selector": "button[aria-label='Dừng tạo']"},
                {"selector": "button[aria-label='Stop generating']"},
                {"role": "button", "name": "Dừng tạo", "exact": True},
                {"role": "button", "name": "Stop generating", "exact": True},
            ],
        )
        if not button:
            return False
        try:
            self.broker.call("click", element_ref=button, timeout_ms=3000)
            return True
        except Exception:
            return False

    def _is_generating(self, tab_ref: str) -> bool:
        return bool(
            self._find_first(
                tab_ref,
                [
                    {"selector": "button[data-testid='stop-button']"},
                    {"selector": "button[aria-label='Dừng tạo']"},
                    {"selector": "button[aria-label='Stop generating']"},
                    {"role": "button", "name": "Dừng tạo", "exact": True},
                    {"role": "button", "name": "Stop generating", "exact": True},
                ],
            )
        )

    def _main_text(self, tab_ref: str) -> str:
        main = self._find_first(tab_ref, [{"selector": "main"}])
        if not main:
            raise RuntimeError("ChatGPT main DOM surface was not found.")
        return self._read_text(main, max_chars=1_000_000)

    def _read_text(self, element_ref: str, max_chars: int = 1_000_000) -> str:
        value = self.broker.call(
            "read_text",
            element_ref=element_ref,
            max_chars=max_chars,
        )
        return str(value.get("text") or "")

    def _find_first(self, tab_ref: str, specs: list[dict]) -> str | None:
        for spec in specs:
            try:
                result = self.broker.call(
                    "find",
                    tab_ref=tab_ref,
                    max_items=5,
                    **spec,
                )
            except Exception:
                continue
            matches = result.get("matches")
            if not isinstance(matches, list):
                continue
            for item in matches:
                if not isinstance(item, dict):
                    continue
                ref = str(item.get("element_ref") or "")
                if ref and item.get("visible", True) is not False:
                    return ref
        return None

    @staticmethod
    def _extract_response(current: str, baseline: str, prompt: str) -> str:
        text = str(current or "")
        baseline_text = str(baseline or "")
        labels = ("ChatGPT đã nói:", "ChatGPT said:")

        segment = ""
        start = text.rfind(prompt) if prompt else -1
        if start >= 0:
            segment = text[start + len(prompt):]
        elif baseline_text and text.startswith(baseline_text):
            candidate = text[len(baseline_text):]
            if any(label in candidate for label in labels):
                segment = candidate
        else:
            # Long worker prompts are rendered by ChatGPT with normalized
            # whitespace and collapsed "Show more" sections, so an exact
            # rfind(prompt) can fail even though a new assistant turn exists.
            # Detect the new turn relative to the pre-submit baseline instead.
            current_label_count = sum(text.count(label) for label in labels)
            baseline_label_count = sum(
                baseline_text.count(label) for label in labels
            )
            if current_label_count > baseline_label_count:
                label_pos = -1
                label_len = 0
                for label in labels:
                    pos = text.rfind(label)
                    if pos > label_pos:
                        label_pos = pos
                        label_len = len(label)
                if label_pos >= 0:
                    segment = text[label_pos + label_len:]

        label_pos = -1
        label_len = 0
        for label in labels:
            pos = segment.rfind(label)
            if pos > label_pos:
                label_pos = pos
                label_len = len(label)
        if label_pos >= 0:
            segment = segment[label_pos + label_len:]

        lines = segment.strip().splitlines()
        trailing_ui = {
            "Cao",
            "High",
            "Trung bình",
            "Medium",
            "Thấp",
            "Low",
            "ChatGPT có thể mắc lỗi. Hãy kiểm tra thông tin quan trọng.",
            "ChatGPT can make mistakes. Check important info.",
            "ChatGPT is AI and can make mistakes.",
            "ChatGPT is AI and can make mistakes",
            "Chat with ChatGPT",
            "Phản hồi mới nhất",
            "Latest response",
            "Response complete",
            "Do you like this personality?",
            "Is this conversation helpful so far?",
        }
        while lines:
            tail = lines[-1].strip()
            if not tail or tail in trailing_ui:
                lines.pop()
                continue
            break
        return "\n".join(lines).strip()

    def _route(self, session_id: str) -> dict[str, str]:
        token = self._parse_session(session_id)
        with self._lock:
            route = self._sessions.get(token)
            if route is None:
                raise KeyError(f"Unknown Actuator DOM session: {session_id}")
            return dict(route)

    @staticmethod
    def _parse_session(session_id: str) -> str:
        prefix = "actdom:"
        value = str(session_id)
        if not value.startswith(prefix):
            raise ValueError(f"Invalid Actuator DOM session id: {session_id}")
        token = value[len(prefix):].strip()
        if not token:
            raise ValueError("Actuator DOM session token is empty.")
        return token
