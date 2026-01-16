from pathlib import Path

import pytest

from codex_autorunner.integrations.telegram.config import (
    DEFAULT_APP_SERVER_COMMAND,
    DEFAULT_MEDIA_MAX_FILE_BYTES,
    DEFAULT_MESSAGE_OVERFLOW,
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
