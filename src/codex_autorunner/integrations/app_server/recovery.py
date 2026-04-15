from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

from ...core.exceptions import CircuitOpenError
from ...core.logging_utils import log_event
from .errors import CodexAppServerError
from .protocol_helpers import extract_resume_snapshot
from .turn_state import TurnState, TurnStateManager, status_is_terminal

ResumeFn = Callable[[str], Awaitable[Dict[str, Any]]]
DispatchNotificationFn = Callable[[Dict[str, Any]], None]

_RECOVERY_EXCEPTION_TYPES: tuple[type[Exception], ...] = (
    CodexAppServerError,
    CircuitOpenError,
    asyncio.TimeoutError,
    ConnectionError,
    OSError,
)


@dataclass
class RecoveryConfig:
    stall_timeout_seconds: Optional[float] = None
    stall_recovery_min_interval_seconds: float = 10.0
    stall_max_recovery_attempts: Optional[int] = 8
    completion_gap_timeout_seconds: Optional[float] = 15.0


@dataclass
class _ResumeAttempt:
    applied_status: Optional[str]
    effective_status: Optional[str]
    exception: Optional[Exception] = None
    snapshot_missing: bool = False


class TurnRecoveryCoordinator:
    def __init__(
        self,
        config: RecoveryConfig,
        *,
        logger: logging.Logger,
        turn_state_manager: TurnStateManager,
        dispatch_recovered_notification: Optional[DispatchNotificationFn] = None,
    ) -> None:
        self._config = config
        self._logger = logger
        self._turn_state_manager = turn_state_manager
        self._dispatch = dispatch_recovered_notification

    @property
    def config(self) -> RecoveryConfig:
        return self._config

    def check_stall_recovery_needed(
        self,
        state: TurnState,
        *,
        deadline: Optional[float],
    ) -> Optional[float]:
        if state.future.done():
            return None
        stall_timeout = self._config.stall_timeout_seconds
        if deadline is None and stall_timeout is None:
            return None
        if deadline is not None and deadline <= time.monotonic():
            raise asyncio.TimeoutError()
        idle_seconds = time.monotonic() - state.last_event_at
        if stall_timeout is not None and idle_seconds >= stall_timeout:
            return idle_seconds
        return None

    def check_completion_gap_recovery_needed(
        self,
        state: TurnState,
        *,
        thread_id: Optional[str],
    ) -> Optional[float]:
        if state.future.done() or state.turn_completed_seen:
            return None
        timeout = self._config.completion_gap_timeout_seconds
        if timeout is None or thread_id is None:
            return None
        started_at = state.completion_gap_started_at
        if started_at is None:
            return None
        now = time.monotonic()
        if state.active_item_ids:
            return None
        if state.last_event_at and now - state.last_event_at < timeout:
            return None
        gap_seconds = now - started_at
        if gap_seconds < timeout:
            return None
        min_interval = self._config.stall_recovery_min_interval_seconds
        if (
            min_interval is not None
            and state.last_completion_gap_recovery_at
            and now - state.last_completion_gap_recovery_at < min_interval
        ):
            return None
        return gap_seconds

    async def _resume_and_apply_snapshot(
        self,
        state: TurnState,
        *,
        turn_id: str,
        thread_id: str,
        resume_fn: ResumeFn,
        recovery_source: str,
    ) -> _ResumeAttempt:
        try:
            resume_result = await resume_fn(thread_id)
        except _RECOVERY_EXCEPTION_TYPES as exc:
            return _ResumeAttempt(
                applied_status=None,
                effective_status=None,
                exception=exc,
            )

        snapshot = extract_resume_snapshot(resume_result, turn_id)
        if snapshot is None:
            return _ResumeAttempt(
                applied_status=None,
                effective_status=None,
                snapshot_missing=True,
            )

        applied_status = self._turn_state_manager.apply_resume_snapshot(state, snapshot)
        effective_status = applied_status or state.status
        is_terminal = effective_status is not None and status_is_terminal(
            effective_status
        )
        if is_terminal and not state.future.done():
            self._emit_recovered_notification(
                state, thread_id=thread_id, recovery_source=recovery_source
            )
            self._turn_state_manager.set_turn_result_if_pending(state)

        return _ResumeAttempt(
            applied_status=applied_status,
            effective_status=effective_status,
        )

    async def maybe_recover_stalled_turn(
        self,
        state: TurnState,
        *,
        turn_id: str,
        thread_id: Optional[str],
        deadline: Optional[float],
        resume_fn: ResumeFn,
    ) -> None:
        idle_seconds = self.check_stall_recovery_needed(state, deadline=deadline)
        if idle_seconds is None:
            return
        await self._recover_stalled_turn(
            state,
            turn_id,
            thread_id=thread_id,
            idle_seconds=idle_seconds,
            resume_fn=resume_fn,
        )

    async def _recover_stalled_turn(
        self,
        state: TurnState,
        turn_id: str,
        *,
        thread_id: Optional[str],
        idle_seconds: float,
        resume_fn: ResumeFn,
    ) -> None:
        now = time.monotonic()
        if thread_id is None:
            state.last_event_at = now
            return
        tid = thread_id
        min_interval = self._config.stall_recovery_min_interval_seconds
        if (
            min_interval is not None
            and state.last_recovery_at
            and now - state.last_recovery_at < min_interval
        ):
            return
        state.last_recovery_at = now
        state.recovery_attempts += 1
        log_event(
            self._logger,
            logging.WARNING,
            "app_server.turn_stalled",
            turn_id=turn_id,
            thread_id=tid,
            idle_seconds=round(idle_seconds, 2),
            last_method=state.last_method,
            recovery_attempts=state.recovery_attempts,
            max_recovery_attempts=self._config.stall_max_recovery_attempts,
        )

        attempt = await self._resume_and_apply_snapshot(
            state,
            turn_id=turn_id,
            thread_id=tid,
            resume_fn=resume_fn,
            recovery_source="turn_stall",
        )

        if attempt.exception is not None:
            log_event(
                self._logger,
                logging.WARNING,
                "app_server.turn_recovery.failed",
                turn_id=turn_id,
                thread_id=tid,
                idle_seconds=round(idle_seconds, 2),
                exc=attempt.exception,
            )
            self._turn_state_manager.maybe_fail_stalled_turn(
                state,
                turn_id=turn_id,
                thread_id=tid,
                idle_seconds=idle_seconds,
                reason="thread_resume_failed",
                recovery_status=state.status,
                max_attempts=self._config.stall_max_recovery_attempts,
            )
            state.last_event_at = now
            return

        if attempt.snapshot_missing:
            self._turn_state_manager.maybe_fail_stalled_turn(
                state,
                turn_id=turn_id,
                thread_id=tid,
                idle_seconds=idle_seconds,
                reason="resume_snapshot_missing",
                recovery_status=state.status,
                max_attempts=self._config.stall_max_recovery_attempts,
            )
            state.last_event_at = now
            return

        if attempt.effective_status is not None and status_is_terminal(
            attempt.effective_status
        ):
            return

        self._turn_state_manager.maybe_fail_stalled_turn(
            state,
            turn_id=turn_id,
            thread_id=tid,
            idle_seconds=idle_seconds,
            reason="resume_non_terminal",
            recovery_status=state.status,
            max_attempts=self._config.stall_max_recovery_attempts,
        )
        state.last_event_at = now

    async def maybe_reconcile_completion_gap(
        self,
        state: TurnState,
        *,
        turn_id: str,
        thread_id: Optional[str],
        resume_fn: ResumeFn,
    ) -> None:
        gap_seconds = self.check_completion_gap_recovery_needed(
            state, thread_id=thread_id
        )
        if gap_seconds is None:
            return

        assert thread_id is not None
        tid = thread_id
        now = time.monotonic()
        state.last_completion_gap_recovery_at = now
        state.completion_gap_recovery_attempts += 1
        log_event(
            self._logger,
            logging.WARNING,
            "app_server.turn_completion_gap",
            turn_id=turn_id,
            thread_id=tid,
            completion_gap_seconds=round(gap_seconds, 2),
            item_completed_count=state.item_completed_count,
            last_method=state.last_method,
            recovery_attempts=state.completion_gap_recovery_attempts,
        )

        attempt = await self._resume_and_apply_snapshot(
            state,
            turn_id=turn_id,
            thread_id=tid,
            resume_fn=resume_fn,
            recovery_source="turn_completion_gap",
        )

        if attempt.exception is not None:
            log_event(
                self._logger,
                logging.WARNING,
                "app_server.turn_completion_gap_recovery.failed",
                turn_id=turn_id,
                thread_id=tid,
                completion_gap_seconds=round(gap_seconds, 2),
                item_completed_count=state.item_completed_count,
                exc=attempt.exception,
            )
            self._turn_state_manager.maybe_fail_completion_gap_turn(
                state,
                turn_id=turn_id,
                thread_id=tid,
                completion_gap_seconds=gap_seconds,
                reason="thread_resume_failed",
                recovery_status=state.status,
                max_attempts=self._config.stall_max_recovery_attempts,
            )
            return

        if attempt.snapshot_missing:
            log_event(
                self._logger,
                logging.WARNING,
                "app_server.turn_completion_gap_recovery.missing_snapshot",
                turn_id=turn_id,
                thread_id=tid,
                completion_gap_seconds=round(gap_seconds, 2),
                item_completed_count=state.item_completed_count,
            )
            self._turn_state_manager.maybe_fail_completion_gap_turn(
                state,
                turn_id=turn_id,
                thread_id=tid,
                completion_gap_seconds=gap_seconds,
                reason="resume_snapshot_missing",
                recovery_status=state.status,
                max_attempts=self._config.stall_max_recovery_attempts,
            )
            return

        effective_status = attempt.effective_status
        if effective_status is not None and status_is_terminal(effective_status):
            log_event(
                self._logger,
                logging.INFO,
                "app_server.turn_completion_gap_recovery.completed",
                turn_id=turn_id,
                thread_id=tid,
                status=effective_status,
                item_completed_count=state.item_completed_count,
                recovery_attempts=state.completion_gap_recovery_attempts,
            )
            return
        if effective_status and not status_is_terminal(effective_status):
            self._turn_state_manager.reset_completion_gap_recovery(state)
            state.last_event_at = now
            log_event(
                self._logger,
                logging.INFO,
                "app_server.turn_completion_gap_recovery.active",
                turn_id=turn_id,
                thread_id=tid,
                status=effective_status,
                item_completed_count=state.item_completed_count,
            )
            return

        log_event(
            self._logger,
            logging.INFO,
            "app_server.turn_completion_gap_recovery.pending",
            turn_id=turn_id,
            thread_id=tid,
            status=state.status,
            item_completed_count=state.item_completed_count,
            recovery_attempts=state.completion_gap_recovery_attempts,
        )
        self._turn_state_manager.maybe_fail_completion_gap_turn(
            state,
            turn_id=turn_id,
            thread_id=tid,
            completion_gap_seconds=gap_seconds,
            reason="resume_non_terminal",
            recovery_status=state.status,
            max_attempts=self._config.stall_max_recovery_attempts,
        )

    def _emit_recovered_notification(
        self,
        state: TurnState,
        *,
        thread_id: str,
        recovery_source: str,
    ) -> None:
        if state.turn_completed_seen:
            return
        params: Dict[str, Any] = {
            "turnId": state.turn_id,
            "threadId": thread_id,
            "status": state.status,
            "recoveredVia": "thread/resume",
            "recoverySource": recovery_source,
            "synthetic": True,
        }
        message: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": "turn/completed",
            "params": params,
        }
        self._turn_state_manager.mark_notification_event(
            state=state, method="turn/completed"
        )
        self._turn_state_manager.apply_turn_completed(state, message, params)
        if self._dispatch is not None:
            self._dispatch(message)


__all__ = [
    "RecoveryConfig",
    "TurnRecoveryCoordinator",
]
