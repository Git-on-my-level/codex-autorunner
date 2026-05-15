from __future__ import annotations

from pathlib import Path

import pytest

from codex_autorunner.tickets.agent_pool import AgentTurnRequest, AgentTurnResult
from codex_autorunner.tickets.runner_step_support import (
    execute_turn_with_thread_binding_retry,
)
from codex_autorunner.tickets.runner_thread_bindings import (
    clear_previous_ticket_binding,
    clear_ticket_thread_binding,
    normalize_profile,
    resolve_ticket_thread_binding,
    store_ticket_thread_binding,
    validate_lint_retry_conversation_id,
)


class _CapturingAgentPool:
    def __init__(self) -> None:
        self.requests: list[AgentTurnRequest] = []

    async def run_turn(self, req: AgentTurnRequest) -> AgentTurnResult:
        self.requests.append(req)
        if req.conversation_id == "stale-thread":
            raise KeyError("Unknown thread target stale-thread")
        return AgentTurnResult(
            agent_id=req.agent_id,
            conversation_id=req.conversation_id or "fresh-thread",
            turn_id="turn-1",
            text="ok",
        )


class TestNormalizeProfile:
    def test_string_passes_through(self) -> None:
        assert normalize_profile("m4-pma") == "m4-pma"

    def test_whitespace_stripped(self) -> None:
        assert normalize_profile("  m4-pma  ") == "m4-pma"

    def test_empty_string_becomes_none(self) -> None:
        assert normalize_profile("") is None

    def test_whitespace_only_becomes_none(self) -> None:
        assert normalize_profile("   ") is None

    def test_non_string_becomes_none(self) -> None:
        assert normalize_profile(None) is None
        assert normalize_profile(42) is None

    def test_none_returns_none(self) -> None:
        assert normalize_profile(None) is None


class TestResolveTicketThreadBinding:
    def test_no_existing_binding_creates_new(self) -> None:
        state: dict = {}
        result, debug = resolve_ticket_thread_binding(
            state,
            ticket_id="tkt-1",
            ticket_path="tickets/T1.md",
            agent_id="hermes",
            profile="m4-pma",
            lint_retry_conversation_id=None,
        )
        assert result is None
        assert debug["action"] == "create"
        assert debug["reason"] == "no_existing_binding"

    def test_matching_binding_reuses(self) -> None:
        state: dict = {
            "ticket_thread_bindings": {
                "tkt-1": {
                    "thread_target_id": "thread-1",
                    "agent_id": "hermes",
                    "profile": "m4-pma",
                    "ticket_path": "tickets/T1.md",
                }
            }
        }
        result, debug = resolve_ticket_thread_binding(
            state,
            ticket_id="tkt-1",
            ticket_path="tickets/T1.md",
            agent_id="hermes",
            profile="m4-pma",
            lint_retry_conversation_id=None,
        )
        assert result == "thread-1"
        assert debug["action"] == "reuse"
        assert debug["reason"] == "binding_matched_runtime_identity"

    def test_agent_change_resets_binding(self) -> None:
        state: dict = {
            "ticket_thread_bindings": {
                "tkt-1": {
                    "thread_target_id": "thread-1",
                    "agent_id": "codex",
                    "profile": None,
                    "ticket_path": "tickets/T1.md",
                }
            }
        }
        result, debug = resolve_ticket_thread_binding(
            state,
            ticket_id="tkt-1",
            ticket_path="tickets/T1.md",
            agent_id="hermes",
            profile=None,
            lint_retry_conversation_id=None,
        )
        assert result is None
        assert debug["action"] == "reset"
        assert debug["reason"] == "agent_changed"
        assert debug["previous_agent_id"] == "codex"

    def test_profile_change_resets_binding(self) -> None:
        state: dict = {
            "ticket_thread_bindings": {
                "tkt-1": {
                    "thread_target_id": "thread-1",
                    "agent_id": "hermes",
                    "profile": "m4-pma",
                    "ticket_path": "tickets/T1.md",
                }
            }
        }
        result, debug = resolve_ticket_thread_binding(
            state,
            ticket_id="tkt-1",
            ticket_path="tickets/T1.md",
            agent_id="hermes",
            profile="other-profile",
            lint_retry_conversation_id=None,
        )
        assert result is None
        assert debug["action"] == "reset"
        assert debug["reason"] == "profile_changed"
        assert debug["previous_profile"] == "m4-pma"

    def test_missing_thread_target_id_resets(self) -> None:
        state: dict = {
            "ticket_thread_bindings": {
                "tkt-1": {
                    "thread_target_id": "",
                    "agent_id": "hermes",
                    "profile": None,
                    "ticket_path": "tickets/T1.md",
                }
            }
        }
        result, debug = resolve_ticket_thread_binding(
            state,
            ticket_id="tkt-1",
            ticket_path="tickets/T1.md",
            agent_id="hermes",
            profile=None,
            lint_retry_conversation_id=None,
        )
        assert result is None
        assert debug["action"] == "reset"
        assert debug["reason"] == "missing_thread_target_id"

    def test_lint_retry_conversation_id_reused_when_no_binding(self) -> None:
        state: dict = {}
        result, debug = resolve_ticket_thread_binding(
            state,
            ticket_id="tkt-1",
            ticket_path="tickets/T1.md",
            agent_id="hermes",
            profile=None,
            lint_retry_conversation_id="lint-conv-1",
        )
        assert result == "lint-conv-1"
        assert debug["action"] == "reuse"
        assert debug["reason"] == "lint_retry_conversation"

    def test_binding_takes_precedence_over_lint_retry(self) -> None:
        state: dict = {
            "ticket_thread_bindings": {
                "tkt-1": {
                    "thread_target_id": "bound-thread",
                    "agent_id": "hermes",
                    "profile": None,
                    "ticket_path": "tickets/T1.md",
                }
            }
        }
        result, debug = resolve_ticket_thread_binding(
            state,
            ticket_id="tkt-1",
            ticket_path="tickets/T1.md",
            agent_id="hermes",
            profile=None,
            lint_retry_conversation_id="lint-conv-1",
        )
        assert result == "bound-thread"
        assert debug["action"] == "reuse"


