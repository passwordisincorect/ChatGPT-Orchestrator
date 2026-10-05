from __future__ import annotations



import subprocess

import threading

import time

from pathlib import Path



import psutil

import pythoncom

import pywintypes

import win32api

import win32clipboard

import win32con

import win32gui

import win32process

from pywinauto import Desktop, keyboard



from .adapters import WorkerAdapter





class EdgeChatGPTAdapter(WorkerAdapter):

    """ChatGPT Web worker adapter for Microsoft Edge using Windows UIA."""



    _ui_lock = threading.RLock()



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



    def create(self, role: str) -> str:

        del role  # Role remains Orchestrator metadata; MAIN controls the prompt.

        with self._ui_lock:

            before = set(self._edge_windows())

            subprocess.Popen(

                [self.executable, "--new-window", self.chat_url],

                shell=False,

                close_fds=True,

            )



            deadline = time.monotonic() + self.create_timeout_seconds

            while time.monotonic() < deadline:

                fresh = [

                    hwnd for hwnd in self._edge_windows()

                    if hwnd not in before

                ]

                for hwnd in fresh:

                    try:

                        self._focus(hwnd)

                        url = self._get_url(hwnd)

                        composer = self._find_composer(hwnd)

                        title = win32gui.GetWindowText(hwnd)

                        if (

                            "chatgpt.com" in url.casefold()

                            and "chatgpt" in title.casefold()

                            and composer is not None

                        ):

                            return f"edge:{hwnd}"

                    except Exception:

                        continue

                time.sleep(0.25)



        raise RuntimeError("Timed out waiting for a ready ChatGPT Edge window.")



    def send(
        self,
        session_id: str,
        prompt: str,
        *,
        cancel_event: threading.Event | None = None,
        timeout_seconds: float | None = None,
    ) -> str:
        hwnd = self._parse_session(session_id)
        prompt = str(prompt).strip()
        if not prompt:
            raise ValueError("prompt is required")

        # Only foreground-sensitive submission is serialized. Generation and
        # polling may continue while another worker receives its prompt.
        with self._ui_lock:
            self._validate_window(hwnd)
            self._focus(hwnd)

            if "chatgpt.com" not in self._get_url(hwnd).casefold():
                self._navigate(hwnd, self.chat_url)

            composer = self._wait_for_composer(hwnd, timeout_seconds=10.0)
            old_clipboard = self._read_clipboard()
            try:
                composer.set_focus()
                self._write_clipboard(prompt)
                keyboard.send_keys("^v", pause=0.02)
                time.sleep(0.10)
                keyboard.send_keys("{ENTER}", pause=0.02)
            finally:
                self._restore_clipboard(old_clipboard)

            if not self._wait_for_prompt_echo(hwnd, prompt, timeout_seconds=8.0):
                raise RuntimeError(
                    "ChatGPT composer did not submit the delegated prompt."
                )

        return self._wait_for_response(
            hwnd,
            prompt,
            cancel_event=cancel_event,
            timeout_seconds=timeout_seconds,
        )

    def close(self, session_id: str) -> None:

        hwnd = self._parse_session(session_id)

        if win32gui.IsWindow(hwnd):

            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)



    def cancel(self, session_id: str) -> bool:
        hwnd = self._parse_session(session_id)
        if not win32gui.IsWindow(hwnd):
            return False

        with self._ui_lock:
            surface = self._main_surface(hwnd)
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
                        self._focus(hwnd)
                        element.click_input()
                        return True
                    except Exception:
                        return False
        return False

    def inspect(self, session_id: str) -> dict:

        hwnd = self._parse_session(session_id)

        self._validate_window(hwnd)

        with self._ui_lock:

            return {

                "hwnd": hwnd,

                "title": win32gui.GetWindowText(hwnd),

                "url": self._get_url(hwnd),

                "visible": bool(win32gui.IsWindowVisible(hwnd)),

                "foreground": int(win32gui.GetForegroundWindow() or 0) == hwnd,

                "composer_found": self._find_composer(hwnd) is not None,

            }



    def _wait_for_prompt_echo(

        self,

        hwnd: int,

        prompt: str,

        timeout_seconds: float,

    ) -> bool:

        deadline = time.monotonic() + float(timeout_seconds)

        while time.monotonic() < deadline:

            texts = self._main_texts(hwnd)

            if any(text == prompt for text in texts):

                return True

            time.sleep(0.25)

        return False



    def _wait_for_response(
        self,
        hwnd: int,
        prompt: str,
        *,
        cancel_event: threading.Event | None = None,
        timeout_seconds: float | None = None,
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

            self._validate_window(hwnd)
            answer = self._extract_latest_response(self._main_texts(hwnd), prompt)
            generating = self._is_generating(hwnd)

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

    @staticmethod

    def _extract_latest_response(texts: list[str], prompt: str) -> str:

        exact_indexes = [i for i, value in enumerate(texts) if value == prompt]

        if exact_indexes:

            start = exact_indexes[-1] + 1

            while start < len(texts) and EdgeChatGPTAdapter._is_assistant_label(texts[start]):

                start += 1

        else:

            # Edge/UIA may split or omit a long user prompt from Text nodes even
            # though the turn itself was submitted successfully. In that case,
            # anchor extraction to the latest user-turn marker followed by an
            # assistant-turn marker. This preserves fail-closed behavior when no
            # assistant turn exists and avoids replaying an ambiguous submission.
            user_indexes = [
                i for i, value in enumerate(texts)
                if EdgeChatGPTAdapter._is_user_label(value)
            ]
            if not user_indexes:
                return ""
            user_index = user_indexes[-1]
            assistant_indexes = [
                i for i in range(user_index + 1, len(texts))
                if EdgeChatGPTAdapter._is_assistant_label(texts[i])
            ]
            if not assistant_indexes:
                return ""
            start = assistant_indexes[-1] + 1



        answer: list[str] = []

        for value in texts[start:]:

            if EdgeChatGPTAdapter._is_terminal_marker(value):

                break

            if EdgeChatGPTAdapter._is_progress_marker(value):

                continue

            if EdgeChatGPTAdapter._is_assistant_label(value) and not answer:

                continue

            answer.append(value)



        return "\n".join(part for part in answer if part).strip()



    @staticmethod

    def _is_user_label(value: str) -> bool:

        folded = value.casefold().strip()

        return folded.startswith("báº¡n Ä‘Ã£ nÃ³i") or folded.startswith("you said")



    @staticmethod

    def _is_assistant_label(value: str) -> bool:

        folded = value.casefold().strip()

        return folded.startswith("chatgpt ") and (

            "đã nói" in folded

            or "said" in folded

        )



    @staticmethod

    def _is_progress_marker(value: str) -> bool:

        folded = value.casefold().strip()

        return (

            folded in {

                "chatgpt đang phản hồi",

                "chatgpt is responding",

                "đang suy nghĩ",

                "thinking",

                "thinking...",

                "đang suy nghĩ...",

            }

            or folded.startswith("chatgpt đang phản hồi")

            or folded.startswith("chatgpt is responding")

        )



    @staticmethod

    def _is_terminal_marker(value: str) -> bool:

        folded = value.casefold().strip()

        return (

            folded.startswith("bạn đã nói")

            or folded.startswith("you said")

            or folded in {"hỏi chatgpt", "ask chatgpt"}

            or folded.startswith("chatgpt có thể mắc lỗi")

            or folded.startswith("chatgpt can make mistakes")

            or folded.startswith("phản hồi mới nhất")

            or folded.startswith("latest response")

            or folded.startswith("bạn có thích tính cách này không")

            or folded.startswith("do you like this personality")
            or folded.startswith("cho đến lúc này, cuộc hội thoại này có hữu ích không")
            or folded.startswith("so far, has this conversation been helpful")

        )



    def _is_generating(self, hwnd: int) -> bool:

        surface = self._main_surface(hwnd)

        if surface is None:

            return False



        for value in self._main_texts(hwnd):

            if self._is_progress_marker(value):

                return True

        for element in surface.descendants(control_type="Button"):

            try:

                name = (element.element_info.name or "").casefold()

            except Exception:

                continue

            if (

                "stop generating" in name

                or "dừng tạo" in name

                or "ngừng tạo" in name

            ):

                return True

        return False



    def _main_texts(self, hwnd: int) -> list[str]:

        surface = self._main_surface(hwnd)

        if surface is None:

            return []



        result: list[str] = []

        for element in surface.descendants(control_type="Text"):

            try:

                name = (element.element_info.name or "").strip()

            except Exception:

                continue

            if name:

                result.append(name)

        return result



    def _main_surface(self, hwnd: int):

        window = self._uia_window(hwnd)

        for element in window.descendants(control_type="Group"):

            try:

                class_name = element.element_info.class_name or ""

            except Exception:

                continue

            if "MainContentSurface" in class_name:

                return element

        return None



    def _wait_for_composer(self, hwnd: int, timeout_seconds: float):

        deadline = time.monotonic() + float(timeout_seconds)

        while time.monotonic() < deadline:

            composer = self._find_composer(hwnd)

            if composer is not None:

                return composer

            time.sleep(0.25)

        raise TimeoutError("ChatGPT composer was not found.")



    def _find_composer(self, hwnd: int):

        window = self._uia_window(hwnd)

        for element in window.descendants(control_type="Edit"):

            try:

                class_name = element.element_info.class_name or ""

                rect = element.rectangle()

                if (

                    "ProseMirror" in class_name

                    and element.is_enabled()

                    and rect.width() > 0

                    and rect.height() > 0

                ):

                    return element

            except Exception:

                continue

        return None



    def _get_url(self, hwnd: int) -> str:

        window = self._uia_window(hwnd)

        for element in window.descendants(control_type="Edit"):

            try:

                if element.element_info.automation_id == "view_1017":

                    return str(element.get_value()).strip()

            except Exception:

                continue

        raise RuntimeError("Edge address bar was not found through UI Automation.")



    def _navigate(self, hwnd: int, url: str) -> None:

        window = self._uia_window(hwnd)

        address = None

        for element in window.descendants(control_type="Edit"):

            try:

                if element.element_info.automation_id == "view_1017":

                    address = element

                    break

            except Exception:

                continue

        if address is None:

            raise RuntimeError("Edge address bar was not found.")



        old_clipboard = self._read_clipboard()

        try:

            address.set_focus()

            keyboard.send_keys("^a", pause=0.02)

            self._write_clipboard(url)

            keyboard.send_keys("^v", pause=0.02)

            keyboard.send_keys("{ENTER}", pause=0.02)

        finally:

            self._restore_clipboard(old_clipboard)



        self._wait_for_composer(hwnd, timeout_seconds=10.0)



    @staticmethod

    def _uia_window(hwnd: int):

        pythoncom.CoInitialize()

        return Desktop(backend="uia").window(handle=hwnd)



    @staticmethod

    def _edge_windows() -> list[int]:

        handles: list[int] = []



        def callback(hwnd: int, _extra) -> bool:

            if not win32gui.IsWindowVisible(hwnd):

                return True

            if win32gui.GetClassName(hwnd) != "Chrome_WidgetWin_1":

                return True

            try:

                _thread, pid = win32process.GetWindowThreadProcessId(hwnd)

                if psutil.Process(pid).name().casefold() == "msedge.exe":

                    handles.append(int(hwnd))

            except (psutil.Error, pywintypes.error):

                pass

            return True



        try:

            win32gui.EnumWindows(callback, None)

        except pywintypes.error:

            pass

        return handles



    @staticmethod

    def _parse_session(session_id: str) -> int:

        prefix = "edge:"

        if not str(session_id).startswith(prefix):

            raise ValueError(f"Invalid Edge session id: {session_id}")

        return int(str(session_id)[len(prefix):])



    @staticmethod

    def _validate_window(hwnd: int) -> None:

        if hwnd <= 0 or not win32gui.IsWindow(hwnd):

            raise RuntimeError(f"Worker Edge window no longer exists: {hwnd}")



    @staticmethod

    def _focus(hwnd: int) -> None:

        EdgeChatGPTAdapter._validate_window(hwnd)

        if win32gui.IsIconic(hwnd):

            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)



        foreground = int(win32gui.GetForegroundWindow() or 0)

        current_thread = int(win32api.GetCurrentThreadId())

        target_thread, _ = win32process.GetWindowThreadProcessId(hwnd)

        foreground_thread = 0

        if foreground:

            foreground_thread, _ = win32process.GetWindowThreadProcessId(foreground)



        user32 = __import__("ctypes").windll.user32

        attached_current = False

        attached_foreground = False

        try:

            if current_thread != target_thread:

                attached_current = bool(

                    user32.AttachThreadInput(current_thread, target_thread, True)

                )

            if foreground_thread and foreground_thread != target_thread:

                attached_foreground = bool(

                    user32.AttachThreadInput(foreground_thread, target_thread, True)

                )

            win32gui.BringWindowToTop(hwnd)

            win32gui.SetForegroundWindow(hwnd)

        finally:

            if attached_foreground:

                user32.AttachThreadInput(foreground_thread, target_thread, False)

            if attached_current:

                user32.AttachThreadInput(current_thread, target_thread, False)



        deadline = time.monotonic() + 0.8

        while time.monotonic() < deadline:

            if int(win32gui.GetForegroundWindow() or 0) == hwnd:

                return

            time.sleep(0.02)

        raise RuntimeError(f"Could not focus Edge worker window: {hwnd}")



    @staticmethod

    def _open_clipboard() -> None:

        last_error = None

        for _ in range(20):

            try:

                win32clipboard.OpenClipboard()

                return

            except Exception as exc:

                last_error = exc

                time.sleep(0.025)

        if last_error is not None:

            raise last_error

        raise RuntimeError("Unable to open clipboard.")



    @classmethod

    def _read_clipboard(cls) -> str | None:

        cls._open_clipboard()

        try:

            if not win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):

                return None

            return str(win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT))

        finally:

            win32clipboard.CloseClipboard()



    @classmethod

    def _write_clipboard(cls, text: str) -> None:

        cls._open_clipboard()

        try:

            win32clipboard.EmptyClipboard()

            win32clipboard.SetClipboardText(str(text), win32con.CF_UNICODETEXT)

        finally:

            win32clipboard.CloseClipboard()



    @classmethod

    def _restore_clipboard(cls, text: str | None) -> None:

        if text is not None:

            cls._write_clipboard(text)
