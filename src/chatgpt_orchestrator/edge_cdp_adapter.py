from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any
from uuid import uuid4

from .adapters import WorkerAdapter

try:
    import websocket
except ImportError:  # pragma: no cover
    websocket = None


class EdgeCDPAdapter(WorkerAdapter):
    """Background-first Edge adapter using Chrome DevTools Protocol.

    A dedicated Edge user-data directory is used so CDP never attaches to the
    user's normal Edge profile. After a one-time ChatGPT sign-in in that profile,
    worker tabs can be controlled without OS mouse/keyboard input or foreground
    tab switching.
    """

    def __init__(
        self,
        executable: str,
        profile_dir: str,
        chat_url: str = "https://chatgpt.com/",
        create_timeout_seconds: float = 20.0,
        send_timeout_seconds: float = 120.0,
        stable_seconds: float = 3.0,
    ) -> None:
        if websocket is None:
            raise RuntimeError(
                "edge_cdp requires websocket-client. Run scripts/setup.ps1."
            )

        self.executable = str(Path(executable))
        self.profile_dir = Path(profile_dir)
        self.chat_url = str(chat_url)
        self.create_timeout_seconds = float(create_timeout_seconds)
        self.send_timeout_seconds = float(send_timeout_seconds)
        self.stable_seconds = float(stable_seconds)

        if not Path(self.executable).is_file():
            raise FileNotFoundError(f"Edge executable not found: {self.executable}")

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._browser_lock = threading.RLock()
        self._session_locks: dict[str, threading.RLock] = {}
        self._session_owners: dict[str, str] = {}
        self._free_targets: list[str] = []
        self._port: int | None = None
        self._process: subprocess.Popen | None = None
        self._cleanup_done = False
        self._message_id = 0

    def create(self, role: str) -> str:
        with self._browser_lock:
            self._ensure_browser()
            self._cleanup_stale_targets_once()
            target_id = self._take_reusable_target()
            reused = target_id is not None
            if target_id is None:
                result = self._browser_call(
                    "Target.createTarget",
                    {"url": self.chat_url, "newWindow": False},
                )
                target_id = str(result["targetId"])
            session_id = self._register_owned_target(target_id, role)

        try:
            if not reused:
                self._page_call(session_id, "Page.navigate", {"url": self.chat_url})
            self._wait_target(target_id, self.create_timeout_seconds)
            self._wait_for_composer(
                session_id,
                min(15.0, self.create_timeout_seconds),
            )
            self._mark_owned(session_id)
            return session_id
        except Exception:
            self._forget_session(session_id)
            self._close_target_best_effort(target_id)
            raise

    def send(
        self,
        session_id: str,
        prompt: str,
        *,
        cancel_event: threading.Event | None = None,
        timeout_seconds: float | None = None,
    ) -> str:
        prompt = str(prompt).strip()
        if not prompt:
            raise ValueError("prompt is required")

        timeout = self.send_timeout_seconds if timeout_seconds is None else float(timeout_seconds)
        self._require_owned_session(session_id)
        lock = self._session_locks[session_id]

        with lock:
            self._wait_for_composer(session_id, min(15.0, timeout))
            baseline = self._snapshot(session_id)
            self._focus_and_clear_composer(session_id)
            self._page_call(session_id, "Input.insertText", {"text": prompt})

            submitted = False
            submit_deadline = time.monotonic() + min(8.0, timeout)
            while time.monotonic() < submit_deadline:
                if cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError("Worker job was cancelled.")
                submitted = bool(
                    self._evaluate(
                        session_id,
                        """(() => {
                          const direct = document.querySelector('[data-testid="send-button"]');
                          const buttons = [...document.querySelectorAll('button')];
                          const fallback = buttons.find(b => {
                            const label = ((b.getAttribute('aria-label') || '') + ' ' +
                                           (b.getAttribute('data-testid') || '')).toLowerCase();
                            return label.includes('send') || label.includes('gửi');
                          });
                          const button = direct || fallback;
                          if (!button || button.disabled) return false;
                          button.click();
                          return true;
                        })()""",
                    )
                )
                if submitted:
                    break
                time.sleep(0.15)

            if not submitted:
                self._page_call(
                    session_id,
                    "Input.dispatchKeyEvent",
                    {
                        "type": "keyDown",
                        "key": "Enter",
                        "code": "Enter",
                        "windowsVirtualKeyCode": 13,
                        "nativeVirtualKeyCode": 13,
                    },
                )
                self._page_call(
                    session_id,
                    "Input.dispatchKeyEvent",
                    {
                        "type": "keyUp",
                        "key": "Enter",
                        "code": "Enter",
                        "windowsVirtualKeyCode": 13,
                        "nativeVirtualKeyCode": 13,
                    },
                )

            echo_deadline = time.monotonic() + min(10.0, timeout)
            while time.monotonic() < echo_deadline:
                if cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError("Worker job was cancelled.")
                snap = self._snapshot(session_id)
                if (
                    snap["user_count"] > baseline["user_count"]
                    and prompt[:160].strip() in snap["last_user"]
                ):
                    break
                time.sleep(0.2)
            else:
                raise RuntimeError("ChatGPT composer did not submit the delegated prompt.")

        return self._wait_for_response(
            session_id,
            baseline_assistant_count=int(baseline["assistant_count"]),
            cancel_event=cancel_event,
            timeout_seconds=timeout,
        )

    def cancel(self, session_id: str) -> bool:
        self._require_owned_session(session_id)
        try:
            return bool(
                self._evaluate(
                    session_id,
                    """(() => {
                      const buttons = [...document.querySelectorAll('button')];
                      const button = buttons.find(b => {
                        const label = ((b.getAttribute('aria-label') || '') + ' ' +
                                       (b.getAttribute('data-testid') || '')).toLowerCase();
                        return label.includes('stop') || label.includes('dừng') ||
                               label.includes('ngừng');
                      });
                      if (!button) return false;
                      button.click();
                      return true;
                    })()""",
                )
            )
        except Exception:
            return False

    def close(self, session_id: str) -> None:
        with self._browser_lock:
            if session_id not in self._session_owners:
                return
        target_id = self._parse_session(session_id)
        reusable = False
        try:
            reusable = self._release_to_pool(session_id)
        finally:
            self._forget_session(session_id)
        if not reusable:
            self._close_target_best_effort(target_id)

    def inspect(self, session_id: str) -> dict[str, Any]:
        target_id, owner = self._require_owned_session(session_id)
        snap = self._snapshot(session_id)
        return {
            "mode": "cdp_background",
            "target_id": target_id,
            "owner_token": owner,
            "title": snap["title"],
            "url": snap["url"],
            "visibility": snap["visibility"],
            "composer_found": snap["composer_found"],
            "authenticated": not snap["auth_required"],
            "challenge_detected": bool(snap["challenge_detected"]),
            "user_message_count": snap["user_count"],
            "assistant_message_count": snap["assistant_count"],
        }

    def profile_status(self) -> dict[str, Any]:
        with self._browser_lock:
            self._ensure_browser()
            pages = self._targets()
            debug_port = self._port
            endpoint_reachable = bool(
                debug_port is not None and self._probe_port(debug_port)
            )

        chat_pages = [
            p for p in pages
            if "chatgpt.com" in str(p.get("url", "")).casefold()
        ]
        composer_found = False
        authenticated = False
        auth_required = False
        challenge_detected = False
        ready_url = None

        for page in chat_pages:
            target_id = str(page.get("id") or "")
            if not target_id:
                continue
            try:
                snap = self._snapshot(self._raw_session(target_id))
            except Exception:
                continue
            challenge_detected = challenge_detected or bool(
                snap["challenge_detected"]
            )
            auth_required = auth_required or bool(snap["auth_required"])
            if not snap["auth_required"] and not snap["challenge_detected"]:
                authenticated = True
            if (
                snap["composer_found"]
                and not snap["auth_required"]
                and not snap["challenge_detected"]
            ):
                composer_found = True
                authenticated = True
                ready_url = snap["url"]
                break

        ready = bool(endpoint_reachable and composer_found)
        if ready:
            failure_code = None
        elif challenge_detected:
            failure_code = "cdp_challenged"
        elif not endpoint_reachable:
            failure_code = "cdp_connect_failed"
        elif not chat_pages:
            failure_code = "cdp_no_chat_page"
        elif auth_required and not authenticated:
            failure_code = "cdp_unauthenticated"
        else:
            failure_code = "cdp_not_ready"

        with self._browser_lock:
            owned_count = len(self._session_owners)
            reusable_count = len(self._free_targets)

        return {
            "mode": "cdp_background",
            "profile_dir": str(self.profile_dir),
            "debug_port": debug_port,
            "browser_running": endpoint_reachable,
            "endpoint_reachable": endpoint_reachable,
            "chat_page_count": len(chat_pages),
            "composer_found": composer_found,
            "authenticated": authenticated,
            "auth_required": auth_required,
            "challenge_detected": challenge_detected,
            "ready": ready,
            "failure_code": failure_code,
            "ready_url": ready_url,
            "owned_session_count": owned_count,
            "reusable_target_count": reusable_count,
        }

    def _ensure_browser(self) -> None:
        if self._port is not None and self._probe_port(self._port):
            return

        active_file = self.profile_dir / "DevToolsActivePort"
        if active_file.is_file():
            try:
                lines = active_file.read_text(encoding="utf-8").splitlines()
                port = int(lines[0].strip())
                if self._probe_port(port):
                    self._port = port
                    return
            except Exception:
                pass
            try:
                active_file.unlink()
            except OSError:
                pass

        self._process = subprocess.Popen(
            [
                self.executable,
                "--remote-debugging-port=0",
                "--remote-allow-origins=*",
                f"--user-data-dir={self.profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "--start-minimized",
                "about:blank",
            ],
            shell=False,
            close_fds=True,
        )

        deadline = time.monotonic() + self.create_timeout_seconds
        while time.monotonic() < deadline:
            if active_file.is_file():
                try:
                    lines = active_file.read_text(encoding="utf-8").splitlines()
                    port = int(lines[0].strip())
                    if self._probe_port(port):
                        self._port = port
                        return
                except Exception:
                    pass
            if self._process.poll() is not None:
                raise RuntimeError(
                    f"Dedicated Edge CDP process exited with code {self._process.returncode}."
                )
            time.sleep(0.15)

        raise RuntimeError("Timed out waiting for Edge CDP debugging endpoint.")

    def _probe_port(self, port: int) -> bool:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version",
                timeout=0.6,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return bool(payload.get("webSocketDebuggerUrl"))
        except Exception:
            return False

    def _http_json(self, path: str) -> Any:
        self._ensure_browser()
        assert self._port is not None
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self._port}{path}",
            timeout=2.0,
        ) as response:
            return json.loads(response.read().decode("utf-8"))

    def _browser_ws_url(self) -> str:
        payload = self._http_json("/json/version")
        return str(payload["webSocketDebuggerUrl"])

    def _targets(self) -> list[dict[str, Any]]:
        payload = self._http_json("/json/list")
        return [item for item in payload if item.get("type") == "page"]

    def _target_ws_url(self, target_id: str) -> str:
        for item in self._targets():
            if str(item.get("id")) == target_id:
                url = item.get("webSocketDebuggerUrl")
                if url:
                    return str(url)
        raise RuntimeError(f"CDP target is no longer available: {target_id}")

    def _wait_target(self, target_id: str, timeout_seconds: float) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                self._target_ws_url(target_id)
                state = self._evaluate(f"cdp:{target_id}", "document.readyState")
                current_url = str(self._evaluate(f"cdp:{target_id}", "location.href") or "")
                expected_host = "chatgpt.com" if "chatgpt.com" in self.chat_url.casefold() else self.chat_url
                if expected_host.casefold() in current_url.casefold() and state in {"interactive", "complete"}:
                    return
            except Exception:
                pass
            time.sleep(0.1)
        raise TimeoutError(f"Timed out waiting for CDP target: {target_id}")

    def _browser_call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        return self._ws_call(self._browser_ws_url(), method, params or {})

    def _page_call(
        self,
        session_id: str,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        target_id = self._parse_session(session_id)
        return self._ws_call(self._target_ws_url(target_id), method, params or {})

    def _ws_call(self, ws_url: str, method: str, params: dict[str, Any]) -> Any:
        with self._browser_lock:
            self._message_id += 1
            message_id = self._message_id

        ws = websocket.create_connection(
            ws_url,
            timeout=max(2.0, self.create_timeout_seconds),
            suppress_origin=True,
        )
        try:
            ws.send(json.dumps({"id": message_id, "method": method, "params": params}))
            deadline = time.monotonic() + max(2.0, self.create_timeout_seconds)
            while time.monotonic() < deadline:
                payload = json.loads(ws.recv())
                if payload.get("id") != message_id:
                    continue
                if "error" in payload:
                    error = payload["error"]
                    raise RuntimeError(
                        f"CDP {method} failed: {error.get('message') or error}"
                    )
                return payload.get("result", {})
        finally:
            ws.close()
        raise TimeoutError(f"Timed out waiting for CDP method: {method}")

    def _evaluate(self, session_id: str, expression: str) -> Any:
        result = self._page_call(
            session_id,
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        if result.get("exceptionDetails"):
            details = result["exceptionDetails"]
            raise RuntimeError(
                f"CDP Runtime.evaluate failed: {details.get('text') or details}"
            )
        return result.get("result", {}).get("value")

    def _wait_for_composer(self, session_id: str, timeout_seconds: float) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            snap = self._snapshot(session_id)
            if snap["challenge_detected"]:
                raise RuntimeError(
                    "CDP_CHALLENGED: ChatGPT/Cloudflare challenge detected. "
                    "Challenge handling requires normal user/browser verification."
                )
            if snap["auth_required"]:
                raise RuntimeError(
                    "The dedicated Edge CDP profile is not signed in to ChatGPT. "
                    "Run scripts/open-cdp-profile.ps1 and sign in once."
                )
            if snap["composer_found"]:
                return
            time.sleep(0.2)
        snap = self._snapshot(session_id)
        raise RuntimeError(
            "ChatGPT composer was not found in the dedicated CDP profile. "
            f"Current page: {snap['url']}. A one-time ChatGPT sign-in may be required."
        )

    def _focus_and_clear_composer(self, session_id: str) -> None:
        ok = self._evaluate(
            session_id,
            """(() => {
              const el = document.querySelector('#prompt-textarea') ||
                         document.querySelector('textarea') ||
                         document.querySelector('[contenteditable="true"]');
              if (!el) return false;
              el.focus();
              if ('value' in el) {
                const proto = Object.getPrototypeOf(el);
                const descriptor = Object.getOwnPropertyDescriptor(proto, 'value') ||
                                   Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value');
                if (descriptor && descriptor.set) descriptor.set.call(el, '');
                else el.value = '';
              } else {
                el.innerHTML = '';
              }
              el.dispatchEvent(new Event('input', {bubbles: true}));
              return true;
            })()""",
        )
        if not ok:
            raise RuntimeError("ChatGPT composer was not available for CDP input.")

    def _snapshot(self, session_id: str) -> dict[str, Any]:
        value = self._evaluate(
            session_id,
            """(() => {
              const user = [...document.querySelectorAll('[data-message-author-role="user"]')];
              const assistant = [...document.querySelectorAll('[data-message-author-role="assistant"]')];
              const composer = document.querySelector('#prompt-textarea') ||
                               document.querySelector('textarea') ||
                               document.querySelector('[contenteditable="true"]');
              const buttons = [...document.querySelectorAll('button')];
              const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
              const title_text = (document.title || '').trim().toLowerCase();
              const body_text = (document.body?.innerText || '').slice(0, 5000).toLowerCase();
              const challenge_detected =
                title_text.includes('just a moment') ||
                body_text.includes('verify you are human') ||
                body_text.includes('checking your browser') ||
                body_text.includes('security verification');
              const auth_required = buttons.some(b => {
                const text = (b.innerText || '').trim().toLowerCase();
                return visible(b) && (text === 'log in' || text === 'đăng nhập' || text === 'sign up for free');
              });
              const generating = buttons.some(b => {
                const label = ((b.getAttribute('aria-label') || '') + ' ' +
                               (b.getAttribute('data-testid') || '')).toLowerCase();
                return label.includes('stop') || label.includes('dừng') ||
                       label.includes('ngừng');
              });
              return {
                title: document.title || '',
                url: location.href,
                visibility: document.visibilityState,
                composer_found: !!composer,
                auth_required,
                challenge_detected,
                user_count: user.length,
                assistant_count: assistant.length,
                last_user: user.length ? (user[user.length - 1].innerText || '').trim() : '',
                last_assistant: assistant.length ? (assistant[assistant.length - 1].innerText || '').trim() : '',
                generating,
              };
            })()""",
        )
        if not isinstance(value, dict):
            raise RuntimeError("Unexpected CDP page snapshot.")
        return value

    def _wait_for_response(
        self,
        session_id: str,
        *,
        baseline_assistant_count: int,
        cancel_event: threading.Event | None,
        timeout_seconds: float,
    ) -> str:
        deadline = time.monotonic() + timeout_seconds
        last_answer = ""
        stable_since = time.monotonic()

        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("Worker job was cancelled.")

            snap = self._snapshot(session_id)
            answer = str(snap["last_assistant"] or "")
            has_new_answer = int(snap["assistant_count"]) > baseline_assistant_count

            if answer != last_answer:
                last_answer = answer
                stable_since = time.monotonic()

            if (
                has_new_answer
                and answer
                and not bool(snap["generating"])
                and time.monotonic() - stable_since >= self.stable_seconds
            ):
                return answer
            time.sleep(0.4)

        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("Worker job was cancelled.")
        if last_answer:
            return last_answer
        raise TimeoutError("Timed out waiting for a ChatGPT CDP worker response.")

    def _register_owned_target(self, target_id: str, role: str) -> str:
        del role
        owner = uuid4().hex[:16]
        session_id = f"cdp:{owner}:{target_id}"
        self._session_locks[session_id] = threading.RLock()
        self._session_owners[session_id] = owner
        return session_id

    def _mark_owned(self, session_id: str) -> None:
        target_id, owner = self._require_owned_session(
            session_id,
            verify_marker=False,
        )
        marker = self._owned_marker(owner, target_id)
        self._evaluate(session_id, f"window.name = {json.dumps(marker)}; true")

    def _require_owned_session(
        self,
        session_id: str,
        *,
        verify_marker: bool = True,
    ) -> tuple[str, str]:
        with self._browser_lock:
            owner = self._session_owners.get(session_id)
        if owner is None:
            raise RuntimeError(f"cdp_target_not_owned: {session_id}")
        target_id = self._parse_session(session_id)
        try:
            self._target_ws_url(target_id)
        except Exception as exc:
            raise RuntimeError(
                f"cdp_target_missing: {target_id}"
            ) from exc
        if verify_marker:
            marker = self._evaluate(session_id, "window.name")
            expected = self._owned_marker(owner, target_id)
            if marker != expected:
                raise RuntimeError(
                    f"cdp_target_not_owned: ownership marker mismatch for {target_id}"
                )
        return target_id, owner

    def _release_to_pool(self, session_id: str) -> bool:
        try:
            target_id, _owner = self._require_owned_session(session_id)
            self._page_call(session_id, "Page.navigate", {"url": self.chat_url})
            self._wait_target(target_id, self.create_timeout_seconds)
            self._wait_for_composer(
                session_id,
                min(10.0, self.create_timeout_seconds),
            )
            snap = self._snapshot(session_id)
            if (
                snap["challenge_detected"]
                or snap["auth_required"]
                or not snap["composer_found"]
            ):
                return False
            marker = self._free_marker(target_id)
            self._evaluate(
                session_id,
                f"window.name = {json.dumps(marker)}; true",
            )
            with self._browser_lock:
                if target_id not in self._free_targets:
                    self._free_targets.append(target_id)
            return True
        except Exception:
            return False

    def _take_reusable_target(self) -> str | None:
        while self._free_targets:
            target_id = self._free_targets.pop(0)
            try:
                self._target_ws_url(target_id)
                marker = self._evaluate(
                    self._raw_session(target_id),
                    "window.name",
                )
                if marker != self._free_marker(target_id):
                    continue
                snap = self._snapshot(self._raw_session(target_id))
                if (
                    snap["challenge_detected"]
                    or snap["auth_required"]
                    or not snap["composer_found"]
                ):
                    self._close_target_best_effort(target_id)
                    continue
                return target_id
            except Exception:
                self._close_target_best_effort(target_id)
        return None

    def _forget_session(self, session_id: str) -> None:
        with self._browser_lock:
            self._session_locks.pop(session_id, None)
            self._session_owners.pop(session_id, None)

    def _close_target_best_effort(self, target_id: str) -> None:
        try:
            self._browser_call("Target.closeTarget", {"targetId": target_id})
        except Exception:
            pass
        with self._browser_lock:
            self._free_targets = [
                item for item in self._free_targets if item != target_id
            ]

    def _cleanup_stale_targets_once(self) -> None:
        if self._cleanup_done:
            return
        self._cleanup_done = True
        for target in self._targets():
            target_id = str(target.get("id") or "")
            if not target_id:
                continue
            try:
                marker = self._evaluate(
                    self._raw_session(target_id),
                    "window.name",
                )
            except Exception:
                continue
            if marker == self._free_marker(target_id):
                try:
                    snap = self._snapshot(self._raw_session(target_id))
                except Exception:
                    self._close_target_best_effort(target_id)
                    continue
                if (
                    not snap["challenge_detected"]
                    and not snap["auth_required"]
                    and snap["composer_found"]
                ):
                    if target_id not in self._free_targets:
                        self._free_targets.append(target_id)
                    continue
                self._close_target_best_effort(target_id)
                continue
            if isinstance(marker, str) and marker.startswith(
                "chatgpt-orchestrator:"
            ):
                # An owned target from a previous process cannot be trusted.
                self._close_target_best_effort(target_id)

    @staticmethod
    def _raw_session(target_id: str) -> str:
        return f"cdp:probe:{target_id}"

    @staticmethod
    def _owned_marker(owner: str, target_id: str) -> str:
        return f"chatgpt-orchestrator:v061:owned:{owner}:{target_id}"

    @staticmethod
    def _free_marker(target_id: str) -> str:
        return f"chatgpt-orchestrator:v061:free:{target_id}"

    @staticmethod
    def _parse_session(session_id: str) -> str:
        value = str(session_id).strip()
        if not value.startswith("cdp:"):
            raise ValueError(f"Invalid CDP session id: {session_id}")
        parts = value.split(":")
        if len(parts) == 2:
            target_id = parts[1].strip()
        elif len(parts) == 3:
            target_id = parts[2].strip()
        else:
            raise ValueError(f"Invalid CDP session id: {session_id}")
        if not target_id:
            raise ValueError("CDP target id is empty.")
        return target_id
