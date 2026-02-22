from __future__ import annotations

import asyncio
import logging

import pytest

from codex_autorunner.integrations.discord.gateway import (
    DiscordGatewayClient,
    build_identify_payload,
    calculate_reconnect_backoff,
    parse_gateway_frame,
)


def test_build_identify_payload_contains_required_keys() -> None:
    payload = build_identify_payload(bot_token="bot-token", intents=513)
    assert payload["op"] == 2
    data = payload["d"]
    assert data["token"] == "bot-token"
    assert data["intents"] == 513
    properties = data["properties"]
    assert set(properties.keys()) == {"os", "browser", "device"}
    assert properties["browser"] == "codex-autorunner"
    assert properties["device"] == "codex-autorunner"


def test_calculate_reconnect_backoff_stays_within_bounds() -> None:
    low = calculate_reconnect_backoff(
        attempt=0,
        base_seconds=1.0,
        max_seconds=30.0,
        rand_float=lambda: 0.0,
    )
    high = calculate_reconnect_backoff(
        attempt=100,
        base_seconds=1.0,
        max_seconds=30.0,
        rand_float=lambda: 1.0,
    )
    assert 0.8 <= low <= 1.2
    assert 0.0 <= high <= 30.0


def test_parse_gateway_frame_allows_unknown_fields() -> None:
    frame = parse_gateway_frame(
        {
            "op": 0,
            "s": 42,
            "t": "INTERACTION_CREATE",
            "d": {"id": "abc"},
            "unexpected": {"nested": True},
        }
    )
    assert frame.op == 0
    assert frame.s == 42
    assert frame.t == "INTERACTION_CREATE"
    assert frame.d == {"id": "abc"}
    assert isinstance(frame.raw, dict)
    assert frame.raw.get("unexpected") == {"nested": True}


@pytest.mark.anyio
async def test_cancel_heartbeat_does_not_propagate_task_errors() -> None:
    client = DiscordGatewayClient(
        bot_token="token",
        intents=0,
        logger=logging.getLogger("test.gateway"),
    )

    async def _boom() -> None:
        raise RuntimeError("socket closed")

    client._heartbeat_task = asyncio.create_task(_boom())
    await asyncio.sleep(0)

    await client._cancel_heartbeat()

    assert client._heartbeat_task is None
