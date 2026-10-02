from __future__ import annotations

import json
import tempfile
import time
import traceback
from pathlib import Path

from chatgpt_orchestrator.models import Settings
from chatgpt_orchestrator.orchestrator import Orchestrator
from chatgpt_orchestrator.store import Store

ROOT = Path(__file__).resolve().parents[1]
RESULT = ROOT / "data" / "live-v03-result.json"
RESULT.parent.mkdir(parents=True, exist_ok=True)
if RESULT.exists():
    RESULT.unlink()

report = {"status": "started"}
workers = []
try:
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "v03-live.db")
        settings = Settings(
            backend="edge",
            max_active_workers=3,
            database_path=db,
            edge_executable=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            chat_url="https://chatgpt.com/",
            create_timeout_seconds=20,
            send_timeout_seconds=60,
            stable_seconds=2,
            default_job_timeout_seconds=60,
            default_max_retries=0,
        )
        core = Orchestrator(settings=settings, store=Store(db))
        task = core.task_create("Live v0.3 multi-worker verification")

        specs = [
            ("architect", "Reply exactly with: ORCH_V03_ARCH_OK", "ORCH_V03_ARCH_OK"),
            ("reviewer", "Reply exactly with: ORCH_V03_REVIEW_OK", "ORCH_V03_REVIEW_OK"),
        ]

        jobs = []
        for role, prompt, expected in specs:
            worker = core.chat_create(role, task["id"])
            workers.append(worker["id"])
            job = core.chat_submit(
                worker["id"],
                prompt,
                timeout_seconds=60,
                max_retries=0,
            )
            jobs.append((job["id"], worker["id"], role, expected))

        start = time.monotonic()
        deadline = start + 75
        snapshots = []
        terminal = {}

        while time.monotonic() < deadline:
            terminal.clear()
            snapshot = []
            for job_id, worker_id, role, expected in jobs:
                job = core.chat_job_status(job_id)
                snapshot.append({
                    "job_id": job_id,
                    "worker_id": worker_id,
                    "role": role,
                    "status": job["status"],
                    "attempts": job["attempts"],
                })
                if job["status"] in {"COMPLETED", "ERROR", "TIMED_OUT", "CANCELLED"}:
                    terminal[job_id] = job
            snapshots.append(snapshot)
            if len(terminal) == len(jobs):
                break
            time.sleep(0.5)

        elapsed = round(time.monotonic() - start, 3)
        collected = core.task_collect(task["id"])

        checks = []
        for job_id, worker_id, role, expected in jobs:
            job = core.chat_job_status(job_id)
            checks.append({
                "job_id": job_id,
                "worker_id": worker_id,
                "role": role,
                "status": job["status"],
                "result": job["result"],
                "expected": expected,
                "passed": job["status"] == "COMPLETED"
                          and (job["result"] or "").strip() == expected,
            })

        close_results = []
        for worker_id in workers:
            close_results.append(core.chat_close(worker_id)["status"])

        report.update({
            "task_id": task["id"],
            "jobs": checks,
            "elapsed_seconds": elapsed,
            "collect": collected,
            "close_results": close_results,
            "passed": (
                all(item["passed"] for item in checks)
                and collected["ready"]
                and collected["completed_count"] == len(jobs)
                and all(status == "CLOSED" for status in close_results)
            ),
        })
        report["status"] = "passed" if report["passed"] else "failed"

except Exception as exc:
    report["status"] = "error"
    report["error_type"] = type(exc).__name__
    report["error"] = str(exc)
    report["traceback"] = traceback.format_exc()
finally:
    RESULT.write_text(json.dumps(report, indent=2), encoding="utf-8")
