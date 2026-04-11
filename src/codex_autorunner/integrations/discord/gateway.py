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

from ...core.logging_utils import log_event
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
DISCORD_DISPATCH_CALLBACK_MAX_IN_FLIGHT = 8


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
        self._dispatch_callback_tasks: set[asyncio.Task[None]] = set()
        self._dispatch_callback_semaphore: Optional[asyncio.Semaphore] = None
        self._dispatch_failure_future: Optional[asyncio.Future[None]] = None
        self._dispatch_order = 0
        self._websocket: Any = None

    async def stop(self) -> None:
        self._stop_event.set()
        await self._cancel_heartbeat()
        await self._cancel_dispatch_worker()
        if self._websocket is not None:
            with contextlib.suppress(ConnectionClosed, OSError):
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
                log_event(
                    self._logger,
                    logging.INFO,
                    "discord.gateway.transport.connect.start",
                    reconnect_attempt=reconnect_attempt,
                    gateway_url=gateway_url,
                )
                async with websockets.connect(gateway_url) as websocket:
                    self._websocket = websocket
                    log_event(
                        self._logger,
                        logging.INFO,
                        "discord.gateway.transport.connect.success",
                        reconnect_attempt=reconnect_attempt,
                        gateway_url=gateway_url,
                    )
                    established_session = await self._run_connection(
                        websocket, on_dispatch
                    )
            except asyncio.CancelledError:
                raise
            except DiscordPermanentError as exc:
                fatal_failure = True
                fatal_reason = str(exc)
                log_event(
                    self._logger,
                    logging.ERROR,
                    "discord.gateway.reconnect.failure",
                    reconnect_attempt=reconnect_attempt,
                    fatal=True,
                    cause="permanent_error",
                    error=str(exc),
                )
                self._logger.error(
                    "Discord gateway encountered permanent failure; halting reconnect loop: %s",
                    exc,
                )
            except ConnectionClosed as exc:
                close_code = gateway_close_code(exc)
                if close_code in FATAL_GATEWAY_CLOSE_CODES:
                    fatal_failure = True
                    fatal_reason = f"gateway_close_code={close_code}"
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.gateway.reconnect.failure",
                    reconnect_attempt=reconnect_attempt,
                    fatal=bool(fatal_failure),
                    cause="connection_closed",
                    close_code=close_code,
                )
                if close_code in FATAL_GATEWAY_CLOSE_CODES:
                    self._logger.error(
                        "Discord gateway closed with fatal code=%s; halting reconnect loop",
                        close_code,
                    )
                else:
                    self._logger.info("Discord gateway socket closed; reconnecting")
            except (
                Exception
            ) as exc:  # intentional: reconnect loop catches all transient failures
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.gateway.reconnect.failure",
                    reconnect_attempt=reconnect_attempt,
                    fatal=False,
                    cause="unexpected_error",
                    error=str(exc),
                )
                self._logger.warning("Discord gateway error; reconnecting: %s", exc)
            finally:
                self._websocket = None
                await self._cancel_heartbeat()
                await self._cancel_dispatch_worker()
                log_event(
                    self._logger,
                    logging.INFO,
                    "discord.gateway.transport.disconnect",
                    reconnect_attempt=reconnect_attempt,
                    established_session=established_session,
                    ready_seen=self._ready_in_connection,
                    fatal_failure=fatal_failure,
                )

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
                if reconnect_attempt > 0:
                    log_event(
                        self._logger,
                        logging.INFO,
                        "discord.gateway.reconnect.success",
                        reconnect_attempt=reconnect_attempt,
                        ready_seen=self._ready_in_connection,
                        established_session=established_session,
                    )
                reconnect_attempt = 0
            backoff = calculate_reconnect_backoff(reconnect_attempt)
            log_event(
                self._logger,
                logging.INFO,
                "discord.gateway.reconnect.scheduled",
                reconnect_attempt=reconnect_attempt,
                backoff_seconds=backoff,
            )
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
        self._dispatch_callback_semaphore = asyncio.Semaphore(
            DISCORD_DISPATCH_CALLBACK_MAX_IN_FLIGHT
        )
        self._dispatch_failure_future = asyncio.get_running_loop().create_future()
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
                self._dispatch_callback_semaphore = None
                self._dispatch_failure_future = None
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
        except (
            Exception
        ) as exc:  # intentional: heartbeat cleanup must not abort reconnect/shutdown
            # Heartbeat failures can happen after websocket disconnects; do not let
            # them abort reconnect/shutdown paths.
            self._logger.debug("Discord heartbeat task ended with error: %s", exc)

    async def _dispatch_loop(
        self,
        queue: asyncio.Queue[tuple[str, dict[str, Any]]],
        on_dispatch: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> None:
        if self._dispatch_callback_semaphore is None:
            self._dispatch_callback_semaphore = asyncio.Semaphore(
                DISCORD_DISPATCH_CALLBACK_MAX_IN_FLIGHT
            )
        if self._dispatch_failure_future is None:
            self._dispatch_failure_future = asyncio.get_running_loop().create_future()
        try:
            while True:
                event_type, payload = await queue.get()
                try:
                    await self._start_dispatch_callback(
                        event_type=event_type,
                        payload=payload,
                        on_dispatch=on_dispatch,
                    )
                finally:
                    queue.task_done()
        except asyncio.CancelledError:
            raise
        except (
            Exception
        ):  # intentional: dispatch callback failures re-raised after queue drain
            while True:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                else:
                    queue.task_done()
            raise

    async def _start_dispatch_callback(
        self,
        *,
        event_type: str,
        payload: dict[str, Any],
        on_dispatch: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> None:
        semaphore = self._dispatch_callback_semaphore
        if semaphore is None:
            raise RuntimeError("Discord dispatch callback semaphore is not running")
        await semaphore.acquire()
        dispatch_payload = dict(payload)
        if event_type == "INTERACTION_CREATE":
            self._dispatch_order += 1
            dispatch_payload["__car_dispatch_order"] = self._dispatch_order

        async def _run_dispatch_callback() -> None:
            await on_dispatch(event_type, dispatch_payload)

        task: asyncio.Task[None] = asyncio.create_task(_run_dispatch_callback())
        self._dispatch_callback_tasks.add(task)
        task.add_done_callback(self._dispatch_callback_done)

    def _dispatch_callback_done(self, task: asyncio.Task[None]) -> None:
        self._dispatch_callback_tasks.discard(task)
        semaphore = self._dispatch_callback_semaphore
        if semaphore is not None:
            semaphore.release()
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is None:
            return
        failure_future = self._dispatch_failure_future
        if failure_future is not None and not failure_future.done():
            failure_future.set_exception(exc)

    async def _recv_gateway_message(self, websocket_iter: Any) -> Any | None:
        dispatch_task = self._dispatch_worker_task
        if dispatch_task is None:
            raise RuntimeError("Discord dispatch worker is not running")
        failure_future = self._dispatch_failure_future

        message_task = asyncio.create_task(websocket_iter.__anext__())
        try:
            wait_targets: set[asyncio.Future[Any] | asyncio.Task[Any]] = {
                message_task,
                dispatch_task,
            }
            if failure_future is not None:
                wait_targets.add(failure_future)
            done, _pending = await asyncio.wait(
                wait_targets,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except (
            Exception
        ):  # intentional: asyncio.wait failure, re-raised after task cleanup
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
        if failure_future is not None and failure_future in done:
            message_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await message_task
            failure_future.result()

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
        failure_future = self._dispatch_failure_future

        queue_put_task = asyncio.create_task(queue.put((event_type, payload)))
        try:
            wait_targets: set[asyncio.Future[Any] | asyncio.Task[Any]] = {
                queue_put_task,
                dispatch_task,
            }
            if failure_future is not None:
                wait_targets.add(failure_future)
            done, _pending = await asyncio.wait(
                wait_targets,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except (
            Exception
        ):  # intentional: asyncio.wait failure, re-raised after task cleanup
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
        if failure_future is not None and failure_future in done:
            queue_put_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await queue_put_task
            failure_future.result()

        await queue_put_task
        return True

    async def _wait_for_dispatch_queue(
        self,
        queue: asyncio.Queue[tuple[str, dict[str, Any]]],
    ) -> None:
        dispatch_task = self._dispatch_worker_task
        if dispatch_task is None:
            return
        failure_future = self._dispatch_failure_future

        join_task = asyncio.create_task(queue.join())
        try:
            wait_targets: set[asyncio.Future[Any] | asyncio.Task[Any]] = {
                join_task,
                dispatch_task,
            }
            if failure_future is not None:
                wait_targets.add(failure_future)
            done, _pending = await asyncio.wait(
                wait_targets,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except (
            Exception
        ):  # intentional: asyncio.wait failure, re-raised after task cleanup
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
        if failure_future is not None and failure_future in done:
            join_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await join_task
            failure_future.result()

        await join_task
        self._raise_if_dispatch_worker_failed(dispatch_task)
        await self._wait_for_dispatch_callbacks()

    async def _wait_for_dispatch_callbacks(self) -> None:
        if not self._dispatch_callback_tasks:
            failure_future = self._dispatch_failure_future
            if failure_future is not None and failure_future.done():
                failure_future.result()
            return
        await asyncio.gather(*tuple(self._dispatch_callback_tasks))

    async def _cancel_dispatch_worker(self) -> None:
        queue = self._dispatch_queue
        if self._dispatch_worker_task is None:
            if queue is not None:
                self._drain_dispatch_queue(queue)
            await self._cancel_dispatch_callbacks()
            return
        task = self._dispatch_worker_task
        self._dispatch_worker_task = None
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        if queue is not None:
            self._drain_dispatch_queue(queue)
        await self._cancel_dispatch_callbacks()

    async def _cancel_dispatch_callbacks(self) -> None:
        callbacks = tuple(self._dispatch_callback_tasks)
        if not callbacks:
            return
        self._dispatch_callback_tasks.clear()
        for task in callbacks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*callbacks, return_exceptions=True)

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
