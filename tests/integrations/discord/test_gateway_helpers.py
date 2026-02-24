from __future__ import annotations

import asyncio
import logging

import pytest

from codex_autorunner.integrations.discord.errors import DiscordPermanentError
from codex_autorunner.integrations.discord.gateway import (
    DiscordGatewayClient,
    build_identify_payload,
    calculate_reconnect_backoff,
    parse_gateway_frame,
)


async def _noop_dispatch(_event_type: str, _payload: dict[str, object]) -> None:
    return None


@pytest.mark.anyio
async def test_run_reconnect_path_does_not_raise_name_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codex_autorunner.integrations.discord import gateway as gateway_module

    if gateway_module.websockets is None:
        pytest.skip("websockets dependency is not installed")

    client = DiscordGatewayClient(
        bot_token="token",
        intents=0,
        logger=logging.getLogger("test.gateway"),
        gateway_url="wss://example.invalid",
    )

    class _FailingWebSocketModule:
        def connect(self, _gateway_url: str):
            client._stop_event.set()
            raise RuntimeError("simulated connect failure")

    monkeypatch.setattr(gateway_module, "websockets", _FailingWebSocketModule())

    await client.run(_noop_dispatch)


@pytest.mark.anyio
async def test_run_retries_resolve_failures_without_exiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codex_autorunner.integrations.discord import gateway as gateway_module

    client = DiscordGatewayClient(
        bot_token="token",
        intents=0,
        logger=logging.getLogger("test.gateway"),
    )
    monkeypatch.setattr(gateway_module, "websockets", object())

    async def _fail_resolve() -> str:
        raise RuntimeError("resolve failed")

    monkeypatch.setattr(client, "_resolve_gateway_url", _fail_resolve)
    backoff_attempts: list[int] = []
    monkeypatch.setattr(
        gateway_module,
        "calculate_reconnect_backoff",
        lambda attempt: float(backoff_attempts.append(attempt) or 2.0),
    )
    sleep_calls: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        client._stop_event.set()

    monkeypatch.setattr(gateway_module.asyncio, "sleep", _fake_sleep)
    await client.run(_noop_dispatch)

    assert backoff_attempts == [0]
    assert sleep_calls == [2.0]


@pytest.mark.anyio
async def test_run_slow_retries_on_permanent_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codex_autorunner.integrations.discord import gateway as gateway_module

    client = DiscordGatewayClient(
        bot_token="token",
        intents=0,
        logger=logging.getLogger("test.gateway"),
    )
    monkeypatch.setattr(gateway_module, "websockets", object())

    async def _fail_resolve() -> str:
        raise DiscordPermanentError("invalid credentials")

    monkeypatch.setattr(client, "_resolve_gateway_url", _fail_resolve)
    monkeypatch.setattr(gateway_module, "calculate_reconnect_backoff", lambda _a: 1.0)
    sleep_calls: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        client._stop_event.set()

    monkeypatch.setattr(gateway_module.asyncio, "sleep", _fake_sleep)
    await client.run(_noop_dispatch)

    assert sleep_calls == [60.0]


@pytest.mark.anyio
async def test_run_slow_retries_for_fatal_gateway_close_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codex_autorunner.integrations.discord import gateway as gateway_module

    client = DiscordGatewayClient(
        bot_token="token",
        intents=0,
        logger=logging.getLogger("test.gateway"),
    )

    class _FakeConnectionClosed(Exception):
        def __init__(self, code: int) -> None:
            super().__init__(f"code={code}")
            self.code = code

    class _FailingWebSocketModule:
        def connect(self, _gateway_url: str):
            class _Context:
                async def __aenter__(self) -> object:
                    raise _FakeConnectionClosed(4004)

                async def __aexit__(self, *_exc: object) -> bool:
                    return False

            return _Context()

    monkeypatch.setattr(gateway_module, "ConnectionClosed", _FakeConnectionClosed)
    monkeypatch.setattr(gateway_module, "websockets", _FailingWebSocketModule())

    async def _resolve_gateway_url() -> str:
        return "wss://example.invalid"

    monkeypatch.setattr(client, "_resolve_gateway_url", _resolve_gateway_url)
    monkeypatch.setattr(gateway_module, "calculate_reconnect_backoff", lambda _a: 1.0)
    sleep_calls: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        client._stop_event.set()

    monkeypatch.setattr(gateway_module.asyncio, "sleep", _fake_sleep)
    await client.run(_noop_dispatch)

    assert sleep_calls == [60.0]


