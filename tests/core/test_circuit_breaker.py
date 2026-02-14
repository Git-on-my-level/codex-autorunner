from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from codex_autorunner.core.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerState,
    CircuitState,
)
from codex_autorunner.core.exceptions import CircuitOpenError


@pytest.fixture
def config():
    return CircuitBreakerConfig(
        failure_threshold=3,
        timeout_seconds=5,
        half_open_attempts=2,
    )


@pytest.fixture
def circuit_breaker(config):
    return CircuitBreaker("test-service", config=config)


class TestCircuitBreakerConfig:
    def test_default_config(self):
        config = CircuitBreakerConfig()
        assert config.failure_threshold == 5
        assert config.timeout_seconds == 60
        assert config.half_open_attempts == 1

    def test_custom_config(self, config):
        assert config.failure_threshold == 3
        assert config.timeout_seconds == 5
        assert config.half_open_attempts == 2


class TestCircuitBreakerState:
    def test_default_state(self):
        state = CircuitBreakerState()
        assert state.failure_count == 0
        assert state.state == CircuitState.CLOSED
        assert state.last_failure_time is None
        assert state.success_count == 0


class TestInitialState:
    @pytest.mark.asyncio
    async def test_circuit_starts_closed(self, circuit_breaker):
        assert circuit_breaker._state.state == CircuitState.CLOSED
        assert circuit_breaker._state.failure_count == 0

    @pytest.mark.asyncio
    async def test_successful_call_in_closed_state(self, circuit_breaker):
        async with circuit_breaker.call():
            pass
        assert circuit_breaker._state.state == CircuitState.CLOSED
        assert circuit_breaker._state.failure_count == 0


class TestFailureThreshold:
    @pytest.mark.asyncio
    async def test_circuit_opens_after_threshold_failures(self, circuit_breaker):
        for _ in range(3):
            try:
                async with circuit_breaker.call():
                    raise RuntimeError("failure")
            except RuntimeError:
                pass
        assert circuit_breaker._state.failure_count == 3
        assert circuit_breaker._state.state == CircuitState.CLOSED
        with pytest.raises(CircuitOpenError):
            async with circuit_breaker.call():
                pass
        assert circuit_breaker._state.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_circuit_remains_closed_before_threshold(self, circuit_breaker):
        for _ in range(2):
            try:
                async with circuit_breaker.call():
                    raise RuntimeError("failure")
            except RuntimeError:
                pass
        assert circuit_breaker._state.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_success_resets_failure_count_in_closed_state(self, circuit_breaker):
        try:
            async with circuit_breaker.call():
                raise RuntimeError("failure")
        except RuntimeError:
            pass
        assert circuit_breaker._state.failure_count == 1
        async with circuit_breaker.call():
            pass
        assert circuit_breaker._state.failure_count == 0


class TestCircuitOpenError:
    @pytest.mark.asyncio
    async def test_raises_circuit_open_error_when_open(self, circuit_breaker):
        for _ in range(3):
            try:
                async with circuit_breaker.call():
                    raise RuntimeError("failure")
            except RuntimeError:
                pass
        with pytest.raises(CircuitOpenError) as exc_info:
            async with circuit_breaker.call():
                pass
        assert exc_info.value.service_name == "test-service"

    @pytest.mark.asyncio
    async def test_circuit_open_error_includes_service_name(self, circuit_breaker):
        for _ in range(3):
            try:
                async with circuit_breaker.call():
                    raise RuntimeError("failure")
            except RuntimeError:
                pass
        with pytest.raises(CircuitOpenError) as exc_info:
            async with circuit_breaker.call():
                pass
        assert "test-service" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_circuit_open_error_is_critical_error(self, circuit_breaker):
        from codex_autorunner.core.exceptions import CriticalError

        for _ in range(3):
            try:
                async with circuit_breaker.call():
                    raise RuntimeError("failure")
            except RuntimeError:
                pass
        with pytest.raises(CriticalError):
            async with circuit_breaker.call():
                pass


