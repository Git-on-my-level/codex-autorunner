from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from codex_autorunner.bootstrap import seed_hub_files
from codex_autorunner.integrations.discord.config import (
    DiscordBotConfig,
    DiscordBotShellConfig,
    DiscordCommandRegistration,
)
from codex_autorunner.integrations.discord.service import DiscordBotService
from codex_autorunner.integrations.discord.state import DiscordStateStore

pytestmark = pytest.mark.slow


class _FakeRest:
    def __init__(self) -> None:
        self.interaction_responses: list[dict[str, Any]] = []
        self.followup_messages: list[dict[str, Any]] = []
        self.original_interaction_edits: list[dict[str, Any]] = []
        self.channel_messages: list[dict[str, Any]] = []
        self.attachment_messages: list[dict[str, Any]] = []
        self.edited_channel_messages: list[dict[str, Any]] = []
        self.deleted_channel_messages: list[dict[str, Any]] = []
        self.typing_calls: list[str] = []
        self.message_ops: list[dict[str, Any]] = []
        self.download_requests: list[dict[str, Any]] = []
        self.attachment_data_by_url: dict[str, bytes] = {}

    async def create_interaction_response(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        payload: dict[str, Any],
    ) -> None:
        self.interaction_responses.append(
            {
                "interaction_id": interaction_id,
                "interaction_token": interaction_token,
                "payload": payload,
            }
        )


class _FakeGateway:
    def __init__(self, events: list[tuple[str, dict[str, Any]]]) -> None:
        self._events = events
        self.stopped = False

    async def run(self, on_dispatch) -> None:
        await on_dispatch("", {})

    async def stop(self) -> None:
        self.stopped = True


class _FakeOutboxManager:
    def start(self) -> None:
        return None

    async def run_loop(self) -> None:
        pass


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


def _config(
    root: Path,
    *,
    allowed_guild_ids: frozenset[str] = frozenset({"guild-1"}),
    allowed_channel_ids: frozenset[str] = frozenset({"channel-1"}),
    command_registration_enabled: bool = False,
    pma_enabled: bool = True,
    shell_enabled: bool = True,
    shell_timeout_ms: int = 120000,
    shell_max_output_chars: int = 3800,
    max_message_length: int = 2000,
    message_overflow: str = "split",
) -> DiscordBotConfig:
    return DiscordBotConfig(
        root=root,
        enabled=True,
        bot_token_env="TOKEN_ENV",
        app_id_env="APP_ENV",
        bot_token="token",
        application_id="app-1",
        allowed_guild_ids=allowed_guild_ids,
        allowed_channel_ids=allowed_channel_ids,
        allowed_user_ids=frozenset(),
        command_registration=DiscordCommandRegistration(
            enabled=command_registration_enabled,
            scope="guild",
            guild_ids=("guild-1",),
        ),
        state_file=root / ".codex-autorunner" / "discord_state.sqlite3",
        intents=1,
        max_message_length=max_message_length,
        message_overflow=message_overflow,
        pma_enabled=pma_enabled,
        shell=DiscordBotShellConfig(
            enabled=shell_enabled,
            timeout_ms=shell_timeout_ms,
            max_output_chars=shell_max_output_chars,
        ),
    )


@pytest.mark.anyio
async def test_discord_handshake_compatible_hub(tmp_path: Path) -> None:
    seed_hub_files(tmp_path, force=True)
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()

    class _FakeHubClient:
        async def handshake(self, request: Any) -> Any:
            return SimpleNamespace(
                api_version="1.0.0",
                minimum_client_api_version="1.0.0",
                schema_generation=1,
                capabilities=("compatibility_handshake",),
                hub_build_version="0.0.0",
                hub_asset_version=None,
            )

    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=_FakeRest(),
        gateway_client=_FakeGateway([]),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    service._hub_client = _FakeHubClient()

    await service._perform_hub_handshake()

    assert service._hub_handshake_compatibility is not None
    assert service._hub_handshake_compatibility.compatible is True
    assert service._hub_handshake_compatibility.state == "compatible"

    await store.close()


@pytest.mark.anyio
async def test_discord_handshake_incompatible_hub(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    seed_hub_files(tmp_path, force=True)
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()

    class _FakeIncompatibleHubClient:
        async def handshake(self, request: Any) -> Any:
            return SimpleNamespace(
                api_version="99.0.0",
                minimum_client_api_version="99.0.0",
                schema_generation=1,
                capabilities=(),
                hub_build_version=None,
                hub_asset_version=None,
            )

    logger = logging.getLogger("test.discord.handshake.incompatible")
    service = DiscordBotService(
        _config(tmp_path),
        logger=logger,
        rest_client=_FakeRest(),
        gateway_client=_FakeGateway([]),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    service._hub_client = _FakeIncompatibleHubClient()

    with caplog.at_level(logging.INFO, logger=logger.name):
        await service._perform_hub_handshake()

    assert service._hub_handshake_compatibility is not None
    assert service._hub_handshake_compatibility.compatible is False
    assert service._hub_handshake_compatibility.state == "incompatible"
    assert "major version mismatch" in (
        service._hub_handshake_compatibility.reason or ""
    )
    assert _logged_events(caplog, logger.name)[-1] == {
        "event": "discord.hub_control_plane.handshake_incompatible",
        "hub_root": str(tmp_path),
        "reason": "control-plane API major version mismatch",
        "server_api_version": "99.0.0",
        "client_api_version": "1.0.0",
        "server_schema_generation": 1,
        "expected_schema_generation": None,
    }

    await store.close()


@pytest.mark.anyio
async def test_discord_handshake_hub_unavailable(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    seed_hub_files(tmp_path, force=True)
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()

    from codex_autorunner.core.hub_control_plane.errors import HubControlPlaneError

    class _FakeUnavailableHubClient:
        async def handshake(self, request: Any) -> Any:
            raise HubControlPlaneError(
                "hub_unavailable", "Hub is not running", retryable=True
            )

    logger = logging.getLogger("test.discord.handshake.unavailable")
    service = DiscordBotService(
        _config(tmp_path),
        logger=logger,
        rest_client=_FakeRest(),
        gateway_client=_FakeGateway([]),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    service._hub_client = _FakeUnavailableHubClient()

    with caplog.at_level(logging.INFO, logger=logger.name):
        await service._perform_hub_handshake()

    assert service._hub_handshake_compatibility is None
    assert _logged_events(caplog, logger.name)[-1] == {
        "event": "discord.hub_control_plane.handshake_failed",
        "hub_root": str(tmp_path),
        "error_code": "hub_unavailable",
        "retryable": True,
        "message": "Hub is not running",
    }

    await store.close()


@pytest.mark.anyio
async def test_discord_handshake_no_client(tmp_path: Path) -> None:
    store = DiscordStateStore(tmp_path / "discord_state.sqlite3")
    await store.initialize()

    service = DiscordBotService(
        _config(tmp_path),
        logger=logging.getLogger("test"),
        rest_client=_FakeRest(),
        gateway_client=_FakeGateway([]),
        state_store=store,
        outbox_manager=_FakeOutboxManager(),
    )
    service._hub_client = None

    await service._perform_hub_handshake()

    assert service._hub_handshake_compatibility is None

    await store.close()
