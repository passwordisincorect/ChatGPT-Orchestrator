from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from chatgpt_orchestrator.adapters import SimulatedAdapter
from chatgpt_orchestrator.models import JobStatus, Settings
from chatgpt_orchestrator.orchestrator import Orchestrator
from chatgpt_orchestrator.store import Store


class ProjectStateTests(unittest.TestCase):
    def make_core(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / "project-state.db")
        settings = Settings(
            backend="simulated",
            max_active_workers=3,
            database_path=db,
            default_job_timeout_seconds=2,
        )
        core = Orchestrator(settings, Store(db), SimulatedAdapter())
        return core, db

    def wait_terminal(self, core: Orchestrator, job_id: str, timeout: float = 3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = core.chat_job_status(job_id)
            if job["status"] in {
                JobStatus.COMPLETED,
                JobStatus.ERROR,
                JobStatus.TIMED_OUT,
                JobStatus.CANCELLED,
            }:
                return job
            time.sleep(0.02)
        self.fail(f"job did not finish: {job_id}")

    def test_legacy_database_migrates_additively(self):
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "legacy.db")
            con = sqlite3.connect(db)
            con.execute(
                """CREATE TABLE tasks(
                    id TEXT PRIMARY KEY,
                    goal TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            con.execute(
                "INSERT INTO tasks(id, goal, status) VALUES('T-old','legacy','COMPLETED')"
            )
            con.commit()
            con.close()

            store = Store(db)

            self.assertEqual(store.get_task("T-old")["goal"], "legacy")
            self.assertEqual(store.list_projects(), [])
            project = store.create_project("Migrated")
            self.assertTrue(project["id"].startswith("P-"))

    def test_project_update_uses_optimistic_concurrency(self):
        core, _db = self.make_core()
        project = core.project_create(
            "Agent OS",
            summary="Initial",
            goals=["Keep state"],
            decisions=["UIA fallback stays safe"],
            current_phase="design",
            next_actions=["Implement v0.7"],
        )

        updated = core.project_update(
            project["id"],
            project["state_version"],
            summary="Updated by human",
            current_phase="implementation",
            next_actions=["Run tests"],
        )

        self.assertEqual(updated["state_version"], 2)
        self.assertEqual(updated["summary"], "Updated by human")
        self.assertEqual(updated["current_phase"], "implementation")
        with self.assertRaisesRegex(RuntimeError, "PROJECT_VERSION_CONFLICT"):
            core.project_update(
                project["id"],
                project["state_version"],
                summary="stale write",
            )

    def test_linked_task_gets_read_only_project_context_and_snapshot(self):
        core, _db = self.make_core()
        project = core.project_create(
            "Persistent Project",
            summary="Human summary",
            goals=["Preserve state"],
            decisions=["Do not overwrite human state"],
            current_phase="build",
            next_actions=["Finish task"],
        )

        started = core.task_auto_start(
            "Return a small implementation note",
            worker_count=1,
            review=False,
            project_id=project["id"],
        )
        prompt = started["plan"]["workers"][0]["prompt"]
        self.assertIn("PROJECT STATE (read-only context):", prompt)
        self.assertIn("Human summary", prompt)
        self.assertIn("Do not overwrite human state", prompt)

        self.wait_terminal(core, started["jobs"][0]["job_id"])
        final = core.task_auto_advance(started["task_id"], review=False)
        self.assertEqual(final["status"], "COMPLETED")

        loaded = core.project_get(project["id"], recent_events=20)
        outcome_events = [
            event
            for event in loaded["recent_events"]
            if event["event_type"] == "task_outcome"
            and event["task_id"] == started["task_id"]
        ]
        self.assertTrue(outcome_events)
        self.assertEqual(loaded["summary"], "Human summary")
        self.assertEqual(loaded["decisions"], ["Do not overwrite human state"])
        self.assertEqual(loaded["current_phase"], "build")
        self.assertEqual(loaded["next_actions"], ["Finish task"])
        self.assertEqual(loaded["state_version"], 1)

    def test_terminal_task_can_attach_to_multiple_projects_idempotently(self):
        core, _db = self.make_core()
        task = core.task_create("Historical task")
        worker = core.chat_create("solver", task["id"])
        job = core.chat_submit(worker["id"], "historical result")
        self.wait_terminal(core, job["id"])
        core.task_finalize(task["id"])

        p1 = core.project_create("One")
        p2 = core.project_create("Two")

        first = core.project_attach_task(p1["id"], task["id"])
        second = core.project_attach_task(p2["id"], task["id"])
        repeat = core.project_attach_task(p1["id"], task["id"])

        self.assertTrue(first["attached"])
        self.assertTrue(second["attached"])
        self.assertFalse(repeat["attached"])
        self.assertEqual(
            set(core.task_get(task["id"])["project_ids"]),
            {p1["id"], p2["id"]},
        )
        for project_id in (p1["id"], p2["id"]):
            loaded = core.project_get(project_id, recent_events=20)
            outcomes = [
                event
                for event in loaded["recent_events"]
                if event["event_type"] == "task_outcome"
            ]
            self.assertEqual(len(outcomes), 1)

    def test_project_persists_across_orchestrator_restart(self):
        core, db = self.make_core()
        project = core.project_create(
            "Restart Project",
            summary="Persist me",
            goals=["survive restart"],
        )
        task = core.task_create("Persist task", project_id=project["id"])

        settings = Settings(
            backend="simulated",
            max_active_workers=3,
            database_path=db,
            default_job_timeout_seconds=2,
        )
        restarted = Orchestrator(settings, Store(db), SimulatedAdapter())
        loaded = restarted.project_get(project["id"])

        self.assertEqual(loaded["summary"], "Persist me")
        self.assertEqual(loaded["goals"], ["survive restart"])
        self.assertIn(task["id"], loaded["related_task_ids"])

    def test_startup_reconciliation_restores_missing_outcome_snapshot(self):
        core, db = self.make_core()
        project = core.project_create("Recover snapshots")
        started = core.task_auto_start(
            "Complete for reconciliation",
            worker_count=1,
            review=False,
            project_id=project["id"],
        )
        self.wait_terminal(core, started["jobs"][0]["job_id"])
        core.task_auto_advance(started["task_id"], review=False)

        con = sqlite3.connect(db)
        con.execute(
            "DELETE FROM project_events WHERE project_id=? AND task_id=? AND event_type='task_outcome'",
            (project["id"], started["task_id"]),
        )
        con.commit()
        con.close()

        settings = Settings(
            backend="simulated",
            max_active_workers=3,
            database_path=db,
            default_job_timeout_seconds=2,
        )
        restarted = Orchestrator(settings, Store(db), SimulatedAdapter())
        loaded = restarted.project_get(project["id"], recent_events=20)

        outcomes = [
            event
            for event in loaded["recent_events"]
            if event["event_type"] == "task_outcome"
            and event["task_id"] == started["task_id"]
        ]
        self.assertEqual(len(outcomes), 1)

    def test_reviewer_prompt_receives_project_context(self):
        core, _db = self.make_core()
        project = core.project_create(
            "Review Context",
            summary="Reviewer must see this summary",
            decisions=["Keep transport unchanged"],
        )
        started = core.task_auto_start(
            "Review-aware task",
            worker_count=1,
            review=True,
            project_id=project["id"],
        )
        self.wait_terminal(core, started["jobs"][0]["job_id"])

        submitted = core.task_review_submit(started["task_id"])
        review_job = core.store.get_job(submitted["review_job_id"])

        self.assertIn("PROJECT STATE (read-only context):", review_job["prompt"])
        self.assertIn("Reviewer must see this summary", review_job["prompt"])
        self.assertIn("Keep transport unchanged", review_job["prompt"])

        self.wait_terminal(core, submitted["review_job_id"])
        core.task_auto_advance(started["task_id"], review=True)

    def test_project_list_is_compact(self):
        core, _db = self.make_core()
        project = core.project_create("Compact", summary="large detail")

        items = core.project_list()
        item = next(value for value in items if value["id"] == project["id"])

        self.assertEqual(item["name"], "Compact")
        self.assertIn("task_count", item)
        self.assertNotIn("summary", item)
        self.assertNotIn("recent_events", item)


if __name__ == "__main__":
    unittest.main()
