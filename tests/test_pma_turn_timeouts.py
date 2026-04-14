from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from codex_autorunner.core.config import CONFIG_FILENAME, DEFAULT_HUB_CONFIG
from codex_autorunner.integrations.discord import message_turns as discord_message_turns
from codex_autorunner.integrations.telegram.handlers.commands import (
    execution as telegram_execution,
)
from codex_autorunner.surfaces.web.routes.pma_routes import (
    chat_runtime,
    managed_thread_runtime,
)
from tests.conftest import write_test_config


def _write_hub_config(root: Path, *, timeout_seconds: int) -> None:
    config = json.loads(json.dumps(DEFAULT_HUB_CONFIG))
    config.setdefault("pma", {})
    config["pma"]["turn_timeout_seconds"] = timeout_seconds
    write_test_config(root / CONFIG_FILENAME, config)


def test_discord_pma_turn_timeout_reads_hub_config(tmp_path: Path) -> None:
    _write_hub_config(tmp_path, timeout_seconds=123)

    service = SimpleNamespace(_config=SimpleNamespace(root=tmp_path))

    assert discord_message_turns._load_discord_pma_turn_timeout_seconds(service) == 123


def test_telegram_pma_turn_timeout_reads_hub_config(tmp_path: Path) -> None:
    _write_hub_config(tmp_path, timeout_seconds=234)

    handlers = SimpleNamespace(
        _hub_root=tmp_path,
        _config=SimpleNamespace(root=tmp_path),
        _hub_supervisor=None,
    )

    assert telegram_execution._load_telegram_pma_turn_timeout_seconds(handlers) == 234


def test_web_pma_turn_timeout_reads_request_config() -> None:
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                config=SimpleNamespace(pma=SimpleNamespace(turn_timeout_seconds=345))
            )
        )
    )

    assert chat_runtime._pma_turn_timeout_seconds(request) == 345
    assert managed_thread_runtime._pma_turn_timeout_seconds(request) == 345
