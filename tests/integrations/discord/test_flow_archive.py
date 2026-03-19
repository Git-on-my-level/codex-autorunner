from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any

import pytest

from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
from codex_autorunner.core.flows import FlowStore
from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.integrations.discord import service as discord_service_module
from codex_autorunner.integrations.discord.config import (
    DiscordBotConfig,
    DiscordCommandRegistration,
)
from codex_autorunner.integrations.discord.service import DiscordBotService
from codex_autorunner.integrations.discord.state import DiscordStateStore


class _FakeRest:
    def __init__(self) -> None:
        self.interaction_responses: list[dict[str, Any]] = []
        self.followup_messages: list[dict[str, Any]] = []
        self.edited_original_interaction_responses: list[dict[str, Any]] = []

    async def create_interaction_response(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        payload: dict[str, Any],
    ) -> None:
        self.interaction_responses.append(
            {
                "interaction_id": interaction_id,
                "interaction_token": interaction_token,
                "payload": payload,
            }
        )

    async def create_followup_message(
        self,
        *,
        application_id: str,
        interaction_token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.followup_messages.append(
            {
                "application_id": application_id,
                "interaction_token": interaction_token,
                "payload": payload,
            }
        )
        return payload

    async def edit_original_interaction_response(
        self,
        *,
        application_id: str,
        interaction_token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.edited_original_interaction_responses.append(
            {
                "application_id": application_id,
                "interaction_token": interaction_token,
                "payload": payload,
            }
        )
        return payload

    async def create_channel_message(
        self, *, channel_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return {"id": "msg-1", "channel_id": channel_id, "payload": payload}

    async def bulk_overwrite_application_commands(
        self,
        *,
        application_id: str,
        commands: list[dict[str, Any]],
        guild_id: str | None = None,
    ) -> list[dict[str, Any]]:
        _ = (application_id, guild_id)
        return commands


class _FakeGateway:
    async def run(self, on_dispatch) -> None:
        _ = on_dispatch
        return None

    async def stop(self) -> None:
        return None


class _FakeOutboxManager:
    def start(self) -> None:
        return None

    async def run_loop(self) -> None:
        await asyncio.Event().wait()


class _FlowServiceStub:
    def __init__(self, summary: dict[str, Any]) -> None:
        self.summary = summary
        self.archive_calls: list[dict[str, Any]] = []

    def archive_flow_run(
        self, run_id: str, *, force: bool = False, delete_run: bool = True
    ) -> dict[str, Any]:
        self.archive_calls.append(
            {"run_id": run_id, "force": force, "delete_run": delete_run}
        )
        return dict(self.summary)


class _FlowServiceKeyErrorStub:
    def archive_flow_run(
        self, run_id: str, *, force: bool = False, delete_run: bool = True
    ) -> dict[str, Any]:
        _ = (force, delete_run)
        raise KeyError(run_id)


class _FlowServiceValueErrorStub:
    def archive_flow_run(
        self, run_id: str, *, force: bool = False, delete_run: bool = True
    ) -> dict[str, Any]:
        _ = (run_id, force, delete_run)
        raise ValueError("Can only archive completed/stopped/failed flows")


def _config(root: Path) -> DiscordBotConfig:
    return DiscordBotConfig(
        root=root,
        enabled=True,
        bot_token_env="TOKEN_ENV",
        app_id_env="APP_ENV",
        bot_token="token",
        application_id="app-1",
        allowed_guild_ids=frozenset({"guild-1"}),
        allowed_channel_ids=frozenset({"channel-1"}),
        allowed_user_ids=frozenset({"user-1"}),
        command_registration=DiscordCommandRegistration(
            enabled=True,
            scope="guild",
            guild_ids=("guild-1",),
        ),
        state_file=root / ".codex-autorunner" / "discord_state.sqlite3",
        intents=1,
        max_message_length=2000,
        message_overflow="split",
        pma_enabled=True,
    )


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / ".git").mkdir()
    seed_hub_files(workspace, force=True)
    seed_repo_files(workspace, git_required=False)
    return workspace


def _create_run(workspace: Path, run_id: str, status: FlowRunStatus) -> None:
    with FlowStore(workspace / ".codex-autorunner" / "flows.db") as store:
        store.initialize()
        store.create_flow_run(run_id, "ticket_flow", input_data={}, state={})
        store.update_flow_run_status(run_id, status)


def _service(tmp_path: Path, rest: _FakeRest) -> DiscordBotService:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    return DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=_FakeGateway(),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )


@pytest.mark.anyio
async def test_flow_archive_command_deletes_run_record_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    run_id = str(uuid.uuid4())
    _create_run(workspace, run_id, FlowRunStatus.COMPLETED)

    rest = _FakeRest()
    service = _service(tmp_path, rest)
    flow_service = _FlowServiceStub(
        {
            "run_id": run_id,
            "archived_tickets": 0,
            "archived_runs": True,
            "archived_contextspace": False,
        }
    )
    monkeypatch.setattr(
        discord_service_module,
        "build_ticket_flow_orchestration_service",
        lambda *, workspace_root: flow_service,
    )

    try:
        await service._handle_flow_archive(
            "interaction-1",
            "token-1",
            workspace_root=workspace,
            options={"run_id": run_id},
            channel_id="channel-1",
            guild_id="guild-1",
        )
    finally:
        await service._store.close()

    assert flow_service.archive_calls == [
        {"run_id": run_id, "force": False, "delete_run": True}
    ]
    with FlowStore(workspace / ".codex-autorunner" / "flows.db") as store:
        store.initialize()
        assert store.get_flow_run(run_id) is not None
    assert "Archived run" in rest.interaction_responses[0]["payload"]["data"]["content"]


@pytest.mark.anyio
async def test_flow_archive_button_deletes_run_record_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    run_id = str(uuid.uuid4())

    rest = _FakeRest()
    service = _service(tmp_path, rest)
    flow_service = _FlowServiceStub(
        {
            "run_id": run_id,
            "archived_tickets": 0,
            "archived_runs": True,
            "archived_contextspace": False,
        }
    )
    monkeypatch.setattr(
        discord_service_module,
        "build_ticket_flow_orchestration_service",
        lambda *, workspace_root: flow_service,
    )

    try:
        await service._handle_flow_button(
            "interaction-2",
            "token-2",
            workspace_root=workspace,
            custom_id=f"flow:{run_id}:archive",
            channel_id="channel-1",
            guild_id="guild-1",
        )
    finally:
        await service._store.close()

    assert flow_service.archive_calls == [
        {"run_id": run_id, "force": False, "delete_run": True}
    ]
    assert rest.interaction_responses[0]["payload"]["type"] == 6
    edited = rest.edited_original_interaction_responses[0]["payload"]
    assert "Archived run" in edited["content"]
    assert edited["components"] == []


@pytest.mark.anyio
async def test_car_archive_uses_shared_fresh_start_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)

    rest = _FakeRest()
    service = _service(tmp_path, rest)

    async def _fake_require_bound_workspace(*_args: Any, **_kwargs: Any) -> Path:
        return workspace

    service._require_bound_workspace = _fake_require_bound_workspace  # type: ignore[assignment]

    monkeypatch.setattr(
        "codex_autorunner.core.archive.resolve_workspace_archive_target",
        lambda workspace_root, **_kwargs: type(
            "_Target",
            (),
            {
                "base_repo_root": workspace_root,
                "base_repo_id": "repo-1",
                "workspace_repo_id": "repo-1",
                "worktree_of": "repo-1",
                "source_path": "workspace",
            },
        )(),
    )
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "codex_autorunner.core.archive.archive_workspace_for_fresh_start",
        lambda **kwargs: calls.append(kwargs)
        or type(
            "_Result",
            (),
            {
                "snapshot_id": None,
                "archived_paths": (),
                "archived_thread_ids": ("thread-1", "thread-2"),
            },
        )(),
    )

    try:
        await service._handle_car_archive(
            "interaction-archive-1",
            "token-archive-1",
            channel_id="channel-1",
        )
    finally:
        await service._store.close()

    assert calls
    assert calls[0]["hub_root"] == tmp_path
    assert calls[0]["worktree_repo_root"] == workspace
    assert rest.interaction_responses[0]["payload"]["type"] == 5
    assert len(rest.followup_messages) == 1
    content = rest.followup_messages[0]["payload"]["content"]
    assert "workspace car state was already clean" in content.lower()
    assert "archived 2 managed threads" in content.lower()


