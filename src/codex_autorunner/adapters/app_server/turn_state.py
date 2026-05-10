from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional, cast

from ...core.logging_utils import log_event
from .protocol_helpers import _extract_agent_message_phase, _extract_agent_message_text

TurnKey = tuple[str, str]


@dataclass
class TurnResult:
    turn_id: str
    status: Optional[str]
    final_message: str
    agent_messages: list[str]
    errors: list[str]
    raw_events: list[dict[str, Any]]
    commentary_messages: list[str] = field(default_factory=list)


@dataclass
class TurnState:
    turn_id: str
    thread_id: Optional[str]
    future: asyncio.Future["TurnResult"]
    agent_messages: list[str] = field(default_factory=list)
    commentary_messages: list[str] = field(default_factory=list)
    final_answer_messages: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    raw_events: list[dict[str, Any]] = field(default_factory=list)
    status: Optional[str] = None
    last_event_at: float = field(default_factory=time.monotonic)
    last_method: Optional[str] = None
    recovery_attempts: int = 0
    last_recovery_at: float = 0.0
    agent_message_deltas: dict[str, str] = field(default_factory=dict)
    turn_completed_seen: bool = False
    completion_settle_task: Optional[asyncio.Task[None]] = None
    item_completed_count: int = 0
    completion_gap_started_at: Optional[float] = None
    active_item_ids: set[str] = field(default_factory=set)
    completion_gap_recovery_attempts: int = 0
    last_completion_gap_recovery_at: float = 0.0


