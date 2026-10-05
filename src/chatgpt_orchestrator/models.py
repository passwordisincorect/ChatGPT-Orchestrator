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
    cdp_profile_dir: str = "data/edge-cdp-profile"
    chat_url: str = "https://chatgpt.com/"
    create_timeout_seconds: float = 20.0
    send_timeout_seconds: float = 120.0
    stable_seconds: float = 3.0
    default_job_timeout_seconds: float = 120.0
    default_max_retries: int = 0
    hybrid_cdp_enabled: bool = True
    dom_broker_endpoint: str = "http://127.0.0.1:8765/api/dom"
    dom_broker_token_file: str = r"D:\MCP-Test\.chatgpt-dom-broker-token"
    dom_uia_fallback_enabled: bool = True
    dom_tab_pool_only: bool = False
    dom_tab_pool_size: int = 5
    dom_idle_shutdown_seconds: float = 0.0
