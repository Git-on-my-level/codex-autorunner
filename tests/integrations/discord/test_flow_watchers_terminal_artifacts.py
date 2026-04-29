from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
from codex_autorunner.core.apps import compute_bundle_sha
from codex_autorunner.core.filebox import outbox_dir
from codex_autorunner.core.state_roots import resolve_repo_state_root
from codex_autorunner.integrations.discord.flow_watchers import (
    _scan_and_enqueue_terminal_notifications,
)


def _init_workspace(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    seed_hub_files(workspace, force=True)
    seed_repo_files(workspace, git_required=False)


def _write_installed_wrapup_app(workspace: Path) -> None:
    state_root = resolve_repo_state_root(workspace)
    app_root = state_root / "apps" / "local.wrapup"
    bundle_root = app_root / "bundle"
    bundle_root.mkdir(parents=True, exist_ok=True)
    manifest_text = """schema_version: 1
id: local.wrapup
name: Wrapup App
version: 1.0.0
hooks:
  before_chat_wrapup:
    - artifacts:
        - "artifacts/summary.md"
"""
    (bundle_root / "car-app.yaml").write_text(manifest_text, encoding="utf-8")
    bundle_sha = compute_bundle_sha(bundle_root)
    (app_root / "artifacts").mkdir(parents=True, exist_ok=True)
    (app_root / "artifacts" / "summary.md").write_text(
        "# summary\n",
        encoding="utf-8",
    )
    (app_root / "state").mkdir(parents=True, exist_ok=True)
    (app_root / "logs").mkdir(parents=True, exist_ok=True)
    (app_root / "app.lock.json").write_text(
        json.dumps(
            {
                "id": "local.wrapup",
                "version": "1.0.0",
                "source_repo_id": "local",
                "source_url": "https://example.invalid/apps.git",
                "source_path": "apps/wrapup",
                "source_ref": "main",
                "commit_sha": "deadbeef",
                "manifest_sha": "manifest-sha",
                "bundle_sha": bundle_sha,
                "trusted": True,
                "installed_at": "2026-04-28T00:00:00Z",
            },
            indent=2,
        ),
        encoding="utf-8",
    )


class _Mirror:
    def mirror_outbound(self, **_kwargs) -> None:
        return None


@pytest.mark.anyio
async def test_discord_terminal_notification_publishes_and_flushes_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    _init_workspace(workspace)
    _write_installed_wrapup_app(workspace)
    enqueued = []

    async def _enqueue(record):
        enqueued.append(record)

    flush_calls = []

    async def _flush_outbox_files(*, workspace_root: Path, channel_id: str) -> None:
        flush_calls.append((workspace_root, channel_id))

    service = SimpleNamespace(
        _logger=logging.getLogger("test"),
        _store=SimpleNamespace(
            list_bindings=AsyncMock(
                return_value=[
                    {
                        "channel_id": "channel-1",
                        "workspace_path": str(workspace),
                        "last_terminal_run_id": None,
                    }
                ]
            ),
            enqueue_outbox=AsyncMock(side_effect=_enqueue),
            mark_terminal_run_seen=AsyncMock(),
        ),
        _hub_raw_config_cache={},
        _flow_run_mirror=lambda _workspace: _Mirror(),
        _flush_outbox_files=AsyncMock(side_effect=_flush_outbox_files),
    )
    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.flow_watchers._load_latest_terminal_ticket_flow_run",
        lambda _service, _workspace: ("run-1", "completed", None),
    )

    notified = await _scan_and_enqueue_terminal_notifications(service)

    assert notified == 1
    assert len(enqueued) == 1
    assert enqueued[0].payload_json["content"] == (
        "Ticket flow completed successfully (run run-1)."
    )
    assert flush_calls == [(workspace.resolve(), "channel-1")]
    assert (outbox_dir(workspace) / "local.wrapup-summary.md").read_text(
        encoding="utf-8"
    ) == "# summary\n"


@pytest.mark.anyio
async def test_discord_terminal_notification_keeps_plain_text_when_no_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    _init_workspace(workspace)
    enqueued = []

    async def _enqueue(record):
        enqueued.append(record)

    service = SimpleNamespace(
        _logger=logging.getLogger("test"),
        _store=SimpleNamespace(
            list_bindings=AsyncMock(
                return_value=[
                    {
                        "channel_id": "channel-1",
                        "workspace_path": str(workspace),
                        "last_terminal_run_id": None,
                    }
                ]
            ),
            enqueue_outbox=AsyncMock(side_effect=_enqueue),
            mark_terminal_run_seen=AsyncMock(),
        ),
        _hub_raw_config_cache={},
        _flow_run_mirror=lambda _workspace: _Mirror(),
    )
    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.flow_watchers._load_latest_terminal_ticket_flow_run",
        lambda _service, _workspace: ("run-2", "completed", None),
    )

    notified = await _scan_and_enqueue_terminal_notifications(service)

    assert notified == 1
    assert enqueued[0].payload_json["content"] == (
        "Ticket flow completed successfully (run run-2)."
    )