@pytest.mark.asyncio
async def test_thread_binding_retry_carries_compact_prompt_without_replacing_full(
    tmp_path: Path,
) -> None:
    state: dict = {
        "ticket_thread_bindings": {
            "tkt-1": {
                "thread_target_id": "stale-thread",
                "agent_id": "codex",
                "profile": None,
                "ticket_path": ".codex-autorunner/tickets/TICKET-001.md",
            }
        }
    }
    pool = _CapturingAgentPool()

    result, binding_decision = await execute_turn_with_thread_binding_retry(
        agent_pool=pool,
        workspace_root=tmp_path,
        state=state,
        ticket_id="tkt-1",
        ticket_path=".codex-autorunner/tickets/TICKET-001.md",
        agent_id="codex",
        profile=None,
        prompt="full bootstrap prompt",
        existing_session_prompt="compact same-thread prompt",
        lint_retry_conversation_id=None,
        turn_options=None,
        emit_event=None,
        max_network_retries=0,
        current_network_retries=0,
    )

    assert result.success is True
    assert binding_decision["action"] == "reset"
    assert binding_decision["reason"] == "stale_or_missing_thread_target"
    assert [request.prompt for request in pool.requests] == [
        "full bootstrap prompt",
        "full bootstrap prompt",
    ]
    assert [request.existing_session_prompt for request in pool.requests] == [
        "compact same-thread prompt",
        "compact same-thread prompt",
    ]
    assert [request.conversation_id for request in pool.requests] == [
        "stale-thread",
        None,
    ]


