from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from chatgpt_orchestrator.store import Store


class StoreTests(unittest.TestCase):
    def test_persists_task_across_store_instances(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "db.sqlite3")
            first = Store(path)
            task = first.create_task("persistent")
            second = Store(path)
            self.assertEqual(second.get_task(task["id"])["goal"], "persistent")

    def test_persists_terminal_job(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "db.sqlite3")
            store = Store(path)
            worker = store.create_worker("reviewer", "sim:1", None)
            job = store.create_job(
                worker["id"],
                "persist job",
                timeout_seconds=10,
                max_retries=0,
            )
            store.set_job_running(job["id"], 1)
            from chatgpt_orchestrator.models import JobStatus
            store.finish_job(job["id"], JobStatus.COMPLETED, result="ok")

            second = Store(path)
            loaded = second.get_job(job["id"])
            self.assertEqual(loaded["status"], "COMPLETED")
            self.assertEqual(loaded["result"], "ok")


if __name__ == "__main__":
    unittest.main()
