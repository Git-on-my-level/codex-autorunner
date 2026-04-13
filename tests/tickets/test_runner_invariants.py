from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codex_autorunner.tickets.agent_pool import AgentTurnRequest, AgentTurnResult
from codex_autorunner.tickets.files import read_ticket
from codex_autorunner.tickets.models import TicketContextEntry, TicketRunConfig
from codex_autorunner.tickets.runner import (
    TICKET_CONTEXT_TOTAL_MAX_BYTES,
    TicketRunner,
    load_ticket_context_block,
)
from codex_autorunner.tickets.runner_execution import (
    LOOP_NO_CHANGE_THRESHOLD,
    compute_loop_guard,
    should_pause_for_loop,
)
from codex_autorunner.tickets.runner_prompt import build_prompt as runner_prompt_build
from codex_autorunner.tickets.runner_selection import build_reply_context


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


class FakeAgentPool:
    def __init__(self, handler):
        self._handler = handler
        self.requests: list[AgentTurnRequest] = []

    async def run_turn(self, req: AgentTurnRequest) -> AgentTurnResult:
        self.requests.append(req)
        return self._handler(req)


def _prompts_match_or_known_drift(legacy: str, module: str) -> bool:
    if legacy == module:
        return True
    normalized_legacy = legacy.replace("\u201c", '"').replace("\u201d", '"')
    normalized_module = module.replace("\u201c", '"').replace("\u201d", '"')
    return normalized_legacy == normalized_module


class TestLoadTicketContextBlock:
    def test_empty_entries_returns_empty(self, tmp_path: Path) -> None:
        rendered, missing = load_ticket_context_block(
            workspace_root=tmp_path,
            entries=(),
        )
        assert rendered == ""
        assert missing == []

    def test_missing_required_reported(self, tmp_path: Path) -> None:
        entries = (TicketContextEntry(path="docs/absent.md", required=True),)
        rendered, missing = load_ticket_context_block(
            workspace_root=tmp_path,
            entries=entries,
        )
        assert "docs/absent.md" in missing
        assert "status: missing" in rendered

    def test_missing_optional_not_reported(self, tmp_path: Path) -> None:
        entries = (TicketContextEntry(path="docs/optional.md", required=False),)
        rendered, missing = load_ticket_context_block(
            workspace_root=tmp_path,
            entries=entries,
        )
        assert missing == []
        assert "status: missing" in rendered

    def test_existing_file_included(self, tmp_path: Path) -> None:
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "notes.md").write_text("Hello world", encoding="utf-8")
        entries = (TicketContextEntry(path="docs/notes.md", required=True),)
        rendered, missing = load_ticket_context_block(
            workspace_root=tmp_path,
            entries=entries,
        )
        assert missing == []
        assert "Hello world" in rendered
        assert "status: included" in rendered

    def test_directory_instead_of_file_treated_as_missing(self, tmp_path: Path) -> None:
        (tmp_path / "docs_dir").mkdir()
        entries = (TicketContextEntry(path="docs_dir", required=True),)
        rendered, missing = load_ticket_context_block(
            workspace_root=tmp_path,
            entries=entries,
        )
        assert "docs_dir" in missing
        assert "status: not_a_file" in rendered

    def test_total_bytes_budget_respected(self, tmp_path: Path) -> None:
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "a.txt").write_text("A" * 20000, encoding="utf-8")
        (docs / "b.txt").write_text("B" * 20000, encoding="utf-8")
        entries = (
            TicketContextEntry(path="docs/a.txt"),
            TicketContextEntry(path="docs/b.txt"),
        )
        rendered, missing = load_ticket_context_block(
            workspace_root=tmp_path,
            entries=entries,
        )
        assert len(rendered.encode("utf-8")) <= TICKET_CONTEXT_TOTAL_MAX_BYTES

    def test_per_entry_max_bytes_overrides_default(self, tmp_path: Path) -> None:
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "big.txt").write_text("X" * 50000, encoding="utf-8")
        entries = (TicketContextEntry(path="docs/big.txt", max_bytes=500),)
        rendered, missing = load_ticket_context_block(
            workspace_root=tmp_path,
            entries=entries,
        )
        ctx_start = rendered.find("CONTENT:\n")
        if ctx_start != -1:
            content_section = rendered[ctx_start + len("CONTENT:\n") :]
            end = content_section.find("</CAR_CONTEXT_ENTRY>")
            if end != -1:
                content_text = content_section[:end].strip()
                assert len(content_text.encode("utf-8")) <= 500

    def test_budget_exhausted_skips_later_entries(self, tmp_path: Path) -> None:
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "first.txt").write_text(
            "A" * (TICKET_CONTEXT_TOTAL_MAX_BYTES - 100), encoding="utf-8"
        )
        (docs / "second.txt").write_text("B" * 1000, encoding="utf-8")
        entries = (
            TicketContextEntry(path="docs/first.txt"),
            TicketContextEntry(path="docs/second.txt"),
        )
        rendered, missing = load_ticket_context_block(
            workspace_root=tmp_path,
            entries=entries,
        )
        assert len(rendered.encode("utf-8")) <= TICKET_CONTEXT_TOTAL_MAX_BYTES
        assert "budget_exhausted" in rendered or "TRUNCATED" in rendered

    def test_read_error_for_unreadable_file(self, tmp_path: Path) -> None:
        docs = tmp_path / "docs"
        docs.mkdir()
        bad_file = docs / "bad.txt"
        bad_file.write_bytes(b"\xff\xfe\x00")
        entries = (TicketContextEntry(path="docs/bad.txt", required=False),)
        rendered, missing = load_ticket_context_block(
            workspace_root=tmp_path,
            entries=entries,
        )
        assert missing == []
        assert "read_error" in rendered

    def test_read_error_for_required_file_reported(self, tmp_path: Path) -> None:
        docs = tmp_path / "docs"
        docs.mkdir()
        bad_file = docs / "bad.txt"
        bad_file.write_bytes(b"\xff\xfe\x00")
        entries = (TicketContextEntry(path="docs/bad.txt", required=True),)
        rendered, missing = load_ticket_context_block(
            workspace_root=tmp_path,
            entries=entries,
        )
        assert "docs/bad.txt" in missing
        assert "read_error" in rendered