class TestStoreTicketThreadBinding:
    def test_stores_binding_with_profile(self) -> None:
        state: dict = {}
        binding_decision = {"action": "create", "reason": "no_existing_binding"}
        store_ticket_thread_binding(
            state,
            ticket_id="tkt-1",
            ticket_path="tickets/T1.md",
            agent_id="hermes",
            profile="m4-pma",
            thread_target_id="thread-1",
            binding_decision=binding_decision,
        )
        assert state["ticket_thread_bindings"]["tkt-1"] == {
            "thread_target_id": "thread-1",
            "agent_id": "hermes",
            "profile": "m4-pma",
            "ticket_path": "tickets/T1.md",
        }
        assert state["ticket_thread_debug"]["action"] == "created"

    def test_stores_binding_without_profile(self) -> None:
        state: dict = {}
        binding_decision = {"action": "create", "reason": "no_existing_binding"}
        store_ticket_thread_binding(
            state,
            ticket_id="tkt-1",
            ticket_path="tickets/T1.md",
            agent_id="hermes",
            profile=None,
            thread_target_id="thread-1",
            binding_decision=binding_decision,
        )
        assert state["ticket_thread_bindings"]["tkt-1"]["profile"] is None

    def test_skips_when_thread_target_id_is_none(self) -> None:
        state: dict = {}
        binding_decision = {"action": "create", "reason": "no_existing_binding"}
        store_ticket_thread_binding(
            state,
            ticket_id="tkt-1",
            ticket_path="tickets/T1.md",
            agent_id="hermes",
            profile=None,
            thread_target_id=None,
            binding_decision=binding_decision,
        )
        assert "ticket_thread_bindings" not in state


class TestClearTicketThreadBinding:
    def test_clears_existing_binding(self) -> None:
        state: dict = {
            "ticket_thread_bindings": {
                "tkt-1": {
                    "thread_target_id": "thread-1",
                    "agent_id": "hermes",
                    "profile": "m4-pma",
                    "ticket_path": "tickets/T1.md",
                }
            }
        }
        clear_ticket_thread_binding(state, ticket_id="tkt-1", reason="done")
        assert "tkt-1" not in state.get("ticket_thread_bindings", {})
        assert state["ticket_thread_debug"]["action"] == "clear"
        assert state["ticket_thread_debug"]["reason"] == "done"

    def test_noop_when_no_binding_exists(self) -> None:
        state: dict = {}
        clear_ticket_thread_binding(state, ticket_id="tkt-missing", reason="done")
        assert state["ticket_thread_debug"]["action"] == "noop"


