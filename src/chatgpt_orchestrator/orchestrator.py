from __future__ import annotations

import threading
import time
from typing import Any

from .adapters import SimulatedAdapter, WorkerAdapter
from .config import load_settings
from .models import (
    JobKind,
    JobStatus,
    Settings,
    TaskStatus,
    TERMINAL_JOB_STATUSES,
    WorkerStatus,
)
from .store import Store


class Orchestrator:
    def __init__(
        self,
        settings: Settings | None = None,
        store: Store | None = None,
        adapter: WorkerAdapter | None = None,
    ):
        self.settings = settings or load_settings()
        self.store = store or Store(self.settings.database_path)

        if adapter is not None:
            self.adapter = adapter
        elif self.settings.backend == "simulated":
            self.adapter = SimulatedAdapter()
        elif self.settings.backend == "edge":
            from .edge_adapter import EdgeChatGPTAdapter

            self.adapter = EdgeChatGPTAdapter(
                executable=self.settings.edge_executable,
                chat_url=self.settings.chat_url,
                create_timeout_seconds=self.settings.create_timeout_seconds,
                send_timeout_seconds=self.settings.send_timeout_seconds,
                stable_seconds=self.settings.stable_seconds,
            )
        elif self.settings.backend == "edge_tabs":
            from .edge_tabs_adapter import EdgeSharedTabsAdapter

            self.adapter = EdgeSharedTabsAdapter(
                executable=self.settings.edge_executable,
                chat_url=self.settings.chat_url,
                create_timeout_seconds=self.settings.create_timeout_seconds,
                send_timeout_seconds=self.settings.send_timeout_seconds,
                stable_seconds=self.settings.stable_seconds,
            )
        else:
            raise ValueError(f"Unsupported backend: {self.settings.backend}")

        self._job_lock = threading.RLock()
        self._cancel_events: dict[str, threading.Event] = {}
        self._job_threads: dict[str, threading.Thread] = {}

    # ----------------------------
    # Task lifecycle
    # ----------------------------

    def task_create(self, goal: str) -> dict[str, Any]:
        goal = goal.strip()
        if not goal:
            raise ValueError("goal is required")
        return self.store.create_task(goal)

    def task_get(self, task_id: str) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        task["workers"] = [
            w
            for w in self.store.list_workers(include_closed=True)
            if w["task_id"] == task_id
        ]
        task["jobs"] = self.store.list_jobs(task_id=task_id)
        return task

    def task_list(self) -> list[dict[str, Any]]:
        return self.store.list_tasks()

    def task_cancel(self, task_id: str) -> dict[str, Any]:
        self.store.get_task(task_id)
        for job in self.store.list_jobs(task_id=task_id, include_terminal=False):
            self.chat_cancel(job["id"])
        for worker in self.store.list_workers():
            if worker["task_id"] == task_id:
                self.chat_close(worker["id"])
        return self.store.set_task_status(task_id, TaskStatus.CANCELLED)

    def task_collect(self, task_id: str) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        jobs = self.store.list_jobs(task_id=task_id, kind=JobKind.WORKER)
        review_jobs = self.store.list_jobs(task_id=task_id, kind=JobKind.REVIEW)
        workers = {
            worker["id"]: worker
            for worker in self.store.list_workers(include_closed=True)
            if worker["task_id"] == task_id
        }

        active = [
            job for job in jobs
            if job["status"] in {JobStatus.QUEUED, JobStatus.RUNNING}
        ]
        completed = [job for job in jobs if job["status"] == JobStatus.COMPLETED]
        failed = [
            job for job in jobs
            if job["status"] in {JobStatus.ERROR, JobStatus.TIMED_OUT}
        ]
        cancelled = [job for job in jobs if job["status"] == JobStatus.CANCELLED]

        self._sync_task_status(task_id)
        task = self.store.get_task(task_id)

        results = []
        combined_parts = []
        for job in completed:
            worker = workers.get(job["worker_id"], {})
            item = {
                "job_id": job["id"],
                "worker_id": job["worker_id"],
                "role": worker.get("role"),
                "result": job["result"],
            }
            results.append(item)
            combined_parts.append(
                f"## {worker.get('role') or job['worker_id']} ({job['id']})\n"
                f"{job['result'] or ''}"
            )

        errors = [
            {
                "job_id": job["id"],
                "worker_id": job["worker_id"],
                "status": job["status"],
                "error": job["error"],
            }
            for job in failed
        ]

        latest_review = review_jobs[-1] if review_jobs else None
        review = None
        if latest_review is not None:
            reviewer = workers.get(latest_review["worker_id"], {})
            review = {
                "job_id": latest_review["id"],
                "worker_id": latest_review["worker_id"],
                "role": reviewer.get("role"),
                "status": latest_review["status"],
                "result": latest_review["result"],
                "error": latest_review["error"],
            }

        return {
            "task_id": task_id,
            "goal": task["goal"],
            "task_status": task["status"],
            "ready": bool(jobs) and not active,
            "job_count": len(jobs),
            "active_count": len(active),
            "completed_count": len(completed),
            "failed_count": len(failed),
            "cancelled_count": len(cancelled),
            "results": results,
            "errors": errors,
            "combined": "\n\n".join(combined_parts),
            "review": review,
        }

    def task_review_submit(
        self,
        task_id: str,
        instructions: str | None = None,
        role: str = "reviewer",
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        collected = self.task_collect(task_id)
        if not collected["ready"]:
            raise RuntimeError("Worker jobs are still active; review cannot start yet.")
        if collected["completed_count"] == 0:
            raise RuntimeError("There are no completed worker results to review.")

        active_reviews = [
            job
            for job in self.store.list_jobs(
                task_id=task_id,
                kind=JobKind.REVIEW,
                include_terminal=False,
            )
        ]
        if active_reviews:
            raise RuntimeError(
                f"Task already has an active review job: {active_reviews[-1]['id']}"
            )

        reviewer = self.chat_create(role, task_id)
        prompt = self._build_review_prompt(
            goal=collected["goal"],
            results=collected["results"],
            errors=collected["errors"],
            instructions=instructions,
        )
        job = self._submit_job(
            reviewer["id"],
            prompt,
            timeout_seconds=timeout_seconds,
            max_retries=0,
            kind=JobKind.REVIEW,
        )
        self.store.set_task_status(task_id, TaskStatus.REVIEWING)
        return {
            "task_id": task_id,
            "reviewer_worker_id": reviewer["id"],
            "review_job_id": job["id"],
            "status": job["status"],
        }

    def task_review_status(self, task_id: str) -> dict[str, Any]:
        self.store.get_task(task_id)
        reviews = self.store.list_jobs(task_id=task_id, kind=JobKind.REVIEW)
        if not reviews:
            return {
                "task_id": task_id,
                "has_review": False,
                "status": None,
                "result": None,
                "error": None,
            }

        latest = reviews[-1]
        latest = self.chat_job_status(latest["id"])
        worker = self.store.get_worker(latest["worker_id"])
        return {
            "task_id": task_id,
            "has_review": True,
            "job_id": latest["id"],
            "worker_id": latest["worker_id"],
            "role": worker["role"],
            "status": latest["status"],
            "result": latest["result"],
            "error": latest["error"],
        }

    def task_finalize(self, task_id: str) -> dict[str, Any]:
        collected = self.task_collect(task_id)
        review = self.task_review_status(task_id)

        if review["has_review"]:
            if review["status"] in {JobStatus.QUEUED, JobStatus.RUNNING}:
                return {
                    "task_id": task_id,
                    "ready": False,
                    "reviewed": False,
                    "status": review["status"],
                    "result": None,
                    "review_job_id": review["job_id"],
                }
            if review["status"] == JobStatus.COMPLETED:
                return {
                    "task_id": task_id,
                    "ready": True,
                    "reviewed": True,
                    "status": "COMPLETED",
                    "result": review["result"],
                    "review_job_id": review["job_id"],
                    "worker_results": collected["results"],
                }

        return {
            "task_id": task_id,
            "ready": collected["ready"],
            "reviewed": False,
            "status": collected["task_status"],
            "result": collected["combined"] if collected["ready"] else None,
            "review_error": review.get("error") if review["has_review"] else None,
            "worker_results": collected["results"],
        }

    @staticmethod
    def _build_review_prompt(
        *,
        goal: str,
        results: list[dict[str, Any]],
        errors: list[dict[str, Any]],
        instructions: str | None,
    ) -> str:
        sections = [
            "You are the final reviewer for a delegated task.",
            "",
            "TASK GOAL:",
            goal,
            "",
            "INDEPENDENT WORKER OUTPUTS:",
        ]

        for item in results:
            sections.extend([
                "",
                f"[{item.get('role') or 'worker'} | {item['job_id']}]",
                str(item.get("result") or ""),
            ])

        if errors:
            sections.extend(["", "WORKER ERRORS:"])
            for item in errors:
                sections.append(
                    f"- {item['job_id']} ({item['status']}): {item.get('error') or ''}"
                )

        sections.extend([
            "",
            "REVIEW INSTRUCTIONS:",
            "Compare the outputs, identify meaningful disagreements or missing pieces, "
            "resolve them where the evidence allows, and produce one consolidated final answer. "
            "Do not blindly vote by majority. Preserve useful details from minority outputs "
            "when they are better supported.",
        ])
        if instructions and instructions.strip():
            sections.append(instructions.strip())

        return "\n".join(sections)

    # ----------------------------
    # Worker lifecycle
    # ----------------------------

    def chat_create(self, role: str, task_id: str | None = None) -> dict[str, Any]:
        role = role.strip()
        if not role:
            raise ValueError("role is required")
        if self.store.active_worker_count() >= self.settings.max_active_workers:
            raise RuntimeError(
                f"Active worker limit reached ({self.settings.max_active_workers})"
            )

        session_id = self.adapter.create(role)
        worker = self.store.create_worker(role, session_id, task_id)
        if task_id is not None:
            task = self.store.get_task(task_id)
            if task["status"] == TaskStatus.PENDING:
                self.store.set_task_status(task_id, TaskStatus.RUNNING)
        return worker

    def chat_send(self, worker_id: str, prompt: str) -> dict[str, Any]:
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("prompt is required")

        worker = self.store.get_worker(worker_id)
        if worker["status"] == WorkerStatus.CLOSED:
            raise RuntimeError("worker is closed")
        self._require_worker_idle(worker_id)

        self.store.update_worker(
            worker_id,
            status=WorkerStatus.RUNNING,
            prompt=prompt,
        )
        try:
            result = self.adapter.send(
                worker["session_id"],
                prompt,
                timeout_seconds=self.settings.default_job_timeout_seconds,
            )
        except Exception:
            self.store.update_worker(worker_id, status=WorkerStatus.ERROR)
            raise

        return self.store.update_worker(
            worker_id,
            status=WorkerStatus.COMPLETED,
            result=result,
        )

    def chat_status(self, worker_id: str) -> dict[str, Any]:
        worker = self.store.get_worker(worker_id)
        details: dict[str, Any] = {
            "id": worker["id"],
            "task_id": worker["task_id"],
            "role": worker["role"],
            "status": worker["status"],
            "updated_at": worker["updated_at"],
            "active_jobs": self.store.list_jobs(
                worker_id=worker_id,
                include_terminal=False,
            ),
        }

        inspect = getattr(self.adapter, "inspect", None)
        if callable(inspect) and worker["status"] != WorkerStatus.CLOSED:
            try:
                details["session"] = inspect(worker["session_id"])
            except Exception as exc:
                details["session_error"] = str(exc)
        return details

    def chat_read(self, worker_id: str) -> dict[str, Any]:
        worker = self.store.get_worker(worker_id)
        return {
            "id": worker["id"],
            "status": worker["status"],
            "result": worker["last_result"],
        }

    def chat_list(self, include_closed: bool = False) -> list[dict[str, Any]]:
        return self.store.list_workers(include_closed=include_closed)

    def chat_close(self, worker_id: str) -> dict[str, Any]:
        worker = self.store.get_worker(worker_id)

        for job in self.store.list_jobs(
            worker_id=worker_id,
            include_terminal=False,
        ):
            self.chat_cancel(job["id"])

        if worker["status"] != WorkerStatus.CLOSED:
            self.adapter.close(worker["session_id"])
        return self.store.update_worker(worker_id, status=WorkerStatus.CLOSED)

    # ----------------------------
    # Asynchronous jobs
    # ----------------------------

    def chat_submit(
        self,
        worker_id: str,
        prompt: str,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
    ) -> dict[str, Any]:
        return self._submit_job(
            worker_id,
            prompt,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            kind=JobKind.WORKER,
        )

    def _submit_job(
        self,
        worker_id: str,
        prompt: str,
        *,
        timeout_seconds: float | None,
        max_retries: int | None,
        kind: JobKind,
    ) -> dict[str, Any]:
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("prompt is required")

        worker = self.store.get_worker(worker_id)
        if worker["status"] == WorkerStatus.CLOSED:
            raise RuntimeError("worker is closed")
        self._require_worker_idle(worker_id)

        timeout = (
            self.settings.default_job_timeout_seconds
            if timeout_seconds is None
            else float(timeout_seconds)
        )
        retries = (
            self.settings.default_max_retries
            if max_retries is None
            else int(max_retries)
        )
        if timeout <= 0:
            raise ValueError("timeout_seconds must be > 0")
        if retries < 0 or retries > 5:
            raise ValueError("max_retries must be between 0 and 5")

        job = self.store.create_job(
            worker_id,
            prompt,
            timeout_seconds=timeout,
            max_retries=retries,
            kind=kind,
        )
        event = threading.Event()
        thread = threading.Thread(
            target=self._run_job,
            args=(job["id"], event),
            name=f"orchestrator-{job['id']}",
            daemon=True,
        )

        with self._job_lock:
            self._cancel_events[job["id"]] = event
            self._job_threads[job["id"]] = thread

        thread.start()
        return self.store.get_job(job["id"])

    def chat_job_status(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if job["status"] in TERMINAL_JOB_STATUSES:
            with self._job_lock:
                thread = self._job_threads.get(job_id)
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=1.0)
            job = self.store.get_job(job_id)
        return job

    def chat_job_list(
        self,
        task_id: str | None = None,
        worker_id: str | None = None,
        include_terminal: bool = True,
    ) -> list[dict[str, Any]]:
        return self.store.list_jobs(
            task_id=task_id,
            worker_id=worker_id,
            include_terminal=include_terminal,
        )

    def chat_cancel(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if job["status"] in TERMINAL_JOB_STATUSES:
            return job

        job = self.store.request_job_cancel(job_id)
        worker = self.store.get_worker(job["worker_id"])

        with self._job_lock:
            event = self._cancel_events.get(job_id)
        if event is not None:
            event.set()

        cancel = getattr(self.adapter, "cancel", None)
        if callable(cancel):
            try:
                cancel(worker["session_id"])
            except Exception:
                pass

        return self.store.get_job(job_id)

    def chat_retry(
        self,
        job_id: str,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        old = self.store.get_job(job_id)
        if old["status"] not in {
            JobStatus.ERROR,
            JobStatus.TIMED_OUT,
            JobStatus.CANCELLED,
        }:
            raise RuntimeError(
                "Only ERROR, TIMED_OUT or CANCELLED jobs can be retried."
            )
        return self._submit_job(
            old["worker_id"],
            old["prompt"],
            timeout_seconds=(
                old["timeout_seconds"]
                if timeout_seconds is None
                else timeout_seconds
            ),
            max_retries=old["max_retries"],
            kind=JobKind(old["kind"]),
        )

    def _run_job(self, job_id: str, cancel_event: threading.Event) -> None:
        job = self.store.get_job(job_id)
        worker = self.store.get_worker(job["worker_id"])
        max_attempts = int(job["max_retries"]) + 1
        final_status: JobStatus | None = None

        try:
            for attempt in range(1, max_attempts + 1):
                if cancel_event.is_set() or self.store.get_job(job_id)["cancel_requested"]:
                    final_status = JobStatus.CANCELLED
                    break

                self.store.set_job_running(job_id, attempt)
                self.store.update_worker(
                    worker["id"],
                    status=WorkerStatus.RUNNING,
                    prompt=job["prompt"],
                )

                try:
                    result = self.adapter.send(
                        worker["session_id"],
                        job["prompt"],
                        cancel_event=cancel_event,
                        timeout_seconds=float(job["timeout_seconds"]),
                    )
                    if cancel_event.is_set():
                        final_status = JobStatus.CANCELLED
                        break

                    self.store.finish_job(
                        job_id,
                        JobStatus.COMPLETED,
                        result=result,
                    )
                    self.store.update_worker(
                        worker["id"],
                        status=WorkerStatus.COMPLETED,
                        result=result,
                    )
                    final_status = JobStatus.COMPLETED
                    break

                except InterruptedError as exc:
                    self.store.finish_job(
                        job_id,
                        JobStatus.CANCELLED,
                        error=str(exc),
                    )
                    if self.store.get_worker(worker["id"])["status"] != WorkerStatus.CLOSED:
                        self.store.update_worker(worker["id"], status=WorkerStatus.IDLE)
                    final_status = JobStatus.CANCELLED
                    break

                except TimeoutError as exc:
                    if cancel_event.is_set():
                        self.store.finish_job(
                            job_id,
                            JobStatus.CANCELLED,
                            error="Cancelled while waiting for worker response.",
                        )
                        final_status = JobStatus.CANCELLED
                        break
                    if attempt < max_attempts:
                        self._cancel_adapter_best_effort(worker["session_id"])
                        time.sleep(0.4)
                        continue
                    self.store.finish_job(
                        job_id,
                        JobStatus.TIMED_OUT,
                        error=str(exc),
                    )
                    self.store.update_worker(worker["id"], status=WorkerStatus.ERROR)
                    final_status = JobStatus.TIMED_OUT
                    break

                except Exception as exc:
                    if cancel_event.is_set():
                        self.store.finish_job(
                            job_id,
                            JobStatus.CANCELLED,
                            error="Cancelled while worker operation was active.",
                        )
                        final_status = JobStatus.CANCELLED
                        break
                    if attempt < max_attempts:
                        self._cancel_adapter_best_effort(worker["session_id"])
                        time.sleep(0.4)
                        continue
                    self.store.finish_job(
                        job_id,
                        JobStatus.ERROR,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    self.store.update_worker(worker["id"], status=WorkerStatus.ERROR)
                    final_status = JobStatus.ERROR
                    break

            if final_status == JobStatus.CANCELLED:
                current = self.store.get_job(job_id)
                if current["status"] not in TERMINAL_JOB_STATUSES:
                    self.store.finish_job(
                        job_id,
                        JobStatus.CANCELLED,
                        error="Cancellation requested.",
                    )
                if self.store.get_worker(worker["id"])["status"] != WorkerStatus.CLOSED:
                    self.store.update_worker(worker["id"], status=WorkerStatus.IDLE)

        finally:
            self._sync_task_status(worker["task_id"])
            with self._job_lock:
                self._cancel_events.pop(job_id, None)
                self._job_threads.pop(job_id, None)

    def _sync_task_status(self, task_id: str | None) -> None:
        if task_id is None:
            return

        task = self.store.get_task(task_id)
        if task["status"] == TaskStatus.CANCELLED:
            return

        workers = self.store.list_jobs(task_id=task_id, kind=JobKind.WORKER)
        reviews = self.store.list_jobs(task_id=task_id, kind=JobKind.REVIEW)

        if not workers:
            return

        worker_statuses = {job["status"] for job in workers}
        if JobStatus.QUEUED in worker_statuses or JobStatus.RUNNING in worker_statuses:
            self.store.set_task_status(task_id, TaskStatus.RUNNING)
            return

        if JobStatus.ERROR in worker_statuses or JobStatus.TIMED_OUT in worker_statuses:
            self.store.set_task_status(task_id, TaskStatus.FAILED)
            return

        if worker_statuses <= {JobStatus.CANCELLED}:
            self.store.set_task_status(task_id, TaskStatus.CANCELLED)
            return

        if reviews:
            latest = reviews[-1]
            if latest["status"] in {JobStatus.QUEUED, JobStatus.RUNNING}:
                self.store.set_task_status(task_id, TaskStatus.REVIEWING)
                return
            if latest["status"] == JobStatus.COMPLETED:
                self.store.set_task_status(task_id, TaskStatus.COMPLETED)
                return
            if latest["status"] in {JobStatus.ERROR, JobStatus.TIMED_OUT}:
                self.store.set_task_status(task_id, TaskStatus.FAILED)
                return

        self.store.set_task_status(task_id, TaskStatus.COMPLETED)

    def _require_worker_idle(self, worker_id: str) -> None:
        active = self.store.list_jobs(
            worker_id=worker_id,
            include_terminal=False,
        )
        if active:
            raise RuntimeError(
                f"Worker {worker_id} already has an active job: {active[0]['id']}"
            )

    def _cancel_adapter_best_effort(self, session_id: str) -> None:
        cancel = getattr(self.adapter, "cancel", None)
        if callable(cancel):
            try:
                cancel(session_id)
            except Exception:
                pass
