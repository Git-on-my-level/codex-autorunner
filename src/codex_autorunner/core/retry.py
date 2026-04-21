from __future__ import annotations

import logging
import random
from functools import wraps
from typing import Any, Callable, Coroutine, TypeVar, cast

from tenacity import (
    before_sleep_log,
    retry_if_exception_type,
    stop_after_attempt,
)
from tenacity import (
    retry as tenacity_retry,
)

from .exceptions import TransientError

T = TypeVar("T")


def _compute_exponential_retry_delay(
    *,
    attempt_number: int,
    base_wait: float,
    max_wait: float,
    jitter: float,
) -> float:
    resolved_max_wait: float = max_wait if max_wait > 0.0 else 0.0
    resolved_base_wait: float = base_wait if base_wait > 0.0 else 0.0
    base_delay: float = min(
        resolved_base_wait * (2 ** max(attempt_number - 1, 0)),
        resolved_max_wait,
    )
    if base_delay <= 0.0 or jitter <= 0.0:
        return base_delay
    jittered_ceiling: float = min(base_delay * (1.0 + jitter), resolved_max_wait)
    if jittered_ceiling <= base_delay:
        return base_delay
    return float(random.uniform(base_delay, jittered_ceiling))


def _build_wait_strategy(
    *,
    base_wait: float,
    max_wait: float,
    jitter: float,
) -> Callable[[Any], float]:
    def _wait(retry_state: Any) -> float:
        attempt_number = int(getattr(retry_state, "attempt_number", 1) or 1)
        return _compute_exponential_retry_delay(
            attempt_number=attempt_number,
            base_wait=base_wait,
            max_wait=max_wait,
            jitter=jitter,
        )

    return _wait


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
            wait=_build_wait_strategy(
                base_wait=base_wait,
                max_wait=max_wait,
                jitter=jitter,
            ),
            retry=retry_if_exception_type(TransientError),
            before_sleep=before_sleep_log(logger, logging.WARNING),
            reraise=True,
        )
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            return cast(T, await func(*args, **kwargs))

        return wrapper

    return decorator
