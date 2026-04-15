"""OpenCode stream lifecycle controller: timeout, reconnect, and stall management.

Owns first-event timeout, stall timeout, reconnect backoff, session polling,
and post-completion grace-window decisions.  Exposes explicit
:class:`LifecycleDecision` signals so output-assembly code never decides when
to reconnect, poll session state, or fail a stalled stream.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from enum import Enum
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

import httpx

from ...core.logging_utils import log_event
from ...core.sse import SSEEvent
from .protocol_payload import extract_status_type, status_is_idle

_OPENCODE_STREAM_STALL_TIMEOUT_SECONDS = 60.0
_OPENCODE_FIRST_EVENT_TIMEOUT_SECONDS = 60.0
_OPENCODE_STREAM_RECONNECT_BACKOFF_SECONDS = (0.5, 1.0, 2.0, 5.0, 10.0)
_OPENCODE_STREAM_MAX_STALL_RECONNECT_ATTEMPTS = 5
_OPENCODE_STREAM_MAX_STALL_RECONNECT_SECONDS = 120.0
_OPENCODE_STREAM_STALL_TIMEOUT_REASON = "opencode_stream_stalled_timeout"
_OPENCODE_FIRST_EVENT_TIMEOUT_REASON = "opencode_first_event_timeout"
_OPENCODE_POST_COMPLETION_GRACE_SECONDS = 5.0
_OPENCODE_ABSOLUTE_MAX_IDLE_SECONDS = 300.0

StatusEventHandler = Callable[[str, dict[str, Any], Optional[str]], Awaitable[None]]


class LifecycleAction(Enum):
    CONTINUE = "continue"
    BREAK = "break"


class LifecycleDecision:
    __slots__ = ("action", "error", "should_flush_if_pending")

    def __init__(
        self,
        action: LifecycleAction,
        error: Optional[str] = None,
        should_flush_if_pending: bool = False,
    ):
        self.action = action
        self.error = error
        self.should_flush_if_pending = should_flush_if_pending


class StreamLifecycleController:
    """Encapsulates stream lifecycle state and policy.

    Tracks when the stream started, whether any relevant event has been
    received, reconnect attempts, post-completion grace windows, and stall
    timing.  Callers feed events and timeouts in; the controller returns
    explicit :class:`LifecycleDecision` values describing what to do next.
    """

    def __init__(
        self,
        *,
        session_id: str,
        event_stream_factory: Optional[Callable[[], AsyncIterator[SSEEvent]]],
        session_fetcher: Optional[Callable[[], Awaitable[Any]]],
        stall_timeout_seconds: Optional[float],
        first_event_timeout_seconds: Optional[float],
        status_event_handler: Optional[StatusEventHandler],
        logger: Optional[logging.Logger] = None,
    ):
        self._session_id = session_id
        self._event_stream_factory = event_stream_factory
        self._session_fetcher = session_fetcher
        self._stall_timeout_seconds = stall_timeout_seconds
        self._first_event_timeout_seconds = first_event_timeout_seconds
        self._status_event_handler = status_event_handler
        self._logger = logger or logging.getLogger(__name__)

        self._stream_started_at = time.monotonic()
        self._last_relevant_event_at = self._stream_started_at
        self._received_any_event = False
        self._last_primary_completion_at: Optional[float] = None
        self._post_completion_deadline: Optional[float] = None
        self._reconnect_attempts = 0
        self._reconnect_started_at: Optional[float] = None
        self._can_reconnect = (
            event_stream_factory is not None and stall_timeout_seconds is not None
        )
        self._stream_iter: Optional[AsyncIterator[SSEEvent]] = None

    def set_stream_iterator(self, iterator: AsyncIterator[SSEEvent]) -> None:
        self._stream_iter = iterator

    @property
    def stream_iterator(self) -> Optional[AsyncIterator[SSEEvent]]:
        return self._stream_iter

    def compute_wait_timeout(self, *, now: Optional[float] = None) -> Optional[float]:
        if now is None:
            now = time.monotonic()

        wait_timeout: Optional[float] = None

        if (
            self._first_event_timeout_seconds is not None
            and not self._received_any_event
        ):
            wait_timeout = max(
                0.0,
                self._stream_started_at + self._first_event_timeout_seconds - now,
            )

        if (
            self._can_reconnect
            and self._stall_timeout_seconds is not None
            and (self._received_any_event or self._first_event_timeout_seconds is None)
        ):
            if wait_timeout is None:
                wait_timeout = self._stall_timeout_seconds
            else:
                wait_timeout = min(wait_timeout, self._stall_timeout_seconds)

        if self._post_completion_deadline is not None:
            remaining = max(0.0, self._post_completion_deadline - now)
            if wait_timeout is None:
                wait_timeout = remaining
            else:
                wait_timeout = min(wait_timeout, remaining)

        if wait_timeout is None and self._received_any_event:
            wait_timeout = _OPENCODE_ABSOLUTE_MAX_IDLE_SECONDS

        return wait_timeout

    def on_relevant_event(self, *, now: Optional[float] = None) -> None:
        if now is None:
            now = time.monotonic()
        self._last_relevant_event_at = now
        self._received_any_event = True
        self._reconnect_attempts = 0
        self._reconnect_started_at = None

    def on_primary_completion(self, *, now: Optional[float] = None) -> None:
        if now is None:
            now = time.monotonic()
        self._last_primary_completion_at = now
        self._post_completion_deadline = now + max(
            _OPENCODE_POST_COMPLETION_GRACE_SECONDS, 0.0
        )

    async def on_timeout_error(self, *, now: float) -> LifecycleDecision:
        if (
            self._post_completion_deadline is not None
            and now >= self._post_completion_deadline
        ):
            log_event(
                self._logger,
                logging.INFO,
                "opencode.stream.completed.grace_elapsed",
                session_id=self._session_id,
                grace_seconds=_OPENCODE_POST_COMPLETION_GRACE_SECONDS,
                idle_seconds=now - self._last_relevant_event_at,
            )
            return LifecycleDecision(
                action=LifecycleAction.BREAK,
                should_flush_if_pending=True,
            )

        if (
            not self._received_any_event
            and self._first_event_timeout_seconds is not None
        ):
            if now - self._stream_started_at >= self._first_event_timeout_seconds:
                status_type = await self._poll_session_status_quiet()
                if status_type and not status_is_idle(status_type):
                    return await self._handle_stall_recovery(now=now)
                return await self._fail_first_event_timeout(now=now)
            return LifecycleDecision(action=LifecycleAction.CONTINUE)

        return await self._handle_stall_recovery(now=now)

    async def on_irrelevant_event(self, *, now: float) -> LifecycleDecision:
        if (
            not self._received_any_event
            and self._first_event_timeout_seconds is not None
        ):
            if now - self._stream_started_at >= self._first_event_timeout_seconds:
                status_type = await self._poll_session_status_quiet()
                if status_type and not status_is_idle(status_type):
                    return await self._handle_stall_recovery(now=now)
                return await self._fail_first_event_timeout(now=now)
            return LifecycleDecision(action=LifecycleAction.CONTINUE)

        if (
            self._stall_timeout_seconds is not None
            and now - self._last_relevant_event_at > self._stall_timeout_seconds
        ):
            return await self._handle_stall_recovery(now=now)

        return LifecycleDecision(action=LifecycleAction.CONTINUE)

    def check_grace_elapsed(
        self, *, now: Optional[float] = None
    ) -> Optional[LifecycleDecision]:
        if now is None:
            now = time.monotonic()
        if (
            self._post_completion_deadline is not None
            and now >= self._post_completion_deadline
        ):
            log_event(
                self._logger,
                logging.INFO,
                "opencode.stream.completed.grace_elapsed",
                session_id=self._session_id,
                grace_seconds=_OPENCODE_POST_COMPLETION_GRACE_SECONDS,
                idle_seconds=now - self._last_relevant_event_at,
            )
            return LifecycleDecision(
                action=LifecycleAction.BREAK,
                should_flush_if_pending=True,
            )
        return None

    async def close(self) -> None:
        if self._stream_iter is not None:
            await _close_stream(self._stream_iter)
            self._stream_iter = None

    async def _emit_status(self, payload: dict[str, Any]) -> None:
        if self._status_event_handler is not None:
            await self._status_event_handler("status", payload, None)

    async def _poll_session_status_warn(self) -> Optional[str]:
        if self._session_fetcher is None:
            return None
        try:
            fetched = await self._session_fetcher()
            return extract_status_type(fetched)
        except (httpx.HTTPError, ValueError, OSError) as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "opencode.session.poll_failed",
                session_id=self._session_id,
                exc=exc,
            )
            return None

    async def _poll_session_status_quiet(self) -> Optional[str]:
        if self._session_fetcher is None:
            return None
        try:
            fetched = await self._session_fetcher()
            return extract_status_type(fetched)
        except (ConnectionError, OSError, TimeoutError):
            self._logger.debug(
                "session fetch during timeout check failed", exc_info=True
            )
            return None

    async def _fail_first_event_timeout(self, *, now: float) -> LifecycleDecision:
        timeout_seconds = self._first_event_timeout_seconds
        if timeout_seconds is None:
            timeout_seconds = 0.0
        idle_seconds = now - self._stream_started_at
        error_msg = (
            f"{_OPENCODE_FIRST_EVENT_TIMEOUT_REASON}: "
            f"no relevant events received within {timeout_seconds:.1f}s"
        )
        log_event(
            self._logger,
            logging.ERROR,
            "opencode.stream.first_event_timeout",
            session_id=self._session_id,
            idle_seconds=idle_seconds,
            timeout_seconds=timeout_seconds,
        )
        await self._emit_status(
            {
                "type": "first_event_timeout",
                "reason": _OPENCODE_FIRST_EVENT_TIMEOUT_REASON,
                "idleSeconds": idle_seconds,
                "firstEventTimeoutSeconds": timeout_seconds,
            }
        )
        return LifecycleDecision(
            action=LifecycleAction.BREAK,
            error=error_msg,
        )

    async def _attempt_reconnect(
        self,
        *,
        now: float,
        idle_seconds: float,
        status_type: Optional[str],
    ) -> tuple[bool, Optional[str]]:
        if not self._can_reconnect:
            return False, None

        if self._reconnect_started_at is None:
            self._reconnect_started_at = now
        stalled_elapsed_seconds = now - self._reconnect_started_at
        attempts_exceeded = (
            self._reconnect_attempts >= _OPENCODE_STREAM_MAX_STALL_RECONNECT_ATTEMPTS
        )
        elapsed_exceeded = (
            stalled_elapsed_seconds >= _OPENCODE_STREAM_MAX_STALL_RECONNECT_SECONDS
        )

        if attempts_exceeded or elapsed_exceeded:
            error_msg = (
                f"{_OPENCODE_STREAM_STALL_TIMEOUT_REASON}: "
                f"stalled for {stalled_elapsed_seconds:.1f}s after "
                f"{self._reconnect_attempts} reconnect attempts"
            )
            log_event(
                self._logger,
                logging.ERROR,
                "opencode.stream.stalled.timeout",
                session_id=self._session_id,
                idle_seconds=idle_seconds,
                stalled_elapsed_seconds=stalled_elapsed_seconds,
                reconnect_attempts=self._reconnect_attempts,
                max_reconnect_attempts=_OPENCODE_STREAM_MAX_STALL_RECONNECT_ATTEMPTS,
                max_stalled_seconds=_OPENCODE_STREAM_MAX_STALL_RECONNECT_SECONDS,
                status_type=status_type,
            )
            await self._emit_status(
                {
                    "type": "stall_timeout",
                    "reason": _OPENCODE_STREAM_STALL_TIMEOUT_REASON,
                    "idleSeconds": idle_seconds,
                    "stalledSeconds": stalled_elapsed_seconds,
                    "attempts": self._reconnect_attempts,
                }
            )
            return False, error_msg

        backoff_index = min(
            self._reconnect_attempts,
            len(_OPENCODE_STREAM_RECONNECT_BACKOFF_SECONDS) - 1,
        )
        backoff = _OPENCODE_STREAM_RECONNECT_BACKOFF_SECONDS[backoff_index]
        self._reconnect_attempts += 1
        log_event(
            self._logger,
            logging.WARNING,
            "opencode.stream.stalled.reconnecting",
            session_id=self._session_id,
            idle_seconds=idle_seconds,
            backoff_seconds=backoff,
            status_type=status_type,
            attempts=self._reconnect_attempts,
        )
        await self._emit_status(
            {
                "type": "reconnecting",
                "idleSeconds": idle_seconds,
                "backoffSeconds": backoff,
                "attempts": self._reconnect_attempts,
                "maxAttempts": _OPENCODE_STREAM_MAX_STALL_RECONNECT_ATTEMPTS,
                "stalledSeconds": stalled_elapsed_seconds,
                "maxStalledSeconds": _OPENCODE_STREAM_MAX_STALL_RECONNECT_SECONDS,
            }
        )

        if self._stream_iter is not None:
            await _close_stream(self._stream_iter)
        await asyncio.sleep(backoff)
        if self._event_stream_factory is not None:
            self._stream_iter = self._event_stream_factory().__aiter__()
        self._last_relevant_event_at = now
        return True, None

    async def _handle_stall_recovery(self, *, now: float) -> LifecycleDecision:
        idle_seconds = now - self._last_relevant_event_at
        status_type = await self._poll_session_status_warn()

        if status_is_idle(status_type):
            log_event(
                self._logger,
                logging.INFO,
                "opencode.stream.stalled.session_idle",
                session_id=self._session_id,
                status_type=status_type,
                idle_seconds=idle_seconds,
            )
            return LifecycleDecision(
                action=LifecycleAction.BREAK,
                should_flush_if_pending=True,
            )

        if self._last_primary_completion_at is not None:
            log_event(
                self._logger,
                logging.INFO,
                "opencode.stream.stalled.after_completion",
                session_id=self._session_id,
                status_type=status_type,
                idle_seconds=idle_seconds,
            )

        reconnected, reconnect_error = await self._attempt_reconnect(
            now=now,
            idle_seconds=idle_seconds,
            status_type=status_type,
        )

        if not reconnected:
            if status_type and not status_is_idle(status_type):
                while True:
                    await asyncio.sleep(5.0)
                    polled = await self._poll_session_status_quiet()
                    if status_is_idle(polled):
                        break
                return LifecycleDecision(
                    action=LifecycleAction.BREAK,
                    should_flush_if_pending=False,
                )
            return LifecycleDecision(
                action=LifecycleAction.BREAK,
                error=reconnect_error,
                should_flush_if_pending=False,
            )

        return LifecycleDecision(action=LifecycleAction.CONTINUE)


async def _close_stream(iterator: AsyncIterator[SSEEvent]) -> None:
    aclose = getattr(iterator, "aclose", None)
    if aclose is None:
        return
    with suppress(Exception):
        await aclose()
