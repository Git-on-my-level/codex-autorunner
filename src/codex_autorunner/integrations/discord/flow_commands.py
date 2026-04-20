from __future__ import annotations

import asyncio
import hashlib
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, cast

from ...core.config import ConfigError
from ...core.flows import (
    FlowRunRecord,
    FlowRunStatus,
    flow_action_label,
)
from ...core.flows.archive_helpers import flow_run_archive_root
from ...core.flows.reconciler import reconcile_flow_run
from ...core.flows.ux_helpers import (
    GitHubServiceProtocol,
    build_flow_status_snapshot,
    issue_md_path,
    resolve_ticket_flow_archive_mode,
    seed_issue_from_github,
    seed_issue_from_text,
    select_default_ticket_flow_run,
    select_ticket_flow_run_record,
    ticket_flow_archive_requires_force,
    ticket_progress,
)
from ...core.logging_utils import log_event
from ...core.ticket_flow_summary import (
    build_ticket_flow_display,
    format_ticket_flow_summary_lines,
)
from ...core.utils import atomic_write
from ...integrations.github.service import GitHubError, GitHubService
from ...tickets.outbox import resolve_outbox_paths
from .components import (
    DISCORD_SELECT_OPTION_MAX_OPTIONS,
    build_action_row,
    build_button,
    build_flow_runs_picker,
    build_flow_status_buttons,
)
from .errors import DiscordTransientError
from .interaction_registry import FLOW_ACTION_SELECT_PREFIX
from .interaction_runtime import (
    defer_and_update_runtime_component_message,
    ensure_component_response_deferred,
    ensure_public_response_deferred,
    interaction_response_deferred,
    send_runtime_ephemeral,
    update_runtime_component_message,
)

_logger = logging.getLogger(__name__)

FLOW_RUNS_DEFAULT_LIMIT = 5
FLOW_RUNS_MAX_LIMIT = DISCORD_SELECT_OPTION_MAX_OPTIONS
_LEGACY_FLOW_ARCHIVE_MARKERS = frozenset(
    {"archived_tickets", "archived_runs", "contextspace", "flow_state"}
)


def _flow_run_matches_action(record: FlowRunRecord, action: str) -> bool:
    if action == "resume":
        return record.status == FlowRunStatus.PAUSED
    if action == "reply":
        return record.status == FlowRunStatus.PAUSED
    if action == "stop":
        return not record.status.is_terminal()
    if action == "recover":
        return not record.status.is_terminal()
    return True


def flow_archive_prompt_text(record: FlowRunRecord) -> str:
    return (
        f"Run {record.id} is {record.status.value}. "
        "Archiving it will reset the live tickets/contextspace state and move the "
        "current run artifacts into the archive. Archive it anyway?"
    )


def flow_archive_in_progress_text(run_id: str) -> str:
    return f"Archiving run {run_id}... This can take a few seconds."


def flow_restart_in_progress_text(run_id: str) -> str:
    return f"Restarting run {run_id}..."


def flow_resume_in_progress_text(run_id: str) -> str:
    return f"Resuming run {run_id}..."


def flow_stop_in_progress_text(run_id: str) -> str:
    return f"Stopping run {run_id}..."


def flow_refresh_in_progress_text(run_id: str) -> str:
    return f"Refreshing run {run_id}..."


async def _send_flow_public_response(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    deferred: bool,
    text: str,
    component_response: bool = False,
    components: Optional[list[dict[str, Any]]] = None,
) -> None:
    if component_response:
        await service.send_or_update_component_message(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=text,
            components=components,
        )
        return
    if components is not None:
        await service.send_or_respond_public_with_components(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=text,
            components=components,
        )
        return
    await service.send_or_respond_public(
        interaction_id=interaction_id,
        interaction_token=interaction_token,
        deferred=deferred,
        text=text,
    )


async def _send_flow_ephemeral_response(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    deferred: bool,
    text: str,
    component_response: bool = False,
    components: Optional[list[dict[str, Any]]] = None,
) -> None:
    if component_response:
        await service.send_or_update_component_message(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=text,
            components=components,
        )
        return
    if components is not None:
        await service.send_or_respond_ephemeral_with_components(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=text,
            components=components,
        )
        return
    await service.send_or_respond_ephemeral(
        interaction_id=interaction_id,
        interaction_token=interaction_token,
        deferred=deferred,
        text=text,
    )


def _normalize_run_id(value: str) -> Optional[str]:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        return None


def _legacy_flow_archive_root(workspace_root: Path, run_id: str) -> Path:
    return workspace_root / ".codex-autorunner" / "flows" / run_id


