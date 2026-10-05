from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from . import __version__
from .adapters import NonRetryableWorkerError, SimulatedAdapter, WorkerAdapter
from .autonomy import (
    build_rework_prompt,
    build_worker_prompt,
    choose_roles,
    parse_review_verdict,
)
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
        elif self.settings.backend == "dom_hybrid":
            from .dom_hybrid_adapter import DOMUIAHybridAdapter

            self.adapter = DOMUIAHybridAdapter(
                executable=self.settings.edge_executable,
                chat_url=self.settings.chat_url,
                create_timeout_seconds=self.settings.create_timeout_seconds,
                send_timeout_seconds=self.settings.send_timeout_seconds,
                stable_seconds=self.settings.stable_seconds,
                broker_endpoint=self.settings.dom_broker_endpoint,
                broker_token_file=self.settings.dom_broker_token_file,
                uia_fallback_enabled=self.settings.dom_uia_fallback_enabled,
                dom_tab_pool_only=self.settings.dom_tab_pool_only,
                dom_tab_pool_size=self.settings.dom_tab_pool_size,
                dom_idle_shutdown_seconds=self.settings.dom_idle_shutdown_seconds,
                status_path=str(
                    Path(self.settings.database_path).parent
                    / "runtime-status.json"
                ),
            )
        elif self.settings.backend == "edge_hybrid":
            from .edge_hybrid_adapter import EdgeHybridAdapter

            self.adapter = EdgeHybridAdapter(
                executable=self.settings.edge_executable,
                profile_dir=self.settings.cdp_profile_dir,
                chat_url=self.settings.chat_url,
                create_timeout_seconds=self.settings.create_timeout_seconds,
                send_timeout_seconds=self.settings.send_timeout_seconds,
                stable_seconds=self.settings.stable_seconds,
                cdp_enabled=self.settings.hybrid_cdp_enabled,
                status_path=str(Path(self.settings.database_path).parent / "runtime-status.json"),
            )
        elif self.settings.backend == "edge_cdp":
            from .edge_cdp_adapter import EdgeCDPAdapter

            self.adapter = EdgeCDPAdapter(
                executable=self.settings.edge_executable,
                profile_dir=self.settings.cdp_profile_dir,
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
        self._standalone_idle_timers: dict[str, threading.Timer] = {}
        self._reconcile_project_snapshots_best_effort()

    def orchestrator_info(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "version": __version__,
            "backend": self.settings.backend,
            "max_active_workers": self.settings.max_active_workers,
            "active_worker_count": len(self.chat_list()),
            "active_job_count": len(self.chat_job_list(include_terminal=False)),
            "capabilities": {
                "autonomous_planning": True,
                "automatic_role_assignment": True,
                "review_rework_loop": True,
                "restart_recovery": True,
                "shared_tabs": True,
                "uia_only_shared_tabs": True,
                "tab_pool_reuse": True,
                "worker_edge_auto_shutdown": (
                    self.settings.backend == "dom_hybrid"
                    and self.settings.dom_idle_shutdown_seconds > 0
                ),
                "cdp_background": (
                    self.settings.backend == "edge_cdp"
                    or (
                        self.settings.backend == "edge_hybrid"
                        and self.settings.hybrid_cdp_enabled
                    )
                ),
                "dom_background": self.settings.backend == "dom_hybrid",
                "shared_cdp_via_actuator": self.settings.backend == "dom_hybrid",
                "background_first_hybrid": self.settings.backend in {
                    "edge_hybrid",
                    "dom_hybrid",
                },
                "safe_uia_fallback": self.settings.backend in {
                    "edge_tabs",
                    "edge_hybrid",
                    "dom_hybrid",
                },
                "uia_fallback_serialized": self.settings.backend in {
                    "edge_tabs",
                    "edge_hybrid",
                    "dom_hybrid",
                },
                "non_retryable_ambiguous_submissions": True,
                "safe_restart_recovery": True,
                "stability_metrics": self.settings.backend in {
                    "edge_hybrid",
                    "dom_hybrid",
                },
                "soak_test_support": True,
                "persistent_project_state": True,
                "project_context_injection": True,
                "project_outcome_snapshots": True,
                "optimistic_project_updates": True,
                "cdp_experimental": self.settings.backend == "edge_cdp",
            },
        }
        profile_status = getattr(self.adapter, "profile_status", None)
        if callable(profile_status):
            try:
                info["backend_status"] = profile_status()
            except Exception as exc:
                info["backend_status"] = {"ready": False, "error": str(exc)}
        return info

    # ----------------------------
    # Persistent project state
    # ----------------------------

    def project_create(
        self,
        name: str,
        *,
        summary: str = "",
        goals: list[str] | None = None,
        decisions: list[str] | None = None,
        current_phase: str = "",
        status: str = "active",
        next_actions: list[str] | None = None,
    ) -> dict[str, Any]:
        return self.store.create_project(
            name,
            summary=summary,
            goals=goals,
            decisions=decisions,
            current_phase=current_phase,
            status=status,
            next_actions=next_actions,
        )

    def project_list(
        self,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return self.store.list_projects(status=status, limit=limit)

    def project_get(
        self,
        project_id: str,
        recent_events: int = 10,
    ) -> dict[str, Any]:
        self._reconcile_project_snapshots_best_effort(project_id=project_id)
        return self.store.get_project(
            project_id,
            recent_events=recent_events,
        )

    def project_update(
        self,
        project_id: str,
        expected_version: int,
        *,
        name: str | None = None,
        summary: str | None = None,
        goals: list[str] | None = None,
        decisions: list[str] | None = None,
        current_phase: str | None = None,
        status: str | None = None,
        next_actions: list[str] | None = None,
    ) -> dict[str, Any]:
        return self.store.update_project(
            project_id,
            expected_version=expected_version,
            name=name,
            summary=summary,
            goals=goals,
            decisions=decisions,
            current_phase=current_phase,
            status=status,
            next_actions=next_actions,
        )

    def project_attach_task(
        self,
        project_id: str,
        task_id: str,
    ) -> dict[str, Any]:
        attached = self.store.attach_project_task(
            project_id,
            task_id,
            attached_by="human",
        )
        snapshots = self._snapshot_task_to_projects_best_effort(task_id)
        return {
            **attached,
            "snapshots": snapshots,
        }

    # ----------------------------
    # Task lifecycle
    # ----------------------------

    def task_create(
        self,
        goal: str,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        goal = goal.strip()
        if not goal:
            raise ValueError("goal is required")
        if project_id is not None:
            self.store.get_project(project_id, recent_events=0)
        task = self.store.create_task(goal)
        if project_id is not None:
            self.store.attach_project_task(
                project_id,
                task["id"],
                attached_by="task_create",
            )
            task["project_ids"] = [project_id]
        else:
            task["project_ids"] = []
        return task

    def task_get(self, task_id: str) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        task["workers"] = [
            w
            for w in self.store.list_workers(include_closed=True)
            if w["task_id"] == task_id
        ]
        task["jobs"] = self.store.list_jobs(task_id=task_id)
        task["project_ids"] = self.store.project_ids_for_task(task_id)
        return task

    def task_list(self) -> list[dict[str, Any]]:
        return self.store.list_tasks()

    def task_plan(
        self,
        goal: str,
        worker_count: int | None = None,
        review: bool = True,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        goal = goal.strip()
        if not goal:
            raise ValueError("goal is required")

        project_context = None
        if project_id is not None:
            project_context = self._project_context_text(project_id)

        review_available = bool(review and self.settings.max_active_workers >= 2)
        reserved_slots = 1 if review_available else 0
        worker_capacity = self.settings.max_active_workers - reserved_slots
        if worker_capacity < 1:
            worker_capacity = 1

        if worker_count is None:
            count = min(2, worker_capacity)
        else:
            count = int(worker_count)
            if count < 1:
                raise ValueError("worker_count must be >= 1")
            count = min(count, worker_capacity)

        roles = choose_roles(goal, count)
        workers = [
            {
                "role": role,
                "prompt": build_worker_prompt(
                    goal,
                    role,
                    index,
                    len(roles),
                    project_context=project_context,
                ),
            }
            for index, role in enumerate(roles, start=1)
        ]
        return {
            "goal": goal,
            "worker_count": len(workers),
            "max_active_workers": self.settings.max_active_workers,
            "review_requested": bool(review),
            "review_enabled": review_available,
            "review_slot_reserved": reserved_slots == 1,
            "project_id": project_id,
            "project_context_included": project_context is not None,
            "workers": workers,
        }

    def task_auto_start(
        self,
        goal: str,
        worker_count: int | None = None,
        review: bool = True,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        plan = self.task_plan(
            goal,
            worker_count=worker_count,
            review=review,
            project_id=project_id,
        )
        task = self.task_create(plan["goal"], project_id=project_id)
        submitted: list[dict[str, Any]] = []

        try:
            for spec in plan["workers"]:
                worker = self.chat_create(spec["role"], task["id"])
                job = self.chat_submit(
                    worker["id"],
                    spec["prompt"],
                    timeout_seconds=timeout_seconds,
                    max_retries=max_retries,
                )
                submitted.append(
                    {
                        "worker_id": worker["id"],
                        "role": spec["role"],
                        "job_id": job["id"],
                        "status": job["status"],
                    }
                )
        except Exception:
            self.task_cancel(task["id"])
            raise

        return {
            "task_id": task["id"],
            "status": self.store.get_task(task["id"])["status"],
            "plan": plan,
            "jobs": submitted,
            "next_action": "task_auto_advance",
        }

    def task_auto_advance(
        self,
        task_id: str,
        *,
        review: bool = True,
        review_instructions: str | None = None,
        max_rework_rounds: int = 1,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        self.store.get_task(task_id)
        if max_rework_rounds < 0 or max_rework_rounds > 5:
            raise ValueError("max_rework_rounds must be between 0 and 5")

        jobs = self.store.list_jobs(task_id=task_id)
        active = [
            job for job in jobs
            if job["status"] in {JobStatus.QUEUED, JobStatus.RUNNING}
        ]
        if active:
            return {
                "task_id": task_id,
                "phase": "waiting",
                "ready": False,
                "active_jobs": [job["id"] for job in active],
            }

        # A job can reach a terminal DB state just before its background thread
        # finishes final status synchronization. Join those threads before
        # finalization so task cleanup cannot race SQLite/temp-directory cleanup.
        self._join_task_threads(task_id)

        worker_jobs = [job for job in jobs if job["kind"] == str(JobKind.WORKER)]
        review_jobs = [job for job in jobs if job["kind"] == str(JobKind.REVIEW)]
        completed_workers = [
            job for job in worker_jobs if job["status"] == JobStatus.COMPLETED
        ]
        if not completed_workers:
            errors = self.task_collect(task_id)["errors"]
            self._close_task_workers(task_id)
            return {
                "task_id": task_id,
                "phase": "failed",
                "ready": False,
                "reason": "No completed worker results are available.",
                "errors": errors,
            }

        review_enabled = bool(review and self.settings.max_active_workers >= 2)
        if not review_enabled:
            final = self.task_finalize(task_id)
            self._close_task_workers(task_id)
            return {
                "task_id": task_id,
                "phase": "completed_without_review",
                **final,
            }

        if not review_jobs:
            submitted = self.task_review_submit(
                task_id,
                instructions=review_instructions,
                timeout_seconds=timeout_seconds,
            )
            return {
                "task_id": task_id,
                "phase": "review_submitted",
                "ready": False,
                **submitted,
            }

        latest_review = review_jobs[-1]
        if latest_review["status"] in {
            JobStatus.ERROR,
            JobStatus.TIMED_OUT,
            JobStatus.CANCELLED,
        }:
            return {
                "task_id": task_id,
                "phase": "review_failed",
                "ready": False,
                "review_job_id": latest_review["id"],
                "status": latest_review["status"],
                "error": latest_review["error"],
            }

        source_tag = f"REWORK SOURCE: {latest_review['id']}"
        source_jobs = [job for job in worker_jobs if source_tag in job["prompt"]]
        if source_jobs:
            failed_rework = [
                job for job in source_jobs if job["status"] != JobStatus.COMPLETED
            ]
            if failed_rework:
                return {
                    "task_id": task_id,
                    "phase": "rework_failed",
                    "ready": False,
                    "jobs": [
                        {
                            "job_id": job["id"],
                            "status": job["status"],
                            "error": job["error"],
                        }
                        for job in failed_rework
                    ],
                }

            submitted = self.task_review_submit(
                task_id,
                instructions=review_instructions,
                timeout_seconds=timeout_seconds,
            )
            return {
                "task_id": task_id,
                "phase": "re_review_submitted",
                "ready": False,
                **submitted,
            }

        verdict = parse_review_verdict(latest_review["result"])
        if verdict["verdict"] == "PASS":
            final = self.task_finalize(task_id)
            self._close_task_workers(task_id)
            return {
                "task_id": task_id,
                "phase": "completed",
                "review_verdict": verdict,
                **final,
            }

        if verdict["verdict"] != "REWORK":
            return {
                "task_id": task_id,
                "phase": "review_unknown",
                "ready": False,
                "review_job_id": latest_review["id"],
                "review_verdict": verdict,
                "review_result": latest_review["result"],
            }

        rework_requests = sum(
            1
            for job in review_jobs
            if job["status"] == JobStatus.COMPLETED
            and parse_review_verdict(job["result"])["verdict"] == "REWORK"
        )
        if rework_requests > max_rework_rounds:
            return {
                "task_id": task_id,
                "phase": "rework_limit_reached",
                "ready": False,
                "review_job_id": latest_review["id"],
                "review_verdict": verdict,
                "max_rework_rounds": max_rework_rounds,
            }

        targets = self._select_rework_workers(task_id, verdict["roles"])
        if not targets:
            return {
                "task_id": task_id,
                "phase": "rework_unavailable",
                "ready": False,
                "review_job_id": latest_review["id"],
                "review_verdict": verdict,
            }

        task = self.store.get_task(task_id)
        submitted_jobs = []
        for worker in targets:
            prior = self._latest_worker_result(worker["id"])
            prompt = build_rework_prompt(
                goal=task["goal"],
                role=worker["role"],
                review_job_id=latest_review["id"],
                review_result=str(latest_review["result"] or ""),
                prior_result=prior,
                project_context=self._task_project_context_text(task_id),
            )
            job = self._submit_job(
                worker["id"],
                prompt,
                timeout_seconds=timeout_seconds,
                max_retries=0,
                kind=JobKind.WORKER,
            )
            submitted_jobs.append(
                {
                    "worker_id": worker["id"],
                    "role": worker["role"],
                    "job_id": job["id"],
                    "status": job["status"],
                }
            )

        return {
            "task_id": task_id,
            "phase": "rework_submitted",
            "ready": False,
            "review_job_id": latest_review["id"],
            "review_verdict": verdict,
            "jobs": submitted_jobs,
        }

    def task_recover(
        self,
        task_id: str,
        *,
        restart_only: bool = True,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        self.store.get_task(task_id)
        jobs = self.store.list_jobs(task_id=task_id)
        retryable = []
        for job in jobs:
            if job["status"] not in {JobStatus.ERROR, JobStatus.TIMED_OUT}:
                continue
            error = str(job.get("error") or "")
            if self._is_non_retryable_error(error):
                continue
            if restart_only and "safe to recover" not in error.casefold():
                continue
            retryable.append(job)

        recovered = []
        for old in retryable:
            old_worker = self.store.get_worker(old["worker_id"])
            if old_worker["status"] != WorkerStatus.CLOSED:
                try:
                    self.chat_close(old_worker["id"])
                except Exception:
                    self.store.update_worker(old_worker["id"], status=WorkerStatus.CLOSED)

            replacement = self._get_or_create_task_worker(
                old_worker["role"],
                task_id,
                allow_reuse=False,
            )
            new_job = self._submit_job(
                replacement["id"],
                old["prompt"],
                timeout_seconds=(
                    float(old["timeout_seconds"])
                    if timeout_seconds is None
                    else timeout_seconds
                ),
                max_retries=int(old["max_retries"]),
                kind=JobKind(old["kind"]),
            )
            recovered.append(
                {
                    "old_job_id": old["id"],
                    "new_job_id": new_job["id"],
                    "worker_id": replacement["id"],
                    "role": replacement["role"],
                    "kind": new_job["kind"],
                }
            )

        return {
            "task_id": task_id,
            "recovered_count": len(recovered),
            "recovered": recovered,
        }

    def task_cancel(self, task_id: str) -> dict[str, Any]:
        self.store.get_task(task_id)
        for job in self.store.list_jobs(task_id=task_id, include_terminal=False):
            self.chat_cancel(job["id"])
        self._close_task_workers(task_id)
        task = self.store.set_task_status(task_id, TaskStatus.CANCELLED)
        self._snapshot_task_to_projects_best_effort(task_id)
        return task

    def _close_task_workers(self, task_id: str) -> None:
        """Close every non-closed worker owned by a terminal task.

        Worker/job records remain persisted for inspection, while adapter sessions
        and active-worker capacity are released immediately.
        """
        for worker in self.store.list_workers():
            if worker["task_id"] != task_id:
                continue
            try:
                self.chat_close(worker["id"])
            except Exception:
                # Capacity must not remain wedged if adapter cleanup fails.
                self.store.update_worker(worker["id"], status=WorkerStatus.CLOSED)

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

        reviewer = self._get_or_create_task_worker(role, task_id)
        prompt = self._build_review_prompt(
            goal=collected["goal"],
            results=collected["results"],
            errors=collected["errors"],
            instructions=instructions,
            project_context=self._task_project_context_text(task_id),
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
                verdict = parse_review_verdict(review["result"])
                if verdict["verdict"] == "REWORK":
                    return {
                        "task_id": task_id,
                        "ready": False,
                        "reviewed": True,
                        "status": "REWORK_REQUIRED",
                        "result": None,
                        "review_result": review["result"],
                        "review_verdict": verdict,
                        "review_job_id": review["job_id"],
                        "worker_results": collected["results"],
                    }
                final = {
                    "task_id": task_id,
                    "ready": True,
                    "reviewed": True,
                    "status": "COMPLETED",
                    "result": review["result"],
                    "review_verdict": verdict,
                    "review_job_id": review["job_id"],
                    "worker_results": collected["results"],
                }
                self._close_task_workers(task_id)
                return final

        final = {
            "task_id": task_id,
            "ready": collected["ready"],
            "reviewed": False,
            "status": collected["task_status"],
            "result": collected["combined"] if collected["ready"] else None,
            "review_error": review.get("error") if review["has_review"] else None,
            "worker_results": collected["results"],
        }
        if collected["ready"] and not review["has_review"]:
            self._close_task_workers(task_id)
        return final

    @staticmethod
    def _build_review_prompt(
        *,
        goal: str,
        results: list[dict[str, Any]],
        errors: list[dict[str, Any]],
        instructions: str | None,
        project_context: str | None = None,
    ) -> str:
        sections = [
            "You are the final reviewer for a delegated task.",
            "",
            "TASK GOAL:",
            goal,
        ]
        if project_context and project_context.strip():
            sections.extend([
                "",
                "PROJECT STATE (read-only context):",
                project_context.strip(),
                "",
                "PROJECT STATE RULES:",
                "- Use this state to check consistency with project decisions.",
                "- Do not treat worker output as permission to overwrite project state.",
            ])
        sections.extend([
            "",
            "INDEPENDENT WORKER OUTPUTS:",
        ])

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
            "The first line MUST be exactly one of these machine-readable forms:",
            "ORCH_REVIEW: PASS",
            "ORCH_REVIEW: REWORK role1, role2",
            "Use PASS when the available outputs can support a reliable final answer. "
            "Use REWORK only when a named worker role must revise a material error or gap. "
            "After the first line, provide the final answer or precise rework feedback.",
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
        else:
            self._schedule_standalone_idle_close(worker["id"])
        return worker

    def chat_send(self, worker_id: str, prompt: str) -> dict[str, Any]:
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("prompt is required")

        with self._job_lock:
            self._cancel_standalone_idle_close_locked(worker_id)
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
            result = self._clean_worker_result(
                self.adapter.send(
                    worker["session_id"],
                    prompt,
                    timeout_seconds=self.settings.default_job_timeout_seconds,
                )
            )
        except Exception:
            self.store.update_worker(worker_id, status=WorkerStatus.ERROR)
            self._auto_close_standalone_worker_best_effort(worker_id)
            raise

        completed = self.store.update_worker(
            worker_id,
            status=WorkerStatus.COMPLETED,
            result=result,
        )
        self._auto_close_standalone_worker_best_effort(worker_id)
        return completed

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
        self._cancel_standalone_idle_close(worker_id)
        worker = self.store.get_worker(worker_id)

        for job in self.store.list_jobs(
            worker_id=worker_id,
            include_terminal=False,
        ):
            self.chat_cancel(job["id"])

        if worker["status"] != WorkerStatus.CLOSED:
            self.adapter.close(worker["session_id"])
        return self.store.update_worker(worker_id, status=WorkerStatus.CLOSED)

    def _standalone_auto_close_enabled(self) -> bool:
        return (
            self.settings.backend == "dom_hybrid"
            and self.settings.dom_idle_shutdown_seconds > 0
        )

    def _cancel_standalone_idle_close_locked(self, worker_id: str) -> None:
        timer = self._standalone_idle_timers.pop(worker_id, None)
        if timer is not None:
            timer.cancel()

    def _cancel_standalone_idle_close(self, worker_id: str) -> None:
        with self._job_lock:
            self._cancel_standalone_idle_close_locked(worker_id)

    def _schedule_standalone_idle_close(self, worker_id: str) -> None:
        if not self._standalone_auto_close_enabled():
            return
        delay = max(0.05, float(self.settings.dom_idle_shutdown_seconds))
        holder: dict[str, threading.Timer] = {}

        def expire() -> None:
            timer = holder["timer"]
            with self._job_lock:
                if self._standalone_idle_timers.get(worker_id) is not timer:
                    return
                self._standalone_idle_timers.pop(worker_id, None)
                try:
                    worker = self.store.get_worker(worker_id)
                    if (
                        worker["task_id"] is not None
                        or worker["status"] != WorkerStatus.IDLE
                        or self.store.list_jobs(
                            worker_id=worker_id,
                            include_terminal=False,
                        )
                    ):
                        return
                    self.adapter.close(worker["session_id"])
                    self.store.update_worker(
                        worker_id,
                        status=WorkerStatus.CLOSED,
                    )
                except Exception:
                    pass

        timer = threading.Timer(delay, expire)
        timer.daemon = True
        holder["timer"] = timer
        with self._job_lock:
            self._cancel_standalone_idle_close_locked(worker_id)
            self._standalone_idle_timers[worker_id] = timer
        timer.start()

    def _auto_close_standalone_worker_best_effort(self, worker_id: str) -> None:
        """Release terminal standalone DOM sessions when Worker Edge auto-shutdown is enabled.

        Task-owned workers intentionally remain available for review/rework and are
        closed by the task lifecycle. The result is persisted before this cleanup,
        so cleanup failure never changes a completed job outcome.
        """
        if not self._standalone_auto_close_enabled():
            return
        try:
            worker = self.store.get_worker(worker_id)
            if (
                worker["task_id"] is not None
                or worker["status"] == WorkerStatus.CLOSED
            ):
                return
            if worker["status"] not in {
                WorkerStatus.COMPLETED,
                WorkerStatus.ERROR,
                WorkerStatus.IDLE,
            }:
                return
            if self.store.list_jobs(
                worker_id=worker_id,
                include_terminal=False,
            ):
                return
            self.chat_close(worker_id)
        except Exception:
            pass

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

        self._cancel_standalone_idle_close(worker_id)
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
        if self._is_non_retryable_error(str(old.get("error") or "")):
            raise RuntimeError(
                "This job is marked non-retryable because submission may "
                "already have reached the remote service."
            )
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
                    result = self._clean_worker_result(
                        self.adapter.send(
                            worker["session_id"],
                            job["prompt"],
                            cancel_event=cancel_event,
                            timeout_seconds=float(job["timeout_seconds"]),
                        )
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

                except NonRetryableWorkerError as exc:
                    self.store.finish_job(
                        job_id,
                        JobStatus.ERROR,
                        error=f"NON_RETRYABLE: {type(exc).__name__}: {exc}",
                    )
                    self.store.update_worker(
                        worker["id"],
                        status=WorkerStatus.ERROR,
                    )
                    final_status = JobStatus.ERROR
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
            self._auto_close_standalone_worker_best_effort(worker["id"])
            with self._job_lock:
                self._cancel_events.pop(job_id, None)
                self._job_threads.pop(job_id, None)

    def _project_context_text(self, project_id: str) -> str:
        project = self.store.get_project(project_id, recent_events=8)
        lines = [
            f"Project: {project['name']} ({project['id']})",
            f"Status: {project['status']}",
            f"Phase: {project['current_phase'] or '(unspecified)'}",
            f"State version: {project['state_version']}",
        ]
        if project["summary"]:
            lines.extend(["Summary:", project["summary"]])
        if project["goals"]:
            lines.append("Goals:")
            lines.extend(f"- {item}" for item in project["goals"])
        if project["decisions"]:
            lines.append("Recorded decisions:")
            lines.extend(f"- {item}" for item in project["decisions"])
        if project["next_actions"]:
            lines.append("Next actions:")
            lines.extend(f"- {item}" for item in project["next_actions"])
        if project["related_task_ids"]:
            lines.append(
                "Related task IDs: " + ", ".join(project["related_task_ids"][-12:])
            )

        outcomes = [
            event
            for event in project.get("recent_events", [])
            if event.get("event_type") == "task_outcome"
        ][:3]
        if outcomes:
            lines.append("Recent task outcomes:")
            for event in outcomes:
                payload = event.get("payload") or {}
                outcome = str(payload.get("outcome") or "").strip()
                if len(outcome) > 1500:
                    outcome = outcome[:1500] + "…"
                lines.append(
                    f"- {event.get('task_id')}: {payload.get('status')}"
                    + (f" | {outcome}" if outcome else "")
                )
        return "\n".join(lines)

    def _task_project_context_text(self, task_id: str) -> str | None:
        project_ids = self.store.project_ids_for_task(task_id)
        if not project_ids:
            return None
        contexts = [self._project_context_text(project_id) for project_id in project_ids]
        return "\n\n---\n\n".join(contexts)

    def _task_outcome_text(self, task_id: str) -> str:
        review_jobs = self.store.list_jobs(
            task_id=task_id,
            kind=JobKind.REVIEW,
        )
        completed_reviews = [
            job for job in review_jobs if job["status"] == JobStatus.COMPLETED
        ]
        if completed_reviews:
            return str(completed_reviews[-1].get("result") or "")

        worker_jobs = self.store.list_jobs(
            task_id=task_id,
            kind=JobKind.WORKER,
        )
        workers = {
            worker["id"]: worker
            for worker in self.store.list_workers(include_closed=True)
            if worker["task_id"] == task_id
        }
        parts: list[str] = []
        for job in worker_jobs:
            if job["status"] == JobStatus.COMPLETED:
                role = workers.get(job["worker_id"], {}).get("role") or job["worker_id"]
                parts.append(f"## {role} ({job['id']})\n{job.get('result') or ''}")
        if parts:
            return "\n\n".join(parts)

        errors = [
            f"{job['id']} {job['status']}: {job.get('error') or ''}"
            for job in worker_jobs
            if job["status"] in {
                JobStatus.ERROR,
                JobStatus.TIMED_OUT,
                JobStatus.CANCELLED,
            }
        ]
        return "\n".join(errors)

    def _snapshot_task_to_projects_best_effort(
        self,
        task_id: str,
    ) -> list[dict[str, Any]]:
        try:
            task = self.store.get_task(task_id)
            if task["status"] not in {
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }:
                return []
            return self.store.snapshot_project_task(
                task_id,
                status=str(task["status"]),
                outcome=self._task_outcome_text(task_id),
                source="orchestrator",
            )
        except Exception:
            # Project memory is additive. It must never change task outcome.
            return []

    def _reconcile_project_snapshots_best_effort(
        self,
        project_id: str | None = None,
    ) -> None:
        try:
            links = self.store.list_project_task_links()
        except Exception:
            return
        seen: set[str] = set()
        for link in links:
            if project_id is not None and link["project_id"] != project_id:
                continue
            task_id = str(link["task_id"])
            if task_id in seen:
                continue
            seen.add(task_id)
            self._snapshot_task_to_projects_best_effort(task_id)

    @staticmethod
    def _is_non_retryable_error(error: str) -> bool:
        value = str(error or "").casefold()
        return (
            "non_retryable:" in value
            or "cdp_send_ambiguous" in value
            or "submission state is ambiguous" in value
        )

    def _join_task_threads(
        self,
        task_id: str,
        timeout_seconds: float = 2.0,
    ) -> None:
        job_ids = {
            job["id"]
            for job in self.store.list_jobs(task_id=task_id)
        }
        with self._job_lock:
            threads = [
                thread
                for job_id, thread in self._job_threads.items()
                if job_id in job_ids
                and thread is not threading.current_thread()
            ]
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)

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
            self._snapshot_task_to_projects_best_effort(task_id)
            self._close_task_workers(task_id)
            return

        if worker_statuses <= {JobStatus.CANCELLED}:
            self.store.set_task_status(task_id, TaskStatus.CANCELLED)
            self._snapshot_task_to_projects_best_effort(task_id)
            self._close_task_workers(task_id)
            return

        if reviews:
            latest = reviews[-1]
            if latest["status"] in {JobStatus.QUEUED, JobStatus.RUNNING}:
                self.store.set_task_status(task_id, TaskStatus.REVIEWING)
                return
            if latest["status"] == JobStatus.COMPLETED:
                self.store.set_task_status(task_id, TaskStatus.COMPLETED)
                self._snapshot_task_to_projects_best_effort(task_id)
                return
            if latest["status"] in {JobStatus.ERROR, JobStatus.TIMED_OUT}:
                self.store.set_task_status(task_id, TaskStatus.FAILED)
                self._snapshot_task_to_projects_best_effort(task_id)
                self._close_task_workers(task_id)
                return

        self.store.set_task_status(task_id, TaskStatus.COMPLETED)
        self._snapshot_task_to_projects_best_effort(task_id)

    def _get_or_create_task_worker(
        self,
        role: str,
        task_id: str,
        *,
        allow_reuse: bool = True,
    ) -> dict[str, Any]:
        role_key = role.strip().casefold()
        if allow_reuse:
            for worker in self.store.list_workers():
                if worker["task_id"] != task_id:
                    continue
                if worker["role"].strip().casefold() != role_key:
                    continue
                if worker["status"] == WorkerStatus.ERROR:
                    try:
                        self.chat_close(worker["id"])
                    except Exception:
                        self.store.update_worker(worker["id"], status=WorkerStatus.CLOSED)
                    continue
                if not self.store.list_jobs(
                    worker_id=worker["id"],
                    include_terminal=False,
                ):
                    return worker
        return self.chat_create(role, task_id)

    def _select_rework_workers(
        self,
        task_id: str,
        requested_roles: list[str],
    ) -> list[dict[str, Any]]:
        workers = [
            worker
            for worker in self.store.list_workers()
            if worker["task_id"] == task_id
            and worker["role"].strip().casefold() != "reviewer"
        ]
        if not workers:
            return []

        if not requested_roles:
            return workers

        requested = {role.strip().casefold() for role in requested_roles if role.strip()}
        matched = [
            worker
            for worker in workers
            if worker["role"].strip().casefold() in requested
        ]
        return matched or workers

    def _latest_worker_result(self, worker_id: str) -> str | None:
        jobs = self.store.list_jobs(worker_id=worker_id, kind=JobKind.WORKER)
        completed = [job for job in jobs if job["status"] == JobStatus.COMPLETED]
        if completed:
            return completed[-1]["result"]
        return self.store.get_worker(worker_id).get("last_result")

    @staticmethod
    def _clean_worker_result(result: str) -> str:
        """Remove ChatGPT page chrome that may trail an otherwise complete answer."""
        text = str(result or "")
        lines = text.splitlines()
        while lines and not lines[-1].strip():
            lines.pop()

        footer_prefixes = (
            "chatgpt có thể mắc lỗi",
            "chatgpt can make mistakes",
            "phản hồi mới nhất",
            "latest response",
        )
        start = max(0, len(lines) - 8)
        for index in range(start, len(lines)):
            folded = lines[index].strip().casefold()
            if any(folded.startswith(prefix) for prefix in footer_prefixes):
                lines = lines[:index]
                break

        return "\n".join(lines).strip()

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
