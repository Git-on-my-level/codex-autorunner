from __future__ import annotations

from pathlib import Path

import pytest

from codex_autorunner.integrations.telegram.adapter import TelegramMessage
from codex_autorunner.integrations.telegram.handlers.commands import (
    flows as flows_module,
)
from codex_autorunner.integrations.telegram.handlers.commands.flows import FlowCommands


class _FlowRecord:
    def __init__(self, run_id: str) -> None:
        self.id = run_id


class _ControllerStub:
    def __init__(self) -> None:
        self.start_calls: list[dict[str, object]] = []

    async def start_flow(
        self, *, input_data: dict[str, object], metadata: dict[str, object]
    ) -> _FlowRecord:
        self.start_calls.append({"input_data": input_data, "metadata": metadata})
        return _FlowRecord("run-1")


class _FlowBootstrapHandler(FlowCommands):
    def __init__(self) -> None:
        self.sent: list[str] = []
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
        _ = (thread_id, reply_to, reply_markup, parse_mode)
        self.sent.append(text)

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

    controller = _ControllerStub()
    monkeypatch.setattr(
        flows_module, "_get_ticket_controller", lambda _root: controller
    )
    monkeypatch.setattr(flows_module, "_spawn_flow_worker", lambda _root, _run: None)

    handler = _FlowBootstrapHandler()
    await handler._handle_flow_bootstrap(_message(), repo_root, argv=[])

    assert handler.prompts == []
    assert controller.start_calls


@pytest.mark.anyio
async def test_flow_bootstrap_skips_prompt_when_issue_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path
    issue_path = repo_root / ".codex-autorunner" / "ISSUE.md"
    issue_path.parent.mkdir(parents=True, exist_ok=True)
    issue_path.write_text("Issue content", encoding="utf-8")

    controller = _ControllerStub()
    monkeypatch.setattr(
        flows_module, "_get_ticket_controller", lambda _root: controller
    )
    monkeypatch.setattr(flows_module, "_spawn_flow_worker", lambda _root, _run: None)

    handler = _FlowBootstrapHandler()
    await handler._handle_flow_bootstrap(_message(), repo_root, argv=[])

    assert handler.prompts == []
    assert controller.start_calls


@pytest.mark.anyio
async def test_flow_bootstrap_prompts_for_issue_when_github_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path
    controller = _ControllerStub()
    monkeypatch.setattr(
        flows_module, "_get_ticket_controller", lambda _root: controller
    )
    monkeypatch.setattr(flows_module, "_spawn_flow_worker", lambda _root, _run: None)

    handler = _FlowBootstrapHandler()

    def _gh_status(_root: Path) -> tuple[bool, str | None]:
        return True, "example/repo"

    handler._github_bootstrap_status = _gh_status  # type: ignore[assignment]
    handler.prompt_responses = ["https://github.com/example/repo/issues/123"]

    await handler._handle_flow_bootstrap(_message(), repo_root, argv=[])

    assert handler.prompts
    assert handler.seed_issue_refs == ["https://github.com/example/repo/issues/123"]
    assert controller.start_calls


@pytest.mark.anyio
async def test_flow_bootstrap_prompts_for_plan_when_github_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path
    controller = _ControllerStub()
    monkeypatch.setattr(
        flows_module, "_get_ticket_controller", lambda _root: controller
    )
    monkeypatch.setattr(flows_module, "_spawn_flow_worker", lambda _root, _run: None)

    handler = _FlowBootstrapHandler()
    handler.prompt_responses = ["do the thing"]

    await handler._handle_flow_bootstrap(_message(), repo_root, argv=[])

    assert handler.prompts
    assert handler.seed_plan_texts == ["do the thing"]
    assert controller.start_calls
