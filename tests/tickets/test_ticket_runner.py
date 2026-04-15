from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from typing import Callable

import pytest

from codex_autorunner.agents.hermes_identity import CanonicalHermesIdentity
from codex_autorunner.tickets import runner as runner_module
from codex_autorunner.tickets import runner_selection as runner_selection_module
from codex_autorunner.tickets.agent_pool import AgentTurnRequest, AgentTurnResult
from codex_autorunner.tickets.models import (
    DEFAULT_MAX_TOTAL_TURNS,
    TicketRunConfig,
)
from codex_autorunner.tickets.runner import (
    TICKET_CONTEXT_TOTAL_MAX_BYTES,
    TicketRunner,
)


def _write_ticket(
    path: Path,
    *,
    agent: str = "codex",
    done: bool = False,
    body: str = "Do the thing",
    frontmatter_extra: str = "",
) -> None:
    text = (
        "---\n"
        f"ticket_id: tkt_{uuid.uuid4().hex}\n"
        f"agent: {agent}\n"
        f"done: {str(done).lower()}\n"
        f"{frontmatter_extra}"
        "title: Test\n"
        "goal: Finish the test\n"
        "---\n\n"
        f"{body}\n"
    )
    path.write_text(text, encoding="utf-8")


def _set_ticket_done(path: Path, *, done: bool = True) -> None:
    raw = path.read_text(encoding="utf-8")
    raw = raw.replace("done: false", f"done: {str(done).lower()}")
    path.write_text(raw, encoding="utf-8")


def _corrupt_ticket_frontmatter(path: Path) -> None:
    raw = path.read_text(encoding="utf-8")
    # Make 'done' invalid.
    raw = raw.replace("done: false", "done: notabool")
    path.write_text(raw, encoding="utf-8")


def _set_ticket_id(path: Path, ticket_id: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.startswith("ticket_id:"):
            lines[index] = f'ticket_id: "{ticket_id}"'
            break
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"], cwd=path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True
    )


def test_ticket_run_config_default_max_turns() -> None:
    cfg = TicketRunConfig(
        ticket_dir=Path(".codex-autorunner/tickets"),
        auto_commit=False,
    )
    assert cfg.max_total_turns == DEFAULT_MAX_TOTAL_TURNS


class FakeAgentPool:
    def __init__(self, handler: Callable[[AgentTurnRequest], AgentTurnResult]):
        self._handler = handler
        self.requests: list[AgentTurnRequest] = []

    async def run_turn(self, req: AgentTurnRequest) -> AgentTurnResult:
        self.requests.append(req)
        return self._handler(req)


@pytest.mark.asyncio
async def test_ticket_runner_pauses_when_no_tickets(tmp_path: Path) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)

    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        ),
        agent_pool=FakeAgentPool(
            lambda req: AgentTurnResult(
                agent_id=req.agent_id,
                conversation_id=req.conversation_id or "conv",
                turn_id="t1",
                text="noop",
            )
        ),
    )

    result = await runner.step({})
    assert result.status == "paused"
    assert "No tickets found" in (result.reason or "")


@pytest.mark.asyncio
async def test_ticket_runner_fails_when_required_context_file_missing(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(
        ticket_path,
        frontmatter_extra=("context:\n  - path: docs/missing.md\n    required: true\n"),
    )

    pool = FakeAgentPool(
        lambda req: AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id=req.conversation_id or "conv",
            turn_id="t1",
            text="noop",
        )
    )
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    result = await runner.step({})
    assert result.status == "failed"
    assert "Required ticket context file missing" in (result.reason or "")
    assert "docs/missing.md" in (result.reason_details or "")
    assert len(pool.requests) == 0


@pytest.mark.asyncio
async def test_ticket_runner_marks_non_required_missing_context_in_prompt(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(
        ticket_path,
        frontmatter_extra=(
            "context:\n  - path: docs/optional-missing.md\n    required: false\n"
        ),
    )

    pool = FakeAgentPool(
        lambda req: AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id=req.conversation_id or "conv",
            turn_id="t1",
            text="ok",
        )
    )
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    result = await runner.step({})
    assert result.status == "continue"
    assert len(pool.requests) == 1
    prompt = pool.requests[0].prompt
    assert "<CAR_REQUESTED_CONTEXT>" in prompt
    assert "docs/optional-missing.md" in prompt
    assert "status: missing" in prompt


@pytest.mark.asyncio
async def test_ticket_runner_includes_read_error_for_binary_context_file(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    docs_dir = workspace_root / "docs"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    docs_dir.mkdir(parents=True, exist_ok=True)
    binary_path = docs_dir / "binary.txt"
    binary_path.write_bytes(b"\xff\xfe\x00")

    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(
        ticket_path,
        frontmatter_extra=(
            "context:\n  - path: docs/binary.txt\n    required: false\n"
        ),
    )

    pool = FakeAgentPool(
        lambda req: AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id=req.conversation_id or "conv",
            turn_id="t1",
            text="ok",
        )
    )

    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    result = await runner.step({})
    assert result.status == "continue"
    assert len(pool.requests) == 1
    prompt = pool.requests[0].prompt
    assert "docs/binary.txt" in prompt
    assert "read_error" in prompt


@pytest.mark.asyncio
async def test_ticket_runner_requested_context_respects_size_caps(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    docs_dir = workspace_root / "docs"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "large.txt").write_text("A" * (TICKET_CONTEXT_TOTAL_MAX_BYTES * 2))
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(
        ticket_path,
        frontmatter_extra=(
            "context:\n"
            "  - path: docs/large.txt\n"
            f"    max_bytes: {TICKET_CONTEXT_TOTAL_MAX_BYTES * 2}\n"
        ),
    )

    pool = FakeAgentPool(
        lambda req: AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id=req.conversation_id or "conv",
            turn_id="t1",
            text="ok",
        )
    )
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    result = await runner.step({})
    assert result.status == "continue"
    prompt = pool.requests[0].prompt
    start = prompt.index("<CAR_REQUESTED_CONTEXT>") + len("<CAR_REQUESTED_CONTEXT>\n")
    end = prompt.index("</CAR_REQUESTED_CONTEXT>")
    section = prompt[start:end].rstrip("\n")
    assert len(section.encode("utf-8")) <= TICKET_CONTEXT_TOTAL_MAX_BYTES


