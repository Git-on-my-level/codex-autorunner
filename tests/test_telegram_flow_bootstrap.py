from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_autorunner.core.flows import FlowStore
from codex_autorunner.core.flows.models import FlowRunStatus
from codex_autorunner.integrations.telegram.adapter import TelegramMessage
from codex_autorunner.integrations.telegram.handlers.commands import (
    flows as flows_module,
)
from codex_autorunner.integrations.telegram.handlers.commands.flows import FlowCommands


class _FlowServiceStub:
    def __init__(self) -> None:
        self.start_calls: list[dict[str, object]] = []
        self.ensure_calls: list[tuple[str, bool]] = []

    async def start_flow_run(
        self,
        _flow_target_id: str,
        *,
        input_data: dict[str, object] | None = None,
        metadata: dict[str, object] | None = None,
        run_id: str | None = None,
    ) -> object:
        self.start_calls.append(
            {
                "input_data": input_data or {},
                "metadata": metadata or {},
                "run_id": run_id,
            }
        )
        return type("Run", (), {"run_id": run_id or "run-1"})()

    def ensure_flow_run_worker(self, run_id: str, *, is_terminal: bool = False) -> None:
        self.ensure_calls.append((run_id, is_terminal))


class _FlowBootstrapHandler(FlowCommands):
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.markups: list[dict[str, object] | None] = []
        self.prompts: list[str] = []
        self.prompt_responses: list[str | None] = []
        self.seed_issue_refs: list[str] = []
        self.seed_plan_texts: list[str] = []

    async def _send_message(
        self,
        _chat_id: int,
        text: str,
        *,
        thread_id: int | None = None,
        reply_to: int | None = None,
        reply_markup: dict[str, object] | None = None,
        parse_mode: str | None = None,
    ) -> None:
        _ = (thread_id, reply_to, parse_mode)
        self.sent.append(text)
        self.markups.append(reply_markup)

    async def _prompt_flow_text_input(
        self, _message: TelegramMessage, prompt_text: str
    ) -> str | None:
        self.prompts.append(prompt_text)
        if self.prompt_responses:
            return self.prompt_responses.pop(0)
        return None

    def _github_bootstrap_status(self, _repo_root: Path) -> tuple[bool, str | None]:
        return False, None

    async def _seed_issue_from_ref(
        self, _repo_root: Path, issue_ref: str
    ) -> tuple[int, str]:
        self.seed_issue_refs.append(issue_ref)
        return 123, "example/repo"

    def _seed_issue_from_plan(self, _repo_root: Path, plan_text: str) -> None:
        self.seed_plan_texts.append(plan_text)


def _message() -> TelegramMessage:
    return TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=999,
        thread_id=123,
        from_user_id=1,
        text="/flow bootstrap",
        date=None,
        is_topic_message=True,
    )


