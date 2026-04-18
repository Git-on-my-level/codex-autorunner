from __future__ import annotations

import asyncio
import inspect
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Any, Callable, Coroutine, TypeVar

from .client import HubControlPlaneClient
from .errors import HubControlPlaneError

ResultT = TypeVar("ResultT")


def build_hub_unavailable_error(
    *,
    operation: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> HubControlPlaneError:
    payload: dict[str, Any] = {"operation": operation}
    if isinstance(details, dict):
        payload.update(details)
    return HubControlPlaneError(
        "hub_unavailable",
        f"Hub control-plane unavailable during {operation}: {message}",
        retryable=True,
        details=payload,
    )


def run_sync_via_thread(
    client: HubControlPlaneClient,
    *,
    operation: str,
    timeout_seconds: float,
    action: Callable[[HubControlPlaneClient], Coroutine[Any, Any, ResultT]],
) -> ResultT:
    def _invoke() -> ResultT:
        background_client = client
        clone = getattr(type(client), "clone_for_background_loop", None)
        if callable(clone) and not inspect.iscoroutinefunction(clone):
            cloned_client = clone(client)
            if cloned_client is not None and not inspect.isawaitable(cloned_client):
                background_client = cloned_client

        async def _run_action() -> ResultT:
            try:
                return await action(background_client)
            finally:
                close = getattr(background_client, "aclose", None)
                if callable(close) and background_client is not client:
                    result = close()
                    if inspect.isawaitable(result):
                        await result

        return asyncio.run(_run_action())

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_invoke)
            return future.result(timeout=timeout_seconds)
    except FuturesTimeoutError as exc:
        raise build_hub_unavailable_error(
            operation=operation,
            message=f"request timed out after {timeout_seconds:g}s",
            details={"timeout_seconds": timeout_seconds},
        ) from exc
    except HubControlPlaneError as exc:
        if exc.code in {"hub_unavailable", "transport_failure"}:
            raise build_hub_unavailable_error(
                operation=operation,
                message=str(exc),
                details={
                    "cause_code": exc.code,
                    **dict(exc.details),
                },
            ) from exc
        raise
    except (ConnectionError, OSError) as exc:
        raise build_hub_unavailable_error(
            operation=operation,
            message=str(exc) or exc.__class__.__name__,
            details={"cause_type": exc.__class__.__name__},
        ) from exc


__all__ = [
    "build_hub_unavailable_error",
    "run_sync_via_thread",
]
