$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

@'
from __future__ import annotations

import json

from chatgpt_orchestrator.config import load_settings
from chatgpt_orchestrator.edge_cdp_adapter import EdgeCDPAdapter

settings = load_settings()
adapter = EdgeCDPAdapter(
    settings.edge_executable,
    settings.cdp_profile_dir,
    settings.chat_url,
    settings.create_timeout_seconds,
    settings.send_timeout_seconds,
    settings.stable_seconds,
)

session_id = None
try:
    session_id = adapter.create("cdp-status")
    data = adapter.inspect(session_id)
    data["ready"] = bool(data.get("composer_found") and data.get("authenticated"))
    print(json.dumps(data, ensure_ascii=False, indent=2))
finally:
    if session_id is not None:
        adapter.close(session_id)
'@ | & ".\.venv\Scripts\python.exe" -
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
