from __future__ import annotations

import asyncio
import inspect
import threading
import time
from typing import Awaitable, Callable, Union

SyncPredicate = Callable[[], bool]
AsyncPredicate = Callable[[], Union[bool, Awaitable[bool]]]


def wait_for_thread_event(
    event: threading.Event,
    *,
    timeout_seconds: float,
    description: str,
) -> None:
    if event.wait(timeout=max(timeout_seconds, 0.0)):
        return
    raise AssertionError(
        f"Timed out after {timeout_seconds:.3f}s waiting for {description}."
    )


def wait_for_predicate(
    predicate: SyncPredicate,
    *,
    timeout_seconds: float,
    description: str,
    poll_interval_seconds: float = 0.01,
) -> None:
    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    poll_interval = max(poll_interval_seconds, 0.001)
    while True:
        if predicate():
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(
                f"Timed out after {timeout_seconds:.3f}s waiting for {description}."
            )
        time.sleep(min(poll_interval, remaining))


async def wait_for_async_event(
    event: asyncio.Event,
    *,
    timeout_seconds: float,
    description: str,
) -> None:
    try:
        await asyncio.wait_for(event.wait(), timeout=max(timeout_seconds, 0.0))
    except asyncio.TimeoutError as exc:
        raise AssertionError(
            f"Timed out after {timeout_seconds:.3f}s waiting for {description}."
        ) from exc


async def wait_for_async_predicate(
    predicate: AsyncPredicate,
    *,
    timeout_seconds: float,
    description: str,
    poll_interval_seconds: float = 0.01,
) -> None:
    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    poll_interval = max(poll_interval_seconds, 0.001)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(
                f"Timed out after {timeout_seconds:.3f}s waiting for {description}."
            )

        result = predicate()
        if inspect.isawaitable(result):
            try:
                condition = await asyncio.wait_for(result, timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise AssertionError(
                    f"Timed out after {timeout_seconds:.3f}s waiting for {description}."
                ) from exc
        else:
            condition = bool(result)
        if condition:
            return

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(
                f"Timed out after {timeout_seconds:.3f}s waiting for {description}."
            )
        await asyncio.sleep(min(poll_interval, remaining))