@pytest.mark.anyio
async def test_flow_archive_button_retires_stale_card_on_missing_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    run_id = str(uuid.uuid4())

    rest = _FakeRest()
    service = _service(tmp_path, rest)
    monkeypatch.setattr(
        discord_service_module,
        "build_ticket_flow_orchestration_service",
        lambda *, workspace_root: _FlowServiceKeyErrorStub(),
    )

    try:
        await service._handle_flow_button(
            "interaction-stale",
            "token-stale",
            workspace_root=workspace,
            custom_id=f"flow:{run_id}:archive",
            channel_id="channel-1",
            guild_id="guild-1",
        )
    finally:
        await service._store.close()

    assert rest.interaction_responses[0]["payload"]["type"] == 6
    edited = rest.edited_original_interaction_responses[0]["payload"]
    assert run_id in edited["content"]
    assert "no longer exists" in edited["content"]
    assert edited["components"] == []


@pytest.mark.anyio
async def test_flow_archive_button_keeps_original_card_on_validation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    run_id = str(uuid.uuid4())

    rest = _FakeRest()
    service = _service(tmp_path, rest)
    monkeypatch.setattr(
        discord_service_module,
        "build_ticket_flow_orchestration_service",
        lambda *, workspace_root: _FlowServiceValueErrorStub(),
    )

    try:
        await service._handle_flow_button(
            "interaction-invalid",
            "token-invalid",
            workspace_root=workspace,
            custom_id=f"flow:{run_id}:archive",
            channel_id="channel-1",
            guild_id="guild-1",
        )
    finally:
        await service._store.close()

    assert rest.interaction_responses[0]["payload"]["type"] == 6
    assert rest.edited_original_interaction_responses == []
    assert len(rest.followup_messages) == 1
    assert (
        rest.followup_messages[0]["payload"]["content"]
        == "Can only archive completed/stopped/failed flows"
    )


