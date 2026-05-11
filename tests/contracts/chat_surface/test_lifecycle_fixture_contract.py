from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from codex_autorunner.core.orchestration.chat_operation_state import ChatOperationState
from codex_autorunner.core.orchestration.managed_thread_delivery import (
    ManagedThreadDeliveryState,
)

FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "chat_surface"
    / "lifecycle_contract.json"
)

EXPECTED_SCENARIOS = {
    "discord_new_thread",
    "telegram_bind_topic",
    "discord_rebind_channel",
    "pma_archive_thread",
    "web_originated_queued_turn",
    "telegram_running_turn",
    "discord_done_turn_delivery",
    "pma_failed_turn",
    "telegram_delivery_retry",
    "discord_channel_discovery",
    "notification_reply_continuation",
}
EXPECTED_SURFACES = {"discord", "telegram", "web", "pma", "notification"}
EXPECTED_LIFECYCLE = {
    "discovered",
    "bound",
    "queued",
    "running",
    "idle",
    "failed",
    "archived",
}


def _load_contract() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_chat_surface_lifecycle_fixture_covers_required_scenarios() -> None:
    payload = _load_contract()
    scenario_ids = {scenario["id"] for scenario in payload["scenarios"]}

    assert scenario_ids == EXPECTED_SCENARIOS
    assert {item["state"] for item in payload["normalized_lifecycle"]} == (
        EXPECTED_LIFECYCLE
    )
    assert {scenario["surface_kind"] for scenario in payload["scenarios"]} == (
        EXPECTED_SURFACES
    )


def test_chat_surface_lifecycle_fixture_declares_owner_boundaries() -> None:
    payload = _load_contract()
    owners = {entry["area"]: entry for entry in payload["owner_inventory"]}

    assert owners["orchestration"]["contract_role"] == "target"
    assert owners["notification_replies"]["contract_role"] == "target"
    assert owners["discord_adapter"]["contract_role"] == "legacy_transport_owner"
    assert owners["telegram_adapter"]["contract_role"] == "legacy_transport_owner"
    assert owners["channel_directory"]["contract_role"] == "discovery_projection"

    for scenario in payload["scenarios"]:
        assert scenario["required_current_sources"]
        assert scenario["legacy_notes"]


def test_chat_surface_fixture_uses_existing_operation_and_delivery_terms() -> None:
    payload = _load_contract()
    operation_states = {state.value for state in ChatOperationState}
    delivery_states = {state.value for state in ManagedThreadDeliveryState}

    event_terms = {
        term
        for scenario in payload["scenarios"]
        for event in scenario["expect_events"]
        for term in event.split(".")[1:]
    }

    assert {"queued", "running", "completed", "failed"} <= operation_states
    assert {"pending", "retry_scheduled", "delivered"} <= delivery_states
    assert {"queued", "running", "completed", "failed"} <= event_terms
    assert {"pending", "retry_scheduled", "delivered"} <= event_terms