def _describe_archived_flow_run(
    workspace_root: Path,
    run_id: str,
) -> Optional[dict[str, Any]]:
    normalized_run_id = _normalize_run_id(run_id)
    if normalized_run_id is None:
        return None
    roots = (
        (flow_run_archive_root(workspace_root, normalized_run_id), True),
        (_legacy_flow_archive_root(workspace_root, normalized_run_id), False),
    )
    for archive_root, treat_any_child_as_archive in roots:
        if not archive_root.exists() or not archive_root.is_dir():
            continue
        tickets_dir = archive_root / "archived_tickets"
        runs_dirs = [
            child
            for child in archive_root.iterdir()
            if child.is_dir() and child.name.startswith("archived_runs")
        ]
        flow_state_dirs = [
            child
            for child in archive_root.iterdir()
            if child.is_dir() and child.name.startswith("flow_state")
        ]
        contextspace_dir = archive_root / "contextspace"
        has_tickets = tickets_dir.exists() and tickets_dir.is_dir()
        has_runs = bool(runs_dirs)
        has_contextspace = contextspace_dir.exists() and contextspace_dir.is_dir()
        has_flow_state = bool(flow_state_dirs)
        if not any((has_tickets, has_runs, has_contextspace, has_flow_state)):
            continue
        artifact_children: list[Path] = []
        if has_tickets:
            artifact_children.append(tickets_dir)
        artifact_children.extend(runs_dirs)
        if has_contextspace:
            artifact_children.append(contextspace_dir)
        artifact_children.extend(flow_state_dirs)
        if not treat_any_child_as_archive:
            artifact_children = [
                child
                for child in artifact_children
                if child.name in _LEGACY_FLOW_ARCHIVE_MARKERS
                or child.name.startswith("archived_runs")
                or child.name.startswith("flow_state")
            ]
        if not artifact_children:
            continue
        mtime_candidates = []
        for child in artifact_children:
            try:
                mtime_candidates.append(child.stat().st_mtime)
            except OSError:
                continue
        archived_at = None
        if mtime_candidates:
            archived_at = datetime.fromtimestamp(
                max(mtime_candidates), tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            archive_path = archive_root.relative_to(workspace_root).as_posix()
        except ValueError:
            archive_path = str(archive_root)
        return {
            "run_id": normalized_run_id,
            "archive_path": archive_path,
            "archived_at": archived_at,
            "has_tickets": has_tickets,
            "has_runs": has_runs,
            "has_contextspace": has_contextspace,
            "has_flow_state": has_flow_state,
        }
    return None


def _format_archived_flow_status_text(
    run_id: str,
    archived_summary: dict[str, Any],
    *,
    prefix: Optional[str] = None,
) -> str:
    lines: list[str] = []
    if isinstance(prefix, str) and prefix.strip():
        lines.append(prefix.strip())
        lines.append("")
    else:
        lines.append(f"Run {run_id} has already been archived.")
        lines.append("")
    lines.append(f"Run: {run_id}")
    lines.append("Status: archived")
    archived_at = archived_summary.get("archived_at")
    if isinstance(archived_at, str) and archived_at.strip():
        lines.append(f"Archived at: {archived_at.strip()}")
    archive_path = archived_summary.get("archive_path")
    if isinstance(archive_path, str) and archive_path.strip():
        lines.append(f"Archive path: {archive_path.strip()}")
    lines.append(
        f"Tickets archived: {'yes' if archived_summary.get('has_tickets') else 'no'}"
    )
    lines.append(
        f"Run artifacts archived: {'yes' if archived_summary.get('has_runs') else 'no'}"
    )
    lines.append(
        "Contextspace archived: "
        f"{'yes' if archived_summary.get('has_contextspace') else 'no'}"
    )
    lines.append(
        "Flow state archived: "
        f"{'yes' if archived_summary.get('has_flow_state') else 'no'}"
    )
    lines.append(
        "Use /flow status to inspect the current run or /flow runs for history."
    )
    return "\n".join(lines)


def _archived_flow_status_summary_from_archive_result(
    workspace_root: Path,
    summary: dict[str, Any],
) -> Optional[dict[str, Any]]:
    run_id_value = summary.get("run_id")
    if not isinstance(run_id_value, str) or not run_id_value.strip():
        return None
    normalized_run_id = _normalize_run_id(run_id_value)
    if normalized_run_id is None:
        return None

    archive_dir_value = summary.get("archive_dir")
    if isinstance(archive_dir_value, str) and archive_dir_value.strip():
        archive_root = Path(archive_dir_value)
    else:
        archive_root = flow_run_archive_root(workspace_root, normalized_run_id)

    has_tickets = bool(summary.get("archived_tickets"))
    has_runs = bool(summary.get("archived_runs"))
    has_contextspace = bool(summary.get("archived_contextspace"))
    has_flow_state = bool(summary.get("archived_flow_state"))

    artifact_children: list[Path] = []
    if archive_root.exists() and archive_root.is_dir():
        artifact_children = [
            child
            for child in archive_root.iterdir()
            if child.is_dir()
            and (
                child.name == "archived_tickets"
                or child.name == "contextspace"
                or child.name.startswith("archived_runs")
                or child.name.startswith("flow_state")
            )
        ]
        has_tickets = has_tickets or any(
            child.name == "archived_tickets" for child in artifact_children
        )
        has_runs = has_runs or any(
            child.name.startswith("archived_runs") for child in artifact_children
        )
        has_contextspace = has_contextspace or any(
            child.name == "contextspace" for child in artifact_children
        )
        has_flow_state = has_flow_state or any(
            child.name.startswith("flow_state") for child in artifact_children
        )

    mtime_candidates = []
    for child in artifact_children:
        try:
            mtime_candidates.append(child.stat().st_mtime)
        except OSError:
            continue
    archived_at = None
    if mtime_candidates:
        archived_at = datetime.fromtimestamp(
            max(mtime_candidates), tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        archive_path = archive_root.relative_to(workspace_root).as_posix()
    except ValueError:
        archive_path = str(archive_root)
    return {
        "run_id": normalized_run_id,
        "archive_path": archive_path,
        "archived_at": archived_at,
        "has_tickets": has_tickets,
        "has_runs": has_runs,
        "has_contextspace": has_contextspace,
        "has_flow_state": has_flow_state,
    }


def _format_flow_archive_completion_text(summary: dict[str, Any]) -> str:
    return (
        f"Archived run {summary['run_id']} "
        f"(tickets={summary['archived_tickets']}, "
        f"runs_archived={summary['archived_runs']}, "
        f"contextspace={summary['archived_contextspace']})."
    )


def build_flow_archive_confirmation_components(
    run_id: str,
    *,
    prompt_variant: bool,
) -> list[dict[str, Any]]:
    confirm_action = "archive_confirm_prompt" if prompt_variant else "archive_confirm"
    cancel_action = "archive_cancel_prompt" if prompt_variant else "archive_cancel"
    from .components import DISCORD_BUTTON_STYLE_DANGER

    return [
        build_action_row(
            [
                build_button(
                    "Archive now",
                    f"flow:{run_id}:{confirm_action}",
                    style=DISCORD_BUTTON_STYLE_DANGER,
                ),
                build_button("Cancel", f"flow:{run_id}:{cancel_action}"),
            ]
        )
    ]


def select_default_status_run(
    records: list[FlowRunRecord],
) -> Optional[FlowRunRecord]:
    if not records:
        return None
    return select_ticket_flow_run_record(records, selection="authoritative")


def build_historical_runs_picker(
    runs: list[FlowRunRecord],
) -> list[dict[str, Any]]:
    if not runs:
        return []
    run_tuples = [(run.id, run.status.value) for run in runs]
    return [
        build_flow_runs_picker(
            run_tuples,
            placeholder="Select a historical run...",
        )
    ]


def build_flow_status_components(
    record: FlowRunRecord,
    runs: list[FlowRunRecord],
) -> list[dict[str, Any]]:
    components = build_flow_status_buttons(
        record.id,
        record.status.value,
        include_refresh=True,
    )
    run_tuples = [(run.id, run.status.value) for run in runs]
    if len(run_tuples) > 1:
        components.append(
            build_flow_runs_picker(
                run_tuples,
                placeholder="Select another run...",
                current_run_id=record.id,
            )
        )
    return components


def format_flow_status_response_text(
    record: FlowRunRecord,
    snapshot: dict[str, Any],
) -> str:
    from ...core.flows.ux_helpers import format_ticket_flow_status_lines

    return "\n".join(format_ticket_flow_status_lines(record, snapshot))


def build_flow_status_message(
    *,
    record: FlowRunRecord,
    runs: list[FlowRunRecord],
    snapshot: dict[str, Any],
    prefix: Optional[str] = None,
) -> tuple[str, list[dict[str, Any]]]:
    response_text = format_flow_status_response_text(record, snapshot)
    if isinstance(prefix, str) and prefix.strip():
        response_text = f"{prefix.strip()}\n\n{response_text}"
    return response_text, build_flow_status_components(record, runs)


def build_flow_status_summary_fallback(
    workspace_root: Path,
) -> Optional[str]:
    progress = ticket_progress(workspace_root)
    if int(progress.get("total", 0)) <= 0:
        return None
    display = build_ticket_flow_display(
        status=None,
        done_count=int(progress.get("done", 0)),
        total_count=int(progress.get("total", 0)),
        run_id=None,
    )
    from typing import Mapping

    summary: Mapping[str, Any] = {**display, "current_step": None}
    lines = format_ticket_flow_summary_lines(summary)
    if not lines:
        return None
    return "\n".join(lines)


async def prompt_flow_action_picker(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    workspace_root: Path,
    action: str,
    deferred: bool = False,
) -> None:
    try:
        store = service._open_flow_store(workspace_root)
    except (sqlite3.Error, OSError, RuntimeError, ConfigError) as exc:
        raise DiscordTransientError(
            f"Failed to open flow database: {exc}",
            user_message="Unable to access flow database. Please try again later.",
        ) from None
    try:
        runs = store.list_flow_runs(flow_type="ticket_flow")
    except (sqlite3.Error, OSError) as exc:
        raise DiscordTransientError(
            f"Failed to query flow runs: {exc}",
            user_message="Unable to query flow database. Please try again later.",
        ) from None
    finally:
        store.close()

    filtered = [run for run in runs if _flow_run_matches_action(run, action)]
    if not filtered:
        await service.send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=f"No ticket_flow runs available for {flow_action_label(action)}.",
        )
        return
    run_tuples = [(record.id, record.status.value) for record in filtered]
    custom_id = f"{FLOW_ACTION_SELECT_PREFIX}:{action}"
    prompt = f"Select a run to {flow_action_label(action)}:"
    await service.send_or_respond_ephemeral_with_components(
        interaction_id=interaction_id,
        interaction_token=interaction_token,
        deferred=deferred,
        text=prompt,
        components=[
            build_flow_runs_picker(
                run_tuples,
                custom_id=custom_id,
                placeholder=f"Select run to {flow_action_label(action)}...",
            )
        ],
    )


async def resolve_flow_run_input(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    workspace_root: Path,
    action: str,
    run_id_opt: Any,
    deferred: bool = False,
) -> Optional[str]:
    if not (isinstance(run_id_opt, str) and run_id_opt.strip()):
        if action == "status":
            return ""
        await prompt_flow_action_picker(
            service,
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            action=action,
            deferred=deferred,
        )
        return None

    run_id_value = run_id_opt.strip()
    try:
        store = service._open_flow_store(workspace_root)
    except (sqlite3.Error, OSError, RuntimeError, ConfigError):
        return run_id_value
    try:
        runs = store.list_flow_runs(flow_type="ticket_flow")
    except (sqlite3.Error, OSError):
        return run_id_value
    finally:
        store.close()

    matching_runs = [run for run in runs if _flow_run_matches_action(run, action)]
    if not matching_runs:
        return run_id_value

    items = [(record.id, record.status.value) for record in matching_runs]
    search_items = [(record.id, record.id) for record in matching_runs]
    aliases = {record.id: (record.status.value,) for record in matching_runs}
    custom_id = f"{FLOW_ACTION_SELECT_PREFIX}:{action}"

    async def _prompt_run_matches(
        query_text: str,
        filtered_search_items: list[tuple[str, str]],
    ) -> None:
        status_by_run_id = {run_id: status for run_id, status in items}
        filtered_items = [
            (run_id, status_by_run_id.get(run_id, ""))
            for run_id, _label in filtered_search_items
        ]
        prompt = (
            f"Matched {len(filtered_items)} runs for `{query_text}`. "
            f"Select a run to {flow_action_label(action)}:"
        )
        await service.send_or_respond_ephemeral_with_components(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=prompt,
            components=[
                build_flow_runs_picker(
                    filtered_items,
                    custom_id=custom_id,
                    placeholder=f"Select run to {flow_action_label(action)}...",
                )
            ],
        )

    resolved_run_id = await service._resolve_picker_query_or_prompt(
        query=run_id_value,
        items=search_items,
        limit=DISCORD_SELECT_OPTION_MAX_OPTIONS,
        aliases=aliases,
        prompt_filtered_items=_prompt_run_matches,
    )
    if resolved_run_id is None:
        return None
    return str(resolved_run_id)


def write_user_reply(
    service: Any,
    workspace_root: Path,
    record: FlowRunRecord,
    text: str,
) -> Path:
    run_paths = resolve_outbox_paths(
        workspace_root=workspace_root,
        run_id=record.id,
    )
    try:
        run_paths.run_dir.mkdir(parents=True, exist_ok=True)
        reply_path = run_paths.run_dir / "USER_REPLY.md"
        reply_path.write_text(text.strip() + "\n", encoding="utf-8")
        return reply_path
    except OSError as exc:
        service._logger.error(
            "Failed to write user reply (run_id=%s, path=%s): %s",
            record.id,
            run_paths.run_dir,
            exc,
        )
        raise


async def handle_flow_status(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    workspace_root: Path,
    options: dict[str, Any],
    channel_id: Optional[str] = None,
    guild_id: Optional[str] = None,
    update_message: bool = False,
    prefix: Optional[str] = None,
) -> None:
    if update_message:
        deferred_component = await ensure_component_response_deferred(
            service,
            interaction_id,
            interaction_token,
        )
        deferred_public = False
    else:
        deferred_public = await ensure_public_response_deferred(
            service,
            interaction_id,
            interaction_token,
        )
        deferred_component = False
    run_id_opt = await resolve_flow_run_input(
        service,
        interaction_id,
        interaction_token,
        workspace_root=workspace_root,
        action="status",
        run_id_opt=options.get("run_id"),
        deferred=deferred_public,
    )
    if run_id_opt is None:
        return
    try:
        store = service._open_flow_store(workspace_root)
    except (sqlite3.Error, OSError, RuntimeError, ConfigError) as exc:
        log_event(
            service._logger,
            logging.ERROR,
            "discord.flow.store_open_failed",
            workspace_root=str(workspace_root),
            exc=exc,
        )
        raise DiscordTransientError(
            f"Failed to open flow database: {exc}",
            user_message="Unable to access flow database. Please try again later.",
        ) from None
    try:
        record: Optional[FlowRunRecord]
        runs: list[FlowRunRecord] = []
        if isinstance(run_id_opt, str) and run_id_opt.strip():
            try:
                record = service._resolve_flow_run_by_id(
                    store, run_id=run_id_opt.strip()
                )
                runs = store.list_flow_runs(flow_type="ticket_flow")
            except (sqlite3.Error, OSError) as exc:
                log_event(
                    service._logger,
                    logging.ERROR,
                    "discord.flow.query_failed",
                    exc=exc,
                    run_id=run_id_opt.strip(),
                )
                raise DiscordTransientError(
                    f"Failed to query flow run: {exc}",
                    user_message="Unable to query flow database. Please try again later.",
                ) from None
        else:
            try:
                runs = store.list_flow_runs(flow_type="ticket_flow")
            except (sqlite3.Error, OSError) as exc:
                log_event(
                    service._logger,
                    logging.ERROR,
                    "discord.flow.query_failed",
                    exc=exc,
                    workspace_root=str(workspace_root),
                )
                raise DiscordTransientError(
                    f"Failed to query flow runs: {exc}",
                    user_message="Unable to query flow database. Please try again later.",
                ) from None
            record = select_default_status_run(runs)
        explicit_run_requested = isinstance(run_id_opt, str) and bool(
            run_id_opt.strip()
        )
        if record is None:
            archived_summary = None
            if explicit_run_requested:
                archived_summary = _describe_archived_flow_run(
                    workspace_root, run_id_opt.strip()
                )
            if archived_summary is not None:
                archived_text = _format_archived_flow_status_text(
                    run_id_opt.strip(),
                    archived_summary,
                    prefix=prefix,
                )
                if update_message:
                    await update_runtime_component_message(
                        service,
                        interaction_id,
                        interaction_token,
                        archived_text,
                        components=[],
                    )
                else:
                    await service.send_or_respond_public_with_components(
                        interaction_id=interaction_id,
                        interaction_token=interaction_token,
                        deferred=deferred_public,
                        text=archived_text,
                        components=[],
                    )
                return
            if runs and not explicit_run_requested:
                content = (
                    "No ticket_flow run found.\n\n"
                    "Use the picker below to inspect historical runs."
                )
                components = build_historical_runs_picker(runs)
                if update_message:
                    await update_runtime_component_message(
                        service,
                        interaction_id,
                        interaction_token,
                        content,
                        components=components,
                    )
                else:
                    await service.send_or_respond_public_with_components(
                        interaction_id=interaction_id,
                        interaction_token=interaction_token,
                        deferred=deferred_public,
                        text=content,
                        components=components,
                    )
                return
            if not explicit_run_requested:
                summary_text = build_flow_status_summary_fallback(workspace_root)
                if summary_text:
                    if update_message:
                        await update_runtime_component_message(
                            service,
                            interaction_id,
                            interaction_token,
                            summary_text,
                            components=[],
                        )
                    else:
                        await service.send_or_respond_public_with_components(
                            interaction_id=interaction_id,
                            interaction_token=interaction_token,
                            deferred=deferred_public,
                            text=summary_text,
                            components=[],
                        )
                    return
            message = (
                f"Ticket_flow run {run_id_opt.strip()} not found."
                if explicit_run_requested
                else "No ticket_flow runs found."
            )
            await service.send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred_component or deferred_public,
                text=message,
            )
            return
        try:
            record, _updated, locked = reconcile_flow_run(workspace_root, record, store)
            if locked:
                await service.send_or_respond_ephemeral(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    deferred=deferred_component or deferred_public,
                    text=f"Run {record.id} is locked for reconcile; try again.",
                )
                return
        except (sqlite3.Error, OSError) as exc:
            log_event(
                service._logger,
                logging.ERROR,
                "discord.flow.reconcile_failed",
                exc=exc,
                run_id=record.id,
            )
            raise DiscordTransientError(
                f"Failed to reconcile flow run: {exc}",
                user_message="Unable to reconcile flow run. Please try again later.",
            ) from None
        try:
            snapshot = build_flow_status_snapshot(workspace_root, record, store)
        except (sqlite3.Error, OSError) as exc:
            log_event(
                service._logger,
                logging.ERROR,
                "discord.flow.snapshot_failed",
                exc=exc,
                run_id=record.id,
            )
            raise DiscordTransientError(
                f"Failed to build flow snapshot: {exc}",
                user_message="Unable to build flow snapshot. Please try again later.",
            ) from None
    finally:
        store.close()

    response_text, status_buttons = build_flow_status_message(
        record=record,
        runs=runs,
        snapshot=snapshot,
        prefix=prefix,
    )
    run_mirror = service._flow_run_mirror(workspace_root)
    run_mirror.mirror_inbound(
        run_id=record.id,
        platform="discord",
        event_type="flow_status_command",
        kind="command",
        actor="user",
        text="/flow status",
        chat_id=channel_id,
        thread_id=guild_id,
        message_id=interaction_id,
        meta={"interaction_token": interaction_token},
    )
    response_type = "component_update" if update_message else "channel"
    run_mirror.mirror_outbound(
        run_id=record.id,
        platform="discord",
        event_type="flow_status_notice",
        kind="notice",
        actor="car",
        text=response_text,
        chat_id=channel_id,
        thread_id=guild_id,
        meta={"response_type": response_type},
    )
    if status_buttons:
        if update_message:
            await update_runtime_component_message(
                service,
                interaction_id,
                interaction_token,
                response_text,
                components=status_buttons,
            )
        else:
            await service.send_or_respond_public_with_components(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred_public,
                text=response_text,
                components=status_buttons,
            )
    else:
        if update_message:
            await update_runtime_component_message(
                service,
                interaction_id,
                interaction_token,
                response_text,
                components=[],
            )
        else:
            await service.send_or_respond_public(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred_public,
                text=response_text,
            )