@pytest.mark.anyio
async def test_flow_bootstrap_skips_prompt_when_tickets_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path
    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    (ticket_dir / "TICKET-001.md").write_text("ticket", encoding="utf-8")

    flow_service = _FlowServiceStub()

    async def _start_flow_run(
        _flow_target_id: str,
        *,
        input_data: dict[str, object] | None = None,
        metadata: dict[str, object] | None = None,
        run_id: str | None = None,
    ) -> object:
        flow_service.start_calls.append(
            {
                "input_data": input_data or {},
                "metadata": metadata or {},
                "run_id": run_id,
            }
        )
        store = FlowStore(repo_root / ".codex-autorunner" / "flows.db")
        store.initialize()
        store.create_flow_run("run-1", "ticket_flow", {})
        store.update_flow_run_status("run-1", FlowRunStatus.RUNNING)
        store.close()
        return type("Run", (), {"run_id": "run-1"})()

    flow_service.start_flow_run = _start_flow_run  # type: ignore[method-assign]
    monkeypatch.setattr(
        flows_module,
        "build_ticket_flow_orchestration_service",
        lambda *, workspace_root: flow_service,
    )
    monkeypatch.setattr(
        flows_module,
        "build_flow_status_snapshot",
        lambda _root, _record, _store: {
            "worker_health": SimpleNamespace(status="alive", pid=123, message=None),
            "effective_current_ticket": None,
            "last_event_seq": 1,
            "last_event_at": "2026-03-16T03:25:44Z",
            "freshness": {"summary": "fresh"},
        },
    )

    handler = _FlowBootstrapHandler()
    await handler._handle_flow_bootstrap(_message(), repo_root, argv=[])

    assert handler.prompts == []
    assert flow_service.start_calls
    inbound_path = (
        repo_root / ".codex-autorunner" / "flows" / "run-1" / "chat" / "inbound.jsonl"
    )
    outbound_path = (
        repo_root / ".codex-autorunner" / "flows" / "run-1" / "chat" / "outbound.jsonl"
    )
    assert inbound_path.exists()
    assert outbound_path.exists()
    inbound_records = [
        json.loads(line)
        for line in inbound_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    outbound_records = [
        json.loads(line)
        for line in outbound_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert inbound_records[-1]["event_type"] == "flow_bootstrap_command"
    assert inbound_records[-1]["kind"] == "command"
    assert outbound_records[-1]["event_type"] == "flow_bootstrap_started_notice"
    assert outbound_records[-1]["kind"] == "notice"
    assert "Started ticket flow run `run-1`." in handler.sent[-1]
    assert "Run: run-1" in handler.sent[-1]
    assert "Status: running" in handler.sent[-1]
    assert handler.markups[-1] is not None


@pytest.mark.anyio
async def test_flow_bootstrap_skips_prompt_when_issue_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path
    issue_path = repo_root / ".codex-autorunner" / "ISSUE.md"
    issue_path.parent.mkdir(parents=True, exist_ok=True)
    issue_path.write_text("Issue content", encoding="utf-8")

    flow_service = _FlowServiceStub()
    monkeypatch.setattr(
        flows_module,
        "build_ticket_flow_orchestration_service",
        lambda *, workspace_root: flow_service,
    )

    handler = _FlowBootstrapHandler()
    await handler._handle_flow_bootstrap(_message(), repo_root, argv=[])

    assert handler.prompts == []
    assert flow_service.start_calls


@pytest.mark.anyio
async def test_flow_bootstrap_prompts_for_issue_when_github_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path
    flow_service = _FlowServiceStub()
    monkeypatch.setattr(
        flows_module,
        "build_ticket_flow_orchestration_service",
        lambda *, workspace_root: flow_service,
    )

    handler = _FlowBootstrapHandler()

    def _gh_status(_root: Path) -> tuple[bool, str | None]:
        return True, "example/repo"

    handler._github_bootstrap_status = _gh_status  # type: ignore[assignment]
    handler.prompt_responses = ["https://github.com/example/repo/issues/123"]

    await handler._handle_flow_bootstrap(_message(), repo_root, argv=[])

    assert handler.prompts
    assert handler.seed_issue_refs == ["https://github.com/example/repo/issues/123"]
    assert flow_service.start_calls


@pytest.mark.anyio
async def test_flow_bootstrap_prompts_for_plan_when_github_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path
    flow_service = _FlowServiceStub()
    monkeypatch.setattr(
        flows_module,
        "build_ticket_flow_orchestration_service",
        lambda *, workspace_root: flow_service,
    )

    handler = _FlowBootstrapHandler()
    handler.prompt_responses = ["do the thing"]

    await handler._handle_flow_bootstrap(_message(), repo_root, argv=[])

    assert handler.prompts
    assert handler.seed_plan_texts == ["do the thing"]
    assert flow_service.start_calls


@pytest.mark.anyio
async def test_flow_bootstrap_reuses_active_run_without_spawning_new_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = FlowStore(db_path)
    store.initialize()
    run_id = str(uuid.uuid4())
    store.create_flow_run(run_id, "ticket_flow", {})
    store.update_flow_run_status(run_id, FlowRunStatus.RUNNING)
    store.close()

    flow_service = _FlowServiceStub()
    monkeypatch.setattr(
        flows_module,
        "build_ticket_flow_orchestration_service",
        lambda *, workspace_root: flow_service,
    )

    handler = _FlowBootstrapHandler()
    await handler._handle_flow_bootstrap(_message(), repo_root, argv=[])

    assert flow_service.ensure_calls == [(run_id, False)]
    assert any("Reusing ticket flow run" in message for message in handler.sent)
