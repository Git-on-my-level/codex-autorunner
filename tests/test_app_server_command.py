from codex_autorunner.core.app_server_command import (
    DEFAULT_APP_SERVER_COMMAND,
    parse_command,
    resolve_app_server_command,
    resolve_app_server_command_with_source,
)


def test_resolve_app_server_command_uses_config_before_default() -> None:
    configured = resolve_app_server_command(["config-codex", "app-server"])
    fallback = resolve_app_server_command(None)

    assert configured == ["config-codex", "app-server"]
    assert fallback == list(DEFAULT_APP_SERVER_COMMAND)


def test_resolve_app_server_command_ignores_invalid_config_string() -> None:
    command = resolve_app_server_command('"unterminated', fallback=())
    assert command == []


def test_resolve_app_server_command_with_source_prefers_codex_env() -> None:
    resolved = resolve_app_server_command_with_source(
        ["config-codex", "app-server"],
        env={
            "CAR_CODEX_APP_SERVER_COMMAND": "/opt/homebrew/bin/codex app-server",
            "CAR_TELEGRAM_APP_SERVER_COMMAND": "/old/node/codex app-server",
        },
    )

    assert resolved.command == ["/opt/homebrew/bin/codex", "app-server"]
    assert resolved.source == "env:CAR_CODEX_APP_SERVER_COMMAND"
    assert resolved.ignored_env == ("CAR_TELEGRAM_APP_SERVER_COMMAND",)


def test_resolve_app_server_command_with_source_ignores_telegram_env() -> None:
    resolved = resolve_app_server_command_with_source(
        ["config-codex", "app-server"],
        env={"CAR_TELEGRAM_APP_SERVER_COMMAND": "/old/node/codex app-server"},
    )

    assert resolved.command == ["config-codex", "app-server"]
    assert resolved.source == "config"
    assert resolved.ignored_env == ("CAR_TELEGRAM_APP_SERVER_COMMAND",)


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
