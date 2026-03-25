from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

from .publish_journal import PublishJournalStore, PublishOperation

DEFAULT_PUBLISH_RETRY_DELAYS_SECONDS = (0.0, 30.0, 300.0)


class PublishActionExecutor(Protocol):
    def __call__(self, operation: PublishOperation) -> Optional[dict[str, Any]]: ...


class PublishExecutionError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: Optional[float] = None,
    ) -> None:
        super().__init__(message)
        if retry_after_seconds is None:
            self.retry_after_seconds: Optional[float] = None
        else:
            self.retry_after_seconds = max(float(retry_after_seconds), 0.0)


class RetryablePublishError(PublishExecutionError):
    pass


class TerminalPublishError(PublishExecutionError):
    pass


def _normalize_action_type(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("action_type is required")
    return normalized


def _normalize_retry_delays(
    retry_delays_seconds: Sequence[float],
) -> tuple[float, ...]:
    normalized: list[float] = []
    for delay in retry_delays_seconds:
        normalized.append(max(float(delay), 0.0))
    return tuple(normalized)


def _coerce_now(
    now_fn: Optional[Callable[[], datetime]],
) -> datetime:
    current = now_fn() if callable(now_fn) else datetime.now(timezone.utc)
    if not isinstance(current, datetime):
        raise TypeError("now_fn must return a datetime")
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_error_text(exc: Exception) -> str:
    details = str(exc).strip()
    if details:
        return f"{exc.__class__.__name__}: {details}"
    return exc.__class__.__name__


def _resolve_retry_at(
    operation: PublishOperation,
    exc: Exception,
    *,
    retry_delays_seconds: tuple[float, ...],
    current_time: datetime,
) -> Optional[str]:
    if isinstance(exc, TerminalPublishError):
        return None
    attempt_index = max(operation.attempt_count - 1, 0)
    if attempt_index >= len(retry_delays_seconds):
        return None
    retry_after_seconds = (
        exc.retry_after_seconds
        if isinstance(exc, PublishExecutionError)
        else retry_delays_seconds[attempt_index]
    )
    if retry_after_seconds is None:
        retry_after_seconds = retry_delays_seconds[attempt_index]
    retry_at = current_time + timedelta(seconds=max(retry_after_seconds, 0.0))
    return _format_timestamp(retry_at)


class PublishExecutorRegistry:
    def __init__(
        self,
        executors: Optional[Mapping[str, PublishActionExecutor]] = None,
    ) -> None:
        self._executors: dict[str, PublishActionExecutor] = {}
        if executors is not None:
            for action_type, executor in executors.items():
                self.register(action_type, executor)

    def register(self, action_type: str, executor: PublishActionExecutor) -> None:
        normalized_action_type = _normalize_action_type(action_type)
        self._executors[normalized_action_type] = executor

    def dispatch(self, operation: PublishOperation) -> dict[str, Any]:
        # The journal stores the canonical action key as operation_kind.
        action_type = _normalize_action_type(operation.operation_kind)
        executor = self._executors.get(action_type)
        if executor is None:
            raise TerminalPublishError(
                f"No publish executor registered for action_type '{action_type}'"
            )
        response = executor(operation)
        if response is None:
            return {}
        if not isinstance(response, dict):
            raise TerminalPublishError(
                f"Publish executor '{action_type}' returned a non-object response"
            )
        return dict(response)


def drain_pending_publish_operations(
    journal: PublishJournalStore,
    *,
    executor_registry: PublishExecutorRegistry,
    limit: int = 10,
    retry_delays_seconds: Sequence[float] = DEFAULT_PUBLISH_RETRY_DELAYS_SECONDS,
    now_fn: Optional[Callable[[], datetime]] = None,
) -> list[PublishOperation]:
    resolved_limit = max(int(limit), 0)
    if resolved_limit <= 0:
        return []
    resolved_retry_delays = _normalize_retry_delays(retry_delays_seconds)
    current_time = _coerce_now(now_fn)
    claimed = journal.claim_pending_operations(
        limit=resolved_limit,
        now_timestamp=_format_timestamp(current_time),
    )
    processed: list[PublishOperation] = []
    for operation in claimed:
        current_operation = journal.mark_running(operation.operation_id) or operation
        try:
            response = executor_registry.dispatch(current_operation)
        except Exception as exc:
            failed = journal.mark_failed(
                current_operation.operation_id,
                error_text=_resolve_error_text(exc),
                next_attempt_at=_resolve_retry_at(
                    current_operation,
                    exc,
                    retry_delays_seconds=resolved_retry_delays,
                    current_time=current_time,
                ),
            )
            if failed is not None:
                processed.append(failed)
            continue
        succeeded = journal.mark_succeeded(
            current_operation.operation_id,
            response=response,
        )
        if succeeded is not None:
            processed.append(succeeded)
    return processed


class PublishOperationProcessor:
    def __init__(
        self,
        journal: PublishJournalStore,
        *,
        executors: PublishExecutorRegistry | Mapping[str, PublishActionExecutor],
        retry_delays_seconds: Sequence[float] = DEFAULT_PUBLISH_RETRY_DELAYS_SECONDS,
        now_fn: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._journal = journal
        if isinstance(executors, PublishExecutorRegistry):
            self._executor_registry = executors
        else:
            self._executor_registry = PublishExecutorRegistry(executors)
        self._retry_delays_seconds = _normalize_retry_delays(retry_delays_seconds)
        self._now_fn = now_fn

    def process_now(self, limit: int = 10) -> list[PublishOperation]:
        return drain_pending_publish_operations(
            self._journal,
            executor_registry=self._executor_registry,
            limit=limit,
            retry_delays_seconds=self._retry_delays_seconds,
            now_fn=self._now_fn,
        )


__all__ = [
    "DEFAULT_PUBLISH_RETRY_DELAYS_SECONDS",
    "PublishActionExecutor",
    "PublishExecutionError",
    "PublishExecutorRegistry",
    "PublishOperationProcessor",
    "RetryablePublishError",
    "TerminalPublishError",
    "drain_pending_publish_operations",
]
