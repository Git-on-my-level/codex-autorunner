from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import platform
import random
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from .constants import DISCORD_GATEWAY_URL
from .errors import DiscordAPIError
from .rest import DiscordRestClient

try:
    import websockets
    from websockets.exceptions import ConnectionClosed
except (
    ImportError
):  # pragma: no cover - optional dependency gate handles this at runtime.
    websockets = None  # type: ignore[assignment]

    class ConnectionClosed(Exception):
        pass


@dataclass(frozen=True)
class GatewayFrame:
    op: int
    d: Any = None
    s: Optional[int] = None
    t: Optional[str] = None
    raw: dict[str, Any] | None = None


def build_identify_payload(*, bot_token: str, intents: int) -> dict[str, Any]:
    return {
        "op": 2,
        "d": {
            "token": bot_token,
            "intents": intents,
            "properties": {
                "os": platform.system().lower() or "unknown",
                "browser": "codex-autorunner",
                "device": "codex-autorunner",
            },
        },
    }


def parse_gateway_frame(frame: str | bytes | dict[str, Any]) -> GatewayFrame:
    if isinstance(frame, bytes):
        frame = frame.decode("utf-8")
    payload = json.loads(frame) if isinstance(frame, str) else dict(frame)
    if not isinstance(payload, dict):
        raise DiscordAPIError("Discord gateway frame must be a JSON object")
    op = payload.get("op")
    if not isinstance(op, int):
        raise DiscordAPIError(f"Discord gateway frame missing numeric op: {payload!r}")
    seq = payload.get("s")
    event_type = payload.get("t")
    return GatewayFrame(
        op=op,
        d=payload.get("d"),
        s=seq if isinstance(seq, int) else None,
        t=event_type if isinstance(event_type, str) else None,
        raw=payload,
    )


def calculate_reconnect_backoff(
    attempt: int,
    *,
    base_seconds: float = 1.0,
    max_seconds: float = 30.0,
    rand_float: Callable[[], float] = random.random,
) -> float:
    scaled = base_seconds * (2 ** max(attempt, 0))
    jitter_factor = 0.8 + (0.4 * min(max(rand_float(), 0.0), 1.0))
    return min(max_seconds, max(0.0, scaled * jitter_factor))


class DiscordGatewayClient:
    def __init__(
        self,
        *,
        bot_token: str,
        intents: int,
        logger: logging.Logger,
        gateway_url: str | None = None,
    ) -> None:
        self._bot_token = bot_token
        self._intents = intents
        self._logger = logger
        self._gateway_url = gateway_url
        self._sequence: Optional[int] = None
        self._last_heartbeat_ack: Optional[float] = None
        self._stop_event = asyncio.Event()
        self._heartbeat_task: Optional[asyncio.Task[None]] = None
        self._websocket: Any = None

    async def stop(self) -> None:
        self._stop_event.set()
        await self._cancel_heartbeat()
        if self._websocket is not None:
            with contextlib.suppress(Exception):
                await self._websocket.close()

    async def run(
        self, on_dispatch: Callable[[str, dict[str, Any]], Awaitable[None]]
    ) -> None:
        if websockets is None:
            raise DiscordAPIError(
                "Discord gateway requires optional dependency: install with .[discord]"
            )

        reconnect_attempt = 0
        while not self._stop_event.is_set():
            gateway_url = await self._resolve_gateway_url()
            try:
                async with websockets.connect(gateway_url) as websocket:
                    self._websocket = websocket
                    reconnect_attempt = 0
                    await self._run_connection(websocket, on_dispatch)
            except asyncio.CancelledError:
                raise
            except ConnectionClosed:
                self._logger.info("Discord gateway socket closed; reconnecting")
            except Exception as exc:
                self._logger.warning("Discord gateway error; reconnecting: %s", exc)
            finally:
                self._websocket = None
                await self._cancel_heartbeat()

            if self._stop_event.is_set():
                break
            backoff = calculate_reconnect_backoff(reconnect_attempt)
            reconnect_attempt += 1
            await asyncio.sleep(backoff)

    async def _resolve_gateway_url(self) -> str:
        if self._gateway_url:
            return self._gateway_url
        async with DiscordRestClient(bot_token=self._bot_token) as rest:
            payload = await rest.get_gateway_bot()
        url = payload.get("url") if isinstance(payload, dict) else None
        if not isinstance(url, str) or not url:
            return DISCORD_GATEWAY_URL
        if "?" in url:
            return url
        return f"{url}?v=10&encoding=json"

    async def _run_connection(
        self,
        websocket: Any,
        on_dispatch: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> None:
        raw = await websocket.recv()
        hello = parse_gateway_frame(raw)
        if hello.op != 10:
            raise DiscordAPIError(
                "Discord gateway expected HELLO frame before IDENTIFY"
            )
        heartbeat_data = hello.d if isinstance(hello.d, dict) else {}
        heartbeat_ms = heartbeat_data.get("heartbeat_interval")
        if not isinstance(heartbeat_ms, (int, float)) or heartbeat_ms <= 0:
            raise DiscordAPIError("Discord gateway HELLO missing heartbeat_interval")

        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(websocket, float(heartbeat_ms) / 1000.0)
        )
        await websocket.send(
            json.dumps(
                build_identify_payload(bot_token=self._bot_token, intents=self._intents)
            )
        )

        async for raw_message in websocket:
            frame = parse_gateway_frame(raw_message)
            if frame.s is not None:
                self._sequence = frame.s

            if frame.op == 0:
                if frame.t and isinstance(frame.d, dict):
                    await on_dispatch(frame.t, frame.d)
                continue
            if frame.op == 1:
                await websocket.send(
                    json.dumps({"op": 1, "d": self._sequence})
                )  # heartbeat request
                continue
            if frame.op == 11:
                self._last_heartbeat_ack = asyncio.get_running_loop().time()
                continue
            if frame.op == 7:
                self._logger.info("Discord gateway requested reconnect")
                return
            if frame.op == 9:
                await asyncio.sleep(1.0)
                await websocket.send(
                    json.dumps(
                        build_identify_payload(
                            bot_token=self._bot_token, intents=self._intents
                        )
                    )
                )
                continue

    async def _heartbeat_loop(self, websocket: Any, interval_seconds: float) -> None:
        try:
            while not self._stop_event.is_set():
                await asyncio.sleep(interval_seconds)
                await websocket.send(json.dumps({"op": 1, "d": self._sequence}))
        except asyncio.CancelledError:
            raise

    async def _cancel_heartbeat(self) -> None:
        if self._heartbeat_task is None:
            return
        task = self._heartbeat_task
        self._heartbeat_task = None
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            # Heartbeat failures can happen after websocket disconnects; do not let
            # them abort reconnect/shutdown paths.
            self._logger.debug("Discord heartbeat task ended with error: %s", exc)
