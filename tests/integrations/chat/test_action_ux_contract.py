from __future__ import annotations

from codex_autorunner.integrations.chat.action_ux_contract import (
    CHAT_ACTION_UX_CONTRACT,
    CHAT_ACTION_UX_CONTRACT_VERSION,
    callback_entry_bypasses_queue,
    discord_component_ux_contract_for_route,
    discord_slash_command_ux_contract_for_id,
    plain_text_turn_ux_contract_for_mode,
    telegram_callback_ux_contract_for_callback,
    telegram_command_ux_contract_for_name,
)


def test_action_ux_contract_declares_all_required_surface_families() -> None:
    surfaces = {entry.surface for entry in CHAT_ACTION_UX_CONTRACT}

    assert CHAT_ACTION_UX_CONTRACT_VERSION == "chat-action-ux-v1"
    assert {
        "telegram_command",
        "discord_slash_command",
        "telegram_callback",
        "discord_component",
        "discord_modal",
        "discord_autocomplete",
        "plain_text_turn",
        "control",
    }.issubset(surfaces)


def test_action_ux_contract_reuses_shared_control_entries_across_surfaces() -> None:
    telegram_interrupt = telegram_callback_ux_contract_for_callback(
        "cancel",
        {"kind": "interrupt"},
    )
    discord_interrupt = discord_component_ux_contract_for_route(
        "turn.cancel",
        custom_id="cancel_turn",
    )
    pagination = discord_component_ux_contract_for_route(
        "bind.page",
        custom_id="bind_page:next",
    )
    refresh = telegram_callback_ux_contract_for_callback(
        "flow",
        {"action": "refresh"},
    )

    assert telegram_interrupt is not None
    assert telegram_interrupt.id == "control.interrupt"
    assert discord_interrupt is not None
    assert discord_interrupt.id == "control.interrupt"
    assert pagination is not None
    assert pagination.id == "control.pagination"
    assert refresh is not None
    assert refresh.id == "control.refresh"


def test_action_ux_contract_bridges_command_and_plain_text_policies() -> None:
    telegram_status = telegram_command_ux_contract_for_name("status")
    discord_flow_status = discord_slash_command_ux_contract_for_id("car.flow.status")
    plain_text_mentions = plain_text_turn_ux_contract_for_mode("mentions")

    assert telegram_status is not None
    assert telegram_status.queue_policy == "allow_during_turn"
    assert discord_flow_status is not None
    assert discord_flow_status.ack_class == "defer_public"
    assert plain_text_mentions is not None
    assert plain_text_mentions.anchor_message_reuse == "prefer"


def test_callback_entry_bypasses_queue_returns_false_for_none() -> None:
    assert callback_entry_bypasses_queue(None) is False


def test_callback_entry_bypasses_queue_returns_false_for_serialize() -> None:
    entry = telegram_callback_ux_contract_for_callback("resume")
    assert entry is not None
    assert entry.queue_policy == "serialize"
    assert callback_entry_bypasses_queue(entry) is False


def test_control_callbacks_bypass_queue() -> None:
    control_cases = [
        ("cancel", {"kind": "interrupt"}, "control.interrupt"),
        ("cancel", {"kind": "queue_cancel:123"}, "control.queue_cancel"),
        (
            "cancel",
            {"kind": "queue_interrupt_send:123"},
            "control.queue_interrupt_send",
        ),
        ("page", {"picker_name": "bind", "page": "1"}, "control.pagination"),
        ("flow", {"action": "refresh"}, "control.refresh"),
    ]
    for callback_id, payload, expected_id in control_cases:
        entry = telegram_callback_ux_contract_for_callback(callback_id, payload)
        assert entry is not None, f"missing UX contract for {callback_id}/{payload}"
        assert entry.id == expected_id
        assert (
            callback_entry_bypasses_queue(entry) is True
        ), f"{expected_id} should bypass queue but queue_policy={entry.queue_policy}"


def test_approval_and_question_callbacks_bypass_queue() -> None:
    bypass_cases = [
        "approval",
        "question_option",
        "question_done",
        "question_custom",
        "question_cancel",
    ]
    for callback_id in bypass_cases:
        entry = telegram_callback_ux_contract_for_callback(callback_id)
        assert entry is not None, f"missing UX contract for {callback_id}"
        assert (
            callback_entry_bypasses_queue(entry) is True
        ), f"{callback_id} should bypass queue but queue_policy={entry.queue_policy}"


def test_cancel_selection_bypasses_queue() -> None:
    entry = telegram_callback_ux_contract_for_callback("cancel", {"kind": "agent"})
    assert entry is not None
    assert entry.id == "telegram_callback.cancel.selection"
    assert (
        callback_entry_bypasses_queue(entry) is True
    ), f"cancel.selection should bypass queue but queue_policy={entry.queue_policy}"


def test_long_running_callbacks_do_not_bypass_queue() -> None:
    serialize_cases = [
        "resume",
        "bind",
        "agent",
        "agent_profile",
        "model",
        "effort",
        "update",
        "update_confirm",
        "review_commit",
        "compact",
        "flow",
        "flow_run",
    ]
    for callback_id in serialize_cases:
        payload = None
        if callback_id == "flow":
            payload = {"action": "status"}
        entry = telegram_callback_ux_contract_for_callback(callback_id, payload)
        assert entry is not None, f"missing UX contract for {callback_id}"
        assert (
            callback_entry_bypasses_queue(entry) is False
        ), f"{callback_id} should serialize but queue_policy={entry.queue_policy}"
