from pathlib import Path

import pytest

from codex_autorunner.core.app_server_command import GLOBAL_APP_SERVER_COMMAND_ENV
from codex_autorunner.integrations.telegram.config import (
    DEFAULT_APP_SERVER_COMMAND,
    DEFAULT_MEDIA_MAX_FILE_BYTES,
    DEFAULT_MESSAGE_OVERFLOW,
    DEFAULT_METRICS_MODE,
    DEFAULT_OPENCODE_IDLE_TTL_SECONDS,
    DEFAULT_OPENCODE_MAX_HANDLES,
    TelegramBotConfig,
    TelegramBotConfigError,
)


def test_telegram_bot_config_env_resolution(tmp_path: Path) -> None:
    raw = {
        "enabled": True,
        "bot_token_env": "TEST_BOT_TOKEN",
        "chat_id_env": "TEST_CHAT_ID",
        "allowed_user_ids": [123],
    }
    env = {
        "TEST_BOT_TOKEN": "token",
        "TEST_CHAT_ID": "-100",
    }
    cfg = TelegramBotConfig.from_raw(raw, root=tmp_path, env=env)
    assert cfg.bot_token == "token"
    assert cfg.allowed_chat_ids == {-100}
    assert cfg.allowed_user_ids == {123}
    assert cfg.app_server_command == list(DEFAULT_APP_SERVER_COMMAND)
    assert cfg.shell.enabled is False
    assert cfg.default_notification_chat_id == -100


def test_telegram_bot_config_app_server_command_env_override(tmp_path: Path) -> None:
    raw = {
        "enabled": True,
        "bot_token_env": "TEST_BOT_TOKEN",
        "chat_id_env": "TEST_CHAT_ID",
        "allowed_user_ids": [123],
        "app_server_command_env": "TEST_APP_SERVER_COMMAND",
        "app_server_command": ["config-codex", "app-server"],
    }
    env = {
        "TEST_BOT_TOKEN": "token",
        "TEST_CHAT_ID": "-100",
        "TEST_APP_SERVER_COMMAND": "/opt/codex/bin/codex app-server --flag",
    }
    cfg = TelegramBotConfig.from_raw(raw, root=tmp_path, env=env)
    assert cfg.app_server_command == ["/opt/codex/bin/codex", "app-server", "--flag"]


def test_telegram_bot_config_prefers_global_app_server_command_env(
    tmp_path: Path,
) -> None:
    raw = {
        "enabled": True,
        "bot_token_env": "TEST_BOT_TOKEN",
        "chat_id_env": "TEST_CHAT_ID",
        "allowed_user_ids": [123],
        "app_server_command_env": "TEST_APP_SERVER_COMMAND",
        "app_server_command": ["config-codex", "app-server"],
    }
    env = {
        "TEST_BOT_TOKEN": "token",
        "TEST_CHAT_ID": "-100",
        "TEST_APP_SERVER_COMMAND": "/opt/legacy/codex app-server",
        GLOBAL_APP_SERVER_COMMAND_ENV: "/opt/global/codex app-server --flag",
    }

    cfg = TelegramBotConfig.from_raw(raw, root=tmp_path, env=env)

    assert cfg.app_server_command == ["/opt/global/codex", "app-server", "--flag"]


def test_telegram_bot_config_invalid_env_override_falls_back_to_config(
    tmp_path: Path,
) -> None:
    raw = {
        "enabled": True,
        "bot_token_env": "TEST_BOT_TOKEN",
        "chat_id_env": "TEST_CHAT_ID",
        "allowed_user_ids": [123],
        "app_server_command_env": "TEST_APP_SERVER_COMMAND",
        "app_server_command": ["config-codex", "app-server"],
    }
    env = {
        "TEST_BOT_TOKEN": "token",
        "TEST_CHAT_ID": "-100",
        "TEST_APP_SERVER_COMMAND": '"unterminated',
    }

    cfg = TelegramBotConfig.from_raw(raw, root=tmp_path, env=env)

    assert cfg.app_server_command == ["config-codex", "app-server"]


