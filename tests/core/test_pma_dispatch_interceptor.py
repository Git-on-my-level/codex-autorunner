from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
from codex_autorunner.core.config import CONFIG_FILENAME, DEFAULT_HUB_CONFIG
from codex_autorunner.core.flows import FlowStore
from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.core.lifecycle_events import LifecycleEvent, LifecycleEventType
from codex_autorunner.core.pma_dispatch_interceptor import PmaDispatchInterceptor
from codex_autorunner.integrations.discord.state import DiscordStateStore
from tests.conftest import write_test_config


def _enable_discord(hub_root: Path) -> None:
    cfg = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    cfg.setdefault("discord_bot", {})
    cfg["discord_bot"]["enabled"] = True
    write_test_config(hub_root / CONFIG_FILENAME, cfg)


def _workspace(tmp_path: Path) -> Path:
    seed_hub_files(tmp_path, force=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / ".git").mkdir()
    seed_repo_files(workspace, git_required=False)
    return workspace


def _create_dispatch(workspace: Path, run_id: str, body: str) -> None:
    history_dir = (
        workspace / ".codex-autorunner" / "runs" / run_id / "dispatch_history" / "0001"
    )
    history_dir.mkdir(parents=True, exist_ok=True)
    (history_dir / "DISPATCH.md").write_text(
        f"---\nmode: pause\ntitle: Need guidance\n---\n\n{body}\n",
        encoding="utf-8",
    )


def _set_run_status(workspace: Path, run_id: str, status: FlowRunStatus) -> None:
    db_path = workspace / ".codex-autorunner" / "flows.db"
    with FlowStore(db_path) as store:
        if store.get_flow_run(run_id) is None:
            store.create_flow_run(run_id, "ticket_flow", input_data={}, state={})
        store.update_flow_run_status(run_id, status)


@pytest.mark.anyio
async def test_interceptor_notifies_bound_discord_chat_on_auto_resolve(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    _enable_discord(tmp_path)
    run_id = str(uuid.uuid4())
    _set_run_status(workspace, run_id, FlowRunStatus.PAUSED)
    _create_dispatch(workspace, run_id, "continue")

    store = DiscordStateStore(tmp_path / ".codex-autorunner" / "discord_state.sqlite3")
    await store.initialize()
    try:
        await store.upsert_binding(
            channel_id="discord-bound",
            guild_id="guild-1",
            workspace_path=str(workspace),
            repo_id="repo-1",
        )

        interceptor = PmaDispatchInterceptor(hub_root=tmp_path, supervisor=None)
        result = await interceptor.process_dispatch_event(
            LifecycleEvent(
                event_type=LifecycleEventType.DISPATCH_CREATED,
                repo_id="repo-1",
                run_id=run_id,
            ),
            workspace,
        )

        assert result is not None
        assert result.action == "auto_resolved"
        assert result.reply == "Continuing with the current task."
        assert result.notified is True

        outbox = await store.list_outbox()
        assert any(
            record.channel_id == "discord-bound"
            and "PMA handled the paused ticket-flow dispatch automatically."
            in str(record.payload_json.get("content", ""))
            and "No user response is needed."
            in str(record.payload_json.get("content", ""))
            for record in outbox
        )

        reply_history = (
            workspace
            / ".codex-autorunner"
            / "runs"
            / run_id
            / "reply_history"
            / "0001"
            / "USER_REPLY.md"
        )
        assert reply_history.exists()
    finally:
        await store.close()


@pytest.mark.anyio
async def test_interceptor_auto_resolve_notice_is_idempotent_for_same_event(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    _enable_discord(tmp_path)
    run_id = str(uuid.uuid4())
    _set_run_status(workspace, run_id, FlowRunStatus.PAUSED)
    _create_dispatch(workspace, run_id, "continue")

    store = DiscordStateStore(tmp_path / ".codex-autorunner" / "discord_state.sqlite3")
    await store.initialize()
    try:
        await store.upsert_binding(
            channel_id="discord-bound",
            guild_id="guild-1",
            workspace_path=str(workspace),
            repo_id="repo-1",
        )
        event = LifecycleEvent(
            event_type=LifecycleEventType.DISPATCH_CREATED,
            repo_id="repo-1",
            run_id=run_id,
            event_id="evt-stable-1",
        )

        interceptor = PmaDispatchInterceptor(hub_root=tmp_path, supervisor=None)
        first = await interceptor.process_dispatch_event(event, workspace)
        second = await interceptor.process_dispatch_event(event, workspace)

        assert first is not None and first.action == "auto_resolved"
        assert second is not None and second.action == "auto_resolved"
        outbox = await store.list_outbox()
        assert len(outbox) == 1
    finally:
        await store.close()


@pytest.mark.anyio
async def test_interceptor_notifies_primary_pma_discord_chat_on_escalation(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    _enable_discord(tmp_path)
    run_id = str(uuid.uuid4())
    _set_run_status(workspace, run_id, FlowRunStatus.COMPLETED)
    _create_dispatch(workspace, run_id, "continue")

    store = DiscordStateStore(tmp_path / ".codex-autorunner" / "discord_state.sqlite3")
    await store.initialize()
    try:
        await store.upsert_binding(
            channel_id="discord-pma",
            guild_id="guild-1",
            workspace_path=str(workspace),
            repo_id="repo-1",
        )
        await store.update_pma_state(
            channel_id="discord-pma",
            pma_enabled=True,
            pma_prev_workspace_path=str(workspace),
            pma_prev_repo_id="repo-1",
        )

        interceptor = PmaDispatchInterceptor(hub_root=tmp_path, supervisor=None)
        result = await interceptor.process_dispatch_event(
            LifecycleEvent(
                event_type=LifecycleEventType.DISPATCH_CREATED,
                repo_id="repo-1",
                run_id=run_id,
            ),
            workspace,
        )

        assert result is not None
        assert result.action == "escalate"
        assert result.reason == "Run cannot be auto-resumed"
        assert result.notified is True

        outbox = await store.list_outbox()
        assert any(
            record.channel_id == "discord-pma"
            and "PMA escalated a paused ticket-flow dispatch for user attention."
            in str(record.payload_json.get("content", ""))
            and "the flow should stop and ask the user instead of guessing"
            in str(record.payload_json.get("content", ""))
            for record in outbox
        )
    finally:
        await store.close()