class TestTimeoutBasedRecovery:
    @pytest.mark.asyncio
    async def test_circuit_transitions_to_half_open_after_timeout(
        self, circuit_breaker
    ):
        for _ in range(3):
            try:
                async with circuit_breaker.call():
                    raise RuntimeError("failure")
            except RuntimeError:
                pass
        with pytest.raises(CircuitOpenError):
            async with circuit_breaker.call():
                pass
        assert circuit_breaker._state.state == CircuitState.OPEN
        past_time = datetime.utcnow() - timedelta(seconds=10)
        circuit_breaker._state.last_failure_time = past_time
        async with circuit_breaker.call():
            pass
        assert circuit_breaker._state.state == CircuitState.HALF_OPEN

    @pytest.mark.asyncio
    async def test_circuit_remains_open_before_timeout(self, circuit_breaker):
        for _ in range(3):
            try:
                async with circuit_breaker.call():
                    raise RuntimeError("failure")
            except RuntimeError:
                pass
        circuit_breaker._state.last_failure_time = datetime.utcnow()
        with pytest.raises(CircuitOpenError):
            async with circuit_breaker.call():
                pass


class TestSuccessCountForClosing:
    @pytest.mark.asyncio
    async def test_circuit_closes_after_enough_successes_in_half_open(
        self, circuit_breaker
    ):
        for _ in range(3):
            try:
                async with circuit_breaker.call():
                    raise RuntimeError("failure")
            except RuntimeError:
                pass
        with pytest.raises(CircuitOpenError):
            async with circuit_breaker.call():
                pass
        past_time = datetime.utcnow() - timedelta(seconds=10)
        circuit_breaker._state.last_failure_time = past_time
        async with circuit_breaker.call():
            pass
        assert circuit_breaker._state.state == CircuitState.HALF_OPEN
        assert circuit_breaker._state.success_count == 1
        async with circuit_breaker.call():
            pass
        assert circuit_breaker._state.state == CircuitState.CLOSED
        assert circuit_breaker._state.failure_count == 0
        assert circuit_breaker._state.success_count == 0

    @pytest.mark.asyncio
    async def test_half_open_requires_multiple_successes(self):
        config = CircuitBreakerConfig(
            failure_threshold=2,
            timeout_seconds=5,
            half_open_attempts=3,
        )
        cb = CircuitBreaker("test-service", config=config)
        for _ in range(2):
            try:
                async with cb.call():
                    raise RuntimeError("failure")
            except RuntimeError:
                pass
        with pytest.raises(CircuitOpenError):
            async with cb.call():
                pass
        past_time = datetime.utcnow() - timedelta(seconds=10)
        cb._state.last_failure_time = past_time
        async with cb.call():
            pass
        assert cb._state.state == CircuitState.HALF_OPEN
        assert cb._state.success_count == 1
        async with cb.call():
            pass
        assert cb._state.state == CircuitState.HALF_OPEN
        assert cb._state.success_count == 2
        async with cb.call():
            pass
        assert cb._state.state == CircuitState.CLOSED


class TestFailureInHalfOpenState:
    @pytest.mark.asyncio
    async def test_failure_in_half_open_reopens_circuit(self, circuit_breaker):
        for _ in range(3):
            try:
                async with circuit_breaker.call():
                    raise RuntimeError("failure")
            except RuntimeError:
                pass
        with pytest.raises(CircuitOpenError):
            async with circuit_breaker.call():
                pass
        past_time = datetime.utcnow() - timedelta(seconds=10)
        circuit_breaker._state.last_failure_time = past_time
        async with circuit_breaker.call():
            pass
        assert circuit_breaker._state.state == CircuitState.HALF_OPEN
        try:
            async with circuit_breaker.call():
                raise RuntimeError("failure in half-open")
        except RuntimeError:
            pass
        assert circuit_breaker._state.state == CircuitState.OPEN


