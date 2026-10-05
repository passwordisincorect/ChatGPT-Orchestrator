# ChatGPT-Orchestrator 0.8.0

ChatGPT-Orchestrator is a local Windows control plane that lets a MAIN ChatGPT delegate work to multiple ChatGPT Web workers, aggregate independent outputs, run a reviewer, and automatically request targeted rework when the reviewer finds a material gap.

## v0.8.0 persistent project state

- New persistent project subsystem stored in the existing SQLite database with additive tables only; existing v0.6.3 task/job/worker rows remain unchanged.
- New MCP tools: `project_create`, `project_list`, `project_get`, `project_update`, and `project_attach_task`.
- Project state tracks identity, summary, goals, recorded decisions, current phase, status, next actions, related task IDs, timestamps, and recent events.
- `task_create`, `task_plan`, and `task_auto_start` accept an optional `project_id`. Existing calls without it behave as before.
- Worker, reviewer, and rework prompts receive bounded **read-only** project context when a project is explicitly selected.
- Automatic task snapshots append machine-generated `task_outcome` events and update `last_activity_at`; they do **not** modify human-authored summary/goals/decisions/phase/status/next-actions and do not increment `state_version`.
- Human project updates use optimistic concurrency with `expected_version`; stale writes fail with `PROJECT_VERSION_CONFLICT` instead of silently overwriting newer state.
- A task may be attached to multiple projects. Attachment and outcome snapshots are idempotent.
- Terminal tasks attached after completion are snapshotted immediately.
- Startup reconciliation repairs a narrow crash window where a terminal task was persisted but its project outcome event was not.
- Project-state snapshot failures are best-effort and never change the underlying task result.
- `scripts/soak-v070.ps1` repeatedly exercises project persistence, routing metadata, autonomy, async jobs, and store compatibility; the latest summary is written to `data/soak-v070-last.json`.
- v0.6.3 browser transport, retry safety, reviewer/rework behavior, recovery, UIA serialization, and CDP fail-closed handling remain intact.

### Project-state workflow

```text
project_create(...)
      |
      +-- durable SQLite state
      |
task_auto_start(..., project_id=P-...)
      |
      +-- worker prompts receive read-only project context
      |
task_auto_advance(...)
      |
      +-- normal worker/reviewer/rework lifecycle
      |
terminal task
      |
      +-- append task_outcome event
      +-- update last_activity_at only
      +-- never rewrite human-authored project state
```

Use `project_update(project_id, expected_version, ...)` for explicit state changes. Project names are labels, not unique identifiers; use the returned `project_id` for task linking.

## v0.6.3 stability and soak

- Hybrid runtime status now exposes stability counters for sessions, backend selection, send success/failure/cancellation, CDP readiness failures, ambiguous submissions, and all-backend failures.
- The status includes a compact `stability` block with `clean_idle`, completed-send count, and runtime send-success rate.
- Metrics are updated under the same adapter lock used by session state and are included in the persisted hybrid runtime-status file.
- Repeated fallback-cycle tests verify create/send/close cleanup over 25 cycles with no leaked hybrid sessions.
- `scripts/soak-v063.ps1` repeatedly runs the hybrid, UIA shared-tabs, async-job, and autonomy suites and writes the latest result to `data/soak-v063-last.json`.
- v0.6.2 safety behavior remains unchanged: ambiguous CDP submissions are non-retryable and UIA fallback send/response cycles remain single-lane.
- Reviewer verdict parsing tolerates accessibility line wrapping such as `ORCH_\nREVIEW:\nPASS` and still marks the verdict as explicit.
- Live soak is expected to finish with zero active workers/jobs; CDP challenge detection remains fail-closed and is never bypassed.

## v0.6.2 recovery and failure handling

- Ambiguous CDP send failures are now raised as non-retryable submission errors. Once a state-changing CDP send has started, Orchestrator will not spend a retry budget or replay the same prompt automatically.
- `chat_retry()` rejects jobs marked non-retryable, preventing manual retry through the normal retry API when delivery may already have occurred.
- Restart recovery is now state-aware: jobs that were still `QUEUED` at restart are marked safe to recover; jobs that were already `RUNNING` are marked non-retryable because their submission state is ambiguous.
- `task_recover()` skips ambiguous/non-retryable jobs even when broad recovery is requested.
- CDP target disappearance before submission is recoverable through the existing UIA fallback path; target disappearance or transport failure after CDP send begins is fail-closed and not replayed.
- Failure tests now cover CDP timeout after send start, target loss before send, UIA fallback creation failure, three concurrent worker sessions, retry-budget suppression, and safe queued-vs-running restart recovery.
- Existing normal transient failures remain retryable when a retry budget is explicitly configured.
- UIA shared-tabs fallback is intentionally single-lane for send/response cycles. Multiple worker tabs may exist, but only one UIA prompt is actively submitted/polled at a time so tab selection cannot corrupt another worker's response polling.
- No challenge bypass was added; browser verification remains a normal user/browser step.

## v0.6.1 hardening

