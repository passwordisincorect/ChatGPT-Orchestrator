from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from chatgpt_orchestrator.adapters import SimulatedAdapter, WorkerAdapter
from chatgpt_orchestrator.autonomy import parse_review_verdict
from chatgpt_orchestrator.models import JobKind, JobStatus, Settings, WorkerStatus
from chatgpt_orchestrator.orchestrator import Orchestrator
from chatgpt_orchestrator.store import Store


class AutoAdapter(WorkerAdapter):
    def __init__(self):
        self.sessions: dict[str, str] = {}
        self.counter = 0
        self.review_count = 0

    def create(self, role: str) -> str:
        self.counter += 1
        session = f"auto:{self.counter}"
        self.sessions[session] = role
        return session

    def send(self, session_id, prompt, *, cancel_event=None, timeout_seconds=None):
        del cancel_event, timeout_seconds
        role = self.sessions[session_id]
        if role == "reviewer":
            self.review_count += 1
            if self.review_count == 1:
                return "ORCH_REVIEW: REWORK architect\nThe architecture must address recovery."
            return "ORCH_REVIEW: PASS\nFINAL_AUTO_REVIEW"
        if role == "architect":
            if prompt.startswith("REWORK SOURCE:"):
                return "ARCH_FIXED_WITH_RECOVERY"
            return "ARCH_INITIAL"
        return f"{role.upper()}_RESULT"

    def cancel(self, session_id: str) -> bool:
        return session_id in self.sessions

    def close(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)


class PassAdapter(AutoAdapter):
    def send(self, session_id, prompt, *, cancel_event=None, timeout_seconds=None):
        role = self.sessions[session_id]
        if role == "reviewer":
            return "ORCH_REVIEW: PASS\nFINAL_PASS"
        return f"{role}:{prompt}"


class FailAdapter(AutoAdapter):
    def send(self, session_id, prompt, *, cancel_event=None, timeout_seconds=None):
        del session_id, prompt, cancel_event, timeout_seconds
        raise RuntimeError("simulated worker failure")


