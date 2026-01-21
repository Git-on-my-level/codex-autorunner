from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import AsyncIterator, Optional

from .exceptions import CircuitOpenError

logger = logging.getLogger(__name__)


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker behavior."""

    failure_threshold: int = 5
    """Number of failures before opening circuit."""

    timeout_seconds: int = 60
    """Seconds to wait before attempting recovery (half-open state)."""

    half_open_attempts: int = 1
    """Number of successful calls needed to close circuit from half-open state."""


@dataclass
class CircuitBreakerState:
    """Internal state tracking for circuit breaker."""

    failure_count: int = 0
    state: CircuitState = CircuitState.CLOSED
    last_failure_time: Optional[datetime] = None
    success_count: int = 0


class CircuitBreaker:
    """
    Circuit breaker pattern implementation for external service resilience.

    Opens after N consecutive failures, closes after success or timeout.
    Prevents cascading failures and provides fast-fail for degraded services.
    """

    def __init__(
        self,
        service_name: str,
        *,
        config: Optional[CircuitBreakerConfig] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._service_name = service_name
        self._config = config or CircuitBreakerConfig()
        self._logger = logger or logging.getLogger(__name__)
        self._state = CircuitBreakerState()
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def call(self) -> AsyncIterator[None]:
        """
        Context manager that raises CircuitOpenError if circuit is open.

        Tracks failures/successes to manage circuit state transitions.

        Raises:
            CircuitOpenError: If circuit is open.

        Example:
            async with circuit_breaker.call():
                result = await external_service.request()
        """
        is_open = False
        async with self._lock:
            if self._should_open_circuit():
                self._open_circuit()
                is_open = True
            elif self._should_half_open_circuit():
                self._half_open_circuit()
            elif self._state.state == CircuitState.OPEN:
                is_open = True

        if is_open:
            raise CircuitOpenError(
                self._service_name,
                message=f"Circuit breaker OPEN for {self._service_name}. "
                f"Last failure: {self._state.last_failure_time}",
            )

        try:
            yield
        except Exception as exc:
            self._logger.debug(
                "Exception caught by circuit breaker for %s: %s",
                self._service_name,
                exc,
            )
            await self._record_failure()
            raise
        else:
            await self._record_success()

    def _should_open_circuit(self) -> bool:
        """Check if circuit should open based on failure count."""
        return (
            self._state.state == CircuitState.CLOSED
            and self._state.failure_count >= self._config.failure_threshold
        )

    def _should_half_open_circuit(self) -> bool:
        """Check if circuit should transition to half-open state."""
        if self._state.state != CircuitState.OPEN:
            return False
        if self._state.last_failure_time is None:
            return False
        return datetime.utcnow() >= self._state.last_failure_time + timedelta(
            seconds=self._config.timeout_seconds
        )

    def _open_circuit(self) -> None:
        """Open circuit and log the transition."""
        self._state.state = CircuitState.OPEN
        self._state.last_failure_time = datetime.utcnow()
        self._logger.warning(
            "Circuit breaker OPEN for %s after %d failures",
            self._service_name,
            self._state.failure_count,
        )

    def _half_open_circuit(self) -> None:
        """Transition to half-open state and allow one test call."""
        self._state.state = CircuitState.HALF_OPEN
        self._state.success_count = 0
        self._logger.info(
            "Circuit breaker HALF_OPEN for %s (testing recovery)",
            self._service_name,
        )

    async def _record_failure(self) -> None:
        """Record a failure and update state accordingly."""
        async with self._lock:
            self._state.failure_count += 1
            self._state.last_failure_time = datetime.utcnow()

            if self._state.state == CircuitState.HALF_OPEN:
                self._state.state = CircuitState.OPEN
                self._logger.warning(
                    "Circuit breaker OPEN for %s (failure in half-open state)",
                    self._service_name,
                )
            elif self._state.state == CircuitState.CLOSED:
                self._logger.debug(
                    "Circuit breaker recorded failure for %s (count: %d/%d)",
                    self._service_name,
                    self._state.failure_count,
                    self._config.failure_threshold,
                )

    async def _record_success(self) -> None:
        """Record a success and update state accordingly."""
        async with self._lock:
            if self._state.state == CircuitState.CLOSED:
                self._state.failure_count = 0
            elif self._state.state == CircuitState.HALF_OPEN:
                self._state.success_count += 1
                if self._state.success_count >= self._config.half_open_attempts:
                    self._state.state = CircuitState.CLOSED
                    self._state.failure_count = 0
                    self._state.success_count = 0
                    self._logger.info(
                        "Circuit breaker CLOSED for %s (recovery successful)",
                        self._service_name,
                    )
            else:
                pass
