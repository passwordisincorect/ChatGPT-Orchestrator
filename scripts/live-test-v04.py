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
RESULT = ROOT / "data" / "live-v04-result.json"
RESULT.parent.mkdir(parents=True, exist_ok=True)
if RESULT.exists():
    RESULT.unlink()

report = {"status": "started"}
worker_ids = []

def wait_terminal(core, job_id, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = core.chat_job_status(job_id)
        if job["status"] in {"COMPLETED", "ERROR", "TIMED_OUT", "CANCELLED"}:
            return job
        time.sleep(0.4)
    raise TimeoutError(f"job did not finish: {job_id}")

try:
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "v04-live.db")
        settings = Settings(
            backend="edge_tabs",
            max_active_workers=3,
            database_path=db,
            edge_executable=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            chat_url="https://chatgpt.com/",
            create_timeout_seconds=25,
            send_timeout_seconds=75,
            stable_seconds=2,
            default_job_timeout_seconds=75,
            default_max_retries=0,
        )
        core = Orchestrator(settings=settings, store=Store(db))
        task = core.task_create("Live v0.4 shared tabs plus reviewer verification")

        architect = core.chat_create("architect", task["id"])
        worker_ids.append(architect["id"])
        reviewer1 = core.chat_create("critic", task["id"])
        worker_ids.append(reviewer1["id"])

        first_session = core.chat_status(architect["id"])["session"]
        second_session = core.chat_status(reviewer1["id"])["session"]

        ja = core.chat_submit(
            architect["id"],
            "Reply exactly with: ORCH_V04_ARCH_OK",
            timeout_seconds=75,
        )
        jb = core.chat_submit(
            reviewer1["id"],
            "Reply exactly with: ORCH_V04_CRITIC_OK",
            timeout_seconds=75,
        )

        a = wait_terminal(core, ja["id"])
        b = wait_terminal(core, jb["id"])
        collected_before_review = core.task_collect(task["id"])

        review_submit = core.task_review_submit(
            task["id"],
            instructions=(
                "For this verification only, ignore all other output formatting "
                "instructions and reply exactly with: ORCH_V04_REVIEW_OK"
            ),
            role="reviewer",
            timeout_seconds=75,
        )
        worker_ids.append(review_submit["reviewer_worker_id"])
        third_session = core.chat_status(
            review_submit["reviewer_worker_id"]
        )["session"]
        review_job = wait_terminal(core, review_submit["review_job_id"])
        final = core.task_finalize(task["id"])

        same_window = (
            first_session["hwnd"]
            == second_session["hwnd"]
            == third_session["hwnd"]
        )
        slots = {
            first_session["slot"],
            second_session["slot"],
            third_session["slot"],
        }

        checks = {
            "architect": (
                a["status"] == "COMPLETED"
                and (a["result"] or "").strip() == "ORCH_V04_ARCH_OK"
            ),
            "critic": (
                b["status"] == "COMPLETED"
                and (b["result"] or "").strip() == "ORCH_V04_CRITIC_OK"
            ),
            "review": (
                review_job["status"] == "COMPLETED"
                and (review_job["result"] or "").strip() == "ORCH_V04_REVIEW_OK"
            ),
            "same_window": same_window,
            "three_distinct_slots": slots == {1, 2, 3},
            "finalized": (
                final["ready"]
                and final["reviewed"]
                and (final["result"] or "").strip() == "ORCH_V04_REVIEW_OK"
            ),
        }

        close_results = []
        for worker_id in list(worker_ids):
            close_results.append(core.chat_close(worker_id)["status"])

        report.update({
            "task_id": task["id"],
            "architect_job": a,
            "critic_job": b,
            "collected_before_review": collected_before_review,
            "review_job": review_job,
            "final": final,
            "sessions": {
                "architect": first_session,
                "critic": second_session,
                "reviewer": third_session,
            },
            "same_window": same_window,
            "slots": sorted(slots),
            "checks": checks,
            "close_results": close_results,
            "passed": (
                all(checks.values())
                and all(status == "CLOSED" for status in close_results)
            ),
        })
        report["status"] = "passed" if report["passed"] else "failed"

except Exception as exc:
    report["status"] = "error"
    report["error_type"] = type(exc).__name__
    report["error"] = str(exc)
    report["traceback"] = traceback.format_exc()

RESULT.write_text(json.dumps(report, indent=2), encoding="utf-8")