@pytest.mark.anyio
async def test_run_resets_backoff_only_after_established_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codex_autorunner.integrations.discord import gateway as gateway_module

    client = DiscordGatewayClient(
        bot_token="token",
        intents=0,
        logger=logging.getLogger("test.gateway"),
    )

    class _WebSocketModule:
        def connect(self, _gateway_url: str):
            class _Context:
                async def __aenter__(self) -> object:
                    return object()

                async def __aexit__(self, *_exc: object) -> bool:
                    return False

            return _Context()

    monkeypatch.setattr(gateway_module, "websockets", _WebSocketModule())

    async def _resolve_gateway_url() -> str:
        return "wss://example.invalid"

    monkeypatch.setattr(client, "_resolve_gateway_url", _resolve_gateway_url)
    call_count = 0

    async def _run_connection(_websocket: object, _dispatch: object) -> bool:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("early disconnect")
        return True

    monkeypatch.setattr(client, "_run_connection", _run_connection)
    attempts: list[int] = []
    monkeypatch.setattr(
        gateway_module,
        "calculate_reconnect_backoff",
        lambda attempt: float(attempts.append(attempt) or (attempt + 1)),
    )
    sleep_calls: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 2:
            client._stop_event.set()

    monkeypatch.setattr(gateway_module.asyncio, "sleep", _fake_sleep)
    await client.run(_noop_dispatch)

    assert attempts == [0, 0]
    assert sleep_calls == [1.0, 1.0]


@pytest.mark.anyio
async def test_run_resets_backoff_when_ready_seen_before_socket_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codex_autorunner.integrations.discord import gateway as gateway_module

    client = DiscordGatewayClient(
        bot_token="token",
        intents=0,
        logger=logging.getLogger("test.gateway"),
    )

    class _FakeConnectionClosed(Exception):
        def __init__(self, code: int) -> None:
            super().__init__(f"code={code}")
            self.code = code

    class _WebSocketModule:
        def connect(self, _gateway_url: str):
            class _Context:
                async def __aenter__(self) -> object:
                    return object()

                async def __aexit__(self, *_exc: object) -> bool:
                    return False

            return _Context()

    monkeypatch.setattr(gateway_module, "ConnectionClosed", _FakeConnectionClosed)
    monkeypatch.setattr(gateway_module, "websockets", _WebSocketModule())

    async def _resolve_gateway_url() -> str:
        return "wss://example.invalid"

    monkeypatch.setattr(client, "_resolve_gateway_url", _resolve_gateway_url)
    call_count = 0

    async def _run_connection(_websocket: object, _dispatch: object) -> bool:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            client._ready_in_connection = True
            raise _FakeConnectionClosed(1000)
        raise RuntimeError("early disconnect")

    monkeypatch.setattr(client, "_run_connection", _run_connection)
    attempts: list[int] = []
    monkeypatch.setattr(
        gateway_module,
        "calculate_reconnect_backoff",
        lambda attempt: float(attempts.append(attempt) or (attempt + 1)),
    )
    sleep_calls: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 2:
            client._stop_event.set()

    monkeypatch.setattr(gateway_module.asyncio, "sleep", _fake_sleep)
    await client.run(_noop_dispatch)

    assert attempts == [0, 1]
    assert sleep_calls == [1.0, 2.0]


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


def test_calculate_reconnect_backoff_large_attempt_short_circuits() -> None:
    value = calculate_reconnect_backoff(
        attempt=100_000,
        base_seconds=1.0,
        max_seconds=30.0,
    )
    assert value == 30.0


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