- CDP status now distinguishes endpoint reachability, no ChatGPT page, signed-out state, browser challenge, and a ready authenticated composer.
- Idle hybrid status now reports the backend that would actually be selected instead of `selected_backend: none`.
- CDP worker sessions carry explicit ownership markers; stale or mismatched target ownership is rejected instead of silently using another target.
- Healthy orchestrator-owned CDP targets are returned to a reusable pool after a worker closes, reducing unnecessary `Target.createTarget` calls and preserving fresh-chat isolation by navigating back to ChatGPT home before reuse.
- Stale owned targets from a previous process are closed; only explicitly marked healthy free targets may be reused after restart.
- Hybrid backend transitions are structured with session, source backend, destination backend, reason, and timestamp; duplicate fallback events were removed.
- Terminal task advancement now joins finishing background job threads before final cleanup, eliminating an intermittent Windows SQLite temp-file lock seen during tests.
- Challenge handling remains fail-closed; v0.6.1 detects and falls back but does not automate browser-verification challenges.

## v0.6 highlights

- New default backend: `edge_hybrid`.
- CDP is attempted first for background browser execution.
- CDP workers are accepted only after a readiness preflight confirms a usable, authenticated ChatGPT composer with no detected browser-verification challenge.
- If CDP is unavailable, signed out, challenged, or not ready **before prompt submission**, the worker falls back automatically to the proven `edge_tabs` UIA-only transport.
- State-changing prompts are never silently replayed on UIA after a CDP send has started, preventing duplicate actions when delivery is uncertain.
- Hybrid status reports `cdp_ready`, `selected_backend`, `degraded`, structured failure codes, session counts, and recent transport transitions.
- `hybrid_cdp_enabled` can disable CDP and preserve UIA-only behavior.
- Challenge detection is fail-closed; the project does not automate challenge completion.
- v0.5 autonomous planning, reviewer/rework, restart recovery, shared-tabs, and tab-pool behavior remain compatible.

## v0.5 highlights

- Autonomous task planning with `task_plan()`.
- Automatic complementary role selection for coding, architecture, research, analysis and general tasks.
- One-call worker launch with `task_auto_start()`.
- Automatic state progression with `task_auto_advance()`:
  - wait for workers;
  - submit reviewer;
  - parse reviewer verdict;
  - request targeted rework;
  - re-review;
  - finalize.
- Machine-readable reviewer protocol:
  - `ORCH_REVIEW: PASS`
  - `ORCH_REVIEW: REWORK role1, role2`
- Reviewer worker reuse during re-review, avoiding an extra browser tab.
- Restart recovery with `task_recover()`.
- Orphaned worker records are closed on restart so stale browser state does not consume worker capacity.
- Stable job ordering even when multiple jobs are created in the same second.
- `orchestrator_info()` exposes version, backend, capacity and capabilities directly through MCP.
- **UIA-only shared-tabs transport**: no OS mouse, keyboard, or clipboard input for worker tab creation, selection, navigation, prompt submission, cancellation, or lifecycle.
- Worker tab slots are pooled and reused instead of repeatedly creating/closing tabs.
- Experimental Edge CDP backend remains available for investigation, but is not the recommended backend because ChatGPT/Cloudflare may challenge newly created CDP tabs.
- v0.4 task/job/reviewer APIs remain available for compatibility.

## Architecture

```text
                         ChatGPT MAIN
                              |
                    ChatGPT-Orchestrator
                              |
                         Task Planner
                              |
                  +-----------+-----------+
                  |                       |
             Worker A                 Worker B
           (auto role)               (auto role)
                  \                       /
                   +----- task_collect ---+
                              |
                           Reviewer
                              |
                     PASS? / REWORK?
                       /          \
                    yes            rework roles
                     |                  |
                  Final          selected workers
                                        |
                                     Reviewer
                                        |
                                      Final
```

Workers cannot create child workers. MAIN/Orchestrator remains the only component allowed to create workers.

## MCP tools

System:

```text
orchestrator_info
```

Persistent project tools:

```text
project_create
project_list
project_get
project_update
project_attach_task
```

Autonomous task tools:

```text
task_plan
task_auto_start
task_auto_advance
task_recover
```

Manual task/review tools:

```text
task_create
task_get
task_list
task_cancel
task_collect
task_review_submit
task_review_status
task_finalize
```

Worker/job tools:

```text
chat_create
chat_send
chat_submit
chat_job_status
chat_job_list
chat_cancel
chat_retry
chat_status
chat_read
chat_list
chat_close
```

## Recommended v0.7 workflow

For most delegated tasks MAIN can keep the existing workflow. When durable project context is needed, pass `project_id` explicitly:

```text
project = project_get(project_id)   # optional across new chats
task_auto_start(goal, project_id=project_id)
        |
        +-- auto-select worker roles
        +-- create workers
        +-- submit jobs in parallel
        |
task_auto_advance(task_id)
        |
        +-- if workers still active: waiting
        +-- otherwise: submit reviewer
        |
task_auto_advance(task_id)
        |
        +-- PASS -> final result
        |
        +-- REWORK -> targeted worker jobs
                           |
                    task_auto_advance()
                           |
                       re-review
                           |
                    task_auto_advance()
                           |
                         final
```