class TestPromptDuplicationParity:
    def test_build_prompt_and_legacy_produce_same_xml_structure(
        self, tmp_path: Path
    ) -> None:
        workspace_root = tmp_path
        ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
        ticket_dir.mkdir(parents=True, exist_ok=True)
        ticket_path = ticket_dir / "TICKET-001.md"
        _write_ticket(ticket_path)

        outbox_paths = MagicMock()
        outbox_paths.dispatch_dir = (
            workspace_root / ".codex-autorunner" / "runs" / "run-1" / "dispatch"
        )
        outbox_paths.dispatch_path = (
            workspace_root / ".codex-autorunner" / "runs" / "run-1" / "DISPATCH.md"
        )

        ticket_doc, _ = read_ticket(ticket_path)

        config = TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        )
        runner = TicketRunner(
            workspace_root=workspace_root,
            run_id="run-1",
            config=config,
            agent_pool=MagicMock(),
        )

        legacy_prompt = runner._build_prompt(
            ticket_path=ticket_path,
            ticket_doc=ticket_doc,
            last_agent_output="previous output",
            last_checkpoint_error=None,
            commit_required=False,
            commit_attempt=0,
            commit_max_attempts=2,
            outbox_paths=outbox_paths,
            lint_errors=None,
            reply_context=None,
            requested_context=None,
            previous_ticket_content=None,
            prior_no_change_turns=0,
        )

        module_prompt = runner_prompt_build(
            ticket_path=ticket_path,
            workspace_root=workspace_root,
            ticket_doc=ticket_doc,
            last_agent_output="previous output",
            last_checkpoint_error=None,
            commit_required=False,
            commit_attempt=0,
            commit_max_attempts=2,
            outbox_paths=outbox_paths,
            lint_errors=None,
            reply_context=None,
            requested_context=None,
            previous_ticket_content=None,
            prior_no_change_turns=0,
        )

        xml_tags = [
            "<CAR_TICKET_FLOW_PROMPT>",
            "</CAR_TICKET_FLOW_PROMPT>",
            "<CAR_TICKET_FLOW_INSTRUCTIONS>",
            "</CAR_TICKET_FLOW_INSTRUCTIONS>",
            "<CAR_RUNTIME_PATHS>",
            "</CAR_RUNTIME_PATHS>",
            "<CAR_HUD>",
            "</CAR_HUD>",
            "<CAR_REQUESTED_CONTEXT>",
            "</CAR_REQUESTED_CONTEXT>",
            "<CAR_WORKSPACE_DOCS>",
            "</CAR_WORKSPACE_DOCS>",
            "<CAR_HUMAN_REPLIES>",
            "</CAR_HUMAN_REPLIES>",
            "<CAR_PREVIOUS_TICKET_REFERENCE>",
            "</CAR_PREVIOUS_TICKET_REFERENCE>",
            "<CAR_CURRENT_TICKET_FILE>",
            "</CAR_CURRENT_TICKET_FILE>",
            "<TICKET_MARKDOWN>",
            "</TICKET_MARKDOWN>",
            "<CAR_PREVIOUS_AGENT_OUTPUT>",
            "</CAR_PREVIOUS_AGENT_OUTPUT>",
        ]
        for tag in xml_tags:
            assert tag in legacy_prompt, f"Legacy prompt missing {tag}"
            assert tag in module_prompt, f"Module prompt missing {tag}"

        assert legacy_prompt == module_prompt or _prompts_match_or_known_drift(
            legacy_prompt, module_prompt
        ), (
            "Prompt implementations diverged beyond known quote drift. "
            "If this is intentional, update the characterization."
        )

    def test_build_prompt_with_lint_errors_parity(self, tmp_path: Path) -> None:
        workspace_root = tmp_path
        ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
        ticket_dir.mkdir(parents=True, exist_ok=True)
        ticket_path = ticket_dir / "TICKET-001.md"
        _write_ticket(ticket_path)

        outbox_paths = MagicMock()
        outbox_paths.dispatch_dir = (
            workspace_root / ".codex-autorunner" / "runs" / "run-1" / "dispatch"
        )
        outbox_paths.dispatch_path = (
            workspace_root / ".codex-autorunner" / "runs" / "run-1" / "DISPATCH.md"
        )

        ticket_doc, _ = read_ticket(ticket_path)

        config = TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        )
        runner = TicketRunner(
            workspace_root=workspace_root,
            run_id="run-1",
            config=config,
            agent_pool=MagicMock(),
        )

        lint_errors = ["done must be a boolean", "agent is required"]
        legacy = runner._build_prompt(
            ticket_path=ticket_path,
            ticket_doc=ticket_doc,
            last_agent_output=None,
            outbox_paths=outbox_paths,
            lint_errors=lint_errors,
        )
        module = runner_prompt_build(
            ticket_path=ticket_path,
            workspace_root=workspace_root,
            ticket_doc=ticket_doc,
            last_agent_output=None,
            outbox_paths=outbox_paths,
            lint_errors=lint_errors,
        )

        assert "<CAR_TICKET_FRONTMATTER_LINT_REPAIR>" in legacy
        assert "<CAR_TICKET_FRONTMATTER_LINT_REPAIR>" in module
        assert _prompts_match_or_known_drift(legacy, module)

    def test_build_prompt_with_commit_required_parity(self, tmp_path: Path) -> None:
        workspace_root = tmp_path
        ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
        ticket_dir.mkdir(parents=True, exist_ok=True)
        ticket_path = ticket_dir / "TICKET-001.md"
        _write_ticket(ticket_path)

        outbox_paths = MagicMock()
        outbox_paths.dispatch_dir = (
            workspace_root / ".codex-autorunner" / "runs" / "run-1" / "dispatch"
        )
        outbox_paths.dispatch_path = (
            workspace_root / ".codex-autorunner" / "runs" / "run-1" / "DISPATCH.md"
        )

        ticket_doc, _ = read_ticket(ticket_path)

        config = TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        )
        runner = TicketRunner(
            workspace_root=workspace_root,
            run_id="run-1",
            config=config,
            agent_pool=MagicMock(),
        )

        legacy = runner._build_prompt(
            ticket_path=ticket_path,
            ticket_doc=ticket_doc,
            last_agent_output=None,
            outbox_paths=outbox_paths,
            lint_errors=None,
            commit_required=True,
            commit_attempt=1,
            commit_max_attempts=2,
        )
        module = runner_prompt_build(
            ticket_path=ticket_path,
            workspace_root=workspace_root,
            ticket_doc=ticket_doc,
            last_agent_output=None,
            outbox_paths=outbox_paths,
            lint_errors=None,
            commit_required=True,
            commit_attempt=1,
            commit_max_attempts=2,
        )

        assert "<CAR_COMMIT_REQUIRED>" in legacy
        assert "<CAR_COMMIT_REQUIRED>" in module
        assert "Attempts remaining before user intervention: 2" in legacy
        assert "Attempts remaining before user intervention: 2" in module
        assert _prompts_match_or_known_drift(legacy, module)

    def test_build_prompt_with_loop_guard_parity(self, tmp_path: Path) -> None:
        workspace_root = tmp_path
        ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
        ticket_dir.mkdir(parents=True, exist_ok=True)
        ticket_path = ticket_dir / "TICKET-001.md"
        _write_ticket(ticket_path)

        outbox_paths = MagicMock()
        outbox_paths.dispatch_dir = (
            workspace_root / ".codex-autorunner" / "runs" / "run-1" / "dispatch"
        )
        outbox_paths.dispatch_path = (
            workspace_root / ".codex-autorunner" / "runs" / "run-1" / "DISPATCH.md"
        )

        ticket_doc, _ = read_ticket(ticket_path)

        config = TicketRunConfig(
            ticket_dir=Path(".codex-autorunner/tickets"),
            auto_commit=False,
        )
        runner = TicketRunner(
            workspace_root=workspace_root,
            run_id="run-1",
            config=config,
            agent_pool=MagicMock(),
        )

        legacy = runner._build_prompt(
            ticket_path=ticket_path,
            ticket_doc=ticket_doc,
            last_agent_output=None,
            outbox_paths=outbox_paths,
            lint_errors=None,
            prior_no_change_turns=1,
        )
        module = runner_prompt_build(
            ticket_path=ticket_path,
            workspace_root=workspace_root,
            ticket_doc=ticket_doc,
            last_agent_output=None,
            outbox_paths=outbox_paths,
            lint_errors=None,
            prior_no_change_turns=1,
        )

        assert "<CAR_LOOP_GUARD>" in legacy
        assert "<CAR_LOOP_GUARD>" in module
        assert "Consecutive no-change turns so far: 1" in legacy
        assert "Consecutive no-change turns so far: 1" in module
        assert _prompts_match_or_known_drift(legacy, module)