class TurnStateManager:
    def __init__(
        self,
        *,
        logger: logging.Logger,
        output_policy: str,
        completion_settle_seconds: float,
        max_turn_raw_events: int,
    ) -> None:
        self._logger = logger
        self._output_policy = output_policy
        self._completion_settle_seconds = max(float(completion_settle_seconds), 0.0)
        self._max_turn_raw_events = max(int(max_turn_raw_events), 1)
        self.turns: dict[TurnKey, TurnState] = {}
        self.pending_turns: dict[str, TurnState] = {}

    def find_turn_state(
        self, turn_id: str, *, thread_id: Optional[str]
    ) -> tuple[Optional[TurnKey], Optional[TurnState]]:
        key = turn_key(thread_id, turn_id)
        if key is not None:
            state = self.turns.get(key)
            if state is not None:
                return key, state
        matches = [
            (candidate_key, state)
            for candidate_key, state in self.turns.items()
            if candidate_key[1] == turn_id
        ]
        if len(matches) == 1:
            candidate_key, state = matches[0]
            if key is not None and candidate_key != key:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "app_server.turn.thread_mismatch",
                    turn_id=turn_id,
                    requested_thread_id=thread_id,
                    actual_thread_id=candidate_key[0],
                )
            return candidate_key, state
        if len(matches) > 1:
            log_event(
                self._logger,
                logging.WARNING,
                "app_server.turn.ambiguous",
                turn_id=turn_id,
                matches=len(matches),
            )
        return None, None

    def ensure_turn_state(self, turn_id: str, thread_id: str) -> TurnState:
        key = turn_key(thread_id, turn_id)
        if key is None:
            raise ValueError("turn state missing thread id")
        state = self.turns.get(key)
        if state is not None:
            return state
        loop = asyncio.get_running_loop()
        future = cast(asyncio.Future[TurnResult], loop.create_future())
        state = TurnState(turn_id=turn_id, thread_id=thread_id, future=future)
        self.turns[key] = state
        return state

    def ensure_pending_turn_state(self, turn_id: str) -> TurnState:
        state = self.pending_turns.get(turn_id)
        if state is not None:
            return state
        loop = asyncio.get_running_loop()
        future = cast(asyncio.Future[TurnResult], loop.create_future())
        state = TurnState(turn_id=turn_id, thread_id=None, future=future)
        self.pending_turns[turn_id] = state
        return state

    def register_turn_state(self, turn_id: str, thread_id: str) -> TurnState:
        key = turn_key(thread_id, turn_id)
        if key is None:
            raise ValueError("turn/start missing thread id")
        pending = self.pending_turns.pop(turn_id, None)
        state = self.turns.get(key)
        if pending is not None:
            if state is None:
                pending.thread_id = thread_id
                self.turns[key] = pending
                return pending
            self.merge_turn_state(state, pending)
            return state
        if state is None:
            return self.ensure_turn_state(turn_id, thread_id)
        return state

    def resolve_notification_turn_state(
        self,
        turn_id: Optional[str],
        thread_id: Optional[str],
        *,
        create_pending: bool = True,
    ) -> Optional[TurnState]:
        if not turn_id:
            return None
        _key, state = self.find_turn_state(turn_id, thread_id=thread_id)
        if state is not None:
            return state
        if thread_id:
            return self.ensure_turn_state(turn_id, thread_id)
        if create_pending:
            return self.ensure_pending_turn_state(turn_id)
        return None

    def merge_turn_state(self, target: TurnState, source: TurnState) -> None:
        target_last_event_at = target.last_event_at
        source_last_event_at = source.last_event_at
        if not target.agent_messages:
            target.agent_messages = list(source.agent_messages)
        else:
            target.agent_messages.extend(source.agent_messages)
        if not target.commentary_messages:
            target.commentary_messages = list(source.commentary_messages)
        else:
            target.commentary_messages.extend(source.commentary_messages)
        if not target.final_answer_messages:
            target.final_answer_messages = list(source.final_answer_messages)
        else:
            target.final_answer_messages.extend(source.final_answer_messages)
        if source.agent_message_deltas:
            target.agent_message_deltas.update(source.agent_message_deltas)
        if not target.raw_events:
            target.raw_events = list(source.raw_events)
        else:
            target.raw_events.extend(source.raw_events)
        self.trim_raw_events(target)
        if not target.errors:
            target.errors = list(source.errors)
        else:
            target.errors.extend(source.errors)
        if source.last_event_at > target.last_event_at:
            target.last_event_at = source.last_event_at
            target.last_method = source.last_method
        elif target.last_method is None and source.last_method is not None:
            target.last_method = source.last_method
        target.turn_completed_seen = (
            target.turn_completed_seen or source.turn_completed_seen
        )
        target.item_completed_count = max(
            target.item_completed_count, source.item_completed_count
        )
        if source_last_event_at > target_last_event_at:
            target.completion_gap_started_at = source.completion_gap_started_at
            target.completion_gap_recovery_attempts = (
                source.completion_gap_recovery_attempts
            )
            target.last_completion_gap_recovery_at = (
                source.last_completion_gap_recovery_at
            )
        elif source_last_event_at == target_last_event_at:
            if (
                target.completion_gap_started_at is None
                or source.completion_gap_started_at is None
            ):
                target.completion_gap_started_at = None
            elif source.completion_gap_started_at is not None:
                target.completion_gap_started_at = max(
                    target.completion_gap_started_at,
                    source.completion_gap_started_at,
                )
            target.completion_gap_recovery_attempts = max(
                target.completion_gap_recovery_attempts,
                source.completion_gap_recovery_attempts,
            )
            target.last_completion_gap_recovery_at = max(
                target.last_completion_gap_recovery_at,
                source.last_completion_gap_recovery_at,
            )
        if target.turn_completed_seen or source.turn_completed_seen:
            target.active_item_ids.clear()
        elif source.active_item_ids:
            target.active_item_ids.update(source.active_item_ids)
        if target.status is None and source.status is not None:
            target.status = source.status
        if source.future.done() and not target.future.done():
            self.set_turn_result_if_pending(target)
            return
        if source.turn_completed_seen and not target.future.done():
            self.schedule_turn_completion_settle(target)
        self.cancel_turn_completion_settle(source)

    def reset_completion_gap_recovery(self, state: TurnState) -> None:
        state.completion_gap_started_at = None
        state.completion_gap_recovery_attempts = 0
        state.last_completion_gap_recovery_at = 0.0

    def mark_notification_event(self, *, state: TurnState, method: str) -> None:
        state.last_event_at = time.monotonic()
        state.last_method = method
        if state.turn_completed_seen:
            return
        self.reset_completion_gap_recovery(state)

    def apply_resume_snapshot(
        self,
        state: TurnState,
        snapshot: tuple[Optional[str], list[str], list[str], list[str], list[str]],
    ) -> Optional[str]:
        status, agent_messages, commentary_messages, final_answer_messages, errors = (
            snapshot
        )
        if agent_messages:
            state.agent_messages = agent_messages
            state.commentary_messages = commentary_messages
            state.final_answer_messages = final_answer_messages
            state.agent_message_deltas.clear()
        if errors:
            state.errors.extend(errors)
        if status:
            state.status = status
        return status

    def maybe_fail_stalled_turn(
        self,
        state: TurnState,
        *,
        turn_id: str,
        thread_id: str,
        idle_seconds: float,
        reason: str,
        recovery_status: Optional[str],
        max_attempts: Optional[int],
    ) -> None:
        if state.future.done():
            return
        if max_attempts is None or state.recovery_attempts < max_attempts:
            return
        error = (
            "Turn stalled and recovery exhausted: "
            f"attempts={state.recovery_attempts}, "
            f"max_attempts={max_attempts}, "
            f"reason={reason}, "
            f"last_method={state.last_method or 'unknown'}, "
            f"status={recovery_status or state.status or 'unknown'}."
        )
        state.status = "failed"
        state.errors.append(error)
        state.raw_events.append(
            {
                "method": "turn/stalledRecoveryExhausted",
                "params": {
                    "turnId": turn_id,
                    "threadId": thread_id,
                    "reason": reason,
                    "recoveryAttempts": state.recovery_attempts,
                    "maxRecoveryAttempts": max_attempts,
                    "lastMethod": state.last_method,
                    "status": recovery_status or state.status,
                    "idleSeconds": round(idle_seconds, 2),
                },
            }
        )
        self.trim_raw_events(state)
        log_event(
            self._logger,
            logging.ERROR,
            "app_server.turn_recovery.exhausted",
            turn_id=turn_id,
            thread_id=thread_id,
            reason=reason,
            idle_seconds=round(idle_seconds, 2),
            last_method=state.last_method,
            status=recovery_status or state.status,
            recovery_attempts=state.recovery_attempts,
            max_recovery_attempts=max_attempts,
        )
        self.set_turn_result_if_pending(state)

    def maybe_fail_completion_gap_turn(
        self,
        state: TurnState,
        *,
        turn_id: str,
        thread_id: str,
        completion_gap_seconds: float,
        reason: str,
        recovery_status: Optional[str],
        max_attempts: Optional[int],
    ) -> None:
        if state.future.done():
            return
        if (
            max_attempts is None
            or state.completion_gap_recovery_attempts < max_attempts
        ):
            return
        error = (
            "Turn completion-gap recovery exhausted: "
            f"attempts={state.completion_gap_recovery_attempts}, "
            f"max_attempts={max_attempts}, "
            f"reason={reason}, "
            f"last_method={state.last_method or 'unknown'}, "
            f"status={recovery_status or state.status or 'unknown'}, "
            f"item_completed_count={state.item_completed_count}."
        )
        state.status = "failed"
        state.errors.append(error)
        state.raw_events.append(
            {
                "method": "turn/completionGapRecoveryExhausted",
                "params": {
                    "turnId": turn_id,
                    "threadId": thread_id,
                    "reason": reason,
                    "recoveryAttempts": state.completion_gap_recovery_attempts,
                    "maxRecoveryAttempts": max_attempts,
                    "lastMethod": state.last_method,
                    "status": recovery_status or state.status,
                    "completionGapSeconds": round(completion_gap_seconds, 2),
                    "itemCompletedCount": state.item_completed_count,
                },
            }
        )
        self.trim_raw_events(state)
        log_event(
            self._logger,
            logging.ERROR,
            "app_server.turn_completion_gap_recovery.exhausted",
            turn_id=turn_id,
            thread_id=thread_id,
            completion_gap_seconds=round(completion_gap_seconds, 2),
            recovery_attempts=state.completion_gap_recovery_attempts,
            max_recovery_attempts=max_attempts,
            reason=reason,
            last_method=state.last_method,
            status=recovery_status or state.status,
            item_completed_count=state.item_completed_count,
        )
        self.set_turn_result_if_pending(state)

    def apply_item_completed(
        self,
        state: TurnState,
        message: dict[str, Any],
        params: Any,
        decoded: Any = None,
    ) -> None:
        item = params.get("item") if isinstance(params, dict) else None
        item_id = extract_notification_item_id(params, decoded)
        matched_active_item_id = item_id if isinstance(item_id, str) else None
        if item_id is not None:
            state.active_item_ids.discard(item_id)
        text: Optional[str] = None
        if isinstance(item, dict) and item.get("type") == "agentMessage":
            delta_text: Optional[str] = None
            text = _extract_agent_message_text(item)
            phase = _extract_agent_message_phase(item)
            if isinstance(item_id, str):
                delta_text = state.agent_message_deltas.pop(item_id, None)
            elif text:
                matched_active_item_id = prune_unambiguous_stale_delta(
                    state.agent_message_deltas,
                    completed_text=text,
                )
            if not text:
                text = delta_text
            append_agent_message_for_phase(state, text, phase=phase)
        if item_id is None:
            discard_completed_active_item(
                state.active_item_ids,
                matched_item_id=matched_active_item_id,
            )
        review_text = extract_review_text(item)
        if review_text and review_text != text:
            append_agent_message(state.agent_messages, review_text)
        state.item_completed_count += 1
        if not state.turn_completed_seen:
            state.completion_gap_started_at = state.last_event_at
        item_type = item.get("type") if isinstance(item, dict) else None
        log_event(
            self._logger,
            logging.INFO,
            "app_server.item.completed",
            turn_id=state.turn_id,
            thread_id=state.thread_id,
            item_type=item_type,
            item_completed_count=state.item_completed_count,
        )
        self.record_raw_event(state, message)

    def apply_error(
        self,
        state: TurnState,
        message: dict[str, Any],
        params: Any,
        decoded: Any = None,
    ) -> None:
        error_message = getattr(decoded, "message", None) or extract_error_message(
            params
        )
        if error_message:
            state.errors.append(error_message)
        error_payload = params.get("error") if isinstance(params, dict) else None
        error_code = getattr(decoded, "code", None)
        if error_code is None:
            error_code = (
                error_payload.get("code") if isinstance(error_payload, dict) else None
            )
        will_retry = getattr(decoded, "will_retry", None)
        if will_retry is None and isinstance(params, dict):
            will_retry = params.get("willRetry")
        log_event(
            self._logger,
            logging.WARNING,
            "app_server.turn_error",
            turn_id=state.turn_id,
            thread_id=state.thread_id,
            message=error_message,
            code=error_code,
            will_retry=will_retry,
        )
        self.record_raw_event(state, message)

    def apply_turn_completed(
        self,
        state: TurnState,
        message: dict[str, Any],
        params: Any,
        decoded: Any = None,
    ) -> None:
        self.record_raw_event(state, message)
        status = getattr(decoded, "status", None)
        if status is None and isinstance(params, dict):
            status = params.get("status")
            if status is None and isinstance(params.get("turn"), dict):
                turn_status = params["turn"].get("status")
                if isinstance(turn_status, dict):
                    status = turn_status.get("type") or turn_status.get("status")
                elif isinstance(turn_status, str):
                    status = turn_status
        state.status = status if status is not None else state.status
        log_event(
            self._logger,
            logging.INFO,
            "app_server.turn.completed",
            turn_id=state.turn_id,
            thread_id=state.thread_id,
            status=state.status,
        )
        state.turn_completed_seen = True
        state.active_item_ids.clear()
        state.completion_gap_started_at = None
        if status_prefers_completion_settle(state.status) or not status_is_terminal(
            state.status
        ):
            self.schedule_turn_completion_settle(state)
            return
        self.set_turn_result_if_pending(state)

    def build_turn_result(self, state: TurnState) -> TurnResult:
        return TurnResult(
            turn_id=state.turn_id,
            status=state.status,
            final_message=final_message_for_result(state, policy=self._output_policy),
            agent_messages=agent_messages_for_result(state),
            commentary_messages=list(state.commentary_messages),
            errors=list(state.errors),
            raw_events=list(state.raw_events),
        )

    def cancel_turn_completion_settle(self, state: TurnState) -> None:
        settle_task = state.completion_settle_task
        if settle_task is not None and not settle_task.done():
            settle_task.cancel()
        state.completion_settle_task = None

    def set_turn_result_if_pending(
        self, state: TurnState, *, cancel_settle_task: bool = True
    ) -> None:
        if state.future.done():
            return
        if cancel_settle_task:
            self.cancel_turn_completion_settle(state)
        state.future.set_result(self.build_turn_result(state))

    def schedule_turn_completion_settle(self, state: TurnState) -> None:
        if state.future.done():
            return
        if self._completion_settle_seconds <= 0:
            self.set_turn_result_if_pending(state)
            return
        self.cancel_turn_completion_settle(state)

        async def _finalize_after_settle() -> None:
            try:
                await asyncio.sleep(self._completion_settle_seconds)
            except asyncio.CancelledError:
                return
            state.completion_settle_task = None
            self.set_turn_result_if_pending(state, cancel_settle_task=False)

        state.completion_settle_task = asyncio.create_task(_finalize_after_settle())

    def record_raw_event(self, state: TurnState, message: dict[str, Any]) -> None:
        state.raw_events.append(message)
        self.trim_raw_events(state)

    def trim_raw_events(self, state: TurnState) -> None:
        if len(state.raw_events) > self._max_turn_raw_events:
            state.raw_events = state.raw_events[-self._max_turn_raw_events :]


