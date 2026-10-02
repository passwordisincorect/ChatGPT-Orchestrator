# ChatGPT-Orchestrator 0.4.0

ChatGPT-Orchestrator is a local Windows control plane that lets a MAIN ChatGPT delegate work to multiple ChatGPT Web workers, aggregate their outputs, and optionally ask a separate reviewer to produce a final consolidated answer.

## v0.4 highlights

- Persistent SQLite tasks, workers and asynchronous jobs.
- Maximum 3 active workers by default.
- MAIN owns delegation; workers cannot create child workers.
- **Shared-tabs Edge backend**:
  - one dedicated Edge window for Orchestrator;
  - one ChatGPT tab per worker;
  - workers share the same top-level Edge HWND;
  - tabs are selected only when Orchestrator needs to submit/poll/cancel;
  - closing a worker closes only its tab and reindexes remaining worker slots;
  - the user's normal Edge window/tabs are not modified.
- Multi-worker asynchronous submit/poll from v0.3.
- Per-job timeout, retry and cancellation.
- Aggregation via `task_collect()`.
- Independent reviewer via `task_review_submit()`.
- Reviewed final output via `task_finalize()`.
- Reviewer prompt includes the task goal, independent worker outputs, worker errors and optional user instructions.
- Reviewer is instructed to compare disagreements rather than vote by majority.

## Architecture

```text
                    ChatGPT MAIN
                         |
                ChatGPT-Orchestrator
                         |
              one dedicated Edge window
                         |
          +--------------+--------------+
          |              |              |
        Tab 1          Tab 2          Tab 3
      Architect        Critic        Reviewer
          |              |              |
        Job A          Job B        Review Job
          \              /              |
           +-- task_collect() ----------+
                         |
                  task_finalize()
                         |
                    ChatGPT MAIN
```

## Why one dedicated Edge window?

Using one Orchestrator-owned window keeps all worker tabs together like a tab group while avoiding accidental changes to the user's personal Edge tabs. The legacy `edge` backend remains available if one-window-per-worker behavior is ever needed.

Current default:

```json
"backend": "edge_tabs"
```

## MCP tools

Task/review tools:

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

## Recommended workflow

```text
task_create()
   |
   +-- chat_create("architect") -> W1 / tab 1
   +-- chat_create("critic")    -> W2 / tab 2
   |
   +-- chat_submit(W1, ...)
   +-- chat_submit(W2, ...)
   |
   +-- poll chat_job_status()
   |
   +-- task_collect()
   |
   +-- task_review_submit() -> Reviewer / tab 3
   |
   +-- task_review_status()
   |
   +-- task_finalize()
   |
   +-- MAIN returns final answer
```

## Setup

```powershell
cd D:\MCP-Test\ChatGPT-Orchestrator
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

## Regression tests

```powershell
.\scripts\test.ps1
```

## Live v0.4 test

```powershell
.\.venv\Scripts\python.exe .\scripts\live-test-v04.py
Get-Content .\data\live-v04-result.json
```

The live test verifies:
- two independent worker jobs;
- a third reviewer;
- all three sessions use the same Edge HWND;
- slots are 1, 2 and 3;
- exact worker results are collected;
- reviewer result is finalized;
- all worker tabs close cleanly.

## Current limits

- Windows + Microsoft Edge only.
- Foreground tab switching is required for UIA reads/writes, so the dedicated worker window may visibly switch tabs while working.
- A worker accepts one active job at a time.
- Default maximum is three active workers.
- Reviewer needs an available worker slot. With the default limit of 3, a common pattern is 2 workers + 1 reviewer.
- Shared-tab session slot mappings live in the running Orchestrator process. After an Orchestrator restart, incomplete jobs are marked ERROR rather than attempting to reuse uncertain browser tab state.
- Automatic retry is disabled by default because repeating a prompt can have side effects.