def test_telegram_bot_config_uses_explicit_opencode_lifecycle_settings(
    tmp_path: Path,
) -> None:
    raw = {
        "enabled": True,
        "bot_token_env": "TEST_BOT_TOKEN",
        "chat_id_env": "TEST_CHAT_ID",
        "allowed_user_ids": [123],
        "app_server": {"max_handles": 3, "idle_ttl_seconds": 120},
    }
    env = {
        "TEST_BOT_TOKEN": "token",
        "TEST_CHAT_ID": "-100",
    }

    cfg = TelegramBotConfig.from_raw(
        raw,
        root=tmp_path,
        env=env,
        opencode_raw={"max_handles": 7, "idle_ttl_seconds": 2222},
    )

    assert cfg.app_server_max_handles == 3
    assert cfg.app_server_idle_ttl_seconds == 120
    assert cfg.opencode_max_handles == 7
    assert cfg.opencode_idle_ttl_seconds == 2222


def test_telegram_bot_config_uses_tighter_default_opencode_lifecycle_settings(
    tmp_path: Path,
) -> None:
    raw = {
        "enabled": True,
        "bot_token_env": "TEST_BOT_TOKEN",
        "chat_id_env": "TEST_CHAT_ID",
        "allowed_user_ids": [123],
    }
    env = {
        "TEST_BOT_TOKEN": "token",
        "TEST_CHAT_ID": "-100",
    }

    cfg = TelegramBotConfig.from_raw(raw, root=tmp_path, env=env)

    assert cfg.opencode_max_handles == DEFAULT_OPENCODE_MAX_HANDLES == 4
    assert cfg.opencode_idle_ttl_seconds == DEFAULT_OPENCODE_IDLE_TTL_SECONDS == 900


def test_telegram_bot_config_validate_requires_allowlist(tmp_path: Path) -> None:
    raw = {
        "enabled": True,
        "bot_token_env": "TEST_BOT_TOKEN",
        "chat_id_env": "TEST_CHAT_ID",
        "allowed_user_ids": [],
    }
    env = {
        "TEST_BOT_TOKEN": "token",
        "TEST_CHAT_ID": "",
    }
    cfg = TelegramBotConfig.from_raw(raw, root=tmp_path, env=env)
    with pytest.raises(TelegramBotConfigError):
        cfg.validate()


def test_telegram_bot_config_validate_poll_timeout(tmp_path: Path) -> None:
    raw = {
        "enabled": True,
        "bot_token_env": "TEST_BOT_TOKEN",
        "chat_id_env": "TEST_CHAT_ID",
        "allowed_user_ids": [123],
        "polling": {"timeout_seconds": 0},
    }
    env = {
        "TEST_BOT_TOKEN": "token",
        "TEST_CHAT_ID": "123",
    }
    cfg = TelegramBotConfig.from_raw(raw, root=tmp_path, env=env)
    with pytest.raises(TelegramBotConfigError):
        cfg.validate()


def test_telegram_bot_config_validate_request_timeout(tmp_path: Path) -> None:
    raw = {
        "enabled": True,
        "bot_token_env": "TEST_BOT_TOKEN",
        "chat_id_env": "TEST_CHAT_ID",
        "allowed_user_ids": [123],
        "polling": {"timeout_seconds": 30, "request_timeout_seconds": 30},
    }
    env = {
        "TEST_BOT_TOKEN": "token",
        "TEST_CHAT_ID": "123",
    }
    cfg = TelegramBotConfig.from_raw(raw, root=tmp_path, env=env)
    with pytest.raises(TelegramBotConfigError):
        cfg.validate()


