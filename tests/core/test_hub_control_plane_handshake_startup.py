from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from codex_autorunner.core.hub_control_plane.errors import HubControlPlaneError
from codex_autorunner.core.hub_control_plane.handshake_startup import (
    perform_startup_hub_handshake,
)
from codex_autorunner.core.orchestration import ORCHESTRATION_SCHEMA_VERSION


@pytest.mark.anyio
async def test_startup_handshake_retries_with_jittered_info_logging(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    attempts = 0
    slept: list[float] = []

    class _FlakyHubClient:
        async def handshake(self, request: Any) -> Any:
            nonlocal attempts
            _ = request
            attempts += 1
            if attempts == 1:
                raise HubControlPlaneError(
                    "transport_failure",
                    "temporary startup transport failure",
                    retryable=True,
                )
            return SimpleNamespace(
                api_version="1.0.0",
                minimum_client_api_version="1.0.0",
                schema_generation=ORCHESTRATION_SCHEMA_VERSION,
                capabilities=("compatibility_handshake",),
                hub_build_version=None,
                hub_asset_version=None,
            )

    async def _fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(
        "codex_autorunner.core.hub_control_plane.handshake_startup.random.uniform",
        lambda start, end: end,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.hub_control_plane.handshake_startup.asyncio.sleep",
        _fake_sleep,
    )
    monkeypatch.setattr(
        "codex_autorunner.core.hub_control_plane.handshake_startup.time.monotonic",
        lambda: 0.0,
    )

    logger = logging.getLogger("test.handshake_startup.retry")
    with caplog.at_level(logging.INFO, logger=logger.name):
        ok, compatibility = await perform_startup_hub_handshake(
            hub_client=_FlakyHubClient(),
            log_event_name_prefix="test",
            handshake_client_name="test-client",
            hub_root_str="/tmp/hub",
            startup_monotonic=0.0,
            retry_window_seconds=1.0,
            retry_delay_seconds=0.5,
            retry_max_delay_seconds=2.0,
            client_api_version="1.0.0",
            logger=logger,
        )

    assert ok is True
    assert compatibility is not None
    assert slept == [0.55]
    assert any(
        '"event":"test.hub_control_plane.handshake_retrying"' in record.message
        and record.levelno == logging.INFO
        for record in caplog.records
    )
