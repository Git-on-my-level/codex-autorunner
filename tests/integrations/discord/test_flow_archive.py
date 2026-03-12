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
    captured: list[dict[str, Any]] = []
    mirrored_run_exists: list[bool] = []

    def _archive_flow_run_artifacts(repo_root: Path, **kwargs: Any) -> dict[str, Any]:
        captured.append({"repo_root": str(repo_root), **kwargs})
        return {
            "run_id": kwargs["run_id"],
            "archived_tickets": 0,
            "archived_runs": True,
            "archived_contextspace": False,
        }

    monkeypatch.setattr(
        discord_service_module,
        "archive_flow_run_artifacts",
        _archive_flow_run_artifacts,
    )

    class _Mirror:
        def mirror_inbound(self, **kwargs: Any) -> None:
            _ = kwargs

        def mirror_outbound(self, **kwargs: Any) -> None:
            _ = kwargs
            with FlowStore(workspace / ".codex-autorunner" / "flows.db") as store:
                store.initialize()
                mirrored_run_exists.append(store.get_flow_run(run_id) is not None)

    monkeypatch.setattr(service, "_flow_run_mirror", lambda _workspace_root: _Mirror())

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

    assert captured == [
        {
            "repo_root": str(workspace),
            "run_id": run_id,
            "force": False,
            "delete_run": False,
        }
    ]
    assert mirrored_run_exists == [True]
    with FlowStore(workspace / ".codex-autorunner" / "flows.db") as store:
        store.initialize()
        assert store.get_flow_run(run_id) is None
    assert "Archived run" in rest.interaction_responses[0]["payload"]["data"]["content"]


@pytest.mark.anyio
async def test_flow_archive_button_deletes_run_record_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    run_id = str(uuid.uuid4())

    rest = _FakeRest()
    service = _service(tmp_path, rest)
    captured: list[dict[str, Any]] = []

    def _archive_flow_run_artifacts(repo_root: Path, **kwargs: Any) -> dict[str, Any]:
        captured.append({"repo_root": str(repo_root), **kwargs})
        return {
            "run_id": kwargs["run_id"],
            "archived_tickets": 0,
            "archived_runs": True,
            "archived_contextspace": False,
        }

    monkeypatch.setattr(
        discord_service_module,
        "archive_flow_run_artifacts",
        _archive_flow_run_artifacts,
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

    assert captured == [
        {
            "repo_root": str(workspace),
            "run_id": run_id,
            "force": False,
            "delete_run": True,
        }
    ]
    assert "Archived run" in rest.interaction_responses[0]["payload"]["data"]["content"]
