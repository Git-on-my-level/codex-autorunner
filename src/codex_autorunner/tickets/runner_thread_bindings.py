from __future__ import annotations

from typing import Any, Optional

TICKET_THREAD_BINDINGS_KEY = "ticket_thread_bindings"
TICKET_THREAD_DEBUG_KEY = "ticket_thread_debug"


def normalize_profile(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def is_stale_ticket_thread_error(exc: BaseException) -> bool:
    if not isinstance(exc, KeyError):
        return False
    return "Unknown thread target" in str(exc)


def _ticket_thread_bindings(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = state.get(TICKET_THREAD_BINDINGS_KEY)
    if not isinstance(raw, dict):
        return {}
    bindings: dict[str, dict[str, Any]] = {}
    for ticket_id, payload in raw.items():
        if not isinstance(ticket_id, str) or not ticket_id.strip():
            continue
        if not isinstance(payload, dict):
            continue
        bindings[ticket_id] = dict(payload)
    return bindings


def _persist_ticket_thread_bindings(
    state: dict[str, Any],
    bindings: dict[str, dict[str, Any]],
) -> None:
    if bindings:
        state[TICKET_THREAD_BINDINGS_KEY] = bindings
    else:
        state.pop(TICKET_THREAD_BINDINGS_KEY, None)


def validate_lint_retry_conversation_id(
    *,
    lint_state: dict[str, Any],
    conversation_id: Optional[str],
    current_ticket_path: str,
    current_ticket_id: str,
    canonical_agent_id: str,
    current_ticket_profile: Optional[str],
) -> Optional[str]:
    if conversation_id is None:
        return None
    lint_retry_ticket_path = lint_state.get("ticket_path")
    lint_retry_ticket_id = lint_state.get("ticket_id")
    lint_retry_agent_id = normalize_profile(lint_state.get("agent_id"))
    lint_retry_profile = normalize_profile(lint_state.get("profile"))
    if (
        (
            isinstance(lint_retry_ticket_path, str)
            and lint_retry_ticket_path != current_ticket_path
        )
        or (
            isinstance(lint_retry_ticket_id, str)
            and current_ticket_id != "lint-retry-ticket"
            and lint_retry_ticket_id != current_ticket_id
        )
        or (
            lint_retry_agent_id is not None
            and lint_retry_agent_id != canonical_agent_id
        )
        or ("profile" in lint_state and lint_retry_profile != current_ticket_profile)
    ):
        return None
    return conversation_id


def clear_previous_ticket_binding(
    state: dict[str, Any],
    *,
    previous_ticket_id: Optional[str],
    current_ticket_id: str,
) -> None:
    if previous_ticket_id and previous_ticket_id != current_ticket_id:
        clear_ticket_thread_binding(
            state,
            ticket_id=previous_ticket_id,
            reason="ticket_changed",
        )


def _set_ticket_thread_debug(state: dict[str, Any], **payload: Any) -> None:
    cleaned = {key: value for key, value in payload.items() if value is not None}
    if cleaned:
        state[TICKET_THREAD_DEBUG_KEY] = cleaned
    else:
        state.pop(TICKET_THREAD_DEBUG_KEY, None)


def clear_ticket_thread_binding(
    state: dict[str, Any],
    *,
    ticket_id: str,
    reason: str,
) -> None:
    bindings = _ticket_thread_bindings(state)
    existing = bindings.pop(ticket_id, None)
    _persist_ticket_thread_bindings(state, bindings)
    _set_ticket_thread_debug(
        state,
        ticket_id=ticket_id,
        action="clear" if existing is not None else "noop",
        reason=reason,
        thread_target_id=(
            existing.get("thread_target_id") if isinstance(existing, dict) else None
        ),
        agent_id=existing.get("agent_id") if isinstance(existing, dict) else None,
        profile=(
            normalize_profile(existing.get("profile"))
            if isinstance(existing, dict)
            else None
        ),
        ticket_path=existing.get("ticket_path") if isinstance(existing, dict) else None,
    )


def resolve_ticket_thread_binding(
    state: dict[str, Any],
    *,
    ticket_id: str,
    ticket_path: str,
    agent_id: str,
    profile: Optional[str],
    lint_retry_conversation_id: Optional[str],
) -> tuple[Optional[str], dict[str, Any]]:
    bindings = _ticket_thread_bindings(state)
    binding = bindings.get(ticket_id)
    if isinstance(binding, dict):
        bound_thread_target_id = binding.get("thread_target_id")
        bound_agent_id = binding.get("agent_id")
        bound_profile = normalize_profile(binding.get("profile"))
        if not isinstance(bound_thread_target_id, str) or not bound_thread_target_id:
            bindings.pop(ticket_id, None)
            _persist_ticket_thread_bindings(state, bindings)
            debug = {
                "ticket_id": ticket_id,
                "ticket_path": ticket_path,
                "agent_id": agent_id,
                "profile": profile,
                "action": "reset",
                "reason": "missing_thread_target_id",
            }
            _set_ticket_thread_debug(state, **debug)
            return None, debug
        if bound_agent_id != agent_id:
            bindings.pop(ticket_id, None)
            _persist_ticket_thread_bindings(state, bindings)
            debug = {
                "ticket_id": ticket_id,
                "ticket_path": ticket_path,
                "agent_id": agent_id,
                "profile": profile,
                "action": "reset",
                "reason": "agent_changed",
                "previous_thread_target_id": bound_thread_target_id,
                "previous_agent_id": bound_agent_id,
            }
            _set_ticket_thread_debug(state, **debug)
            return None, debug
        if bound_profile != profile:
            bindings.pop(ticket_id, None)
            _persist_ticket_thread_bindings(state, bindings)
            debug = {
                "ticket_id": ticket_id,
                "ticket_path": ticket_path,
                "agent_id": agent_id,
                "profile": profile,
                "action": "reset",
                "reason": "profile_changed",
                "previous_thread_target_id": bound_thread_target_id,
                "previous_profile": bound_profile,
            }
            _set_ticket_thread_debug(state, **debug)
            return None, debug
        debug = {
            "ticket_id": ticket_id,
            "ticket_path": ticket_path,
            "agent_id": agent_id,
            "profile": profile,
            "action": "reuse",
            "reason": "binding_matched_runtime_identity",
            "thread_target_id": bound_thread_target_id,
        }
        _set_ticket_thread_debug(state, **debug)
        return bound_thread_target_id, debug

    if lint_retry_conversation_id:
        debug = {
            "ticket_id": ticket_id,
            "ticket_path": ticket_path,
            "agent_id": agent_id,
            "profile": profile,
            "action": "reuse",
            "reason": "lint_retry_conversation",
            "thread_target_id": lint_retry_conversation_id,
        }
        _set_ticket_thread_debug(state, **debug)
        return lint_retry_conversation_id, debug

    debug = {
        "ticket_id": ticket_id,
        "ticket_path": ticket_path,
        "agent_id": agent_id,
        "profile": profile,
        "action": "create",
        "reason": "no_existing_binding",
    }
    _set_ticket_thread_debug(state, **debug)
    return None, debug


def store_ticket_thread_binding(
    state: dict[str, Any],
    *,
    ticket_id: str,
    ticket_path: str,
    agent_id: str,
    profile: Optional[str],
    thread_target_id: Optional[str],
    binding_decision: dict[str, Any],
) -> None:
    if not isinstance(thread_target_id, str) or not thread_target_id:
        return
    bindings = _ticket_thread_bindings(state)
    previous = bindings.get(ticket_id)
    bindings[ticket_id] = {
        "thread_target_id": thread_target_id,
        "agent_id": agent_id,
        "profile": profile,
        "ticket_path": ticket_path,
    }
    _persist_ticket_thread_bindings(state, bindings)

    previous_thread_target_id = (
        previous.get("thread_target_id") if isinstance(previous, dict) else None
    )
    decision_action = binding_decision.get("action")
    if (
        decision_action == "reuse"
        and binding_decision.get("thread_target_id") == thread_target_id
    ):
        action = "reused"
        reason = binding_decision.get("reason") or "binding_matched_runtime_identity"
    elif decision_action == "reset":
        action = "reset_and_recreated"
        reason = binding_decision.get("reason") or "binding_reset"
    elif previous_thread_target_id and previous_thread_target_id != thread_target_id:
        action = "rebound"
        reason = binding_decision.get("reason") or "thread_target_changed"
    else:
        action = "created"
        reason = binding_decision.get("reason") or "no_existing_binding"

    _set_ticket_thread_debug(
        state,
        ticket_id=ticket_id,
        ticket_path=ticket_path,
        agent_id=agent_id,
        profile=profile,
        action=action,
        reason=reason,
        thread_target_id=thread_target_id,
        previous_thread_target_id=binding_decision.get("previous_thread_target_id"),
    )
