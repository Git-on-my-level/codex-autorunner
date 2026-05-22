from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_autorunner.adapters.telegram.state import TelegramStateStore
from codex_autorunner.core.automation import EXECUTOR_PMA_OPERATOR_TURN, AutomationStore
from codex_autorunner.core.config import (
    CONFIG_FILENAME,
    DEFAULT_HUB_CONFIG,
    load_hub_config,
)
from codex_autorunner.core.hub import HubSupervisor
from codex_autorunner.manifest import load_manifest, save_manifest


def _write_hub_config(hub_root: Path) -> None:
    hub_root.mkdir(parents=True, exist_ok=True)
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg.setdefault("pma", {})
    cfg["pma"]["reactive_debounce_seconds"] = 0
    config_path = hub_root / CONFIG_FILENAME
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(cfg, indent=2) + "\n",
        encoding="utf-8",
    )


def _register_repo(hub_root: Path, repo_root: Path, repo_id: str) -> None:
    manifest_path = hub_root / ".codex-autorunner" / "manifest.yml"
    manifest = load_manifest(manifest_path, hub_root)
    manifest.ensure_repo(hub_root, repo_root, repo_id=repo_id, display_name=repo_id)
    save_manifest(manifest_path, manifest, hub_root)


@pytest.mark.anyio
async def test_reactive_flow_failed_creates_paused_automation_job(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    _write_hub_config(hub_root)
    supervisor = HubSupervisor(load_hub_config(hub_root), start_lifecycle_worker=False)

    try:
        supervisor.lifecycle_emitter.emit_flow_failed(
            "repo-1", "run-1", origin="runner"
        )
        supervisor.process_lifecycle_events()

        jobs = AutomationStore(hub_root).list_jobs()
        assert len(jobs) == 1
        assert jobs[0].state == "paused"
        assert jobs[0].executor.get("kind") == EXECUTOR_PMA_OPERATOR_TURN
        assert "Lifecycle event received" in str(jobs[0].executor.get("message_text"))

        telegram_store = TelegramStateStore(hub_root / "telegram_state.sqlite3")
        outbox = await telegram_store.list_outbox()
        await telegram_store.close()
        assert outbox == []
    finally:
        supervisor.shutdown()


@pytest.mark.anyio
async def test_reactive_dispatch_created_does_not_enqueue_telegram_outbox(
    tmp_path: Path,
) -> None:
    hub_root = tmp_path / "hub"
    _write_hub_config(hub_root)
    supervisor = HubSupervisor(load_hub_config(hub_root), start_lifecycle_worker=False)

    try:
        repo_root = hub_root / "repo-1"
        repo_root.mkdir(parents=True, exist_ok=True)
        _register_repo(hub_root, repo_root, "repo-1")
        supervisor.lifecycle_emitter.emit_dispatch_created(
            "repo-1", "run-1", origin="runner"
        )
        supervisor.process_lifecycle_events()

        jobs = AutomationStore(hub_root).list_jobs()
        assert len(jobs) == 1
        assert jobs[0].state == "paused"

        telegram_store = TelegramStateStore(hub_root / "telegram_state.sqlite3")
        outbox = await telegram_store.list_outbox()
        await telegram_store.close()
        assert outbox == []
    finally:
        supervisor.shutdown()
