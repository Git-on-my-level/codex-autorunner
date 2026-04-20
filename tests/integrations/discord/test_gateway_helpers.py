from __future__ import annotations

import asyncio
import logging

import pytest

from codex_autorunner.integrations.discord.errors import DiscordPermanentError
from codex_autorunner.integrations.discord.gateway import (
    DISCORD_DISPATCH_CALLBACK_MAX_IN_FLIGHT,
    DiscordGatewayClient,
    GatewayDispatchWorker,
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
        lambda attempt, **_kw: float(backoff_attempts.append(attempt) or 2.0),
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
async def test_run_halts_reconnects_on_permanent_errors(
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
    wait_calls: list[bool] = []

    async def _fake_wait() -> None:
        wait_calls.append(True)

    monkeypatch.setattr(client._stop_event, "wait", _fake_wait)
    sleep_calls: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(gateway_module.asyncio, "sleep", _fake_sleep)
    await client.run(_noop_dispatch)

    assert wait_calls == [True]
    assert sleep_calls == []


@pytest.mark.anyio
async def test_run_halts_reconnects_for_fatal_gateway_close_codes(
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
    wait_calls: list[bool] = []

    async def _fake_wait() -> None:
        wait_calls.append(True)

    monkeypatch.setattr(client._stop_event, "wait", _fake_wait)
    sleep_calls: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        client._stop_event.set()

    monkeypatch.setattr(gateway_module.asyncio, "sleep", _fake_sleep)
    await client.run(_noop_dispatch)

    assert wait_calls == [True]
    assert sleep_calls == []


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
        lambda attempt, **_kw: float(attempts.append(attempt) or (attempt + 1)),
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
        lambda attempt, **_kw: float(attempts.append(attempt) or (attempt + 1)),
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


@pytest.mark.anyio
async def test_run_connection_reads_later_frames_while_dispatch_worker_is_busy() -> (
    None
):
    client = DiscordGatewayClient(
        bot_token="token",
        intents=0,
        logger=logging.getLogger("test.gateway"),
    )

    class _FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[object] = []
            self.second_frame_read = asyncio.Event()
            self._frames = iter(
                [
                    {"op": 0, "t": "INTERACTION_CREATE", "d": {"id": "first"}},
                    {"op": 0, "t": "INTERACTION_CREATE", "d": {"id": "second"}},
                ]
            )

        async def recv(self) -> dict[str, object]:
            return {"op": 10, "d": {"heartbeat_interval": 1000}}

        async def send(self, payload: object) -> None:
            self.sent.append(payload)

        def __aiter__(self) -> "_FakeWebSocket":
            return self

        async def __anext__(self) -> dict[str, object]:
            try:
                frame = next(self._frames)
                if frame["d"]["id"] == "second":
                    self.second_frame_read.set()
                return frame
            except StopIteration as exc:
                raise StopAsyncIteration from exc

    release_first = asyncio.Event()
    first_started = asyncio.Event()
    second_started = asyncio.Event()

    async def _dispatch(event_type: str, payload: dict[str, object]) -> None:
        assert event_type == "INTERACTION_CREATE"
        if payload["id"] == "first":
            first_started.set()
            await release_first.wait()
        elif payload["id"] == "second":
            second_started.set()

    websocket = _FakeWebSocket()
    run_task = asyncio.create_task(client._run_connection(websocket, _dispatch))
    await asyncio.wait_for(first_started.wait(), timeout=1.0)
    await asyncio.wait_for(websocket.second_frame_read.wait(), timeout=1.0)

    await asyncio.wait_for(second_started.wait(), timeout=1.0)

    release_first.set()
    await asyncio.wait_for(run_task, timeout=1.0)

    assert second_started.is_set()


@pytest.mark.anyio
async def test_run_connection_backpressures_when_dispatch_queue_is_full() -> None:
    client = DiscordGatewayClient(
        bot_token="token",
        intents=0,
        logger=logging.getLogger("test.gateway"),
    )

    class _FakeWebSocket:
        def __init__(self) -> None:
            self.last_frame_read = asyncio.Event()
            total = DISCORD_DISPATCH_CALLBACK_MAX_IN_FLIGHT + 3
            self._frames = iter(
                [
                    {
                        "op": 0,
                        "t": "INTERACTION_CREATE",
                        "d": {"id": f"frame-{index}"},
                    }
                    for index in range(1, total + 1)
                ]
            )
            self._last_frame_id = f"frame-{total}"

        async def recv(self) -> dict[str, object]:
            return {"op": 10, "d": {"heartbeat_interval": 1000}}

        async def send(self, _payload: object) -> None:
            return None

        def __aiter__(self) -> "_FakeWebSocket":
            return self

        async def __anext__(self) -> dict[str, object]:
            try:
                frame = next(self._frames)
                if frame["d"]["id"] == self._last_frame_id:
                    self.last_frame_read.set()
                return frame
            except StopIteration as exc:
                raise StopAsyncIteration from exc

    release_blocked = asyncio.Event()
    blocked_started = asyncio.Event()
    started_ids: list[str] = []
    blocked_frame_ids = {
        f"frame-{index}"
        for index in range(1, DISCORD_DISPATCH_CALLBACK_MAX_IN_FLIGHT + 1)
    }

    async def _dispatch(_event_type: str, payload: dict[str, object]) -> None:
        frame_id = str(payload["id"])
        started_ids.append(frame_id)
        if frame_id in blocked_frame_ids:
            if len([value for value in started_ids if value in blocked_frame_ids]) == (
                DISCORD_DISPATCH_CALLBACK_MAX_IN_FLIGHT
            ):
                blocked_started.set()
            await release_blocked.wait()

    websocket = _FakeWebSocket()
    run_task = asyncio.create_task(client._run_connection(websocket, _dispatch))
    await asyncio.wait_for(blocked_started.wait(), timeout=1.0)
    await asyncio.sleep(0)

    assert websocket.last_frame_read.is_set() is False

    release_blocked.set()
    await asyncio.wait_for(run_task, timeout=2.0)

    assert websocket.last_frame_read.is_set()


@pytest.mark.anyio
async def test_stop_cancels_outstanding_dispatch_worker() -> None:
    client = DiscordGatewayClient(
        bot_token="token",
        intents=0,
        logger=logging.getLogger("test.gateway"),
    )
    cancelled = asyncio.Event()

    async def _dispatch(_event_type: str, _payload: dict[str, object]) -> None:
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    dispatch_worker = GatewayDispatchWorker(
        logger=logging.getLogger("test.gateway"),
    )
    dispatch_worker.start(_dispatch, client._stop_event)
    client._active_dispatch_worker = dispatch_worker
    await dispatch_worker.enqueue("INTERACTION_CREATE", {"id": "slow"})
    await asyncio.sleep(0)

    await client.stop()

    assert cancelled.is_set()
    assert client._active_dispatch_worker is None


@pytest.mark.anyio
async def test_run_connection_propagates_dispatch_failures_while_waiting_for_next_frame() -> (
    None
):
    client = DiscordGatewayClient(
        bot_token="token",
        intents=0,
        logger=logging.getLogger("test.gateway"),
    )

    class _FakeWebSocket:
        def __init__(self) -> None:
            self._yielded_first = False

        async def recv(self) -> dict[str, object]:
            return {"op": 10, "d": {"heartbeat_interval": 1000}}

        async def send(self, _payload: object) -> None:
            return None

        def __aiter__(self) -> "_FakeWebSocket":
            return self

        async def __anext__(self) -> dict[str, object]:
            if not self._yielded_first:
                self._yielded_first = True
                return {"op": 0, "t": "INTERACTION_CREATE", "d": {"id": "first"}}
            await asyncio.Future()
            raise StopAsyncIteration

    async def _dispatch(_event_type: str, _payload: dict[str, object]) -> None:
        raise RuntimeError("dispatch failed")

    with pytest.raises(RuntimeError, match="dispatch failed"):
        await asyncio.wait_for(
            client._run_connection(_FakeWebSocket(), _dispatch),
            timeout=1.0,
        )


@pytest.mark.anyio
async def test_run_connection_propagates_dispatch_failures_during_queue_drain() -> None:
    client = DiscordGatewayClient(
        bot_token="token",
        intents=0,
        logger=logging.getLogger("test.gateway"),
    )

    class _FakeWebSocket:
        def __init__(self) -> None:
            self._frames = iter(
                [
                    {"op": 0, "t": "INTERACTION_CREATE", "d": {"id": "first"}},
                    {"op": 0, "t": "INTERACTION_CREATE", "d": {"id": "second"}},
                ]
            )

        async def recv(self) -> dict[str, object]:
            return {"op": 10, "d": {"heartbeat_interval": 1000}}

        async def send(self, _payload: object) -> None:
            return None

        def __aiter__(self) -> "_FakeWebSocket":
            return self

        async def __anext__(self) -> dict[str, object]:
            try:
                return next(self._frames)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

    release_first = asyncio.Event()
    first_started = asyncio.Event()

    async def _dispatch(_event_type: str, payload: dict[str, object]) -> None:
        if payload["id"] == "first":
            first_started.set()
            await release_first.wait()
            raise RuntimeError("dispatch failed")

    run_task = asyncio.create_task(client._run_connection(_FakeWebSocket(), _dispatch))
    await asyncio.wait_for(first_started.wait(), timeout=1.0)

    release_first.set()

    with pytest.raises(RuntimeError, match="dispatch failed"):
        await asyncio.wait_for(run_task, timeout=1.0)


@pytest.mark.anyio
async def test_stop_does_not_turn_dispatch_worker_cancellation_into_gateway_error() -> (
    None
):
    client = DiscordGatewayClient(
        bot_token="token",
        intents=0,
        logger=logging.getLogger("test.gateway"),
    )
    first_started = asyncio.Event()

    class _FakeWebSocket:
        def __init__(self) -> None:
            self._closed = asyncio.Event()
            self._yielded_first = False

        async def recv(self) -> dict[str, object]:
            return {"op": 10, "d": {"heartbeat_interval": 1000}}

        async def send(self, _payload: object) -> None:
            return None

        async def close(self) -> None:
            self._closed.set()

        def __aiter__(self) -> "_FakeWebSocket":
            return self

        async def __anext__(self) -> dict[str, object]:
            if not self._yielded_first:
                self._yielded_first = True
                return {"op": 0, "t": "INTERACTION_CREATE", "d": {"id": "first"}}
            await self._closed.wait()
            raise StopAsyncIteration

    async def _dispatch(_event_type: str, _payload: dict[str, object]) -> None:
        first_started.set()
        await asyncio.Future()

    websocket = _FakeWebSocket()
    run_task = asyncio.create_task(client._run_connection(websocket, _dispatch))
    await asyncio.wait_for(first_started.wait(), timeout=1.0)

    await client.stop()

    assert await asyncio.wait_for(run_task, timeout=1.0) is False
