from __future__ import annotations

import tempfile
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
