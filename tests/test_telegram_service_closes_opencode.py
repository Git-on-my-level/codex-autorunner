import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from codex_autorunner.integrations.telegram import service as telegram_service_module
from codex_autorunner.integrations.telegram.config import TelegramBotConfig
from codex_autorunner.integrations.telegram.service import TelegramBotService


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


class StubOpenCodeSupervisor:
    def __init__(self) -> None:
        self.close_all_called = False
        self._handles: dict[str, Any] = {"ws1": MagicMock(), "ws2": MagicMock()}

    async def close_all(self) -> None:
        self.close_all_called = True

    async def prune_idle(self) -> int:
        return 0


def test_telegram_service_closes_opencode_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    config = _make_config(tmp_path)

    stub_supervisor = StubOpenCodeSupervisor()

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        service = TelegramBotService(config, hub_root=tmp_path)
    finally:
        asyncio.set_event_loop(None)
        loop.close()

    original_supervisor = service._opencode_supervisor
    service._opencode_supervisor = stub_supervisor  # type: ignore[attr-defined]

    assert service._opencode_supervisor is stub_supervisor
    assert not stub_supervisor.close_all_called

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(service._app_server_supervisor.close_all())
        if service._opencode_supervisor is not None:
            loop.run_until_complete(service._opencode_supervisor.close_all())
    finally:
        asyncio.set_event_loop(None)
        loop.close()

    assert stub_supervisor.close_all_called
    service._opencode_supervisor = original_supervisor  # type: ignore[attr-defined]


def test_telegram_service_uses_opencode_lifecycle_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    config = TelegramBotConfig.from_raw(
        {
            "enabled": True,
            "mode": "polling",
            "allowed_chat_ids": [123],
            "allowed_user_ids": [456],
            "app_server": {"max_handles": 3, "idle_ttl_seconds": 120},
        },
        root=tmp_path,
        env={
            "CAR_TELEGRAM_BOT_TOKEN": "test-token",
            "CAR_TELEGRAM_CHAT_ID": "123",
        },
        opencode_raw={"max_handles": 7, "idle_ttl_seconds": 2222},
    )
    captured: dict[str, Any] = {}

    def _fake_build_opencode_supervisor(**kwargs: Any) -> StubOpenCodeSupervisor:
        captured.update(kwargs)
        return StubOpenCodeSupervisor()

    monkeypatch.setattr(
        telegram_service_module,
        "build_opencode_supervisor",
        _fake_build_opencode_supervisor,
    )

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        service = TelegramBotService(config, hub_root=tmp_path)
    finally:
        asyncio.set_event_loop(None)
        loop.close()

    assert isinstance(service._opencode_supervisor, StubOpenCodeSupervisor)
    assert captured["max_handles"] == 7
    assert captured["idle_ttl_seconds"] == 2222
