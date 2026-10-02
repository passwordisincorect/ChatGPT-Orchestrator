from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import JobKind, JobStatus, TaskStatus, WorkerStatus


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class Store:
    def __init__(self, database_path: str):
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        self._migrate_schema()
        self.recover_incomplete_jobs()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=10.0,
            factory=ClosingConnection,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as con:
            con.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    goal TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS workers (
                    id TEXT PRIMARY KEY,
                    task_id TEXT,
                    role TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    last_prompt TEXT,
                    last_result TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(task_id) REFERENCES tasks(id)
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    worker_id TEXT NOT NULL,
                    task_id TEXT,
                    prompt TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'worker',
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_retries INTEGER NOT NULL DEFAULT 0,
                    timeout_seconds REAL NOT NULL,
                    result TEXT,
                    error TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    started_at TEXT,
                    completed_at TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(worker_id) REFERENCES workers(id),
                    FOREIGN KEY(task_id) REFERENCES tasks(id)
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_worker ON jobs(worker_id);
                CREATE INDEX IF NOT EXISTS idx_jobs_task ON jobs(task_id);
                """
            )

    def _migrate_schema(self) -> None:
        with self._connect() as con:
            columns = {
                row["name"]
                for row in con.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "kind" not in columns:
                con.execute(
                    "ALTER TABLE jobs ADD COLUMN kind TEXT NOT NULL DEFAULT 'worker'"
                )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_kind ON jobs(kind)"
            )

    def recover_incomplete_jobs(self) -> int:
        with self._connect() as con:
            cur = con.execute(
                """UPDATE jobs
                   SET status=?, error=?, completed_at=CURRENT_TIMESTAMP,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE status IN (?, ?)""",
                (
                    JobStatus.ERROR,
                    "Orchestrator restarted before the job completed.",
                    JobStatus.QUEUED,
                    JobStatus.RUNNING,
                ),
            )
            return int(cur.rowcount)

    # ----------------------------
    # Tasks
    # ----------------------------

    def create_task(self, goal: str) -> dict[str, Any]:
        task_id = "T-" + uuid4().hex[:10]
        with self._connect() as con:
            con.execute(
                "INSERT INTO tasks(id, goal, status) VALUES(?, ?, ?)",
                (task_id, goal, TaskStatus.PENDING),
            )
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict[str, Any]:
        with self._connect() as con:
            row = con.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown task: {task_id}")
        return dict(row)

    def list_tasks(self) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute("SELECT * FROM tasks ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    def set_task_status(self, task_id: str, status: TaskStatus) -> dict[str, Any]:
        with self._connect() as con:
            cur = con.execute(
                "UPDATE tasks SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (status, task_id),
            )
            if cur.rowcount != 1:
                raise KeyError(f"Unknown task: {task_id}")
        return self.get_task(task_id)

    # ----------------------------
    # Workers
    # ----------------------------

    def create_worker(self, role: str, session_id: str, task_id: str | None) -> dict[str, Any]:
        if task_id is not None:
            self.get_task(task_id)
        worker_id = "W-" + uuid4().hex[:10]
        with self._connect() as con:
            con.execute(
                """INSERT INTO workers(id, task_id, role, session_id, status)
                   VALUES(?, ?, ?, ?, ?)""",
                (worker_id, task_id, role, session_id, WorkerStatus.IDLE),
            )
        return self.get_worker(worker_id)

    def get_worker(self, worker_id: str) -> dict[str, Any]:
        with self._connect() as con:
            row = con.execute("SELECT * FROM workers WHERE id=?", (worker_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown worker: {worker_id}")
        return dict(row)

    def list_workers(self, include_closed: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM workers"
        params: tuple[Any, ...] = ()
        if not include_closed:
            sql += " WHERE status != ?"
            params = (WorkerStatus.CLOSED,)
        sql += " ORDER BY created_at"
        with self._connect() as con:
            rows = con.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def active_worker_count(self) -> int:
        with self._connect() as con:
            row = con.execute(
                "SELECT COUNT(*) AS n FROM workers WHERE status != ?",
                (WorkerStatus.CLOSED,),
            ).fetchone()
        return int(row["n"])

    def update_worker(
        self,
        worker_id: str,
        *,
        status: WorkerStatus | None = None,
        prompt: str | None = None,
        result: str | None = None,
    ) -> dict[str, Any]:
        current = self.get_worker(worker_id)
        with self._connect() as con:
            con.execute(
                """UPDATE workers
                   SET status=?, last_prompt=?, last_result=?,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (
                    status or current["status"],
                    current["last_prompt"] if prompt is None else prompt,
                    current["last_result"] if result is None else result,
                    worker_id,
                ),
            )
        return self.get_worker(worker_id)

    # ----------------------------
    # Jobs
    # ----------------------------

    def create_job(
        self,
        worker_id: str,
        prompt: str,
        *,
        timeout_seconds: float,
        max_retries: int,
        kind: JobKind = JobKind.WORKER,
    ) -> dict[str, Any]:
        worker = self.get_worker(worker_id)
        job_id = "J-" + uuid4().hex[:12]
        with self._connect() as con:
            con.execute(
                """INSERT INTO jobs(
                       id, worker_id, task_id, prompt, kind, status,
                       max_retries, timeout_seconds
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job_id,
                    worker_id,
                    worker["task_id"],
                    prompt,
                    kind,
                    JobStatus.QUEUED,
                    int(max_retries),
                    float(timeout_seconds),
                ),
            )
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._connect() as con:
            row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown job: {job_id}")
        result = dict(row)
        result["cancel_requested"] = bool(result["cancel_requested"])
        return result

    def list_jobs(
        self,
        *,
        task_id: str | None = None,
        worker_id: str | None = None,
        kind: JobKind | str | None = None,
        include_terminal: bool = True,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []

        if task_id is not None:
            clauses.append("task_id=?")
            params.append(task_id)
        if worker_id is not None:
            clauses.append("worker_id=?")
            params.append(worker_id)
        if kind is not None:
            clauses.append("kind=?")
            params.append(str(kind))
        if not include_terminal:
            clauses.append("status IN (?, ?)")
            params.extend([JobStatus.QUEUED, JobStatus.RUNNING])

        sql = "SELECT * FROM jobs"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, id"

        with self._connect() as con:
            rows = con.execute(sql, tuple(params)).fetchall()

        results = []
        for row in rows:
            item = dict(row)
            item["cancel_requested"] = bool(item["cancel_requested"])
            results.append(item)
        return results

    def set_job_running(self, job_id: str, attempts: int) -> dict[str, Any]:
        with self._connect() as con:
            con.execute(
                """UPDATE jobs
                   SET status=?, attempts=?, started_at=COALESCE(started_at, CURRENT_TIMESTAMP),
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (JobStatus.RUNNING, int(attempts), job_id),
            )
        return self.get_job(job_id)

    def finish_job(
        self,
        job_id: str,
        status: JobStatus,
        *,
        result: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        with self._connect() as con:
            con.execute(
                """UPDATE jobs
                   SET status=?, result=?, error=?,
                       completed_at=CURRENT_TIMESTAMP,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (status, result, error, job_id),
            )
        return self.get_job(job_id)

    def request_job_cancel(self, job_id: str) -> dict[str, Any]:
        with self._connect() as con:
            con.execute(
                """UPDATE jobs
                   SET cancel_requested=1, updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (job_id,),
            )
        return self.get_job(job_id)
