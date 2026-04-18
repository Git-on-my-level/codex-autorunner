from __future__ import annotations

import codex_autorunner.integrations.chat.managed_thread_turns as managed_thread_turns_module


def test_build_managed_thread_delivery_intent_projects_finalization_result() -> None:
    finalized = managed_thread_turns_module.ManagedThreadFinalizationResult(
        status="ok",
        assistant_text="Final answer",
        error=None,
        managed_thread_id="thread-1",
        managed_turn_id="turn-1",
        backend_thread_id="backend-1",
        token_usage={"output_tokens": 12},
        session_notice="Notice: recovered session.",
    )
    surface = managed_thread_turns_module.ManagedThreadSurfaceInfo(
        log_label="Telegram",
        surface_kind="telegram",
        surface_key="chat:1",
        metadata={"parse_mode": "HTML"},
    )

    intent = managed_thread_turns_module.build_managed_thread_delivery_intent(
        finalized,
        surface=surface,
        transport_target={"chat_id": 1, "thread_id": 2},
        metadata={"source": "test"},
    )

    assert intent.managed_thread_id == "thread-1"
    assert intent.target.surface_kind == "telegram"
    assert intent.target.transport_target == {"chat_id": 1, "thread_id": 2}
    assert intent.envelope.final_status == "ok"
    assert intent.envelope.session_notice == "Notice: recovered session."
    assert intent.envelope.transport_hints == {"parse_mode": "HTML"}
    assert intent.idempotency_key.startswith("managed-delivery:")
