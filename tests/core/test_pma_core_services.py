from __future__ import annotations

from pathlib import Path

import pytest

from codex_autorunner.core.car_context import build_car_context_bundle
from codex_autorunner.core.pma.outbound_payloads import (
    MANAGED_THREAD_PUBLIC_EXECUTION_ERROR,
    sanitize_managed_thread_result_error,
)
from codex_autorunner.core.pma.policies import (
    normalize_busy_policy,
    validate_max_text_chars,
    validate_required_message,
)
from codex_autorunner.core.pma.prompts import (
    ManagedThreadPromptRequest,
    compose_compacted_prompt,
    compose_managed_thread_execution_prompt,
)
from codex_autorunner.core.pma.queue_prompts import (
    PmaQueuePromptInputs,
    build_queue_execution_prompt,
)


def test_policy_helpers_raise_value_error_for_transport_adapters() -> None:
    assert normalize_busy_policy("Interrupt") == "interrupt"
    assert validate_required_message(" hello ") == " hello "
    validate_max_text_chars("abc", 3)

    with pytest.raises(ValueError, match="busy_policy"):
        normalize_busy_policy("block")
    with pytest.raises(ValueError, match="message is required"):
        validate_required_message(" ")
    with pytest.raises(ValueError, match="max_text_chars"):
        validate_max_text_chars("abcd", 3)


def test_managed_thread_prompt_compacts_only_fresh_backend_conversations(
    tmp_path: Path,
) -> None:
    compacted = compose_compacted_prompt("prior context", "continue")
    assert "prior context" in compacted
    assert "continue" in compacted

    fresh = compose_managed_thread_execution_prompt(
        ManagedThreadPromptRequest(
            agent="codex",
            hub_root=tmp_path,
            runtime_cwd=None,
            stored_backend_id=None,
            compact_seed="prior context",
            message="continue",
            context_bundle=build_car_context_bundle("none"),
        )
    )
    resumed = compose_managed_thread_execution_prompt(
        ManagedThreadPromptRequest(
            agent="codex",
            hub_root=tmp_path,
            runtime_cwd=None,
            stored_backend_id="backend-1",
            compact_seed="prior context",
            message="continue",
            context_bundle=build_car_context_bundle("none"),
        )
    )

    assert "Context summary (from compaction)" in fresh
    assert "Context summary (from compaction)" not in resumed


def test_queue_prompt_builder_can_return_plain_prompt_without_injector(
    tmp_path: Path,
) -> None:
    prompt = build_queue_execution_prompt(
        PmaQueuePromptInputs(
            prompt_base="Base prompt",
            snapshot={},
            message="hello",
            hub_root=tmp_path,
            prompt_state_key="pma.codex",
        )
    )

    assert isinstance(prompt, str)
    assert "Base prompt" in prompt
    assert "hello" in prompt


def test_outbound_error_sanitizer_is_transport_neutral() -> None:
    assert sanitize_managed_thread_result_error("PMA chat timed out") == (
        "PMA chat timed out"
    )
    assert sanitize_managed_thread_result_error("unknown internal detail") == (
        MANAGED_THREAD_PUBLIC_EXECUTION_ERROR
    )
