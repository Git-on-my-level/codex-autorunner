from __future__ import annotations

from codex_autorunner.integrations.discord.interactions import (
    extract_channel_id,
    extract_command_path_and_options,
    extract_guild_id,
    extract_interaction_id,
    extract_interaction_token,
    extract_user_id,
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