@pytest.mark.asyncio
async def test_ticket_runner_recovers_when_current_ticket_path_is_stale(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)

    renamed_ticket_path = ticket_dir / "TICKET-009-ALREADY-DONE.md"
    _write_ticket(renamed_ticket_path, done=False)

    pool = FakeAgentPool(
        lambda req: AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id=req.conversation_id or "conv",
            turn_id="t1",
            text="noop",
        )
    )
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    stale_state = {
        "current_ticket": ".codex-autorunner/tickets/TICKET-009.md",
        "ticket_turns": 3,
        "last_agent_output": "stale output",
        "lint": {"errors": ["old"], "retries": 1},
        "commit": {"pending": True, "retries": 1},
    }

    result = await runner.step(stale_state)

    assert result.status == "continue"
    assert len(pool.requests) == 1
    assert "<CAR_COMMIT_REQUIRED>" not in pool.requests[0].prompt
    assert (
        result.state.get("current_ticket")
        == ".codex-autorunner/tickets/TICKET-009-ALREADY-DONE.md"
    )
    assert result.state.get("ticket_turns") == 1
    assert result.state.get("last_agent_output") == "noop"
    assert result.state.get("lint") is None
    assert result.state.get("commit") is None


@pytest.mark.asyncio
async def test_ticket_runner_delegates_selection_and_pre_turn_planning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, done=False)

    calls: list[tuple[str, str]] = []
    real_select = runner_selection_module.select_ticket
    real_plan = runner_selection_module.plan_pre_turn
    real_validate = runner_selection_module.validate_ticket_for_execution

    def wrapped_select(**kwargs):
        result = real_select(**kwargs)
        selected_rel_path = result.selected.rel_path if result.selected else ""
        calls.append(("select", selected_rel_path))
        return result

    def wrapped_plan(**kwargs):
        selection = kwargs["selection_result"]
        selected_rel_path = selection.selected.rel_path if selection.selected else ""
        calls.append(("plan", selected_rel_path))
        return real_plan(**kwargs)

    def wrapped_validate(**kwargs):
        calls.append(("validate", kwargs["ticket_path"].name))
        return real_validate(**kwargs)

    monkeypatch.setattr(runner_module.runner_selection, "select_ticket", wrapped_select)
    monkeypatch.setattr(runner_module.runner_selection, "plan_pre_turn", wrapped_plan)
    monkeypatch.setattr(
        runner_module.runner_selection,
        "validate_ticket_for_execution",
        wrapped_validate,
    )

    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        ),
        agent_pool=FakeAgentPool(
            lambda req: AgentTurnResult(
                agent_id=req.agent_id,
                conversation_id=req.conversation_id or "conv",
                turn_id="t1",
                text="ok",
            )
        ),
    )

    result = await runner.step({})

    assert result.status == "continue"
    assert calls == [
        ("select", ".codex-autorunner/tickets/TICKET-001.md"),
        ("plan", ".codex-autorunner/tickets/TICKET-001.md"),
        ("validate", "TICKET-001.md"),
    ]


@pytest.mark.asyncio
async def test_ticket_runner_respects_validation_pause_from_selection_seam(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, done=False)

    pool = FakeAgentPool(
        lambda req: AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id=req.conversation_id or "conv",
            turn_id="t1",
            text="unexpected",
        )
    )

    def paused_validation(**kwargs):
        _ = kwargs
        from codex_autorunner.tickets.runner_types import TicketValidationResult

        return TicketValidationResult(
            status="paused",
            pause_reason="Paused from validation seam.",
            pause_reason_code="user_pause",
        )

    monkeypatch.setattr(
        runner_module.runner_selection,
        "validate_ticket_for_execution",
        paused_validation,
    )

    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    result = await runner.step({})

    assert result.status == "paused"
    assert result.reason == "Paused from validation seam."
    assert len(pool.requests) == 0


@pytest.mark.asyncio
async def test_ticket_runner_completes_when_all_tickets_done(tmp_path: Path) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, done=False)

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        _set_ticket_done(ticket_path, done=True)
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id=req.conversation_id or "conv1",
            turn_id="t1",
            text="done",
        )

    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        ),
        agent_pool=FakeAgentPool(handler),
    )

    # First step runs agent and marks ticket done.
    r1 = await runner.step({})
    assert r1.status == "continue"
    assert r1.state.get("current_ticket") is None

    # Second step should observe all done.
    r2 = await runner.step(r1.state)
    assert r2.status == "completed"
    assert "All tickets done" in (r2.reason or "")
    assert not (workspace_root / ".codex-autorunner" / "run_index.json").exists()


@pytest.mark.asyncio
async def test_ticket_runner_dispatch_pause_message(tmp_path: Path) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, done=False)

    runs_dir = Path(".codex-autorunner/runs")
    run_id = "run-1"
    run_dir = workspace_root / runs_dir / run_id
    dispatch_dir = run_dir / "dispatch"
    dispatch_path = run_dir / "DISPATCH.md"

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        dispatch_dir.mkdir(parents=True, exist_ok=True)
        (dispatch_dir / "review.md").write_text("Please review", encoding="utf-8")
        dispatch_path.write_text(
            "---\nmode: pause\n---\n\nReview attached.\n", encoding="utf-8"
        )
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id=req.conversation_id or "conv1",
            turn_id="t1",
            text="wrote outbox",
        )

    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id=run_id,
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        ),
        agent_pool=FakeAgentPool(handler),
    )

    r1 = await runner.step({})
    assert r1.status == "paused"
    assert r1.dispatch is not None
    assert r1.dispatch.dispatch.mode == "pause"
    assert r1.dispatch.dispatch.extra["ticket_id"] == (
        ".codex-autorunner/tickets/TICKET-001.md"
    )
    # dispatch_seq is 2: dispatch at seq=1, turn_summary at seq=2
    assert r1.state.get("dispatch_seq") == 2
    assert (run_dir / "dispatch_history" / "0001" / "DISPATCH.md").exists()
    assert (run_dir / "dispatch_history" / "0001" / "review.md").exists()
    # Turn summary should also be created
    assert (run_dir / "dispatch_history" / "0002" / "DISPATCH.md").exists()