def test_telegram_bot_config_request_timeout_override(tmp_path: Path) -> None:
    raw = {
        "enabled": True,
        "bot_token_env": "TEST_BOT_TOKEN",
        "chat_id_env": "TEST_CHAT_ID",
        "allowed_user_ids": [123],
        "polling": {"timeout_seconds": 30, "request_timeout_seconds": 45},
    }
    env = {
        "TEST_BOT_TOKEN": "token",
        "TEST_CHAT_ID": "123",
    }
    cfg = TelegramBotConfig.from_raw(raw, root=tmp_path, env=env)
    assert cfg.poll_request_timeout_seconds == 45.0


def test_telegram_bot_config_debug_prefix(tmp_path: Path) -> None:
    raw = {
        "enabled": True,
        "bot_token_env": "TEST_BOT_TOKEN",
        "chat_id_env": "TEST_CHAT_ID",
        "allowed_user_ids": [123],
        "debug": {"prefix_context": True},
    }
    env = {
        "TEST_BOT_TOKEN": "token",
        "TEST_CHAT_ID": "123",
    }
    cfg = TelegramBotConfig.from_raw(raw, root=tmp_path, env=env)
    assert cfg.debug_prefix_context is True


def test_telegram_bot_config_shell_overrides(tmp_path: Path) -> None:
    raw = {
        "enabled": True,
        "bot_token_env": "TEST_BOT_TOKEN",
        "chat_id_env": "TEST_CHAT_ID",
        "allowed_user_ids": [123],
        "shell": {"enabled": True, "timeout_ms": 5000, "max_output_chars": 123},
    }
    env = {
        "TEST_BOT_TOKEN": "token",
        "TEST_CHAT_ID": "123",
    }
    cfg = TelegramBotConfig.from_raw(raw, root=tmp_path, env=env)
    assert cfg.shell.enabled is True
    assert cfg.shell.timeout_ms == 5000
    assert cfg.shell.max_output_chars == 123


def test_telegram_bot_config_command_registration_defaults(tmp_path: Path) -> None:
    raw = {
        "enabled": True,
        "bot_token_env": "TEST_BOT_TOKEN",
        "chat_id_env": "TEST_CHAT_ID",
        "allowed_user_ids": [123],
    }
    env = {
        "TEST_BOT_TOKEN": "token",
        "TEST_CHAT_ID": "123",
    }
    cfg = TelegramBotConfig.from_raw(raw, root=tmp_path, env=env)
    registration = cfg.command_registration
    assert registration.enabled is True
    assert [scope.scope["type"] for scope in registration.scopes] == [
        "default",
        "all_group_chats",
    ]


def test_telegram_bot_config_media_file_defaults(tmp_path: Path) -> None:
    raw = {
        "enabled": True,
        "bot_token_env": "TEST_BOT_TOKEN",
        "chat_id_env": "TEST_CHAT_ID",
        "allowed_user_ids": [123],
    }
    env = {
        "TEST_BOT_TOKEN": "token",
        "TEST_CHAT_ID": "123",
    }
    cfg = TelegramBotConfig.from_raw(raw, root=tmp_path, env=env)
    assert cfg.media.files is True
    assert cfg.media.max_file_bytes == DEFAULT_MEDIA_MAX_FILE_BYTES
    assert cfg.pause_dispatch_notifications.enabled is True
    assert cfg.pause_dispatch_notifications.send_attachments is True
    assert cfg.pause_dispatch_notifications.max_file_size_bytes == 50 * 1024 * 1024
    assert cfg.pause_dispatch_notifications.chunk_long_messages is True
    assert cfg.default_notification_chat_id == 123


def test_telegram_bot_config_message_overflow_default(tmp_path: Path) -> None:
    raw = {
        "enabled": True,
        "bot_token_env": "TEST_BOT_TOKEN",
        "chat_id_env": "TEST_CHAT_ID",
        "allowed_user_ids": [123],
    }
    env = {
        "TEST_BOT_TOKEN": "token",
        "TEST_CHAT_ID": "123",
    }
    cfg = TelegramBotConfig.from_raw(raw, root=tmp_path, env=env)
    assert cfg.message_overflow == DEFAULT_MESSAGE_OVERFLOW


