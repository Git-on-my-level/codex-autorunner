from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import platform
import random
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from .constants import DISCORD_GATEWAY_URL
from .errors import DiscordAPIError, DiscordPermanentError
from .rest import DiscordRestClient

try:
    import websockets
    from websockets.exceptions import ConnectionClosed as WebsocketsConnectionClosed
except (
    ImportError
):  # pragma: no cover - optional dependency gate handles this at runtime.
    websockets = None  # type: ignore[assignment]
    ConnectionClosed: type[Exception] = Exception
else:
    ConnectionClosed = WebsocketsConnectionClosed


FATAL_GATEWAY_CLOSE_CODES = {4004, 4010, 4011, 4012, 4013, 4014}
# Allow one queued dispatch in addition to the active worker item so later
# frames can start flowing without letting backlog grow unbounded.
DISCORD_DISPATCH_QUEUE_MAXSIZE = 1


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
    normalized_attempt = max(attempt, 0)
    if max_seconds <= 0.0:
        return 0.0
    if base_seconds <= 0.0:
        return 0.0
    min_jitter = 0.8
    cap_threshold = math.ceil(math.log2(max_seconds / (base_seconds * min_jitter)))
    if normalized_attempt >= max(cap_threshold, 0):
        return max_seconds
    scaled = base_seconds * (2**normalized_attempt)
    jitter_factor = 0.8 + (0.4 * min(max(rand_float(), 0.0), 1.0))
    result: float = min(max_seconds, max(0.0, scaled * jitter_factor))
    return result