@pytest.mark.anyio
async def test_flow_archive_command_cleans_live_contextspace(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    run_id = str(uuid.uuid4())
    _create_run(workspace, run_id, FlowRunStatus.COMPLETED)

    tickets_dir = workspace / ".codex-autorunner" / "tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)
    (tickets_dir / "TICKET-001.md").write_text("ticket", encoding="utf-8")

    context_dir = workspace / ".codex-autorunner" / "contextspace"
    context_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / "active_context.md").write_text("Active context\n", encoding="utf-8")
    (context_dir / "decisions.md").write_text("Decision log\n", encoding="utf-8")

    run_dir = workspace / ".codex-autorunner" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "DISPATCH.md").write_text("dispatch", encoding="utf-8")
    live_flow_dir = workspace / ".codex-autorunner" / "flows" / run_id / "chat"
    live_flow_dir.mkdir(parents=True, exist_ok=True)
    (live_flow_dir / "outbound.jsonl").write_text("{}", encoding="utf-8")

    rest = _FakeRest()
    service = _service(tmp_path, rest)

    try:
        await service._handle_flow_archive(
            "interaction-3",
            "token-3",
            workspace_root=workspace,
            options={"run_id": run_id},
            channel_id="channel-1",
            guild_id="guild-1",
        )
    finally:
        await service._store.close()

    assert (
        workspace
        / ".codex-autorunner"
        / "archive"
        / "runs"
        / run_id
        / "contextspace"
        / "active_context.md"
    ).read_text(encoding="utf-8") == "Active context\n"
    assert (
        workspace
        / ".codex-autorunner"
        / "archive"
        / "runs"
        / run_id
        / "flow_state"
        / "chat"
        / "outbound.jsonl"
    ).read_text(encoding="utf-8") == "{}"
    assert (
        workspace / ".codex-autorunner" / "contextspace" / "active_context.md"
    ).read_text(encoding="utf-8") == ""
    assert (
        workspace / ".codex-autorunner" / "contextspace" / "decisions.md"
    ).read_text(encoding="utf-8") == ""
    assert not (workspace / ".codex-autorunner" / "tickets" / "TICKET-001.md").exists()
    assert not (workspace / ".codex-autorunner" / "flows" / run_id).exists()
    assert not (workspace / ".codex-autorunner" / "runs" / run_id).exists()
