from __future__ import annotations

import threading
from dataclasses import dataclass, field
from uuid import uuid4


class WorkerAdapter:
    def create(self, role: str) -> str:
        raise NotImplementedError

    def send(
        self,
        session_id: str,
        prompt: str,
        *,
        cancel_event: threading.Event | None = None,
        timeout_seconds: float | None = None,
    ) -> str:
        raise NotImplementedError

    def cancel(self, session_id: str) -> bool:
        return False

    def close(self, session_id: str) -> None:
        raise NotImplementedError


@dataclass
class SimulatedAdapter(WorkerAdapter):
    sessions: dict[str, dict[str, str]] = field(default_factory=dict)

    def create(self, role: str) -> str:
        session_id = "SIM-" + uuid4().hex[:12]
        self.sessions[session_id] = {"role": role}
        return session_id

    def send(
        self,
        session_id: str,
        prompt: str,
        *,
        cancel_event: threading.Event | None = None,
        timeout_seconds: float | None = None,
    ) -> str:
        del timeout_seconds
        if session_id not in self.sessions:
            raise KeyError(f"Unknown simulated session: {session_id}")
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("Worker job was cancelled.")
        role = self.sessions[session_id]["role"]
        return f"[SIMULATED {role}] Completed: {prompt}"

    def cancel(self, session_id: str) -> bool:
        return session_id in self.sessions

    def close(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)