@pytest.mark.asyncio
async def test_ticket_runner_lint_retry_reuses_conversation_id(tmp_path: Path) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, done=False)
    ticket_id_holder: list[str] = []

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        if len(pool.requests) == 1:
            _corrupt_ticket_frontmatter(ticket_path)
            return AgentTurnResult(
                agent_id=req.agent_id,
                conversation_id="conv1",
                turn_id="t1",
                text="corrupted",
            )

        # Second pass fixes the frontmatter.
        _write_ticket(ticket_path, done=False)
        if ticket_id_holder:
            _set_ticket_id(ticket_path, ticket_id_holder[0])
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id=req.conversation_id,
            turn_id="t2",
            text="fixed",
        )

    pool = FakeAgentPool(handler)
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            max_lint_retries=3,
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    # First step triggers lint retry (continue, with lint state set).
    r1 = await runner.step({})
    assert r1.status == "continue"
    assert isinstance(r1.state.get("lint"), dict)
    ticket_id = r1.state["current_ticket_id"]
    ticket_id_holder[:] = [ticket_id]

    # Second step should pass conversation id + include lint errors in the prompt.
    _write_ticket(ticket_path, done=False)
    _set_ticket_id(ticket_path, ticket_id)
    r2 = await runner.step(r1.state)
    assert r2.status == "continue"
    assert r2.state.get("lint") is None

    assert len(pool.requests) == 2
    assert pool.requests[1].conversation_id == "conv1"
    assert "Ticket frontmatter lint failed" in pool.requests[1].prompt


@pytest.mark.asyncio
async def test_ticket_runner_lint_retry_drops_conversation_after_profile_change(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, agent="hermes", done=False)

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        if req.conversation_id is None and len(pool.requests) == 1:
            _corrupt_ticket_frontmatter(ticket_path)
            return AgentTurnResult(
                agent_id=req.agent_id,
                conversation_id="conv1",
                turn_id="t1",
                text="corrupted",
            )

        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id=req.conversation_id or "conv2",
            turn_id="t2",
            text="fixed",
        )

    pool = FakeAgentPool(handler)
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            max_lint_retries=3,
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    r1 = await runner.step({})
    assert r1.status == "continue"
    assert isinstance(r1.state.get("lint"), dict)
    ticket_id = r1.state["current_ticket_id"]

    _write_ticket(
        ticket_path,
        agent="hermes",
        done=False,
        frontmatter_extra="profile: m4-pma\n",
    )
    _set_ticket_id(ticket_path, ticket_id)
    r2 = await runner.step(r1.state)

    assert r2.status == "continue"
    assert pool.requests[1].conversation_id is None
    assert r2.state.get("lint") is None


@pytest.mark.asyncio
async def test_ticket_runner_passes_workspace_root_to_hermes_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, agent="hermes", done=False)

    observed: dict[str, object] = {}

    def _canonicalize(agent_id: str, profile: str | None = None, *, context=None):
        observed["agent_id"] = agent_id
        observed["profile"] = profile
        observed["context"] = context
        return CanonicalHermesIdentity(agent="hermes", profile="m4-pma")

    monkeypatch.setattr(runner_module, "canonicalize_hermes_identity", _canonicalize)

    pool = FakeAgentPool(
        lambda req: AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id=req.conversation_id or "conv1",
            turn_id="t1",
            text="ok",
        )
    )
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    result = await runner.step({})

    assert result.status == "continue"
    assert observed["agent_id"] == "hermes"
    assert observed["context"] == workspace_root
    assert pool.requests[0].agent_id == "hermes"


@pytest.mark.asyncio
async def test_ticket_runner_switches_agents_between_tickets(tmp_path: Path) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_1 = ticket_dir / "TICKET-001.md"
    ticket_2 = ticket_dir / "TICKET-002.md"
    _write_ticket(ticket_1, agent="codex", done=False)
    _write_ticket(ticket_2, agent="opencode", done=False)

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        if req.agent_id == "codex":
            _set_ticket_done(ticket_1, done=True)
            return AgentTurnResult(
                agent_id=req.agent_id,
                conversation_id="conv-codex",
                turn_id="t1",
                text="codex turn",
            )
        _set_ticket_done(ticket_2, done=True)
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id="conv-opencode",
            turn_id="t2",
            text="opencode turn",
        )

    pool = FakeAgentPool(handler)
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    r1 = await runner.step({})
    r2 = await runner.step(r1.state)
    await runner.step(r2.state)

    assert len(pool.requests) == 2


@pytest.mark.asyncio
async def test_ticket_runner_executes_hermes_ticket(tmp_path: Path) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, agent="hermes", done=False)

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        assert req.agent_id == "hermes"
        _set_ticket_done(ticket_path, done=True)
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id="conv-hermes",
            turn_id="t-hermes",
            text="hermes turn",
        )

    pool = FakeAgentPool(handler)
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    result = await runner.step({})

    assert result.status == "continue"
    assert len(pool.requests) == 1
    assert pool.requests[0].agent_id == "hermes"
    assert result.agent_id == "hermes"
    assert result.agent_conversation_id == "conv-hermes"


@pytest.mark.asyncio
async def test_ticket_runner_reuses_bound_thread_for_unfinished_ticket(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, agent="hermes", done=False)

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        conversation_id = req.conversation_id or "thread-1"
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id=conversation_id,
            turn_id=f"turn-{len(pool.requests)}",
            text="still working",
        )

    pool = FakeAgentPool(handler)
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    first = await runner.step({})
    second = await runner.step(first.state)

    assert first.status == "continue"
    assert second.status == "continue"
    assert len(pool.requests) == 2
    assert pool.requests[0].conversation_id is None
    assert pool.requests[1].conversation_id == "thread-1"
    bindings = second.state.get("ticket_thread_bindings") or {}
    ticket_id = second.state.get("current_ticket_id")
    assert bindings[ticket_id]["thread_target_id"] == "thread-1"
    assert second.state.get("ticket_thread_debug", {}).get("action") == "reused"


@pytest.mark.asyncio
async def test_ticket_runner_resets_binding_when_ticket_profile_changes(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, agent="hermes", done=False)

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        if req.conversation_id is None and len(pool.requests) == 1:
            return AgentTurnResult(
                agent_id=req.agent_id,
                conversation_id="thread-default",
                turn_id="t1",
                text="default profile turn",
            )
        assert req.conversation_id is None
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id="thread-profile",
            turn_id="t2",
            text="named profile turn",
        )

    pool = FakeAgentPool(handler)
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    first = await runner.step({})
    raw = ticket_path.read_text(encoding="utf-8")
    ticket_path.write_text(
        raw.replace("title: Test\n", "profile: m4-pma\ntitle: Test\n"),
        encoding="utf-8",
    )
    second = await runner.step(first.state)

    assert first.status == "continue"
    assert second.status == "continue"
    assert len(pool.requests) == 2
    assert pool.requests[0].conversation_id is None
    assert pool.requests[1].conversation_id is None
    bindings = second.state.get("ticket_thread_bindings") or {}
    ticket_id = second.state.get("current_ticket_id")
    assert bindings[ticket_id]["thread_target_id"] == "thread-profile"
    assert (
        second.state.get("ticket_thread_debug", {}).get("reason") == "profile_changed"
    )


