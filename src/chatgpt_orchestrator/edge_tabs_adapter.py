from __future__ import annotations



import subprocess

import threading

import time

from pathlib import Path

from urllib.parse import urlparse

from uuid import uuid4



import win32gui



from .adapters import AmbiguousSubmissionError, WorkerAdapter

from .edge_adapter import EdgeChatGPTAdapter





class EdgeSharedTabsAdapter(WorkerAdapter):

    """One dedicated Edge window with one ChatGPT tab per worker.



    v0.5 transport is UIA-only: no OS keyboard, mouse, or clipboard input is

    used for tab creation, tab selection, navigation, prompt submission,

    cancellation, or tab closing.

    """



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

        self._window_sessions: dict[str, int] = {}

        self._free_slots: set[int] = set()

        # UIA exposes one active tab surface per Edge window. Serialize full

        # send/response cycles so concurrent workers do not steal selection

        # from one another while polling generated responses.

        self._send_lock = threading.RLock()



    def create(self, role: str) -> str:

        del role

        token = uuid4().hex[:16]



        with self._ui_lock:

            if self._shared_hwnd is None or not win32gui.IsWindow(self._shared_hwnd):

                hwnd = self._create_shared_window()

                try:

                    tabs = self._tab_items(hwnd)

                except Exception:

                    tabs = []

                if tabs:

                    self._shared_hwnd = hwnd

                    self._sessions[token] = 1

                else:

                    # Some Edge builds expose the web content through UIA while

                    # omitting browser-chrome TabItem nodes. Keep a dedicated

                    # UIA window for this worker instead of failing on send().

                    self._sessions[token] = 0

                    self._window_sessions[token] = hwnd

                return f"edgetab:{token}"



            hwnd = self._shared_hwnd

            if self._free_slots:

                slot = min(self._free_slots)

                self._free_slots.remove(slot)

                self._activate_slot(hwnd, slot)

            else:

                before = len(self._tab_items(hwnd))

                self._invoke_new_tab(hwnd)

                self._wait_for_tab_count(hwnd, before + 1, timeout_seconds=5.0)

                slot = self._selected_tab_slot(hwnd)

                if slot is None:

                    slot = before + 1

                if slot > 8:

                    raise RuntimeError("Edge shared-tab mode supports at most 8 tab slots.")



            self._navigate_uia(hwnd, self.chat_url)

            self._helper._wait_for_composer(

                hwnd,

                timeout_seconds=self.create_timeout_seconds,

            )

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



        with self._send_lock:

            submission_started = False

            try:

                with self._ui_lock:

                    hwnd = self._activate(session_id)

                    if "chatgpt.com" not in self._helper._get_url(hwnd).casefold():

                        self._navigate_uia(hwnd, self.chat_url)



                    composer = self._helper._wait_for_composer(

                        hwnd,

                        timeout_seconds=10.0,

                    )

                    self._set_value(composer, prompt)



                    send_button = self._wait_for_send_button(

                        hwnd,

                        timeout_seconds=5.0,

                    )



                    # Invoke is state-changing. If Invoke raises, ChatGPT may still

                    # have accepted the prompt, so any failure from this point on

                    # must never trigger automatic replay.

                    submission_started = True


                    try:

                        self._invoke(send_button)

                    except Exception as invoke_exc:

                        # Some Edge/UIA providers can raise after Invoke has already

                        # dispatched the action. Never replay blindly. Instead, accept

                        # the send only when post-invoke UI state proves submission.

                        if not self._submission_observed_after_invoke_error(

                            hwnd,

                            composer,

                            prompt,

                            timeout_seconds=2.5,

                        ):

                            raise AmbiguousSubmissionError(

                                "uia_send_ambiguous: UIA Invoke raised and submission "

                                "could not be confirmed; automatic retry/replay is disabled. "

                                f"{type(invoke_exc).__name__}: {invoke_exc}"

                            ) from invoke_exc



                    # Prompt echo is a best-effort readiness hint. Recent Edge

                    # UIA trees can lag even after ChatGPT accepted the prompt.

                    # Continue polling the response instead of treating a missed

                    # early echo as a submission failure. Any later failure is

                    # still fail-closed by the ambiguous-submission guard.

                    self._helper._wait_for_prompt_echo(

                        hwnd,

                        prompt,

                        timeout_seconds=8.0,

                    )



                return self._wait_for_response(

                    session_id,

                    prompt,

                    cancel_event=cancel_event,

                    timeout_seconds=timeout_seconds,

                )

            except InterruptedError:

                raise

            except AmbiguousSubmissionError:

                raise

            except Exception as exc:

                if submission_started:

                    raise AmbiguousSubmissionError(

                        "uia_send_ambiguous: UIA submission may already have reached "

                        "ChatGPT; automatic retry/replay is disabled. "

                        f"{type(exc).__name__}: {exc}"

                    ) from exc

                raise



    def profile_status(self) -> dict:

        with self._ui_lock:

            active_sessions = len(self._sessions)

            direct_ready = any(

                win32gui.IsWindow(hwnd)

                for hwnd in self._window_sessions.values()

            )

            window_ready = bool(

                direct_ready

                or (

                    self._shared_hwnd is not None

                    and win32gui.IsWindow(self._shared_hwnd)

                )

            )

        return {

            "mode": "shared_tabs_uia",

            "ready": True,

            "serialized_sends": True,

            "max_parallel_sends": 1,

            "active_sessions": active_sessions,

            "window_ready": window_ready,

            "window_per_session_fallback": bool(self._window_sessions),

            "input_mode": "uia_only",

        }



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

                    self._invoke(element)

                    return True

                except Exception:

                    return False

        return False



    def close(self, session_id: str) -> None:

        token = self._parse_session(session_id)

        with self._ui_lock:

            if token not in self._sessions:

                return



            direct_hwnd = self._window_sessions.pop(token, None)

            if direct_hwnd is not None:

                self._sessions.pop(token, None)

                if win32gui.IsWindow(direct_hwnd):

                    win32gui.PostMessage(direct_hwnd, 0x0010, 0, 0)  # WM_CLOSE

                if not self._sessions:

                    self._free_slots.clear()

                    shared_hwnd = self._shared_hwnd

                    if shared_hwnd is not None and win32gui.IsWindow(shared_hwnd):

                        win32gui.PostMessage(shared_hwnd, 0x0010, 0, 0)  # WM_CLOSE

                    self._shared_hwnd = None

                return



            if self._shared_hwnd is None or not win32gui.IsWindow(self._shared_hwnd):

                self._sessions.pop(token, None)

                if not self._sessions:

                    self._shared_hwnd = None

                return



            hwnd = self._shared_hwnd

            slot = self._sessions.pop(token)



            if self._sessions:

                self._free_slots.add(slot)

                try:

                    self._activate_slot(hwnd, slot)

                    self._navigate_uia(hwnd, self.chat_url)

                except Exception:

                    pass

                return



            self._free_slots.clear()

            win32gui.PostMessage(hwnd, 0x0010, 0, 0)  # WM_CLOSE

            deadline = time.monotonic() + 3.0

            while time.monotonic() < deadline and win32gui.IsWindow(hwnd):

                time.sleep(0.05)

            self._shared_hwnd = None



    def inspect(self, session_id: str) -> dict:

        token = self._parse_session(session_id)

        with self._ui_lock:

            hwnd = self._activate(session_id)

            return {

                "mode": "shared_tabs_uia",

                "input_mode": "uia_only",

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



        direct_hwnd = self._window_sessions.get(token)

        if direct_hwnd is not None:

            if not win32gui.IsWindow(direct_hwnd):

                raise RuntimeError("Dedicated Edge worker window no longer exists.")

            return direct_hwnd



        hwnd = self._shared_hwnd

        if hwnd is None or not win32gui.IsWindow(hwnd):

            raise RuntimeError("Shared Edge worker window no longer exists.")



        slot = self._sessions[token]

        try:

            self._activate_slot(hwnd, slot)

            return hwnd

        except RuntimeError as exc:

            if not str(exc).startswith("Worker tab slot "):

                raise



        # Edge can drop browser-chrome TabItem nodes after session creation

        # while the ChatGPT page itself remains accessible through UIA. Migrate

        # this worker to a fresh dedicated window instead of failing the job.

        direct_hwnd = self._create_shared_window()

        self._sessions[token] = 0

        self._window_sessions[token] = direct_hwnd

        return direct_hwnd



    def _activate_slot(self, hwnd: int, slot: int) -> None:

        tabs = self._tab_items(hwnd)

        if slot < 1 or slot > len(tabs):

            raise RuntimeError(

                f"Worker tab slot {slot} is unavailable; Edge currently exposes {len(tabs)} tabs."

            )



        target = tabs[slot - 1]

        try:

            selected = bool(target.iface_selection_item.CurrentIsSelected)

        except Exception:

            selected = False

        if not selected:

            target.iface_selection_item.Select()

            deadline = time.monotonic() + 2.0

            while time.monotonic() < deadline:

                tabs = self._tab_items(hwnd)

                if slot <= len(tabs):

                    try:

                        if bool(tabs[slot - 1].iface_selection_item.CurrentIsSelected):

                            return

                    except Exception:

                        pass

                time.sleep(0.03)



    def _tab_items(self, hwnd: int):

        window = self._helper._uia_window(hwnd)

        items = []

        seen_rects: set[tuple[int, int, int, int]] = set()

        for element in window.descendants(control_type="TabItem"):

            try:

                if (element.element_info.class_name or "") != "EdgeTab":

                    continue

                rect = element.rectangle()

                if rect.width() <= 0 or rect.height() <= 0:

                    continue



                # Recent Edge builds can expose duplicate accessibility nodes

                # for the same visible tab (especially with vertical tabs).

                # Slot accounting must use physical tab geometry, not raw

                # descendant count, or a four-tab window can appear to contain

                # hundreds of EdgeTab nodes.

                key = (

                    int(rect.left),

                    int(rect.top),

                    int(rect.right),

                    int(rect.bottom),

                )

                if key in seen_rects:

                    continue

                seen_rects.add(key)

                items.append(element)

            except Exception:

                continue

        items.sort(key=lambda e: (e.rectangle().top, e.rectangle().left))

        return items



    def _selected_tab_slot(self, hwnd: int) -> int | None:

        for index, element in enumerate(self._tab_items(hwnd), start=1):

            try:

                if bool(element.iface_selection_item.CurrentIsSelected):

                    return index

            except Exception:

                continue

        return None



    def _invoke_new_tab(self, hwnd: int) -> None:

        window = self._helper._uia_window(hwnd)

        for element in window.descendants(control_type="Button"):

            try:

                if (element.element_info.class_name or "") == "EdgeNewTabButton":

                    self._invoke(element)

                    return

            except Exception:

                continue

        raise RuntimeError("Edge New Tab button was not found through UI Automation.")



    def _wait_for_tab_count(self, hwnd: int, expected: int, timeout_seconds: float) -> None:

        deadline = time.monotonic() + timeout_seconds

        while time.monotonic() < deadline:

            if len(self._tab_items(hwnd)) >= expected:

                return

            time.sleep(0.05)

        raise TimeoutError(

            f"Timed out waiting for Edge tab count >= {expected}."

        )



    def _navigate_uia(self, hwnd: int, url: str) -> None:

        window = self._helper._uia_window(hwnd)

        address = None

        for element in window.descendants(control_type="Edit"):

            try:

                if element.element_info.automation_id == "view_1017":

                    address = element

                    break

            except Exception:

                continue

        if address is None:

            raise RuntimeError("Edge address bar was not found through UI Automation.")



        self._set_value(address, url)

        host = (urlparse(url).hostname or url).casefold()

        deadline = time.monotonic() + 5.0

        chosen = None

        while time.monotonic() < deadline:

            window = self._helper._uia_window(hwnd)

            candidates = []

            for element in window.descendants(control_type="ListItem"):

                try:

                    if (element.element_info.class_name or "") != "OmniboxResultView":

                        continue

                    name = (element.element_info.name or "").strip()

                    if not name:

                        continue

                    candidates.append((element, name))

                except Exception:

                    continue



            exactish = [

                element

                for element, name in candidates

                if host in name.casefold()

            ]

            if exactish:

                chosen = exactish[0]

                break

            time.sleep(0.05)



        if chosen is None:

            raise RuntimeError(

                f"Edge omnibox did not expose a result for {url!r}."

            )



        self._invoke(chosen)



        deadline = time.monotonic() + self.create_timeout_seconds

        while time.monotonic() < deadline:

            try:

                current = self._helper._get_url(hwnd).casefold()

                if host in current:

                    return

            except Exception:

                pass

            time.sleep(0.1)

        raise TimeoutError(f"Timed out navigating Edge to {url!r}.")



    def _submission_observed_after_invoke_error(
        self,
        hwnd: int,
        composer,
        prompt: str,
        timeout_seconds: float = 2.5,
    ) -> bool:
        """Confirm a possibly-successful UIA Invoke without replaying the send."""

        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        while time.monotonic() < deadline:
            try:
                if self._helper._wait_for_prompt_echo(
                    hwnd,
                    prompt,
                    timeout_seconds=0.25,
                ):
                    return True
            except Exception:
                pass

            try:
                current = str(composer.get_value()).strip()
                # _set_value() verified the exact prompt before Invoke. ChatGPT
                # clearing the composer is therefore strong evidence the send landed.
                if current == "":
                    return True
            except Exception:
                pass

            try:
                is_generating = getattr(self._helper, "_is_generating", None)
                if callable(is_generating) and is_generating(hwnd):
                    return True
            except Exception:
                pass

            time.sleep(0.05)

        return False


    def _wait_for_send_button(self, hwnd: int, timeout_seconds: float):

        deadline = time.monotonic() + float(timeout_seconds)

        while time.monotonic() < deadline:

            element = self._find_send_button(hwnd)

            if element is not None:

                return element

            time.sleep(0.1)

        raise RuntimeError(

            "ChatGPT Send button was not found through UI Automation."

        )



    def _find_send_button(self, hwnd: int):

        surface = self._helper._main_surface(hwnd)

        if surface is None:

            return None



        fallback = None

        for element in surface.descendants(control_type="Button"):

            try:

                name = (element.element_info.name or "").strip().casefold()

                if name in {"send", "gửi"}:

                    return element

                if name.startswith("send ") or name.startswith("gửi "):

                    fallback = fallback or element

            except Exception:

                continue

        return fallback



    def _set_value(self, element, value: str) -> None:

        element.iface_value.SetValue(value)

        deadline = time.monotonic() + 2.0

        while time.monotonic() < deadline:

            try:

                current = str(element.get_value()).strip()

                if current == value:

                    return

            except Exception:

                pass

            time.sleep(0.03)

        raise RuntimeError("UI Automation ValuePattern did not retain the requested value.")



    @staticmethod

    def _invoke(element) -> None:

        element.iface_invoke.Invoke()



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