class TestLoopGuardInvariants:
    def test_compute_loop_guard_increments_on_same_ticket_no_change(self) -> None:
        state = {
            "loop_guard": {
                "ticket": "tkt-001",
                "no_change_count": 1,
            }
        }
        result = compute_loop_guard(
            state=state,
            current_ticket_id="tkt-001",
            repo_fingerprint_before="abc123\n",
            repo_fingerprint_after="abc123\n",
            lint_retry_mode=False,
        )
        guard = result["loop_guard"]
        assert guard is not None
        assert guard["no_change_count"] == 2

    def test_compute_loop_guard_resets_on_repo_change(self) -> None:
        state = {
            "loop_guard": {
                "ticket": "tkt-001",
                "no_change_count": 5,
            }
        }
        result = compute_loop_guard(
            state=state,
            current_ticket_id="tkt-001",
            repo_fingerprint_before="abc123\n",
            repo_fingerprint_after="def456\n",
            lint_retry_mode=False,
        )
        guard = result["loop_guard"]
        assert guard["no_change_count"] == 0

    def test_compute_loop_guard_resets_on_ticket_change(self) -> None:
        state = {
            "loop_guard": {
                "ticket": "tkt-001",
                "no_change_count": 3,
            }
        }
        result = compute_loop_guard(
            state=state,
            current_ticket_id="tkt-002",
            repo_fingerprint_before="abc123\n",
            repo_fingerprint_after="abc123\n",
            lint_retry_mode=False,
        )
        guard = result["loop_guard"]
        assert guard["ticket"] == "tkt-002"
        assert guard["no_change_count"] == 1

    def test_compute_loop_guard_skipped_in_lint_retry_mode(self) -> None:
        state = {
            "loop_guard": {
                "ticket": "tkt-001",
                "no_change_count": 10,
            }
        }
        result = compute_loop_guard(
            state=state,
            current_ticket_id="tkt-001",
            repo_fingerprint_before="abc123\n",
            repo_fingerprint_after="abc123\n",
            lint_retry_mode=True,
        )
        assert result["loop_guard"] is None

    def test_should_pause_for_loop_at_threshold(self) -> None:
        assert (
            should_pause_for_loop(
                loop_guard_updates={"no_change_count": LOOP_NO_CHANGE_THRESHOLD}
            )
            is True
        )

    def test_should_pause_for_loop_below_threshold(self) -> None:
        assert (
            should_pause_for_loop(
                loop_guard_updates={"no_change_count": LOOP_NO_CHANGE_THRESHOLD - 1}
            )
            is False
        )

    def test_should_pause_for_loop_zero(self) -> None:
        assert should_pause_for_loop(loop_guard_updates={"no_change_count": 0}) is False


