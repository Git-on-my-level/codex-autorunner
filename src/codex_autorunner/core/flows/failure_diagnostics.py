from __future__ import annotations

from typing import Any, Optional

from .models import FailureReasonCode, FlowEventType, FlowRunRecord
from .store import FlowStore, now_iso

_MAX_STDERR_LINES = 5
_MAX_STDERR_CHARS = 320
_MAX_SUMMARY_CHARS = 160


def _coerce_str(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _coerce_int(value: Any) -> Optional[int]:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _extract_command(
    item: Optional[dict[str, Any]], params: Optional[dict[str, Any]]
) -> str:
    command = None
    if isinstance(item, dict):
        command = item.get("command")
    if command is None and isinstance(params, dict):
        command = params.get("command")
    if isinstance(command, list):
        return " ".join(str(part) for part in command).strip()
    if isinstance(command, str):
        return command.strip()
    return ""


def _extract_exit_code(
    item: Optional[dict[str, Any]], params: Optional[dict[str, Any]]
) -> Optional[int]:
    for source in (item, params):
        if not isinstance(source, dict):
            continue
        for key in ("exitCode", "exit_code", "exit", "code"):
            value = source.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.strip().isdigit():
                return int(value.strip())
    return None


def _extract_error_message(params: Optional[dict[str, Any]]) -> str:
    if not isinstance(params, dict):
        return ""
    err = params.get("error")
    if isinstance(err, dict):
        message = err.get("message") if isinstance(err.get("message"), str) else ""
        details = ""
        if isinstance(err.get("additionalDetails"), str):
            details = err["additionalDetails"]
        elif isinstance(err.get("details"), str):
            details = err["details"]
        if message and details and message != details:
            return f"{message} ({details})"
        return message or details
    if isinstance(err, str):
        return err
    message = params.get("message")
    if isinstance(message, str):
        return message
    return ""


def _extract_stderr(
    item: Optional[dict[str, Any]], params: Optional[dict[str, Any]]
) -> Optional[str]:
    for source in (item, params):
        if not isinstance(source, dict):
            continue
        for key in ("stderr", "stdErr", "stderr_tail", "stderrTail"):
            value = source.get(key)
            if isinstance(value, list):
                joined = "\n".join(str(part) for part in value if str(part).strip())
                if joined.strip():
                    return joined.strip()
            if isinstance(value, str) and value.strip():
                return value.strip()
    error_message = _extract_error_message(params)
    if error_message:
        return error_message
    return None


def _tail_text(text: Optional[str]) -> Optional[str]:
    if not isinstance(text, str) or not text.strip():
        return None
    lines = [line for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return None
    tail = lines[-_MAX_STDERR_LINES:]
    joined = "\n".join(tail)
    if len(joined) <= _MAX_STDERR_CHARS:
        return joined
    return joined[-_MAX_STDERR_CHARS:].lstrip()


def _extract_command_context(
    store: Optional[FlowStore], run_id: str, *, limit: int = 200
) -> tuple[Optional[str], Optional[int], Optional[str]]:
    if store is None:
        return None, None, None
    try:
        last_seq = store.get_last_event_seq_by_types(
            run_id, [FlowEventType.APP_SERVER_EVENT]
        )
        after_seq = None
        if limit > 0 and isinstance(last_seq, int):
            after_seq = max(0, last_seq - limit)
        events = store.get_events_by_type(
            run_id,
            FlowEventType.APP_SERVER_EVENT,
            after_seq=after_seq,
            limit=limit,
        )
    except Exception:
        return None, None, None
    if not events:
        return None, None, None

    last_command: Optional[str] = None
    exit_code: Optional[int] = None
    stderr_tail: Optional[str] = None

    for event in reversed(events):
        data = event.data or {}
        message = data.get("message")
        if not isinstance(message, dict):
            continue
        method = message.get("method")
        raw_params = message.get("params")
        params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
        raw_item = params.get("item")
        item = raw_item if isinstance(raw_item, dict) else None

        if stderr_tail is None:
            stderr_tail = _tail_text(_extract_stderr(item, params))

        if last_command is None and method == "item/commandExecution/requestApproval":
            cmd = _extract_command(item, params)
            if cmd:
                last_command = cmd

        if last_command is None and method == "item/completed" and item:
            item_type = item.get("type")
            if item_type == "commandExecution":
                cmd = _extract_command(item, params)
                if cmd:
                    last_command = cmd
                if exit_code is None:
                    exit_code = _extract_exit_code(item, params)

        if last_command and (exit_code is not None or stderr_tail is not None):
            break

    return last_command, exit_code, stderr_tail


def _extract_ticket_id(state: Any) -> Optional[str]:
    if not isinstance(state, dict):
        return None
    engine = state.get("ticket_engine") if isinstance(state, dict) else {}
    if not isinstance(engine, dict):
        return None
    ticket_id = engine.get("current_ticket")
    if isinstance(ticket_id, str) and ticket_id.strip():
        return ticket_id.strip()
    return None


def _is_network_error(error_message: str) -> bool:
    if not error_message:
        return False
    error_lower = error_message.lower()
    return any(
        phrase in error_lower
        for phrase in (
            "connection error",
            "connection failed",
            "connection reset",
            "network error",
            "network issue",
            "timeout",
            "timed out",
            "temporarily unavailable",
            "service unavailable",
            "rate limit",
            "rate-limited",
            "429",
        )
    )


def _is_oom_error(error_message: str) -> bool:
    if not error_message:
        return False
    error_lower = error_message.lower()
    return any(
        phrase in error_lower
        for phrase in (
            "oom",
            "out of memory",
            "memory allocation failed",
            "cannot allocate memory",
            "killed",
            "signal 9",
            "sigkill",
        )
    )


def _is_preflight_error(error_message: str) -> bool:
    if not error_message:
        return False
    error_lower = error_message.lower()
    return any(
        phrase in error_lower
        for phrase in (
            "preflight",
            "bootstrap failed",
            "initialization failed",
            "setup failed",
            "config error",
            "invalid config",
        )
    )


def _is_repo_not_found(error_message: str) -> bool:
    if not error_message:
        return False
    error_lower = error_message.lower()
    return any(
        phrase in error_lower
        for phrase in (
            "repo not found",
            "repository not found",
            "no such repo",
            "could not resolve",
        )
    )


def _derive_failure_reason_code(
    *,
    state: Any,
    error_message: Optional[str],
    note: Optional[str],
    exit_code: Optional[int] = None,
) -> FailureReasonCode:
    if isinstance(state, dict):
        engine = state.get("ticket_engine")
        if isinstance(engine, dict):
            value = engine.get("reason_code")
            if isinstance(value, str) and value.strip():
                reason_lower = value.strip().lower()
                if reason_lower == "user_stop":
                    return FailureReasonCode.USER_STOP
                if reason_lower == "timeout":
                    return FailureReasonCode.TIMEOUT
                if reason_lower == "network":
                    return FailureReasonCode.NETWORK_ERROR
    if isinstance(note, str):
        note_lower = note.strip().lower()
        if "worker-dead" in note_lower or "worker_dead" in note_lower:
            return FailureReasonCode.WORKER_DEAD
        if "agent" in note_lower and "crash" in note_lower:
            return FailureReasonCode.AGENT_CRASH
    msg = (error_message or "").lower()
    if _is_oom_error(msg) or (isinstance(exit_code, int) and exit_code in (137, 139)):
        return FailureReasonCode.OOM_KILLED
    if _is_preflight_error(msg):
        return FailureReasonCode.PREFLIGHT_ERROR
    if _is_repo_not_found(msg):
        return FailureReasonCode.REPO_NOT_FOUND
    if "timeout" in msg or "timed out" in msg:
        return FailureReasonCode.TIMEOUT
    if _is_network_error(msg):
        return FailureReasonCode.NETWORK_ERROR
    if "worker died" in msg or "crash" in msg:
        return FailureReasonCode.AGENT_CRASH
    if msg:
        return FailureReasonCode.UNCAUGHT_EXCEPTION
    return FailureReasonCode.UNKNOWN


def _derive_failure_class(
    *, state: Any, error_message: Optional[str], note: Optional[str]
) -> Optional[str]:
    engine_reason = None
    if isinstance(state, dict):
        engine = state.get("ticket_engine")
        if isinstance(engine, dict):
            value = engine.get("reason_code")
            if isinstance(value, str) and value.strip():
                engine_reason = value.strip()
    if engine_reason:
        return engine_reason
    if isinstance(note, str) and note.strip():
        return note.strip()
    msg = (error_message or "").lower()
    if "worker died" in msg:
        return "worker_dead"
    if "timeout" in msg or "timed out" in msg:
        return "timeout"
    if "network" in msg or "connection" in msg:
        return "network"
    if msg:
        return "error"
    return None


def build_failure_payload(
    record: FlowRunRecord,
    *,
    step_id: Optional[str] = None,
    error_message: Optional[str] = None,
    store: Optional[FlowStore] = None,
    note: Optional[str] = None,
    failed_at: Optional[str] = None,
) -> dict[str, Any]:
    state = record.state if isinstance(record.state, dict) else {}
    ticket_id = _extract_ticket_id(state)
    step = step_id or record.current_step
    last_command, exit_code, stderr_tail = _extract_command_context(store, record.id)
    err_text = _coerce_str(error_message) or _coerce_str(record.error_message)
    if stderr_tail is None:
        stderr_tail = _tail_text(err_text)
    failure_class = _derive_failure_class(
        state=state, error_message=err_text, note=note
    )
    failure_reason_code = _derive_failure_reason_code(
        state=state,
        error_message=err_text,
        note=note,
        exit_code=exit_code,
    )
    retryable = _is_network_error(err_text or "")
    if not retryable and isinstance(failure_class, str):
        retryable = failure_class in {"network", "timeout"}
    last_event_seq = None
    last_event_at = None
    if store is not None:
        try:
            last_event_seq, last_event_at = store.get_last_event_meta(record.id)
        except Exception:
            pass
    payload = {
        "failed_at": failed_at or now_iso(),
        "ticket_id": ticket_id,
        "step": step,
        "last_step": step,
        "last_command": last_command,
        "exit_code": exit_code,
        "stderr_tail": stderr_tail,
        "retryable": retryable,
        "failure_class": failure_class,
        "failure_reason_code": failure_reason_code.value,
        "last_event_seq": last_event_seq,
        "last_event_at": last_event_at,
    }
    return payload


def get_failure_payload(record: FlowRunRecord) -> Optional[dict[str, Any]]:
    state = record.state if isinstance(record.state, dict) else {}
    failure = state.get("failure") if isinstance(state, dict) else None
    if isinstance(failure, dict) and failure:
        return failure
    return None


def ensure_failure_payload(
    state: dict[str, Any],
    *,
    record: FlowRunRecord,
    step_id: Optional[str],
    error_message: Optional[str],
    store: Optional[FlowStore],
    note: Optional[str] = None,
    failed_at: Optional[str] = None,
) -> dict[str, Any]:
    existing = state.get("failure") if isinstance(state, dict) else None
    if isinstance(existing, dict) and existing.get("failed_at"):
        return state
    payload = build_failure_payload(
        record,
        step_id=step_id,
        error_message=error_message,
        store=store,
        note=note,
        failed_at=failed_at,
    )
    updated = dict(state)
    updated["failure"] = payload
    return updated


def format_failure_summary(
    payload: dict[str, Any], *, max_len: int = _MAX_SUMMARY_CHARS
) -> Optional[str]:
    if not isinstance(payload, dict) or not payload:
        return None
    parts: list[str] = []
    failure_class = _coerce_str(payload.get("failure_class"))
    if failure_class:
        parts.append(failure_class)
    ticket_id = _coerce_str(payload.get("ticket_id"))
    if ticket_id:
        parts.append(f"ticket {ticket_id}")
    step = _coerce_str(payload.get("step"))
    if step:
        parts.append(f"step {step}")
    last_command = _coerce_str(payload.get("last_command"))
    if last_command:
        parts.append(f"cmd: {last_command}")
    exit_code = _coerce_int(payload.get("exit_code"))
    if exit_code is not None:
        parts.append(f"exit {exit_code}")
    stderr_tail = _coerce_str(payload.get("stderr_tail"))
    if stderr_tail:
        parts.append(f"stderr: {stderr_tail}")
    retryable = payload.get("retryable")
    if isinstance(retryable, bool) and retryable:
        parts.append("retryable")
    if not parts:
        return None
    summary = " · ".join(parts)
    if len(summary) <= max_len:
        return summary
    return summary[: max_len - 1].rstrip() + "…"