class TestValidateLintRetryConversationId:
    def test_returns_none_when_conversation_id_is_none(self) -> None:
        result = validate_lint_retry_conversation_id(
            lint_state={},
            conversation_id=None,
            current_ticket_path="tickets/T1.md",
            current_ticket_id="tkt-1",
            canonical_agent_id="hermes",
            current_ticket_profile=None,
        )
        assert result is None

    def test_returns_conversation_id_when_all_identity_matches(self) -> None:
        lint_state: dict = {
            "ticket_path": "tickets/T1.md",
            "ticket_id": "tkt-1",
            "agent_id": "hermes",
            "profile": "m4-pma",
        }
        result = validate_lint_retry_conversation_id(
            lint_state=lint_state,
            conversation_id="conv-1",
            current_ticket_path="tickets/T1.md",
            current_ticket_id="tkt-1",
            canonical_agent_id="hermes",
            current_ticket_profile="m4-pma",
        )
        assert result == "conv-1"

    def test_returns_none_when_ticket_path_mismatched(self) -> None:
        lint_state: dict = {
            "ticket_path": "tickets/T2.md",
            "ticket_id": "tkt-1",
            "agent_id": "hermes",
        }
        result = validate_lint_retry_conversation_id(
            lint_state=lint_state,
            conversation_id="conv-1",
            current_ticket_path="tickets/T1.md",
            current_ticket_id="tkt-1",
            canonical_agent_id="hermes",
            current_ticket_profile=None,
        )
        assert result is None

    def test_returns_none_when_ticket_id_mismatched(self) -> None:
        lint_state: dict = {
            "ticket_path": "tickets/T1.md",
            "ticket_id": "tkt-other",
            "agent_id": "hermes",
        }
        result = validate_lint_retry_conversation_id(
            lint_state=lint_state,
            conversation_id="conv-1",
            current_ticket_path="tickets/T1.md",
            current_ticket_id="tkt-1",
            canonical_agent_id="hermes",
            current_ticket_profile=None,
        )
        assert result is None

    def test_allows_lint_retry_ticket_id_for_lint_retry_ticket(self) -> None:
        lint_state: dict = {
            "ticket_path": "tickets/T1.md",
            "ticket_id": "tkt-other",
            "agent_id": "hermes",
        }
        result = validate_lint_retry_conversation_id(
            lint_state=lint_state,
            conversation_id="conv-1",
            current_ticket_path="tickets/T1.md",
            current_ticket_id="lint-retry-ticket",
            canonical_agent_id="hermes",
            current_ticket_profile=None,
        )
        assert result == "conv-1"

    def test_returns_none_when_agent_id_mismatched(self) -> None:
        lint_state: dict = {
            "ticket_id": "tkt-1",
            "agent_id": "codex",
        }
        result = validate_lint_retry_conversation_id(
            lint_state=lint_state,
            conversation_id="conv-1",
            current_ticket_path="tickets/T1.md",
            current_ticket_id="tkt-1",
            canonical_agent_id="hermes",
            current_ticket_profile=None,
        )
        assert result is None

    def test_returns_none_when_profile_mismatched(self) -> None:
        lint_state: dict = {
            "ticket_id": "tkt-1",
            "agent_id": "hermes",
            "profile": "m4-pma",
        }
        result = validate_lint_retry_conversation_id(
            lint_state=lint_state,
            conversation_id="conv-1",
            current_ticket_path="tickets/T1.md",
            current_ticket_id="tkt-1",
            canonical_agent_id="hermes",
            current_ticket_profile="other-profile",
        )
        assert result is None

    def test_returns_conversation_id_when_no_identity_fields_in_lint_state(
        self,
    ) -> None:
        lint_state: dict = {}
        result = validate_lint_retry_conversation_id(
            lint_state=lint_state,
            conversation_id="conv-1",
            current_ticket_path="tickets/T1.md",
            current_ticket_id="tkt-1",
            canonical_agent_id="hermes",
            current_ticket_profile=None,
        )
        assert result == "conv-1"

    def test_ignores_non_string_agent_id(self) -> None:
        lint_state: dict = {
            "agent_id": 123,
            "ticket_id": "tkt-1",
        }
        result = validate_lint_retry_conversation_id(
            lint_state=lint_state,
            conversation_id="conv-1",
            current_ticket_path="tickets/T1.md",
            current_ticket_id="tkt-1",
            canonical_agent_id="hermes",
            current_ticket_profile=None,
        )
        assert result == "conv-1"


class TestClearPreviousTicketBinding:
    def test_clears_when_previous_differs_from_current(self) -> None:
        state: dict = {
            "ticket_thread_bindings": {
                "tkt-old": {
                    "thread_target_id": "thread-old",
                    "agent_id": "hermes",
                    "profile": None,
                    "ticket_path": "tickets/T1.md",
                }
            }
        }
        clear_previous_ticket_binding(
            state,
            previous_ticket_id="tkt-old",
            current_ticket_id="tkt-new",
        )
        assert "tkt-old" not in state.get("ticket_thread_bindings", {})
        assert state["ticket_thread_debug"]["reason"] == "ticket_changed"

    def test_noop_when_previous_is_none(self) -> None:
        state: dict = {}
        clear_previous_ticket_binding(
            state,
            previous_ticket_id=None,
            current_ticket_id="tkt-new",
        )
        assert "ticket_thread_debug" not in state

    def test_noop_when_previous_matches_current(self) -> None:
        state: dict = {
            "ticket_thread_bindings": {
                "tkt-1": {
                    "thread_target_id": "thread-1",
                    "agent_id": "hermes",
                    "profile": None,
                    "ticket_path": "tickets/T1.md",
                }
            }
        }
        clear_previous_ticket_binding(
            state,
            previous_ticket_id="tkt-1",
            current_ticket_id="tkt-1",
        )
        assert "tkt-1" in state["ticket_thread_bindings"]

    def test_noop_when_previous_is_empty_string(self) -> None:
        state: dict = {}
        clear_previous_ticket_binding(
            state,
            previous_ticket_id="",
            current_ticket_id="tkt-new",
        )
        assert "ticket_thread_debug" not in state