class TestMaxTurnsInvariant:
    @pytest.mark.asyncio
    async def test_runner_pauses_at_max_total_turns(self, tmp_path: Path) -> None:
        workspace_root = tmp_path
        ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
        ticket_dir.mkdir(parents=True, exist_ok=True)
        ticket_path = ticket_dir / "TICKET-001.md"
        _write_ticket(ticket_path, done=False)

        runner = TicketRunner(
            workspace_root=workspace_root,
            run_id="run-1",
            config=TicketRunConfig(
                ticket_dir=Path(".codex-autorunner/tickets"),
                max_total_turns=3,
                auto_commit=False,
            ),
            agent_pool=FakeAgentPool(
                lambda req: AgentTurnResult(
                    agent_id=req.agent_id,
                    conversation_id="conv",
                    turn_id="t1",
                    text="ok",
                )
            ),
        )

        state = {"total_turns": 3}
        result = await runner.step(state)
        assert result.status == "paused"
        assert "Max turns reached (3)" in (result.reason or "")


class TestReplyContextOrdering:
    @pytest.mark.asyncio
    async def test_reply_context_only_includes_new_replies(
        self, tmp_path: Path
    ) -> None:
        workspace_root = tmp_path

        run_dir = workspace_root / ".codex-autorunner" / "runs" / "run-1"
        reply_history = run_dir / "reply_history"
        reply_history.mkdir(parents=True, exist_ok=True)

        seq1_dir = reply_history / "0001"
        seq1_dir.mkdir()
        (seq1_dir / "USER_REPLY.md").write_text(
            "---\n---\n\nOld reply\n", encoding="utf-8"
        )

        seq2_dir = reply_history / "0002"
        seq2_dir.mkdir()
        (seq2_dir / "USER_REPLY.md").write_text(
            "---\n---\n\nNew reply\n", encoding="utf-8"
        )

        reply_paths = MagicMock()
        reply_paths.reply_history_dir = reply_history

        rendered, max_seq = build_reply_context(
            workspace_root=workspace_root, reply_paths=reply_paths, last_seq=1
        )

        assert max_seq == 2
        assert "New reply" in rendered
        assert "Old reply" not in rendered

    @pytest.mark.asyncio
    async def test_reply_context_returns_empty_when_no_new_replies(
        self, tmp_path: Path
    ) -> None:
        workspace_root = tmp_path
        ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
        ticket_dir.mkdir(parents=True, exist_ok=True)
        ticket_path = ticket_dir / "TICKET-001.md"
        _write_ticket(ticket_path, done=False)

        run_dir = workspace_root / ".codex-autorunner" / "runs" / "run-1"
        reply_history = run_dir / "reply_history"
        reply_history.mkdir(parents=True, exist_ok=True)

        seq1_dir = reply_history / "0001"
        seq1_dir.mkdir()
        (seq1_dir / "USER_REPLY.md").write_text(
            "---\n---\n\nOld reply\n", encoding="utf-8"
        )

        reply_paths = MagicMock()
        reply_paths.reply_history_dir = reply_history

        rendered, max_seq = build_reply_context(
            workspace_root=workspace_root, reply_paths=reply_paths, last_seq=1
        )

        assert rendered == ""
        assert max_seq == 1


