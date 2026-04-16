from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Optional

from ...core.chat_bindings import (
    preferred_non_pma_chat_notification_source_for_workspace,
    preferred_non_pma_chat_notification_sources_by_workspace,
)
from ...core.config import ConfigError, load_hub_config, load_repo_config
from ...core.diagnostics.loop_attribution import track_loop
from ...core.flows import (
    FlowRunRecord,
    FlowRunStatus,
    FlowStore,
    list_unseen_ticket_flow_dispatches,
)
from ...core.logging_utils import log_event
from ...core.utils import canonicalize_path
from ..chat.pause_notifications import (
    format_pause_notification_source,
    format_pause_notification_text,
)
from .rendering import chunk_discord_message
from .state import OutboxRecord

PAUSE_SCAN_INTERVAL_SECONDS = 5.0
TERMINAL_SCAN_INTERVAL_SECONDS = 5.0


def _truncate_error(error_message: Optional[str], limit: int = 200) -> str:
    if not error_message:
        return "Unknown error"
    normalized = " ".join(error_message.split())
    if len(normalized) > limit:
        return f"{normalized[: limit - 3]}..."
    return normalized


def _load_latest_terminal_ticket_flow_run(
    service: Any,
    workspace_root: Path,
) -> Optional[tuple[str, str, Optional[str]]]:
    db_path = workspace_root / ".codex-autorunner" / "flows.db"
    if not db_path.exists():
        return None
    config = load_repo_config(workspace_root)
    terminal_statuses = (
        FlowRunStatus.COMPLETED,
        FlowRunStatus.FAILED,
        FlowRunStatus.STOPPED,
    )
    latest_run: Optional[FlowRunRecord] = None
    with FlowStore(db_path, durable=config.durable_writes) as store:
        for status in terminal_statuses:
            runs = store.list_flow_runs(flow_type="ticket_flow", status=status)
            for run in runs:
                if latest_run is None:
                    latest_run = run
                elif run.finished_at and latest_run.finished_at:
                    if run.finished_at > latest_run.finished_at:
                        latest_run = run
                elif run.created_at > latest_run.created_at:
                    latest_run = run
    if latest_run is None:
        return None
    return (latest_run.id, latest_run.status.value, latest_run.error_message)


def _format_terminal_notification(
    service: Any,
    *,
    run_id: str,
    status: str,
    error_message: Optional[str],
) -> str:
    if status == FlowRunStatus.COMPLETED.value:
        return f"Ticket flow completed successfully (run {run_id})."
    elif status == FlowRunStatus.FAILED.value:
        error_text = _truncate_error(error_message)
        return f"Ticket flow failed (run {run_id}). Error: {error_text}"
    elif status == FlowRunStatus.STOPPED.value:
        return f"Ticket flow stopped (run {run_id})."
    return f"Ticket flow ended (run {run_id}, status: {status})."


def _format_pause_notification_source(
    service: Any,
    *,
    workspace_root: Optional[Path],
    repo_id: Optional[str],
) -> str:
    return format_pause_notification_source(
        workspace_root=workspace_root,
        repo_id=repo_id,
        hub_root=service._config.root,
        manifest_path=service._manifest_path,
        logger=service._logger,
        debug_label="discord.pause_watch.manifest_label_failed",
    )


def _format_ticket_flow_dispatch_notification(
    service: Any,
    *,
    run_id: str,
    dispatch_seq: str,
    content: str,
    source: Optional[str],
    allow_resume_hint: bool,
    is_handoff: bool,
) -> str:
    if is_handoff:
        return format_pause_notification_text(
            run_id=run_id,
            dispatch_seq=dispatch_seq,
            content=content,
            source=source,
            resume_hint="`/flow resume`" if allow_resume_hint else "",
        )
    body = content.strip() or "(no dispatch message)"
    header_lines = [f"Ticket flow dispatch (run {run_id}). Dispatch #{dispatch_seq}:"]
    if isinstance(source, str) and source.strip():
        header_lines.append(f"Source: {source.strip()}")
    return "\n\n".join(("\n".join(header_lines), body))


