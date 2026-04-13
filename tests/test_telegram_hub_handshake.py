from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.core.hub_control_plane.errors import HubControlPlaneError
from codex_autorunner.core.orchestration import ORCHESTRATION_SCHEMA_VERSION
from codex_autorunner.integrations.telegram.config import TelegramBotConfig
from codex_autorunner.integrations.telegram.service import TelegramBotService


def _config(root: Path) -> TelegramBotConfig:
    return TelegramBotConfig.from_raw(
        {
            "enabled": True,
            "mode": "polling",
            "allowed_chat_ids": [123],
            "allowed_user_ids": [456],
            "require_topics": False,
        },
        root=root,
        env={"CAR_TELEGRAM_BOT_TOKEN": "test-token"},
    )


def _logged_events(
    caplog: pytest.LogCaptureFixture, logger_name: str
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for record in caplog.records:
        if record.name != logger_name:
            continue
        message = record.getMessage()
        if not message.startswith("{"):
            continue
        events.append(json.loads(message))
    return events


@pytest.mark.anyio
async def test_telegram_handshake_compatible_hub(tmp_path: Path) -> None:
    seed_hub_files(tmp_path, force=True)
    captured_request: dict[str, Any] = {}

    class _FakeHubClient:
        async def handshake(self, request: Any) -> Any:
            captured_request["request"] = request
            return SimpleNamespace(
                api_version="1.0.0",
                minimum_client_api_version="1.0.0",
                schema_generation=ORCHESTRATION_SCHEMA_VERSION,
                capabilities=("compatibility_handshake",),
                hub_build_version="0.0.0",
                hub_asset_version=None,
            )

    service = TelegramBotService(_config(tmp_path), hub_root=tmp_path)
    service._hub_client = _FakeHubClient()

    try:
        handshake_ok = await service._perform_hub_handshake()

        assert handshake_ok is True
        assert service._hub_handshake_compatibility is not None
        assert service._hub_handshake_compatibility.compatible is True
        assert service._hub_handshake_compatibility.state == "compatible"
        assert (
            captured_request["request"].expected_schema_generation
            == ORCHESTRATION_SCHEMA_VERSION
        )
    finally:
        await service._app_server_supervisor.close_all()
        await service._bot.close()


@pytest.mark.anyio
async def test_telegram_handshake_incompatible_hub(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    seed_hub_files(tmp_path, force=True)
    captured_request: dict[str, Any] = {}

    class _FakeIncompatibleHubClient:
        async def handshake(self, request: Any) -> Any:
            captured_request["request"] = request
            return SimpleNamespace(
                api_version="99.0.0",
                minimum_client_api_version="99.0.0",
                schema_generation=ORCHESTRATION_SCHEMA_VERSION,
                capabilities=(),
                hub_build_version=None,
                hub_asset_version=None,
            )

    logger = logging.getLogger("test.telegram.handshake.incompatible")
    service = TelegramBotService(_config(tmp_path), logger=logger, hub_root=tmp_path)
    service._hub_client = _FakeIncompatibleHubClient()

    try:
        with caplog.at_level(logging.INFO, logger=logger.name):
            handshake_ok = await service._perform_hub_handshake()

        assert handshake_ok is False
        assert service._hub_handshake_compatibility is not None
        assert service._hub_handshake_compatibility.compatible is False
        assert service._hub_handshake_compatibility.state == "incompatible"
        assert "major version mismatch" in (
            service._hub_handshake_compatibility.reason or ""
        )
        assert _logged_events(caplog, logger.name)[-1] == {
            "event": "telegram.hub_control_plane.handshake_incompatible",
            "hub_root": str(tmp_path),
            "reason": "control-plane API major version mismatch",
            "server_api_version": "99.0.0",
            "client_api_version": "1.0.0",
            "server_schema_generation": ORCHESTRATION_SCHEMA_VERSION,
            "expected_schema_generation": ORCHESTRATION_SCHEMA_VERSION,
        }
        assert (
            captured_request["request"].expected_schema_generation
            == ORCHESTRATION_SCHEMA_VERSION
        )
    finally:
        await service._app_server_supervisor.close_all()
        await service._bot.close()


@pytest.mark.anyio
async def test_telegram_handshake_rejects_schema_generation_mismatch(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    seed_hub_files(tmp_path, force=True)
    captured_request: dict[str, Any] = {}

    class _FakeMismatchedSchemaHubClient:
        async def handshake(self, request: Any) -> Any:
            captured_request["request"] = request
            return SimpleNamespace(
                api_version="1.0.0",
                minimum_client_api_version="1.0.0",
                schema_generation=ORCHESTRATION_SCHEMA_VERSION + 1,
                capabilities=("compatibility_handshake",),
                hub_build_version=None,
                hub_asset_version=None,
            )

    logger = logging.getLogger("test.telegram.handshake.schema_mismatch")
    service = TelegramBotService(_config(tmp_path), logger=logger, hub_root=tmp_path)
    service._hub_client = _FakeMismatchedSchemaHubClient()

    try:
        with caplog.at_level(logging.INFO, logger=logger.name):
            handshake_ok = await service._perform_hub_handshake()

        assert handshake_ok is False
        assert service._hub_handshake_compatibility is not None
        assert service._hub_handshake_compatibility.state == "incompatible"
        assert service._hub_handshake_compatibility.reason == (
            "orchestration schema generation mismatch"
        )
        assert _logged_events(caplog, logger.name)[-1] == {
            "event": "telegram.hub_control_plane.handshake_incompatible",
            "hub_root": str(tmp_path),
            "reason": "orchestration schema generation mismatch",
            "server_api_version": "1.0.0",
            "client_api_version": "1.0.0",
            "server_schema_generation": ORCHESTRATION_SCHEMA_VERSION + 1,
            "expected_schema_generation": ORCHESTRATION_SCHEMA_VERSION,
        }
        assert (
            captured_request["request"].expected_schema_generation
            == ORCHESTRATION_SCHEMA_VERSION
        )
    finally:
        await service._app_server_supervisor.close_all()
        await service._bot.close()


@pytest.mark.anyio
async def test_telegram_handshake_hub_unavailable(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    seed_hub_files(tmp_path, force=True)

    class _FakeUnavailableHubClient:
        async def handshake(self, request: Any) -> Any:
            raise HubControlPlaneError(
                "hub_unavailable", "Hub is not running", retryable=True
            )

    logger = logging.getLogger("test.telegram.handshake.unavailable")
    service = TelegramBotService(_config(tmp_path), logger=logger, hub_root=tmp_path)
    service._hub_client = _FakeUnavailableHubClient()

    try:
        with caplog.at_level(logging.INFO, logger=logger.name):
            handshake_ok = await service._perform_hub_handshake()

        assert handshake_ok is False
        assert service._hub_handshake_compatibility is None
        assert _logged_events(caplog, logger.name)[-1] == {
            "event": "telegram.hub_control_plane.handshake_failed",
            "hub_root": str(tmp_path),
            "error_code": "hub_unavailable",
            "retryable": True,
            "message": "Hub is not running",
            "expected_schema_generation": ORCHESTRATION_SCHEMA_VERSION,
        }
    finally:
        await service._app_server_supervisor.close_all()
        await service._bot.close()


@pytest.mark.anyio
async def test_telegram_handshake_no_client(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    logger = logging.getLogger("test.telegram.handshake.no_client")
    service = TelegramBotService(_config(tmp_path), logger=logger, hub_root=tmp_path)
    service._hub_client = None

    try:
        with caplog.at_level(logging.INFO, logger=logger.name):
            handshake_ok = await service._perform_hub_handshake()

        assert handshake_ok is False
        assert service._hub_handshake_compatibility is None
        assert _logged_events(caplog, logger.name)[-1] == {
            "event": "telegram.hub_control_plane.client_not_configured",
            "hub_root": str(tmp_path),
            "expected_schema_generation": ORCHESTRATION_SCHEMA_VERSION,
        }
    finally:
        await service._app_server_supervisor.close_all()
        await service._bot.close()


@pytest.mark.anyio
async def test_telegram_startup_aborts_before_polling_on_incompatible_handshake(
    tmp_path: Path,
) -> None:
    seed_hub_files(tmp_path, force=True)
    service = TelegramBotService(
        _config(tmp_path),
        logger=logging.getLogger("test.telegram.handshake.startup_abort"),
        hub_root=tmp_path,
    )
    chat_core_called = False

    class _FakeIncompatibleHubClient:
        async def handshake(self, request: Any) -> Any:
            return SimpleNamespace(
                api_version="1.0.0",
                minimum_client_api_version="1.0.0",
                schema_generation=ORCHESTRATION_SCHEMA_VERSION + 1,
                capabilities=("compatibility_handshake",),
                hub_build_version=None,
                hub_asset_version=None,
            )

    async def _fake_chat_core_run() -> None:
        nonlocal chat_core_called
        chat_core_called = True

    service._hub_client = _FakeIncompatibleHubClient()
    service._chat_core.run = _fake_chat_core_run  # type: ignore[method-assign]

    try:
        with pytest.raises(SystemExit) as exc_info:
            await service.run_polling()

        assert exc_info.value.code == 1
        assert chat_core_called is False
    finally:
        await service._app_server_supervisor.close_all()
        await service._bot.close()
