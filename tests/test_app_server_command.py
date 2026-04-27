from codex_autorunner.core.app_server_command import (
    DEFAULT_APP_SERVER_COMMAND,
    parse_command,
    resolve_app_server_command,
)


def test_resolve_app_server_command_uses_config_before_default() -> None:
    configured = resolve_app_server_command(["config-codex", "app-server"])
    fallback = resolve_app_server_command(None)

    assert configured == ["config-codex", "app-server"]
    assert fallback == list(DEFAULT_APP_SERVER_COMMAND)


def test_resolve_app_server_command_ignores_invalid_config_string() -> None:
    command = resolve_app_server_command('"unterminated', fallback=())
    assert command == []


class TestParseCommandEdgeCases:
    def test_none_returns_empty(self) -> None:
        assert parse_command(None) == []

    def test_empty_string_returns_empty(self) -> None:
        assert parse_command("") == []
        assert parse_command("   ") == []

    def test_list_input_passes_through(self) -> None:
        assert parse_command(["a", "b"]) == ["a", "b"]

    def test_non_string_non_list_returns_empty(self) -> None:
        assert parse_command(42) == []