def _preferred_bound_source_for_workspace(
    service: Any, workspace_root: Path
) -> str | None:
    raw_config = service._hub_raw_config_cache
    if raw_config is None:
        try:
            raw_config = load_hub_config(service._config.root).raw
        except (ConfigError, OSError, ValueError, TypeError):
            raw_config = {}
        service._hub_raw_config_cache = raw_config
    try:
        return preferred_non_pma_chat_notification_source_for_workspace(
            hub_root=service._config.root,
            raw_config=raw_config,
            workspace_root=workspace_root,
        )
    except (KeyError, ValueError, TypeError) as exc:
        log_event(
            service._logger,
            logging.WARNING,
            "discord.pause_watch.route_lookup_failed",
            exc=exc,
            workspace_root=str(workspace_root),
        )
        return None


def _preferred_bound_sources_by_workspace(service: Any) -> dict[str, str]:
    raw_config = service._hub_raw_config_cache
    if raw_config is None:
        try:
            raw_config = load_hub_config(service._config.root).raw
        except (ConfigError, OSError, ValueError, TypeError):
            raw_config = {}
        service._hub_raw_config_cache = raw_config
    try:
        return preferred_non_pma_chat_notification_sources_by_workspace(
            hub_root=service._config.root,
            raw_config=raw_config,
        )
    except (KeyError, ValueError, TypeError) as exc:
        log_event(
            service._logger,
            logging.WARNING,
            "discord.pause_watch.route_lookup_failed",
            exc=exc,
        )
        return {}


async def _scan_and_enqueue_pause_notifications(service: Any) -> None:
    bindings = await service._store.list_bindings()
    preferred_sources = _preferred_bound_sources_by_workspace(service)
    for binding in bindings:
        channel_id = binding.get("channel_id")
        workspace_raw = binding.get("workspace_path")
        if not isinstance(channel_id, str) or not isinstance(workspace_raw, str):
            continue
        workspace_root = canonicalize_path(Path(workspace_raw))
        preferred_source = preferred_sources.get(str(workspace_root))
        if preferred_source is None:
            preferred_source = _preferred_bound_source_for_workspace(
                service, workspace_root
            )
        if preferred_source == "telegram":
            continue
        run_mirror = service._flow_run_mirror(workspace_root)
        snapshots = await asyncio.to_thread(
            list_unseen_ticket_flow_dispatches,
            workspace_root,
            last_run_id=binding.get("last_dispatch_run_id"),
            last_dispatch_seq=binding.get("last_dispatch_seq"),
        )
        if not snapshots:
            continue

        for snapshot in snapshots:
            content = _format_ticket_flow_dispatch_notification(
                service,
                run_id=snapshot.run_id,
                dispatch_seq=snapshot.dispatch_seq,
                content=snapshot.dispatch_markdown,
                source=_format_pause_notification_source(
                    service,
                    workspace_root=workspace_root,
                    repo_id=binding.get("repo_id"),
                ),
                allow_resume_hint=snapshot.allow_resume_hint,
                is_handoff=snapshot.is_handoff,
            )
            chunks = chunk_discord_message(
                content,
                max_len=service._config.max_message_length,
                with_numbering=False,
            )
            if not chunks:
                chunks = ["(dispatch notification had no content)"]

            record_prefix = "pause" if snapshot.is_handoff else "dispatch"
            event_type = (
                "flow_pause_dispatch_notice"
                if snapshot.is_handoff
                else "flow_dispatch_notice"
            )
            enqueued = True
            for index, chunk in enumerate(chunks, start=1):
                record_id = (
                    f"{record_prefix}:{channel_id}:{snapshot.run_id}:"
                    f"{snapshot.dispatch_seq}:{index}"
                )
                try:
                    await service._store.enqueue_outbox(
                        OutboxRecord(
                            record_id=record_id,
                            channel_id=channel_id,
                            message_id=None,
                            operation="send",
                            payload_json={"content": chunk},
                        )
                    )
                    run_mirror.mirror_outbound(
                        run_id=snapshot.run_id,
                        platform="discord",
                        event_type=event_type,
                        kind="dispatch",
                        actor="car",
                        text=chunk,
                        chat_id=channel_id,
                        thread_id=binding.get("guild_id"),
                        message_id=record_id,
                        meta={
                            "dispatch_seq": snapshot.dispatch_seq,
                            "chunk_index": index,
                            "mode": snapshot.mode,
                        },
                    )
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    enqueued = False
                    log_event(
                        service._logger,
                        logging.WARNING,
                        "discord.dispatch_watch.enqueue_failed",
                        exc=exc,
                        channel_id=channel_id,
                        run_id=snapshot.run_id,
                        dispatch_seq=snapshot.dispatch_seq,
                        mode=snapshot.mode,
                    )
                    break

            if not enqueued:
                break

            await service._store.mark_dispatch_seen(
                channel_id=channel_id,
                run_id=snapshot.run_id,
                dispatch_seq=snapshot.dispatch_seq,
            )
            if snapshot.is_handoff:
                await service._store.mark_pause_dispatch_seen(
                    channel_id=channel_id,
                    run_id=snapshot.run_id,
                    dispatch_seq=snapshot.dispatch_seq,
                )
            log_event(
                service._logger,
                logging.INFO,
                "discord.dispatch_watch.notified",
                channel_id=channel_id,
                run_id=snapshot.run_id,
                dispatch_seq=snapshot.dispatch_seq,
                mode=snapshot.mode,
                chunk_count=len(chunks),
            )


