from __future__ import annotations

import json
from pathlib import Path
import threading
import time
from typing import Any
from uuid import uuid4

from .adapters import AmbiguousSubmissionError, WorkerAdapter
from .edge_cdp_adapter import EdgeCDPAdapter
from .edge_tabs_adapter import EdgeSharedTabsAdapter


class EdgeHybridAdapter(WorkerAdapter):
    """Background-first Edge transport with a UIA-only safety fallback.

    CDP is selected only after a readiness probe succeeds. A worker can move
    from CDP to UIA only before prompt submission. Once a state-changing CDP
    send begins, errors are surfaced instead of replaying the prompt through
    another transport.
    """

    def __init__(
        self,
        executable: str,
        profile_dir: str,
        chat_url: str = "https://chatgpt.com/",
        create_timeout_seconds: float = 20.0,
        send_timeout_seconds: float = 120.0,
        stable_seconds: float = 3.0,
        *,
        cdp_enabled: bool = True,
        cdp_adapter: WorkerAdapter | None = None,
        fallback_adapter: WorkerAdapter | None = None,
        status_path: str | None = None,
    ) -> None:
        self.cdp_enabled = bool(cdp_enabled)
        self._cdp = cdp_adapter or EdgeCDPAdapter(
            executable=executable,
            profile_dir=profile_dir,
            chat_url=chat_url,
            create_timeout_seconds=create_timeout_seconds,
            send_timeout_seconds=send_timeout_seconds,
            stable_seconds=stable_seconds,
        )
        self._fallback = fallback_adapter or EdgeSharedTabsAdapter(
            executable=executable,
            chat_url=chat_url,
            create_timeout_seconds=create_timeout_seconds,
            send_timeout_seconds=send_timeout_seconds,
            stable_seconds=stable_seconds,
        )
        self._lock = threading.RLock()
        self._sessions: dict[str, dict[str, str]] = {}
        self._last_cdp_error: str | None = None
        self._last_cdp_error_code: str | None = None
        self._transitions: list[dict[str, str]] = []
        self._metrics: dict[str, int] = {
            "sessions_created_total": 0,
            "cdp_sessions_total": 0,
            "uia_fallback_sessions_total": 0,
            "sessions_closed_total": 0,
            "send_success_total": 0,
            "send_failure_total": 0,
            "send_cancelled_total": 0,
            "ambiguous_send_failure_total": 0,
            "cdp_preflight_failure_total": 0,
            "all_backends_failed_total": 0,
        }
        self._status_path = Path(status_path) if status_path else None
        self._persist_runtime_status()

    def create(self, role: str) -> str:
        if self.cdp_enabled:
            inner_session: str | None = None
            try:
                inner_session = self._cdp.create(role)
                ready, code, reason = self._cdp_ready(inner_session)
                if not ready:
                    raise RuntimeError(f"{code}: {reason}")
                return self._register(
                    role,
                    "cdp",
                    inner_session,
                    transition_code="cdp_selected",
                    transition_detail="CDP readiness preflight passed.",
                )
            except Exception as exc:
                code = self._classify_cdp_failure(exc)
                self._increment_metric("cdp_preflight_failure_total")
                self._set_cdp_error(code, exc)
                if inner_session is not None:
                    self._close_best_effort(self._cdp, inner_session)
        else:
            self._last_cdp_error_code = "cdp_disabled"
            self._last_cdp_error = "CDP is disabled by configuration."

        try:
            inner_session = self._fallback.create(role)
        except Exception as exc:
            self._increment_metric("all_backends_failed_total")
            self._record_transition("all_backends_failed", str(exc))
            raise RuntimeError(
                "all_backends_failed: CDP was unavailable/not ready and "
                f"UIA fallback failed: {type(exc).__name__}: {exc}"
            ) from exc

        return self._register(
            role,
            "uia",
            inner_session,
            transition_code="fallback_uia_active",
            transition_detail=(
                self._last_cdp_error or "CDP disabled or unavailable."
            ),
        )

    def send(
        self,
        session_id: str,
        prompt: str,
        *,
        cancel_event: threading.Event | None = None,
        timeout_seconds: float | None = None,
    ) -> str:
        route = self._route(session_id)

        if route["mode"] == "cdp":
            ready, code, reason = self._cdp_ready(route["inner_session"])
            if not ready:
                self._increment_metric("cdp_preflight_failure_total")
                self._last_cdp_error_code = code
                self._last_cdp_error = reason
                route = self._migrate_to_fallback(
                    session_id,
                    route,
                    code,
                    reason,
                )

        adapter = self._cdp if route["mode"] == "cdp" else self._fallback

        try:
            result = adapter.send(
                route["inner_session"],
                prompt,
                cancel_event=cancel_event,
                timeout_seconds=timeout_seconds,
            )
        except InterruptedError:
            self._increment_metric("send_cancelled_total")
            raise
        except AmbiguousSubmissionError:
            self._increment_metric("send_failure_total")
            self._increment_metric("ambiguous_send_failure_total")
            raise
        except Exception as exc:
            self._increment_metric("send_failure_total")
            if route["mode"] != "cdp":
                raise

            # Deliberately no transport fallback after a state-changing CDP
            # send starts. ChatGPT may already have accepted the prompt, so
            # this failure is non-retryable at both adapter and job layers.
            self._increment_metric("ambiguous_send_failure_total")
            code = self._classify_cdp_failure(exc)
            self._set_cdp_error(code, exc)
            detail = (
                f"{type(exc).__name__}: {exc}. "
                "CDP send had already started, so automatic replay is disabled."
            )
            self._record_transition(
                "cdp_send_ambiguous",
                detail,
                session_id=session_id,
                from_backend="cdp_background",
                to_backend="cdp_background",
            )
            raise AmbiguousSubmissionError(
                "cdp_send_ambiguous: CDP send failed after submission may "
                "have begun; automatic retry/replay is disabled."
            ) from exc

        self._increment_metric("send_success_total")
        return result

    def cancel(self, session_id: str) -> bool:
        route = self._route(session_id)
        adapter = self._cdp if route["mode"] == "cdp" else self._fallback
        return bool(adapter.cancel(route["inner_session"]))

    def close(self, session_id: str) -> None:
        token = self._parse_session(session_id)
        with self._lock:
            route = self._sessions.pop(token, None)
        if route is None:
            return
        adapter = self._cdp if route["mode"] == "cdp" else self._fallback
        try:
            adapter.close(route["inner_session"])
        finally:
            self._increment_metric("sessions_closed_total")
            self._persist_runtime_status()

    def inspect(self, session_id: str) -> dict[str, Any]:
        route = self._route(session_id)
        adapter = self._cdp if route["mode"] == "cdp" else self._fallback
        inspect = getattr(adapter, "inspect", None)
        inner: dict[str, Any] = {}
        if callable(inspect):
            inner = dict(inspect(route["inner_session"]))
        return {
            "session_id": session_id,
            "mode": (
                "hybrid_cdp_background"
                if route["mode"] == "cdp"
                else "hybrid_uia_fallback"
            ),
            "transport": route["mode"],
            "inner_session": route["inner_session"],
            "background": route["mode"] == "cdp",
            "fallback_active": route["mode"] == "uia",
            "failure_code": (
                None if route["mode"] == "cdp" else "fallback_uia_active"
            ),
            "last_cdp_error_code": self._last_cdp_error_code,
            "last_cdp_error": self._last_cdp_error,
            "inner": inner,
        }

    def profile_status(self) -> dict[str, Any]:
        if not self.cdp_enabled:
            cdp_status: dict[str, Any] = {
                "ready": False,
                "configured": False,
                "failure_code": "cdp_disabled",
            }
        else:
            status = getattr(self._cdp, "profile_status", None)
            if callable(status):
                try:
                    cdp_status = dict(status())
                except Exception as exc:
                    code = self._classify_cdp_failure(exc)
                    self._set_cdp_error(code, exc)
                    cdp_status = {
                        "ready": False,
                        "configured": True,
                        "failure_code": code,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
            else:
                cdp_status = {
                    "ready": True,
                    "configured": True,
                    "mode": "injected",
                }

        fallback_status: dict[str, Any] = {
            "mode": "shared_tabs_uia",
            "ready": True,
        }
        fallback_profile_status = getattr(self._fallback, "profile_status", None)
        if callable(fallback_profile_status):
            try:
                fallback_status = dict(fallback_profile_status())
            except Exception as exc:
                fallback_status = {
                    "mode": "shared_tabs_uia",
                    "ready": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }

        with self._lock:
            active_cdp = sum(
                1 for item in self._sessions.values() if item["mode"] == "cdp"
            )
            active_uia = sum(
                1 for item in self._sessions.values() if item["mode"] == "uia"
            )
            transitions = list(self._transitions[-20:])
            metrics = dict(self._metrics)

        completed_sends = (
            metrics["send_success_total"]
            + metrics["send_failure_total"]
            + metrics["send_cancelled_total"]
        )
        send_success_rate = (
            round(metrics["send_success_total"] / completed_sends, 4)
            if completed_sends
            else None
        )
        clean_idle = active_cdp == 0 and active_uia == 0

        cdp_ready = bool(cdp_status.get("ready"))
        preferred = "cdp_background" if self.cdp_enabled else "uia_fallback"
        if active_cdp and active_uia:
            selected = "mixed"
        elif active_cdp:
            selected = "cdp_background"
        elif active_uia:
            selected = "uia_fallback"
        elif self.cdp_enabled and cdp_ready:
            selected = "cdp_background"
        else:
            selected = "uia_fallback"

        degraded = bool(
            self.cdp_enabled
            and (
                not cdp_ready
                or active_uia > 0
            )
        )
        degraded_reason = None
        if degraded:
            degraded_reason = (
                self._last_cdp_error_code
                or cdp_status.get("failure_code")
                or "fallback_uia_active"
            )

        return {
            "mode": "hybrid_background_first",
            "ready": True,
            "primary": "cdp_background",
            "fallback": "shared_tabs_uia",
            "preferred_backend": preferred,
            "effective_backend": selected,
            "cdp_supported": True,
            "cdp_configured": self.cdp_enabled,
            "cdp_ready": cdp_ready,
            "uia_fallback_available": bool(
                fallback_status.get("ready", True)
            ),
            "uia_status": fallback_status,
            "selected_backend": selected,
            "degraded": degraded,
            "degraded_reason": degraded_reason,
            "cdp_status": cdp_status,
            "active_cdp_sessions": active_cdp,
            "active_uia_sessions": active_uia,
            "last_cdp_error_code": self._last_cdp_error_code,
            "last_cdp_error": self._last_cdp_error,
            "last_transition": transitions[-1] if transitions else None,
            "recent_transitions": transitions,
            "runtime_metrics": metrics,
            "stability": {
                "clean_idle": clean_idle,
                "completed_send_count": completed_sends,
                "send_success_rate": send_success_rate,
            },
            "replay_on_cdp_send_failure": False,
        }

    def _register(
        self,
        role: str,
        mode: str,
        inner_session: str,
        *,
        transition_code: str,
        transition_detail: str,
    ) -> str:
        token = uuid4().hex[:16]
        session_id = f"hybrid:{token}"
        with self._lock:
            self._sessions[token] = {
                "mode": mode,
                "inner_session": inner_session,
                "role": role,
            }
            self._metrics["sessions_created_total"] += 1
            if mode == "cdp":
                self._metrics["cdp_sessions_total"] += 1
            else:
                self._metrics["uia_fallback_sessions_total"] += 1
        self._record_transition(
            transition_code,
            transition_detail,
            session_id=session_id,
            from_backend="none",
            to_backend=(
                "cdp_background"
                if mode == "cdp"
                else "uia_fallback"
            ),
        )
        return session_id

    def _cdp_ready(
        self,
        inner_session: str,
    ) -> tuple[bool, str, str | None]:
        inspect = getattr(self._cdp, "inspect", None)
        if not callable(inspect):
            return True, "cdp_ready", None
        try:
            state = dict(inspect(inner_session))
        except Exception as exc:
            code = self._classify_cdp_failure(exc)
            return False, code, f"{type(exc).__name__}: {exc}"

        if state.get("challenge_detected"):
            return False, "cdp_challenged", (
                "ChatGPT/Cloudflare challenge detected; challenge bypass is not attempted."
            )
        if state.get("authenticated") is False:
            return False, "cdp_unauthenticated", "CDP profile is not authenticated."
        if state.get("composer_found") is False:
            title = str(state.get("title") or "")
            url = str(state.get("url") or "")
            detail = title or url or "composer unavailable"
            return False, "cdp_operation_failed", (
                f"ChatGPT composer is unavailable in CDP target: {detail}"
            )
        return True, "cdp_ready", None

    def _migrate_to_fallback(
        self,
        session_id: str,
        route: dict[str, str],
        code: str,
        reason: str | None,
    ) -> dict[str, str]:
        token = self._parse_session(session_id)
        with self._lock:
            current = self._sessions.get(token)
            if current is None:
                raise KeyError(f"Unknown hybrid session: {session_id}")
            if current["mode"] != "cdp":
                return dict(current)
            role = current["role"]
            old_inner = current["inner_session"]

        try:
            new_inner = self._fallback.create(role)
        except Exception as exc:
            self._increment_metric("all_backends_failed_total")
            self._record_transition("all_backends_failed", str(exc))
            raise RuntimeError(
                "all_backends_failed: CDP preflight failed and UIA fallback "
                f"could not be created: {type(exc).__name__}: {exc}"
            ) from exc

        replacement = {
            "mode": "uia",
            "inner_session": new_inner,
            "role": role,
        }
        with self._lock:
            current = self._sessions.get(token)
            if current is None:
                self._close_best_effort(self._fallback, new_inner)
                raise KeyError(f"Unknown hybrid session: {session_id}")
            if current["mode"] != "cdp":
                self._close_best_effort(self._fallback, new_inner)
                return dict(current)
            self._sessions[token] = replacement
            self._metrics["uia_fallback_sessions_total"] += 1

        self._close_best_effort(self._cdp, old_inner)
        self._record_transition(
            "fallback_uia_active",
            f"{code}: {reason or 'CDP preflight failed'}",
            session_id=session_id,
            from_backend="cdp_background",
            to_backend="uia_fallback",
        )
        return replacement

    def _route(self, session_id: str) -> dict[str, str]:
        token = self._parse_session(session_id)
        with self._lock:
            route = self._sessions.get(token)
            if route is None:
                raise KeyError(f"Unknown hybrid session: {session_id}")
            return dict(route)

    def _set_cdp_error(self, code: str, exc: Exception) -> None:
        self._last_cdp_error_code = code
        self._last_cdp_error = f"{type(exc).__name__}: {exc}"
        self._record_transition(code, self._last_cdp_error)

    def _increment_metric(self, name: str, amount: int = 1) -> None:
        with self._lock:
            if name not in self._metrics:
                self._metrics[name] = 0
            self._metrics[name] += int(amount)
        self._persist_runtime_status()

    def _record_transition(
        self,
        code: str,
        detail: str,
        *,
        session_id: str | None = None,
        from_backend: str | None = None,
        to_backend: str | None = None,
    ) -> None:
        event = {
            "at_unix": str(round(time.time(), 3)),
            "code": code,
            "detail": str(detail)[:500],
            "session_id": session_id or "",
            "from_backend": from_backend or "",
            "to_backend": to_backend or "",
        }
        with self._lock:
            self._transitions.append(event)
            if len(self._transitions) > 20:
                del self._transitions[:-20]
        self._persist_runtime_status()

    def _persist_runtime_status(self) -> None:
        if self._status_path is None:
            return
        try:
            with self._lock:
                active_cdp = sum(1 for item in self._sessions.values() if item["mode"] == "cdp")
                active_uia = sum(1 for item in self._sessions.values() if item["mode"] == "uia")
                if active_cdp and active_uia:
                    selected = "mixed"
                elif active_cdp:
                    selected = "cdp_background"
                elif active_uia:
                    selected = "uia_fallback"
                else:
                    selected = "idle"
                payload = {
                    "updated_at_unix": time.time(),
                    "selected_backend": selected,
                    "active_cdp_sessions": active_cdp,
                    "active_uia_sessions": active_uia,
                    "last_cdp_error_code": self._last_cdp_error_code,
                    "last_cdp_error": self._last_cdp_error,
                    "recent_transitions": list(self._transitions[-20:]),
                    "runtime_metrics": dict(self._metrics),
                }
            self._status_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self._status_path.with_suffix(self._status_path.suffix + ".tmp")
            temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self._status_path)
        except Exception:
            pass

    @staticmethod
    def _classify_cdp_failure(exc: Exception) -> str:
        text = f"{type(exc).__name__}: {exc}".casefold()
        if any(x in text for x in ("just a moment", "challenge", "cloudflare", "captcha")):
            return "cdp_challenged"
        if any(x in text for x in ("not signed in", "unauthenticated", "log in")):
            return "cdp_unauthenticated"
        if "cdp_send_ambiguous" in text:
            return "cdp_send_ambiguous"
        if "target_not_owned" in text:
            return "cdp_target_not_owned"
        if any(x in text for x in ("target is no longer available", "target_missing")):
            return "cdp_target_missing"
        if any(
            x in text
            for x in (
                "connection refused",
                "websocket",
                "devtoolsactiveport",
                "debugging endpoint",
                "connect failed",
            )
        ):
            return "cdp_connect_failed"
        if any(x in text for x in ("executable not found", "cdp_unavailable")):
            return "cdp_unavailable"
        return "cdp_operation_failed"

    @staticmethod
    def _close_best_effort(adapter: WorkerAdapter, session_id: str) -> None:
        try:
            adapter.close(session_id)
        except Exception:
            pass

    @staticmethod
    def _parse_session(session_id: str) -> str:
        prefix = "hybrid:"
        value = str(session_id)
        if not value.startswith(prefix):
            raise ValueError(f"Invalid hybrid session id: {session_id}")
        token = value[len(prefix):].strip()
        if not token:
            raise ValueError("Hybrid session token is empty.")
        return token
