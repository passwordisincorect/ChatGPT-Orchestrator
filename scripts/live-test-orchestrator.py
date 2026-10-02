from __future__ import annotations

import json
import tempfile
import traceback
from pathlib import Path

from chatgpt_orchestrator.models import Settings
from chatgpt_orchestrator.orchestrator import Orchestrator
from chatgpt_orchestrator.store import Store

ROOT = Path(__file__).resolve().parents[1]
RESULT = ROOT / "data" / "live-orchestrator-result.json"
RESULT.parent.mkdir(parents=True, exist_ok=True)
if RESULT.exists():
    RESULT.unlink()

report = {"status": "started"}
worker_id = None
try:
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "orchestrator.db")
        settings = Settings(
            backend="edge",
            max_active_workers=3,
            database_path=db,
            edge_executable=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            chat_url="https://chatgpt.com/",
            create_timeout_seconds=20,
            send_timeout_seconds=45,
            stable_seconds=2,
        )
        core = Orchestrator(settings=settings, store=Store(db))

        task = core.task_create("Live v0.2 orchestration verification")
        worker = core.chat_create("reviewer", task["id"])
        worker_id = worker["id"]

        sent = core.chat_send(
            worker_id,
            "Reply exactly with: ORCH_CORE_V02_OK",
        )
        read = core.chat_read(worker_id)
        status = core.chat_status(worker_id)
        closed = core.chat_close(worker_id)

        report.update({
            "task_id": task["id"],
            "worker_id": worker_id,
            "worker_session": worker["session_id"],
            "send_status": sent["status"],
            "read": read,
            "status_before_close": status,
            "close_status": closed["status"],
            "passed": (
                sent["status"] == "COMPLETED"
                and read["result"].strip() == "ORCH_CORE_V02_OK"
                and closed["status"] == "CLOSED"
            ),
        })
        report["status"] = "passed" if report["passed"] else "failed"
except Exception as exc:
    report["status"] = "error"
    report["error_type"] = type(exc).__name__
    report["error"] = str(exc)
    report["traceback"] = traceback.format_exc()

RESULT.write_text(json.dumps(report, indent=2), encoding="utf-8")