async def _scan_and_enqueue_terminal_notifications(service: Any) -> None:
    bindings = await service._store.list_bindings()
    for binding in bindings:
        channel_id = binding.get("channel_id")
        workspace_raw = binding.get("workspace_path")
        if not isinstance(channel_id, str) or not isinstance(workspace_raw, str):
            continue
        workspace_root = canonicalize_path(Path(workspace_raw))
        terminal_run = await asyncio.to_thread(
            _load_latest_terminal_ticket_flow_run,
            service,
            workspace_root,
        )
        if terminal_run is None:
            continue
        run_id, status, error_message = terminal_run
        if binding.get("last_terminal_run_id") == run_id:
            continue
        message = _format_terminal_notification(
            service,
            run_id=run_id,
            status=status,
            error_message=error_message,
        )
        record_id = f"terminal:{channel_id}:{run_id}"
        try:
            await service._store.enqueue_outbox(
                OutboxRecord(
                    record_id=record_id,
                    channel_id=channel_id,
                    message_id=None,
                    operation="send",
                    payload_json={"content": message},
                )
            )
            run_mirror = service._flow_run_mirror(workspace_root)
            run_mirror.mirror_outbound(
                run_id=run_id,
                platform="discord",
                event_type="flow_terminal_notice",
                kind="notification",
                actor="car",
                text=message,
                chat_id=channel_id,
                message_id=record_id,
                meta={"status": status},
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log_event(
                service._logger,
                logging.WARNING,
                "discord.terminal_watch.enqueue_failed",
                exc=exc,
                channel_id=channel_id,
                run_id=run_id,
            )
            continue
        await service._store.mark_terminal_run_seen(
            channel_id=channel_id, run_id=run_id
        )
        log_event(
            service._logger,
            logging.INFO,
            "discord.terminal_watch.notified",
            channel_id=channel_id,
            run_id=run_id,
            status=status,
        )


async def watch_ticket_flow_pauses(service: Any) -> None:
    while True:
        with track_loop("discord.flow_watchers.pause_scan") as scope:
            scope.record_db_read(1)
            try:
                await _scan_and_enqueue_pause_notifications(service)
                if await service._store.list_bindings():
                    scope.mark_productive()
            except Exception as exc:  # intentional: supervisor loop must never die
                log_event(
                    service._logger,
                    logging.WARNING,
                    "discord.pause_watch.scan_failed",
                    exc=exc,
                )
        await asyncio.sleep(PAUSE_SCAN_INTERVAL_SECONDS)


async def watch_ticket_flow_terminals(service: Any) -> None:
    while True:
        with track_loop("discord.flow_watchers.terminal_scan") as scope:
            scope.record_db_read(1)
            try:
                await _scan_and_enqueue_terminal_notifications(service)
                if await service._store.list_bindings():
                    scope.mark_productive()
            except Exception as exc:  # intentional: supervisor loop must never die
                log_event(
                    service._logger,
                    logging.WARNING,
                    "discord.terminal_watch.scan_failed",
                    exc=exc,
                )
        await asyncio.sleep(TERMINAL_SCAN_INTERVAL_SECONDS)