def turn_key(thread_id: Optional[str], turn_id: Optional[str]) -> Optional[TurnKey]:
    if not thread_id or not turn_id:
        return None
    return (thread_id, turn_id)


def extract_review_text(item: Any) -> Optional[str]:
    if not isinstance(item, dict):
        return None
    exited = item.get("exitedReviewMode")
    if isinstance(exited, dict):
        review = exited.get("review")
        if isinstance(review, str) and review.strip():
            return review
    if item.get("type") == "review":
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            return text
    review = item.get("review")
    if isinstance(review, str) and review.strip():
        return review
    return None


def extract_error_message(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    message: Optional[str] = None
    details: Optional[str] = None
    if isinstance(error, dict):
        raw_message = error.get("message")
        if isinstance(raw_message, str):
            message = raw_message.strip() or None
        raw_details = error.get("additionalDetails") or error.get("details")
        if isinstance(raw_details, str):
            details = raw_details.strip() or None
    elif isinstance(error, str):
        message = error.strip() or None
    if message is None:
        fallback = payload.get("message")
        if isinstance(fallback, str):
            message = fallback.strip() or None
    if details and details != message:
        if message:
            return f"{message} ({details})"
        return details
    return message


def extract_notification_item_id(params: Any, decoded: Any = None) -> Optional[str]:
    item_id = getattr(decoded, "item_id", None)
    if isinstance(item_id, str):
        return item_id
    if not isinstance(params, dict):
        return None
    item_id = params.get("itemId")
    if isinstance(item_id, str):
        return item_id
    item = params.get("item")
    if isinstance(item, dict):
        nested_item_id = item.get("id")
        if isinstance(nested_item_id, str):
            return nested_item_id
    return None


def append_agent_message(messages: list[str], candidate: Optional[str]) -> None:
    if not candidate:
        return
    if messages and messages[-1] == candidate:
        return
    messages.append(candidate)


def append_agent_message_for_phase(
    state: TurnState,
    candidate: Optional[str],
    *,
    phase: Optional[str],
) -> None:
    if not candidate:
        return
    append_agent_message(state.agent_messages, candidate)
    if phase == "commentary":
        append_agent_message(state.commentary_messages, candidate)
    elif phase == "final_answer":
        append_agent_message(state.final_answer_messages, candidate)


def agent_message_deltas_as_list(agent_message_deltas: dict[str, str]) -> list[str]:
    return [
        text for text in agent_message_deltas.values() if isinstance(text, str) and text
    ]


def prune_unambiguous_stale_delta(
    agent_message_deltas: dict[str, str], *, completed_text: str
) -> Optional[str]:
    cleaned_completed = completed_text.strip()
    if not cleaned_completed:
        return None
    matching_keys = [
        item_id
        for item_id, delta_text in agent_message_deltas.items()
        if isinstance(delta_text, str)
        and delta_text.strip()
        and cleaned_completed.startswith(delta_text.strip())
    ]
    if len(matching_keys) == 1:
        matched_item_id = matching_keys[0]
        agent_message_deltas.pop(matched_item_id, None)
        return matched_item_id
    return None


def discard_completed_active_item(
    active_item_ids: set[str], *, matched_item_id: Optional[str]
) -> None:
    if matched_item_id is not None:
        active_item_ids.discard(matched_item_id)
        return
    if len(active_item_ids) == 1:
        active_item_ids.clear()


def agent_messages_for_result(state: TurnState) -> list[str]:
    messages = list(state.agent_messages)
    pending_deltas = agent_message_deltas_as_list(state.agent_message_deltas)
    if not messages:
        return pending_deltas
    for text in pending_deltas:
        if not isinstance(text, str):
            continue
        candidate = text.strip()
        if not candidate:
            continue
        last = messages[-1].strip() if isinstance(messages[-1], str) else ""
        if last == candidate:
            continue
        messages.append(candidate)
    return messages


def final_message_for_result(state: TurnState, *, policy: str) -> str:
    final_answers = [
        msg.strip()
        for msg in state.final_answer_messages
        if isinstance(msg, str) and msg.strip()
    ]
    if final_answers:
        if policy == "all_agent_messages":
            return "\n\n".join(
                msg.strip()
                for msg in agent_messages_for_result(state)
                if isinstance(msg, str) and msg.strip()
            )
        return final_answers[-1]
    messages = agent_messages_for_result(state)
    cleaned = [msg.strip() for msg in messages if isinstance(msg, str) and msg.strip()]
    if not cleaned:
        return ""
    if policy == "all_agent_messages":
        return "\n\n".join(cleaned)
    return cleaned[-1]


def extract_status_value(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("type", "status", "state"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate
    return None


def status_is_terminal(status: Any) -> bool:
    normalized = extract_status_value(status)
    if not isinstance(normalized, str):
        return False
    return normalized.lower() in {
        "completed",
        "complete",
        "done",
        "failed",
        "error",
        "errored",
        "cancelled",
        "canceled",
        "interrupted",
        "stopped",
        "success",
        "succeeded",
    }


def status_prefers_completion_settle(status: Any) -> bool:
    normalized = extract_status_value(status)
    if not isinstance(normalized, str):
        return False
    return normalized.lower() in {
        "completed",
        "complete",
        "done",
        "success",
        "succeeded",
    }


__all__ = [
    "TurnKey",
    "TurnResult",
    "TurnState",
    "TurnStateManager",
    "extract_error_message",
    "extract_notification_item_id",
    "status_is_terminal",
    "status_prefers_completion_settle",
    "turn_key",
]
