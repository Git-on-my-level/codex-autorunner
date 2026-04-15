from __future__ import annotations

from codex_autorunner.integrations.chat.action_ux_contract import (
    CHAT_ACTION_UX_CONTRACT,
    CHAT_ACTION_UX_CONTRACT_VERSION,
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