async def handle_flow_runs(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    workspace_root: Path,
    options: dict[str, Any],
) -> None:
    deferred = interaction_response_deferred(service, interaction_token)
    raw_limit = options.get("limit")
    limit = FLOW_RUNS_DEFAULT_LIMIT
    if isinstance(raw_limit, int):
        limit = raw_limit
    limit = max(1, min(limit, FLOW_RUNS_MAX_LIMIT))

    try:
        store = service._open_flow_store(workspace_root)
    except (sqlite3.Error, OSError, RuntimeError, ConfigError) as exc:
        log_event(
            service._logger,
            logging.ERROR,
            "discord.flow.store_open_failed",
            workspace_root=str(workspace_root),
            exc=exc,
        )
        raise DiscordTransientError(
            f"Failed to open flow database: {exc}",
            user_message="Unable to access flow database. Please try again later.",
        ) from None
    try:
        try:
            runs = store.list_flow_runs(flow_type="ticket_flow")[:limit]
        except (sqlite3.Error, OSError) as exc:
            log_event(
                service._logger,
                logging.ERROR,
                "discord.flow.query_failed",
                exc=exc,
                workspace_root=str(workspace_root),
            )
            raise DiscordTransientError(
                f"Failed to query flow runs: {exc}",
                user_message="Unable to query flow database. Please try again later.",
            ) from None
    finally:
        store.close()

    if not runs:
        await service.send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text="No ticket_flow runs found.",
        )
        return

    run_tuples = [(record.id, record.status.value) for record in runs]
    components = [build_flow_runs_picker(run_tuples)]
    lines = [f"Recent ticket_flow runs (limit={limit}):"]
    for record in runs:
        lines.append(f"- {record.id} [{record.status.value}]")
    await service.send_or_respond_ephemeral_with_components(
        interaction_id=interaction_id,
        interaction_token=interaction_token,
        deferred=deferred,
        text="\n".join(lines),
        components=components,
    )


async def handle_flow_issue(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    workspace_root: Path,
    options: dict[str, Any],
    channel_id: Optional[str] = None,
    guild_id: Optional[str] = None,
) -> None:
    _ = channel_id, guild_id
    issue_ref = options.get("issue_ref")
    if not isinstance(issue_ref, str) or not issue_ref.strip():
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "Provide an issue reference: `/flow issue issue_ref:<issue#|url>`",
        )
        return
    issue_ref = issue_ref.strip()
    deferred = interaction_response_deferred(service, interaction_token)
    try:

        def _seed_issue() -> Any:
            seeded = seed_issue_from_github(
                workspace_root,
                issue_ref,
                github_service_factory=lambda repo_root: cast(
                    GitHubServiceProtocol, GitHubService(repo_root)
                ),
            )
            atomic_write(issue_md_path(workspace_root), seeded.content)
            return seeded

        seed = await asyncio.to_thread(
            _seed_issue,
        )
    except GitHubError as exc:
        await service.send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=f"GitHub error: {exc}",
        )
        return
    except RuntimeError as exc:
        await service.send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=str(exc),
        )
        return
    except (OSError, ValueError, ConnectionError) as exc:
        await service.send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=f"Failed to fetch issue: {exc}",
        )
        return
    await service.send_or_respond_ephemeral(
        interaction_id=interaction_id,
        interaction_token=interaction_token,
        deferred=deferred,
        text=f"Seeded ISSUE.md from GitHub issue {seed.issue_number}.",
    )