@pytest.mark.asyncio
async def test_ticket_runner_refreshes_stale_bound_thread(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, agent="hermes", done=False)

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        if req.conversation_id == "stale-thread":
            raise KeyError("Unknown thread target 'stale-thread'")
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id=req.conversation_id or "fresh-thread",
            turn_id=f"t{len(pool.requests)}",
            text="fresh thread turn",
        )

    pool = FakeAgentPool(handler)
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    seeded_state = {
        "current_ticket": ".codex-autorunner/tickets/TICKET-001.md",
        "current_ticket_id": "tkt_stale_thread",
        "ticket_thread_bindings": {
            "tkt_stale_thread": {
                "thread_target_id": "stale-thread",
                "agent_id": "hermes",
                "ticket_path": ".codex-autorunner/tickets/TICKET-001.md",
            }
        },
    }
    _set_ticket_id(ticket_path, "tkt_stale_thread")

    result = await runner.step(seeded_state)

    assert result.status == "continue"
    assert len(pool.requests) == 2
    assert pool.requests[0].conversation_id == "stale-thread"
    assert pool.requests[1].conversation_id is None
    bindings = result.state.get("ticket_thread_bindings") or {}
    assert bindings["tkt_stale_thread"]["thread_target_id"] == "fresh-thread"
    assert result.state.get("ticket_thread_debug", {}).get("reason") == (
        "stale_or_missing_thread_target"
    )


@pytest.mark.asyncio
async def test_ticket_runner_clears_completed_ticket_binding_before_next_ticket(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_1 = ticket_dir / "TICKET-001.md"
    ticket_2 = ticket_dir / "TICKET-002.md"
    _write_ticket(ticket_1, agent="hermes", done=False)
    _write_ticket(ticket_2, agent="hermes", done=False)

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        if len(pool.requests) == 1:
            _set_ticket_done(ticket_1, done=True)
            return AgentTurnResult(
                agent_id=req.agent_id,
                conversation_id="thread-1",
                turn_id="t1",
                text="ticket one done",
            )
        assert req.conversation_id is None
        _set_ticket_done(ticket_2, done=True)
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id="thread-2",
            turn_id="t2",
            text="ticket two done",
        )

    pool = FakeAgentPool(handler)
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    first = await runner.step({})
    second = await runner.step(first.state)

    assert first.status == "continue"
    assert second.status == "continue"
    assert len(pool.requests) == 2
    assert pool.requests[0].conversation_id is None
    assert pool.requests[1].conversation_id is None
    assert "ticket_thread_bindings" not in first.state
    assert "ticket_thread_bindings" not in second.state


async def test_ticket_runner_pauses_on_duplicate_ticket_indices(tmp_path: Path) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)

    _write_ticket(ticket_dir / "TICKET-001.md", done=False)
    _write_ticket(ticket_dir / "TICKET-001-duplicate.md", done=False)
    _write_ticket(ticket_dir / "TICKET-002.md", done=False)

    run_id = "run-1"

    pool = FakeAgentPool(
        lambda req: AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id="conv-1",
            turn_id="t1",
            text="done",
        )
    )

    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id=run_id,
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    result = await runner.step({})

    assert result.status == "paused"
    assert "Duplicate ticket indices" in result.reason
    assert "001" in result.reason_details


@pytest.mark.asyncio
async def test_previous_ticket_context_excluded_by_default(tmp_path: Path) -> None:
    """Test that previous ticket content is NOT included in prompt by default."""
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)

    ticket_1 = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_1, done=False)
    _set_ticket_done(ticket_1, done=True)

    ticket_2 = ticket_dir / "TICKET-002.md"
    _write_ticket(ticket_2, done=False)

    pool = FakeAgentPool(
        lambda req: AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id="conv",
            turn_id="t1",
            text="done",
        )
    )

    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
            include_previous_ticket_context=False,
        ),
        agent_pool=pool,
    )

    result = await runner.step({})

    assert result.status == "continue"
    assert len(pool.requests) == 1
    assert "PREVIOUS TICKET CONTEXT" not in pool.requests[0].prompt


@pytest.mark.asyncio
async def test_previous_ticket_context_included_when_enabled(tmp_path: Path) -> None:
    """Test that previous ticket content IS included (and truncated) when enabled."""
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)

    ticket_1 = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_1, done=False)
    _set_ticket_done(ticket_1, done=True)

    ticket_2 = ticket_dir / "TICKET-002.md"
    _write_ticket(ticket_2, done=False)

    pool = FakeAgentPool(
        lambda req: AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id="conv",
            turn_id="t1",
            text="done",
        )
    )

    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
            include_previous_ticket_context=True,
        ),
        agent_pool=pool,
    )

    result = await runner.step({})

    assert result.status == "continue"
    assert len(pool.requests) == 1
    assert "PREVIOUS TICKET CONTEXT (truncated to 16KB" in pool.requests[0].prompt
    assert (
        "Cross-ticket context should flow through contextspace docs"
        in pool.requests[0].prompt
    )
    assert "agent: codex\ndone: true" in pool.requests[0].prompt


def test_is_network_error_detection() -> None:
    """Test network error detection logic."""
    from codex_autorunner.tickets.runner import _is_network_error

    # Positive cases (should return True)
    assert _is_network_error("network error") is True
    assert _is_network_error("Connection timeout") is True
    assert _is_network_error("transport error") is True
    assert _is_network_error("stream disconnected") is True
    assert _is_network_error("Reconnecting... 1/5") is True
    assert _is_network_error("connection refused") is True
    assert _is_network_error("connection reset") is True
    assert _is_network_error("connection broken") is True
    assert _is_network_error("unreachable") is True
    assert _is_network_error("temporary failure") is True

    # Negative cases (should return False)
    assert _is_network_error("validation error") is False
    assert _is_network_error("config error") is False
    assert _is_network_error("permission denied") is False
    assert _is_network_error("auth failed") is False
    assert _is_network_error("") is False
    assert _is_network_error("successful completion") is False


