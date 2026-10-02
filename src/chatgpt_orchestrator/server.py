from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from .orchestrator import Orchestrator

mcp = MCPServer(
    name="ChatGPT-Orchestrator",
    version="0.4.0",
    description="Persistent multi-worker and review control plane for ChatGPT Web.",
)
_core: Orchestrator | None = None


def core() -> Orchestrator:
    global _core
    if _core is None:
        _core = Orchestrator()
    return _core


@mcp.tool()
def task_create(goal: str) -> dict:
    """Create one persistent top-level task."""
    return core().task_create(goal)


@mcp.tool()
def task_get(task_id: str) -> dict:
    """Read one task, its workers, and its jobs."""
    return core().task_get(task_id)


@mcp.tool()
def task_list() -> list[dict]:
    """List persistent tasks."""
    return core().task_list()


@mcp.tool()
def task_cancel(task_id: str) -> dict:
    """Cancel a task, its active jobs, and its active workers."""
    return core().task_cancel(task_id)


@mcp.tool()
def task_collect(task_id: str) -> dict:
    """Collect independent worker results for MAIN."""
    return core().task_collect(task_id)


@mcp.tool()
def task_review_submit(
    task_id: str,
    instructions: str | None = None,
    role: str = "reviewer",
    timeout_seconds: float | None = None,
) -> dict:
    """Create a reviewer worker and asynchronously synthesize completed worker outputs."""
    return core().task_review_submit(
        task_id,
        instructions=instructions,
        role=role,
        timeout_seconds=timeout_seconds,
    )


@mcp.tool()
def task_review_status(task_id: str) -> dict:
    """Poll the latest reviewer job for a task."""
    return core().task_review_status(task_id)


@mcp.tool()
def task_finalize(task_id: str) -> dict:
    """Return reviewed final output when available, otherwise the collected fallback."""
    return core().task_finalize(task_id)


@mcp.tool()
def chat_create(role: str, task_id: str | None = None) -> dict:
    """Create a bounded ChatGPT worker."""
    return core().chat_create(role, task_id)


@mcp.tool()
def chat_send(worker_id: str, prompt: str) -> dict:
    """Compatibility synchronous send. Prefer chat_submit for multi-worker work."""
    return core().chat_send(worker_id, prompt)


@mcp.tool()
def chat_submit(
    worker_id: str,
    prompt: str,
    timeout_seconds: float | None = None,
    max_retries: int | None = None,
) -> dict:
    """Submit a worker job asynchronously and return a persistent job id immediately."""
    return core().chat_submit(
        worker_id,
        prompt,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
    )


@mcp.tool()
def chat_job_status(job_id: str) -> dict:
    """Poll one asynchronous job."""
    return core().chat_job_status(job_id)


@mcp.tool()
def chat_job_list(
    task_id: str | None = None,
    worker_id: str | None = None,
    include_terminal: bool = True,
) -> list[dict]:
    """List asynchronous jobs by task or worker."""
    return core().chat_job_list(
        task_id=task_id,
        worker_id=worker_id,
        include_terminal=include_terminal,
    )


@mcp.tool()
def chat_cancel(job_id: str) -> dict:
    """Request cancellation of one asynchronous worker job."""
    return core().chat_cancel(job_id)


@mcp.tool()
def chat_retry(job_id: str, timeout_seconds: float | None = None) -> dict:
    """Retry a failed, timed-out, or cancelled job on the same worker."""
    return core().chat_retry(job_id, timeout_seconds=timeout_seconds)


@mcp.tool()
def chat_status(worker_id: str) -> dict:
    """Read worker and active-job status."""
    return core().chat_status(worker_id)


@mcp.tool()
def chat_read(worker_id: str) -> dict:
    """Read the latest completed worker result."""
    return core().chat_read(worker_id)


@mcp.tool()
def chat_list(include_closed: bool = False) -> list[dict]:
    """List workers."""
    return core().chat_list(include_closed)


@mcp.tool()
def chat_close(worker_id: str) -> dict:
    """Cancel active jobs and close one worker."""
    return core().chat_close(worker_id)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
