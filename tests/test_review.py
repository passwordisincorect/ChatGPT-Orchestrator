from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from chatgpt_orchestrator.adapters import WorkerAdapter
from chatgpt_orchestrator.models import Settings
from chatgpt_orchestrator.orchestrator import Orchestrator
from chatgpt_orchestrator.store import Store


class ReviewAdapter(WorkerAdapter):
    def __init__(self):
        self.sessions = set()
        self.prompts = []

    def create(self, role: str) -> str:
        session = f"review-test:{role}:{len(self.sessions)}"
        self.sessions.add(session)
        return session

    def send(self, session_id, prompt, *, cancel_event=None, timeout_seconds=None):
        del session_id, cancel_event, timeout_seconds
        self.prompts.append(prompt)
        if "You are the final reviewer" in prompt:
            return "FINAL_REVIEW"
        return f"WORKER_RESULT:{prompt}"

    def close(self, session_id: str) -> None:
        self.sessions.discard(session_id)


class ReviewTests(unittest.TestCase):
    def make_core(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / "review.db")
        adapter = ReviewAdapter()
        settings = Settings(
            backend="simulated",
            max_active_workers=3,
            database_path=db,
            default_job_timeout_seconds=2,
        )
        return Orchestrator(settings, Store(db), adapter), adapter

    def wait_terminal(self, core, job_id, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = core.chat_job_status(job_id)
            if job["status"] in {"COMPLETED", "ERROR", "TIMED_OUT", "CANCELLED"}:
                return job
            time.sleep(0.02)
        self.fail(f"job did not finish: {job_id}")

    def test_collect_review_finalize(self):
        core, adapter = self.make_core()
        task = core.task_create("Choose a robust architecture")

        architect = core.chat_create("architect", task["id"])
        critic = core.chat_create("critic", task["id"])

        ja = core.chat_submit(architect["id"], "proposal A")
        jb = core.chat_submit(critic["id"], "proposal B")
        self.assertEqual(self.wait_terminal(core, ja["id"])["status"], "COMPLETED")
        self.assertEqual(self.wait_terminal(core, jb["id"])["status"], "COMPLETED")

        collected = core.task_collect(task["id"])
        self.assertTrue(collected["ready"])
        self.assertEqual(collected["completed_count"], 2)

        review = core.task_review_submit(
            task["id"],
            instructions="Return the strongest consolidated answer.",
        )
        review_job = self.wait_terminal(core, review["review_job_id"])
        self.assertEqual(review_job["kind"], "review")
        self.assertEqual(review_job["result"], "FINAL_REVIEW")

        status = core.task_review_status(task["id"])
        self.assertTrue(status["has_review"])
        self.assertEqual(status["status"], "COMPLETED")

        final = core.task_finalize(task["id"])
        self.assertTrue(final["ready"])
        self.assertTrue(final["reviewed"])
        self.assertEqual(final["result"], "FINAL_REVIEW")
        self.assertEqual(len(final["worker_results"]), 2)

        review_prompt = adapter.prompts[-1]
        self.assertIn("WORKER_RESULT:proposal A", review_prompt)
        self.assertIn("WORKER_RESULT:proposal B", review_prompt)
        self.assertIn("Choose a robust architecture", review_prompt)

    def test_review_requires_completed_worker_result(self):
        core, _adapter = self.make_core()
        task = core.task_create("empty")
        with self.assertRaises(RuntimeError):
            core.task_review_submit(task["id"])


if __name__ == "__main__":
    unittest.main()
