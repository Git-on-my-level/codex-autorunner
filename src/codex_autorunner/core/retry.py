from __future__ import annotations

import logging
from functools import wraps
from typing import Any, Callable, Coroutine, TypeVar, cast

from tenacity import (
    before_sleep_log,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)
from tenacity import (
    retry as tenacity_retry,
)

from .exceptions import TransientError

T = TypeVar("T")


def retry_transient(
    max_attempts: int = 5,
    base_wait: float = 1.0,
    max_wait: float = 60.0,
    jitter: float = 0.1,
) -> Any:
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
        func: Callable[..., Coroutine[Any, Any, T]],
    ) -> Callable[..., Coroutine[Any, Any, T]]:
        @wraps(func)
        @tenacity_retry(
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=base_wait, max=max_wait, exp_base=2),
            retry=retry_if_exception_type(TransientError),
            before_sleep=before_sleep_log(logger, logging.WARNING),
            reraise=True,
        )
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            return cast(T, await func(*args, **kwargs))

        return wrapper

    return decorator