async def handle_flow_plan(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    workspace_root: Path,
    options: dict[str, Any],
    channel_id: Optional[str] = None,
    guild_id: Optional[str] = None,
) -> None:
    _ = channel_id, guild_id
    plan_text = options.get("text")
    if not isinstance(plan_text, str) or not plan_text.strip():
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "Provide a plan: `/flow plan text:<plan>`",
        )
        return
    content = seed_issue_from_text(plan_text.strip())
    atomic_write(issue_md_path(workspace_root), content)
    await service.respond_ephemeral(
        interaction_id,
        interaction_token,
        "Seeded ISSUE.md from your plan.",
    )


async def handle_flow_start(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    workspace_root: Path,
    options: dict[str, Any],
    deferred_public: Optional[bool] = None,
    component_response: bool = False,
) -> None:
    force_new = bool(options.get("force_new"))
    restart_from = options.get("restart_from")
    flow_service = service._ticket_flow_orchestration_service(workspace_root)
    if deferred_public is None:
        deferred_public = await ensure_public_response_deferred(
            service,
            interaction_id,
            interaction_token,
        )

    try:
        store = service._open_flow_store(workspace_root)
    except (sqlite3.Error, OSError, RuntimeError, ConfigError) as exc:
        log_event(
            service._logger,
            logging.ERROR,
            "discord.flow.store_open_failed",
            workspace_root=str(workspace_root),
            exc=exc,
        )
        raise DiscordTransientError(
            f"Failed to open flow database: {exc}",
            user_message="Unable to access flow database. Please try again later.",
        ) from None
    try:
        try:
            runs = store.list_flow_runs(flow_type="ticket_flow")
        except (sqlite3.Error, OSError) as exc:
            log_event(
                service._logger,
                logging.ERROR,
                "discord.flow.query_failed",
                exc=exc,
                workspace_root=str(workspace_root),
            )
            raise DiscordTransientError(
                f"Failed to query flow runs: {exc}",
                user_message="Unable to query flow database. Please try again later.",
            ) from None
    finally:
        store.close()

    if not force_new:
        active_or_paused = next(
            (
                record
                for record in runs
                if record.status in {FlowRunStatus.RUNNING, FlowRunStatus.PAUSED}
            ),
            None,
        )
        if active_or_paused is not None:
            flow_service.ensure_flow_run_worker(
                active_or_paused.id,
                is_terminal=active_or_paused.status.is_terminal(),
            )
            try:
                store = service._open_flow_store(workspace_root)
            except (sqlite3.Error, OSError, RuntimeError, ConfigError) as exc:
                log_event(
                    service._logger,
                    logging.ERROR,
                    "discord.flow.store_open_failed",
                    workspace_root=str(workspace_root),
                    exc=exc,
                )
                raise DiscordTransientError(
                    f"Failed to open flow database: {exc}",
                    user_message="Unable to access flow database. Please try again later.",
                ) from None
            try:
                try:
                    record = service._resolve_flow_run_by_id(
                        store, run_id=active_or_paused.id
                    )
                    runs = store.list_flow_runs(flow_type="ticket_flow")
                except (sqlite3.Error, OSError) as exc:
                    log_event(
                        service._logger,
                        logging.ERROR,
                        "discord.flow.query_failed",
                        exc=exc,
                        run_id=active_or_paused.id,
                    )
                    raise DiscordTransientError(
                        f"Failed to query flow run: {exc}",
                        user_message="Unable to query flow database. Please try again later.",
                    ) from None
                if record is None:
                    await _send_flow_public_response(
                        service,
                        interaction_id,
                        interaction_token,
                        deferred=deferred_public,
                        component_response=component_response,
                        text=(
                            "Reusing ticket_flow run "
                            f"{active_or_paused.id} ({active_or_paused.status.value})."
                        ),
                    )
                    return
                try:
                    record, _updated, locked = reconcile_flow_run(
                        workspace_root, record, store
                    )
                    if locked:
                        await _send_flow_ephemeral_response(
                            service,
                            interaction_id,
                            interaction_token,
                            deferred=deferred_public,
                            component_response=component_response,
                            text=f"Run {record.id} is locked for reconcile; try again.",
                        )
                        return
                except (sqlite3.Error, OSError) as exc:
                    log_event(
                        service._logger,
                        logging.ERROR,
                        "discord.flow.reconcile_failed",
                        exc=exc,
                        run_id=record.id,
                    )
                    raise DiscordTransientError(
                        f"Failed to reconcile flow run: {exc}",
                        user_message="Unable to reconcile flow run. Please try again later.",
                    ) from None
                try:
                    snapshot = build_flow_status_snapshot(workspace_root, record, store)
                except (sqlite3.Error, OSError) as exc:
                    log_event(
                        service._logger,
                        logging.ERROR,
                        "discord.flow.snapshot_failed",
                        exc=exc,
                        run_id=record.id,
                    )
                    raise DiscordTransientError(
                        f"Failed to build flow snapshot: {exc}",
                        user_message="Unable to build flow snapshot. Please try again later.",
                    ) from None
            finally:
                store.close()
            response_text, status_buttons = build_flow_status_message(
                record=record,
                runs=runs,
                snapshot=snapshot,
                prefix=(
                    f"Reusing ticket_flow run {record.id} ({record.status.value})."
                ),
            )
            await _send_flow_public_response(
                service,
                interaction_id,
                interaction_token,
                deferred=deferred_public,
                component_response=component_response,
                text=response_text,
                components=status_buttons or [],
            )
            return

    metadata: dict[str, Any] = {"origin": "discord", "force_new": force_new}
    if isinstance(restart_from, str) and restart_from.strip():
        metadata["restart_from"] = restart_from.strip()

    try:
        started = await flow_service.start_flow_run(
            "ticket_flow",
            input_data={},
            metadata=metadata,
        )
    except ValueError as exc:
        await _send_flow_ephemeral_response(
            service,
            interaction_id,
            interaction_token,
            deferred=deferred_public,
            component_response=component_response,
            text=str(exc),
        )
        return

    try:
        store = service._open_flow_store(workspace_root)
    except (sqlite3.Error, OSError, RuntimeError, ConfigError) as exc:
        log_event(
            service._logger,
            logging.ERROR,
            "discord.flow.store_open_failed",
            workspace_root=str(workspace_root),
            exc=exc,
        )
        raise DiscordTransientError(
            f"Failed to open flow database: {exc}",
            user_message="Unable to access flow database. Please try again later.",
        ) from None
    try:
        try:
            record = service._resolve_flow_run_by_id(store, run_id=started.run_id)
            runs = store.list_flow_runs(flow_type="ticket_flow")
        except (sqlite3.Error, OSError) as exc:
            log_event(
                service._logger,
                logging.ERROR,
                "discord.flow.query_failed",
                exc=exc,
                run_id=started.run_id,
            )
            raise DiscordTransientError(
                f"Failed to query flow run: {exc}",
                user_message="Unable to query flow database. Please try again later.",
            ) from None
        if record is None:
            prefix = "Started new" if force_new else "Started"
            await _send_flow_public_response(
                service,
                interaction_id,
                interaction_token,
                deferred=deferred_public,
                component_response=component_response,
                text=f"{prefix} ticket_flow run {started.run_id}.",
            )
            return
        try:
            record, _updated, locked = reconcile_flow_run(workspace_root, record, store)
            if locked:
                await _send_flow_ephemeral_response(
                    service,
                    interaction_id,
                    interaction_token,
                    deferred=deferred_public,
                    component_response=component_response,
                    text=f"Run {record.id} is locked for reconcile; try again.",
                )
                return
        except (sqlite3.Error, OSError) as exc:
            log_event(
                service._logger,
                logging.ERROR,
                "discord.flow.reconcile_failed",
                exc=exc,
                run_id=record.id,
            )
            raise DiscordTransientError(
                f"Failed to reconcile flow run: {exc}",
                user_message="Unable to reconcile flow run. Please try again later.",
            ) from None
        try:
            snapshot = build_flow_status_snapshot(workspace_root, record, store)
        except (sqlite3.Error, OSError) as exc:
            log_event(
                service._logger,
                logging.ERROR,
                "discord.flow.snapshot_failed",
                exc=exc,
                run_id=record.id,
            )
            raise DiscordTransientError(
                f"Failed to build flow snapshot: {exc}",
                user_message="Unable to build flow snapshot. Please try again later.",
            ) from None
    finally:
        store.close()

    prefix = "Started new" if force_new else "Started"
    response_text, status_buttons = build_flow_status_message(
        record=record,
        runs=runs,
        snapshot=snapshot,
        prefix=f"{prefix} ticket_flow run {record.id}.",
    )
    await _send_flow_public_response(
        service,
        interaction_id,
        interaction_token,
        deferred=deferred_public,
        component_response=component_response,
        text=response_text,
        components=status_buttons or [],
    )


