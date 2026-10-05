from __future__ import annotations

import json
from pathlib import Path
import threading
import time
from typing import Any
from uuid import uuid4

from .actuator_dom_adapter import ActuatorDOMAdapter, DOMPreSubmissionError
from .adapters import AmbiguousSubmissionError, WorkerAdapter
from .edge_tabs_adapter import EdgeSharedTabsAdapter


class DOMUIAHybridAdapter(WorkerAdapter):
    """Actuator DOM primary with serialized shared-tab UIA fallback."""

    def __init__(
        self,
        executable: str,
        chat_url: str = "https://chatgpt.com/",
        create_timeout_seconds: float = 20.0,
        send_timeout_seconds: float = 120.0,
        stable_seconds: float = 3.0,
        *,
        broker_endpoint: str = "http://127.0.0.1:8765/api/dom",
        broker_token_file: str = r"D:\MCP-Test\.chatgpt-dom-broker-token",
        dom_adapter: WorkerAdapter | None = None,
        fallback_adapter: WorkerAdapter | None = None,
        uia_fallback_enabled: bool = True,
        dom_tab_pool_only: bool = False,
        dom_tab_pool_size: int = 5,
        dom_idle_shutdown_seconds: float = 0.0,
        status_path: str | None = None,
    ) -> None:
        self._dom = dom_adapter or ActuatorDOMAdapter(
            chat_url=chat_url,
            create_timeout_seconds=create_timeout_seconds,
            send_timeout_seconds=send_timeout_seconds,
            stable_seconds=stable_seconds,
            broker_endpoint=broker_endpoint,
            broker_token_file=broker_token_file,
            tab_pool_only=dom_tab_pool_only,
            tab_pool_size=dom_tab_pool_size,
            idle_shutdown_seconds=dom_idle_shutdown_seconds,
        )
        self._uia_fallback_enabled = bool(uia_fallback_enabled)
        self._fallback = fallback_adapter or EdgeSharedTabsAdapter(
            executable=executable,
            chat_url=chat_url,
            create_timeout_seconds=create_timeout_seconds,
            send_timeout_seconds=send_timeout_seconds,
            stable_seconds=stable_seconds,
        )
        self._lock = threading.RLock()
        self._sessions: dict[str, dict[str, str]] = {}
        self._transitions: list[dict[str, str]] = []
        self._last_dom_error: str | None = None
        self._metrics = {
            "sessions_created_total": 0,
            "dom_sessions_total": 0,
            "uia_fallback_sessions_total": 0,
            "sessions_closed_total": 0,
            "send_success_total": 0,
            "send_failure_total": 0,
            "send_cancelled_total": 0,
            "ambiguous_send_failure_total": 0,
            "dom_pre_submission_fallback_total": 0,
            "all_backends_failed_total": 0,
            "uia_fallback_blocked_total": 0,
        }
        self._status_path = Path(status_path) if status_path else None
        self._persist_runtime_status()

    def create(self, role: str) -> str:
        try:
            inner = self._dom.create(role)
            return self._register(
                role,
                "dom",
                inner,
                "dom_broker_selected",
                "Actuator shared DOM/CDP broker is ready.",
            )
        except Exception as exc:
            self._last_dom_error = f"{type(exc).__name__}: {exc}"

        if not self._uia_fallback_enabled:
            self._inc("uia_fallback_blocked_total")
            raise RuntimeError(
                "dom_primary_required: Actuator DOM broker is unavailable and "
                "automatic UIA fallback is disabled by strict background policy. "
                f"{self._last_dom_error}"
            )

        try:
            inner = self._fallback.create(role)
        except Exception as exc:
            self._inc("all_backends_failed_total")
            raise RuntimeError(
                "all_backends_failed: DOM broker create failed and UIA fallback "
                f"also failed: {type(exc).__name__}: {exc}"
            ) from exc
        return self._register(
            role,
            "uia",
            inner,
            "fallback_uia_active",
            self._last_dom_error or "DOM broker unavailable.",
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
        adapter = self._dom if route["mode"] == "dom" else self._fallback

        try:
            result = adapter.send(
                route["inner_session"],
                prompt,
                cancel_event=cancel_event,
                timeout_seconds=timeout_seconds,
            )
        except DOMPreSubmissionError as exc:
            if route["mode"] != "dom":
                self._inc("send_failure_total")
                raise
            self._last_dom_error = f"{type(exc).__name__}: {exc}"
            if not self._uia_fallback_enabled:
                self._inc("send_failure_total")
                self._inc("uia_fallback_blocked_total")
                raise
            self._inc("dom_pre_submission_fallback_total")
            route = self._migrate_to_uia(session_id, route, self._last_dom_error)
            try:
                result = self._fallback.send(
                    route["inner_session"],
                    prompt,
                    cancel_event=cancel_event,
                    timeout_seconds=timeout_seconds,
                )
            except Exception:
                self._inc("send_failure_total")
                raise
        except InterruptedError:
            self._inc("send_cancelled_total")
            raise
        except AmbiguousSubmissionError:
            self._inc("send_failure_total")
            self._inc("ambiguous_send_failure_total")
            raise
        except Exception:
            self._inc("send_failure_total")
            raise

        self._inc("send_success_total")
        return result

    def cancel(self, session_id: str) -> bool:
        route = self._route(session_id)
        adapter = self._dom if route["mode"] == "dom" else self._fallback
        return bool(adapter.cancel(route["inner_session"]))

    def close(self, session_id: str) -> None:
        token = self._parse(session_id)
        with self._lock:
            route = self._sessions.pop(token, None)
        if route is None:
            return
        adapter = self._dom if route["mode"] == "dom" else self._fallback
        try:
            adapter.close(route["inner_session"])
        finally:
            self._inc("sessions_closed_total")
            self._persist_runtime_status()

    def inspect(self, session_id: str) -> dict[str, Any]:
        route = self._route(session_id)
        adapter = self._dom if route["mode"] == "dom" else self._fallback
        inspect = getattr(adapter, "inspect", None)
        inner = dict(inspect(route["inner_session"])) if callable(inspect) else {}
        return {
            "session_id": session_id,
            "mode": "dom_primary_uia_fallback",
            "transport": route["mode"],
            "inner_session": route["inner_session"],
            "background": route["mode"] == "dom",
            "fallback_active": route["mode"] == "uia",
            "inner": inner,
        }

    def profile_status(self) -> dict[str, Any]:
        dom_status = self._status_of(self._dom, "actuator_dom_broker")
        uia_status = self._status_of(self._fallback, "shared_tabs_uia")
        with self._lock:
            active_dom = sum(1 for x in self._sessions.values() if x["mode"] == "dom")
            active_uia = sum(1 for x in self._sessions.values() if x["mode"] == "uia")
            metrics = dict(self._metrics)
            transitions = list(self._transitions[-20:])

        if active_dom and active_uia:
            selected = "mixed"
        elif active_dom:
            selected = "actuator_dom_broker"
        elif active_uia:
            selected = "uia_fallback"
        elif dom_status.get("ready"):
            selected = "actuator_dom_broker"
        elif self._uia_fallback_enabled:
            selected = "uia_fallback"
        else:
            selected = "unavailable"

        completed = (
            metrics["send_success_total"]
            + metrics["send_failure_total"]
            + metrics["send_cancelled_total"]
        )
        success_rate = (
            round(metrics["send_success_total"] / completed, 4)
            if completed else None
        )
        return {
            "mode": "dom_primary_uia_fallback",
            "ready": bool(
                dom_status.get("ready")
                or (
                    self._uia_fallback_enabled
                    and uia_status.get("ready", True)
                )
            ),
            "primary": "actuator_dom_broker",
            "fallback": "shared_tabs_uia" if self._uia_fallback_enabled else "disabled",
            "preferred_backend": "actuator_dom_broker",
            "effective_backend": selected,
            "selected_backend": selected,
            "dom_ready": bool(dom_status.get("ready")),
            "dom_status": dom_status,
            "uia_fallback_enabled": self._uia_fallback_enabled,
            "uia_fallback_available": bool(
                self._uia_fallback_enabled and uia_status.get("ready", True)
            ),
            "uia_status": uia_status,
            "active_dom_sessions": active_dom,
            "active_uia_sessions": active_uia,
            "shared_cdp_owner": "ChatGPT-Actuator",
            "last_dom_error": self._last_dom_error,
            "recent_transitions": transitions,
            "runtime_metrics": metrics,
            "stability": {
                "clean_idle": active_dom == 0 and active_uia == 0,
                "completed_send_count": completed,
                "send_success_rate": success_rate,
            },
            "replay_on_dom_send_failure": False,
        }

    def _migrate_to_uia(
        self,
        session_id: str,
        route: dict[str, str],
        reason: str,
    ) -> dict[str, str]:
        if not self._uia_fallback_enabled:
            self._inc("uia_fallback_blocked_total")
            raise RuntimeError(
                "UIA fallback is disabled by strict background policy."
            )
        token = self._parse(session_id)
        role = route["role"]
        new_inner = self._fallback.create(role)
        replacement = {"mode": "uia", "inner_session": new_inner, "role": role}
        with self._lock:
            current = self._sessions.get(token)
            if current is None:
                self._close_best_effort(self._fallback, new_inner)
                raise KeyError(f"Unknown DOM hybrid session: {session_id}")
            if current["mode"] != "dom":
                self._close_best_effort(self._fallback, new_inner)
                return dict(current)
            self._sessions[token] = replacement
            self._metrics["uia_fallback_sessions_total"] += 1

        self._close_best_effort(self._dom, route["inner_session"])
        self._record(
            "fallback_uia_active",
            reason,
            session_id=session_id,
            from_backend="actuator_dom_broker",
            to_backend="uia_fallback",
        )
        return replacement

    def _register(
        self,
        role: str,
        mode: str,
        inner_session: str,
        code: str,
        detail: str,
    ) -> str:
        token = uuid4().hex[:16]
        session_id = f"domhybrid:{token}"
        with self._lock:
            self._sessions[token] = {
                "mode": mode,
                "inner_session": inner_session,
                "role": str(role),
            }
            self._metrics["sessions_created_total"] += 1
            if mode == "dom":
                self._metrics["dom_sessions_total"] += 1
            else:
                self._metrics["uia_fallback_sessions_total"] += 1
        self._record(
            code,
            detail,
            session_id=session_id,
            from_backend="none",
            to_backend=(
                "actuator_dom_broker" if mode == "dom" else "uia_fallback"
            ),
        )
        return session_id

    def _route(self, session_id: str) -> dict[str, str]:
        token = self._parse(session_id)
        with self._lock:
            route = self._sessions.get(token)
            if route is None:
                raise KeyError(f"Unknown DOM hybrid session: {session_id}")
            return dict(route)

    @staticmethod
    def _status_of(adapter: WorkerAdapter, mode: str) -> dict[str, Any]:
        fn = getattr(adapter, "profile_status", None)
        if not callable(fn):
            return {"mode": mode, "ready": True}
        try:
            return dict(fn())
        except Exception as exc:
            return {
                "mode": mode,
                "ready": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _inc(self, name: str) -> None:
        with self._lock:
            self._metrics[name] = self._metrics.get(name, 0) + 1
        self._persist_runtime_status()

    def _record(
        self,
        code: str,
        detail: str,
        *,
        session_id: str = "",
        from_backend: str = "",
        to_backend: str = "",
    ) -> None:
        event = {
            "at_unix": str(round(time.time(), 3)),
            "code": str(code),
            "detail": str(detail)[:500],
            "session_id": session_id,
            "from_backend": from_backend,
            "to_backend": to_backend,
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
            status = self.profile_status()
            payload = {
                "updated_at_unix": time.time(),
                "selected_backend": status["selected_backend"],
                "active_dom_sessions": status["active_dom_sessions"],
                "active_uia_sessions": status["active_uia_sessions"],
                "last_dom_error": status["last_dom_error"],
                "recent_transitions": status["recent_transitions"],
                "runtime_metrics": status["runtime_metrics"],
            }
            self._status_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self._status_path.with_suffix(self._status_path.suffix + ".tmp")
            temp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temp.replace(self._status_path)
        except Exception:
            pass

    @staticmethod
    def _close_best_effort(adapter: WorkerAdapter, session_id: str) -> None:
        try:
            adapter.close(session_id)
        except Exception:
            pass

    @staticmethod
    def _parse(session_id: str) -> str:
        prefix = "domhybrid:"
        value = str(session_id)
        if not value.startswith(prefix):
            raise ValueError(f"Invalid DOM hybrid session id: {session_id}")
        token = value[len(prefix):].strip()
        if not token:
            raise ValueError("DOM hybrid session token is empty.")
        return token
