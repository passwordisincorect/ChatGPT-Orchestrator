from __future__ import annotations

import json
import time
from pathlib import Path

from chatgpt_orchestrator.orchestrator import Orchestrator

OUT = Path("D:/MCP-Test/ChatGPT-Orchestrator/data/e2e-project-v070-last.json")


def wait_terminal(core: Orchestrator, job_id: str, timeout: float = 150.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = core.chat_job_status(job_id)
        if job["status"] in {"COMPLETED", "ERROR", "TIMED_OUT", "CANCELLED"}:
            return job
        time.sleep(0.5)
    raise TimeoutError(f"E2E job did not finish: {job_id}")


core = Orchestrator()
existing = [
    p for p in core.project_list(limit=200)
    if p["name"] == "ChatGPT-Orchestrator" and p["status"] != "archived"
]
if existing:
    project = core.project_get(existing[0]["id"], recent_events=20)
else:
    project = core.project_create(
        "ChatGPT-Orchestrator",
        summary=(
            "Local multi-worker control plane for ChatGPT Web. "
            "v0.7.0 adds persistent project state while preserving v0.6.3 execution behavior."
        ),
        goals=[
            "Persist project context across ChatGPT conversations and Orchestrator restarts.",
            "Preserve task, reviewer/rework, recovery, and browser behavior from v0.6.3.",
            "Keep browser execution background-first with safe UIA fallback.",
        ],
        decisions=[
            "Persistent Project State is additive and opt-in through project_id.",
            "Automatic task snapshots never overwrite human-authored project fields.",
            "Explicit project updates use expected_version and reject stale writes.",
            "CDP challenge handling remains fail-closed; UIA fallback remains serialized.",
        ],
        current_phase="v0.7.0 live validation",
        status="active",
        next_actions=[
            "Validate a live project-linked task and task_outcome snapshot.",
            "Refresh ChatGPT connector schema so project_* tools are visible to MAIN.",
        ],
    )

before = core.project_get(project["id"], recent_events=20)
started = core.task_auto_start(
    "Reply exactly with: ORCH_V070_PROJECT_OK",
    worker_count=1,
    review=False,
    timeout_seconds=120,
    max_retries=0,
    project_id=project["id"],
)
job = wait_terminal(core, started["jobs"][0]["job_id"])
final = core.task_auto_advance(
    started["task_id"],
    review=False,
    max_rework_rounds=0,
    timeout_seconds=120,
)
after_task = core.project_get(project["id"], recent_events=30)
outcomes = [
    event
    for event in after_task["recent_events"]
    if event["event_type"] == "task_outcome"
    and event.get("task_id") == started["task_id"]
]
human_state_unchanged = all([
    after_task["summary"] == before["summary"],
    after_task["goals"] == before["goals"],
    after_task["decisions"] == before["decisions"],
    after_task["current_phase"] == before["current_phase"],
    after_task["status"] == before["status"],
    after_task["next_actions"] == before["next_actions"],
    after_task["state_version"] == before["state_version"],
])

updated = core.project_update(
    project["id"],
    after_task["state_version"],
    current_phase="v0.7.0 deployed",
    next_actions=[
        "Use project_id for future ChatGPT-Orchestrator work that needs durable context.",
        "Refresh/reopen the ChatGPT connector schema if project_* tools are not yet visible in an existing chat.",
    ],
)

conflict = None
try:
    core.project_update(
        project["id"],
        after_task["state_version"],
        summary="This stale write must not apply.",
    )
except Exception as exc:
    conflict = f"{type(exc).__name__}: {exc}"

result = {
    "version": core.orchestrator_info()["version"],
    "project_id": project["id"],
    "task_id": started["task_id"],
    "job_id": job["id"],
    "job_status": job["status"],
    "job_result": job.get("result"),
    "final_status": final.get("status"),
    "final_result": final.get("result"),
    "project_context_included": started["plan"].get("project_context_included"),
    "task_outcome_event_count": len(outcomes),
    "human_state_unchanged_after_snapshot": human_state_unchanged,
    "state_version_before": before["state_version"],
    "state_version_after_task": after_task["state_version"],
    "state_version_after_explicit_update": updated["state_version"],
    "phase_after_explicit_update": updated["current_phase"],
    "stale_update_conflict": conflict,
    "active_worker_count": core.orchestrator_info()["active_worker_count"],
    "active_job_count": core.orchestrator_info()["active_job_count"],
}
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=False, indent=2))