@pytest.mark.asyncio
async def test_ticket_runner_retries_on_network_error(tmp_path: Path) -> None:
    """Test that network errors trigger automatic retries."""
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, done=False)

    call_count = 0
    max_network_errors = 2

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        nonlocal call_count
        call_count += 1
        if call_count <= max_network_errors:
            return AgentTurnResult(
                agent_id=req.agent_id,
                conversation_id="conv1",
                turn_id=f"t{call_count}",
                text="failed",
                error="Network error: stream disconnected",
            )
        _set_ticket_done(ticket_path, done=True)
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id="conv1",
            turn_id=f"t{call_count}",
            text="done",
        )

    pool = FakeAgentPool(handler)
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            max_network_retries=5,
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    # First step: network error 1, should retry
    r1 = await runner.step({})
    assert r1.status == "continue"
    assert "Network error detected (attempt 1/5)" in r1.reason
    assert r1.state.get("network_retry") is not None
    assert r1.state["network_retry"]["retries"] == 1

    # Second step: network error 2, should retry
    r2 = await runner.step(r1.state)
    assert r2.status == "continue"
    assert "Network error detected (attempt 2/5)" in r2.reason
    assert r2.state["network_retry"]["retries"] == 2

    # Third step: success, retry state should be cleared
    r3 = await runner.step(r2.state)
    assert r3.status == "continue"
    assert r3.state.get("network_retry") is None

    assert call_count == 3


@pytest.mark.asyncio
async def test_ticket_runner_persists_thread_binding_across_network_retry(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, agent="hermes", done=False)

    call_count = 0

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            assert req.conversation_id is None
            return AgentTurnResult(
                agent_id=req.agent_id,
                conversation_id="thread-1",
                turn_id="t1",
                text="failed",
                error="Network error: stream disconnected",
            )
        assert req.conversation_id == "thread-1"
        _set_ticket_done(ticket_path, done=True)
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id="thread-1",
            turn_id="t2",
            text="done",
        )

    pool = FakeAgentPool(handler)
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            max_network_retries=5,
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    first = await runner.step({})
    assert first.status == "continue"
    ticket_id = first.state["current_ticket_id"]
    bindings = first.state.get("ticket_thread_bindings") or {}
    assert bindings[ticket_id]["thread_target_id"] == "thread-1"

    second = await runner.step(first.state)
    assert second.status == "continue"
    assert len(pool.requests) == 2
    assert pool.requests[1].conversation_id == "thread-1"


@pytest.mark.asyncio
async def test_ticket_runner_pauses_after_network_retries_exhausted(
    tmp_path: Path,
) -> None:
    """Test that the runner pauses when network retries are exhausted."""
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, done=False)

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id="conv1",
            turn_id="t1",
            text="failed",
            error="Network error: connection timeout",
        )

    pool = FakeAgentPool(handler)
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            max_network_retries=2,
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    # Retry 1
    r1 = await runner.step({})
    assert r1.status == "continue"
    assert r1.state["network_retry"]["retries"] == 1

    # Retry 2
    r2 = await runner.step(r1.state)
    assert r2.status == "continue"
    assert r2.state["network_retry"]["retries"] == 2

    # Retry 3 (exhausted, should pause)
    r3 = await runner.step(r2.state)
    assert r3.status == "paused"
    assert r3.reason == "Agent turn failed. Fix the issue and resume."
    assert "Network error: connection timeout" in r3.reason_details
    assert r3.state.get("network_retry") is None

    assert len(pool.requests) == 3


@pytest.mark.asyncio
async def test_ticket_runner_clears_network_retry_on_non_network_error(
    tmp_path: Path,
) -> None:
    """Test that network retry state is cleared on non-network errors."""
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, done=False)

    call_count = 0

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return AgentTurnResult(
                agent_id=req.agent_id,
                conversation_id="conv1",
                turn_id="t1",
                text="failed",
                error="Network error: connection failed",
            )
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id="conv1",
            turn_id="t2",
            text="failed",
            error="Validation error: invalid config",
        )

    pool = FakeAgentPool(handler)
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            max_network_retries=5,
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    # First step: network error, set retry state
    r1 = await runner.step({})
    assert r1.status == "continue"
    assert r1.state["network_retry"]["retries"] == 1

    # Second step: non-network error, should pause immediately
    r2 = await runner.step(r1.state)
    assert r2.status == "paused"
    assert r2.reason == "Agent turn failed. Fix the issue and resume."
    assert "Validation error" in r2.reason_details
    assert r2.state.get("network_retry") is None


@pytest.mark.asyncio
async def test_ticket_runner_persists_thread_binding_across_paused_failure(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, agent="hermes", done=False)

    call_count = 0

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            assert req.conversation_id is None
            return AgentTurnResult(
                agent_id=req.agent_id,
                conversation_id="thread-1",
                turn_id="t1",
                text="failed",
                error="Validation error: still blocked",
            )
        assert req.conversation_id == "thread-1"
        _set_ticket_done(ticket_path, done=True)
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id="thread-1",
            turn_id="t2",
            text="done",
        )

    pool = FakeAgentPool(handler)
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    first = await runner.step({})
    assert first.status == "paused"
    ticket_id = first.state["current_ticket_id"]
    bindings = first.state.get("ticket_thread_bindings") or {}
    assert bindings[ticket_id]["thread_target_id"] == "thread-1"

    second = await runner.step(first.state)
    assert second.status == "continue"
    assert len(pool.requests) == 2
    assert pool.requests[1].conversation_id == "thread-1"


@pytest.mark.asyncio
async def test_ticket_runner_clears_network_retry_on_success(tmp_path: Path) -> None:
    """Test that network retry state is cleared on successful turn."""
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, done=False)

    call_count = 0

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return AgentTurnResult(
                agent_id=req.agent_id,
                conversation_id="conv1",
                turn_id="t1",
                text="failed",
                error="Network error: transport error",
            )
        _set_ticket_done(ticket_path, done=True)
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id="conv1",
            turn_id="t2",
            text="done",
        )

    pool = FakeAgentPool(handler)
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            max_network_retries=5,
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    # First step: network error, set retry state
    r1 = await runner.step({})
    assert r1.status == "continue"
    assert r1.state["network_retry"]["retries"] == 1

    # Second step: success, retry state should be cleared
    r2 = await runner.step(r1.state)
    assert r2.status == "continue"
    assert r2.state.get("network_retry") is None

    assert call_count == 2


