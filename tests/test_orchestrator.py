from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from chatgpt_orchestrator.adapters import SimulatedAdapter
from chatgpt_orchestrator.models import Settings
from chatgpt_orchestrator.orchestrator import Orchestrator
from chatgpt_orchestrator.store import Store


class OrchestratorTests(unittest.TestCase):
    def make_core(self, limit: int = 3) -> Orchestrator:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / "orchestrator.db")
        settings = Settings("simulated", limit, db)
        return Orchestrator(settings, Store(db), SimulatedAdapter())

    def make_dom_autoclose_core(
        self,
        limit: int = 3,
        idle_seconds: float = 30.0,
    ) -> Orchestrator:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / "orchestrator.db")
        settings = Settings(
            "dom_hybrid",
            limit,
            db,
            dom_idle_shutdown_seconds=idle_seconds,
        )
        return Orchestrator(settings, Store(db), SimulatedAdapter())

    def test_abandoned_standalone_idle_worker_auto_closes(self):
        core = self.make_dom_autoclose_core(idle_seconds=0.05)
        worker = core.chat_create("abandoned-demo")
        self.assertEqual(core.chat_status(worker["id"])["status"], "IDLE")
        time.sleep(0.12)
        self.assertEqual(core.chat_status(worker["id"])["status"], "CLOSED")

    def test_standalone_sync_worker_auto_closes_when_idle_shutdown_enabled(self):
        core = self.make_dom_autoclose_core()
        worker = core.chat_create("demo")
        sent = core.chat_send(worker["id"], "hello")
        self.assertEqual(sent["status"], "COMPLETED")
        self.assertEqual(core.chat_status(worker["id"])["status"], "CLOSED")
        self.assertIn("hello", core.chat_read(worker["id"])["result"])

    def test_task_owned_worker_is_not_auto_closed_before_task_finalization(self):
        core = self.make_dom_autoclose_core()
        task = core.task_create("Task with possible review")
        worker = core.chat_create("solver", task["id"])
        core.chat_send(worker["id"], "solve")
        self.assertEqual(core.chat_status(worker["id"])["status"], "COMPLETED")

    def test_standalone_async_worker_auto_closes_when_idle_shutdown_enabled(self):
        core = self.make_dom_autoclose_core()
        worker = core.chat_create("demo")
        job = core.chat_submit(worker["id"], "async hello", timeout_seconds=2.0)
        state = core.chat_job_status(job["id"])
        for _ in range(100):
            if state["status"] not in {"QUEUED", "RUNNING"}:
                break
            time.sleep(0.01)
            state = core.chat_job_status(job["id"])
        self.assertEqual(state["status"], "COMPLETED")
        self.assertEqual(core.chat_status(worker["id"])["status"], "CLOSED")
        self.assertIn("async hello", core.chat_read(worker["id"])["result"])

    def test_task_worker_send_read_close(self):
        core = self.make_core()
        task = core.task_create("Build a robot manager")
        worker = core.chat_create("architect", task["id"])
        self.assertEqual(core.task_get(task["id"])["status"], "RUNNING")
        sent = core.chat_send(worker["id"], "Design the architecture")
        self.assertEqual(sent["status"], "COMPLETED")
        self.assertIn("Design the architecture", core.chat_read(worker["id"])["result"])
        self.assertEqual(core.chat_close(worker["id"])["status"], "CLOSED")

    def test_hard_worker_limit(self):
        core = self.make_core(limit=3)
        for role in ("architect", "developer", "reviewer"):
            core.chat_create(role)
        with self.assertRaises(RuntimeError):
            core.chat_create("fourth")

    def test_cancel_closes_workers(self):
        core = self.make_core()
        task = core.task_create("Cancelled work")
        worker = core.chat_create("developer", task["id"])
        self.assertEqual(core.task_cancel(task["id"])["status"], "CANCELLED")
        self.assertEqual(core.chat_status(worker["id"])["status"], "CLOSED")


if __name__ == "__main__":
    unittest.main()
