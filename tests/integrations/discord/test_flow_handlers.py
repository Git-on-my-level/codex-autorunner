from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
from codex_autorunner.core.flows import FlowStore
from codex_autorunner.core.flows import hub_overview as hub_overview_module
from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.integrations.discord.config import (
    DiscordBotConfig,
    DiscordCommandRegistration,
)
from codex_autorunner.integrations.discord.service import DiscordBotService
from codex_autorunner.integrations.discord.state import DiscordStateStore


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class _FakeRest:
    def __init__(self) -> None:
        self.interaction_responses: list[dict[str, Any]] = []
        self.channel_messages: list[dict[str, Any]] = []
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
        self.channel_messages.append({"channel_id": channel_id, "payload": payload})
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


class _FlowServiceStub:
    def __init__(self) -> None:
        self.start_calls: list[dict[str, Any]] = []
        self.stop_calls: list[str] = []
        self.resume_calls: list[str] = []
        self.ensure_calls: list[tuple[str, bool]] = []
        self.reconcile_calls: list[str] = []
        self.wait_result: SimpleNamespace | None = None

    async def start_flow_run(
        self,
        _flow_target_id: str,
        *,
        input_data: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> SimpleNamespace:
        self.start_calls.append(
            {
                "input_data": input_data or {},
                "metadata": metadata or {},
                "run_id": run_id,
            }
        )
        return SimpleNamespace(run_id=run_id or "run-new", status="running")

    async def stop_flow_run(self, run_id: str) -> SimpleNamespace:
        self.stop_calls.append(run_id)
        return SimpleNamespace(run_id=run_id, status="stopped")

    async def resume_flow_run(self, run_id: str, *, force: bool = False):
        _ = force
        self.resume_calls.append(run_id)
        return SimpleNamespace(run_id=run_id, status="running")

    def ensure_flow_run_worker(self, run_id: str, *, is_terminal: bool = False) -> None:
        self.ensure_calls.append((run_id, is_terminal))

    async def wait_for_flow_run_terminal(
        self, _run_id: str, **_kwargs: Any
    ) -> SimpleNamespace | None:
        return self.wait_result

    def reconcile_flow_run(self, run_id: str):
        self.reconcile_calls.append(run_id)
        return SimpleNamespace(run_id=run_id, status="running"), True, False


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


def _flow_interaction(
    name: str,
    options: list[dict[str, Any]],
    *,
    root: str = "car",
) -> dict[str, Any]:
    if root == "flow":
        data = {
            "name": "flow",
            "options": [{"type": 1, "name": name, "options": options}],
        }
    else:
        data = {
            "name": "car",
            "options": [
                {
                    "type": 2,
                    "name": "flow",
                    "options": [{"type": 1, "name": name, "options": options}],
                }
            ],
        }
    return {
        "id": str(uuid.uuid4()),
        "token": "token-1",
        "channel_id": "channel-1",
        "guild_id": "guild-1",
        "member": {"user": {"id": "user-1"}},
        "data": data,
    }


def _flow_component_interaction(custom_id: str) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "token": "token-1",
        "channel_id": "channel-1",
        "guild_id": "guild-1",
        "type": 3,
        "member": {"user": {"id": "user-1"}},
        "data": {
            "component_type": 2,
            "custom_id": custom_id,
        },
    }