def test_telegram_bot_config_rejects_json_state_file(tmp_path: Path) -> None:
    raw = {
        "enabled": True,
        "bot_token_env": "TEST_BOT_TOKEN",
        "chat_id_env": "TEST_CHAT_ID",
        "allowed_user_ids": [123],
        "state_file": "telegram_state.json",
    }
    env = {
        "TEST_BOT_TOKEN": "token",
        "TEST_CHAT_ID": "123",
    }
    with pytest.raises(TelegramBotConfigError):
        TelegramBotConfig.from_raw(raw, root=tmp_path, env=env)


def test_telegram_bot_config_message_overflow_override(tmp_path: Path) -> None:
    raw = {
        "enabled": True,
        "bot_token_env": "TEST_BOT_TOKEN",
        "chat_id_env": "TEST_CHAT_ID",
        "allowed_user_ids": [123],
        "message_overflow": "split",
    }
    env = {
        "TEST_BOT_TOKEN": "token",
        "TEST_CHAT_ID": "123",
    }
    cfg = TelegramBotConfig.from_raw(raw, root=tmp_path, env=env)
    assert cfg.message_overflow == "split"


def test_telegram_bot_config_default_notification_chat_id(tmp_path: Path) -> None:
    raw = {
        "enabled": True,
        "bot_token_env": "TEST_BOT_TOKEN",
        "chat_id_env": "TEST_CHAT_ID",
        "allowed_user_ids": [123],
        "default_notification_chat_id": "42",
        "pause_dispatch_notifications": {"enabled": True},
    }
    env = {"TEST_BOT_TOKEN": "token", "TEST_CHAT_ID": "123"}
    cfg = TelegramBotConfig.from_raw(raw, root=tmp_path, env=env)
    assert cfg.default_notification_chat_id == 42


def test_telegram_bot_config_invalid_default_chat_falls_back_to_env(
    tmp_path: Path,
) -> None:
    raw = {
        "enabled": True,
        "bot_token_env": "TEST_BOT_TOKEN",
        "chat_id_env": "TEST_CHAT_ID",
        "allowed_user_ids": [123],
        "default_notification_chat_id": "not-a-number",
    }
    env = {"TEST_BOT_TOKEN": "token", "TEST_CHAT_ID": "321"}
    cfg = TelegramBotConfig.from_raw(raw, root=tmp_path, env=env)
    assert cfg.default_notification_chat_id == 321


def test_telegram_bot_config_falls_back_to_allowed_chat_ids(tmp_path: Path) -> None:
    raw = {
        "enabled": True,
        "bot_token_env": "TEST_BOT_TOKEN",
        "chat_id_env": "TEST_CHAT_ID",
        "allowed_chat_ids": [999, 111],
        "allowed_user_ids": [123],
    }
    env = {"TEST_BOT_TOKEN": "token", "TEST_CHAT_ID": ""}
    cfg = TelegramBotConfig.from_raw(raw, root=tmp_path, env=env)
    assert cfg.allowed_chat_ids == {111, 999}
    assert cfg.default_notification_chat_id == 111


def test_telegram_bot_config_metrics_mode_default(tmp_path: Path) -> None:
    raw = {
        "enabled": True,
        "bot_token_env": "TEST_BOT_TOKEN",
        "chat_id_env": "TEST_CHAT_ID",
        "allowed_user_ids": [123],
    }
    env = {
        "TEST_BOT_TOKEN": "token",
        "TEST_CHAT_ID": "123",
    }
    cfg = TelegramBotConfig.from_raw(raw, root=tmp_path, env=env)
    assert cfg.metrics_mode == DEFAULT_METRICS_MODE


def test_telegram_bot_config_metrics_mode_override(tmp_path: Path) -> None:
    raw = {
        "enabled": True,
        "bot_token_env": "TEST_BOT_TOKEN",
        "chat_id_env": "TEST_CHAT_ID",
        "allowed_user_ids": [123],
        "metrics": {"mode": "append_to_response"},
    }
    env = {
        "TEST_BOT_TOKEN": "token",
        "TEST_CHAT_ID": "123",
    }
    cfg = TelegramBotConfig.from_raw(raw, root=tmp_path, env=env)
    assert cfg.metrics_mode == "append_to_response"


