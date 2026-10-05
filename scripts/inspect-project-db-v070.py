import json, sqlite3
db = r"D:/MCP-Test/ChatGPT-Orchestrator/data/orchestrator.db"
project_id = "P-dbff1b7610"
task_id = "T-c68af81880"
con = sqlite3.connect(db)
con.row_factory = sqlite3.Row
project = dict(con.execute(
    "SELECT id,name,summary,current_phase,status,state_version,updated_at,last_activity_at FROM projects WHERE id=?",
    (project_id,)
).fetchone())
task = dict(con.execute(
    "SELECT id,goal,status,created_at,updated_at FROM tasks WHERE id=?",
    (task_id,)
).fetchone())
events = []
for row in con.execute(
    """SELECT event_type,task_id,source,payload_json,created_at
       FROM project_events
       WHERE project_id=?
       ORDER BY rowid DESC
       LIMIT 20""",
    (project_id,)
):
    item = dict(row)
    try:
        item["payload"] = json.loads(item.pop("payload_json"))
    except Exception:
        pass
    events.append(item)
outcomes = [e for e in events if e["event_type"]=="task_outcome" and e["task_id"]==task_id]
print(json.dumps({
    "project": project,
    "task": task,
    "connector_task_outcomes": outcomes,
    "connector_task_outcome_count": len(outcomes),
}, ensure_ascii=False, indent=2))
con.close()
