from __future__ import annotations

import json
import sys

from .config import load_settings
from .orchestrator import Orchestrator


def main() -> None:
    settings = load_settings()
    core = Orchestrator(settings=settings)
    print(json.dumps({
        "version": "0.4.0",
        "python": sys.version.split()[0],
        "backend": settings.backend,
        "max_active_workers": settings.max_active_workers,
        "database_path": settings.database_path,
        "task_count": len(core.task_list()),
        "active_worker_count": len(core.chat_list()),
        "active_job_count": len(core.chat_job_list(include_terminal=False)),
        "status": "ok",
    }, indent=2))


if __name__ == "__main__":
    main()