async def handle_flow_restart(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    workspace_root: Path,
    options: dict[str, Any],
    deferred_public: Optional[bool] = None,
    component_response: bool = False,
) -> None:
    if deferred_public is None:
        deferred_public = await ensure_public_response_deferred(
            service,
            interaction_id,
            interaction_token,
        )
    run_id_opt = await resolve_flow_run_input(
        service,
        interaction_id,
        interaction_token,
        workspace_root=workspace_root,
        action="restart",
        run_id_opt=options.get("run_id"),
        deferred=deferred_public,
    )
    if run_id_opt is None:
        return
    try:
        store = service._open_flow_store(workspace_root)
    except (sqlite3.Error, OSError, RuntimeError, ConfigError) as exc:
        log_event(
            service._logger,
            logging.ERROR,
            "discord.flow.store_open_failed",
            workspace_root=str(workspace_root),
            exc=exc,
        )
        raise DiscordTransientError(
            f"Failed to open flow database: {exc}",
            user_message="Unable to access flow database. Please try again later.",
        ) from None
    try:
        target: Optional[FlowRunRecord] = None
        if isinstance(run_id_opt, str) and run_id_opt.strip():
            try:
                target = service._resolve_flow_run_by_id(
                    store, run_id=run_id_opt.strip()
                )
            except (sqlite3.Error, OSError) as exc:
                log_event(
                    service._logger,
                    logging.ERROR,
                    "discord.flow.query_failed",
                    exc=exc,
                    run_id=run_id_opt.strip(),
                )
                raise DiscordTransientError(
                    f"Failed to query flow run: {exc}",
                    user_message="Unable to query flow database. Please try again later.",
                ) from None
        else:
            try:
                runs = store.list_flow_runs(flow_type="ticket_flow")
            except (sqlite3.Error, OSError) as exc:
                log_event(
                    service._logger,
                    logging.ERROR,
                    "discord.flow.query_failed",
                    exc=exc,
                    workspace_root=str(workspace_root),
                )
                raise DiscordTransientError(
                    f"Failed to query flow runs: {exc}",
                    user_message="Unable to query flow database. Please try again later.",
                ) from None
            target = select_ticket_flow_run_record(runs, selection="active")
    finally:
        store.close()

    if isinstance(run_id_opt, str) and run_id_opt.strip() and target is None:
        await _send_flow_ephemeral_response(
            service,
            interaction_id,
            interaction_token,
            deferred=deferred_public,
            component_response=component_response,
            text=f"No ticket_flow run found for run_id {run_id_opt.strip()}.",
        )
        return

    flow_service = service._ticket_flow_orchestration_service(workspace_root)
    if target is not None and target.status.is_active():
        try:
            await flow_service.stop_flow_run(target.id)
        except ValueError as exc:
            await _send_flow_ephemeral_response(
                service,
                interaction_id,
                interaction_token,
                deferred=deferred_public,
                component_response=component_response,
                text=str(exc),
            )
            return
        latest = await flow_service.wait_for_flow_run_terminal(target.id)
        if latest is None or latest.status not in {
            "completed",
            "stopped",
            "failed",
        }:
            status_value = latest.status if latest is not None else "unknown"
            await _send_flow_public_response(
                service,
                interaction_id,
                interaction_token,
                deferred=deferred_public,
                component_response=component_response,
                text=(
                    f"Run {target.id} is still active ({status_value}); "
                    "restart aborted to avoid concurrent workers. Try again after it stops."
                ),
            )
            return

    await handle_flow_start(
        service,
        interaction_id,
        interaction_token,
        workspace_root=workspace_root,
        options={
            "force_new": True,
            "restart_from": target.id if target is not None else None,
        },
        deferred_public=deferred_public,
        component_response=component_response,
    )


async def handle_flow_recover(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    workspace_root: Path,
    options: dict[str, Any],
) -> None:
    deferred = await ensure_public_response_deferred(
        service,
        interaction_id,
        interaction_token,
    )
    run_id_opt = await resolve_flow_run_input(
        service,
        interaction_id,
        interaction_token,
        workspace_root=workspace_root,
        action="recover",
        run_id_opt=options.get("run_id"),
        deferred=deferred,
    )
    if run_id_opt is None:
        return
    try:
        store = service._open_flow_store(workspace_root)
    except (sqlite3.Error, OSError, RuntimeError, ConfigError) as exc:
        log_event(
            service._logger,
            logging.ERROR,
            "discord.flow.store_open_failed",
            workspace_root=str(workspace_root),
            exc=exc,
        )
        raise DiscordTransientError(
            f"Failed to open flow database: {exc}",
            user_message="Unable to access flow database. Please try again later.",
        ) from None
    try:
        target: Optional[FlowRunRecord] = None
        if isinstance(run_id_opt, str) and run_id_opt.strip():
            try:
                target = service._resolve_flow_run_by_id(
                    store, run_id=run_id_opt.strip()
                )
            except (sqlite3.Error, OSError) as exc:
                log_event(
                    service._logger,
                    logging.ERROR,
                    "discord.flow.query_failed",
                    exc=exc,
                    run_id=run_id_opt.strip(),
                )
                raise DiscordTransientError(
                    f"Failed to query flow run: {exc}",
                    user_message="Unable to query flow database. Please try again later.",
                ) from None
        else:
            try:
                runs = store.list_flow_runs(flow_type="ticket_flow")
            except (sqlite3.Error, OSError) as exc:
                log_event(
                    service._logger,
                    logging.ERROR,
                    "discord.flow.query_failed",
                    exc=exc,
                    workspace_root=str(workspace_root),
                )
                raise DiscordTransientError(
                    f"Failed to query flow runs: {exc}",
                    user_message="Unable to query flow database. Please try again later.",
                ) from None
            target = select_ticket_flow_run_record(runs, selection="active")

        if target is None:
            await service.send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text="No active ticket_flow run found.",
            )
            return

        flow_service = service._ticket_flow_orchestration_service(workspace_root)
        target_run, updated, locked = flow_service.reconcile_flow_run(target.id)
        if locked:
            await service.send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=f"Run {target_run.run_id} is locked for reconcile; try again.",
            )
            return
        verdict = "Recovered" if updated else "No changes needed"
        await service.send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=f"{verdict} for run {target_run.run_id} ({target_run.status}).",
        )
    finally:
        store.close()


async def handle_flow_resume(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    workspace_root: Path,
    options: dict[str, Any],
    channel_id: Optional[str] = None,
    guild_id: Optional[str] = None,
    component_response: bool = False,
) -> None:
    deferred = await ensure_public_response_deferred(
        service,
        interaction_id,
        interaction_token,
    )
    run_id_opt = await resolve_flow_run_input(
        service,
        interaction_id,
        interaction_token,
        workspace_root=workspace_root,
        action="resume",
        run_id_opt=options.get("run_id"),
        deferred=deferred,
    )
    if run_id_opt is None:
        return
    try:
        store = service._open_flow_store(workspace_root)
    except (sqlite3.Error, OSError, RuntimeError, ConfigError) as exc:
        log_event(
            service._logger,
            logging.ERROR,
            "discord.flow.store_open_failed",
            workspace_root=str(workspace_root),
            exc=exc,
        )
        raise DiscordTransientError(
            f"Failed to open flow database: {exc}",
            user_message="Unable to access flow database. Please try again later.",
        ) from None
    try:
        if isinstance(run_id_opt, str) and run_id_opt.strip():
            try:
                target = service._resolve_flow_run_by_id(
                    store, run_id=run_id_opt.strip()
                )
            except (sqlite3.Error, OSError) as exc:
                log_event(
                    service._logger,
                    logging.ERROR,
                    "discord.flow.query_failed",
                    exc=exc,
                    run_id=run_id_opt.strip(),
                )
                raise DiscordTransientError(
                    f"Failed to query flow run: {exc}",
                    user_message="Unable to query flow database. Please try again later.",
                ) from None
        else:
            try:
                runs = store.list_flow_runs(flow_type="ticket_flow")
            except (sqlite3.Error, OSError) as exc:
                log_event(
                    service._logger,
                    logging.ERROR,
                    "discord.flow.query_failed",
                    exc=exc,
                    workspace_root=str(workspace_root),
                )
                raise DiscordTransientError(
                    f"Failed to query flow runs: {exc}",
                    user_message="Unable to query flow database. Please try again later.",
                ) from None
            target = select_ticket_flow_run_record(runs, selection="paused")
    finally:
        store.close()

    if target is None:
        await _send_flow_ephemeral_response(
            service,
            interaction_id,
            interaction_token,
            deferred=deferred,
            component_response=component_response,
            text="No paused ticket_flow run found to resume.",
        )
        return

    run_mirror = service._flow_run_mirror(workspace_root)
    run_mirror.mirror_inbound(
        run_id=target.id,
        platform="discord",
        event_type="flow_resume_command",
        kind="command",
        actor="user",
        text="/flow resume",
        chat_id=channel_id,
        thread_id=guild_id,
        message_id=interaction_id,
    )
    flow_service = service._ticket_flow_orchestration_service(workspace_root)
    try:
        updated = await flow_service.resume_flow_run(target.id)
    except (KeyError, ValueError) as exc:
        await _send_flow_ephemeral_response(
            service,
            interaction_id,
            interaction_token,
            deferred=deferred,
            component_response=component_response,
            text=str(exc),
        )
        return

    outbound_text = f"Resumed run {updated.run_id}."
    if component_response:
        await handle_flow_status(
            service,
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            options={"run_id": updated.run_id},
            channel_id=channel_id,
            guild_id=guild_id,
            update_message=True,
            prefix=outbound_text,
        )
    else:
        await service.send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=outbound_text,
        )
    run_mirror.mirror_outbound(
        run_id=updated.run_id,
        platform="discord",
        event_type="flow_resume_notice",
        kind="notice",
        actor="car",
        text=outbound_text,
        chat_id=channel_id,
        thread_id=guild_id,
    )


