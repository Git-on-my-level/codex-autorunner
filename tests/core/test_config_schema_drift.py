from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.agent_config import (
    SHARED_AGENT_PARSER_FIELD_PATHS,
    parse_agents_config,
)
from codex_autorunner.core.config import DEFAULT_REPO_CONFIG
from codex_autorunner.core.config_field_schema import SHARED_SCHEMA_FIELD_PATHS
from codex_autorunner.core.config_parsers import (
    SHARED_CONFIG_PARSER_FIELD_PATHS,
    _parse_app_server_config,
    _parse_update_backend,
)


def test_shared_config_schema_parser_and_validator_coverage_stay_in_lockstep() -> None:
    parser_paths = SHARED_CONFIG_PARSER_FIELD_PATHS | SHARED_AGENT_PARSER_FIELD_PATHS

    assert parser_paths == SHARED_SCHEMA_FIELD_PATHS


def test_update_backend_compatibility_repair_falls_back_to_auto() -> None:
    assert _parse_update_backend({"backend": ""}) == "auto"
    assert _parse_update_backend({"backend": "unsupported"}) == "auto"


def test_agent_optional_string_fields_use_explicit_schema_repair() -> None:
    parsed = parse_agents_config(
        {
            "agents": {
                "hermes": {
                    "binary": "hermes",
                    "base_url": 123,
                    "profiles": {
                        "m4": {
                            "binary": "hermes-m4",
                            "display_name": "  ",
                        }
                    },
                }
            }
        },
        {},
    )

    assert parsed["hermes"].base_url is None
    assert parsed["hermes"].profiles is not None
    assert parsed["hermes"].profiles["m4"].display_name is None


def test_app_server_numeric_repair_uses_declared_defaults(tmp_path: Path) -> None:
    parsed = _parse_app_server_config(
        {
            "turn_stall_poll_interval_seconds": 0,
            "client": {"max_message_bytes": 0},
        },
        tmp_path,
        DEFAULT_REPO_CONFIG["app_server"],
    )

    assert parsed.turn_stall_poll_interval_seconds == 2
    assert parsed.client.max_message_bytes == 50 * 1024 * 1024