@pytest.mark.asyncio
async def test_ticket_runner_archives_user_reply_before_turn(tmp_path: Path) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, done=False)

    run_dir = workspace_root / ".codex-autorunner" / "runs" / "run-1"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "USER_REPLY.md").write_text("User says hi\n", encoding="utf-8")

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        assert "User says hi" in req.prompt
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id=req.conversation_id or "conv1",
            turn_id="t1",
            text="ok",
        )

    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        ),
        agent_pool=FakeAgentPool(handler),
    )

    result = await runner.step({})
    assert result.status == "continue"

    archived_reply = run_dir / "reply_history" / "0001" / "USER_REPLY.md"
    assert archived_reply.exists()


@pytest.mark.asyncio
async def test_ticket_runner_only_consumes_archived_reply_after_success(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, done=False)

    run_dir = workspace_root / ".codex-autorunner" / "runs" / "run-1"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "USER_REPLY.md").write_text("Please unblock this\n", encoding="utf-8")

    prompts: list[str] = []
    call_count = 0

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        nonlocal call_count
        call_count += 1
        prompts.append(req.prompt)
        if call_count == 1:
            return AgentTurnResult(
                agent_id=req.agent_id,
                conversation_id="conv1",
                turn_id="t1",
                text="failed",
                error="Validation error: still blocked",
            )

        _set_ticket_done(ticket_path, done=True)
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id="conv1",
            turn_id="t2",
            text="done",
        )

    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        ),
        agent_pool=FakeAgentPool(handler),
    )

    first = await runner.step({})
    assert first.status == "paused"
    assert first.state.get("reply_seq") is None
    assert "Please unblock this" in prompts[0]
    assert (run_dir / "reply_history" / "0001" / "USER_REPLY.md").exists()

    second = await runner.step(first.state)
    assert second.status == "continue"
    assert second.state.get("reply_seq") == 1
    assert "Please unblock this" in prompts[1]
    assert call_count == 2


@pytest.mark.asyncio
async def test_ticket_runner_pauses_after_two_no_diff_turns(tmp_path: Path) -> None:
    workspace_root = tmp_path
    _init_git_repo(workspace_root)
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, done=False)

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        # Intentionally no file changes.
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id=req.conversation_id or "conv1",
            turn_id="t1",
            text="still blocked",
        )

    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        ),
        agent_pool=FakeAgentPool(handler),
    )

    first = await runner.step({})
    assert first.status == "continue"
    assert first.state.get("loop_guard", {}).get("no_change_count") == 1

    second = await runner.step(first.state)
    assert second.status == "paused"
    assert second.state.get("reason_code") == "loop_no_diff"
    assert second.dispatch is not None
    assert second.dispatch.dispatch.mode == "pause"


@pytest.mark.asyncio
async def test_ticket_runner_requires_commit_before_advancing_done_ticket(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path
    _init_git_repo(workspace_root)
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, done=False)

    call_count = 0

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            _set_ticket_done(ticket_path, done=True)
            (workspace_root / "work.txt").write_text("dirty\n", encoding="utf-8")
            return AgentTurnResult(
                agent_id=req.agent_id,
                conversation_id="conv1",
                turn_id="t1",
                text="done but dirty",
            )

        assert "<CAR_COMMIT_REQUIRED>" in req.prompt
        subprocess.run(
            ["git", "add", "-A"], cwd=workspace_root, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "finish ticket"],
            cwd=workspace_root,
            check=True,
            capture_output=True,
        )
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id="conv1",
            turn_id="t2",
            text="committed",
        )

    pool = FakeAgentPool(handler)
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    first = await runner.step({})
    assert first.status == "continue"
    assert isinstance(first.state.get("commit"), dict)
    assert first.state["commit"]["pending"] is True
    assert (
        first.state.get("current_ticket") == ".codex-autorunner/tickets/TICKET-001.md"
    )

    second = await runner.step(first.state)
    assert second.status == "continue"
    assert second.state.get("commit") is None
    assert second.state.get("current_ticket") is None
    assert len(pool.requests) == 2
    assert "<CAR_COMMIT_REQUIRED>" in pool.requests[1].prompt


@pytest.mark.asyncio
async def test_ticket_runner_pauses_when_commit_retries_exhausted(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path
    _init_git_repo(workspace_root)
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, done=False)

    call_count = 0

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            _set_ticket_done(ticket_path, done=True)
            (workspace_root / "work.txt").write_text("dirty\n", encoding="utf-8")
        else:
            assert "<CAR_COMMIT_REQUIRED>" in req.prompt
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id="conv1",
            turn_id=f"t{call_count}",
            text="still dirty",
        )

    pool = FakeAgentPool(handler)
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            max_commit_retries=1,
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    first = await runner.step({})
    assert first.status == "continue"
    assert first.state.get("commit", {}).get("pending") is True

    second = await runner.step(first.state)
    assert second.status == "paused"
    assert second.reason == "Commit failed after 1 attempts. Manual commit required."
    assert "Working tree status" in (second.reason_details or "")
    assert second.state.get("commit", {}).get("retries") == 1
    assert len(pool.requests) == 2


@pytest.mark.asyncio
async def test_ticket_runner_preserves_commit_state_on_commit_turn_network_retry(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path
    _init_git_repo(workspace_root)
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, done=False)

    call_count = 0

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            _set_ticket_done(ticket_path, done=True)
            (workspace_root / "work.txt").write_text("dirty\n", encoding="utf-8")
            return AgentTurnResult(
                agent_id=req.agent_id,
                conversation_id="conv1",
                turn_id="t1",
                text="done but dirty",
            )

        assert "<CAR_COMMIT_REQUIRED>" in req.prompt
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id="conv1",
            turn_id="t2",
            text="network retry",
            error="Network error: connection timeout",
        )

    pool = FakeAgentPool(handler)
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            max_network_retries=2,
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    first = await runner.step({})
    assert first.status == "continue"
    assert first.state.get("commit", {}).get("pending") is True

    second = await runner.step(first.state)
    assert second.status == "continue"
    assert second.state.get("network_retry", {}).get("retries") == 1
    assert second.state.get("commit", {}).get("pending") is True
    assert second.state.get("commit", {}).get("retries") == 0


