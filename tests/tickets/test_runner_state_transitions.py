"""Focused tests for runner state-transition helpers.

These tests cover the extracted helpers in runner_post_turn.py and the
private methods on TicketRunner that handle failed turns, commit gating,
loop-guard pauses, and dispatch-pause assembly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest
from tests.support.git_test_helpers import init_git_repo as _init_git_repo

from codex_autorunner.tickets.agent_pool import AgentTurnResult
from codex_autorunner.tickets.models import (
    Dispatch,
    DispatchRecord,
    TicketFrontmatter,
    TicketRunConfig,
)
from codex_autorunner.tickets.runner import TicketRunner
from codex_autorunner.tickets.runner_post_turn import (
    apply_checkpoint_error_state,
    apply_completion_cleanup,
    apply_loop_guard_state,
    build_dispatch_pause_result,
    build_loop_guard_pause_result,
    build_pause_result,
    check_ticket_frontmatter,
    get_repo_fingerprint,
    handle_frontmatter_recheck,
)
from codex_autorunner.tickets.runner_types import TurnExecutionResult


class FakeAgentPool:
    def __init__(self, handler):
        self._handler = handler

    async def run_turn(self, req):
        return self._handler(req)


def _write_ticket(path, *, agent="codex", done=False, body="Do the thing"):
    import uuid

    text = (
        "---\n"
        f"ticket_id: tkt_{uuid.uuid4().hex}\n"
        f"agent: {agent}\n"
        f"done: {str(done).lower()}\n"
        "title: Test\n"
        "goal: Finish the test\n"
        "---\n\n"
        f"{body}\n"
    )
    path.write_text(text, encoding="utf-8")


class TestApplyLoopGuardState:
    def test_applies_loop_guard_from_result(self):
        state: dict[str, Any] = {}
        loop_result = {
            "loop_guard": {"ticket": "tkt-1", "no_change_count": 2},
            "loop_guard_updates": {},
        }
        result = apply_loop_guard_state(state=state, loop_guard_result=loop_result)
        assert result["loop_guard"] == {"ticket": "tkt-1", "no_change_count": 2}

    def test_clears_loop_guard_when_none(self):
        state = {"loop_guard": {"ticket": "old", "no_change_count": 1}}
        loop_result = {"loop_guard": None}
        result = apply_loop_guard_state(state=state, loop_guard_result=loop_result)
        assert result["loop_guard"] is None

    def test_no_mutation_when_key_absent(self):
        state: dict[str, Any] = {"loop_guard": "existing"}
        loop_result: dict[str, Any] = {"loop_guard_updates": {}}
        result = apply_loop_guard_state(state=state, loop_guard_result=loop_result)
        assert result["loop_guard"] == "existing"

    def test_does_not_mutate_input_state(self):
        original: dict[str, Any] = {}
        apply_loop_guard_state(
            state=original,
            loop_guard_result={"loop_guard": {"ticket": "t", "no_change_count": 0}},
        )
        assert "loop_guard" not in original


class TestApplyCompletionCleanup:
    def test_clears_per_ticket_state_when_done(self):
        fm = TicketFrontmatter(ticket_id="t1", agent="codex", done=True)
        state = {
            "commit": {"pending": True},
            "current_ticket": "path.md",
            "current_ticket_id": "t1",
            "ticket_turns": 3,
            "last_agent_output": "output",
            "lint": {"errors": ["x"]},
        }
        result = apply_completion_cleanup(state=state, updated_fm=fm)
        assert "commit" not in result
        assert "current_ticket" not in result
        assert "current_ticket_id" not in result
        assert "ticket_turns" not in result
        assert "last_agent_output" not in result
        assert "lint" not in result

    def test_clears_only_commit_when_not_done(self):
        fm = TicketFrontmatter(ticket_id="t1", agent="codex", done=False)
        state = {
            "commit": {"pending": True},
            "current_ticket": "path.md",
            "ticket_turns": 3,
        }
        result = apply_completion_cleanup(state=state, updated_fm=fm)
        assert "commit" not in result
        assert result["current_ticket"] == "path.md"
        assert result["ticket_turns"] == 3

    def test_handles_none_updated_fm(self):
        state = {"commit": {"pending": True}, "current_ticket": "path.md"}
        result = apply_completion_cleanup(state=state, updated_fm=None)
        assert "commit" not in result
        assert result["current_ticket"] == "path.md"

    def test_does_not_mutate_input_state(self):
        fm = TicketFrontmatter(ticket_id="t1", agent="codex", done=True)
        original = {"commit": {"pending": True}, "current_ticket": "p.md"}
        apply_completion_cleanup(state=original, updated_fm=fm)
        assert "commit" in original


class TestApplyCheckpointErrorState:
    def test_records_error_when_present(self):
        state: dict[str, Any] = {}
        result = apply_checkpoint_error_state(state=state, checkpoint_error="boom")
        assert result["last_checkpoint_error"] == "boom"

    def test_clears_error_when_none(self):
        state = {"last_checkpoint_error": "old"}
        result = apply_checkpoint_error_state(state=state, checkpoint_error=None)
        assert "last_checkpoint_error" not in result

    def test_clears_error_when_empty_string(self):
        state = {"last_checkpoint_error": "old"}
        result = apply_checkpoint_error_state(state=state, checkpoint_error="")
        assert "last_checkpoint_error" not in result

    def test_does_not_mutate_input_state(self):
        original: dict[str, Any] = {}
        apply_checkpoint_error_state(state=original, checkpoint_error="err")
        assert "last_checkpoint_error" not in original


class TestBuildLoopGuardPauseResult:
    def test_builds_paused_result_with_correct_fields(self, tmp_path):
        state: dict[str, Any] = {}
        dispatch = _make_dispatch_record(tmp_path)
        result = build_loop_guard_pause_result(
            state=state,
            current_ticket_path="tickets/TICKET-001.md",
            loop_guard_updates={"no_change_count": 2},
            dispatch_record=dispatch,
            agent_text="agent output",
            agent_id="codex",
            agent_conversation_id="conv-1",
            agent_turn_id="turn-1",
            workspace_root=tmp_path,
        )
        assert result.status == "paused"
        assert result.agent_output == "agent output"
        assert result.agent_id == "codex"
        assert result.agent_conversation_id == "conv-1"
        assert result.agent_turn_id == "turn-1"
        assert result.dispatch is dispatch
        assert result.state["status"] == "paused"
        assert result.state["reason_code"] == "loop_no_diff"
        assert "loop_no_diff" not in (result.reason or "")
        assert "stuck" in (result.reason or "").lower()
        assert "Consecutive no-change turns: 2" in (result.reason_details or "")


class TestBuildDispatchPauseResult:
    def test_builds_paused_result_from_dispatch(self, tmp_path):
        state: dict[str, Any] = {}
        dispatch = _make_dispatch_record(tmp_path, title="Review needed")
        result = build_dispatch_pause_result(
            state=state,
            dispatch=dispatch,
            checkpoint_error=None,
            current_ticket_rel_path="tickets/TICKET-001.md",
            agent_text="agent text",
            agent_id="codex",
            agent_conversation_id="conv-1",
            agent_turn_id="turn-1",
            workspace_root=tmp_path,
        )
        assert result.status == "paused"
        assert result.reason == "Review needed"
        assert result.agent_output == "agent text"
        assert result.dispatch is dispatch
        assert result.state["status"] == "paused"
        assert result.state["reason_code"] == "user_pause"
        assert "pause_context" in result.state

    def test_includes_checkpoint_error_in_reason(self, tmp_path):
        state: dict[str, Any] = {}
        dispatch = _make_dispatch_record(tmp_path, title="Paused for input")
        result = build_dispatch_pause_result(
            state=state,
            dispatch=dispatch,
            checkpoint_error="commit failed: disk full",
            current_ticket_rel_path="tickets/T.md",
            agent_text="x",
            agent_id="a",
            agent_conversation_id="c",
            agent_turn_id="t",
            workspace_root=tmp_path,
        )
        assert "checkpoint commit failed" in result.reason
        assert "disk full" in result.reason

    def test_uses_default_title_when_missing(self, tmp_path):
        state: dict[str, Any] = {}
        dispatch = _make_dispatch_record(tmp_path, title=None)
        result = build_dispatch_pause_result(
            state=state,
            dispatch=dispatch,
            checkpoint_error=None,
            current_ticket_rel_path="t.md",
            agent_text="x",
            agent_id="a",
            agent_conversation_id="c",
            agent_turn_id="t",
            workspace_root=tmp_path,
        )
        assert result.reason == "Paused for user input."


class TestBuildPauseResult:
    def test_sets_pause_context_with_repo_fingerprint(self, tmp_path):
        _init_git_repo(tmp_path)
        state: dict[str, Any] = {"reply_seq": 3}
        result = build_pause_result(
            state=state,
            reason="stuck",
            reason_code="loop",
            workspace_root=tmp_path,
        )
        assert result["state"]["pause_context"]["paused_reply_seq"] == 3
        assert "repo_fingerprint" in result["state"]["pause_context"]

    def test_sets_reason_details_when_provided(self):
        state: dict[str, Any] = {}
        result = build_pause_result(
            state=state,
            reason="stuck",
            reason_code="loop",
            reason_details="some details",
        )
        assert result["state"]["reason_details"] == "some details"
        assert result["reason_details"] == "some details"

    def test_clears_reason_details_when_absent(self):
        state = {"reason_details": "old"}
        result = build_pause_result(
            state=state,
            reason="stuck",
            reason_code="loop",
        )
        assert "reason_details" not in result["state"]


class TestHandleFailedTurn:
    @pytest.mark.asyncio
    async def test_network_retry_sets_retry_state(self, tmp_path):
        ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
        ticket_dir.mkdir(parents=True)
        ticket_path = ticket_dir / "TICKET-001.md"
        _write_ticket(ticket_path)
        _init_git_repo(tmp_path)

        runner = TicketRunner(
            workspace_root=tmp_path,
            run_id="run-1",
            config=TicketRunConfig(
                ticket_dir=Path(".codex-autorunner/tickets"),
                max_network_retries=5,
                auto_commit=False,
            ),
            agent_pool=FakeAgentPool(
                lambda req: AgentTurnResult(
                    agent_id="codex",
                    conversation_id="c",
                    turn_id="t",
                    text="ok",
                )
            ),
        )

        result_obj = TurnExecutionResult(
            success=False,
            error="Network error: timeout",
            text="partial",
            agent_id="codex",
            conversation_id="conv-1",
            turn_id="turn-1",
            is_network_error=True,
            should_retry=True,
            network_retries=1,
        )

        state: dict[str, Any] = {}
        r = runner._handle_failed_turn(
            state,
            result=result_obj,
            current_ticket_path="tickets/TICKET-001.md",
            commit_pending=False,
            commit_retries=0,
            head_before_turn=None,
        )
        assert r.status == "continue"
        assert r.state["network_retry"]["retries"] == 1
        assert r.state["last_agent_output"] == "partial"
        assert "Network error detected" in r.reason

    @pytest.mark.asyncio
    async def test_non_network_failure_pauses(self, tmp_path):
        ticket_dir = tmp_path / ".codex-autorunner" / "tickets"
        ticket_dir.mkdir(parents=True)
        ticket_path = ticket_dir / "TICKET-001.md"
        _write_ticket(ticket_path)
        _init_git_repo(tmp_path)

        runner = TicketRunner(
            workspace_root=tmp_path,
            run_id="run-1",
            config=TicketRunConfig(
                ticket_dir=Path(".codex-autorunner/tickets"),
                max_network_retries=5,
                auto_commit=False,
            ),
            agent_pool=FakeAgentPool(
                lambda req: AgentTurnResult(
                    agent_id="codex",
                    conversation_id="c",
                    turn_id="t",
                    text="ok",
                )
            ),
        )

        result_obj = TurnExecutionResult(
            success=False,
            error="Validation error: bad config",
            text="",
            agent_id="codex",
            conversation_id="conv-1",
            turn_id="turn-1",
            is_network_error=False,
            should_retry=False,
            network_retries=0,
        )

        state: dict[str, Any] = {}
        r = runner._handle_failed_turn(
            state,
            result=result_obj,
            current_ticket_path="tickets/TICKET-001.md",
            commit_pending=False,
            commit_retries=0,
            head_before_turn=None,
        )
        assert r.status == "paused"
        assert "network_retry" not in r.state
        assert "Validation error" in (r.reason_details or "")


def _make_dispatch_record(
    tmp_path: Path, title: Optional[str] = "Review"
) -> DispatchRecord:
    dispatch = Dispatch(mode="pause", body="body text", title=title)
    return DispatchRecord(
        seq=1,
        dispatch=dispatch,
        archived_dir=tmp_path / "dispatch_history" / "0001",
        archived_files=(tmp_path / "dispatch_history" / "0001" / "DISPATCH.md",),
    )


def _make_mock_result():
    result = TurnExecutionResult(
        success=True,
        text="done",
        agent_id="codex",
        conversation_id="conv-1",
        turn_id="turn-1",
    )
    return result


class TestHandleFrontmatterRecheck:
    def test_no_errors_returns_updated_fm(self, tmp_path):
        ticket_path = tmp_path / "TICKET-001.md"
        ticket_path.write_text(
            "---\nagent: codex\ndone: false\ntitle: Test\n---\n\nBody\n",
            encoding="utf-8",
        )
        result = handle_frontmatter_recheck(
            ticket_path=ticket_path,
            lint_errors=None,
            lint_retries=0,
            max_lint_retries=3,
            agent_conversation_id="conv-1",
        )
        assert result.should_pause is False
        assert result.should_retry is False
        assert result.updated_frontmatter is not None
        assert result.updated_frontmatter.done is False

    def test_errors_below_limit_returns_retry(self, tmp_path):
        ticket_path = tmp_path / "TICKET-001.md"
        ticket_path.write_text(
            "---\nagent: codex\n# done missing\n---\n\nBody\n",
            encoding="utf-8",
        )
        result = handle_frontmatter_recheck(
            ticket_path=ticket_path,
            lint_errors=["frontmatter invalid"],
            lint_retries=0,
            max_lint_retries=3,
            agent_conversation_id="conv-1",
        )
        assert result.should_retry is True
        assert result.should_pause is False
        assert result.lint_state is not None
        assert result.lint_state["retries"] == 1
        assert result.lint_state["conversation_id"] == "conv-1"
        assert result.lint_state["errors"] is not None

    def test_errors_at_limit_returns_pause(self, tmp_path):
        ticket_path = tmp_path / "TICKET-001.md"
        ticket_path.write_text(
            "---\nagent: codex\n# done missing\n---\n\nBody\n",
            encoding="utf-8",
        )
        result = handle_frontmatter_recheck(
            ticket_path=ticket_path,
            lint_errors=["frontmatter invalid"],
            lint_retries=3,
            max_lint_retries=3,
            agent_conversation_id="conv-1",
        )
        assert result.should_pause is True
        assert result.should_retry is False
        assert result.pause_reason_code == "needs_user_fix"
        assert "Exceeded lint retry limit" in (result.pause_reason_details or "")


class TestGetRepoFingerprint:
    def test_returns_head_and_status_when_git_repo(self, tmp_path):
        _init_git_repo(tmp_path)
        fp = get_repo_fingerprint(tmp_path)
        assert fp is not None
        lines = fp.split("\n")
        assert len(lines[0]) == 40
        assert lines[0].strip() != ""

    def test_returns_none_when_not_git_repo(self, tmp_path):
        fp = get_repo_fingerprint(tmp_path)
        assert fp is None

    def test_includes_dirty_status(self, tmp_path):
        _init_git_repo(tmp_path)
        (tmp_path / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        fp = get_repo_fingerprint(tmp_path)
        assert fp is not None
        assert "dirty.txt" in fp


class TestCheckTicketFrontmatter:
    def test_rewrites_error_prefix_for_read_failure(self, tmp_path):
        nonexistent = tmp_path / "TICKET-999.md"
        fm, errors = check_ticket_frontmatter(ticket_path=nonexistent)
        assert fm is None
        assert errors is not None
        assert any("Failed to read ticket after turn:" in e for e in errors)

    def test_valid_frontmatter_returns_no_errors(self, tmp_path):
        ticket_path = tmp_path / "TICKET-001.md"
        ticket_path.write_text(
            "---\nagent: codex\ndone: false\ntitle: Test\n---\n\nBody\n",
            encoding="utf-8",
        )
        fm, errors = check_ticket_frontmatter(ticket_path=ticket_path)
        assert fm is not None
        assert errors is None or errors == []


class TestCheckpointGit:
    def test_returns_none_on_clean_repo(self, tmp_path):
        from codex_autorunner.tickets.runner_post_turn import checkpoint_git

        _init_git_repo(tmp_path)
        err = checkpoint_git(
            workspace_root=tmp_path,
            run_id="run-1",
            turn=1,
            agent="codex",
            checkpoint_message_template="checkpoint {run_id} turn {turn}",
        )
        assert err is None

    def test_returns_none_after_successful_commit(self, tmp_path):
        from codex_autorunner.tickets.runner_post_turn import checkpoint_git

        _init_git_repo(tmp_path)
        (tmp_path / "work.txt").write_text("content\n", encoding="utf-8")
        err = checkpoint_git(
            workspace_root=tmp_path,
            run_id="run-1",
            turn=1,
            agent="codex",
            checkpoint_message_template="checkpoint {run_id} turn {turn} agent {agent}",
        )
        assert err is None

    def test_returns_error_string_on_git_failure(self, tmp_path):
        from codex_autorunner.tickets.runner_post_turn import checkpoint_git

        err = checkpoint_git(
            workspace_root=tmp_path,
            run_id="run-1",
            turn=1,
            agent="codex",
            checkpoint_message_template="checkpoint",
        )
        assert isinstance(err, str)
        assert len(err) > 0
