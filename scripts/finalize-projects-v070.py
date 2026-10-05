import json
from chatgpt_orchestrator.store import Store

db = r"D:/MCP-Test/ChatGPT-Orchestrator/data/orchestrator.db"
store = Store(db)

test_id = "P-ae2bc893f4"
main_id = "P-dbff1b7610"

try:
    test = store.get_project(test_id, recent_events=5)
    if test["status"] != "archived":
        test = store.update_project(
            test_id,
            expected_version=int(test["state_version"]),
            status="archived",
            next_actions=["Archived after v0.7.0 live validation."],
        )
except KeyError:
    test = None

main = store.get_project(main_id, recent_events=20)
projects = store.list_projects(limit=20)
print(json.dumps({
    "main_project": {
        "project_id": main["project_id"],
        "name": main["name"],
        "status": main["status"],
        "current_phase": main["current_phase"],
        "state_version": main["state_version"],
        "related_task_ids": main["related_task_ids"],
        "recent_event_types": [e["event_type"] for e in main["recent_events"]],
    },
    "archived_test_project": None if test is None else {
        "project_id": test["project_id"],
        "status": test["status"],
        "state_version": test["state_version"],
    },
    "project_list": [
        {
            "project_id": p["project_id"],
            "name": p["name"],
            "status": p["status"],
            "state_version": p["state_version"],
            "task_count": p["task_count"],
        }
        for p in projects
    ],
}, ensure_ascii=False, indent=2))