class TestPauseResultInvariants:
    @pytest.mark.asyncio
    async def test_pause_result_always_sets_status_paused(self, tmp_path: Path) -> None:
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
            agent_pool=MagicMock(),
        )

        result = runner._pause(
            {},
            reason="test pause",
            reason_code="test_code",
            reason_details="some details",
        )
        assert result.status == "paused"
        assert result.state["status"] == "paused"
        assert result.state["reason"] == "test pause"
        assert result.state["reason_code"] == "test_code"
        assert result.state["reason_details"] == "some details"
        assert isinstance(result.state.get("pause_context"), dict)
        assert "paused_reply_seq" in result.state["pause_context"]

    @pytest.mark.asyncio
    async def test_pause_result_clears_reason_details_when_absent(
        self, tmp_path: Path
    ) -> None:
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
            agent_pool=MagicMock(),
        )

        result = runner._pause(
            {"reason_details": "old details"},
            reason="no details pause",
            reason_code="test",
        )
        assert result.state.get("reason_details") is None

    @pytest.mark.asyncio
    async def test_pause_result_does_not_mutate_input_state(
        self, tmp_path: Path
    ) -> None:
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
            agent_pool=MagicMock(),
        )

        original_state = {"key": "value"}
        result = runner._pause(
            original_state,
            reason="test",
            reason_code="test",
        )
        assert "status" not in original_state
        assert result.state["status"] == "paused"


