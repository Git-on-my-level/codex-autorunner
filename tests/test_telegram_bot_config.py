from pathlib import Path

import pytest

from codex_autorunner.telegram_bot import (
    DEFAULT_APP_SERVER_COMMAND,
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