def test_telegram_bot_config_preserves_codex_no_timeout(tmp_path: Path) -> None:
    raw = {
        "enabled": True,
        "bot_token_env": "TEST_BOT_TOKEN",
        "chat_id_env": "TEST_CHAT_ID",
        "allowed_user_ids": [123],
        "app_server": {"turn_timeout_seconds": None},
    }
    env = {"TEST_BOT_TOKEN": "token", "TEST_CHAT_ID": "123"}
    cfg = TelegramBotConfig.from_raw(raw, root=tmp_path, env=env)
    assert cfg.app_server_turn_timeout_seconds is None
    assert cfg.agent_turn_timeout_seconds["codex"] is None


def test_telegram_bot_config_inherits_app_server_timeout(tmp_path: Path) -> None:
    raw = {
        "enabled": True,
        "bot_token_env": "TEST_BOT_TOKEN",
        "chat_id_env": "TEST_CHAT_ID",
        "allowed_user_ids": [123],
        "app_server": {"turn_timeout_seconds": 123},
    }
    env = {"TEST_BOT_TOKEN": "token", "TEST_CHAT_ID": "123"}
    cfg = TelegramBotConfig.from_raw(raw, root=tmp_path, env=env)
    assert cfg.app_server_turn_timeout_seconds == 123
    assert cfg.agent_turn_timeout_seconds["codex"] == 123


def test_telegram_bot_config_validate_trigger_mode(tmp_path: Path) -> None:
    raw = {
        "enabled": True,
        "bot_token_env": "TEST_BOT_TOKEN",
        "chat_id_env": "TEST_CHAT_ID",
        "allowed_user_ids": [123],
        "trigger_mode": "nope",
    }
    env = {
        "TEST_BOT_TOKEN": "token",
        "TEST_CHAT_ID": "123",
    }
    cfg = TelegramBotConfig.from_raw(raw, root=tmp_path, env=env)
    with pytest.raises(TelegramBotConfigError):
        cfg.validate()


def test_telegram_bot_config_builds_shared_collaboration_policy(
    tmp_path: Path,
) -> None:
    raw = {
        "enabled": True,
        "bot_token_env": "TEST_BOT_TOKEN",
        "chat_id_env": "TEST_CHAT_ID",
        "allowed_user_ids": [123],
        "allowed_chat_ids": [-1001],
    }
    env = {"TEST_BOT_TOKEN": "token", "TEST_CHAT_ID": ""}
    cfg = TelegramBotConfig.from_raw(
        raw,
        root=tmp_path,
        env=env,
        collaboration_raw={
            "telegram": {
                "default_mode": "active",
                "destinations": [
                    {
                        "chat_id": -1001,
                        "thread_id": 7,
                        "mode": "command_only",
                        "trigger_mode": "all",
                    }
                ],
            }
        },
    )
    assert cfg.collaboration_policy is not None
    assert cfg.collaboration_policy.default_plain_text_trigger == "always"
    assert cfg.collaboration_policy.destinations[0].mode == "command_only"


def test_telegram_bot_config_rejects_invalid_collaboration_policy(
    tmp_path: Path,
) -> None:
    raw = {
        "enabled": True,
        "bot_token_env": "TEST_BOT_TOKEN",
        "chat_id_env": "TEST_CHAT_ID",
        "allowed_user_ids": [123],
        "allowed_chat_ids": [-1001],
    }
    env = {"TEST_BOT_TOKEN": "token", "TEST_CHAT_ID": ""}
    with pytest.raises(TelegramBotConfigError):
        TelegramBotConfig.from_raw(
            raw,
            root=tmp_path,
            env=env,
            collaboration_raw={
                "telegram": {
                    "destinations": [
                        {
                            "chat_id": -1001,
                            "mode": "nope",
                        }
                    ]
                }
            },
        )