The manual v0.4-style workflow remains supported when MAIN needs exact worker control.

## Browser backends

### `edge_hybrid` — current default

v0.6 combines the two existing browser transports behind one worker adapter:

```text
worker
  |
  +-- CDP readiness preflight
  |      |
  |      +-- ready -> CDP background transport
  |      |
  |      +-- unavailable / signed out / challenged / not ready
  |                 -> UIA-only shared-tabs fallback
  |
  +-- no cross-transport replay after a CDP send begins
```

The hybrid backend keeps one stable transport binding per worker session. If a CDP session loses readiness before a prompt is submitted, it can migrate once to a fresh UIA worker slot. If a CDP send has already begun, failures are returned as non-retryable ambiguous-submission errors so neither the hybrid adapter nor the Orchestrator retry loop can replay the prompt automatically.

Runtime configuration:

```json
{
  "backend": "edge_hybrid",
  "hybrid_cdp_enabled": true
}
```

Set `hybrid_cdp_enabled` to `false` to force the stable UIA-only path.

### `edge_tabs` — stable fallback

One dedicated Edge window contains one ChatGPT tab per worker.

Advantages:

- live-tested end-to-end with two real ChatGPT workers;
- no OS mouse/keyboard/clipboard input;
- tab creation uses UIA InvokePattern;
- tab selection uses SelectionItemPattern;
- navigation and prompt entry use ValuePattern;
- Send/Stop use InvokePattern;
- closed worker slots are pooled and reused without opening more tabs;
- uses the normal signed-in Edge session and remains compatible with the v0.4 workflow.

Current behavior:

- multiple workers share one dedicated Edge window;
- UIA send/response cycles are serialized (`max_parallel_sends = 1`) because one Edge window exposes one active UIA tab surface at a time;
- this favors reliability during CDP fallback; true parallel background execution remains the CDP path;
- the physical mouse and keyboard are not used.

### `edge_cdp` — experimental v0.5 backend

CDP controls a dedicated Edge profile through Chrome DevTools Protocol.

It does **not** use:

- OS mouse automation;
- OS keyboard automation;
- UIA foreground tab switching;
- Playwright.

The backend sends text with CDP `Input.insertText`, submits through the page/CDP, reads ChatGPT responses from the DOM, cancels through the page, and closes tabs with `Target.closeTarget`.

For isolation, it intentionally uses a dedicated profile:

```text
data/edge-cdp-profile
```

The dedicated profile requires a one-time ChatGPT sign-in before it can be used for workers.

Open it with:

```powershell
.\scripts\open-cdp-profile.ps1
```

After signing in, check:

```powershell
.\scripts\cdp-status.ps1
```

`cdp-status.ps1` is useful for diagnostics, but `edge_cdp` is intentionally **not recommended as the default in v0.5**. Live testing found that ChatGPT/Cloudflare can place newly created CDP tabs on a `Just a moment...` challenge even when the dedicated profile is authenticated. The project does not attempt to bypass that challenge.

Keep:

```json
"backend": "edge_tabs"
```

for normal use. The CDP implementation is retained as an experimental backend for future work once new-tab behavior is reliable.

## Setup

```powershell
cd D:\MCP-Test\ChatGPT-Orchestrator
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

v0.5 adds:

```text
websocket-client >= 1.8, < 2
```

for direct CDP WebSocket communication.

## Tests

```powershell
.\scripts\test.ps1
```

The suite covers:

- asynchronous jobs;
- cancellation, timeout, retry, and non-retryable ambiguous submissions;
- persistence;
- reviewer/finalization;
- autonomous planning;
- automatic review;
- reviewer rework loops;
- reviewer worker reuse;
- queued-job restart recovery and running-job replay protection;
- CDP session parsing and signed-out protection;
- serialized UIA fallback under concurrent worker calls;
- repeated hybrid fallback soak cycles;
- runtime stability metrics and cleanup accounting;
- Edge response parsing.

Run the repeated local stability soak with:

```powershell
.\scripts\soak-v063.ps1 -Iterations 5
```

The latest machine-readable soak summary is written to:

```text
data/soak-v063-last.json
```

## Current safety/behavior limits

- Windows + Microsoft Edge only.
- Default maximum is three active workers.
- Autonomous planning normally reserves one slot for the reviewer, so with the default capacity it launches two independent workers.
- A worker accepts one active job at a time.
- Automatic retry remains disabled by default because prompts can have side effects. Even when a retry budget is configured, ambiguous submissions are explicitly non-retryable.
- Physical tab slots are reused, but each reused slot is navigated back to a fresh ChatGPT home/new-chat state before it is assigned to another worker, preventing conversation-context reuse.
- `edge_cdp` remains experimental because newly created CDP tabs may trigger a browser-verification challenge. v0.6 detects this condition and the `edge_hybrid` backend falls back before submission; it does not automate challenge completion.
- On restart, queued jobs are marked safe to recover. Jobs that were already running are marked non-retryable because delivery may have occurred; uncertain old browser sessions are not trusted or replayed automatically.