class AutonomyTests(unittest.TestCase):
    def make_core(self, adapter: WorkerAdapter | None = None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / "auto.db")
        settings = Settings(
            backend="simulated",
            max_active_workers=3,
            database_path=db,
            default_job_timeout_seconds=2,
        )
        return Orchestrator(settings, Store(db), adapter or SimulatedAdapter()), db

    def wait_job(self, core: Orchestrator, job_id: str, timeout: float = 3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = core.chat_job_status(job_id)
            if job["status"] in {"COMPLETED", "ERROR", "TIMED_OUT", "CANCELLED"}:
                return job
            time.sleep(0.02)
        self.fail(f"job did not finish: {job_id}")

    def wait_all(self, core: Orchestrator, task_id: str, timeout: float = 3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            active = core.chat_job_list(task_id=task_id, include_terminal=False)
            if not active:
                return
            time.sleep(0.02)
        self.fail(f"task jobs did not finish: {task_id}")

    def test_review_parser_accepts_wrapped_pass_marker(self):
        verdict = parse_review_verdict(
            "ORCH_\nREVIEW:\nPASS\nFINAL"
        )
        self.assertEqual(verdict["verdict"], "PASS")
        self.assertTrue(verdict["explicit"])

    def test_review_parser_accepts_wrapped_rework_marker(self):
        verdict = parse_review_verdict(
            "ORCH_\nREVIEW:\nREWORK architect, critic\nDetails"
        )
        self.assertEqual(verdict["verdict"], "REWORK")
        self.assertEqual(verdict["roles"], ["architect", "critic"])
        self.assertTrue(verdict["explicit"])

    def test_review_parser_keeps_legacy_unmarked_pass_non_explicit(self):
        verdict = parse_review_verdict("Looks good to me.")
        self.assertEqual(verdict["verdict"], "PASS")
        self.assertFalse(verdict["explicit"])

    def test_plan_reserves_reviewer_slot_and_assigns_roles(self):
        core, _db = self.make_core()
        plan = core.task_plan("Triển khai kiến trúc phần mềm MCP", review=True)
        self.assertEqual(plan["worker_count"], 2)
        self.assertTrue(plan["review_enabled"])
        self.assertTrue(plan["review_slot_reserved"])
        self.assertEqual(
            [worker["role"] for worker in plan["workers"]],
            ["architect", "implementer"],
        )

    def test_auto_start_review_and_finalize(self):
        core, _db = self.make_core(PassAdapter())
        started = core.task_auto_start("Analyze this design", review=True)
        self.assertEqual(len(started["jobs"]), 2)
        self.wait_all(core, started["task_id"])

        advance = core.task_auto_advance(started["task_id"])
        self.assertEqual(advance["phase"], "review_submitted")
        self.wait_all(core, started["task_id"])

        final = core.task_auto_advance(started["task_id"])
        self.assertEqual(final["phase"], "completed")
        self.assertTrue(final["ready"])
        self.assertEqual(final["result"], "ORCH_REVIEW: PASS\nFINAL_PASS")

        self.assertEqual(core.store.active_worker_count(), 0)
        task_workers = [
            worker
            for worker in core.chat_list(include_closed=True)
            if worker["task_id"] == started["task_id"]
        ]
        self.assertEqual(len(task_workers), 3)
        self.assertTrue(
            all(worker["status"] == WorkerStatus.CLOSED for worker in task_workers)
        )

        next_task = core.task_auto_start("Analyze another design", review=False)
        self.assertEqual(len(next_task["jobs"]), 2)
        self.wait_all(core, next_task["task_id"])
        next_final = core.task_auto_advance(next_task["task_id"], review=False)
        self.assertEqual(next_final["phase"], "completed_without_review")
        self.assertEqual(core.store.active_worker_count(), 0)

    def test_failed_auto_advance_releases_error_workers(self):
        core, _db = self.make_core(FailAdapter())
        started = core.task_auto_start(
            "Fail this delegated task",
            worker_count=1,
            review=False,
        )
        self.wait_all(core, started["task_id"])

        self.assertEqual(core.store.active_worker_count(), 1)
        final = core.task_auto_advance(started["task_id"], review=False)

        self.assertEqual(final["phase"], "failed")
        self.assertFalse(final["ready"])
        self.assertEqual(core.store.active_worker_count(), 0)
        task_workers = [
            worker
            for worker in core.chat_list(include_closed=True)
            if worker["task_id"] == started["task_id"]
        ]
        self.assertEqual(len(task_workers), 1)
        self.assertEqual(task_workers[0]["status"], WorkerStatus.CLOSED)

    def test_auto_start_without_review_releases_workers(self):
        core, _db = self.make_core(PassAdapter())
        started = core.task_auto_start("Analyze without review", review=False)
        self.wait_all(core, started["task_id"])

        final = core.task_auto_advance(started["task_id"], review=False)
        self.assertEqual(final["phase"], "completed_without_review")
        self.assertTrue(final["ready"])
        self.assertEqual(core.store.active_worker_count(), 0)

        task_workers = [
            worker
            for worker in core.chat_list(include_closed=True)
            if worker["task_id"] == started["task_id"]
        ]
        self.assertEqual(len(task_workers), 2)
        self.assertTrue(
            all(worker["status"] == WorkerStatus.CLOSED for worker in task_workers)
        )

    def test_direct_finalize_rework_keeps_workers_available(self):
        adapter = AutoAdapter()
        core, _db = self.make_core(adapter)
        task = core.task_create("Preserve workers for rework after direct finalize")
        architect = core.chat_create("architect", task["id"])
        critic = core.chat_create("critic", task["id"])
        core.chat_submit(architect["id"], "initial architecture")
        core.chat_submit(critic["id"], "initial critique")
        self.wait_all(core, task["id"])

        review = core.task_review_submit(task["id"])
        self.wait_job(core, review["review_job_id"])

        final = core.task_finalize(task["id"])
        self.assertFalse(final["ready"])
        self.assertEqual(final["status"], "REWORK_REQUIRED")
        self.assertEqual(core.store.active_worker_count(), 3)

        rework = core.task_auto_advance(task["id"], max_rework_rounds=1)
        self.assertEqual(rework["phase"], "rework_submitted")
        self.assertEqual([job["role"] for job in rework["jobs"]], ["architect"])
        self.wait_all(core, task["id"])
        core._join_task_threads(task["id"])
        core.task_cancel(task["id"])
        self.assertEqual(core.store.active_worker_count(), 0)

    def test_rework_loop_reuses_reviewer_worker(self):
        adapter = AutoAdapter()
        core, _db = self.make_core(adapter)
        started = core.task_auto_start(
            "Implement and design recovery architecture",
            review=True,
        )
        task_id = started["task_id"]
        self.wait_all(core, task_id)

        first_review = core.task_auto_advance(task_id)
        self.assertEqual(first_review["phase"], "review_submitted")
        self.wait_all(core, task_id)

        rework = core.task_auto_advance(task_id, max_rework_rounds=1)
        self.assertEqual(rework["phase"], "rework_submitted")
        self.assertEqual([job["role"] for job in rework["jobs"]], ["architect"])
        self.wait_all(core, task_id)

        second_review = core.task_auto_advance(task_id, max_rework_rounds=1)
        self.assertEqual(second_review["phase"], "re_review_submitted")
        self.wait_all(core, task_id)

        final = core.task_auto_advance(task_id, max_rework_rounds=1)
        self.assertEqual(final["phase"], "completed")
        self.assertTrue(final["ready"])
        self.assertIn("FINAL_AUTO_REVIEW", final["result"])

        reviews = core.store.list_jobs(task_id=task_id, kind=JobKind.REVIEW)
        self.assertEqual(len(reviews), 2)
        self.assertEqual(reviews[0]["worker_id"], reviews[1]["worker_id"])
        self.assertEqual(len(core.chat_list()), 0)

        task_workers = [
            worker
            for worker in core.chat_list(include_closed=True)
            if worker["task_id"] == task_id
        ]
        self.assertEqual(len(task_workers), 3)
        self.assertTrue(
            all(worker["status"] == WorkerStatus.CLOSED for worker in task_workers)
        )

    def test_restart_recovers_queued_job_but_not_running_job(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / "recovery.db")

        first_store = Store(db)
        task = first_store.create_task("recover me")

        queued_worker = first_store.create_worker(
            "architect",
            "queued-session",
            task["id"],
        )
        queued_job = first_store.create_job(
            queued_worker["id"],
            "queued work",
            timeout_seconds=2,
            max_retries=0,
        )

        running_worker = first_store.create_worker(
            "critic",
            "running-session",
            task["id"],
        )
        running_job = first_store.create_job(
            running_worker["id"],
            "possibly submitted work",
            timeout_seconds=2,
            max_retries=0,
        )
        first_store.set_job_running(running_job["id"], 1)

        second_store = Store(db)
        queued_after = second_store.get_job(queued_job["id"])
        running_after = second_store.get_job(running_job["id"])

        self.assertEqual(queued_after["status"], JobStatus.ERROR)
        self.assertIn("safe to recover", queued_after["error"])
        self.assertEqual(running_after["status"], JobStatus.ERROR)
        self.assertIn("NON_RETRYABLE:", running_after["error"])
        self.assertEqual(
            second_store.get_worker(queued_worker["id"])["status"],
            WorkerStatus.CLOSED,
        )
        self.assertEqual(
            second_store.get_worker(running_worker["id"])["status"],
            WorkerStatus.CLOSED,
        )

        settings = Settings("simulated", 3, db, default_job_timeout_seconds=2)
        core = Orchestrator(settings, second_store, SimulatedAdapter())
        recovered = core.task_recover(task["id"])

        self.assertEqual(recovered["recovered_count"], 1)
        self.assertEqual(
            recovered["recovered"][0]["old_job_id"],
            queued_job["id"],
        )
        self.wait_job(core, recovered["recovered"][0]["new_job_id"])
        new_job = core.store.get_job(recovered["recovered"][0]["new_job_id"])
        self.assertEqual(new_job["status"], JobStatus.COMPLETED)

        with self.assertRaisesRegex(RuntimeError, "non-retryable"):
            core.chat_retry(running_job["id"])


if __name__ == "__main__":
    unittest.main()
