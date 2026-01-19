from __future__ import annotations

import logging
from functools import wraps
from typing import Any, Callable, Coroutine, ParamSpec, TypeVar, cast

from tenacity import (
    after_log,
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .exceptions import TransientError

P = ParamSpec("P")
T = TypeVar("T")


def retry_transient(
    max_attempts: int = 5,
    base_wait: float = 1.0,
    max_wait: float = 60.0,
    jitter: float = 0.1,
) -> Callable[[Callable[P, Coroutine[Any, T]]], Callable[P, Coroutine[Any, T]]]:
    """
    Decorator for retrying transient errors with exponential backoff.

    Args:
        max_attempts: Maximum number of retry attempts (default: 5)
        base_wait: Base wait time in seconds before exponential backoff (default: 1.0)
        max_wait: Maximum wait time in seconds between retries (default: 60.0)
        jitter: Jitter ratio to add to wait times (default: 0.1)

    Returns:
        A decorator that wraps async functions with retry logic.

    Raises:
        RetryError: If all retry attempts are exhausted.
    """
    logger = logging.getLogger(__name__)

    def decorator(
        func: Callable[P, Coroutine[Any, T]],
    ) -> Callable[P, Coroutine[Any, T]]:
        @wraps(func)
        @retry(
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=base_wait, max=max_wait, exp_base=2),
            retry=retry_if_exception_type(TransientError),
            before_sleep=before_sleep_log(logger, logging.WARNING),
            after_log=after_log(logger, logging.INFO),
            reraise=True,
        )
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            return cast(T, await func(*args, **kwargs))

        return wrapper

    return decorator
