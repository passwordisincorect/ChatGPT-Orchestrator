from __future__ import annotations



import hashlib
import json
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

                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    goals_json TEXT NOT NULL DEFAULT '[]',
                    decisions_json TEXT NOT NULL DEFAULT '[]',
                    current_phase TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    next_actions_json TEXT NOT NULL DEFAULT '[]',
                    state_version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_activity_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS project_task_links (
                    project_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    attached_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    attached_by TEXT NOT NULL DEFAULT 'human',
                    PRIMARY KEY(project_id, task_id),
                    FOREIGN KEY(project_id) REFERENCES projects(id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS project_events (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    task_id TEXT,
                    source TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(project_id) REFERENCES projects(id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_projects_status_activity
                    ON projects(status, last_activity_at);
                CREATE INDEX IF NOT EXISTS idx_project_links_task
                    ON project_task_links(task_id);
                CREATE INDEX IF NOT EXISTS idx_project_events_project_created
                    ON project_events(project_id, created_at);
                CREATE UNIQUE INDEX IF NOT EXISTS ux_project_task_outcome_hash
                    ON project_events(project_id, task_id, event_type, payload_hash)
                    WHERE event_type='task_outcome' AND payload_hash IS NOT NULL;
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
            queued = con.execute(
                """UPDATE jobs
                   SET status=?, error=?, completed_at=CURRENT_TIMESTAMP,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE status=?""",
                (
                    JobStatus.ERROR,
                    "Orchestrator restarted before queued job started; safe to recover.",
                    JobStatus.QUEUED,
                ),
            )
            running = con.execute(
                """UPDATE jobs
                   SET status=?, error=?, completed_at=CURRENT_TIMESTAMP,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE status=?""",
                (
                    JobStatus.ERROR,
                    "NON_RETRYABLE: Orchestrator restarted while the job was "
                    "running; submission state is ambiguous.",
                    JobStatus.RUNNING,
                ),
            )
            con.execute(
                """UPDATE workers
                   SET status=?, updated_at=CURRENT_TIMESTAMP
                   WHERE status != ?""",
                (WorkerStatus.CLOSED, WorkerStatus.CLOSED),
            )
            return int(queued.rowcount) + int(running.rowcount)


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

    # Persistent projects

    # ----------------------------

    @staticmethod
    def _project_status(value: str) -> str:
        status = str(value or "").strip().casefold()
        allowed = {"active", "paused", "completed", "archived"}
        if status not in allowed:
            raise ValueError(
                "project status must be one of: active, paused, completed, archived"
            )
        return status

    @staticmethod
    def _project_text_list(value: list[str] | tuple[str, ...] | None, field: str) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{field} must be a list of strings")
        result: list[str] = []
        for item in value:
            text = str(item).strip()
            if not text:
                continue
            if len(text) > 4000:
                raise ValueError(f"{field} entries must be <= 4000 characters")
            result.append(text)
        if len(result) > 100:
            raise ValueError(f"{field} must contain at most 100 entries")
        return result

    @staticmethod
    def _json_load_list(raw: str | None) -> list[str]:
        try:
            value = json.loads(str(raw or "[]"))
        except json.JSONDecodeError:
            return []
        if not isinstance(value, list):
            return []
        return [str(item) for item in value]

    def _decode_project_row(self, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item["project_id"] = item["id"]
        item["goals"] = self._json_load_list(item.pop("goals_json", "[]"))
        item["decisions"] = self._json_load_list(item.pop("decisions_json", "[]"))
        item["next_actions"] = self._json_load_list(
            item.pop("next_actions_json", "[]")
        )
        return item

    def create_project(
        self,
        name: str,
        *,
        summary: str = "",
        goals: list[str] | tuple[str, ...] | None = None,
        decisions: list[str] | tuple[str, ...] | None = None,
        current_phase: str = "",
        status: str = "active",
        next_actions: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        name = str(name).strip()
        if not name:
            raise ValueError("project name is required")
        if len(name) > 240:
            raise ValueError("project name must be <= 240 characters")
        summary = str(summary or "").strip()
        if len(summary) > 20000:
            raise ValueError("project summary must be <= 20000 characters")
        phase = str(current_phase or "").strip()
        if len(phase) > 500:
            raise ValueError("current_phase must be <= 500 characters")
        status_value = self._project_status(status)
        goals_value = self._project_text_list(goals, "goals")
        decisions_value = self._project_text_list(decisions, "decisions")
        actions_value = self._project_text_list(next_actions, "next_actions")

        project_id = "P-" + uuid4().hex[:10]
        event_id = "PE-" + uuid4().hex[:12]
        payload = json.dumps(
            {"state_version": 1, "changed_fields": ["project_created"]},
            ensure_ascii=False,
            sort_keys=True,
        )
        with self._connect() as con:
            con.execute(
                """INSERT INTO projects(
                       id, name, summary, goals_json, decisions_json,
                       current_phase, status, next_actions_json
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    project_id,
                    name,
                    summary,
                    json.dumps(goals_value, ensure_ascii=False),
                    json.dumps(decisions_value, ensure_ascii=False),
                    phase,
                    status_value,
                    json.dumps(actions_value, ensure_ascii=False),
                ),
            )
            con.execute(
                """INSERT INTO project_events(
                       id, project_id, event_type, source, payload_json
                   ) VALUES(?, ?, 'project_created', 'human', ?)""",
                (event_id, project_id, payload),
            )
        return self.get_project(project_id)

    def get_project(
        self,
        project_id: str,
        *,
        recent_events: int = 10,
    ) -> dict[str, Any]:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM projects WHERE id=?",
                (project_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"PROJECT_NOT_FOUND: {project_id}")
            links = con.execute(
                """SELECT task_id, attached_at, attached_by
                   FROM project_task_links
                   WHERE project_id=?
                   ORDER BY attached_at, rowid""",
                (project_id,),
            ).fetchall()
            limit = max(0, min(int(recent_events), 50))
            events: list[sqlite3.Row] = []
            if limit:
                events = con.execute(
                    """SELECT id, event_type, task_id, source, payload_json,
                              payload_hash, created_at
                       FROM project_events
                       WHERE project_id=?
                       ORDER BY created_at DESC, rowid DESC
                       LIMIT ?""",
                    (project_id, limit),
                ).fetchall()

        project = self._decode_project_row(row)
        project["related_tasks"] = [dict(item) for item in links]
        project["related_task_ids"] = [item["task_id"] for item in links]
        decoded_events: list[dict[str, Any]] = []
        for event in events:
            item = dict(event)
            try:
                item["payload"] = json.loads(item.pop("payload_json"))
            except (json.JSONDecodeError, TypeError):
                item["payload"] = {}
                item.pop("payload_json", None)
            decoded_events.append(item)
        project["recent_events"] = decoded_events
        return project

    def list_projects(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        count = max(1, min(int(limit), 200))
        params: list[Any] = []
        where = ""
        if status is not None:
            where = "WHERE p.status=?"
            params.append(self._project_status(status))
        params.append(count)
        with self._connect() as con:
            rows = con.execute(
                f"""SELECT p.id, p.name, p.status, p.current_phase,
                           p.state_version, p.created_at, p.updated_at,
                           p.last_activity_at,
                           COUNT(l.task_id) AS task_count
                    FROM projects p
                    LEFT JOIN project_task_links l ON l.project_id=p.id
                    {where}
                    GROUP BY p.id
                    ORDER BY p.last_activity_at DESC, p.created_at DESC
                    LIMIT ?""",
                tuple(params),
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["project_id"] = item["id"]
            results.append(item)
        return results

    def update_project(
        self,
        project_id: str,
        *,
        expected_version: int,
        name: str | None = None,
        summary: str | None = None,
        goals: list[str] | tuple[str, ...] | None = None,
        decisions: list[str] | tuple[str, ...] | None = None,
        current_phase: str | None = None,
        status: str | None = None,
        next_actions: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        expected = int(expected_version)
        if expected < 1:
            raise ValueError("expected_version must be >= 1")

        changes: dict[str, Any] = {}
        if name is not None:
            value = str(name).strip()
            if not value:
                raise ValueError("project name cannot be empty")
            if len(value) > 240:
                raise ValueError("project name must be <= 240 characters")
            changes["name"] = value
        if summary is not None:
            value = str(summary).strip()
            if len(value) > 20000:
                raise ValueError("project summary must be <= 20000 characters")
            changes["summary"] = value
        if goals is not None:
            changes["goals_json"] = json.dumps(
                self._project_text_list(goals, "goals"),
                ensure_ascii=False,
            )
        if decisions is not None:
            changes["decisions_json"] = json.dumps(
                self._project_text_list(decisions, "decisions"),
                ensure_ascii=False,
            )
        if current_phase is not None:
            value = str(current_phase).strip()
            if len(value) > 500:
                raise ValueError("current_phase must be <= 500 characters")
            changes["current_phase"] = value
        if status is not None:
            changes["status"] = self._project_status(status)
        if next_actions is not None:
            changes["next_actions_json"] = json.dumps(
                self._project_text_list(next_actions, "next_actions"),
                ensure_ascii=False,
            )

        if not changes:
            current = self.get_project(project_id)
            if int(current["state_version"]) != expected:
                raise RuntimeError(
                    f"PROJECT_VERSION_CONFLICT: expected {expected}, "
                    f"current {current['state_version']}"
                )
            current["changed_fields"] = []
            return current

        with self._connect() as con:
            current = con.execute(
                "SELECT state_version FROM projects WHERE id=?",
                (project_id,),
            ).fetchone()
            if current is None:
                raise KeyError(f"PROJECT_NOT_FOUND: {project_id}")
            actual = int(current["state_version"])
            if actual != expected:
                raise RuntimeError(
                    f"PROJECT_VERSION_CONFLICT: expected {expected}, current {actual}"
                )

            assignments = [f"{column}=?" for column in changes]
            params = list(changes.values())
            assignments.extend([
                "state_version=state_version+1",
                "updated_at=CURRENT_TIMESTAMP",
                "last_activity_at=CURRENT_TIMESTAMP",
            ])
            params.extend([project_id, expected])
            cur = con.execute(
                f"""UPDATE projects SET {', '.join(assignments)}
                    WHERE id=? AND state_version=?""",
                tuple(params),
            )
            if cur.rowcount != 1:
                raise RuntimeError("PROJECT_VERSION_CONFLICT")

            event_id = "PE-" + uuid4().hex[:12]
            payload = json.dumps(
                {
                    "from_version": expected,
                    "to_version": expected + 1,
                    "changed_fields": [
                        {
                            "goals_json": "goals",
                            "decisions_json": "decisions",
                            "next_actions_json": "next_actions",
                        }.get(field, field)
                        for field in changes
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            con.execute(
                """INSERT INTO project_events(
                       id, project_id, event_type, source, payload_json
                   ) VALUES(?, ?, 'project_updated', 'human', ?)""",
                (event_id, project_id, payload),
            )

        project = self.get_project(project_id)
        project["changed_fields"] = json.loads(payload)["changed_fields"]
        return project

    def attach_project_task(
        self,
        project_id: str,
        task_id: str,
        *,
        attached_by: str = "human",
    ) -> dict[str, Any]:
        self.get_project(project_id, recent_events=0)
        self.get_task(task_id)
        source = str(attached_by or "human").strip() or "human"

        with self._connect() as con:
            existing = con.execute(
                """SELECT 1 FROM project_task_links
                   WHERE project_id=? AND task_id=?""",
                (project_id, task_id),
            ).fetchone()
            if existing is not None:
                attached = False
            else:
                con.execute(
                    """INSERT INTO project_task_links(
                           project_id, task_id, attached_by
                       ) VALUES(?, ?, ?)""",
                    (project_id, task_id, source),
                )
                event_id = "PE-" + uuid4().hex[:12]
                payload = json.dumps(
                    {"task_id": task_id},
                    ensure_ascii=False,
                    sort_keys=True,
                )
                con.execute(
                    """INSERT INTO project_events(
                           id, project_id, event_type, task_id,
                           source, payload_json
                       ) VALUES(?, ?, 'task_attached', ?, ?, ?)""",
                    (event_id, project_id, task_id, source, payload),
                )
                con.execute(
                    """UPDATE projects
                       SET last_activity_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (project_id,),
                )
                attached = True

        return {
            "project_id": project_id,
            "task_id": task_id,
            "attached": attached,
        }

    def project_ids_for_task(self, task_id: str) -> list[str]:
        with self._connect() as con:
            rows = con.execute(
                """SELECT project_id FROM project_task_links
                   WHERE task_id=?
                   ORDER BY attached_at, rowid""",
                (task_id,),
            ).fetchall()
        return [str(row["project_id"]) for row in rows]

    def list_project_task_links(self) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                """SELECT project_id, task_id, attached_at, attached_by
                   FROM project_task_links
                   ORDER BY attached_at, rowid"""
            ).fetchall()
        return [dict(row) for row in rows]

    def snapshot_project_task(
        self,
        task_id: str,
        *,
        status: str,
        outcome: str | None,
        source: str = "orchestrator",
        max_outcome_chars: int = 12000,
    ) -> list[dict[str, Any]]:
        project_ids = self.project_ids_for_task(task_id)
        if not project_ids:
            return []

        full_outcome = str(outcome or "")
        limit = max(1000, min(int(max_outcome_chars), 50000))
        truncated = len(full_outcome) > limit
        snapshot_outcome = full_outcome[:limit]
        full_hash = hashlib.sha256(
            full_outcome.encode("utf-8", errors="replace")
        ).hexdigest()

        payload_obj = {
            "task_id": task_id,
            "status": str(status),
            "outcome": snapshot_outcome,
            "truncated": truncated,
            "full_result_hash": full_hash,
        }
        canonical = json.dumps(
            payload_obj,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        payload_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        snapshots: list[dict[str, Any]] = []

        with self._connect() as con:
            for project_id in project_ids:
                event_id = "PE-" + uuid4().hex[:12]
                cur = con.execute(
                    """INSERT OR IGNORE INTO project_events(
                           id, project_id, event_type, task_id, source,
                           payload_json, payload_hash
                       ) VALUES(?, ?, 'task_outcome', ?, ?, ?, ?)""",
                    (
                        event_id,
                        project_id,
                        task_id,
                        str(source or "orchestrator"),
                        canonical,
                        payload_hash,
                    ),
                )
                inserted = cur.rowcount == 1
                if inserted:
                    con.execute(
                        """UPDATE projects
                           SET last_activity_at=CURRENT_TIMESTAMP
                           WHERE id=?""",
                        (project_id,),
                    )
                snapshots.append(
                    {
                        "project_id": project_id,
                        "task_id": task_id,
                        "inserted": inserted,
                        "payload_hash": payload_hash,
                        "truncated": truncated,
                    }
                )

        return snapshots

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

        sql += " ORDER BY created_at, rowid"

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

        sql += " ORDER BY created_at, rowid"



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