class TestTicketSelectionOrdering:
    @pytest.mark.asyncio
    async def test_runner_selects_first_undone_ticket_in_order(
        self, tmp_path: Path
    ) -> None:
        workspace_root = tmp_path
        ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
        ticket_dir.mkdir(parents=True, exist_ok=True)

        t1 = ticket_dir / "TICKET-001.md"
        t2 = ticket_dir / "TICKET-002.md"
        t3 = ticket_dir / "TICKET-003.md"
        _write_ticket(t1, done=False)
        _set_ticket_done(t1, done=True)
        _write_ticket(t2, done=False)
        _write_ticket(t3, done=False)

        selected: list[str] = []

        def handler(req: AgentTurnRequest) -> AgentTurnResult:
            selected.append(req.prompt)
            _set_ticket_done(t2, done=True)
            return AgentTurnResult(
                agent_id=req.agent_id,
                conversation_id="conv",
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

        result = await runner.step({})
        assert result.status == "continue"
        assert result.current_ticket == ".codex-autorunner/tickets/TICKET-002.md"
        assert "TICKET-002" in selected[0]

    @pytest.mark.asyncio
    async def test_runner_skips_done_tickets(self, tmp_path: Path) -> None:
        workspace_root = tmp_path
        ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
        ticket_dir.mkdir(parents=True, exist_ok=True)

        for i in range(1, 6):
            path = ticket_dir / f"TICKET-{i:03d}.md"
            _write_ticket(path, done=i <= 3)

        selected_paths: list[str] = []

        def handler(req: AgentTurnRequest) -> AgentTurnResult:
            selected_paths.append(req.prompt)
            return AgentTurnResult(
                agent_id=req.agent_id,
                conversation_id="conv",
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
        assert "TICKET-004" in selected_paths[0]


class TestWorkspaceDocsInPrompt:
    @pytest.mark.asyncio
    async def test_contextspace_docs_included_in_prompt(self, tmp_path: Path) -> None:
        workspace_root = tmp_path
        ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
        ticket_dir.mkdir(parents=True, exist_ok=True)
        ticket_path = ticket_dir / "TICKET-001.md"
        _write_ticket(ticket_path, done=False)

        ctx_dir = workspace_root / ".codex-autorunner" / "contextspace"
        ctx_dir.mkdir(parents=True, exist_ok=True)
        (ctx_dir / "active_context.md").write_text(
            "# Active context\n\nWorking on invariants.", encoding="utf-8"
        )

        prompts: list[str] = []

        def handler(req: AgentTurnRequest) -> AgentTurnResult:
            prompts.append(req.prompt)
            return AgentTurnResult(
                agent_id=req.agent_id,
                conversation_id="conv",
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
        assert "<CAR_WORKSPACE_DOCS>" in prompts[0]
        assert "Working on invariants." in prompts[0]

    @pytest.mark.asyncio
    async def test_missing_contextspace_docs_not_in_prompt(
        self, tmp_path: Path
    ) -> None:
        workspace_root = tmp_path
        ticket_dir = workspace_root / ".codex-autorunner" / "tickets"
        ticket_dir.mkdir(parents=True, exist_ok=True)
        ticket_path = ticket_dir / "TICKET-001.md"
        _write_ticket(ticket_path, done=False)

        prompts: list[str] = []

        def handler(req: AgentTurnRequest) -> AgentTurnResult:
            prompts.append(req.prompt)
            return AgentTurnResult(
                agent_id=req.agent_id,
                conversation_id="conv",
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
        assert "<CAR_WORKSPACE_DOCS>" in prompts[0]
        start = prompts[0].index("<CAR_WORKSPACE_DOCS>") + len("<CAR_WORKSPACE_DOCS>\n")
        end = prompts[0].index("</CAR_WORKSPACE_DOCS>")
        section = prompts[0][start:end].strip()
        assert section == ""
