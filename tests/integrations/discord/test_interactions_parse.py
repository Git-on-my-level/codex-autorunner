from __future__ import annotations

from codex_autorunner.integrations.discord.interactions import (
    extract_channel_id,
    extract_command_path_and_options,
    extract_component_custom_id,
    extract_component_values,
    extract_guild_id,
    extract_interaction_id,
    extract_interaction_token,
    extract_user_id,
    is_component_interaction,
)


def test_extract_command_path_and_options_for_flow_status() -> None:
    payload = {
        "data": {
            "name": "car",
            "options": [
                {
                    "type": 2,
                    "name": "flow",
                    "options": [
                        {
                            "type": 1,
                            "name": "status",
                            "options": [
                                {"type": 3, "name": "run_id", "value": "run-123"}
                            ],
                        }
                    ],
                }
            ],
        }
    }
    path, options = extract_command_path_and_options(payload)
    assert path == ("car", "flow", "status")
    assert options == {"run_id": "run-123"}


def test_extract_ids_from_interaction_payload() -> None:
    payload = {
        "id": "inter-1",
        "token": "token-1",
        "channel_id": "chan-1",
        "guild_id": "guild-1",
        "member": {"user": {"id": "user-1"}},
    }
    assert extract_interaction_id(payload) == "inter-1"
    assert extract_interaction_token(payload) == "token-1"
    assert extract_channel_id(payload) == "chan-1"
    assert extract_guild_id(payload) == "guild-1"
    assert extract_user_id(payload) == "user-1"


def test_is_component_interaction_returns_true_for_type_3() -> None:
    payload = {"type": 3, "data": {"custom_id": "bind_select"}}
    assert is_component_interaction(payload) is True


def test_is_component_interaction_returns_false_for_type_2() -> None:
    payload = {"type": 2, "data": {"name": "car"}}
    assert is_component_interaction(payload) is False


def test_extract_component_custom_id() -> None:
    payload = {"data": {"custom_id": "flow:run-123:resume"}}
    assert extract_component_custom_id(payload) == "flow:run-123:resume"


def test_extract_component_values() -> None:
    payload = {"data": {"values": ["repo-1", "repo-2"]}}
    assert extract_component_values(payload) == ["repo-1", "repo-2"]


def test_extract_component_values_returns_empty_for_no_values() -> None:
    payload = {"data": {}}
    assert extract_component_values(payload) == []
