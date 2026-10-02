from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    REVIEWING = "REVIEWING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class WorkerStatus(StrEnum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    CLOSED = "CLOSED"
    ERROR = "ERROR"


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"


class JobKind(StrEnum):
    WORKER = "worker"
    REVIEW = "review"


TERMINAL_JOB_STATUSES = {
    JobStatus.COMPLETED,
    JobStatus.ERROR,
    JobStatus.TIMED_OUT,
    JobStatus.CANCELLED,
}


@dataclass(frozen=True)
class Settings:
    backend: str
    max_active_workers: int
    database_path: str
    edge_executable: str = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    chat_url: str = "https://chatgpt.com/"
    create_timeout_seconds: float = 20.0
    send_timeout_seconds: float = 120.0
    stable_seconds: float = 3.0
    default_job_timeout_seconds: float = 120.0
    default_max_retries: int = 0
