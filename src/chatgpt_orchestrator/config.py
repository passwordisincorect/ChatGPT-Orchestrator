from __future__ import annotations

import json
from pathlib import Path

from .models import Settings

ROOT = Path(__file__).resolve().parents[2]


def load_settings(path: Path | None = None) -> Settings:
    config_path = path or ROOT / "config" / "config.json"
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    limit = int(raw.get("max_active_workers", 3))
    if limit < 1 or limit > 10:
        raise ValueError("max_active_workers must be between 1 and 10")

    db = Path(str(raw.get("database_path", "data/orchestrator.db")))
    if not db.is_absolute():
        db = ROOT / db

    cdp_profile = Path(str(raw.get("cdp_profile_dir", "data/edge-cdp-profile")))
    if not cdp_profile.is_absolute():
        cdp_profile = ROOT / cdp_profile

    retries = int(raw.get("default_max_retries", 0))
    if retries < 0 or retries > 5:
        raise ValueError("default_max_retries must be between 0 and 5")

    timeout = float(raw.get("default_job_timeout_seconds", 120.0))
    if timeout <= 0:
        raise ValueError("default_job_timeout_seconds must be > 0")

    dom_tab_pool_size = int(raw.get("dom_tab_pool_size", 5))
    if dom_tab_pool_size < 1 or dom_tab_pool_size > 20:
        raise ValueError("dom_tab_pool_size must be between 1 and 20")

    dom_idle_shutdown_seconds = float(raw.get("dom_idle_shutdown_seconds", 0.0))
    if dom_idle_shutdown_seconds < 0 or dom_idle_shutdown_seconds > 3600:
        raise ValueError("dom_idle_shutdown_seconds must be between 0 and 3600")

    return Settings(
        backend=str(raw.get("backend", "simulated")),
        max_active_workers=limit,
        database_path=str(db),
        edge_executable=str(
            raw.get(
                "edge_executable",
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            )
        ),
        cdp_profile_dir=str(cdp_profile),
        chat_url=str(raw.get("chat_url", "https://chatgpt.com/")),
        create_timeout_seconds=float(raw.get("create_timeout_seconds", 20.0)),
        send_timeout_seconds=float(raw.get("send_timeout_seconds", 120.0)),
        stable_seconds=float(raw.get("stable_seconds", 3.0)),
        default_job_timeout_seconds=timeout,
        default_max_retries=retries,
        hybrid_cdp_enabled=bool(raw.get("hybrid_cdp_enabled", True)),
        dom_broker_endpoint=str(
            raw.get(
                "dom_broker_endpoint",
                "http://127.0.0.1:8765/api/dom",
            )
        ),
        dom_broker_token_file=str(
            raw.get(
                "dom_broker_token_file",
                r"D:\MCP-Test\.chatgpt-dom-broker-token",
            )
        ),
        dom_uia_fallback_enabled=bool(raw.get("dom_uia_fallback_enabled", True)),
        dom_tab_pool_only=bool(raw.get("dom_tab_pool_only", False)),
        dom_tab_pool_size=dom_tab_pool_size,
        dom_idle_shutdown_seconds=dom_idle_shutdown_seconds,
    )
