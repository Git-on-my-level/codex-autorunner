from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from ...core.chat_bindings import (
    preferred_non_pma_chat_notification_source_for_workspace,
    preferred_non_pma_chat_notification_sources_by_workspace,
)
from ...core.config import ConfigError, load_hub_config, load_repo_config
from ...core.flows import FlowStore
from ...core.flows.models import FlowRunRecord, FlowRunStatus
from ...core.flows.pause_dispatch import load_latest_paused_ticket_flow_dispatch
from ...core.logging_utils import log_event
from ...core.runtime_services import RuntimeServices
from ...core.utils import canonicalize_path
from ...flows.ticket_flow.runtime_helpers import (
    build_ticket_flow_controller,
    spawn_ticket_flow_worker,
)
from ...manifest import load_manifest
from ..chat.pause_notifications import (
    format_pause_notification_source,
    format_pause_notification_text,
)
from ..chat.run_mirror import ChatRunMirror
from .adapter import chunk_message
from .constants import TELEGRAM_MAX_MESSAGE_LENGTH
from .state import parse_topic_key


class TelegramTicketFlowBridge:
    """Encapsulate ticket_flow pause/resume plumbing for Telegram service."""

    _LEGACY_STALE_TERMINAL_FALLBACK_SECONDS = 30.0

    def __init__(
        self,
        *,
        logger: logging.Logger,
        store,
        pause_targets: dict[str, str],
        send_message_with_outbox,
        send_document: Callable[..., Awaitable[bool]],
        pause_config,
        default_notification_chat_id: Optional[int],
        hub_root: Optional[Path] = None,
        manifest_path: Optional[Path] = None,
        config_root: Optional[Path] = None,
        runtime_services: Optional[RuntimeServices] = None,
        hub_raw_config: Optional[dict[str, Any]] = None,
    ) -> None:
        self._logger = logger
        self._store = store
        self._pause_targets = pause_targets
        self._send_message_with_outbox = send_message_with_outbox
        self._send_document = send_document
        self._pause_config = pause_config
        self._default_notification_chat_id = default_notification_chat_id
        self._hub_root = hub_root
        self._manifest_path = manifest_path
        self._config_root = config_root
        self._runtime_services = runtime_services
        self._hub_raw_config = hub_raw_config
        self._last_default_notification: dict[Path, str] = {}
        self._scan_failure_markers: dict[tuple[str, Path], str] = {}

    @staticmethod
    def _select_ticket_flow_topic(
        entries: list[tuple[str, object]],
    ) -> Optional[tuple[str, object]]:
        if not entries:
            return None

        def score(entry: tuple[str, object]) -> tuple[int, float, str]:
            key, record = entry
            thread_id = None
            try:
                _chat_id, thread_id, _scope = parse_topic_key(key)
            except (ValueError, TypeError):
                thread_id = None
            active_raw = getattr(record, "active_thread_id", None)
            try:
                active_thread = int(active_raw) if active_raw is not None else None
            except (TypeError, ValueError):
                active_thread = None
            active_match = (
                int(thread_id) == active_thread if thread_id is not None else False
            )
            last_active_at = getattr(record, "last_active_at", None)
            last_active = TelegramTicketFlowBridge._parse_last_active(last_active_at)
            return (1 if active_match else 0, last_active, key)

        return max(entries, key=score)

    @staticmethod
    def _parse_last_active(raw: Optional[str]) -> float:
        if not isinstance(raw, str):
            return float("-inf")
        try:
            parsed = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
            return parsed.timestamp()
        except ValueError:
            return float("-inf")

    @staticmethod
    def _parse_iso_timestamp(raw: Optional[str]) -> Optional[float]:
        if not isinstance(raw, str):
            return None
        text = raw.strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None

    def _should_notify_terminal(
        self,
        *,
        record: object,
        run_id: str,
        status: str,
        terminal_at: Optional[str],
    ) -> bool:
        last_run_id = getattr(record, "last_terminal_run_id", None)
        if last_run_id == run_id:
            return False

        current_ts = self._parse_iso_timestamp(terminal_at)
        if current_ts is None:
            return True

        seen_ts = self._parse_iso_timestamp(
            getattr(record, "last_terminal_finished_at", None)
        )
        if seen_ts is not None and current_ts < seen_ts:
            return False

        # Backward compatibility for existing topic records that only have run_id:
        # suppress stale terminal notices that predate the topic's last activity.
        if (
            seen_ts is None
            and status == FlowRunStatus.STOPPED.value
            and isinstance(last_run_id, str)
            and last_run_id.strip()
        ):
            last_active_ts = self._parse_last_active(
                getattr(record, "last_active_at", None)
            )
            if (
                current_ts
                <= last_active_ts - self._LEGACY_STALE_TERMINAL_FALLBACK_SECONDS
            ):
                return False

        return True

    async def watch_ticket_flow_pauses(self, interval_seconds: float) -> None:
        interval = max(interval_seconds, 1.0)
        while True:
            try:
                await self._scan_and_notify_pauses()
            except Exception as exc:  # intentional: top-level error handler
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.ticket_flow.watch_failed",
                    exc=exc,
                )
            await asyncio.sleep(interval)

    async def _scan_and_notify_pauses(self) -> None:
        if not self._pause_config.enabled:
            return
        topics = await self._store.list_topics()
        workspace_topics = self._get_all_workspaces(topics or {})
        preferred_sources = self._preferred_bound_sources_by_workspace()

        tasks = []
        for workspace_root, entries in workspace_topics.items():
            if entries:
                tasks.append(
                    asyncio.create_task(
                        self._notify_ticket_flow_pause(
                            workspace_root,
                            entries,
                            preferred_source=preferred_sources.get(str(workspace_root)),
                        )
                    )
                )
            else:
                tasks.append(
                    asyncio.create_task(
                        self._notify_via_default_chat(
                            workspace_root,
                            preferred_source=preferred_sources.get(str(workspace_root)),
                        )
                    )
                )
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _notify_ticket_flow_pause(
        self,
        workspace_root: Path,
        entries: list[tuple[str, object]],
        *,
        preferred_source: Optional[str] = None,
    ) -> None:
        if preferred_source is None:
            preferred_source = self._preferred_bound_source_for_workspace(
                workspace_root
            )
        if preferred_source == "discord":
            return
        try:
            pause = await asyncio.to_thread(
                self._load_ticket_flow_pause, workspace_root
            )
        except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.ticket_flow.scan_failed",
                exc=exc,
                workspace_root=str(workspace_root),
            )
            await self._notify_scan_failure(
                workspace_root,
                entries,
                error=exc,
                failure_kind="pause",
            )
            return
        self._clear_scan_failure_marker("pause", workspace_root)
        if pause is None:
            return
        run_id, seq, content, archived_dir = pause[:4]
        allow_resume_hint = pause[4] if len(pause) > 4 else True
        marker = f"{run_id}:{seq}"
        pending = [
            (key, record)
            for key, record in entries
            if getattr(record, "last_ticket_dispatch_seq", None) != marker
        ]
        if not pending:
            return
        primary = self._select_ticket_flow_topic(pending)
        if not primary:
            return
        updates: list[tuple[str, Optional[str]]] = [
            (key, getattr(record, "last_ticket_dispatch_seq", None))
            for key, record in pending
        ]
        for key, _previous in updates:
            await self._store.update_topic(
                key, self._set_ticket_dispatch_marker(marker)
            )

        primary_key, primary_record = primary
        try:
            chat_id, thread_id, _scope = parse_topic_key(primary_key)
        except (ValueError, TypeError) as exc:
            self._logger.debug("Failed to parse topic key: %s", exc)
            for key, previous in updates:
                await self._store.update_topic(
                    key, self._set_ticket_dispatch_marker(previous)
                )
            return

        try:
            await self._send_full_dispatch(
                chat_id,
                thread_id,
                run_id=run_id,
                seq=seq,
                content=content,
                archived_dir=archived_dir,
                workspace_root=workspace_root,
                repo_id=getattr(primary_record, "repo_id", None),
                allow_resume_hint=allow_resume_hint,
            )
            self._pause_targets[str(workspace_root)] = run_id
        except (RuntimeError, OSError, ConnectionError) as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.ticket_flow.notify_failed",
                exc=exc,
                topic_key=primary_key,
                run_id=run_id,
                seq=seq,
            )
            for key, previous in updates:
                await self._store.update_topic(
                    key, self._set_ticket_dispatch_marker(previous)
                )

    @staticmethod
    def _set_ticket_dispatch_marker(
        value: Optional[str],
    ):
        def apply(topic) -> None:
            topic.last_ticket_dispatch_seq = value

        return apply

    def _get_all_workspaces(
        self, topics: dict[str, object]
    ) -> dict[Path, list[tuple[str, object]]]:
        workspace_topics: dict[Path, list[tuple[str, object]]] = {}
        for key, record in topics.items():
            if not isinstance(record.workspace_path, str) or not record.workspace_path:
                continue
            workspace_root = canonicalize_path(Path(record.workspace_path))
            workspace_topics.setdefault(workspace_root, []).append((key, record))

        # Include config root
        if self._config_root:
            workspace_topics.setdefault(self._config_root.resolve(), [])

        # Include hub manifest worktrees (for web-originated flows)
        if self._hub_root and self._manifest_path and self._manifest_path.exists():
            try:
                manifest = load_manifest(self._manifest_path, self._hub_root)
                for repo in manifest.repos:
                    path = canonicalize_path((self._hub_root / repo.path).resolve())
                    workspace_topics.setdefault(path, [])
            except (OSError, ValueError) as exc:
                self._logger.debug(
                    "telegram.ticket_flow.manifest_load_failed", exc_info=exc
                )

        return workspace_topics

    def _load_ticket_flow_pause(
        self, workspace_root: Path
    ) -> Optional[tuple[str, str, str, Optional[Path], bool]]:
        snapshot = load_latest_paused_ticket_flow_dispatch(workspace_root)
        if snapshot is None:
            return None
        return (
            snapshot.run_id,
            snapshot.dispatch_seq,
            snapshot.dispatch_markdown,
            snapshot.dispatch_dir,
            snapshot.allow_resume_hint,
        )

    def get_paused_ticket_flow(
        self, workspace_root: Path, preferred_run_id: Optional[str] = None
    ) -> Optional[tuple[str, FlowRunRecord]]:
        db_path = workspace_root / ".codex-autorunner" / "flows.db"
        if not db_path.exists():
            return None
        try:
            config = load_repo_config(workspace_root)
            durable_writes = config.durable_writes
        except ConfigError:
            durable_writes = False
        store = FlowStore(db_path, durable=durable_writes)
        try:
            store.initialize()
            if preferred_run_id:
                preferred = store.get_flow_run(preferred_run_id)
                if preferred and preferred.status == FlowRunStatus.PAUSED:
                    return preferred.id, preferred
            runs = store.list_flow_runs(
                flow_type="ticket_flow", status=FlowRunStatus.PAUSED
            )
            if not runs:
                return None
            latest = runs[0]
            return latest.id, latest
        finally:
            store.close()

    async def auto_resume_run(self, workspace_root: Path, run_id: str) -> None:
        """Best-effort resume + worker spawn; failures are logged only."""
        try:
            if self._runtime_services is not None:
                controller = self._runtime_services.ensure_ticket_flow_controller(
                    workspace_root
                )
            else:
                controller = build_ticket_flow_controller(workspace_root)
            updated = await controller.resume_flow(run_id)
            if updated:
                spawn_ticket_flow_worker(workspace_root, updated.id, self._logger)
        except (ConfigError, OSError, RuntimeError, ConnectionError) as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.ticket_flow.auto_resume_failed",
                exc=exc,
                run_id=run_id,
                workspace_root=str(workspace_root),
            )

    async def _notify_via_default_chat(
        self,
        workspace_root: Path,
        *,
        preferred_source: Optional[str] = None,
    ) -> None:
        if not self._pause_config.enabled or self._default_notification_chat_id is None:
            return
        if preferred_source is None:
            preferred_source = self._preferred_bound_source_for_workspace(
                workspace_root
            )
        if preferred_source in {"telegram", "discord"}:
            return
        try:
            pause = await asyncio.to_thread(
                self._load_ticket_flow_pause, workspace_root
            )
        except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.ticket_flow.scan_failed",
                exc=exc,
                workspace_root=str(workspace_root),
            )
            await self._notify_scan_failure(
                workspace_root,
                [],
                error=exc,
                failure_kind="pause",
            )
            return
        self._clear_scan_failure_marker("pause", workspace_root)
        if pause is None:
            return
        run_id, seq, content, archived_dir = pause[:4]
        allow_resume_hint = pause[4] if len(pause) > 4 else True
        marker = f"{run_id}:{seq}"
        previous = self._last_default_notification.get(workspace_root)
        if previous == marker:
            return
        try:
            await self._send_full_dispatch(
                self._default_notification_chat_id,
                None,
                run_id=run_id,
                seq=seq,
                content=content,
                archived_dir=archived_dir,
                workspace_root=workspace_root,
                allow_resume_hint=allow_resume_hint,
            )
            self._last_default_notification[workspace_root] = marker
            self._pause_targets[str(workspace_root)] = run_id
        except (RuntimeError, OSError, ConnectionError) as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.ticket_flow.notify_default_failed",
                exc=exc,
                chat_id=self._default_notification_chat_id,
                run_id=run_id,
                seq=seq,
            )

    def _preferred_bound_source_for_workspace(self, workspace_root: Path) -> str | None:
        if self._hub_root is None:
            return None
        raw_config = self._hub_raw_config
        if raw_config is None:
            try:
                raw_config = load_hub_config(self._hub_root).raw
            except (ConfigError, OSError, RuntimeError, ConnectionError):
                raw_config = {}
            self._hub_raw_config = raw_config
        try:
            return preferred_non_pma_chat_notification_source_for_workspace(
                hub_root=self._hub_root,
                raw_config=raw_config,
                workspace_root=workspace_root,
            )
        except (ConfigError, OSError, RuntimeError, ConnectionError) as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.ticket_flow.route_lookup_failed",
                exc=exc,
                workspace_root=str(workspace_root),
            )
            return None

    def _preferred_bound_sources_by_workspace(self) -> dict[str, str]:
        if self._hub_root is None:
            return {}
        raw_config = self._hub_raw_config
        if raw_config is None:
            try:
                raw_config = load_hub_config(self._hub_root).raw
            except (ConfigError, OSError, RuntimeError, ConnectionError):
                raw_config = {}
            self._hub_raw_config = raw_config
        try:
            return preferred_non_pma_chat_notification_sources_by_workspace(
                hub_root=self._hub_root,
                raw_config=raw_config,
            )
        except (ConfigError, OSError, RuntimeError, ConnectionError) as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.ticket_flow.route_lookup_failed",
                exc=exc,
            )
            return {}

    async def _send_full_dispatch(
        self,
        chat_id: int,
        thread_id: Optional[int],
        *,
        run_id: str,
        seq: str,
        content: str,
        archived_dir: Optional[Path],
        workspace_root: Optional[Path],
        repo_id: Optional[str] = None,
        allow_resume_hint: bool = True,
    ) -> None:
        await self._send_dispatch_text(
            chat_id,
            thread_id,
            run_id=run_id,
            seq=seq,
            content=content,
            workspace_root=workspace_root,
            repo_id=repo_id,
            allow_resume_hint=allow_resume_hint,
        )
        if self._pause_config.send_attachments and archived_dir:
            await self._send_dispatch_attachments(
                chat_id,
                thread_id,
                run_id=run_id,
                seq=seq,
                archived_dir=archived_dir,
            )

    async def _send_dispatch_text(
        self,
        chat_id: int,
        thread_id: Optional[int],
        *,
        run_id: str,
        seq: str,
        content: str,
        workspace_root: Optional[Path],
        repo_id: Optional[str] = None,
        allow_resume_hint: bool = True,
    ) -> None:
        run_mirror = (
            ChatRunMirror(workspace_root, logger_=self._logger)
            if isinstance(workspace_root, Path)
            else None
        )
        source = self._format_dispatch_source(workspace_root, repo_id)
        full_text = format_pause_notification_text(
            run_id=run_id,
            dispatch_seq=seq,
            content=content,
            source=source,
            resume_hint="`/flow resume`" if allow_resume_hint else "",
        )

        if self._pause_config.chunk_long_messages:
            chunks = chunk_message(
                full_text,
                max_len=TELEGRAM_MAX_MESSAGE_LENGTH,
                with_numbering=False,
            )
        else:
            chunks = [full_text]

        for idx, chunk in enumerate(chunks):
            await self._send_message_with_outbox(
                chat_id,
                chunk,
                thread_id=thread_id,
                reply_to=None,
            )
            if run_mirror is not None:
                run_mirror.mirror_outbound(
                    run_id=run_id,
                    platform="telegram",
                    event_type="flow_pause_dispatch_notice",
                    kind="dispatch",
                    actor="car",
                    text=chunk,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    meta={"dispatch_seq": seq, "chunk_index": idx + 1},
                )
            if idx == 0:
                await asyncio.sleep(0)

    def _format_dispatch_source(
        self, workspace_root: Optional[Path], repo_id: Optional[str]
    ) -> str:
        return format_pause_notification_source(
            workspace_root=workspace_root,
            repo_id=repo_id,
            hub_root=self._hub_root,
            manifest_path=self._manifest_path,
            logger=self._logger,
            debug_label="telegram.ticket_flow.manifest_label_failed",
        )

    async def _send_dispatch_attachments(
        self,
        chat_id: int,
        thread_id: Optional[int],
        *,
        run_id: str,
        seq: str,
        archived_dir: Path,
    ) -> None:
        try:
            items = sorted(
                [
                    child
                    for child in archived_dir.iterdir()
                    if child.is_file()
                    and child.name != "DISPATCH.md"
                    and not child.name.startswith(".")
                ],
                key=lambda p: p.name,
            )
        except OSError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.ticket_flow.attachments_list_failed",
                exc=exc,
                run_id=run_id,
                seq=seq,
                dir=str(archived_dir),
            )
            return

        for item in items:
            await self._send_single_attachment(
                chat_id,
                thread_id,
                run_id=run_id,
                seq=seq,
                path=item,
            )

    async def _send_single_attachment(
        self,
        chat_id: int,
        thread_id: Optional[int],
        *,
        run_id: str,
        seq: str,
        path: Path,
    ) -> None:
        try:
            size = path.stat().st_size
        except OSError:
            size = None
        if size is not None and size > self._pause_config.max_file_size_bytes:
            warning = (
                f"Skipped attachment {path.name} "
                f"({size} bytes > {self._pause_config.max_file_size_bytes} limit)."
            )
            await self._send_message_with_outbox(
                chat_id,
                warning,
                thread_id=thread_id,
                reply_to=None,
            )
            return
        try:
            data = path.read_bytes()
        except OSError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.ticket_flow.attachment_read_failed",
                exc=exc,
                file=str(path),
                run_id=run_id,
                seq=seq,
            )
            await self._send_message_with_outbox(
                chat_id,
                f"Failed to read attachment {path.name}.",
                thread_id=thread_id,
                reply_to=None,
            )
            return
        caption = f"[run {run_id} dispatch #{seq}] {path.name}"
        send_ok = False
        try:
            send_ok = await self._send_document(
                chat_id,
                data,
                filename=path.name,
                thread_id=thread_id,
                reply_to=None,
                caption=caption[:1024],
            )
            if not send_ok:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.ticket_flow.attachment_send_failed",
                    file=str(path),
                    run_id=run_id,
                    seq=seq,
                )
        except (RuntimeError, OSError, ConnectionError) as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.ticket_flow.attachment_send_failed",
                exc=exc,
                file=str(path),
                run_id=run_id,
                seq=seq,
            )
        if not send_ok:
            await self._send_message_with_outbox(
                chat_id,
                f"Failed to send attachment {path.name}.",
                thread_id=thread_id,
                reply_to=None,
            )

    async def watch_ticket_flow_terminals(self, interval_seconds: float) -> None:
        interval = max(interval_seconds, 1.0)
        while True:
            try:
                await self._scan_and_notify_terminals()
            except Exception as exc:  # intentional: top-level error handler
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.ticket_flow.terminal_watch_failed",
                    exc=exc,
                )
            await asyncio.sleep(interval)

    async def _scan_and_notify_terminals(self) -> None:
        topics = await self._store.list_topics()
        workspace_topics = self._get_all_workspaces(topics or {})
        tasks = []
        for workspace_root, entries in workspace_topics.items():
            tasks.append(
                asyncio.create_task(
                    self._notify_terminal_for_workspace(workspace_root, entries)
                )
            )
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _notify_terminal_for_workspace(
        self,
        workspace_root: Path,
        entries: list[tuple[str, object]],
    ) -> None:
        try:
            terminal_run = await asyncio.to_thread(
                self._load_latest_terminal_run, workspace_root
            )
        except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.ticket_flow.terminal_scan_failed",
                exc=exc,
                workspace_root=str(workspace_root),
            )
            await self._notify_scan_failure(
                workspace_root,
                entries,
                error=exc,
                failure_kind="terminal",
            )
            return
        self._clear_scan_failure_marker("terminal", workspace_root)
        if terminal_run is None:
            return
        run_id, status, error_message, terminal_at = terminal_run
        pending = [
            (key, record)
            for key, record in entries
            if self._should_notify_terminal(
                record=record,
                run_id=run_id,
                status=status,
                terminal_at=terminal_at,
            )
        ]
        if not pending:
            return
        primary = self._select_ticket_flow_topic(pending)
        if not primary:
            return
        primary_key, primary_record = primary
        try:
            chat_id, thread_id, _scope = parse_topic_key(primary_key)
        except (ValueError, TypeError) as exc:
            self._logger.debug("Failed to parse topic key: %s", exc)
            return
        message = self._format_terminal_notification(
            run_id=run_id, status=status, error_message=error_message
        )
        try:
            await self._send_message_with_outbox(
                chat_id, message, thread_id=thread_id, reply_to=None
            )
            run_mirror = ChatRunMirror(workspace_root, logger_=self._logger)
            run_mirror.mirror_outbound(
                run_id=run_id,
                platform="telegram",
                event_type="flow_terminal_notice",
                kind="notification",
                actor="car",
                text=message,
                chat_id=chat_id,
                thread_id=thread_id,
                meta={"status": status},
            )
        except (RuntimeError, OSError, ConnectionError) as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.ticket_flow.terminal_notify_failed",
                exc=exc,
                topic_key=primary_key,
                run_id=run_id,
            )
            return
        for key, _record in pending:
            await self._store.update_topic(
                key,
                self._set_terminal_run_marker(run_id, finished_at=terminal_at),
            )
        log_event(
            self._logger,
            logging.INFO,
            "telegram.ticket_flow.terminal_notified",
            topic_key=primary_key,
            run_id=run_id,
            status=status,
        )

    def _load_latest_terminal_run(
        self, workspace_root: Path
    ) -> Optional[tuple[str, str, Optional[str], Optional[str]]]:
        db_path = workspace_root / ".codex-autorunner" / "flows.db"
        if not db_path.exists():
            return None
        try:
            config = load_repo_config(workspace_root)
            durable_writes = config.durable_writes
        except ConfigError:
            durable_writes = False
        store = FlowStore(db_path, durable=durable_writes)
        terminal_statuses = (
            FlowRunStatus.COMPLETED,
            FlowRunStatus.FAILED,
            FlowRunStatus.STOPPED,
        )
        latest_run: Optional[FlowRunRecord] = None
        try:
            store.initialize()
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
        finally:
            store.close()
        if latest_run is None:
            return None
        terminal_at = latest_run.finished_at or latest_run.created_at
        return (
            latest_run.id,
            latest_run.status.value,
            latest_run.error_message,
            terminal_at,
        )

    def _format_terminal_notification(
        self, *, run_id: str, status: str, error_message: Optional[str]
    ) -> str:
        if status == FlowRunStatus.COMPLETED.value:
            return f"Ticket flow completed successfully (run {run_id})."
        elif status == FlowRunStatus.FAILED.value:
            error_text = self._truncate_error(error_message)
            return f"Ticket flow failed (run {run_id}). Error: {error_text}"
        elif status == FlowRunStatus.STOPPED.value:
            return f"Ticket flow stopped (run {run_id})."
        return f"Ticket flow ended (run {run_id}, status: {status})."

    def _truncate_error(self, error_message: Optional[str], limit: int = 200) -> str:
        if not error_message:
            return "Unknown error"
        normalized = " ".join(error_message.split())
        if len(normalized) > limit:
            return f"{normalized[: limit - 3]}..."
        return normalized

    @staticmethod
    def _set_terminal_run_marker(value: Optional[str], *, finished_at: Optional[str]):
        def apply(topic) -> None:
            topic.last_terminal_run_id = value
            if hasattr(topic, "last_terminal_finished_at"):
                topic.last_terminal_finished_at = finished_at

        return apply

    async def _notify_scan_failure(
        self,
        workspace_root: Path,
        entries: list[tuple[str, object]],
        *,
        error: Exception,
        failure_kind: str,
    ) -> None:
        marker_key = (failure_kind, workspace_root)
        marker = f"{type(error).__name__}:{error}"
        if self._scan_failure_markers.get(marker_key) == marker:
            return

        message = self._format_scan_failure_message(
            workspace_root=workspace_root,
            error=error,
            failure_kind=failure_kind,
        )
        sent = await self._send_scan_failure_message(
            workspace_root=workspace_root,
            entries=entries,
            message=message,
        )
        if sent:
            self._scan_failure_markers[marker_key] = marker
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.ticket_flow.degraded_notified",
                workspace_root=str(workspace_root),
                failure_kind=failure_kind,
            )

    def _clear_scan_failure_marker(
        self, failure_kind: str, workspace_root: Path
    ) -> None:
        self._scan_failure_markers.pop((failure_kind, workspace_root), None)

    def _format_scan_failure_message(
        self,
        *,
        workspace_root: Path,
        error: Exception,
        failure_kind: str,
    ) -> str:
        label = "paused dispatches" if failure_kind == "pause" else "terminal runs"
        error_text = self._truncate_error(str(error) or type(error).__name__)
        return (
            f"Ticket flow status is degraded for `{workspace_root}`.\n"
            f"I couldn't scan {label}: {error_text}\n"
            "Check the workspace flow state and recover the bridge before relying on this topic."
        )

    async def _send_scan_failure_message(
        self,
        *,
        workspace_root: Path,
        entries: list[tuple[str, object]],
        message: str,
    ) -> bool:
        primary = self._select_ticket_flow_topic(entries)
        if primary is not None:
            primary_key, _primary_record = primary
            try:
                chat_id, thread_id, _scope = parse_topic_key(primary_key)
            except (ValueError, TypeError) as exc:
                self._logger.debug("Failed to parse topic key: %s", exc)
            else:
                sent = await self._send_message_with_outbox(
                    chat_id, message, thread_id=thread_id, reply_to=None
                )
                return bool(sent)
        if self._default_notification_chat_id is None:
            return False
        sent = await self._send_message_with_outbox(
            self._default_notification_chat_id,
            message,
            thread_id=None,
            reply_to=None,
        )
        return bool(sent)