def gateway_close_code(exc: BaseException) -> int | None:
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code
    received = getattr(exc, "rcvd", None)
    received_code = getattr(received, "code", None)
    if isinstance(received_code, int):
        return received_code
    return None


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
        self._ready_in_connection = False
        self._stop_event = asyncio.Event()
        self._heartbeat_task: Optional[asyncio.Task[None]] = None
        self._dispatch_queue: Optional[asyncio.Queue[tuple[str, dict[str, Any]]]] = None
        self._dispatch_worker_task: Optional[asyncio.Task[None]] = None
        self._websocket: Any = None

    async def stop(self) -> None:
        self._stop_event.set()
        await self._cancel_heartbeat()
        await self._cancel_dispatch_worker()
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
            established_session = False
            fatal_failure = False
            fatal_reason: Optional[str] = None
            self._ready_in_connection = False
            try:
                gateway_url = await self._resolve_gateway_url()
                async with websockets.connect(gateway_url) as websocket:
                    self._websocket = websocket
                    established_session = await self._run_connection(
                        websocket, on_dispatch
                    )
            except asyncio.CancelledError:
                raise
            except DiscordPermanentError as exc:
                fatal_failure = True
                fatal_reason = str(exc)
                self._logger.error(
                    "Discord gateway encountered permanent failure; halting reconnect loop: %s",
                    exc,
                )
            except ConnectionClosed as exc:
                close_code = gateway_close_code(exc)
                if close_code in FATAL_GATEWAY_CLOSE_CODES:
                    fatal_failure = True
                    fatal_reason = f"gateway_close_code={close_code}"
                    self._logger.error(
                        "Discord gateway closed with fatal code=%s; halting reconnect loop",
                        close_code,
                    )
                else:
                    self._logger.info("Discord gateway socket closed; reconnecting")
            except Exception as exc:
                self._logger.warning("Discord gateway error; reconnecting: %s", exc)
            finally:
                self._websocket = None
                await self._cancel_heartbeat()
                await self._cancel_dispatch_worker()

            if self._stop_event.is_set():
                break
            if fatal_failure:
                self._logger.error(
                    "Discord gateway is halted after fatal failure (%s). "
                    "Fix token/intents/configuration and restart the service.",
                    fatal_reason or "unknown",
                )
                await self._stop_event.wait()
                break
            if established_session or self._ready_in_connection:
                reconnect_attempt = 0
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
    ) -> bool:
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
        established_session = False
        dispatch_queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(
            maxsize=DISCORD_DISPATCH_QUEUE_MAXSIZE
        )
        self._dispatch_queue = dispatch_queue
        self._dispatch_worker_task = asyncio.create_task(
            self._dispatch_loop(dispatch_queue, on_dispatch)
        )
        websocket_iter = websocket.__aiter__()

        try:
            while True:
                raw_message = await self._recv_gateway_message(websocket_iter)
                if raw_message is None:
                    break
                frame = parse_gateway_frame(raw_message)
                if frame.s is not None:
                    self._sequence = frame.s

                if frame.op == 0:
                    if frame.t == "READY":
                        established_session = True
                        self._ready_in_connection = True
                    if frame.t and isinstance(frame.d, dict):
                        enqueued = await self._enqueue_dispatch(
                            dispatch_queue, frame.t, frame.d
                        )
                        if not enqueued:
                            return established_session
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
                    return established_session
                if frame.op == 9:
                    self._logger.warning("Discord gateway reported invalid session")
                    return established_session

            return established_session
        finally:
            try:
                await self._wait_for_dispatch_queue(dispatch_queue)
            finally:
                self._dispatch_queue = None
                await self._cancel_dispatch_worker()

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

    async def _dispatch_loop(
        self,
        queue: asyncio.Queue[tuple[str, dict[str, Any]]],
        on_dispatch: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> None:
        try:
            while True:
                event_type, payload = await queue.get()
                try:
                    await on_dispatch(event_type, payload)
                finally:
                    queue.task_done()
        except asyncio.CancelledError:
            raise
        except Exception:
            while True:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                else:
                    queue.task_done()
            raise

    async def _recv_gateway_message(self, websocket_iter: Any) -> Any | None:
        dispatch_task = self._dispatch_worker_task
        if dispatch_task is None:
            raise RuntimeError("Discord dispatch worker is not running")

        message_task = asyncio.create_task(websocket_iter.__anext__())
        try:
            done, _pending = await asyncio.wait(
                {message_task, dispatch_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        except Exception:
            message_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await message_task
            raise

        if dispatch_task in done:
            message_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await message_task
            if self._dispatch_worker_cancelled_for_shutdown(dispatch_task):
                return None
            self._raise_if_dispatch_worker_failed(dispatch_task)
            raise RuntimeError("Discord dispatch worker exited unexpectedly")

        try:
            return message_task.result()
        except StopAsyncIteration:
            return None

    async def _enqueue_dispatch(
        self,
        queue: asyncio.Queue[tuple[str, dict[str, Any]]],
        event_type: str,
        payload: dict[str, Any],
    ) -> bool:
        dispatch_task = self._dispatch_worker_task
        if dispatch_task is None:
            raise RuntimeError("Discord dispatch worker is not running")

        queue_put_task = asyncio.create_task(queue.put((event_type, payload)))
        try:
            done, _pending = await asyncio.wait(
                {queue_put_task, dispatch_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        except Exception:
            queue_put_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await queue_put_task
            raise

        if dispatch_task in done:
            queue_put_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await queue_put_task
            if self._dispatch_worker_cancelled_for_shutdown(dispatch_task):
                return False
            self._raise_if_dispatch_worker_failed(dispatch_task)
            raise RuntimeError("Discord dispatch worker exited unexpectedly")

        await queue_put_task
        return True

    async def _wait_for_dispatch_queue(
        self,
        queue: asyncio.Queue[tuple[str, dict[str, Any]]],
    ) -> None:
        dispatch_task = self._dispatch_worker_task
        if dispatch_task is None:
            return

        join_task = asyncio.create_task(queue.join())
        try:
            done, _pending = await asyncio.wait(
                {join_task, dispatch_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        except Exception:
            join_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await join_task
            raise

        if dispatch_task in done:
            join_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await join_task
            if self._dispatch_worker_cancelled_for_shutdown(dispatch_task):
                return
            self._raise_if_dispatch_worker_failed(dispatch_task)
            raise RuntimeError("Discord dispatch worker exited unexpectedly")

        await join_task
        self._raise_if_dispatch_worker_failed(dispatch_task)

    async def _cancel_dispatch_worker(self) -> None:
        queue = self._dispatch_queue
        if self._dispatch_worker_task is None:
            if queue is not None:
                self._drain_dispatch_queue(queue)
            return
        task = self._dispatch_worker_task
        self._dispatch_worker_task = None
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        if queue is not None:
            self._drain_dispatch_queue(queue)

    @staticmethod
    def _drain_dispatch_queue(
        queue: asyncio.Queue[tuple[str, dict[str, Any]]],
    ) -> None:
        while True:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                queue.task_done()

    def _dispatch_worker_cancelled_for_shutdown(self, task: asyncio.Task[None]) -> bool:
        return self._stop_event.is_set() and task.cancelled()

    def _raise_if_dispatch_worker_failed(self, task: asyncio.Task[None]) -> None:
        if not task.done():
            return
        if self._dispatch_worker_cancelled_for_shutdown(task):
            return
        task.result()