async def handle_flow_stop(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    workspace_root: Path,
    options: dict[str, Any],
    channel_id: Optional[str] = None,
    guild_id: Optional[str] = None,
    component_response: bool = False,
) -> None:
    deferred = await ensure_public_response_deferred(
        service,
        interaction_id,
        interaction_token,
    )
    run_id_opt = await resolve_flow_run_input(
        service,
        interaction_id,
        interaction_token,
        workspace_root=workspace_root,
        action="stop",
        run_id_opt=options.get("run_id"),
        deferred=deferred,
    )
    if run_id_opt is None:
        return
    try:
        store = service._open_flow_store(workspace_root)
    except (sqlite3.Error, OSError, RuntimeError, ConfigError) as exc:
        log_event(
            service._logger,
            logging.ERROR,
            "discord.flow.store_open_failed",
            workspace_root=str(workspace_root),
            exc=exc,
        )
        raise DiscordTransientError(
            f"Failed to open flow database: {exc}",
            user_message="Unable to access flow database. Please try again later.",
        ) from None
    try:
        if isinstance(run_id_opt, str) and run_id_opt.strip():
            try:
                target = service._resolve_flow_run_by_id(
                    store, run_id=run_id_opt.strip()
                )
            except (sqlite3.Error, OSError) as exc:
                log_event(
                    service._logger,
                    logging.ERROR,
                    "discord.flow.query_failed",
                    exc=exc,
                    run_id=run_id_opt.strip(),
                )
                raise DiscordTransientError(
                    f"Failed to query flow run: {exc}",
                    user_message="Unable to query flow database. Please try again later.",
                ) from None
        else:
            try:
                runs = store.list_flow_runs(flow_type="ticket_flow")
            except (sqlite3.Error, OSError) as exc:
                log_event(
                    service._logger,
                    logging.ERROR,
                    "discord.flow.query_failed",
                    exc=exc,
                    workspace_root=str(workspace_root),
                )
                raise DiscordTransientError(
                    f"Failed to query flow runs: {exc}",
                    user_message="Unable to query flow database. Please try again later.",
                ) from None
            target = select_ticket_flow_run_record(runs, selection="non_terminal")
    finally:
        store.close()

    if target is None:
        await _send_flow_ephemeral_response(
            service,
            interaction_id,
            interaction_token,
            deferred=deferred,
            component_response=component_response,
            text="No active ticket_flow run found to stop.",
        )
        return

    run_mirror = service._flow_run_mirror(workspace_root)
    run_mirror.mirror_inbound(
        run_id=target.id,
        platform="discord",
        event_type="flow_stop_command",
        kind="command",
        actor="user",
        text="/flow stop",
        chat_id=channel_id,
        thread_id=guild_id,
        message_id=interaction_id,
    )
    flow_service = service._ticket_flow_orchestration_service(workspace_root)
    try:
        updated = await flow_service.stop_flow_run(target.id)
    except (KeyError, ValueError) as exc:
        await _send_flow_ephemeral_response(
            service,
            interaction_id,
            interaction_token,
            deferred=deferred,
            component_response=component_response,
            text=str(exc),
        )
        return

    outbound_text = f"Stop requested for run {updated.run_id} ({updated.status})."
    if component_response:
        await handle_flow_status(
            service,
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            options={"run_id": updated.run_id},
            channel_id=channel_id,
            guild_id=guild_id,
            update_message=True,
            prefix=outbound_text,
        )
    else:
        await service.send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=outbound_text,
        )
    run_mirror.mirror_outbound(
        run_id=updated.run_id,
        platform="discord",
        event_type="flow_stop_notice",
        kind="notice",
        actor="car",
        text=outbound_text,
        chat_id=channel_id,
        thread_id=guild_id,
        meta={"status": updated.status},
    )


async def handle_flow_archive(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    workspace_root: Path,
    options: dict[str, Any],
    channel_id: Optional[str] = None,
    guild_id: Optional[str] = None,
) -> None:
    deferred = interaction_response_deferred(service, interaction_token)
    confirmed = bool(options.get("confirmed"))
    run_id_opt = options.get("run_id")
    if isinstance(run_id_opt, str) and run_id_opt.strip():
        run_id_opt = await resolve_flow_run_input(
            service,
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            action="archive",
            run_id_opt=run_id_opt,
        )
        if run_id_opt is None:
            return
    else:
        run_id_opt = ""
    try:
        store = service._open_flow_store(workspace_root)
    except (sqlite3.Error, OSError, RuntimeError, ConfigError) as exc:
        log_event(
            service._logger,
            logging.ERROR,
            "discord.flow.store_open_failed",
            workspace_root=str(workspace_root),
            exc=exc,
        )
        raise DiscordTransientError(
            f"Failed to open flow database: {exc}",
            user_message="Unable to access flow database. Please try again later.",
        ) from None
    try:
        if isinstance(run_id_opt, str) and run_id_opt.strip():
            try:
                target = service._resolve_flow_run_by_id(
                    store, run_id=run_id_opt.strip()
                )
            except (sqlite3.Error, OSError) as exc:
                log_event(
                    service._logger,
                    logging.ERROR,
                    "discord.flow.query_failed",
                    exc=exc,
                    run_id=run_id_opt.strip(),
                )
                raise DiscordTransientError(
                    f"Failed to query flow run: {exc}",
                    user_message="Unable to query flow database. Please try again later.",
                ) from None
        else:
            try:
                target = select_default_ticket_flow_run(store)
            except (sqlite3.Error, OSError) as exc:
                log_event(
                    service._logger,
                    logging.ERROR,
                    "discord.flow.query_failed",
                    exc=exc,
                    workspace_root=str(workspace_root),
                )
                raise DiscordTransientError(
                    f"Failed to query flow runs: {exc}",
                    user_message="Unable to query flow database. Please try again later.",
                ) from None
    finally:
        store.close()

    if target is None:
        await service.send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text="No ticket_flow run found to archive.",
        )
        return

    archive_mode = resolve_ticket_flow_archive_mode(target)
    if archive_mode == "blocked":
        await service.send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=(
                f"Run {target.id} is {target.status.value}. "
                "Stop or pause it before archiving."
            ),
        )
        return

    if archive_mode == "confirm" and not confirmed:
        await service.send_or_respond_ephemeral_with_components(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=flow_archive_prompt_text(target),
            components=build_flow_archive_confirmation_components(
                target.id,
                prompt_variant=True,
            ),
        )
        return

    run_mirror = service._flow_run_mirror(workspace_root)
    run_mirror.mirror_inbound(
        run_id=target.id,
        platform="discord",
        event_type="flow_archive_command",
        kind="command",
        actor="user",
        text="/flow archive",
        chat_id=channel_id,
        thread_id=guild_id,
        message_id=interaction_id,
    )
    flow_service = service._ticket_flow_orchestration_service(workspace_root)
    try:
        summary = await asyncio.to_thread(
            flow_service.archive_flow_run,
            target.id,
            force=ticket_flow_archive_requires_force(target),
            delete_run=True,
        )
    except KeyError:
        await service.send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=(
                f"Run {target.id} no longer exists. "
                "Use /flow status or /flow runs to inspect historical runs."
            ),
        )
        return
    except ValueError as exc:
        await service.send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=str(exc),
        )
        return

    outbound_text = _format_flow_archive_completion_text(summary)
    await service.send_or_respond_ephemeral(
        interaction_id=interaction_id,
        interaction_token=interaction_token,
        deferred=deferred,
        text=outbound_text,
    )


