import asyncio
import json
import os
from pathlib import Path

import pytest

from codex_autorunner.telegram_bot import (
    TelegramBotConfig,
    TelegramBotLockError,
    TelegramBotService,
    _telegram_lock_path,
)


def _make_config(root: Path) -> TelegramBotConfig:
    raw = {
        "enabled": True,
        "mode": "polling",
        "allowed_chat_ids": [123],
        "allowed_user_ids": [456],
        "require_topics": False,
    }
    env = {
        "CAR_TELEGRAM_BOT_TOKEN": "test-token",
        "CAR_TELEGRAM_CHAT_ID": "123",
    }
    return TelegramBotConfig.from_raw(raw, root=root, env=env)


def _build_service_in_closed_loop(
    tmp_path: Path, config: TelegramBotConfig
) -> TelegramBotService:
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return TelegramBotService(config, hub_root=tmp_path)
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def test_telegram_bot_lock_acquire_and_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    config = _make_config(tmp_path)
    service = _build_service_in_closed_loop(tmp_path, config)
    assert config.bot_token
    lock_path = _telegram_lock_path(config.bot_token)
    try:
        service._acquire_instance_lock()
        assert lock_path.exists()
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        assert payload.get("pid") == os.getpid()
        assert payload.get("config_root") == str(tmp_path)
    finally:
        service._release_instance_lock()
        asyncio.run(service._app_server_supervisor.close_all())
    assert not lock_path.exists()


def test_telegram_bot_lock_contended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    config = _make_config(tmp_path)
    assert config.bot_token
    lock_path = _telegram_lock_path(config.bot_token)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "started_at": "now",
                "host": "test-host",
                "cwd": str(tmp_path),
                "config_root": str(tmp_path),
            }
        ),
        encoding="utf-8",
    )
    service = _build_service_in_closed_loop(tmp_path, config)
    try:
        with pytest.raises(TelegramBotLockError):
            service._acquire_instance_lock()
    finally:
        asyncio.run(service._app_server_supervisor.close_all())
    assert lock_path.exists()
