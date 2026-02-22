from __future__ import annotations

from typing import Any

import httpx
import pytest

from codex_autorunner.integrations.discord.rest import DiscordRestClient


async def _configure_mock_client(
    client: DiscordRestClient, transport: httpx.MockTransport
) -> None:
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="https://discord.test/api/v10",
        transport=transport,
        timeout=10.0,
    )


@pytest.mark.anyio
async def test_discord_rest_client_sets_authorization_header() -> None:
    observed: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["authorization"] = request.headers.get("Authorization")
        observed["path"] = request.url.path
        return httpx.Response(200, json={"url": "wss://gateway.discord.gg"})

    client = DiscordRestClient(
        bot_token="abc123", base_url="https://discord.test/api/v10"
    )
    await _configure_mock_client(client, httpx.MockTransport(handler))
    try:
        payload = await client.get_gateway_bot()
    finally:
        await client.close()

    assert payload["url"] == "wss://gateway.discord.gg"
    assert observed["authorization"] == "Bot abc123"
    assert observed["path"] == "/api/v10/gateway/bot"


@pytest.mark.anyio
async def test_command_routes_global_and_guild() -> None:
    observed_paths: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed_paths.append((request.method, request.url.path))
        if request.method == "PUT":
            return httpx.Response(200, json=[{"id": "cmd-1"}])
        return httpx.Response(200, json=[])

    client = DiscordRestClient(
        bot_token="abc123", base_url="https://discord.test/api/v10"
    )
    await _configure_mock_client(client, httpx.MockTransport(handler))
    try:
        await client.list_application_commands(application_id="app-1")
        await client.list_application_commands(
            application_id="app-1", guild_id="guild-2"
        )
        await client.bulk_overwrite_application_commands(
            application_id="app-1",
            commands=[{"name": "ping"}],
        )
        await client.bulk_overwrite_application_commands(
            application_id="app-1",
            guild_id="guild-2",
            commands=[{"name": "pong"}],
        )
    finally:
        await client.close()

    assert observed_paths == [
        ("GET", "/api/v10/applications/app-1/commands"),
        ("GET", "/api/v10/applications/app-1/guilds/guild-2/commands"),
        ("PUT", "/api/v10/applications/app-1/commands"),
        ("PUT", "/api/v10/applications/app-1/guilds/guild-2/commands"),
    ]


@pytest.mark.anyio
async def test_rate_limit_retry_after_retries_and_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = {"count": 0}
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(
        "codex_autorunner.integrations.discord.rest.asyncio.sleep", fake_sleep
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 3:
            return httpx.Response(429, headers={"Retry-After": "0.25"}, json={})
        return httpx.Response(200, json={"id": "msg-1"})

    client = DiscordRestClient(
        bot_token="abc123", base_url="https://discord.test/api/v10"
    )
    await _configure_mock_client(client, httpx.MockTransport(handler))
    try:
        payload = await client.create_channel_message(
            channel_id="chan-1",
            payload={"content": "hello"},
        )
    finally:
        await client.close()

    assert payload == {"id": "msg-1"}
    assert attempts["count"] == 3
    assert sleeps == [0.25, 0.25]