async def handle_flow_reply(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    workspace_root: Path,
    options: dict[str, Any],
    channel_id: Optional[str] = None,
    guild_id: Optional[str] = None,
    user_id: Optional[str] = None,
    component_response: bool = False,
) -> None:
    text = options.get("text")
    if not isinstance(text, str) or not text.strip():
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "Missing required option: text",
        )
        return

    deferred = await ensure_public_response_deferred(
        service,
        interaction_id,
        interaction_token,
    )
    run_id_opt = options.get("run_id")
    if not (isinstance(run_id_opt, str) and run_id_opt.strip()) and channel_id:
        pending_key = service._pending_interaction_scope_key(
            channel_id=channel_id,
            user_id=user_id,
        )
        service._pending_flow_reply_text[pending_key] = text.strip()
        await prompt_flow_action_picker(
            service,
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            action="reply",
            deferred=deferred,
        )
        return
    if isinstance(run_id_opt, str) and run_id_opt.strip():
        run_id_opt = await resolve_flow_run_input(
            service,
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            action="reply",
            run_id_opt=run_id_opt,
            deferred=deferred,
        )
        if run_id_opt is None:
            return
    try:
        store = service._open_flow_store(workspace_root)
    except (sqlite3.Error, OSError, RuntimeError, ConfigError) as exc:
        log_event(
            service._logger,
            logging.ERROR,
            "discord.flow.store_open_failed",
            workspace_root=str(workspace_root),
            exc=exc,
        )
        raise DiscordTransientError(
            f"Failed to open flow database: {exc}",
            user_message="Unable to access flow database. Please try again later.",
        ) from None
    try:
        if isinstance(run_id_opt, str) and run_id_opt.strip():
            try:
                target = service._resolve_flow_run_by_id(
                    store, run_id=run_id_opt.strip()
                )
            except (sqlite3.Error, OSError) as exc:
                log_event(
                    service._logger,
                    logging.ERROR,
                    "discord.flow.query_failed",
                    exc=exc,
                    run_id=run_id_opt.strip(),
                )
                raise DiscordTransientError(
                    f"Failed to query flow run: {exc}",
                    user_message="Unable to query flow database. Please try again later.",
                ) from None
            if target and target.status != FlowRunStatus.PAUSED:
                target = None
        else:
            try:
                runs = store.list_flow_runs(flow_type="ticket_flow")
            except (sqlite3.Error, OSError) as exc:
                log_event(
                    service._logger,
                    logging.ERROR,
                    "discord.flow.query_failed",
                    exc=exc,
                    workspace_root=str(workspace_root),
                )
                raise DiscordTransientError(
                    f"Failed to query flow runs: {exc}",
                    user_message="Unable to query flow database. Please try again later.",
                ) from None
            target = next(
                (record for record in runs if record.status == FlowRunStatus.PAUSED),
                None,
            )
    finally:
        store.close()

    if target is None:
        await _send_flow_ephemeral_response(
            service,
            interaction_id,
            interaction_token,
            deferred=deferred,
            component_response=component_response,
            text="No paused ticket_flow run found for reply.",
        )
        return

    run_mirror = service._flow_run_mirror(workspace_root)
    run_mirror.mirror_inbound(
        run_id=target.id,
        platform="discord",
        event_type="flow_reply_command",
        kind="command",
        actor="user",
        text=text,
        chat_id=channel_id,
        thread_id=guild_id,
        message_id=interaction_id,
    )
    reply_path = write_user_reply(service, workspace_root, target, text)

    flow_service = service._ticket_flow_orchestration_service(workspace_root)
    try:
        updated = await flow_service.resume_flow_run(target.id)
    except ValueError as exc:
        await _send_flow_ephemeral_response(
            service,
            interaction_id,
            interaction_token,
            deferred=deferred,
            component_response=component_response,
            text=str(exc),
        )
        return

    outbound_text = (
        f"Reply saved to {reply_path.name} and resumed run {updated.run_id}."
    )
    await _send_flow_ephemeral_response(
        service,
        interaction_id,
        interaction_token,
        deferred=deferred,
        component_response=component_response,
        text=outbound_text,
    )
    run_mirror.mirror_outbound(
        run_id=updated.run_id,
        platform="discord",
        event_type="flow_reply_notice",
        kind="notice",
        actor="car",
        text=outbound_text,
        chat_id=channel_id,
        thread_id=guild_id,
    )


async def handle_flow_button(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    workspace_root: Path,
    custom_id: str,
    channel_id: Optional[str] = None,
    guild_id: Optional[str] = None,
) -> None:
    parts = custom_id.split(":")
    if len(parts) < 3:
        await send_runtime_ephemeral(
            service,
            interaction_id,
            interaction_token,
            f"Invalid button action: {custom_id}",
        )
        return

    run_id = parts[1]
    action = parts[2]
    if action == "resume":
        await defer_and_update_runtime_component_message(
            service,
            interaction_id,
            interaction_token,
            flow_resume_in_progress_text(run_id),
            components=[],
        )
        await handle_flow_resume(
            service,
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            options={"run_id": run_id},
            channel_id=channel_id,
            guild_id=guild_id,
            component_response=True,
        )
    elif action == "stop":
        await defer_and_update_runtime_component_message(
            service,
            interaction_id,
            interaction_token,
            flow_stop_in_progress_text(run_id),
            components=[],
        )
        await handle_flow_stop(
            service,
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            options={"run_id": run_id},
            channel_id=channel_id,
            guild_id=guild_id,
            component_response=True,
        )
    elif action in {
        "archive",
        "archive_confirm",
        "archive_cancel",
        "archive_confirm_prompt",
        "archive_cancel_prompt",
    }:
        flow_service = service._ticket_flow_orchestration_service(workspace_root)
        deferred = await ensure_component_response_deferred(
            service,
            interaction_id,
            interaction_token,
        )
        if action == "archive_cancel":
            await handle_flow_status(
                service,
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
                options={"run_id": run_id},
                channel_id=channel_id,
                guild_id=guild_id,
                update_message=True,
            )
            return
        if action == "archive_cancel_prompt":
            await update_runtime_component_message(
                service,
                interaction_id,
                interaction_token,
                "Archive cancelled.",
                components=[],
            )
            return

        def _resolve_archive_target() -> Optional[FlowRunRecord]:
            try:
                store = service._open_flow_store(workspace_root)
            except (sqlite3.Error, OSError, RuntimeError, ConfigError) as exc:
                log_event(
                    service._logger,
                    logging.ERROR,
                    "discord.flow.store_open_failed",
                    workspace_root=str(workspace_root),
                    exc=exc,
                )
                raise DiscordTransientError(
                    f"Failed to open flow database: {exc}",
                    user_message="Unable to access flow database. Please try again later.",
                ) from None
            try:
                try:
                    return cast(
                        Optional[FlowRunRecord],
                        service._resolve_flow_run_by_id(store, run_id=run_id),
                    )
                except (sqlite3.Error, OSError) as exc:
                    log_event(
                        service._logger,
                        logging.ERROR,
                        "discord.flow.query_failed",
                        exc=exc,
                        run_id=run_id,
                    )
                    raise DiscordTransientError(
                        f"Failed to query flow run: {exc}",
                        user_message=(
                            "Unable to query flow database. Please try again later."
                        ),
                    ) from None
            finally:
                store.close()

        target = await asyncio.to_thread(_resolve_archive_target)

        if target is None:
            stale_text = (
                f"Run {run_id} no longer exists. "
                "Use /flow status or /flow runs to inspect historical runs."
            )
            await service.send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=stale_text,
            )
            return

        archive_mode = resolve_ticket_flow_archive_mode(target)
        blocked_text = f"Run {target.id} is {target.status.value}. Stop or pause it before archiving."
        if archive_mode == "blocked":
            if action.endswith("_prompt"):
                await update_runtime_component_message(
                    service,
                    interaction_id,
                    interaction_token,
                    blocked_text,
                    components=[],
                )
            else:
                await service.send_or_respond_ephemeral(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    deferred=deferred,
                    text=blocked_text,
                )
            return

        if action == "archive" and archive_mode == "confirm":
            await update_runtime_component_message(
                service,
                interaction_id,
                interaction_token,
                flow_archive_prompt_text(target),
                components=build_flow_archive_confirmation_components(
                    target.id,
                    prompt_variant=False,
                ),
            )
            return

        await send_runtime_ephemeral(
            service,
            interaction_id,
            interaction_token,
            flow_archive_in_progress_text(target.id),
        )
        try:
            summary = await asyncio.to_thread(
                flow_service.archive_flow_run,
                run_id,
                force=ticket_flow_archive_requires_force(target),
                delete_run=True,
            )
        except KeyError:
            stale_text = (
                f"Run {run_id} no longer exists. "
                "Use /flow status or /flow runs to inspect historical runs."
            )
            await service.send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=stale_text,
            )
            return
        except ValueError as exc:
            await service.send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=str(exc),
            )
            return

        archive_prefix = _format_flow_archive_completion_text(summary)
        try:
            await handle_flow_status(
                service,
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
                options={"run_id": summary["run_id"]},
                channel_id=channel_id,
                guild_id=guild_id,
                update_message=True,
                prefix=archive_prefix,
            )
        except DiscordTransientError as exc:
            archived_summary = _archived_flow_status_summary_from_archive_result(
                workspace_root,
                summary,
            )
            if archived_summary is None:
                raise
            log_event(
                service._logger,
                logging.WARNING,
                "discord.flow.archive.status_refresh_failed",
                run_id=summary["run_id"],
                workspace_root=str(workspace_root),
                exc=exc,
            )
            await update_runtime_component_message(
                service,
                interaction_id,
                interaction_token,
                _format_archived_flow_status_text(
                    summary["run_id"],
                    archived_summary,
                    prefix=archive_prefix,
                ),
                components=[],
            )
        return
    elif action == "restart":
        await defer_and_update_runtime_component_message(
            service,
            interaction_id,
            interaction_token,
            flow_restart_in_progress_text(run_id),
            components=[],
        )
        await handle_flow_restart(
            service,
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            options={"run_id": run_id},
            deferred_public=await ensure_component_response_deferred(
                service,
                interaction_id,
                interaction_token,
            ),
            component_response=True,
        )
    elif action == "refresh":
        await defer_and_update_runtime_component_message(
            service,
            interaction_id,
            interaction_token,
            flow_refresh_in_progress_text(run_id),
            components=[],
        )
        await handle_flow_status(
            service,
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            options={"run_id": run_id},
            channel_id=channel_id,
            guild_id=guild_id,
            update_message=True,
        )
    else:
        await send_runtime_ephemeral(
            service,
            interaction_id,
            interaction_token,
            f"Unknown action: {action}",
        )


TICKET_PICKER_TOKEN_PREFIX = "ticket@"


def _ticket_dir(workspace_root: Path) -> Path:
    return workspace_root / ".codex-autorunner" / "tickets"