@pytest.mark.asyncio
async def test_ticket_runner_bounds_failed_commit_resolution_turns(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path
    _init_git_repo(workspace_root)
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, done=False)

    call_count = 0

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            _set_ticket_done(ticket_path, done=True)
            (workspace_root / "work.txt").write_text("dirty\n", encoding="utf-8")
            return AgentTurnResult(
                agent_id=req.agent_id,
                conversation_id="conv1",
                turn_id="t1",
                text="done but dirty",
            )

        assert "<CAR_COMMIT_REQUIRED>" in req.prompt
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id="conv1",
            turn_id=f"t{call_count}",
            text="commit failed",
            error="OSError: [Errno 28] No space left on device",
        )

    pool = FakeAgentPool(handler)
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            max_commit_retries=2,
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    first = await runner.step({})
    assert first.status == "continue"
    assert first.state.get("commit", {}).get("pending") is True

    second = await runner.step(first.state)
    assert second.status == "continue"
    assert second.reason == "Commit attempt failed; retrying agent commit resolution."
    assert second.state.get("commit", {}).get("retries") == 1

    third = await runner.step(second.state)
    assert third.status == "paused"
    assert third.reason == "Commit failed after 2 attempts. Manual commit required."
    assert "No space left on device" in (third.reason_details or "")
    assert "Working tree status" in (third.reason_details or "")
    assert third.state.get("commit", {}).get("retries") == 2
    assert len(pool.requests) == 3


@pytest.mark.asyncio
async def test_ticket_runner_clears_commit_gate_when_ticket_reopens(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path
    _init_git_repo(workspace_root)
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, done=False)

    prompts: list[str] = []

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        prompts.append(req.prompt)
        _set_ticket_done(ticket_path, done=True)
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id="conv1",
            turn_id="t1",
            text="normal execution resumed",
        )

    _set_ticket_done(ticket_path, done=False)
    (workspace_root / "work.txt").write_text("dirty\n", encoding="utf-8")

    pool = FakeAgentPool(handler)
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    seeded_state = {
        "current_ticket": ".codex-autorunner/tickets/TICKET-001.md",
        "current_ticket_id": "ticket-1",
        "commit": {
            "pending": True,
            "retries": 1,
            "status_porcelain": " M work.txt",
        },
    }

    result = await runner.step(seeded_state)
    assert result.status == "continue"
    assert len(prompts) == 1
    assert "<CAR_COMMIT_REQUIRED>" not in prompts[0]
    assert result.state.get("commit", {}).get("pending") is True
    assert result.state.get("commit", {}).get("retries") == 0


@pytest.mark.asyncio
async def test_dispatch_notify_mode_does_not_pause(tmp_path: Path) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, done=False)

    runs_dir = Path(".codex-autorunner/runs")
    run_id = "run-1"
    run_dir = workspace_root / runs_dir / run_id
    dispatch_dir = run_dir / "dispatch"
    dispatch_path = run_dir / "DISPATCH.md"

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        dispatch_dir.mkdir(parents=True, exist_ok=True)
        dispatch_path.write_text(
            "---\nmode: notify\n---\n\nJust a heads up.\n", encoding="utf-8"
        )
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id=req.conversation_id or "conv1",
            turn_id="t1",
            text="wrote notify dispatch",
        )

    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id=run_id,
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        ),
        agent_pool=FakeAgentPool(handler),
    )

    r1 = await runner.step({})
    assert r1.status == "continue"
    assert r1.dispatch is not None
    assert r1.dispatch.dispatch.mode == "notify"
    assert (run_dir / "dispatch_history" / "0001" / "DISPATCH.md").exists()


@pytest.mark.asyncio
async def test_runner_clears_stale_pause_state_on_resume(tmp_path: Path) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, done=False)

    pool = FakeAgentPool(
        lambda req: AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id=req.conversation_id or "conv",
            turn_id="t1",
            text="ok",
        )
    )
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    stale_state = {
        "status": "paused",
        "reason": "old reason",
        "reason_details": "old details",
        "reason_code": "loop_no_diff",
        "pause_context": {"paused_reply_seq": 2},
    }

    result = await runner.step(stale_state)
    assert result.status == "continue"
    assert result.state.get("status") != "paused"
    assert result.state.get("reason") is None
    assert result.state.get("reason_details") is None
    assert result.state.get("reason_code") is None
    assert result.state.get("pause_context") is None


@pytest.mark.asyncio
async def test_runner_clears_transient_reason_from_previous_cycle(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, done=False)

    pool = FakeAgentPool(
        lambda req: AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id=req.conversation_id or "conv",
            turn_id="t1",
            text="ok",
        )
    )
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    result = await runner.step({"reason": "stale reason from prior cycle"})
    assert result.status == "continue"
    assert result.state.get("reason") is None


@pytest.mark.asyncio
async def test_completion_clears_per_ticket_state_keys(tmp_path: Path) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, done=False)

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        _set_ticket_done(ticket_path, done=True)
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id="conv1",
            turn_id="t1",
            text="done",
        )

    pool = FakeAgentPool(handler)
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    r1 = await runner.step({})
    assert r1.status == "continue"
    assert r1.state.get("current_ticket") is None
    assert r1.state.get("current_ticket_id") is None
    assert r1.state.get("ticket_turns") is None
    assert r1.state.get("last_agent_output") is None
    assert r1.state.get("lint") is None
    assert r1.state.get("commit") is None
    assert r1.state.get("loop_guard") is not None


@pytest.mark.asyncio
async def test_loop_guard_pause_context_includes_reply_seq(tmp_path: Path) -> None:
    workspace_root = tmp_path
    _init_git_repo(workspace_root)
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, done=False)

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id=req.conversation_id or "conv1",
            turn_id="t1",
            text="no change",
        )

    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        ),
        agent_pool=FakeAgentPool(handler),
    )

    first = await runner.step({"reply_seq": 5})
    assert first.status == "continue"
    second = await runner.step(first.state)
    assert second.status == "paused"
    assert second.state.get("reason_code") == "loop_no_diff"
    pause_ctx = second.state.get("pause_context", {})
    assert isinstance(pause_ctx, dict)
    assert "paused_reply_seq" in pause_ctx
    assert isinstance(pause_ctx.get("repo_fingerprint"), str)


