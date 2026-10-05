from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from chatgpt_orchestrator.adapters import NonRetryableWorkerError, WorkerAdapter
from chatgpt_orchestrator.models import Settings
from chatgpt_orchestrator.orchestrator import Orchestrator
from chatgpt_orchestrator.store import Store


class BlockingAdapter(WorkerAdapter):
    def __init__(self):
        self.sessions = set()

    def create(self, role: str) -> str:
        session = f"block:{role}:{len(self.sessions)}"
        self.sessions.add(session)
        return session

    def send(self, session_id, prompt, *, cancel_event=None, timeout_seconds=None):
        deadline = time.monotonic() + float(timeout_seconds or 5)
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("cancelled")
            time.sleep(0.01)
        raise TimeoutError("test timeout")

    def cancel(self, session_id: str) -> bool:
        return session_id in self.sessions

    def close(self, session_id: str) -> None:
        self.sessions.discard(session_id)


class FlakyAdapter(WorkerAdapter):
    def __init__(self):
        self.sessions = set()
        self.calls = 0

    def create(self, role: str) -> str:
        session = f"flaky:{role}"
        self.sessions.add(session)
        return session

    def send(self, session_id, prompt, *, cancel_event=None, timeout_seconds=None):
        del cancel_event, timeout_seconds
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("first attempt fails")
        return f"recovered:{prompt}"

    def close(self, session_id: str) -> None:
        self.sessions.discard(session_id)


class NonRetryableAdapter(WorkerAdapter):
    def __init__(self):
        self.sessions = set()
        self.calls = 0

    def create(self, role: str) -> str:
        session = f"nonretry:{role}"
        self.sessions.add(session)
        return session

    def send(self, session_id, prompt, *, cancel_event=None, timeout_seconds=None):
        del session_id, prompt, cancel_event, timeout_seconds
        self.calls += 1
        raise NonRetryableWorkerError("submission state is ambiguous")

    def close(self, session_id: str) -> None:
        self.sessions.discard(session_id)


class ImmediateAdapter(WorkerAdapter):
    def __init__(self):
        self.sessions = set()
        self.gate = threading.Barrier(2)

    def create(self, role: str) -> str:
        session = f"instant:{role}:{len(self.sessions)}"
        self.sessions.add(session)
        return session

    def send(self, session_id, prompt, *, cancel_event=None, timeout_seconds=None):
        del session_id, cancel_event, timeout_seconds
        return f"done:{prompt}"

    def close(self, session_id: str) -> None:
        self.sessions.discard(session_id)


class AsyncJobTests(unittest.TestCase):
    def make_core(self, adapter: WorkerAdapter) -> Orchestrator:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / "orchestrator.db")
        settings = Settings(
            backend="simulated",
            max_active_workers=3,
            database_path=db,
            default_job_timeout_seconds=1.0,
            default_max_retries=0,
        )
        return Orchestrator(settings, Store(db), adapter)

    def wait_terminal(self, core: Orchestrator, job_id: str, timeout: float = 3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = core.chat_job_status(job_id)
            if job["status"] in {"COMPLETED", "ERROR", "TIMED_OUT", "CANCELLED"}:
                return job
            time.sleep(0.02)
        self.fail(f"job did not finish: {job_id}")

    def test_submit_poll_collect(self):
        core = self.make_core(ImmediateAdapter())
        task = core.task_create("parallel-shaped task")
        a = core.chat_create("architect", task["id"])
        b = core.chat_create("reviewer", task["id"])

        ja = core.chat_submit(a["id"], "architecture")
        jb = core.chat_submit(b["id"], "review")
        self.assertEqual(self.wait_terminal(core, ja["id"])["status"], "COMPLETED")
        self.assertEqual(self.wait_terminal(core, jb["id"])["status"], "COMPLETED")

        collected = core.task_collect(task["id"])
        self.assertTrue(collected["ready"])
        self.assertEqual(collected["completed_count"], 2)
        self.assertIn("done:architecture", collected["combined"])
        self.assertIn("done:review", collected["combined"])
        self.assertEqual(collected["task_status"], "COMPLETED")

    def test_cancel_running_job(self):
        core = self.make_core(BlockingAdapter())
        worker = core.chat_create("developer")
        job = core.chat_submit(worker["id"], "long work", timeout_seconds=2)
        time.sleep(0.05)
        core.chat_cancel(job["id"])
        terminal = self.wait_terminal(core, job["id"])
        self.assertEqual(terminal["status"], "CANCELLED")

    def test_timeout_job(self):
        core = self.make_core(BlockingAdapter())
        worker = core.chat_create("developer")
        job = core.chat_submit(worker["id"], "timeout", timeout_seconds=0.08)
        terminal = self.wait_terminal(core, job["id"])
        self.assertEqual(terminal["status"], "TIMED_OUT")

    def test_automatic_retry(self):
        adapter = FlakyAdapter()
        core = self.make_core(adapter)
        worker = core.chat_create("debugger")
        job = core.chat_submit(worker["id"], "retry me", max_retries=1)
        terminal = self.wait_terminal(core, job["id"])
        self.assertEqual(terminal["status"], "COMPLETED")
        self.assertEqual(terminal["attempts"], 2)
        self.assertEqual(terminal["result"], "recovered:retry me")

    def test_non_retryable_error_ignores_retry_budget(self):
        adapter = NonRetryableAdapter()
        core = self.make_core(adapter)
        worker = core.chat_create("developer")
        job = core.chat_submit(worker["id"], "do not replay", max_retries=3)

        terminal = self.wait_terminal(core, job["id"])

        self.assertEqual(terminal["status"], "ERROR")
        self.assertEqual(terminal["attempts"], 1)
        self.assertEqual(adapter.calls, 1)
        self.assertIn("NON_RETRYABLE:", terminal["error"])
        with self.assertRaisesRegex(RuntimeError, "non-retryable"):
            core.chat_retry(job["id"])

    def test_one_active_job_per_worker(self):
        core = self.make_core(BlockingAdapter())
        worker = core.chat_create("developer")
        job = core.chat_submit(worker["id"], "first", timeout_seconds=1)
        with self.assertRaises(RuntimeError):
            core.chat_submit(worker["id"], "second")
        core.chat_cancel(job["id"])
        self.wait_terminal(core, job["id"])


if __name__ == "__main__":
    unittest.main()
