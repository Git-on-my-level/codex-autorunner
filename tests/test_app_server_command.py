from codex_autorunner.core.app_server_command import (
    DEFAULT_APP_SERVER_COMMAND,
    GLOBAL_APP_SERVER_COMMAND_ENV,
    LEGACY_TELEGRAM_APP_SERVER_COMMAND_ENV,
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