@pytest.mark.asyncio
async def test_dispatch_seq_increments_across_turns(tmp_path: Path) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, done=False)

    runs_dir = Path(".codex-autorunner/runs")
    run_id = "run-1"
    run_dir = workspace_root / runs_dir / run_id
    dispatch_dir = run_dir / "dispatch"
    dispatch_path = run_dir / "DISPATCH.md"

    call_count = 0

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            dispatch_dir.mkdir(parents=True, exist_ok=True)
            dispatch_path.write_text(
                f"---\nmode: notify\n---\n\nTurn {call_count}\n", encoding="utf-8"
            )
        if call_count == 2:
            _set_ticket_done(ticket_path, done=True)
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id=req.conversation_id or "conv1",
            turn_id=f"t{call_count}",
            text=f"turn {call_count}",
        )

    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id=run_id,
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        ),
        agent_pool=FakeAgentPool(handler),
    )

    r1 = await runner.step({})
    assert r1.status == "continue"
    first_seq = r1.state.get("dispatch_seq")

    r2 = await runner.step(r1.state)
    assert r2.status == "continue"
    second_seq = r2.state.get("dispatch_seq")

    assert second_seq > first_seq
    assert (run_dir / "dispatch_history").exists()


@pytest.mark.asyncio
async def test_runner_paused_reason_codes_are_bounded(tmp_path: Path) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)

    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        ),
        agent_pool=FakeAgentPool(
            lambda req: AgentTurnResult(
                agent_id="codex", conversation_id="c", turn_id="t", text="x"
            )
        ),
    )

    result = runner._pause(
        {},
        reason="test reason code",
        reason_code="custom_code",
    )
    assert result.state["reason_code"] == "custom_code"
    assert isinstance(result.state["reason_code"], str)


@pytest.mark.asyncio
async def test_runner_outbox_lint_errors_cause_pause(tmp_path: Path) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, done=False)

    runs_dir = Path(".codex-autorunner/runs")
    run_id = "run-1"
    run_dir = workspace_root / runs_dir / run_id
    dispatch_dir = run_dir / "dispatch"
    dispatch_path = run_dir / "DISPATCH.md"

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        dispatch_dir.mkdir(parents=True, exist_ok=True)
        dispatch_path.write_text(
            "---\nmode: bad_mode_value\n---\n\nInvalid mode\n", encoding="utf-8"
        )
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id=req.conversation_id or "conv1",
            turn_id="t1",
            text="wrote bad dispatch",
        )

    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id=run_id,
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        ),
        agent_pool=FakeAgentPool(handler),
    )

    result = await runner.step({})
    assert result.status == "paused"
    assert "Invalid DISPATCH.md frontmatter" in (result.reason or "")
    assert result.state.get("outbox_lint") is not None
    assert isinstance(result.state.get("outbox_lint"), list)


@pytest.mark.asyncio
async def test_ticket_runner_pauses_for_user_agent_ticket(tmp_path: Path) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, agent="user", done=False)

    pool = FakeAgentPool(
        lambda req: AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id=req.conversation_id or "conv",
            turn_id="t1",
            text="should not be called",
        )
    )
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    result = await runner.step({})
    assert result.status == "paused"
    assert len(pool.requests) == 0


@pytest.mark.asyncio
async def test_ticket_runner_skips_done_user_agent_ticket(tmp_path: Path) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    user_ticket = ticket_dir / "TICKET-001.md"
    _write_ticket(user_ticket, agent="user", done=True)
    codex_ticket = ticket_dir / "TICKET-002.md"
    _write_ticket(codex_ticket, agent="codex", done=False)

    pool = FakeAgentPool(
        lambda req: AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id=req.conversation_id or "conv",
            turn_id="t1",
            text="codex turn",
        )
    )
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    result = await runner.step({})
    assert result.status == "continue"
    assert len(pool.requests) == 1
    assert pool.requests[0].agent_id == "codex"


@pytest.mark.asyncio
async def test_ticket_runner_respects_ascending_numeric_order(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_3 = ticket_dir / "TICKET-003.md"
    _write_ticket(ticket_3, agent="codex", done=False)
    ticket_1 = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_1, agent="codex", done=False)
    ticket_2 = ticket_dir / "TICKET-002.md"
    _write_ticket(ticket_2, agent="codex", done=False)

    pool = FakeAgentPool(
        lambda req: AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id="conv",
            turn_id="t1",
            text="done",
        )
    )
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    result = await runner.step({})
    assert result.status == "continue"
    assert result.state.get("current_ticket") == (
        ".codex-autorunner/tickets/TICKET-001.md"
    )


@pytest.mark.asyncio
async def test_ticket_runner_turn_counter_increments(tmp_path: Path) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, done=False)

    pool = FakeAgentPool(
        lambda req: AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id="conv",
            turn_id="t1",
            text="working",
        )
    )
    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id="run-1",
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        ),
        agent_pool=pool,
    )

    r1 = await runner.step({})
    assert r1.state.get("ticket_turns") == 1
    assert r1.state.get("total_turns") == 1

    r2 = await runner.step(r1.state)
    assert r2.state.get("ticket_turns") == 2
    assert r2.state.get("total_turns") == 2


@pytest.mark.asyncio
async def test_ticket_runner_dispatch_seq_advances_on_notify_and_pause(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path
    ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = ticket_dir / "TICKET-001.md"
    _write_ticket(ticket_path, done=False)

    run_id = "run-1"
    run_dir = workspace_root / ".codex-autorunner" / "runs" / run_id
    dispatch_dir = run_dir / "dispatch"
    dispatch_path = run_dir / "DISPATCH.md"

    call_count = 0

    def handler(req: AgentTurnRequest) -> AgentTurnResult:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            dispatch_dir.mkdir(parents=True, exist_ok=True)
            dispatch_path.write_text(
                "---\nmode: notify\n---\n\nNotify turn\n", encoding="utf-8"
            )
        elif call_count == 2:
            dispatch_dir.mkdir(parents=True, exist_ok=True)
            dispatch_path.write_text(
                "---\nmode: pause\n---\n\nPause turn\n", encoding="utf-8"
            )
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id="conv",
            turn_id=f"t{call_count}",
            text=f"turn {call_count}",
        )

    runner = TicketRunner(
        workspace_root=workspace_root,
        run_id=run_id,
        config=TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        ),
        agent_pool=FakeAgentPool(handler),
    )

    r1 = await runner.step({})
    assert r1.status == "continue"
    seq_after_notify = r1.state.get("dispatch_seq")

    r2 = await runner.step(r1.state)
    assert r2.status == "paused"
    seq_after_pause = r2.state.get("dispatch_seq")

    assert seq_after_pause > seq_after_notify
