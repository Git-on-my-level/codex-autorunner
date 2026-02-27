from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from codex_autorunner.bootstrap import seed_repo_files
from codex_autorunner.core.flows import FlowStore
from codex_autorunner.core.flows.models import FlowRunRecord, FlowRunStatus
from codex_autorunner.integrations.discord.config import (
    DiscordBotConfig,
    DiscordCommandRegistration,
)
from codex_autorunner.integrations.discord.service import DiscordBotService
from codex_autorunner.integrations.discord.state import DiscordStateStore


class _FakeRest:
    def __init__(self) -> None:
        self.interaction_responses: list[dict[str, Any]] = []
        self.command_sync_calls: list[dict[str, Any]] = []

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
        self.command_sync_calls.append(
            {
                "application_id": application_id,
                "guild_id": guild_id,
                "commands": commands,
            }
        )
        return commands


class _FakeGateway:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    async def run(self, on_dispatch) -> None:
        for payload in self._events:
            await on_dispatch("INTERACTION_CREATE", payload)

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
    seed_repo_files(workspace, git_required=False)
    return workspace


def _flow_interaction(name: str, options: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "token": "token-1",
        "channel_id": "channel-1",
        "guild_id": "guild-1",
        "member": {"user": {"id": "user-1"}},
        "data": {
            "name": "car",
            "options": [
                {
                    "type": 2,
                    "name": "flow",
                    "options": [{"type": 1, "name": name, "options": options}],
                }
            ],
        },
    }


def _create_run(workspace: Path, run_id: str, *, status: FlowRunStatus) -> None:
    db_path = workspace / ".codex-autorunner" / "flows.db"
    with FlowStore(db_path) as store:
        store.create_flow_run(
            run_id,
            "ticket_flow",
            input_data={},
            state={"ticket_engine": {"current_ticket": "TICKET-001.md"}},
        )
        store.update_flow_run_status(run_id, status)


def _write_manifest(root: Path, *, repo_id: str, repo_path: str) -> Path:
    manifest_path = root / ".codex-autorunner" / "manifest.yml"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        "\n".join(
            [
                "version: 2",
                "repos:",
                f"  - id: {repo_id}",
                f"    path: {repo_path}",
                "    enabled: true",
                "    auto_run: false",
                "    kind: base",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest_path


@pytest.mark.anyio
async def test_flow_status_and_runs_render_expected_output(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    paused_run_id = str(uuid.uuid4())
    completed_run_id = str(uuid.uuid4())
    _create_run(workspace, completed_run_id, status=FlowRunStatus.COMPLETED)
    _create_run(workspace, paused_run_id, status=FlowRunStatus.PAUSED)

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )

    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            _flow_interaction(name="status", options=[]),
            _flow_interaction(
                name="runs", options=[{"type": 4, "name": "limit", "value": 2}]
            ),
        ]
    )
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    try:
        await service.run_forever()
        assert len(rest.interaction_responses) == 2
        status_payload = rest.interaction_responses[0]["payload"]["data"]["content"]
        runs_payload = rest.interaction_responses[1]["payload"]["data"]["content"]

        assert f"Run: {paused_run_id}" in status_payload
        assert "Status: paused" in status_payload
        assert "Worker:" in status_payload
        assert "Current ticket:" in status_payload

        assert "Recent ticket_flow runs (limit=2)" in runs_payload
        assert paused_run_id in runs_payload
        assert completed_run_id in runs_payload
    finally:
        await store.close()


