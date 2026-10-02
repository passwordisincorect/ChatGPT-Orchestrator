from __future__ import annotations

import json
import traceback
from pathlib import Path

from chatgpt_orchestrator.edge_adapter import EdgeChatGPTAdapter

ROOT = Path(__file__).resolve().parents[1]
RESULT = ROOT / "data" / "live-edge-result.json"
RESULT.parent.mkdir(parents=True, exist_ok=True)
if RESULT.exists():
    RESULT.unlink()

session = None
report = {"status": "started"}
try:
    adapter = EdgeChatGPTAdapter(
        executable=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        chat_url="https://chatgpt.com/",
        create_timeout_seconds=20,
        send_timeout_seconds=45,
        stable_seconds=3,
    )
    session = adapter.create("reviewer")
    report["session"] = session
    report["before"] = adapter.inspect(session)
    result = adapter.send(session, "Reply exactly with: ORCH_LIVE_V02_OK")
    report["result"] = result
    report["after"] = adapter.inspect(session)
    report["passed"] = result.strip() == "ORCH_LIVE_V02_OK"
    report["status"] = "passed" if report["passed"] else "failed"
except Exception as exc:
    report["status"] = "error"
    report["error_type"] = type(exc).__name__
    report["error"] = str(exc)
    report["traceback"] = traceback.format_exc()
finally:
    try:
        if session:
            adapter.close(session)
            report["worker_close_requested"] = True
    except Exception as close_exc:
        report["close_error"] = str(close_exc)
    RESULT.write_text(json.dumps(report, indent=2), encoding="utf-8")
