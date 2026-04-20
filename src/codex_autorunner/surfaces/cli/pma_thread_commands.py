"""PMA thread command implementations.

Managed-thread CLI commands (spawn, list, info, status, send, turns, output,
tail, compact, resume, fork, archive, interrupt) and supporting helpers and
dataclasses.  Registered on the ``thread_app`` typer via
:func:`register_thread_commands`.
"""

import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime
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
            "turn="
            + self.managed_turn_id
            + " status="
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
    enqueued_at: str
    prompt_preview: str

    @classmethod
    def from_dict(cls, data: Any) -> Optional["_PmaQueuedTurnSnapshot"]:
        if not isinstance(data, dict):
            return None
        return cls(
            managed_turn_id=str(data.get("managed_turn_id") or "-"),
            enqueued_at=str(data.get("enqueued_at") or "-"),
            prompt_preview=str(data.get("prompt_preview") or "")[:80],
        )

    def render_line(self) -> str:
        return (
            "queued_turn="
            + self.managed_turn_id
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
    last_turn_outcome: str
    is_alive: bool
    status_reason: str
    managed_turn_id: str
    turn_state: str
    activity: str
    phase: str
    elapsed_seconds: Optional[int]
    idle_seconds: Optional[int]
    guidance: str
    diagnostics: Optional[_PmaActiveTurnDiagnostics]
    last_tool: Optional[_PmaLastToolSnapshot]
    recent_progress: tuple[_PmaTailEvent, ...]
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
        raw_thread_status = str(payload.get("status") or thread.get("status") or "-")
        queue_depth_raw = payload.get("queue_depth")
        recent_progress = payload.get("recent_progress")
        queued_turns = payload.get("queued_turns")
        return cls(
            managed_thread_id=str(payload.get("managed_thread_id") or ""),
            agent=str(thread.get("agent") or "-"),
            owner_label=_format_resource_owner_label(thread),
            operator_status=derive_managed_thread_operator_status(
                normalized_status=raw_thread_status,
                lifecycle_status=str(thread.get("lifecycle_status") or "-"),
            ),
            last_turn_outcome=(
                raw_thread_status
                if raw_thread_status in {"completed", "interrupted", "failed"}
                else "-"
            ),
            is_alive=bool(payload.get("is_alive")),
            status_reason=str(
                payload.get("status_reason") or thread.get("status_reason") or "-"
            ),
            managed_turn_id=str(turn.get("managed_turn_id") or "-"),
            turn_state=str(turn.get("status") or "-"),
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
                    f"status={self.operator_status}",
                    f"last_turn={self.last_turn_outcome}",
                    f"alive={'yes' if self.is_alive else 'no'}",
                ]
            ),
            f"reason={self.status_reason}",
            "turn="
            + self.managed_turn_id
            + " status="
            + self.turn_state
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
            lines.append("latest output:")
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
        f"status={thread.get('status') or ''}",
        f"reason={thread.get('status_reason') or '-'}",
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
                    f"status={thread.get('status') or ''}",
                    f"reason={thread.get('status_reason') or '-'}",
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
    level: str = typer.Option("info", "--level", help="Verbosity level (info|debug)"),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = hub_root_path_option(),
):
    """Show unified managed-thread status in one view."""
    hub_root = _resolve_hub_path(path)
    params: dict[str, Any] = {"limit": limit, "level": level}
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
        help="Auto-subscribe for lifecycle events (supported: terminal)",
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
    """Send a message to a managed PMA thread."""
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
                level="info",
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
                level="info",
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
                typer.echo("\nlatest output:")
                typer.echo(excerpt)
        return

    typer.echo(response.completion_line())
    _echo_delivered_message(response.delivered_message)
    if response.assistant_text:
        typer.echo("\nassistant:")
        typer.echo(response.assistant_text)


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
                    str(turn.get("managed_turn_id") or ""),
                    f"status={turn.get('status') or ''}",
                    f"started={turn.get('started_at') or ''}",
                    f"finished={turn.get('finished_at') or ''}",
                ]
            ).strip()
        )


def pma_thread_output(
    managed_thread_id: str = typer.Option(
        ..., "--id", help="Managed PMA thread id", show_default=False
    ),
    path: Optional[Path] = hub_root_path_option(),
):
    """Print assistant_text for the latest turn of a managed PMA thread."""
    hub_root = _resolve_hub_path(path)
    try:
        config = load_hub_config(hub_root)
        turns_data = _request_json(
            "GET",
            _build_pma_url(config, f"/threads/{managed_thread_id}/turns"),
            token_env=config.server_auth_token_env,
            params={"limit": 1},
        )
        turns = turns_data.get("turns", []) if isinstance(turns_data, dict) else []
        if not isinstance(turns, list) or not turns:
            typer.echo("No turns found", err=True)
            raise typer.Exit(code=1) from None
        latest_turn = turns[0] if isinstance(turns[0], dict) else {}
        latest_turn_id = latest_turn.get("managed_turn_id") if latest_turn else None
        if not isinstance(latest_turn_id, str) or not latest_turn_id:
            typer.echo("Failed to resolve latest turn id", err=True)
            raise typer.Exit(code=1) from None
        turn_data = _request_json(
            "GET",
            _build_pma_url(
                config, f"/threads/{managed_thread_id}/turns/{latest_turn_id}"
            ),
            token_env=config.server_auth_token_env,
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
    typer.echo(str(assistant_text or ""))


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
    level: str = typer.Option("info", "--level", help="Verbosity level (info|debug)"),
    limit: int = typer.Option(50, "--limit", min=1, help="Maximum events to include"),
    output_json: bool = typer.Option(False, "--json", help="Emit JSON output"),
    path: Optional[Path] = hub_root_path_option(),
):
    """Show managed-thread tail/progress events."""
    hub_root = _resolve_hub_path(path)
    params: dict[str, Any] = {"limit": limit, "level": level}
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
            timeout=None,
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
                and str(thread.get("status") or "").strip().lower() != "archived"
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
    try:
        config = load_hub_config(hub_root)
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
        data = _request_json(
            "POST",
            _build_pma_url(config, f"/threads/{managed_thread_id}/interrupt"),
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
    app.command("send")(pma_thread_send)
    app.command("turns")(pma_thread_turns)
    app.command("output")(pma_thread_output)
    app.command("tail")(pma_thread_tail)
    app.command("compact")(pma_thread_compact)
    app.command("resume")(pma_thread_resume)
    app.command("fork")(pma_thread_fork)
    app.command("archive")(pma_thread_archive)
    app.command("interrupt")(pma_thread_interrupt)