@pytest.mark.anyio
async def test_flow_issue_seeds_issue_md(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )

    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.service.seed_issue_from_github",
        lambda *_args, **_kwargs: SimpleNamespace(
            content="# Issue 123\n\nDetails",
            issue_number=123,
            repo_slug="org/repo",
        ),
    )

    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            _flow_interaction(
                name="issue",
                options=[{"type": 3, "name": "issue_ref", "value": "123"}],
            ),
        ]
    )
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    try:
        await service.run_forever()
        issue_path = workspace / ".codex-autorunner" / "ISSUE.md"
        assert issue_path.exists()
        assert "Issue 123" in issue_path.read_text(encoding="utf-8")
        content = rest.interaction_responses[0]["payload"]["data"]["content"]
        assert "Seeded ISSUE.md from GitHub issue 123." in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_flow_plan_seeds_issue_md(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )

    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            _flow_interaction(
                name="plan",
                options=[{"type": 3, "name": "text", "value": "Ship MVP in 3 steps"}],
            ),
        ]
    )
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    try:
        await service.run_forever()
        issue_path = workspace / ".codex-autorunner" / "ISSUE.md"
        assert issue_path.exists()
        assert "Ship MVP in 3 steps" in issue_path.read_text(encoding="utf-8")
        content = rest.interaction_responses[0]["payload"]["data"]["content"]
        assert "Seeded ISSUE.md from your plan." in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_flow_recover_reconciles_active_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    running_run_id = str(uuid.uuid4())
    _create_run(workspace, running_run_id, status=FlowRunStatus.RUNNING)

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )

    def _fake_reconcile(_repo_root, record, _store):
        return record, True, False

    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.service.reconcile_flow_run",
        _fake_reconcile,
    )

    rest = _FakeRest()
    gateway = _FakeGateway([_flow_interaction(name="recover", options=[])])
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    try:
        await service.run_forever()
        content = rest.interaction_responses[0]["payload"]["data"]["content"]
        assert f"Recovered for run {running_run_id}" in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_flow_reply_writes_user_reply_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    paused_run_id = str(uuid.uuid4())
    _create_run(workspace, paused_run_id, status=FlowRunStatus.PAUSED)

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )

    class _FakeController:
        async def resume_flow(self, run_id: str) -> FlowRunRecord:
            return FlowRunRecord(
                id=run_id,
                flow_type="ticket_flow",
                status=FlowRunStatus.RUNNING,
                input_data={},
                state={},
                created_at="2026-01-01T00:00:00Z",
            )

    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.service.build_ticket_flow_controller",
        lambda _workspace_root: _FakeController(),
    )
    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.service.ensure_worker",
        lambda _workspace_root, _run_id, is_terminal=False: {
            "status": "spawned",
            "stdout": None,
            "stderr": None,
            "is_terminal": is_terminal,
        },
    )

    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            _flow_interaction(
                name="reply",
                options=[{"type": 3, "name": "text", "value": "Please continue"}],
            ),
        ]
    )
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    try:
        await service.run_forever()
        reply_path = (
            workspace / ".codex-autorunner" / "runs" / paused_run_id / "USER_REPLY.md"
        )
        assert reply_path.exists()
        assert reply_path.read_text(encoding="utf-8").strip() == "Please continue"
        assert len(rest.interaction_responses) == 1
        content = rest.interaction_responses[0]["payload"]["data"]["content"]
        assert paused_run_id in content
        assert "resumed run" in content.lower()
    finally:
        await store.close()


