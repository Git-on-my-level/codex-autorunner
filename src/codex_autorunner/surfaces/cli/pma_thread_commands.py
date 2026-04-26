"""PMA thread command implementations.

Managed-thread CLI commands (spawn, list, info, status, send, turns, output,
tail, compact, resume, fork, archive, interrupt) and supporting helpers and
dataclasses.  Registered on the ``thread_app`` typer via
:func:`register_thread_commands`.
"""

import json
import logging
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import httpx
import typer

from ...core.car_context import (
    default_managed_thread_context_profile,
    normalize_car_context_profile,
)
from ...core.config import load_hub_config
from ...core.config_contract import ConfigError
from ...core.pma_audit import PmaActionType, PmaAuditEntry, PmaAuditLog
from .commands.utils import format_hub_request_error
from .hub_path_option import hub_root_path_option
from .pma_control_plane import (
    CAPABILITY_REQUIREMENTS as _CAPABILITY_REQUIREMENTS,
)
from .pma_control_plane import (
    MANAGED_THREAD_SEND_REQUEST_TIMEOUT_SECONDS as _MANAGED_THREAD_SEND_REQUEST_TIMEOUT_SECONDS,
)
from .pma_control_plane import (
    ManagedThreadSendRequest as _ManagedThreadSendRequest,
)
from .pma_control_plane import (
    ManagedThreadSendResponse as _ManagedThreadSendResponse,
)
from .pma_control_plane import (
    ManagedThreadSendTimeoutProbe as _ManagedThreadSendTimeoutProbe,
)
from .pma_control_plane import (
    auth_headers_from_env as _auth_headers_from_env,
)
from .pma_control_plane import (
    build_pma_url as _build_pma_url,
)
from .pma_control_plane import (
    capture_managed_thread_send_timeout_probe as _capture_managed_thread_send_timeout_probe,
)
from .pma_control_plane import (
    check_capability as _check_capability,
)
from .pma_control_plane import (
    coerce_optional_int as _coerce_optional_int,
)
from .pma_control_plane import (
    fetch_agent_capabilities as _fetch_agent_capabilities,
)
from .pma_control_plane import (
    format_resource_owner_label as _format_resource_owner_label,
)
from .pma_control_plane import (
    normalize_agent_option as _normalize_agent_option,
)
from .pma_control_plane import (
    normalize_notify_on as _normalize_notify_on,
)
from .pma_control_plane import (
    normalize_resource_owner_options as _normalize_resource_owner_options,
)
from .pma_control_plane import (
    recover_managed_thread_send_timeout as _recover_managed_thread_send_timeout,
)
from .pma_control_plane import (
    request_json as _request_json,
)
from .pma_control_plane import (
    request_json_with_status as _request_json_with_status,
)

logger = logging.getLogger(__name__)

_THREAD_OUTPUT_CURSOR_RELATIVE_PATH = (
    Path(".codex-autorunner") / "pma" / "thread_output_cursors.json"
)
_DEFAULT_OUTPUT_PAGE_LINES = 20


class _PmaVerbosityLevel(str, Enum):
    INFO = "info"
    DEBUG = "debug"


@dataclass(frozen=True)
class _AssistantTextWindow:
    text: str
    start_line: int
    end_line: int
    total_lines: int
    has_more: bool
    next_line: Optional[int]
    page_lines: int
    auto_paginated: bool


def _resolve_hub_path(path: Optional[Path]) -> Path:
    start = path or Path.cwd()
    try:
        return load_hub_config(start).root
    except (
        OSError,
        ValueError,
        ConfigError,
        AttributeError,
    ):
        candidate = start.resolve()
        if candidate.is_file():
            parent = candidate.parent
            if parent.name == ".codex-autorunner":
                return parent.parent.resolve()
            return parent.resolve()
        return candidate


def _resolve_message_body(
    *,
    message: Optional[str],
    message_file: Optional[Path],
    message_stdin: bool,
    option_hint: str,
) -> str:
    selected_inputs = sum(
        1
        for selected in (
            message is not None,
            message_file is not None,
            message_stdin,
        )
        if selected
    )
    if selected_inputs != 1:
        raise typer.BadParameter(
            f"Provide exactly one of {option_hint}.",
            param_hint="--message / --message-file / --message-stdin",
        )

    if message_file is not None:
        try:
            raw_message = message_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise typer.BadParameter(
                f"Failed to read message file: {exc}",
                param_hint="--message-file",
            ) from exc
    elif message_stdin:
        raw_message = sys.stdin.read()
    else:
        raw_message = message or ""

    if not raw_message.strip():
        raise typer.BadParameter("Message cannot be empty.")
    return raw_message


def _parse_thread_id_list(raw: str) -> list[str]:
    thread_ids: list[str] = []
    seen: set[str] = set()
    for line in raw.replace(",", "\n").splitlines():
        for token in line.split():
            thread_id = token.strip()
            if not thread_id or thread_id in seen:
                continue
            thread_ids.append(thread_id)
            seen.add(thread_id)
    return thread_ids


def _resolve_archive_thread_ids(
    *,
    managed_thread_id: Optional[str],
    managed_thread_ids: Optional[str],
    managed_thread_ids_stdin: bool,
) -> list[str]:
    selected_inputs = sum(
        1
        for selected in (
            managed_thread_id is not None,
            managed_thread_ids is not None,
            managed_thread_ids_stdin,
        )
        if selected
    )
    if selected_inputs != 1:
        raise typer.BadParameter(
            "Provide exactly one of --id, --ids, or --ids-stdin.",
            param_hint="--id / --ids / --ids-stdin",
        )

    if managed_thread_id is not None:
        single_id = managed_thread_id.strip()
        resolved_ids = [single_id] if single_id else []
    elif managed_thread_ids is not None:
        resolved_ids = _parse_thread_id_list(managed_thread_ids)
    else:
        resolved_ids = _parse_thread_id_list(sys.stdin.read())

    if not resolved_ids:
        raise typer.BadParameter(
            "Provide at least one managed thread id.",
            param_hint="--id / --ids / --ids-stdin",
        )
    return resolved_ids


def _echo_delivered_message(message: str) -> None:
    typer.echo("delivered message:")
    typer.echo(message, nl=False)
    if not message.endswith("\n"):
        typer.echo()


def _thread_output_cursor_path(hub_root: Path) -> Path:
    return hub_root / _THREAD_OUTPUT_CURSOR_RELATIVE_PATH


