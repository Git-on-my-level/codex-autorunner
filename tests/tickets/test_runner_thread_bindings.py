from __future__ import annotations

from codex_autorunner.tickets.runner_thread_bindings import (
    clear_ticket_thread_binding,
    normalize_profile,
    resolve_ticket_thread_binding,
    store_ticket_thread_binding,
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