@pytest.mark.anyio
async def test_flow_status_in_pma_mode_without_manifest_reports_missing_manifest(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    await store.update_pma_state(
        channel_id="channel-1",
        pma_enabled=True,
        pma_prev_workspace_path=str(workspace),
        pma_prev_repo_id=None,
    )

    rest = _FakeRest()
    gateway = _FakeGateway([_flow_interaction(name="status", options=[])])
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )

    try:
        await service.run_forever()
        assert len(rest.interaction_responses) == 1
        content = rest.interaction_responses[0]["payload"]["data"]["content"]
        assert "Hub manifest not configured." in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_flow_status_in_pma_mode_shows_hub_overview(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    paused_run_id = str(uuid.uuid4())
    _create_run(workspace, paused_run_id, status=FlowRunStatus.PAUSED)
    manifest_path = _write_manifest(
        tmp_path, repo_id="workspace", repo_path="workspace"
    )

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    await store.update_pma_state(
        channel_id="channel-1",
        pma_enabled=True,
        pma_prev_workspace_path=str(workspace),
        pma_prev_repo_id=None,
    )

    rest = _FakeRest()
    gateway = _FakeGateway([_flow_interaction(name="status", options=[])])
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
        manifest_path=manifest_path,
    )

    try:
        await service.run_forever()
        assert len(rest.interaction_responses) == 1
        content = rest.interaction_responses[0]["payload"]["data"]["content"]
        assert "Hub Flow Overview:" in content
        assert "workspace:" in content
        assert paused_run_id in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_flow_runs_in_pma_mode_shows_hub_overview(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    paused_run_id = str(uuid.uuid4())
    _create_run(workspace, paused_run_id, status=FlowRunStatus.PAUSED)
    manifest_path = _write_manifest(
        tmp_path, repo_id="workspace", repo_path="workspace"
    )

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    await store.update_pma_state(
        channel_id="channel-1",
        pma_enabled=True,
        pma_prev_workspace_path=str(workspace),
        pma_prev_repo_id=None,
    )

    rest = _FakeRest()
    gateway = _FakeGateway([_flow_interaction(name="runs", options=[])])
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
        manifest_path=manifest_path,
    )

    try:
        await service.run_forever()
        assert len(rest.interaction_responses) == 1
        content = rest.interaction_responses[0]["payload"]["data"]["content"]
        assert "Hub Flow Overview:" in content
        assert paused_run_id in content
        assert "Recent ticket_flow runs" not in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_flow_status_with_run_id_in_pma_mode_still_shows_hub_overview(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    paused_run_id = str(uuid.uuid4())
    _create_run(workspace, paused_run_id, status=FlowRunStatus.PAUSED)
    manifest_path = _write_manifest(
        tmp_path, repo_id="workspace", repo_path="workspace"
    )

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    await store.update_pma_state(
        channel_id="channel-1",
        pma_enabled=True,
        pma_prev_workspace_path=str(workspace),
        pma_prev_repo_id=None,
    )

    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            _flow_interaction(
                name="status",
                options=[{"type": 3, "name": "run_id", "value": paused_run_id}],
            )
        ]
    )
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
        manifest_path=manifest_path,
    )

    try:
        await service.run_forever()
        assert len(rest.interaction_responses) == 1
        content = rest.interaction_responses[0]["payload"]["data"]["content"]
        assert "Hub Flow Overview:" in content
        assert "Run: " not in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_flow_runs_with_large_limit_in_pma_mode_still_shows_hub_overview(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    paused_run_id = str(uuid.uuid4())
    _create_run(workspace, paused_run_id, status=FlowRunStatus.PAUSED)
    manifest_path = _write_manifest(
        tmp_path, repo_id="workspace", repo_path="workspace"
    )

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )
    await store.update_pma_state(
        channel_id="channel-1",
        pma_enabled=True,
        pma_prev_workspace_path=str(workspace),
        pma_prev_repo_id=None,
    )

    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            _flow_interaction(
                name="runs",
                options=[{"type": 4, "name": "limit", "value": 999}],
            )
        ]
    )
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
        manifest_path=manifest_path,
    )

    try:
        await service.run_forever()
        assert len(rest.interaction_responses) == 1
        content = rest.interaction_responses[0]["payload"]["data"]["content"]
        assert "Hub Flow Overview:" in content
        assert "Recent ticket_flow runs" not in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_flow_recover_uses_explicit_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    target_run_id = str(uuid.uuid4())
    other_run_id = str(uuid.uuid4())
    _create_run(workspace, other_run_id, status=FlowRunStatus.RUNNING)
    _create_run(workspace, target_run_id, status=FlowRunStatus.RUNNING)

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )

    seen: list[str] = []

    def _fake_reconcile(_repo_root, record, _store):
        seen.append(record.id)
        return record, True, False

    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.service.reconcile_flow_run",
        _fake_reconcile,
    )

    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            _flow_interaction(
                name="recover",
                options=[{"type": 3, "name": "run_id", "value": target_run_id}],
            )
        ]
    )
    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=rest,
        gateway_client=gateway,
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    try:
        await service.run_forever()
        assert seen == [target_run_id]
        content = rest.interaction_responses[0]["payload"]["data"]["content"]
        assert f"Recovered for run {target_run_id}" in content
    finally:
        await store.close()
