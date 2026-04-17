from codex_autorunner.core.app_server_command import (
    DEFAULT_APP_SERVER_COMMAND,
    GLOBAL_APP_SERVER_COMMAND_ENV,
    LEGACY_TELEGRAM_APP_SERVER_COMMAND_ENV,
    iter_app_server_command_env_names,
    parse_command,
    resolve_app_server_command,
)


def test_resolve_app_server_command_prefers_global_env_over_legacy_and_config() -> None:
    command = resolve_app_server_command(
        ["config-codex", "app-server"],
        env={
            GLOBAL_APP_SERVER_COMMAND_ENV: "/opt/global/codex app-server --fast",
            LEGACY_TELEGRAM_APP_SERVER_COMMAND_ENV: "/opt/legacy/codex app-server",
        },
        extra_env_vars=[LEGACY_TELEGRAM_APP_SERVER_COMMAND_ENV],
    )

    assert command == ["/opt/global/codex", "app-server", "--fast"]


def test_resolve_app_server_command_falls_back_to_config_then_default() -> None:
    configured = resolve_app_server_command(
        ["config-codex", "app-server"],
        env={GLOBAL_APP_SERVER_COMMAND_ENV: '"unterminated'},
    )
    fallback = resolve_app_server_command(None, env={})

    assert configured == ["config-codex", "app-server"]
    assert fallback == list(DEFAULT_APP_SERVER_COMMAND)


class TestCommandPrecedenceChain:
    def test_global_env_wins_over_legacy_env(self) -> None:
        result = resolve_app_server_command(
            None,
            env={
                GLOBAL_APP_SERVER_COMMAND_ENV: "global-cmd",
                LEGACY_TELEGRAM_APP_SERVER_COMMAND_ENV: "legacy-cmd",
            },
            extra_env_vars=[LEGACY_TELEGRAM_APP_SERVER_COMMAND_ENV],
        )
        assert result == ["global-cmd"]

    def test_legacy_env_wins_over_config_when_no_global(self) -> None:
        result = resolve_app_server_command(
            ["config-cmd", "arg"],
            env={LEGACY_TELEGRAM_APP_SERVER_COMMAND_ENV: "legacy-cmd"},
            extra_env_vars=[LEGACY_TELEGRAM_APP_SERVER_COMMAND_ENV],
        )
        assert result == ["legacy-cmd"]

    def test_config_wins_when_no_env_set(self) -> None:
        result = resolve_app_server_command(
            ["config-cmd", "arg"],
            env={},
        )
        assert result == ["config-cmd", "arg"]

    def test_default_used_when_nothing_set(self) -> None:
        result = resolve_app_server_command(
            None,
            env={},
        )
        assert result == list(DEFAULT_APP_SERVER_COMMAND)

    def test_empty_global_env_skipped(self) -> None:
        result = resolve_app_server_command(
            ["config-cmd"],
            env={GLOBAL_APP_SERVER_COMMAND_ENV: "  "},
        )
        assert result == ["config-cmd"]

    def test_legacy_env_only_used_when_explicitly_passed(self) -> None:
        result = resolve_app_server_command(
            ["config-cmd"],
            env={LEGACY_TELEGRAM_APP_SERVER_COMMAND_ENV: "legacy-cmd"},
        )
        assert result == ["config-cmd"]


class TestIterEnvNamesContract:
    def test_global_always_first(self) -> None:
        names = iter_app_server_command_env_names("Z_CUSTOM", "Y_OTHER")
        assert names[0] == GLOBAL_APP_SERVER_COMMAND_ENV

    def test_extra_vars_appended_after_global(self) -> None:
        names = iter_app_server_command_env_names("Z_CUSTOM")
        assert names == (GLOBAL_APP_SERVER_COMMAND_ENV, "Z_CUSTOM")

    def test_none_and_empty_filtered(self) -> None:
        names = iter_app_server_command_env_names(None, "", "VALID")
        assert names == (GLOBAL_APP_SERVER_COMMAND_ENV, "VALID")

    def test_duplicates_removed(self) -> None:
        names = iter_app_server_command_env_names(GLOBAL_APP_SERVER_COMMAND_ENV)
        assert names.count(GLOBAL_APP_SERVER_COMMAND_ENV) == 1


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
