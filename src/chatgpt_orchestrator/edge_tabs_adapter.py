from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from uuid import uuid4

import pythoncom
import pywintypes
import win32gui
from pywinauto import Desktop, keyboard

from .adapters import WorkerAdapter
from .edge_adapter import EdgeChatGPTAdapter


class EdgeSharedTabsAdapter(WorkerAdapter):
    """One dedicated Edge window with one ChatGPT tab per worker."""

    _ui_lock = EdgeChatGPTAdapter._ui_lock

    def __init__(
        self,
        executable: str,
        chat_url: str = "https://chatgpt.com/",
        create_timeout_seconds: float = 20.0,
        send_timeout_seconds: float = 120.0,
        stable_seconds: float = 3.0,
    ) -> None:
        self.executable = str(Path(executable))
        self.chat_url = chat_url
        self.create_timeout_seconds = float(create_timeout_seconds)
        self.send_timeout_seconds = float(send_timeout_seconds)
        self.stable_seconds = float(stable_seconds)
        if not Path(self.executable).is_file():
            raise FileNotFoundError(f"Edge executable not found: {self.executable}")

        self._helper = EdgeChatGPTAdapter(
            executable=self.executable,
            chat_url=self.chat_url,
            create_timeout_seconds=self.create_timeout_seconds,
            send_timeout_seconds=self.send_timeout_seconds,
            stable_seconds=self.stable_seconds,
        )
        self._shared_hwnd: int | None = None
        self._sessions: dict[str, int] = {}

    def create(self, role: str) -> str:
        del role
        token = uuid4().hex[:16]

        with self._ui_lock:
            if self._shared_hwnd is None or not win32gui.IsWindow(self._shared_hwnd):
                hwnd = self._create_shared_window()
                self._shared_hwnd = hwnd
                self._sessions[token] = 1
                return f"edgetab:{token}"

            hwnd = self._shared_hwnd
            self._helper._focus(hwnd)
            keyboard.send_keys("^t", pause=0.03)
            time.sleep(0.15)
            self._helper._navigate(hwnd, self.chat_url)
            self._helper._wait_for_composer(
                hwnd,
                timeout_seconds=self.create_timeout_seconds,
            )
            slot = len(self._sessions) + 1
            if slot > 8:
                raise RuntimeError("Edge shared-tab mode supports at most 8 tab slots.")
            self._sessions[token] = slot
            return f"edgetab:{token}"

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

        with self._ui_lock:
            hwnd = self._activate(session_id)
            if "chatgpt.com" not in self._helper._get_url(hwnd).casefold():
                self._helper._navigate(hwnd, self.chat_url)

            composer = self._helper._wait_for_composer(hwnd, timeout_seconds=10.0)
            old_clipboard = self._helper._read_clipboard()
            try:
                composer.set_focus()
                self._helper._write_clipboard(prompt)
                keyboard.send_keys("^v", pause=0.02)
                time.sleep(0.10)
                keyboard.send_keys("{ENTER}", pause=0.02)
            finally:
                self._helper._restore_clipboard(old_clipboard)

            if not self._helper._wait_for_prompt_echo(
                hwnd,
                prompt,
                timeout_seconds=8.0,
            ):
                raise RuntimeError(
                    "ChatGPT composer did not submit the delegated prompt."
                )

        return self._wait_for_response(
            session_id,
            prompt,
            cancel_event=cancel_event,
            timeout_seconds=timeout_seconds,
        )

    def cancel(self, session_id: str) -> bool:
        with self._ui_lock:
            hwnd = self._activate(session_id)
            surface = self._helper._main_surface(hwnd)
            if surface is None:
                return False

            for element in surface.descendants(control_type="Button"):
                try:
                    name = (element.element_info.name or "").casefold()
                except Exception:
                    continue
                if (
                    "stop generating" not in name
                    and "dừng tạo" not in name
                    and "ngừng tạo" not in name
                ):
                    continue
                try:
                    element.invoke()
                    return True
                except Exception:
                    try:
                        element.click_input()
                        return True
                    except Exception:
                        return False
        return False

    def close(self, session_id: str) -> None:
        token = self._parse_session(session_id)
        with self._ui_lock:
            if token not in self._sessions:
                return
            if self._shared_hwnd is None or not win32gui.IsWindow(self._shared_hwnd):
                self._sessions.pop(token, None)
                if not self._sessions:
                    self._shared_hwnd = None
                return

            removed_slot = self._sessions[token]
            self._activate(session_id)
            keyboard.send_keys("^w", pause=0.03)
            time.sleep(0.15)
            self._sessions.pop(token, None)

            for other, slot in list(self._sessions.items()):
                if slot > removed_slot:
                    self._sessions[other] = slot - 1

            if not self._sessions:
                self._shared_hwnd = None

    def inspect(self, session_id: str) -> dict:
        token = self._parse_session(session_id)
        with self._ui_lock:
            hwnd = self._activate(session_id)
            return {
                "mode": "shared_tabs",
                "hwnd": hwnd,
                "slot": self._sessions[token],
                "tab_count": len(self._sessions),
                "title": win32gui.GetWindowText(hwnd),
                "url": self._helper._get_url(hwnd),
                "visible": bool(win32gui.IsWindowVisible(hwnd)),
                "foreground": int(win32gui.GetForegroundWindow() or 0) == hwnd,
                "composer_found": self._helper._find_composer(hwnd) is not None,
            }

    def _wait_for_response(
        self,
        session_id: str,
        prompt: str,
        *,
        cancel_event: threading.Event | None,
        timeout_seconds: float | None,
    ) -> str:
        timeout = (
            self.send_timeout_seconds
            if timeout_seconds is None
            else float(timeout_seconds)
        )
        deadline = time.monotonic() + timeout
        last_answer = ""
        stable_since = time.monotonic()

        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("Worker job was cancelled.")

            with self._ui_lock:
                hwnd = self._activate(session_id)
                answer = self._helper._extract_latest_response(
                    self._helper._main_texts(hwnd),
                    prompt,
                )
                generating = self._helper._is_generating(hwnd)

            if answer != last_answer:
                last_answer = answer
                stable_since = time.monotonic()

            if (
                answer
                and not generating
                and time.monotonic() - stable_since >= self.stable_seconds
            ):
                return answer

            time.sleep(0.4)

        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("Worker job was cancelled.")
        if last_answer:
            return last_answer
        raise TimeoutError("Timed out waiting for a ChatGPT worker response.")

    def _create_shared_window(self) -> int:
        before = set(self._helper._edge_windows())
        subprocess.Popen(
            [self.executable, "--new-window", self.chat_url],
            shell=False,
            close_fds=True,
        )

        deadline = time.monotonic() + self.create_timeout_seconds
        while time.monotonic() < deadline:
            fresh = [
                hwnd
                for hwnd in self._helper._edge_windows()
                if hwnd not in before
            ]
            for hwnd in fresh:
                try:
                    self._helper._focus(hwnd)
                    url = self._helper._get_url(hwnd)
                    title = win32gui.GetWindowText(hwnd)
                    composer = self._helper._find_composer(hwnd)
                    if (
                        "chatgpt.com" in url.casefold()
                        and "chatgpt" in title.casefold()
                        and composer is not None
                    ):
                        return hwnd
                except Exception:
                    continue
            time.sleep(0.25)

        raise RuntimeError("Timed out waiting for shared ChatGPT Edge window.")

    def _activate(self, session_id: str) -> int:
        token = self._parse_session(session_id)
        if token not in self._sessions:
            raise RuntimeError(f"Unknown or closed shared-tab session: {session_id}")
        hwnd = self._shared_hwnd
        if hwnd is None or not win32gui.IsWindow(hwnd):
            raise RuntimeError("Shared Edge worker window no longer exists.")

        slot = self._sessions[token]
        self._helper._focus(hwnd)
        keyboard.send_keys(f"^{slot}", pause=0.03)
        time.sleep(0.12)
        return hwnd

    @staticmethod
    def _parse_session(session_id: str) -> str:
        prefix = "edgetab:"
        value = str(session_id)
        if not value.startswith(prefix):
            raise ValueError(f"Invalid shared-tab session id: {session_id}")
        token = value[len(prefix):]
        if not token:
            raise ValueError("Shared-tab session token is empty.")
        return token