def _latest_status_message(rest: _FakeRest) -> dict[str, Any]:
    payload = rest.interaction_responses[-1]["payload"]
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _create_run(
    workspace: Path,
    run_id: str,
    *,
    status: FlowRunStatus,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> None:
    db_path = workspace / ".codex-autorunner" / "flows.db"
    with FlowStore(db_path) as store:
        store.create_flow_run(
            run_id,
            "ticket_flow",
            input_data={},
            state={"ticket_engine": {"current_ticket": "TICKET-001.md"}},
        )
        store.update_flow_run_status(
            run_id,
            status,
            started_at=started_at,
            finished_at=finished_at,
        )


def _write_ticket(workspace: Path, name: str, *, done: bool) -> None:
    ticket_dir = workspace / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / name
    ticket_path.write_text(
        "---\n"
        "agent: codex\n"
        f"done: {'true' if done else 'false'}\n"
        "title: Ticket\n"
        "---\n\n"
        "Body\n",
        encoding="utf-8",
    )


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
            _flow_interaction(
                name="status",
                options=[{"type": 3, "name": "run_id", "value": paused_run_id}],
            ),
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
        assert len(rest.interaction_responses) == 4
        assert rest.interaction_responses[0]["payload"]["type"] == 5
        assert rest.interaction_responses[2]["payload"]["type"] == 5
        status_payload = rest.interaction_responses[1]["payload"]["data"]["content"]
        runs_payload = rest.interaction_responses[3]["payload"]["data"]["content"]

        assert f"Run: {paused_run_id}" in status_payload
        assert "Status: paused" in status_payload
        assert "Freshness:" in status_payload
        assert "Worker:" in status_payload
        assert "Current ticket:" in status_payload

        assert "Recent ticket_flow runs (limit=2)" in runs_payload
        assert paused_run_id in runs_payload
        assert completed_run_id in runs_payload

        mirror_path = (
            workspace
            / ".codex-autorunner"
            / "flows"
            / paused_run_id
            / "chat"
            / "outbound.jsonl"
        )
        mirror_records = _read_jsonl(mirror_path)
        assert mirror_records
        status_mirrors = [
            rec
            for rec in mirror_records
            if rec.get("event_type") == "flow_status_notice"
        ]
        assert status_mirrors
        latest = status_mirrors[-1]
        assert latest["kind"] == "notice"
        assert latest["actor"] == "car"
        assert "Run:" in latest["text"]
    finally:
        await store.close()


@pytest.mark.anyio
async def test_flow_status_without_run_id_uses_authoritative_run_and_includes_picker(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    completed_run_id = str(uuid.uuid4())
    paused_run_id = str(uuid.uuid4())
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
        assert len(rest.interaction_responses) == 2
        assert rest.interaction_responses[0]["payload"]["type"] == 5
        payload = rest.interaction_responses[1]["payload"]["data"]
        content = payload["content"]
        components = payload["components"]

        assert f"Run: {paused_run_id}" in content
        assert "Status: paused" in content
        picker_rows = [
            row
            for row in components
            if row["components"][0].get("custom_id") == "flow_runs_select"
        ]
        assert len(picker_rows) == 1
        picker_options = picker_rows[0]["components"][0]["options"]
        assert [option["value"] for option in picker_options] == [
            paused_run_id,
            completed_run_id,
        ]
        assert picker_options[0]["default"] is True
        assert picker_options[1]["default"] is False
    finally:
        await store.close()


@pytest.mark.anyio
async def test_flow_status_without_run_id_shows_no_current_run_for_history_only(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    newest_completed_run_id = str(uuid.uuid4())
    older_stopped_run_id = str(uuid.uuid4())
    _create_run(workspace, older_stopped_run_id, status=FlowRunStatus.STOPPED)
    _create_run(workspace, newest_completed_run_id, status=FlowRunStatus.COMPLETED)

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
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
        assert len(rest.interaction_responses) == 2
        assert rest.interaction_responses[0]["payload"]["type"] == 5
        payload = rest.interaction_responses[1]["payload"]["data"]
        content = payload["content"]
        components = payload["components"]

        assert f"Run: {newest_completed_run_id}" in content
        assert "Status: completed" in content
        assert "Archive: ready" in content
        picker_rows = [
            row
            for row in components
            if row["components"][0].get("custom_id") == "flow_runs_select"
        ]
        assert len(picker_rows) == 1
        picker_options = picker_rows[0]["components"][0]["options"]
        assert [option["value"] for option in picker_options] == [
            newest_completed_run_id,
            older_stopped_run_id,
        ]
        assert picker_options[0]["default"] is True
        assert picker_options[1]["default"] is False
    finally:
        await store.close()


@pytest.mark.anyio
async def test_flow_status_without_run_uses_ticket_summary_fallback(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    _write_ticket(workspace, "TICKET-001-a.md", done=True)
    _write_ticket(workspace, "TICKET-002-b.md", done=True)

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
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
        assert len(rest.interaction_responses) == 2
        assert rest.interaction_responses[0]["payload"]["type"] == 5
        payload = rest.interaction_responses[1]["payload"]["data"]
        content = payload["content"]
        assert content == "Status: Done\nTickets: 2/2"
        assert payload.get("components") == []
    finally:
        await store.close()


@pytest.mark.anyio
async def test_flow_root_alias_routes_to_authoritative_status(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    completed_run_id = str(uuid.uuid4())
    _create_run(workspace, completed_run_id, status=FlowRunStatus.COMPLETED)

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )

    rest = _FakeRest()
    gateway = _FakeGateway([_flow_interaction(name="status", options=[], root="flow")])
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
        assert rest.interaction_responses[0]["payload"]["type"] == 5
        content = rest.interaction_responses[1]["payload"]["data"]["content"]
        assert f"Run: {completed_run_id}" in content
        assert "Status: completed" in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_flow_status_shows_elapsed_for_completed_run(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    completed_run_id = str(uuid.uuid4())
    _create_run(
        workspace,
        completed_run_id,
        status=FlowRunStatus.COMPLETED,
        started_at="2026-03-13T08:00:00Z",
        finished_at="2026-03-13T10:30:00Z",
    )

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
                name="status",
                options=[{"type": 3, "name": "run_id", "value": completed_run_id}],
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
        assert rest.interaction_responses[0]["payload"]["type"] == 5
        content = rest.interaction_responses[1]["payload"]["data"]["content"]
        assert "Status: completed" in content
        assert "Elapsed: 2h 30m" in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_flow_status_with_missing_explicit_run_id_reports_not_found(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    existing_run_id = str(uuid.uuid4())
    missing_run_id = str(uuid.uuid4())
    _create_run(workspace, existing_run_id, status=FlowRunStatus.COMPLETED)

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
                name="status",
                options=[{"type": 3, "name": "run_id", "value": missing_run_id}],
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
        assert len(rest.interaction_responses) == 2
        assert rest.interaction_responses[0]["payload"]["type"] == 5
        content = rest.interaction_responses[1]["payload"]["data"]["content"]
        assert content == f"Ticket_flow run {missing_run_id} not found."
    finally:
        await store.close()


@pytest.mark.anyio
async def test_flow_refresh_button_updates_existing_status_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    run_id = str(uuid.uuid4())
    _create_run(workspace, run_id, status=FlowRunStatus.RUNNING)

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )

    reconcile_calls: list[str] = []

    def _fake_reconcile(_repo_root, record, _store, logger=None):
        reconcile_calls.append(record.id)
        if len(reconcile_calls) == 1:
            return record, False, False
        return (
            record.model_copy(update={"status": FlowRunStatus.COMPLETED}),
            True,
            False,
        )

    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.service.reconcile_flow_run",
        _fake_reconcile,
    )

    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            _flow_interaction(
                name="status",
                options=[{"type": 3, "name": "run_id", "value": run_id}],
            ),
            _flow_component_interaction(f"flow:{run_id}:refresh"),
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
        assert len(rest.interaction_responses) == 3
        initial_defer = rest.interaction_responses[0]["payload"]
        initial_payload = rest.interaction_responses[1]["payload"]
        refresh_payload = rest.interaction_responses[2]["payload"]

        assert initial_defer["type"] == 5
        assert initial_payload["type"] == 4
        assert "flags" not in initial_payload["data"]
        assert "Status: running" in initial_payload["data"]["content"]

        assert refresh_payload["type"] == 7
        assert "Status: completed" in refresh_payload["data"]["content"]
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
        assert rest.interaction_responses[0]["payload"]["type"] == 5
        content = rest.interaction_responses[1]["payload"]["data"]["content"]
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
async def test_flow_start_reuses_active_or_paused_run(
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

    flow_service = _FlowServiceStub()
    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.service.build_ticket_flow_orchestration_service",
        lambda *, workspace_root: flow_service,
    )

    rest = _FakeRest()
    gateway = _FakeGateway([_flow_interaction(name="start", options=[])])
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
        assert flow_service.ensure_calls == [(paused_run_id, False)]
        assert rest.interaction_responses[0]["payload"]["type"] == 5
        payload = rest.interaction_responses[1]["payload"]["data"]
        content = payload["content"]
        assert f"Reusing ticket_flow run {paused_run_id} (paused)." in content
        assert f"Run: {paused_run_id}" in content
        assert "Status: paused" in content
        assert "components" in payload
    finally:
        await store.close()


@pytest.mark.anyio
async def test_flow_restart_starts_new_run_for_failed_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    failed_run_id = str(uuid.uuid4())
    new_run_id = str(uuid.uuid4())
    _create_run(workspace, failed_run_id, status=FlowRunStatus.FAILED)

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )

    flow_service = _FlowServiceStub()

    async def _start_flow_run(
        _flow_target_id: str,
        *,
        input_data: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> SimpleNamespace:
        flow_service.start_calls.append(
            {
                "input_data": input_data or {},
                "metadata": metadata or {},
                "run_id": run_id,
            }
        )
        _create_run(workspace, new_run_id, status=FlowRunStatus.RUNNING)
        return SimpleNamespace(run_id=new_run_id, status="running")

    flow_service.start_flow_run = _start_flow_run  # type: ignore[method-assign]
    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.service.build_ticket_flow_orchestration_service",
        lambda *, workspace_root: flow_service,
    )

    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            _flow_interaction(
                name="restart",
                options=[{"type": 3, "name": "run_id", "value": failed_run_id}],
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
        assert flow_service.stop_calls == []
        assert len(flow_service.start_calls) == 1
        assert flow_service.start_calls[0]["metadata"]["force_new"] is True
        assert flow_service.start_calls[0]["metadata"]["origin"] == "discord"
        assert rest.interaction_responses[0]["payload"]["type"] == 5
        payload = rest.interaction_responses[1]["payload"]["data"]
        content = payload["content"]
        assert f"Started new ticket_flow run {new_run_id}." in content
        assert f"Run: {new_run_id}" in content
        assert "Status:" in content
        assert "components" in payload
    finally:
        await store.close()


@pytest.mark.anyio
async def test_flow_restart_aborts_when_active_run_does_not_terminate(
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

    flow_service = _FlowServiceStub()
    flow_service.wait_result = SimpleNamespace(run_id=running_run_id, status="stopping")
    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.service.build_ticket_flow_orchestration_service",
        lambda *, workspace_root: flow_service,
    )

    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            _flow_interaction(
                name="restart",
                options=[{"type": 3, "name": "run_id", "value": running_run_id}],
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
        assert flow_service.stop_calls == [running_run_id]
        assert flow_service.start_calls == []
        assert rest.interaction_responses[0]["payload"]["type"] == 5
        content = rest.interaction_responses[1]["payload"]["data"]["content"]
        assert "restart aborted to avoid concurrent workers" in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_flow_restart_button_uses_component_safe_response_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    failed_run_id = str(uuid.uuid4())
    new_run_id = str(uuid.uuid4())
    _create_run(workspace, failed_run_id, status=FlowRunStatus.FAILED)

    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()
    await store.upsert_binding(
        channel_id="channel-1",
        guild_id="guild-1",
        workspace_path=str(workspace),
        repo_id=None,
    )

    flow_service = _FlowServiceStub()

    async def _start_flow_run(
        _flow_target_id: str,
        *,
        input_data: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> SimpleNamespace:
        flow_service.start_calls.append(
            {
                "input_data": input_data or {},
                "metadata": metadata or {},
                "run_id": run_id,
            }
        )
        _create_run(workspace, new_run_id, status=FlowRunStatus.RUNNING)
        return SimpleNamespace(run_id=new_run_id, status="running")

    flow_service.start_flow_run = _start_flow_run  # type: ignore[method-assign]
    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.service.build_ticket_flow_orchestration_service",
        lambda *, workspace_root: flow_service,
    )

    rest = _FakeRest()
    gateway = _FakeGateway(
        [_flow_component_interaction(f"flow:{failed_run_id}:restart")]
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
        assert len(flow_service.start_calls) == 1
        assert len(rest.interaction_responses) == 1
        payload = rest.interaction_responses[0]["payload"]
        assert payload["type"] == 4
        assert (
            f"Started new ticket_flow run {new_run_id}." in payload["data"]["content"]
        )
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

    flow_service = _FlowServiceStub()
    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.service.build_ticket_flow_orchestration_service",
        lambda *, workspace_root: flow_service,
    )

    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            _flow_interaction(
                name="recover",
                options=[{"type": 3, "name": "run_id", "value": running_run_id}],
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
        assert flow_service.reconcile_calls == [running_run_id]
        assert rest.interaction_responses[0]["payload"]["type"] == 5
        content = rest.interaction_responses[1]["payload"]["data"]["content"]
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

    flow_service = _FlowServiceStub()
    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.service.build_ticket_flow_orchestration_service",
        lambda *, workspace_root: flow_service,
    )

    rest = _FakeRest()
    gateway = _FakeGateway(
        [
            _flow_interaction(
                name="reply",
                options=[
                    {"type": 3, "name": "run_id", "value": paused_run_id},
                    {"type": 3, "name": "text", "value": "Please continue"},
                ],
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
        assert flow_service.resume_calls == [paused_run_id]
        reply_path = (
            workspace / ".codex-autorunner" / "runs" / paused_run_id / "USER_REPLY.md"
        )
        assert reply_path.exists()
        assert reply_path.read_text(encoding="utf-8").strip() == "Please continue"
        assert len(rest.interaction_responses) == 2
        assert rest.interaction_responses[0]["payload"]["type"] == 5
        content = rest.interaction_responses[1]["payload"]["data"]["content"]
        assert paused_run_id in content
        assert "resumed run" in content.lower()

        inbound_path = (
            workspace
            / ".codex-autorunner"
            / "flows"
            / paused_run_id
            / "chat"
            / "inbound.jsonl"
        )
        outbound_path = (
            workspace
            / ".codex-autorunner"
            / "flows"
            / paused_run_id
            / "chat"
            / "outbound.jsonl"
        )
        inbound_records = _read_jsonl(inbound_path)
        outbound_records = _read_jsonl(outbound_path)
        assert inbound_records
        assert outbound_records
        assert inbound_records[-1]["event_type"] == "flow_reply_command"
        assert inbound_records[-1]["kind"] == "command"
        assert any(
            record.get("event_type") == "flow_reply_notice"
            and record.get("kind") == "notice"
            for record in outbound_records
        )
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
    _create_run(
        workspace,
        paused_run_id,
        status=FlowRunStatus.COMPLETED,
        started_at="2026-03-13T08:00:00Z",
        finished_at="2026-03-13T09:45:00Z",
    )
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
        assert "took 1h 45m" in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_flow_status_in_pma_mode_uses_manifest_display_name(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    manifest_path = tmp_path / ".codex-autorunner" / "manifest.yml"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        "\n".join(
            [
                "version: 2",
                "repos:",
                "  - id: workspace",
                "    path: workspace",
                "    enabled: true",
                "    auto_run: false",
                "    kind: base",
                "    display_name: Friendly Workspace",
            ]
        )
        + "\n",
        encoding="utf-8",
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
        assert "Friendly Workspace" in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_flow_status_in_pma_mode_shows_only_active_chat_bound_worktrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    visible_worktree = tmp_path / "wt-visible"
    visible_worktree.mkdir(parents=True)
    (visible_worktree / ".git").mkdir()
    seed_repo_files(visible_worktree, git_required=False)

    manifest_path = tmp_path / ".codex-autorunner" / "manifest.yml"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        "\n".join(
            [
                "version: 2",
                "repos:",
                "  - id: workspace",
                "    path: workspace",
                "    enabled: true",
                "    auto_run: false",
                "    kind: base",
                "  - id: workspace--wt-visible",
                "    path: wt-visible",
                "    enabled: true",
                "    auto_run: false",
                "    kind: worktree",
                "    worktree_of: workspace",
                "  - id: workspace--wt-hidden",
                "    path: wt-hidden",
                "    enabled: true",
                "    auto_run: false",
                "    kind: worktree",
                "    worktree_of: workspace",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        hub_overview_module,
        "active_chat_binding_counts",
        lambda *, hub_root, raw_config: {"workspace--wt-visible": 1},
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
        assert "wt-visible" in content
        assert "wt-hidden" not in content
        assert "\n  -> " in content
        assert "\n  - " not in content
    finally:
        await store.close()


@pytest.mark.anyio
async def test_flow_status_in_pma_mode_includes_manifest_worktree_with_active_flow_without_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    hidden_root = tmp_path / "wt-hidden"
    hidden_root.mkdir(parents=True, exist_ok=True)
    _create_run(hidden_root, str(uuid.uuid4()), status=FlowRunStatus.RUNNING)

    manifest_path = tmp_path / ".codex-autorunner" / "manifest.yml"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        "\n".join(
            [
                "version: 2",
                "repos:",
                "  - id: workspace",
                "    path: workspace",
                "    enabled: true",
                "    auto_run: false",
                "    kind: base",
                "  - id: workspace--wt-visible",
                "    path: wt-visible",
                "    enabled: true",
                "    auto_run: false",
                "    kind: worktree",
                "    worktree_of: workspace",
                "  - id: workspace--wt-hidden",
                "    path: wt-hidden",
                "    enabled: true",
                "    auto_run: false",
                "    kind: worktree",
                "    worktree_of: workspace",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        hub_overview_module,
        "active_chat_binding_counts",
        lambda *, hub_root, raw_config: {},
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
        assert "wt-hidden" in content
        assert "wt-visible" not in content
        assert "\n  -> " in content
        assert "\n  - " not in content
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

    flow_service = _FlowServiceStub()
    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.service.build_ticket_flow_orchestration_service",
        lambda *, workspace_root: flow_service,
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
        assert flow_service.reconcile_calls == [target_run_id]
        assert rest.interaction_responses[0]["payload"]["type"] == 5
        content = rest.interaction_responses[1]["payload"]["data"]["content"]
        assert f"Recovered for run {target_run_id}" in content
    finally:
        await store.close()