def _load_thread_output_cursors(hub_root: Path) -> dict[str, Any]:
    path = _thread_output_cursor_path(hub_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_thread_output_cursors(hub_root: Path, payload: dict[str, Any]) -> None:
    path = _thread_output_cursor_path(hub_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        logger.debug("Failed to persist thread output cursors to %s: %s", path, exc)


def _clear_thread_output_cursor(
    hub_root: Path, *, command_name: str, managed_thread_id: str
) -> None:
    payload = _load_thread_output_cursors(hub_root)
    cursor_key = f"{command_name}:{managed_thread_id}"
    if cursor_key not in payload:
        return
    payload.pop(cursor_key, None)
    if payload:
        _save_thread_output_cursors(hub_root, payload)
        return
    path = _thread_output_cursor_path(hub_root)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        logger.debug("Failed to delete empty thread output cursor file: %s", path)


def _auto_output_page_lines() -> int:
    try:
        lines = shutil.get_terminal_size(fallback=(80, 24)).lines
    except OSError:
        lines = 24
    return max(_DEFAULT_OUTPUT_PAGE_LINES, lines - 4)


def _parse_output_lines_spec(value: str) -> tuple[Optional[int], Optional[int]]:
    raw = str(value or "").strip()
    if not raw:
        raise typer.BadParameter("lines cannot be empty", param_hint="--lines")
    if ":" not in raw:
        end_line = _coerce_optional_int(raw)
        if end_line is None or end_line <= 0:
            raise typer.BadParameter(
                "lines must be a positive integer or START:END",
                param_hint="--lines",
            )
        return 1, end_line

    start_text, end_text = raw.split(":", 1)
    start_line = _coerce_optional_int(start_text) if start_text.strip() else None
    end_line = _coerce_optional_int(end_text) if end_text.strip() else None
    if start_line is not None and start_line <= 0:
        raise typer.BadParameter(
            "line ranges are 1-based and must be positive",
            param_hint="--lines",
        )
    if end_line is not None and end_line <= 0:
        raise typer.BadParameter(
            "line ranges are 1-based and must be positive",
            param_hint="--lines",
        )
    if start_line is None and end_line is None:
        raise typer.BadParameter(
            "Provide at least one bound for --lines (for example 1:50 or 100:)",
            param_hint="--lines",
        )
    if start_line is not None and end_line is not None and end_line < start_line:
        raise typer.BadParameter(
            "The end of --lines must be greater than or equal to the start",
            param_hint="--lines",
        )
    return start_line, end_line


def _assistant_text_lines(text: str) -> list[str]:
    if not text:
        return []
    return text.splitlines()


def _thread_payload(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    thread = data.get("thread")
    return thread if isinstance(thread, dict) else {}


def _normalized_thread_runtime_status(data: Any) -> str:
    thread = _thread_payload(data)
    return str(
        thread.get("normalized_status")
        or thread.get("status")
        or data.get("status")
        or ""
    ).strip()


def _normalized_thread_lifecycle_status(data: Any) -> str:
    thread = _thread_payload(data)
    return str(thread.get("lifecycle_status") or thread.get("status") or "").strip()


def _resolve_assistant_text_window(
    *,
    assistant_text: str,
    lines_spec: Optional[str],
    continue_output: bool,
    command_name: str,
    hub_root: Path,
    managed_thread_id: str,
    managed_turn_id: str,
) -> _AssistantTextWindow:
    if continue_output and lines_spec:
        raise typer.BadParameter(
            "Choose either --lines or --continue, not both.",
            param_hint="--lines / --continue",
        )

    all_lines = _assistant_text_lines(assistant_text)
    total_lines = len(all_lines)
    if total_lines == 0:
        _clear_thread_output_cursor(
            hub_root, command_name=command_name, managed_thread_id=managed_thread_id
        )
        return _AssistantTextWindow(
            text="",
            start_line=0,
            end_line=0,
            total_lines=0,
            has_more=False,
            next_line=None,
            page_lines=0,
            auto_paginated=False,
        )

    start_line: Optional[int] = None
    end_line: Optional[int] = None
    auto_paginated = False
    page_lines = 0
    cursor_key = f"{command_name}:{managed_thread_id}"
    if continue_output:
        payload = _load_thread_output_cursors(hub_root)
        saved_cursor = payload.get(cursor_key)
        if not isinstance(saved_cursor, dict):
            raise typer.BadParameter(
                "No saved assistant_text cursor for this thread. Start without --continue first.",
                param_hint="--continue",
            )
        saved_turn_id = str(saved_cursor.get("managed_turn_id") or "").strip()
        if saved_turn_id and saved_turn_id != managed_turn_id:
            raise typer.BadParameter(
                "The saved assistant_text cursor points at a different turn. "
                "Re-run without --continue or pass --turn to read the earlier turn.",
                param_hint="--continue",
            )
        start_line = _coerce_optional_int(saved_cursor.get("next_line"))
        page_lines = _coerce_optional_int(saved_cursor.get("page_lines")) or 0
        if start_line is None or start_line <= 0:
            raise typer.BadParameter(
                "The saved assistant_text cursor is invalid. Re-run without --continue.",
                param_hint="--continue",
            )
        if page_lines <= 0:
            page_lines = _auto_output_page_lines()
        end_line = min(total_lines, start_line + page_lines - 1)
        auto_paginated = True
    elif lines_spec:
        start_line, end_line = _parse_output_lines_spec(lines_spec)
        if start_line is None:
            start_line = 1
        if end_line is None:
            end_line = total_lines
        if start_line > total_lines:
            raise typer.BadParameter(
                f"assistant_text only has {total_lines} line(s); requested start line {start_line}.",
                param_hint="--lines",
            )
        end_line = min(end_line, total_lines)
    else:
        page_lines = _auto_output_page_lines()
        if total_lines > page_lines:
            start_line = 1
            end_line = min(total_lines, page_lines)
            auto_paginated = True
        else:
            start_line = 1
            end_line = total_lines

    selected_lines = all_lines[start_line - 1 : end_line]
    rendered_text = "\n".join(selected_lines)
    has_more = end_line < total_lines
    next_line = end_line + 1 if has_more else None

    if auto_paginated and has_more:
        payload = _load_thread_output_cursors(hub_root)
        payload[cursor_key] = {
            "managed_turn_id": managed_turn_id,
            "next_line": next_line,
            "page_lines": page_lines,
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }
        _save_thread_output_cursors(hub_root, payload)
    else:
        _clear_thread_output_cursor(
            hub_root, command_name=command_name, managed_thread_id=managed_thread_id
        )

    return _AssistantTextWindow(
        text=rendered_text,
        start_line=start_line,
        end_line=end_line,
        total_lines=total_lines,
        has_more=has_more,
        next_line=next_line,
        page_lines=page_lines or max(1, end_line - start_line + 1),
        auto_paginated=auto_paginated,
    )


def _render_assistant_text_window(
    *,
    window: _AssistantTextWindow,
    output_file: Optional[Path],
    label: str = "assistant_text",
) -> None:
    if output_file is not None:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(window.text, encoding="utf-8")
        if window.total_lines > 0:
            typer.echo(
                f"Wrote {label} lines {window.start_line}:{window.end_line} to {output_file}"
            )
        else:
            typer.echo(f"Wrote empty {label} to {output_file}")
        return

    if window.total_lines > 0:
        typer.echo(
            f"{label} lines {window.start_line}:{window.end_line} of {window.total_lines}:"
        )
    else:
        typer.echo(f"{label}:")
    if window.text:
        typer.echo(window.text, nl=False)
        if not window.text.endswith("\n"):
            typer.echo()
    else:
        typer.echo("(empty)")

    if window.has_more and window.next_line is not None:
        next_end_line = min(
            window.total_lines,
            window.next_line + max(window.page_lines, 1) - 1,
        )
        typer.echo(
            "note: "
            f"{label} paginated lines {window.start_line}:{window.end_line} "
            f"of {window.total_lines}. Re-run with --continue or "
            f"--lines {window.next_line}:{next_end_line}."
        )


def _format_archived_thread_line(thread: dict[str, Any]) -> str:
    managed_thread_id = str(thread.get("managed_thread_id") or "").strip()
    name = str(thread.get("name") or "").strip()
    if managed_thread_id and name:
        return f"Archived {managed_thread_id} ({name})"
    if managed_thread_id:
        return f"Archived {managed_thread_id}"
    if name:
        return f"Archived managed thread ({name})"
    return "Archived managed thread"


def _iter_sse_events(lines):
    event_name = "message"
    data_lines: list[str] = []
    event_id: Optional[str] = None
    for line in lines:
        if line is None:
            continue
        if line == "":
            if data_lines or event_id is not None:
                data = "\n".join(data_lines)
                yield event_name, data, event_id
            event_name = "message"
            data_lines = []
            event_id = None
            continue
        if line.startswith(":"):
            continue
        if ":" in line:
            field, value = line.split(":", 1)
            if value.startswith(" "):
                value = value[1:]
        else:
            field, value = line, ""
        if field == "event":
            event_name = value or "message"
        elif field == "data":
            data_lines.append(value)
        elif field == "id":
            event_id = value


def _format_seconds(seconds: Optional[int]) -> str:
    if seconds is None:
        return "-"
    value = max(0, int(seconds))
    if value < 60:
        return f"{value}s"
    minutes, sec = divmod(value, 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, rem_minutes = divmod(minutes, 60)
    return f"{hours}h{rem_minutes:02d}m"


def _format_tail_event_line(event: dict[str, Any]) -> str:
    parsed_event = _PmaTailEvent.from_dict(event)
    if parsed_event is None:
        return ""
    event_type = parsed_event.event_type
    event_id = parsed_event.event_id
    summary = parsed_event.summary
    timestamp = parsed_event.received_at
    ts_out = timestamp
    if timestamp:
        try:
            dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            ts_out = dt.strftime("%H:%M:%S")
        except ValueError:
            ts_out = timestamp
    prefix = f"[{ts_out}] " if ts_out else ""
    id_part = f"#{event_id} " if isinstance(event_id, int) and event_id > 0 else ""
    return f"{prefix}{id_part}{event_type}: {summary}".rstrip()


def _format_received_at_label(value: Any) -> str:
    timestamp = str(value or "").strip()
    if not timestamp:
        return "-"
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return timestamp
    return dt.strftime("%H:%M:%S")


@dataclass(frozen=True)
class _PmaTailEvent:
    event_type: str
    summary: str
    received_at: str
    event_id: Optional[int] = None

    @classmethod
    def from_dict(cls, data: Any) -> Optional["_PmaTailEvent"]:
        if not isinstance(data, dict):
            return None
        event_id = data.get("event_id")
        normalized_event_id = event_id if isinstance(event_id, int) else None
        return cls(
            event_type=str(data.get("event_type") or "event"),
            summary=str(data.get("summary") or ""),
            received_at=str(data.get("received_at") or ""),
            event_id=normalized_event_id,
        )


@dataclass(frozen=True)
class _PmaLastToolSnapshot:
    name: str
    status: str
    in_flight: bool

    @classmethod
    def from_dict(cls, data: Any) -> Optional["_PmaLastToolSnapshot"]:
        if not isinstance(data, dict):
            return None
        name = str(data.get("name") or "").strip()
        if not name:
            return None
        return cls(
            name=name,
            status=str(data.get("status") or "-"),
            in_flight=bool(data.get("in_flight")),
        )

    def render_line(self) -> str:
        return (
            "last_tool="
            + self.name
            + " status="
            + self.status
            + " in_flight="
            + ("yes" if self.in_flight else "no")
        )


@dataclass(frozen=True)
class _PmaActiveTurnDiagnostics:
    request_kind: str
    model: str
    reasoning: str
    stalled: bool
    stream_available: bool
    prompt_preview: str
    last_event_type: str
    last_event_summary: str
    last_event_at: Any
    backend_thread_id: str
    backend_turn_id: str
    stall_reason: str

    @classmethod
    def from_dict(cls, data: Any) -> Optional["_PmaActiveTurnDiagnostics"]:
        if not isinstance(data, dict):
            return None
        return cls(
            request_kind=str(data.get("request_kind") or "-"),
            model=str(data.get("model") or "-"),
            reasoning=str(data.get("reasoning") or "-"),
            stalled=bool(data.get("stalled")),
            stream_available=bool(data.get("stream_available")),
            prompt_preview=str(data.get("prompt_preview") or "").strip(),
            last_event_type=str(data.get("last_event_type") or "").strip(),
            last_event_summary=str(data.get("last_event_summary") or "").strip(),
            last_event_at=data.get("last_event_at"),
            backend_thread_id=str(data.get("backend_thread_id") or "").strip(),
            backend_turn_id=str(data.get("backend_turn_id") or "").strip(),
            stall_reason=str(data.get("stall_reason") or "").strip(),
        )

    def render_lines(self) -> list[str]:
        lines = [
            "active_turn: "
            f"kind={self.request_kind} model={self.model} reasoning={self.reasoning} "
            f"stream={'yes' if self.stream_available else 'no'} "
            f"stalled={'yes' if self.stalled else 'no'}"
        ]
        if self.prompt_preview:
            lines.append(f"prompt: {self.prompt_preview}")
        if self.last_event_type or self.last_event_summary:
            lines.append(
                "last_event: "
                + (self.last_event_type or "-")
                + " @"
                + _format_received_at_label(self.last_event_at)
                + (f" {self.last_event_summary}" if self.last_event_summary else "")
            )
        if self.backend_thread_id or self.backend_turn_id:
            lines.append(
                "backend: "
                f"thread={self.backend_thread_id or '-'} "
                f"turn={self.backend_turn_id or '-'}"
            )
        if self.stall_reason:
            lines.append(f"stall_reason: {self.stall_reason}")
        return lines


@dataclass(frozen=True)
class _PmaTailSnapshot:
    managed_turn_id: str
    turn_status: str
    activity: str
    phase: str
    elapsed_seconds: Optional[int]
    idle_seconds: Optional[int]
    guidance: str
    diagnostics: Optional[_PmaActiveTurnDiagnostics]
    last_tool: Optional[_PmaLastToolSnapshot]
    lifecycle_events: tuple[str, ...]
    events: tuple[_PmaTailEvent, ...]

    @classmethod
    def from_dict(cls, data: Any) -> "_PmaTailSnapshot":
        payload = data if isinstance(data, dict) else {}
        lifecycle = payload.get("lifecycle_events")
        raw_events = payload.get("events")
        return cls(
            managed_turn_id=str(payload.get("managed_turn_id") or "-"),
            turn_status=str(payload.get("turn_status") or "none"),
            activity=str(payload.get("activity") or "idle"),
            phase=str(payload.get("phase") or "-"),
            elapsed_seconds=_coerce_optional_int(payload.get("elapsed_seconds")),
            idle_seconds=_coerce_optional_int(payload.get("idle_seconds")),
            guidance=str(payload.get("guidance") or "").strip(),
            diagnostics=_PmaActiveTurnDiagnostics.from_dict(
                payload.get("active_turn_diagnostics")
            ),
            last_tool=_PmaLastToolSnapshot.from_dict(payload.get("last_tool")),
            lifecycle_events=tuple(
                str(item) for item in (lifecycle if isinstance(lifecycle, list) else [])
            ),
            events=tuple(
                event
                for item in (raw_events if isinstance(raw_events, list) else [])
                if (event := _PmaTailEvent.from_dict(item)) is not None
            ),
        )

    def render_lines(self) -> list[str]:
        lines = [
            "managed_turn_id="
            + self.managed_turn_id
            + " turn_status="
            + self.turn_status
            + " activity="
            + self.activity
            + " phase="
            + self.phase
            + " elapsed="
            + _format_seconds(self.elapsed_seconds)
            + " idle="
            + _format_seconds(self.idle_seconds)
        ]
        if self.guidance:
            lines.append(f"guidance: {self.guidance}")
        if self.diagnostics is not None:
            lines.extend(self.diagnostics.render_lines())
        if self.last_tool is not None:
            lines.append(self.last_tool.render_line())
        if self.lifecycle_events:
            lines.append("lifecycle: " + ", ".join(self.lifecycle_events))
        if not self.events:
            lines.append("No tail events.")
            if self.turn_status == "running" and self.idle_seconds is not None:
                idle_seconds = int(self.idle_seconds or 0)
                if idle_seconds >= 30:
                    lines.append(f"No events for {idle_seconds}s (possibly stalled).")
            return lines
        lines.extend(_format_tail_event_line(event.__dict__) for event in self.events)
        return [line for line in lines if line]


@dataclass(frozen=True)
class _PmaQueuedTurnSnapshot:
    managed_turn_id: str
    request_kind: str
    state: str
    position: Optional[int]
    enqueued_at: str
    prompt_preview: str

    @classmethod
    def from_dict(cls, data: Any) -> Optional["_PmaQueuedTurnSnapshot"]:
        if not isinstance(data, dict):
            return None
        return cls(
            managed_turn_id=str(data.get("managed_turn_id") or "-"),
            request_kind=str(data.get("request_kind") or "-"),
            state=str(data.get("state") or "-"),
            position=_coerce_optional_int(data.get("position")),
            enqueued_at=str(data.get("enqueued_at") or "-"),
            prompt_preview=str(data.get("prompt_preview") or "")[:80],
        )

    def render_line(self) -> str:
        position = self.position if self.position is not None else "-"
        return (
            "queued_turn_id="
            + self.managed_turn_id
            + " position="
            + str(position)
            + " state="
            + self.state
            + " kind="
            + self.request_kind
            + " enqueued="
            + self.enqueued_at
            + " prompt="
            + self.prompt_preview
        )


@dataclass(frozen=True)
class _PmaThreadStatusSnapshot:
    managed_thread_id: str
    agent: str
    owner_label: str
    operator_status: str
    runtime_status: str
    lifecycle_status: str
    is_alive: bool
    status_reason: str
    managed_turn_id: str
    turn_status: str
    activity: str
    phase: str
    elapsed_seconds: Optional[int]
    idle_seconds: Optional[int]
    guidance: str
    diagnostics: Optional[_PmaActiveTurnDiagnostics]
    last_tool: Optional[_PmaLastToolSnapshot]
    recent_progress: tuple[_PmaTailEvent, ...]
    latest_turn_id: str
    latest_assistant_text: str
    latest_output_excerpt: str
    queue_depth: int
    queued_turns: tuple[_PmaQueuedTurnSnapshot, ...]

    @classmethod
    def from_dict(cls, data: Any) -> "_PmaThreadStatusSnapshot":
        from ...core.managed_thread_status import derive_managed_thread_operator_status

        payload = data if isinstance(data, dict) else {}
        raw_thread = payload.get("thread")
        thread: dict[str, Any] = raw_thread if isinstance(raw_thread, dict) else {}
        raw_turn = payload.get("turn")
        turn: dict[str, Any] = raw_turn if isinstance(raw_turn, dict) else {}
        raw_thread_status = str(
            payload.get("status")
            or thread.get("normalized_status")
            or thread.get("status")
            or "-"
        )
        queue_depth_raw = payload.get("queue_depth")
        recent_progress = payload.get("recent_progress")
        queued_turns = payload.get("queued_turns")
        return cls(
            managed_thread_id=str(payload.get("managed_thread_id") or ""),
            agent=str(thread.get("agent") or "-"),
            owner_label=_format_resource_owner_label(thread),
            operator_status=str(payload.get("operator_status") or "").strip()
            or derive_managed_thread_operator_status(
                normalized_status=raw_thread_status,
                lifecycle_status=str(thread.get("lifecycle_status") or "-"),
            ),
            runtime_status=raw_thread_status,
            lifecycle_status=str(thread.get("lifecycle_status") or "-"),
            is_alive=bool(payload.get("is_alive")),
            status_reason=str(
                payload.get("status_reason")
                or payload.get("status_reason_code")
                or thread.get("status_reason_code")
                or thread.get("status_reason")
                or "-"
            ),
            managed_turn_id=str(turn.get("managed_turn_id") or "-"),
            turn_status=str(turn.get("status") or "-"),
            activity=str(turn.get("activity") or "-"),
            phase=str(turn.get("phase") or "-"),
            elapsed_seconds=_coerce_optional_int(turn.get("elapsed_seconds")),
            idle_seconds=_coerce_optional_int(turn.get("idle_seconds")),
            guidance=str(turn.get("guidance") or "").strip(),
            diagnostics=_PmaActiveTurnDiagnostics.from_dict(
                payload.get("active_turn_diagnostics")
            ),
            last_tool=_PmaLastToolSnapshot.from_dict(turn.get("last_tool")),
            recent_progress=tuple(
                event
                for item in (
                    recent_progress if isinstance(recent_progress, list) else []
                )
                if (event := _PmaTailEvent.from_dict(item)) is not None
            ),
            latest_turn_id=str(payload.get("latest_turn_id") or "").strip(),
            latest_assistant_text=str(payload.get("latest_assistant_text") or ""),
            latest_output_excerpt=str(
                payload.get("latest_output_excerpt") or ""
            ).strip(),
            queue_depth=_coerce_optional_int(queue_depth_raw) or 0,
            queued_turns=tuple(
                turn_item
                for item in (queued_turns if isinstance(queued_turns, list) else [])
                if (turn_item := _PmaQueuedTurnSnapshot.from_dict(item)) is not None
            ),
        )

    def render_lines(self) -> list[str]:
        lines = [
            " ".join(
                [
                    f"id={self.managed_thread_id}",
                    f"agent={self.agent}",
                    self.owner_label,
                    f"operator_status={self.operator_status}",
                    f"runtime_status={self.runtime_status}",
                    f"lifecycle_status={self.lifecycle_status}",
                    f"alive={'yes' if self.is_alive else 'no'}",
                ]
            ),
            f"status_reason={self.status_reason}",
            "managed_turn_id="
            + self.managed_turn_id
            + " turn_status="
            + self.turn_status
            + " activity="
            + self.activity
            + " phase="
            + self.phase
            + " elapsed="
            + _format_seconds(self.elapsed_seconds)
            + " idle="
            + _format_seconds(self.idle_seconds),
        ]
        if self.guidance:
            lines.append(f"guidance: {self.guidance}")
        if self.diagnostics is not None:
            lines.extend(self.diagnostics.render_lines())
        if self.last_tool is not None:
            lines.append(self.last_tool.render_line())
        if self.recent_progress:
            lines.append("recent progress:")
            lines.extend(
                _format_tail_event_line(event.__dict__)
                for event in self.recent_progress
            )
        else:
            lines.append("No recent progress events.")
        if self.queue_depth > 0:
            lines.append(f"queued={self.queue_depth}")
            lines.extend(item.render_line() for item in self.queued_turns[:5])
        if self.latest_output_excerpt:
            lines.append("assistant_text_excerpt:")
            lines.append(self.latest_output_excerpt)
        return [line for line in lines if line]


def _render_active_turn_diagnostics(data: dict[str, Any]) -> None:
    diagnostics = _PmaActiveTurnDiagnostics.from_dict(data)
    if diagnostics is None:
        return
    for line in diagnostics.render_lines():
        typer.echo(line)


def _render_tail_snapshot(snapshot: dict[str, Any]) -> None:
    parsed_snapshot = _PmaTailSnapshot.from_dict(snapshot)
    for line in parsed_snapshot.render_lines():
        typer.echo(line)


def _render_thread_status_snapshot(data: dict[str, Any]) -> None:
    snapshot = _PmaThreadStatusSnapshot.from_dict(data)
    for line in snapshot.render_lines():
        typer.echo(line)


def _resume_noop_message(managed_thread_id: str, thread_data: Any) -> str:
    status = _normalized_thread_runtime_status(thread_data)
    if status and status.lower() != "active":
        return f"Thread {managed_thread_id} is already active (status: {status})"
    return f"Thread {managed_thread_id} is already active"


def _interrupt_conflict_message(managed_thread_id: str, thread_data: Any) -> str:
    status = _normalized_thread_runtime_status(thread_data)
    if status:
        return f"Cannot interrupt: thread is not running (status: {status})"
    lifecycle_status = _normalized_thread_lifecycle_status(thread_data)
    if lifecycle_status:
        return (
            "Cannot interrupt: thread is not running "
            f"(lifecycle: {lifecycle_status})"
        )
    return f"Cannot interrupt: thread {managed_thread_id} is not running"


def _resolve_latest_managed_turn_id(config, *, managed_thread_id: str) -> str:
    turns_data = _request_json(
        "GET",
        _build_pma_url(config, f"/threads/{managed_thread_id}/turns"),
        token_env=config.server_auth_token_env,
        params={"limit": 1},
    )
    turns = turns_data.get("turns", []) if isinstance(turns_data, dict) else []
    if not isinstance(turns, list) or not turns:
        raise ValueError("No turns found")
    latest_turn = turns[0] if isinstance(turns[0], dict) else {}
    latest_turn_id = str(latest_turn.get("managed_turn_id") or "").strip()
    if not latest_turn_id:
        raise ValueError("Failed to resolve latest turn id")
    return latest_turn_id


def _fetch_managed_turn_payload(
    config,
    *,
    managed_thread_id: str,
    managed_turn_id: Optional[str] = None,
) -> tuple[str, dict[str, Any]]:
    resolved_turn_id = str(
        managed_turn_id or ""
    ).strip() or _resolve_latest_managed_turn_id(
        config,
        managed_thread_id=managed_thread_id,
    )
    turn_data = _request_json(
        "GET",
        _build_pma_url(
            config, f"/threads/{managed_thread_id}/turns/{resolved_turn_id}"
        ),
        token_env=config.server_auth_token_env,
    )
    return resolved_turn_id, turn_data if isinstance(turn_data, dict) else {}


def _normalize_thread_compact_scope(
    *,
    managed_thread_id: Optional[str],
    status: Optional[str],
    all_threads: bool,
) -> tuple[Optional[str], bool]:
    normalized_id = str(managed_thread_id or "").strip() or None
    normalized_status = str(status or "").strip().lower() or None
    scope_all = all_threads or normalized_status == "all"
    if normalized_id is not None:
        if normalized_status is not None or all_threads:
            raise typer.BadParameter(
                "Use either --id or bulk selection flags, not both.",
                param_hint="--id",
            )
        return normalized_id, False
    if normalized_status is None and not scope_all:
        raise typer.BadParameter(
            "Provide --id, --status, or --all.",
            param_hint="--id / --status / --all",
        )
    return normalized_status, scope_all


def _thread_compact_target_line(thread: dict[str, Any]) -> str:
    parts = [
        str(thread.get("managed_thread_id") or ""),
        f"agent={thread.get('agent') or ''}",
        f"status={thread.get('normalized_status') or thread.get('status') or ''}",
        f"reason={thread.get('status_reason_code') or thread.get('status_reason') or '-'}",
        _format_resource_owner_label(thread),
    ]
    if bool(thread.get("chat_bound")):
        parts.append("chat_bound=yes")
    if bool(thread.get("cleanup_protected")):
        parts.append("protected=yes")
    return " ".join(part for part in parts if part).strip()


def _record_thread_compact_audit(
    *,
    hub_root: Path,
    summary: str,
    managed_thread_ids: list[str],
    mode: str,
    reset_backend: bool,
    status: Optional[str],
    scope_all: bool,
    resource_kind: Optional[str],
    resource_id: Optional[str],
    agent: Optional[str],
    errors: Optional[list[dict[str, Any]]] = None,
) -> None:
    try:
        PmaAuditLog(hub_root).append(
            PmaAuditEntry(
                action_type=PmaActionType.SESSION_COMPACT,
                thread_id=(
                    managed_thread_ids[0] if len(managed_thread_ids) == 1 else None
                ),
                agent=agent,
                status="error" if errors else "ok",
                error="; ".join(
                    str(item.get("error") or "").strip()
                    for item in (errors or [])
                    if str(item.get("error") or "").strip()
                )
                or None,
                details={
                    "command": "managed_thread_compact",
                    "mode": mode,
                    "thread_count": len(managed_thread_ids),
                    "thread_ids": managed_thread_ids,
                    "summary_length": len(summary),
                    "reset_backend": reset_backend,
                    "status_filter": status,
                    "all_threads": scope_all,
                    "resource_kind": resource_kind,
                    "resource_id": resource_id,
                    "errors": errors or [],
                },
            )
        )
    except (OSError, ValueError) as exc:
        logger.warning("Failed to record PMA thread compact audit entry: %s", exc)


def pma_thread_spawn(
    agent: Optional[str] = typer.Option(
        None, "--agent", help="Thread agent to use (codex|opencode|hermes|zeroclaw)"
    ),
    repo_id: Optional[str] = typer.Option(
        None, "--repo", help="Hub repo id for the target workspace"
    ),
    resource_kind: Optional[str] = typer.Option(
        None, "--resource-kind", help="Managed resource kind (repo|agent_workspace)"
    ),
    resource_id: Optional[str] = typer.Option(
        None, "--resource-id", help="Managed resource id"
    ),
    workspace_root: Optional[str] = typer.Option(
        None, "--workspace-root", help="Absolute or hub-relative workspace path"
    ),
    name: Optional[str] = typer.Option(None, "--name", help="Optional thread label"),
    context_profile: Optional[str] = typer.Option(
        None,
        "--context-profile",
        help="CAR context profile (car_core|car_ambient|none)",
    ),
    notify_on: Optional[str] = typer.Option(
        None,
        "--notify-on",
        help="Auto-subscribe for lifecycle events (supported: terminal)",
    ),
    terminal_followup: Optional[bool] = typer.Option(
        None,
        "--terminal-followup/--no-terminal-followup",
        help="Override the default terminal follow-up subscription for new threads",
    ),
    notify_lane: Optional[str] = typer.Option(
        None, "--notify-lane", help="Lane id used for terminal notifications"
    ),
    notify_once: bool = typer.Option(
        True,
        "--notify-once/--no-notify-once",
        help="Auto-cancel notification after first fire",
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = hub_root_path_option(),
):
    """Create a managed PMA thread."""
    normalized_agent = _normalize_agent_option(agent)
    (
        normalized_resource_kind,
        normalized_resource_id,
        normalized_workspace_root,
    ) = _normalize_resource_owner_options(
        repo_id=repo_id,
        resource_kind=resource_kind,
        resource_id=resource_id,
        workspace_root=workspace_root,
    )
    owner_present = (
        normalized_resource_kind is not None and normalized_resource_id is not None
    )
    if (
        sum(
            1
            for present in (
                owner_present,
                normalized_workspace_root is not None,
            )
            if present
        )
        != 1
    ):
        typer.echo(
            "Exactly one of --repo, --resource-kind/--resource-id, or --workspace-root is required",
            err=True,
        )
        raise typer.Exit(code=1) from None
    if normalized_agent is None and normalized_resource_kind != "agent_workspace":
        typer.echo(
            "--agent is required unless --resource-kind agent_workspace is used",
            err=True,
        )
        raise typer.Exit(code=1) from None
    normalized_context_profile = normalize_car_context_profile(context_profile)
    if context_profile is not None and normalized_context_profile is None:
        typer.echo(
            "--context-profile must be one of: car_core, car_ambient, none",
            err=True,
        )
        raise typer.Exit(code=1) from None
    if normalized_context_profile is None:
        normalized_context_profile = default_managed_thread_context_profile(
            resource_kind=normalized_resource_kind
        )

    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
    except (OSError, ValueError) as exc:
        typer.echo(f"Failed to load hub config: {exc}", err=True)
        raise typer.Exit(code=1) from None

    required_cap = _CAPABILITY_REQUIREMENTS.get("thread_spawn")
    if required_cap and normalized_agent is not None:
        capabilities = _fetch_agent_capabilities(config, path)
        if not _check_capability(normalized_agent, required_cap, capabilities):
            typer.echo(
                f"Agent '{normalized_agent}' does not support thread creation (missing capability: {required_cap})",
                err=True,
            )
            raise typer.Exit(code=1) from None

    try:
        normalized_notify_on = _normalize_notify_on(notify_on)
        if terminal_followup is False and normalized_notify_on == "terminal":
            raise typer.BadParameter(
                "--no-terminal-followup cannot be combined with --notify-on terminal"
            )
        data = _request_json(
            "POST",
            _build_pma_url(config, "/threads"),
            {
                "agent": normalized_agent,
                "resource_kind": normalized_resource_kind,
                "resource_id": normalized_resource_id,
                "workspace_root": normalized_workspace_root,
                "name": name,
                "context_profile": normalized_context_profile,
                "notify_on": normalized_notify_on,
                "terminal_followup": terminal_followup,
                "notify_lane": notify_lane,
                "notify_once": notify_once,
            },
            token_env=config.server_auth_token_env,
        )
    except httpx.HTTPError as exc:
        typer.echo(f"HTTP error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except (ValueError, OSError, TypeError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if output_json:
        typer.echo(json.dumps(data, indent=2))
        return

    thread = data.get("thread", {}) if isinstance(data, dict) else {}
    if not isinstance(thread, dict) or not thread.get("managed_thread_id"):
        typer.echo("Failed to create managed thread", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(str(thread.get("managed_thread_id")))


def pma_thread_list(
    agent: Optional[str] = typer.Option(None, "--agent", help="Filter by agent"),
    status: Optional[str] = typer.Option(None, "--status", help="Filter by status"),
    repo_id: Optional[str] = typer.Option(None, "--repo", help="Filter by repo id"),
    resource_kind: Optional[str] = typer.Option(
        None, "--resource-kind", help="Filter by managed resource kind"
    ),
    resource_id: Optional[str] = typer.Option(
        None, "--resource-id", help="Filter by managed resource id"
    ),
    limit: int = typer.Option(200, "--limit", min=1, help="Maximum rows to return"),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON array output"),
    output_ndjson: bool = typer.Option(
        False, "--ndjson", help="Emit newline-delimited JSON output"
    ),
    path: Optional[Path] = hub_root_path_option(),
):
    """List managed PMA threads."""
    if output_json and output_ndjson:
        raise typer.BadParameter(
            "Choose only one of --json or --ndjson.",
            param_hint="--json / --ndjson",
        )

    hub_root = _resolve_hub_path(path)
    (
        normalized_resource_kind,
        normalized_resource_id,
        _normalized_workspace_root,
    ) = _normalize_resource_owner_options(
        repo_id=repo_id,
        resource_kind=resource_kind,
        resource_id=resource_id,
    )
    params = {
        key: value
        for key, value in {
            "agent": agent,
            "status": status,
            "resource_kind": normalized_resource_kind,
            "resource_id": normalized_resource_id,
            "limit": limit,
        }.items()
        if value is not None
    }
    try:
        config = load_hub_config(hub_root)
        data = _request_json(
            "GET",
            _build_pma_url(config, "/threads"),
            token_env=config.server_auth_token_env,
            params=params,
        )
    except httpx.HTTPError as exc:
        typer.echo(f"HTTP error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except (ValueError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    threads = data.get("threads", []) if isinstance(data, dict) else []
    if not isinstance(threads, list):
        threads = []
    normalized_threads = [thread for thread in threads if isinstance(thread, dict)]

    if output_json:
        typer.echo(json.dumps(normalized_threads, indent=2))
        return

    if output_ndjson:
        for thread in normalized_threads:
            typer.echo(json.dumps(thread))
        return

    if not normalized_threads:
        typer.echo("No managed threads found")
        return
    for thread in normalized_threads:
        typer.echo(
            " ".join(
                [
                    str(thread.get("managed_thread_id") or ""),
                    f"agent={thread.get('agent') or ''}",
                    f"status={thread.get('normalized_status') or thread.get('status') or ''}",
                    f"reason={thread.get('status_reason_code') or thread.get('status_reason') or '-'}",
                    _format_resource_owner_label(thread),
                ]
            ).strip()
        )


def pma_thread_info(
    managed_thread_id: str = typer.Option(
        ..., "--id", help="Managed PMA thread id", show_default=False
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = hub_root_path_option(),
):
    """Show managed PMA thread details."""
    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
        data = _request_json(
            "GET",
            _build_pma_url(config, f"/threads/{managed_thread_id}"),
            token_env=config.server_auth_token_env,
        )
    except httpx.HTTPError as exc:
        typer.echo(f"HTTP error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except (ValueError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if output_json:
        typer.echo(json.dumps(data, indent=2))
        return

    thread = data.get("thread", {}) if isinstance(data, dict) else {}
    if not isinstance(thread, dict):
        typer.echo("Thread not found", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(json.dumps(thread, indent=2))


def pma_thread_status(
    managed_thread_id: str = typer.Option(
        ..., "--id", help="Managed PMA thread id", show_default=False
    ),
    limit: int = typer.Option(
        20, "--limit", min=1, help="Maximum progress events to include"
    ),
    since: Optional[str] = typer.Option(
        None, "--since", help="Only include events newer than duration (e.g. 5m)"
    ),
    level: _PmaVerbosityLevel = typer.Option(
        _PmaVerbosityLevel.INFO,
        "--level",
        help="Tail verbosity for recent_progress and diagnostics (accepted: info, debug)",
    ),
    include_output: bool = typer.Option(
        False,
        "--output",
        help="Include assistant_text for the latest turn, with automatic pagination",
    ),
    output_lines: Optional[str] = typer.Option(
        None,
        "--lines",
        help="assistant_text-only line range (1-based), for example 1:80 or 100:",
    ),
    continue_output: bool = typer.Option(
        False,
        "--continue",
        help="Continue assistant_text pagination from the prior status --output call",
    ),
    output_file: Optional[Path] = typer.Option(
        None,
        "--output-file",
        help="Write assistant_text to a file instead of printing it",
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = hub_root_path_option(),
):
    """Show unified managed-thread status.

    Canonical field names in the text view match the API model:
    `operator_status`, `runtime_status`, `managed_turn_id`, `turn_status`,
    `status_reason`, and `assistant_text`.
    """
    hub_root = _resolve_hub_path(path)
    params: dict[str, Any] = {"limit": limit, "level": level.value}
    if since:
        params["since"] = since
    try:
        config = load_hub_config(hub_root)
        data = _request_json(
            "GET",
            _build_pma_url(config, f"/threads/{managed_thread_id}/status"),
            token_env=config.server_auth_token_env,
            params=params,
        )
    except httpx.HTTPError as exc:
        typer.echo(f"HTTP error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except (ValueError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if output_json:
        typer.echo(json.dumps(data, indent=2))
        return
    _render_thread_status_snapshot(data)
    if not include_output:
        return

    snapshot = _PmaThreadStatusSnapshot.from_dict(data)
    window = _resolve_assistant_text_window(
        assistant_text=snapshot.latest_assistant_text,
        lines_spec=output_lines,
        continue_output=continue_output,
        command_name="status",
        hub_root=hub_root,
        managed_thread_id=managed_thread_id,
        managed_turn_id=snapshot.latest_turn_id or snapshot.managed_turn_id,
    )
    typer.echo()
    _render_assistant_text_window(
        window=window,
        output_file=output_file,
        label="assistant_text",
    )


def pma_thread_send(
    managed_thread_id: str = typer.Option(
        ..., "--id", help="Managed PMA thread id", show_default=False
    ),
    message: Optional[str] = typer.Option(
        None, "--message", help="User message to send", show_default=False
    ),
    message_file: Optional[Path] = typer.Option(
        None, "--message-file", help="Read the user message from a file"
    ),
    message_stdin: bool = typer.Option(
        False, "--message-stdin", help="Read the user message from stdin"
    ),
    model: Optional[str] = typer.Option(None, "--model", help="Model override"),
    reasoning: Optional[str] = typer.Option(
        None, "--reasoning", help="Reasoning override"
    ),
    if_busy: str = typer.Option(
        "queue",
        "--if-busy",
        help="Busy-thread policy: queue, interrupt, or reject",
    ),
    watch: bool = typer.Option(
        False,
        "--watch",
        help="Opt into synchronous foreground tailing until terminal state",
    ),
    notify_on: Optional[str] = typer.Option(
        None,
        "--notify-on",
        help="Create a wake-up subscription for lifecycle events (accepted: terminal)",
    ),
    notify_lane: Optional[str] = typer.Option(
        None, "--notify-lane", help="Lane id used for wake-up delivery"
    ),
    notify_once: bool = typer.Option(
        True,
        "--notify-once/--no-notify-once",
        help="Auto-cancel notification after first fire",
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = hub_root_path_option(),
):
    """Send a message to a managed PMA thread.

    When follow-up subscriptions are enabled, the acceptance line includes the
    created `subscription_id`. Use `car pma thread status --output` or
    `car pma thread output` to read `assistant_text` after the wake-up fires.
    """
    message_body = _resolve_message_body(
        message=message,
        message_file=message_file,
        message_stdin=message_stdin,
        option_hint="--message, --message-file, or --message-stdin",
    )
    normalized_notify_on = _normalize_notify_on(notify_on)
    should_defer = True
    normalized_if_busy = (if_busy or "").strip().lower() or "queue"
    if normalized_if_busy not in {"queue", "interrupt", "reject"}:
        raise typer.BadParameter("if-busy must be queue, interrupt, or reject")
    hub_root = _resolve_hub_path(path)
    config = None
    timeout_probe: Optional[_ManagedThreadSendTimeoutProbe] = None
    response: Optional[_ManagedThreadSendResponse] = None
    try:
        config = load_hub_config(hub_root)
        timeout_probe = _capture_managed_thread_send_timeout_probe(
            config,
            managed_thread_id=managed_thread_id,
        )
        request_payload = _ManagedThreadSendRequest(
            message=message_body,
            busy_policy=normalized_if_busy,
            defer_execution=should_defer,
            model=model,
            reasoning=reasoning,
            notify_on=normalized_notify_on,
            notify_lane=notify_lane,
            notify_once=notify_once,
        )
        status_code, data = _request_json_with_status(
            "POST",
            _build_pma_url(config, f"/threads/{managed_thread_id}/messages"),
            request_payload.to_payload(),
            token_env=config.server_auth_token_env,
            timeout=_MANAGED_THREAD_SEND_REQUEST_TIMEOUT_SECONDS,
        )
        response = _ManagedThreadSendResponse.from_http(
            status_code, data, default_message=message_body
        )
    except httpx.TimeoutException as exc:
        recovered_response = _recover_managed_thread_send_timeout(
            config,
            managed_thread_id=managed_thread_id,
            message_body=message_body,
            baseline=timeout_probe,
        )
        if recovered_response is not None:
            response = recovered_response
            data = {
                "status": response.status,
                "send_state": response.send_state,
                "execution_state": response.execution_state,
                "managed_turn_id": response.managed_turn_id,
                "active_managed_turn_id": response.active_managed_turn_id,
                "queue_depth": response.queue_depth,
                "delivered_message": response.delivered_message,
                "assistant_text": response.assistant_text,
                "detail": response.detail,
                "error": response.error,
                "next_step": response.next_step,
            }
        else:
            detail = (
                "Timed out waiting for send confirmation. The message may still "
                "have been delivered. Check `car pma thread status --id "
                f"{managed_thread_id} --path {hub_root}` before retrying."
            )
            typer.echo(f"Error: {detail}", err=True)
            raise typer.Exit(code=1) from exc
    except (httpx.HTTPError, ValueError, OSError, TypeError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if config is None:
        raise typer.Exit(code=1) from None
    if response is None:
        raise typer.Exit(code=1) from None
    if not response.is_ok:
        if output_json:
            typer.echo(json.dumps(data, indent=2))
        else:
            detail = response.error_detail()
            if response.send_state:
                typer.echo(
                    f"send_state={response.send_state} error={detail}",
                    err=True,
                )
            else:
                typer.echo(detail, err=True)
            if response.next_step:
                typer.echo(f"next: {response.next_step}", err=True)
        raise typer.Exit(code=1) from None

    if output_json:
        typer.echo(json.dumps(data, indent=2))
        if watch:
            pma_thread_tail(
                managed_thread_id=managed_thread_id,
                follow=True,
                since=None,
                level=_PmaVerbosityLevel.INFO,
                limit=50,
                output_json=True,
                path=path,
            )
        return

    if response.execution_state == "queued" or (
        should_defer and response.execution_state == "running"
    ):
        typer.echo(response.accepted_line())
        _echo_delivered_message(response.delivered_message)
        if response.detail:
            typer.echo(f"note: {response.detail}")
        if watch:
            pma_thread_tail(
                managed_thread_id=managed_thread_id,
                follow=True,
                since=None,
                level=_PmaVerbosityLevel.INFO,
                limit=50,
                output_json=False,
                path=path,
            )
            try:
                status_data = _request_json(
                    "GET",
                    _build_pma_url(config, f"/threads/{managed_thread_id}/status"),
                    token_env=config.server_auth_token_env,
                    params={"limit": 1},
                )
            except (httpx.HTTPError, ValueError, OSError):
                status_data = {}
            excerpt = str(status_data.get("latest_output_excerpt") or "").strip()
            if excerpt:
                typer.echo("\nassistant_text_excerpt:")
                typer.echo(excerpt)
        return

    typer.echo(response.completion_line())
    _echo_delivered_message(response.delivered_message)
    if response.assistant_text:
        typer.echo("\nassistant:")
        typer.echo(response.assistant_text)


def pma_thread_queue(
    managed_thread_id: str = typer.Option(
        ..., "--id", help="Managed PMA thread id", show_default=False
    ),
    limit: int = typer.Option(
        200, "--limit", min=1, help="Maximum queued turns to list"
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = hub_root_path_option(),
):
    """List pending queued turns for one managed PMA thread."""
    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
        data = _request_json(
            "GET",
            _build_pma_url(config, f"/threads/{managed_thread_id}/queue"),
            token_env=config.server_auth_token_env,
            params={"limit": limit},
        )
    except httpx.HTTPError as exc:
        typer.echo(f"HTTP error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except (ValueError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if output_json:
        typer.echo(json.dumps(data, indent=2))
        return

    queue_depth = _coerce_optional_int(data.get("queue_depth")) or 0
    raw_queued_turns = data.get("queued_turns")
    queued_turns = tuple(
        turn_item
        for item in (raw_queued_turns if isinstance(raw_queued_turns, list) else [])
        if (turn_item := _PmaQueuedTurnSnapshot.from_dict(item)) is not None
    )
    if queue_depth <= 0 or not queued_turns:
        typer.echo(f"No queued turns for {managed_thread_id}")
        return

    typer.echo(f"managed_thread_id={managed_thread_id} queue_depth={queue_depth}")
    for queued_turn in queued_turns:
        typer.echo(queued_turn.render_line())


def pma_thread_cancel_turn(
    managed_thread_id: str = typer.Option(
        ..., "--id", help="Managed PMA thread id", show_default=False
    ),
    queued_turn_id: str = typer.Option(
        ...,
        "--queued",
        "--turn",
        help="Queued managed turn id to cancel",
        show_default=False,
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = hub_root_path_option(),
):
    """Cancel one queued turn without interrupting the active turn."""
    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
        status_code, data = _request_json_with_status(
            "POST",
            _build_pma_url(
                config,
                f"/threads/{managed_thread_id}/queue/{queued_turn_id}/cancel",
            ),
            token_env=config.server_auth_token_env,
        )
    except httpx.HTTPError as exc:
        typer.echo(f"HTTP error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except (ValueError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if status_code >= 400:
        if output_json:
            typer.echo(json.dumps(data, indent=2))
        else:
            detail = str(data.get("detail") or "").strip()
            typer.echo(
                detail or f"Queued turn cancellation failed (HTTP {status_code})",
                err=True,
            )
        raise typer.Exit(code=1) from None

    if output_json:
        typer.echo(json.dumps(data, indent=2))
        return

    position = _coerce_optional_int(data.get("position"))
    queue_depth = _coerce_optional_int(data.get("queue_depth")) or 0
    line = f"Cancelled queued turn {queued_turn_id}"
    if position is not None:
        line += f" (position was {position})"
    line += f" remaining={queue_depth}"
    typer.echo(line)


def pma_thread_turns(
    managed_thread_id: str = typer.Option(
        ..., "--id", help="Managed PMA thread id", show_default=False
    ),
    limit: int = typer.Option(50, "--limit", min=1, help="Maximum rows to return"),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = hub_root_path_option(),
):
    """List managed PMA thread turns."""
    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
        data = _request_json(
            "GET",
            _build_pma_url(config, f"/threads/{managed_thread_id}/turns"),
            token_env=config.server_auth_token_env,
            params={"limit": limit},
        )
    except httpx.HTTPError as exc:
        typer.echo(f"HTTP error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except (ValueError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if output_json:
        typer.echo(json.dumps(data, indent=2))
        return

    turns = data.get("turns", []) if isinstance(data, dict) else []
    if not isinstance(turns, list) or not turns:
        typer.echo("No turns found")
        return
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        typer.echo(
            " ".join(
                [
                    f"managed_turn_id={turn.get('managed_turn_id') or ''}",
                    f"turn_status={turn.get('status') or ''}",
                    f"started_at={turn.get('started_at') or ''}",
                    f"finished_at={turn.get('finished_at') or ''}",
                ]
            ).strip()
        )


def pma_thread_output(
    managed_thread_id: str = typer.Option(
        ..., "--id", help="Managed PMA thread id", show_default=False
    ),
    managed_turn_id: Optional[str] = typer.Option(
        None,
        "--turn",
        help="Managed turn id. Defaults to the latest turn on the thread.",
    ),
    output_lines: Optional[str] = typer.Option(
        None,
        "--lines",
        help="assistant_text line range (1-based), for example 1:80 or 100:",
    ),
    continue_output: bool = typer.Option(
        False,
        "--continue",
        help="Continue assistant_text pagination from the prior output call",
    ),
    output_file: Optional[Path] = typer.Option(
        None,
        "--output-file",
        help="Write assistant_text to a file instead of printing it",
    ),
    path: Optional[Path] = hub_root_path_option(),
):
    """Print `assistant_text` for a managed PMA thread turn.

    This is the first-class CLI surface for `/threads/{thread}/turns/{turn}` and
    supports line ranges, continuation-based pagination, and file export.
    """
    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
        resolved_turn_id, turn_data = _fetch_managed_turn_payload(
            config,
            managed_thread_id=managed_thread_id,
            managed_turn_id=managed_turn_id,
        )
    except httpx.HTTPError as exc:
        typer.echo(f"HTTP error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except typer.Exit:
        raise
    except (ValueError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    turn = turn_data.get("turn", {}) if isinstance(turn_data, dict) else {}
    assistant_text = turn.get("assistant_text") if isinstance(turn, dict) else ""
    window = _resolve_assistant_text_window(
        assistant_text=str(assistant_text or ""),
        lines_spec=output_lines,
        continue_output=continue_output,
        command_name="output",
        hub_root=hub_root,
        managed_thread_id=managed_thread_id,
        managed_turn_id=resolved_turn_id,
    )
    _render_assistant_text_window(
        window=window,
        output_file=output_file,
        label="assistant_text",
    )


def pma_thread_subscribe(
    managed_thread_id: str = typer.Option(
        ..., "--id", help="Managed PMA thread id", show_default=False
    ),
    event_types: Optional[list[str]] = typer.Option(
        None,
        "--event",
        help="Lifecycle event type to subscribe to. Repeat to add more values.",
    ),
    lane_id: Optional[str] = typer.Option(
        None,
        "--lane",
        help="Lane id for wake-up delivery. Defaults from the thread binding when possible.",
    ),
    notify_once: bool = typer.Option(
        True,
        "--once/--persistent",
        help="Auto-cancel the subscription after the first matching wake-up",
    ),
    confirm_duplicate: bool = typer.Option(
        False,
        "--confirm-duplicate",
        help="Allow a duplicate subscription even when an active auto-subscription already covers this thread",
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = hub_root_path_option(),
):
    """Create a thread-scoped wake-up subscription without raw curl.

    This command always targets one managed thread via `thread_id`. For repo- or
    run-scoped automation, use the lower-level `/hub/pma/subscriptions` API.
    """
    hub_root = _resolve_hub_path(path)
    normalized_event_types = [
        str(item).strip().lower() for item in (event_types or []) if str(item).strip()
    ] or [
        "managed_thread_completed",
        "managed_thread_failed",
        "managed_thread_interrupted",
    ]
    payload: dict[str, Any] = {
        "thread_id": managed_thread_id,
        "event_types": normalized_event_types,
        "notify_once": notify_once,
    }
    if lane_id:
        payload["lane_id"] = lane_id
    if confirm_duplicate:
        payload["confirm"] = True

    try:
        config = load_hub_config(hub_root)
        data = _request_json(
            "POST",
            _build_pma_url(config, "/subscriptions"),
            payload,
            token_env=config.server_auth_token_env,
        )
    except httpx.HTTPError as exc:
        typer.echo(f"HTTP error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except (ValueError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if output_json:
        typer.echo(json.dumps(data, indent=2))
        return

    subscription = data.get("subscription", {}) if isinstance(data, dict) else {}
    if not isinstance(subscription, dict):
        typer.echo("Subscription creation failed", err=True)
        raise typer.Exit(code=1) from None
    summary = " ".join(
        [
            f"subscription_id={subscription.get('subscription_id') or '-'}",
            "scope=thread",
            f"thread_id={subscription.get('thread_id') or managed_thread_id}",
            f"lane_id={subscription.get('lane_id') or '-'}",
            "events="
            + ",".join(
                str(item)
                for item in (subscription.get("event_types") or normalized_event_types)
            ),
        ]
    ).strip()
    typer.echo(summary)
    warning = str(data.get("warning") or "").strip()
    if warning:
        typer.echo(f"note: {warning}")
    typer.echo(
        "next: wait for the wake-up, then read assistant_text with "
        f"`car pma thread output --id {managed_thread_id}`."
    )


def pma_thread_tail(
    managed_thread_id: str = typer.Option(
        ..., "--id", help="Managed PMA thread id", show_default=False
    ),
    follow: bool = typer.Option(
        False, "--follow", help="Follow live events until turn completes"
    ),
    since: Optional[str] = typer.Option(
        None, "--since", help="Only include events newer than duration (e.g. 5m)"
    ),
    level: _PmaVerbosityLevel = typer.Option(
        _PmaVerbosityLevel.INFO,
        "--level",
        help="Tail verbosity for event payloads (accepted: info, debug)",
    ),
    limit: int = typer.Option(50, "--limit", min=1, help="Maximum events to include"),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = hub_root_path_option(),
):
    """Show managed-thread tail/progress events."""
    hub_root = _resolve_hub_path(path)
    params: dict[str, Any] = {"limit": limit, "level": level.value}
    if since:
        params["since"] = since
    try:
        config = load_hub_config(hub_root)
        if not follow:
            data = _request_json(
                "GET",
                _build_pma_url(config, f"/threads/{managed_thread_id}/tail"),
                token_env=config.server_auth_token_env,
                params=params,
            )
            if output_json:
                typer.echo(json.dumps(data, indent=2))
            else:
                _render_tail_snapshot(data)
            return

        headers = _auth_headers_from_env(config.server_auth_token_env)
        url = _build_pma_url(config, f"/threads/{managed_thread_id}/tail/events")
        with httpx.stream(
            "GET",
            url,
            params=params,
            headers=headers,
            timeout=httpx.Timeout(connect=10.0, read=300.0, write=10.0, pool=10.0),
        ) as response:
            response.raise_for_status()
            for event_name, data_str, event_id in _iter_sse_events(
                response.iter_lines()
            ):
                try:
                    data = json.loads(data_str) if data_str else {}
                except json.JSONDecodeError:
                    data = {"raw": data_str}
                if output_json:
                    payload = {"event": event_name, "data": data}
                    if event_id is not None:
                        payload["id"] = event_id
                    typer.echo(json.dumps(payload))
                    continue
                if event_name == "state":
                    if isinstance(data, dict):
                        _render_tail_snapshot(data)
                    continue
                if event_name == "tail":
                    if isinstance(data, dict):
                        typer.echo(_format_tail_event_line(data))
                    continue
                if event_name == "progress" and isinstance(data, dict):
                    status = data.get("turn_status") or "running"
                    elapsed = _format_seconds(data.get("elapsed_seconds"))
                    idle = _format_seconds(data.get("idle_seconds"))
                    phase = str(data.get("phase") or "-")
                    line = (
                        f"progress: status={status} phase={phase} "
                        f"elapsed={elapsed} idle={idle}"
                    )
                    idle_seconds = data.get("idle_seconds")
                    if (
                        isinstance(idle_seconds, int)
                        and status == "running"
                        and idle_seconds >= 30
                    ):
                        line += " (possibly stalled)"
                    typer.echo(line)
                    guidance = str(data.get("guidance") or "").strip()
                    if guidance:
                        typer.echo(f"guidance: {guidance}")
                    diagnostics = data.get("active_turn_diagnostics")
                    if isinstance(diagnostics, dict):
                        _render_active_turn_diagnostics(diagnostics)
                    if status != "running":
                        return
    except httpx.HTTPError as exc:
        typer.echo(f"HTTP error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except KeyboardInterrupt:
        raise typer.Exit(code=130) from None
    except (ValueError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None


def pma_thread_compact(
    managed_thread_id: Optional[str] = typer.Option(
        None, "--id", help="Managed PMA thread id", show_default=False
    ),
    status: Optional[str] = typer.Option(
        None, "--status", help="Bulk compact threads matching a status filter"
    ),
    all_threads: bool = typer.Option(
        False,
        "--all",
        help="Compact all non-archived managed threads matching the other filters",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Required when compacting all non-archived managed threads",
    ),
    summary: str = typer.Option(..., "--summary", help="Compaction summary"),
    no_reset_backend: bool = typer.Option(
        False, "--no-reset-backend", help="Preserve backend thread/session id"
    ),
    agent: Optional[str] = typer.Option(None, "--agent", help="Filter by agent"),
    repo_id: Optional[str] = typer.Option(None, "--repo", help="Filter by repo id"),
    resource_kind: Optional[str] = typer.Option(
        None, "--resource-kind", help="Filter by managed resource kind"
    ),
    resource_id: Optional[str] = typer.Option(
        None, "--resource-id", help="Filter by managed resource id"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Preview matching threads without compacting"
    ),
    limit: int = typer.Option(
        1000, "--limit", min=1, help="Maximum bulk-selected threads to inspect"
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = hub_root_path_option(),
):
    """Store a compaction seed on one or more managed PMA threads."""
    hub_root = _resolve_hub_path(path)
    try:
        scope_status, scope_all = _normalize_thread_compact_scope(
            managed_thread_id=managed_thread_id,
            status=status,
            all_threads=all_threads,
        )
    except typer.BadParameter as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if scope_all and not force:
        typer.echo(
            "Error: --force is required with --all or --status all.",
            err=True,
        )
        raise typer.Exit(code=1) from None

    (
        normalized_resource_kind,
        normalized_resource_id,
        _normalized_workspace_root,
    ) = _normalize_resource_owner_options(
        repo_id=repo_id,
        resource_kind=resource_kind,
        resource_id=resource_id,
    )
    try:
        config = load_hub_config(hub_root)
        if managed_thread_id:
            targets = [{"managed_thread_id": managed_thread_id}]
        else:
            params = {
                key: value
                for key, value in {
                    "agent": agent,
                    "status": None if scope_all else scope_status,
                    "resource_kind": normalized_resource_kind,
                    "resource_id": normalized_resource_id,
                    "limit": limit,
                }.items()
                if value is not None
            }
            data = _request_json(
                "GET",
                _build_pma_url(config, "/threads"),
                token_env=config.server_auth_token_env,
                params=params,
            )
            raw_threads = data.get("threads", []) if isinstance(data, dict) else []
            threads = [item for item in raw_threads if isinstance(item, dict)]
            targets = [
                thread
                for thread in threads
                if str(thread.get("lifecycle_status") or "").strip().lower()
                != "archived"
                and str(thread.get("normalized_status") or thread.get("status") or "")
                .strip()
                .lower()
                != "archived"
            ]

        target_ids = [
            str(item.get("managed_thread_id") or "").strip()
            for item in targets
            if str(item.get("managed_thread_id") or "").strip()
        ]
        if not target_ids:
            empty_payload = {
                "dry_run": dry_run,
                "matched": 0,
                "compacted": [],
                "errors": [],
            }
            if output_json:
                typer.echo(json.dumps(empty_payload, indent=2))
            else:
                typer.echo("No managed threads matched the compact selection")
            return

        preview_payload = {
            "dry_run": dry_run,
            "matched": len(target_ids),
            "scope": {
                "id": managed_thread_id,
                "status": scope_status,
                "all": scope_all,
                "agent": agent,
                "resource_kind": normalized_resource_kind,
                "resource_id": normalized_resource_id,
            },
            "threads": targets,
        }
        if output_json and dry_run:
            typer.echo(json.dumps(preview_payload, indent=2))
        elif not output_json:
            typer.echo(
                f"Dry run summary: {len(target_ids)} thread"
                f"{'' if len(target_ids) == 1 else 's'} would be compacted"
            )
            for thread in targets:
                typer.echo(_thread_compact_target_line(thread))

        _record_thread_compact_audit(
            hub_root=hub_root,
            summary=summary,
            managed_thread_ids=target_ids,
            mode="dry_run" if dry_run else "execute",
            reset_backend=(not no_reset_backend),
            status=scope_status,
            scope_all=scope_all,
            resource_kind=normalized_resource_kind,
            resource_id=normalized_resource_id,
            agent=agent,
        )
        if dry_run:
            return

        compacted: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for target_id in target_ids:
            try:
                data = _request_json(
                    "POST",
                    _build_pma_url(config, f"/threads/{target_id}/compact"),
                    {"summary": summary, "reset_backend": (not no_reset_backend)},
                    token_env=config.server_auth_token_env,
                )
            except httpx.HTTPError as exc:
                errors.append(
                    {"managed_thread_id": target_id, "error": f"HTTP error: {exc}"}
                )
                continue
            except (json.JSONDecodeError, ValueError) as exc:
                errors.append({"managed_thread_id": target_id, "error": str(exc)})
                continue
            compacted.append(
                {
                    "managed_thread_id": target_id,
                    "thread": data.get("thread") if isinstance(data, dict) else None,
                }
            )

        _record_thread_compact_audit(
            hub_root=hub_root,
            summary=summary,
            managed_thread_ids=target_ids,
            mode="result",
            reset_backend=(not no_reset_backend),
            status=scope_status,
            scope_all=scope_all,
            resource_kind=normalized_resource_kind,
            resource_id=normalized_resource_id,
            agent=agent,
            errors=errors,
        )

        result_payload = {
            "dry_run": False,
            "matched": len(target_ids),
            "compacted": compacted,
            "errors": errors,
        }
        if output_json:
            typer.echo(json.dumps(result_payload, indent=2))
        else:
            for item in compacted:
                typer.echo(f"Compacted {item['managed_thread_id']}")
            if errors:
                for item in errors:
                    typer.echo(
                        f"Failed {item['managed_thread_id']}: {item['error']}",
                        err=True,
                    )
            typer.echo(
                f"Compacted {len(compacted)} thread{'' if len(compacted) == 1 else 's'}"
            )
        if errors:
            raise typer.Exit(code=1) from None
    except httpx.HTTPError as exc:
        typer.echo(f"HTTP error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except (ValueError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None


def pma_thread_resume(
    managed_thread_id: str = typer.Option(
        ..., "--id", help="Managed PMA thread id", show_default=False
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = hub_root_path_option(),
):
    """Set a managed thread active."""
    hub_root = _resolve_hub_path(path)
    thread_data: dict[str, Any] = {}
    try:
        config = load_hub_config(hub_root)
        thread_data = _request_json(
            "GET",
            _build_pma_url(config, f"/threads/{managed_thread_id}"),
            token_env=config.server_auth_token_env,
        )
        if _normalized_thread_lifecycle_status(thread_data).lower() == "active":
            if output_json:
                typer.echo(
                    json.dumps(
                        {
                            "status": "ok",
                            "resumed": False,
                            "detail": _resume_noop_message(
                                managed_thread_id, thread_data
                            ),
                            "thread": _thread_payload(thread_data),
                        },
                        indent=2,
                    )
                )
            else:
                typer.echo(_resume_noop_message(managed_thread_id, thread_data))
            return
        data = _request_json(
            "POST",
            _build_pma_url(config, f"/threads/{managed_thread_id}/resume"),
            {},
            token_env=config.server_auth_token_env,
        )
    except httpx.HTTPError as exc:
        typer.echo(f"HTTP error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except (ValueError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if output_json:
        typer.echo(json.dumps(data, indent=2))
    else:
        typer.echo(f"Resumed {managed_thread_id}")


def pma_thread_fork(
    managed_thread_id: str = typer.Option(
        ..., "--id", help="Managed PMA thread id", show_default=False
    ),
    name: Optional[str] = typer.Option(
        None, "--name", help="Optional new thread label"
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = hub_root_path_option(),
):
    """Fork a managed PMA thread when the backend runtime supports it."""
    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
        data = _request_json(
            "POST",
            _build_pma_url(config, f"/threads/{managed_thread_id}/fork"),
            {"name": name},
            token_env=config.server_auth_token_env,
        )
    except httpx.HTTPError as exc:
        typer.echo(f"HTTP error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except (ValueError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if output_json:
        typer.echo(json.dumps(data, indent=2))
        return

    thread = data.get("thread", {}) if isinstance(data, dict) else {}
    if not isinstance(thread, dict) or not thread.get("managed_thread_id"):
        typer.echo("Failed to fork managed thread", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(str(thread.get("managed_thread_id")))


def pma_thread_archive(
    managed_thread_id: Optional[str] = typer.Option(
        None, "--id", help="Managed PMA thread id", show_default=False
    ),
    managed_thread_ids: Optional[str] = typer.Option(
        None,
        "--ids",
        help="Comma- or whitespace-separated managed PMA thread ids",
        show_default=False,
    ),
    managed_thread_ids_stdin: bool = typer.Option(
        False,
        "--ids-stdin",
        help="Read managed PMA thread ids from stdin (comma- or whitespace-separated)",
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = hub_root_path_option(),
):
    """Archive a managed PMA thread."""
    hub_root = _resolve_hub_path(path)
    thread_ids = _resolve_archive_thread_ids(
        managed_thread_id=managed_thread_id,
        managed_thread_ids=managed_thread_ids,
        managed_thread_ids_stdin=managed_thread_ids_stdin,
    )
    try:
        config = load_hub_config(hub_root)
        if len(thread_ids) == 1:
            archive_url = _build_pma_url(config, f"/threads/{thread_ids[0]}/archive")
            data = _request_json(
                "POST",
                archive_url,
                token_env=config.server_auth_token_env,
            )
        else:
            archive_url = _build_pma_url(config, "/threads/archive")
            data = _request_json(
                "POST",
                archive_url,
                {"thread_ids": thread_ids},
                token_env=config.server_auth_token_env,
            )
    except httpx.HTTPError as exc:
        typer.echo(
            format_hub_request_error(
                action=(
                    f"Failed to archive managed PMA thread {thread_ids[0]}."
                    if len(thread_ids) == 1
                    else "Failed to archive managed PMA threads."
                ),
                url=archive_url,
                exc=exc,
            ),
            err=True,
        )
        raise typer.Exit(code=1) from None
    except (ValueError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if output_json:
        typer.echo(json.dumps(data, indent=2))
        if len(thread_ids) > 1 and isinstance(data, dict) and data.get("errors"):
            raise typer.Exit(code=1) from None
        return

    if len(thread_ids) == 1:
        thread = data.get("thread", {}) if isinstance(data, dict) else {}
        if isinstance(thread, dict) and thread:
            typer.echo(_format_archived_thread_line(thread))
        else:
            typer.echo(f"Archived {thread_ids[0]}")
        return

    threads = data.get("threads", []) if isinstance(data, dict) else []
    errors = data.get("errors", []) if isinstance(data, dict) else []
    archived_count = len(threads) if isinstance(threads, list) else 0

    if isinstance(threads, list):
        for thread in threads:
            if isinstance(thread, dict):
                typer.echo(_format_archived_thread_line(thread))

    if isinstance(errors, list):
        for error in errors:
            if not isinstance(error, dict):
                continue
            thread_id = str(error.get("thread_id") or "unknown").strip()
            detail = str(error.get("detail") or "Archive failed").strip()
            typer.echo(f"Failed to archive {thread_id}: {detail}", err=True)

    typer.echo(
        f"Archived {archived_count} managed thread{'s' if archived_count != 1 else ''}."
    )
    if errors:
        raise typer.Exit(code=1) from None


def pma_thread_interrupt(
    managed_thread_id: str = typer.Option(
        ..., "--id", help="Managed PMA thread id", show_default=False
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = hub_root_path_option(),
):
    """Interrupt a running managed PMA thread turn."""
    hub_root = _resolve_hub_path(path)
    thread_data: dict[str, Any] = {}
    try:
        config = load_hub_config(hub_root)
    except (OSError, ValueError) as exc:
        typer.echo(f"Failed to load hub config: {exc}", err=True)
        raise typer.Exit(code=1) from None

    thread_url = _build_pma_url(config, f"/threads/{managed_thread_id}")
    try:
        thread_data = _request_json(
            "GET", thread_url, token_env=config.server_auth_token_env
        )
    except (httpx.HTTPError, ValueError, OSError):
        logger.debug("Failed to fetch thread data for interrupt check", exc_info=True)
    else:
        thread = thread_data.get("thread", {}) if isinstance(thread_data, dict) else {}
        if isinstance(thread, dict):
            agent = thread.get("agent", "")
            capabilities = _fetch_agent_capabilities(config, path)
            required_cap = _CAPABILITY_REQUIREMENTS.get("thread_interrupt")
            if required_cap and not _check_capability(
                agent, required_cap, capabilities
            ):
                typer.echo(
                    f"Agent '{agent}' does not support interrupt (missing capability: {required_cap})",
                    err=True,
                )
                raise typer.Exit(code=1) from None

    try:
        status_code, data = _request_json_with_status(
            "POST",
            _build_pma_url(config, f"/threads/{managed_thread_id}/interrupt"),
            token_env=config.server_auth_token_env,
        )
    except (httpx.HTTPError, ValueError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if status_code >= 400:
        if output_json:
            typer.echo(json.dumps(data, indent=2))
        else:
            detail = str(data.get("detail") or "").strip()
            if status_code == 409 and detail == "Managed thread has no running turn":
                typer.echo(
                    _interrupt_conflict_message(managed_thread_id, thread_data),
                    err=True,
                )
            else:
                typer.echo(
                    detail or f"Managed thread interrupt failed (HTTP {status_code})",
                    err=True,
                )
        raise typer.Exit(code=1) from None

    if output_json:
        typer.echo(json.dumps(data, indent=2))
    else:
        status = str(data.get("status") or "").strip().lower()
        if status == "ok":
            typer.echo(f"Interrupted {managed_thread_id}")
        else:
            detail = str(
                data.get("detail")
                or data.get("backend_error")
                or "Managed thread interrupt failed"
            )
            interrupt_state = str(data.get("interrupt_state") or "").strip()
            managed_turn_id = str(data.get("managed_turn_id") or "").strip()
            line = detail
            if interrupt_state:
                line = f"interrupt_state={interrupt_state} error={detail}"
            if managed_turn_id:
                line += f" managed_turn_id={managed_turn_id}"
            typer.echo(line, err=True)
            raise typer.Exit(code=1) from None


def register_thread_commands(app: typer.Typer) -> None:
    """Register all PMA managed-thread commands on *app*."""
    app.command("spawn")(pma_thread_spawn)
    app.command("create")(pma_thread_spawn)
    app.command("list")(pma_thread_list)
    app.command("info")(pma_thread_info)
    app.command("status")(pma_thread_status)
    app.command("queue")(pma_thread_queue)
    app.command("send")(pma_thread_send)
    app.command("cancel-turn")(pma_thread_cancel_turn)
    app.command("cancel-queued")(pma_thread_cancel_turn)
    app.command("turns")(pma_thread_turns)
    app.command("output")(pma_thread_output)
    app.command("subscribe")(pma_thread_subscribe)
    app.command("tail")(pma_thread_tail)
    app.command("compact")(pma_thread_compact)
    app.command("resume")(pma_thread_resume)
    app.command("fork")(pma_thread_fork)
    app.command("archive")(pma_thread_archive)
    app.command("interrupt")(pma_thread_interrupt)
