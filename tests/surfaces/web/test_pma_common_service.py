from __future__ import annotations

import pytest
from fastapi import HTTPException

from codex_autorunner.surfaces.web.schemas import PmaManagedThreadCreateRequest
from codex_autorunner.surfaces.web.services.pma.common import (
    build_idempotency_key,
    normalize_optional_text,
    pma_config_from_raw,
)
from codex_autorunner.surfaces.web.services.pma.managed_thread_followup import (
    resolve_managed_thread_followup_policy,
)


def test_normalize_optional_text_trims_or_returns_none() -> None:
    assert normalize_optional_text("  hello  ") == "hello"
    assert normalize_optional_text("   ") is None
    assert normalize_optional_text(None) is None
    assert normalize_optional_text(42) is None


def test_pma_config_from_raw_uses_defaults_for_missing_values() -> None:
    config = pma_config_from_raw({})
    assert config == {
        "enabled": True,
        "default_agent": None,
        "model": None,
        "reasoning": None,
        "managed_thread_terminal_followup_default": True,
        "active_context_max_lines": 200,
        "max_text_chars": 10_000,
    }


def test_pma_config_from_raw_normalizes_and_coerces_values() -> None:
    raw = {
        "pma": {
            "enabled": 0,
            "default_agent": " codex ",
            "model": " gpt-test ",
            "reasoning": " high ",
            "active_context_max_lines": "350",
            "max_text_chars": "1200",
        }
    }
    config = pma_config_from_raw(raw)
    assert config["enabled"] is False
    assert config["default_agent"] == "codex"
    assert config["model"] == "gpt-test"
    assert config["reasoning"] == "high"
    assert config["managed_thread_terminal_followup_default"] is True
    assert config["active_context_max_lines"] == 350
    assert config["max_text_chars"] == 1200


def test_build_idempotency_key_is_deterministic_and_input_sensitive() -> None:
    first = build_idempotency_key(
        lane_id="lane-1",
        agent="codex",
        model="gpt-test",
        reasoning="high",
        client_turn_id="turn-1",
        message="hello",
    )
    second = build_idempotency_key(
        lane_id="lane-1",
        agent="codex",
        model="gpt-test",
        reasoning="high",
        client_turn_id="turn-1",
        message="hello",
    )
    changed = build_idempotency_key(
        lane_id="lane-1",
        agent="codex",
        model="gpt-test",
        reasoning="high",
        client_turn_id="turn-1",
        message="hello world",
    )
    assert first == second
    assert first.startswith("pma:")
    assert changed != first


def test_managed_thread_create_request_captures_explicit_followup_fields() -> None:
    payload = PmaManagedThreadCreateRequest.model_validate(
        {
            "agent": "codex",
            "workspace_root": "/tmp/workspace",
            "terminalFollowup": True,
            "notifyLane": " lane-1 ",
            "notifyOnce": False,
        }
    )

    assert payload.notify_on_explicit is False
    assert payload.terminal_followup_explicit is True
    assert payload.notify_lane_explicit is True
    assert payload.notify_once_explicit is True


def test_resolve_managed_thread_followup_policy_uses_defaults_best_effort() -> None:
    payload = PmaManagedThreadCreateRequest.model_validate(
        {
            "agent": "codex",
            "workspace_root": "/tmp/workspace",
        }
    )

    policy = resolve_managed_thread_followup_policy(
        payload,
        default_terminal_followup=True,
    )

    assert policy.enabled is True
    assert policy.required is False
    assert policy.event_mode == "terminal"
    assert policy.lane_id is None
    assert policy.notify_once is True


def test_resolve_managed_thread_followup_policy_marks_explicit_customization_required() -> (
    None
):
    payload = PmaManagedThreadCreateRequest.model_validate(
        {
            "agent": "codex",
            "workspace_root": "/tmp/workspace",
            "notifyLane": " lane-1 ",
            "notifyOnce": False,
        }
    )

    policy = resolve_managed_thread_followup_policy(
        payload,
        default_terminal_followup=True,
    )

    assert policy.enabled is True
    assert policy.required is True
    assert policy.event_mode == "terminal"
    assert policy.lane_id == "lane-1"
    assert policy.notify_once is False


def test_resolve_managed_thread_followup_policy_rejects_conflicting_opt_out() -> None:
    payload = PmaManagedThreadCreateRequest.model_validate(
        {
            "agent": "codex",
            "workspace_root": "/tmp/workspace",
            "notifyOn": "terminal",
            "terminalFollowup": False,
        }
    )

    with pytest.raises(HTTPException) as exc_info:
        resolve_managed_thread_followup_policy(
            payload,
            default_terminal_followup=True,
        )

    assert exc_info.value.status_code == 400
    assert "terminal_followup=false" in str(exc_info.value.detail)
