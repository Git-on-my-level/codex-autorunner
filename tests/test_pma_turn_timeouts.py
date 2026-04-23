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
    config["pma"]["turn_idle_timeout_seconds"] = timeout_seconds
    write_test_config(root / CONFIG_FILENAME, config)


def test_discord_pma_turn_idle_timeout_reads_hub_config(tmp_path: Path) -> None:
    _write_hub_config(tmp_path, timeout_seconds=123)

    service = SimpleNamespace(_config=SimpleNamespace(root=tmp_path))

    assert (
        discord_message_turns._load_discord_pma_turn_idle_timeout_seconds(service)
        == 123
    )


def test_discord_repo_managed_thread_coordinator_ignores_pma_turn_timeout(
    tmp_path: Path,
) -> None:
    """Repo-mode Discord turns keep the legacy 7200s cap; PMA config is PMA-only."""
    _write_hub_config(tmp_path, timeout_seconds=30)

    service = SimpleNamespace(_config=SimpleNamespace(root=tmp_path))
    coordinator = discord_message_turns._build_discord_managed_thread_coordinator(
        service=service,
        orchestration_service=SimpleNamespace(),
        channel_id="channel-1",
        public_execution_error="e",
        timeout_error="t",
        interrupted_error="i",
        pma_enabled=False,
    )
    assert coordinator.errors.timeout_seconds == 7200.0


def test_telegram_pma_turn_idle_timeout_reads_hub_config(tmp_path: Path) -> None:
    _write_hub_config(tmp_path, timeout_seconds=234)

    handlers = SimpleNamespace(
        _hub_root=tmp_path,
        _config=SimpleNamespace(root=tmp_path),
        _hub_supervisor=None,
    )

    assert (
        telegram_execution._load_telegram_pma_turn_idle_timeout_seconds(handlers) == 234
    )


def test_telegram_repo_managed_thread_coordinator_ignores_pma_turn_timeout(
    tmp_path: Path,
) -> None:
    """Repo-mode Telegram turns keep the legacy 7200s cap; PMA config is PMA-only."""
    _write_hub_config(tmp_path, timeout_seconds=30)

    handlers = SimpleNamespace(
        _hub_root=tmp_path,
        _config=SimpleNamespace(root=tmp_path),
        _hub_supervisor=None,
    )
    coordinator = telegram_execution._build_telegram_managed_thread_coordinator(
        handlers,
        orchestration_service=SimpleNamespace(),
        surface_key="telegram:-1:1",
        chat_id=-1,
        thread_id=1,
        public_execution_error="e",
        timeout_error="t",
        interrupted_error="i",
        pma_enabled=False,
    )
    assert coordinator.errors.timeout_seconds == 7200.0


def test_web_pma_turn_idle_timeout_reads_request_config() -> None:
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                config=SimpleNamespace(
                    pma=SimpleNamespace(turn_idle_timeout_seconds=345)
                )
            )
        )
    )

    assert chat_runtime._pma_turn_idle_timeout_seconds(request) == 345
    assert managed_thread_runtime._pma_turn_idle_timeout_seconds(request) == 345


class TestPmaTimeoutIsolationInvariants:
    def test_discord_pma_surface_uses_hub_config_timeout(self, tmp_path: Path) -> None:
        _write_hub_config(tmp_path, timeout_seconds=42)
        service = SimpleNamespace(_config=SimpleNamespace(root=tmp_path))
        coordinator = discord_message_turns._build_discord_managed_thread_coordinator(
            service=service,
            orchestration_service=SimpleNamespace(),
            channel_id="channel-1",
            public_execution_error="e",
            timeout_error="t",
            interrupted_error="i",
            pma_enabled=True,
        )
        assert coordinator.errors.timeout_seconds == 42.0
        assert coordinator.errors.stall_timeout_seconds == 42.0

    def test_telegram_pma_surface_uses_hub_config_timeout(self, tmp_path: Path) -> None:
        _write_hub_config(tmp_path, timeout_seconds=55)
        handlers = SimpleNamespace(
            _hub_root=tmp_path,
            _config=SimpleNamespace(root=tmp_path),
            _hub_supervisor=None,
        )
        coordinator = telegram_execution._build_telegram_managed_thread_coordinator(
            handlers,
            orchestration_service=SimpleNamespace(),
            surface_key="telegram:-1:1",
            chat_id=-1,
            thread_id=1,
            public_execution_error="e",
            timeout_error="t",
            interrupted_error="i",
            pma_enabled=True,
        )
        assert coordinator.errors.timeout_seconds == 55.0
        assert coordinator.errors.stall_timeout_seconds == 55.0

    def test_discord_repo_surface_always_uses_legacy_7200_regardless_of_config(
        self, tmp_path: Path
    ) -> None:
        _write_hub_config(tmp_path, timeout_seconds=5)
        service = SimpleNamespace(_config=SimpleNamespace(root=tmp_path))
        coordinator = discord_message_turns._build_discord_managed_thread_coordinator(
            service=service,
            orchestration_service=SimpleNamespace(),
            channel_id="channel-1",
            public_execution_error="e",
            timeout_error="t",
            interrupted_error="i",
            pma_enabled=False,
        )
        assert coordinator.errors.timeout_seconds == 7200.0

    def test_discord_pma_surface_stall_timeout_caps_at_total_timeout(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _write_hub_config(tmp_path, timeout_seconds=42)
        service = SimpleNamespace(_config=SimpleNamespace(root=tmp_path))
        monkeypatch.setattr(
            discord_message_turns,
            "DISCORD_PMA_STALL_TIMEOUT_SECONDS",
            90.0,
        )
        coordinator = discord_message_turns._build_discord_managed_thread_coordinator(
            service=service,
            orchestration_service=SimpleNamespace(),
            channel_id="channel-1",
            public_execution_error="e",
            timeout_error="t",
            interrupted_error="i",
            pma_enabled=True,
        )
        assert coordinator.errors.timeout_seconds == 42.0
        assert coordinator.errors.stall_timeout_seconds == 42.0

    def test_telegram_repo_surface_always_uses_legacy_7200_regardless_of_config(
        self, tmp_path: Path
    ) -> None:
        _write_hub_config(tmp_path, timeout_seconds=5)
        handlers = SimpleNamespace(
            _hub_root=tmp_path,
            _config=SimpleNamespace(root=tmp_path),
            _hub_supervisor=None,
        )
        coordinator = telegram_execution._build_telegram_managed_thread_coordinator(
            handlers,
            orchestration_service=SimpleNamespace(),
            surface_key="telegram:-1:1",
            chat_id=-1,
            thread_id=1,
            public_execution_error="e",
            timeout_error="t",
            interrupted_error="i",
            pma_enabled=False,
        )
        assert coordinator.errors.timeout_seconds == 7200.0