class TestStateTransitions:
    @pytest.mark.asyncio
    async def test_full_cycle_closed_to_open_to_half_open_to_closed(
        self, circuit_breaker
    ):
        assert circuit_breaker._state.state == CircuitState.CLOSED
        for _ in range(3):
            try:
                async with circuit_breaker.call():
                    raise RuntimeError("failure")
            except RuntimeError:
                pass
        with pytest.raises(CircuitOpenError):
            async with circuit_breaker.call():
                pass
        assert circuit_breaker._state.state == CircuitState.OPEN
        past_time = datetime.utcnow() - timedelta(seconds=10)
        circuit_breaker._state.last_failure_time = past_time
        async with circuit_breaker.call():
            pass
        assert circuit_breaker._state.state == CircuitState.HALF_OPEN
        async with circuit_breaker.call():
            pass
        assert circuit_breaker._state.state == CircuitState.CLOSED


class TestConcurrentAccess:
    @pytest.mark.asyncio
    async def test_concurrent_successful_calls(self, circuit_breaker):
        async def make_call():
            async with circuit_breaker.call():
                await asyncio.sleep(0.01)

        await asyncio.gather(*[make_call() for _ in range(10)])
        assert circuit_breaker._state.state == CircuitState.CLOSED
        assert circuit_breaker._state.failure_count == 0

    @pytest.mark.asyncio
    async def test_concurrent_failures_trigger_open(self, circuit_breaker):
        async def make_failing_call():
            try:
                async with circuit_breaker.call():
                    await asyncio.sleep(0.01)
                    raise RuntimeError("failure")
            except RuntimeError:
                pass

        await asyncio.gather(*[make_failing_call() for _ in range(5)])
        assert circuit_breaker._state.failure_count >= 3
        with pytest.raises(CircuitOpenError):
            async with circuit_breaker.call():
                pass
        assert circuit_breaker._state.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_concurrent_mixed_calls(self, circuit_breaker):
        async def make_call(fail=False):
            try:
                async with circuit_breaker.call():
                    await asyncio.sleep(0.01)
                    if fail:
                        raise RuntimeError("failure")
            except RuntimeError:
                pass

        await asyncio.gather(
            *[make_call(fail=True) for _ in range(2)],
            *[make_call(fail=False) for _ in range(3)],
        )
        assert circuit_breaker._state.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_lock_prevents_race_condition_on_state_change(self, circuit_breaker):
        call_count = 0

        async def make_failing_call():
            nonlocal call_count
            try:
                async with circuit_breaker.call():
                    call_count += 1
                    await asyncio.sleep(0.05)
                    raise RuntimeError("failure")
            except RuntimeError:
                pass

        await asyncio.gather(*[make_failing_call() for _ in range(10)])
        assert call_count == 10
        with pytest.raises(CircuitOpenError):
            async with circuit_breaker.call():
                pass
        assert circuit_breaker._state.state == CircuitState.OPEN


class TestExceptionPropagation:
    @pytest.mark.asyncio
    async def test_exception_propagates_from_call(self, circuit_breaker):
        class CustomError(Exception):
            pass

        with pytest.raises(CustomError):
            async with circuit_breaker.call():
                raise CustomError("custom error")

    @pytest.mark.asyncio
    async def test_exception_counted_as_failure(self, circuit_breaker):
        try:
            async with circuit_breaker.call():
                raise ValueError("error")
        except ValueError:
            pass
        assert circuit_breaker._state.failure_count == 1


class TestServiceName:
    def test_service_name_stored(self, circuit_breaker):
        assert circuit_breaker._service_name == "test-service"

    def test_different_services_independent(self, config):
        cb1 = CircuitBreaker("service-1", config=config)
        cb2 = CircuitBreaker("service-2", config=config)
        assert cb1._service_name == "service-1"
        assert cb2._service_name == "service-2"


class TestConfigOverride:
    def test_uses_provided_config(self):
        config = CircuitBreakerConfig(
            failure_threshold=10,
            timeout_seconds=30,
            half_open_attempts=5,
        )
        cb = CircuitBreaker("test", config=config)
        assert cb._config.failure_threshold == 10
        assert cb._config.timeout_seconds == 30
        assert cb._config.half_open_attempts == 5

    def test_uses_default_config_when_none_provided(self):
        cb = CircuitBreaker("test")
        assert cb._config.failure_threshold == 5
        assert cb._config.timeout_seconds == 60
        assert cb._config.half_open_attempts == 1