def _ticket_prompt_text(*, search_query: str = "") -> str:
    normalized_query = search_query.strip()
    if not normalized_query:
        return "Select a ticket to view or edit."
    return f"Select a ticket to view or edit. Search: `{normalized_query}`"


def _ticket_picker_value(service: Any, ticket_rel: str) -> str:
    normalized = ticket_rel.strip()
    if len(normalized) <= 100:
        return normalized
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"{TICKET_PICKER_TOKEN_PREFIX}{digest}"


def _list_ticket_choices(
    service: Any,
    workspace_root: Path,
    *,
    status_filter: str,
    search_query: str = "",
) -> list[tuple[str, str, str]]:
    from ...integrations.chat.picker_filter import filter_picker_items
    from ...tickets.files import (
        list_ticket_paths,
        read_ticket_frontmatter,
        safe_relpath,
    )

    ticket_dir_path = _ticket_dir(workspace_root)
    choices: list[tuple[str, str, str]] = []
    normalized_filter = status_filter.strip().lower()
    if normalized_filter not in {"all", "open", "done"}:
        normalized_filter = "all"
    for path in list_ticket_paths(ticket_dir_path):
        frontmatter, errors = read_ticket_frontmatter(path)
        is_done = bool(frontmatter and frontmatter.done and not errors)
        if normalized_filter == "open" and is_done:
            continue
        if normalized_filter == "done" and not is_done:
            continue
        title = frontmatter.title if frontmatter and frontmatter.title else ""
        label = f"{path.name}{' - ' + title if title else ''}"
        description = "done" if is_done else "open"
        rel_path = safe_relpath(path, workspace_root)
        choices.append((rel_path, label, description))
    normalized_query = search_query.strip()
    if not normalized_query or not choices:
        return choices
    search_items = [
        (value, f"{label} {description}".strip())
        for value, label, description in choices
    ]
    filtered_items = filter_picker_items(
        search_items,
        normalized_query,
        limit=len(search_items),
    )
    choice_by_value = {
        value: (value, label, description) for value, label, description in choices
    }
    return [
        choice_by_value[value]
        for value, _label in filtered_items
        if value in choice_by_value
    ]


def _resolve_ticket_picker_value(
    service: Any,
    selected_value: str,
    *,
    workspace_root: Path,
) -> Optional[str]:
    from ...tickets.files import list_ticket_paths, safe_relpath

    normalized = selected_value.strip()
    if not normalized:
        return None
    if not normalized.startswith(TICKET_PICKER_TOKEN_PREFIX):
        return normalized

    digest = normalized[len(TICKET_PICKER_TOKEN_PREFIX) :]
    if not digest:
        return None

    for path in list_ticket_paths(_ticket_dir(workspace_root)):
        rel_path = safe_relpath(path, workspace_root)
        candidate = _ticket_picker_value(service, rel_path)
        if candidate == normalized:
            return rel_path
    return None


def _build_ticket_components(
    service: Any,
    workspace_root: Path,
    *,
    status_filter: str,
    search_query: str = "",
) -> list[dict[str, Any]]:
    from .components import build_ticket_filter_picker, build_ticket_picker

    ticket_choices = _list_ticket_choices(
        service,
        workspace_root,
        status_filter=status_filter,
        search_query=search_query,
    )
    return [
        build_ticket_filter_picker(current_filter=status_filter),
        build_ticket_picker(
            [
                (_ticket_picker_value(service, value), label, description)
                for value, label, description in ticket_choices
            ]
        ),
    ]


async def handle_ticket_filter_component(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    values: Optional[list[str]],
) -> None:
    if not values:
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "Please select a filter and try again.",
        )
        return
    deferred = await ensure_component_response_deferred(
        service,
        interaction_id,
        interaction_token,
    )
    if not deferred:
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "Discord interaction acknowledgement failed. Please retry.",
        )
        return
    workspace_root = await service._require_bound_workspace(
        interaction_id, interaction_token, channel_id=channel_id
    )
    if not workspace_root:
        return
    status_filter = values[0].strip().lower()
    if status_filter not in {"all", "open", "done"}:
        status_filter = "all"
    search_query = service._pending_ticket_search_queries.get(channel_id, "")
    service._pending_ticket_filters[channel_id] = status_filter
    await update_runtime_component_message(
        service,
        interaction_id,
        interaction_token,
        _ticket_prompt_text(search_query=search_query),
        components=_build_ticket_components(
            service,
            workspace_root,
            status_filter=status_filter,
            search_query=search_query,
        ),
    )


async def _open_ticket_modal(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    workspace_root: Path,
    ticket_rel: str,
) -> None:
    from .effects import DiscordModalEffect, InteractionSessionKind
    from .interaction_registry import TICKETS_MODAL_PREFIX
    from .service import TICKETS_BODY_INPUT_ID

    ticket_dir_path = _ticket_dir(workspace_root).resolve()
    candidate = (workspace_root / ticket_rel).resolve()
    try:
        candidate.relative_to(ticket_dir_path)
    except ValueError:
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "Ticket path is invalid. Re-open the ticket list and try again.",
        )
        return
    if not candidate.exists() or not candidate.is_file():
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "Ticket file not found. Re-open the ticket list and try again.",
        )
        return
    try:
        ticket_text = await asyncio.wait_for(
            asyncio.to_thread(candidate.read_text, encoding="utf-8"),
            timeout=1.5,
        )
    except asyncio.TimeoutError:
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            (
                "Ticket load timed out before opening the modal. "
                "Try again or edit the file directly."
            ),
        )
        return
    except Exception as exc:
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            f"Failed to read ticket: {exc}",
        )
        return
    max_len = 4000
    if len(ticket_text) > max_len:
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            (
                f"`{ticket_rel}` is too large to edit in a Discord modal "
                f"({len(ticket_text)} characters; limit {max_len}). "
                "Use the web UI or edit the file directly."
            ),
        )
        return

    token = uuid.uuid4().hex[:12]
    service._pending_ticket_context[token] = {
        "workspace_root": str(workspace_root),
        "ticket_rel": ticket_rel,
    }

    title = "Edit ticket"
    await service._apply_discord_effect(
        interaction_id=interaction_id,
        interaction_token=interaction_token,
        effect=DiscordModalEffect(
            kind=InteractionSessionKind.COMPONENT,
            custom_id=f"{TICKETS_MODAL_PREFIX}:{token}",
            title=title,
            components=[
                {
                    "type": 18,
                    "label": "Ticket"[:45],
                    "component": {
                        "type": 4,
                        "custom_id": TICKETS_BODY_INPUT_ID,
                        "style": 2,
                        "value": ticket_text[:4000],
                        "required": True,
                        "max_length": 4000,
                    },
                },
            ],
        ),
    )


async def handle_ticket_select_component(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    values: Optional[list[str]],
) -> None:
    if not values:
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "Please select a ticket and try again.",
        )
        return
    if values[0] == "none":
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "No tickets available for this filter.",
        )
        return
    workspace_root = await service._require_bound_workspace(
        interaction_id, interaction_token, channel_id=channel_id
    )
    if not workspace_root:
        return
    ticket_rel = _resolve_ticket_picker_value(
        service,
        values[0],
        workspace_root=workspace_root,
    )
    if not ticket_rel:
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "Ticket selection is invalid. Re-open the ticket list and try again.",
        )
        return
    await _open_ticket_modal(
        service,
        interaction_id,
        interaction_token,
        workspace_root=workspace_root,
        ticket_rel=ticket_rel,
    )


async def handle_ticket_modal_submit(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    custom_id: str,
    values: dict[str, Any],
) -> None:
    from ...tickets.files import safe_relpath
    from .interaction_registry import TICKETS_MODAL_PREFIX
    from .service import TICKETS_BODY_INPUT_ID

    if not custom_id.startswith(f"{TICKETS_MODAL_PREFIX}:"):
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "Unknown modal submission.",
        )
        return

    token = custom_id.split(":", 1)[1].strip()
    context = service._pending_ticket_context.pop(token, None)
    if not context:
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "This ticket modal has expired. Re-open it and try again.",
        )
        return

    ticket_rel = context.get("ticket_rel")
    if not isinstance(ticket_rel, str) or not ticket_rel or ticket_rel == "none":
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "This ticket selection expired. Re-run `/car tickets` and choose one.",
        )
        return

    ticket_body_raw = values.get(TICKETS_BODY_INPUT_ID)
    ticket_body = ticket_body_raw if isinstance(ticket_body_raw, str) else None
    if ticket_body is None:
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "Ticket content is missing. Please try again.",
        )
        return

    workspace_root = Path(context.get("workspace_root", "")).expanduser()
    ticket_dir_path = _ticket_dir(workspace_root).resolve()
    candidate = (workspace_root / ticket_rel).resolve()
    try:
        candidate.relative_to(ticket_dir_path)
    except ValueError:
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "Ticket path is invalid. Re-open the ticket and try again.",
        )
        return

    try:
        candidate.write_text(ticket_body, encoding="utf-8")
    except (OSError, ValueError, TypeError) as exc:
        log_event(
            _logger,
            logging.WARNING,
            "discord.ticket.write_failed",
            path=str(candidate),
            exc=exc,
        )
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            f"Failed to write ticket: {exc}",
        )
        return

    await service.respond_ephemeral(
        interaction_id,
        interaction_token,
        f"Saved {safe_relpath(candidate, workspace_root)}.",
    )
