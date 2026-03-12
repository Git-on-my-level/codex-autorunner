from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from ...core.chat_bindings import (
    preferred_non_pma_chat_notification_source_for_workspace,
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
from ..chat.run_mirror import ChatRunMirror
from .adapter import chunk_message
from .constants import TELEGRAM_MAX_MESSAGE_LENGTH
from .state import parse_topic_key


class TelegramTicketFlowBridge:
    """Encapsulate ticket_flow pause/resume plumbing for Telegram service."""

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
            except Exception:
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
            return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").timestamp()
        except ValueError:
            return float("-inf")

    async def watch_ticket_flow_pauses(self, interval_seconds: float) -> None:
        interval = max(interval_seconds, 1.0)
        while True:
            try:
                await self._scan_and_notify_pauses()
            except Exception as exc:
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

        tasks = []
        for workspace_root, entries in workspace_topics.items():
            if entries:
                tasks.append(
                    asyncio.create_task(
                        self._notify_ticket_flow_pause(workspace_root, entries)
                    )
                )
            else:
                tasks.append(
                    asyncio.create_task(self._notify_via_default_chat(workspace_root))
                )
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _notify_ticket_flow_pause(
        self,
        workspace_root: Path,
        entries: list[tuple[str, object]],
    ) -> None:
        preferred_source = self._preferred_bound_source_for_workspace(workspace_root)
        if preferred_source == "discord":
            return
        try:
            pause = await asyncio.to_thread(
                self._load_ticket_flow_pause, workspace_root
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.ticket_flow.scan_failed",
                exc=exc,
                workspace_root=str(workspace_root),
            )
            return
        if pause is None:
            return
        run_id, seq, content, archived_dir = pause
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
        except Exception as exc:
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
            )
            self._pause_targets[str(workspace_root)] = run_id
        except Exception as exc:
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
            except Exception as exc:
                self._logger.debug(
                    "telegram.ticket_flow.manifest_load_failed", exc_info=exc
                )

        return workspace_topics

    def _load_ticket_flow_pause(
        self, workspace_root: Path
    ) -> Optional[tuple[str, str, str, Optional[Path]]]:
        snapshot = load_latest_paused_ticket_flow_dispatch(workspace_root)
        if snapshot is None:
            return None
        return (
            snapshot.run_id,
            snapshot.dispatch_seq,
            snapshot.dispatch_markdown,
            snapshot.dispatch_dir,
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
                controller = self._runtime_services.get_ticket_flow_controller(
                    workspace_root
                )
            else:
                controller = build_ticket_flow_controller(workspace_root)
            updated = await controller.resume_flow(run_id)
            if updated:
                spawn_ticket_flow_worker(workspace_root, updated.id, self._logger)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.ticket_flow.auto_resume_failed",
                exc=exc,
                run_id=run_id,
                workspace_root=str(workspace_root),
            )

    async def _notify_via_default_chat(self, workspace_root: Path) -> None:
        if not self._pause_config.enabled or self._default_notification_chat_id is None:
            return
        preferred_source = self._preferred_bound_source_for_workspace(workspace_root)
        if preferred_source in {"telegram", "discord"}:
            return
        try:
            pause = await asyncio.to_thread(
                self._load_ticket_flow_pause, workspace_root
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.ticket_flow.scan_failed",
                exc=exc,
                workspace_root=str(workspace_root),
            )
            return
        if pause is None:
            return
        run_id, seq, content, archived_dir = pause
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
            )
            self._last_default_notification[workspace_root] = marker
            self._pause_targets[str(workspace_root)] = run_id
        except Exception as exc:
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
            except Exception:
                raw_config = {}
            self._hub_raw_config = raw_config
        try:
            return preferred_non_pma_chat_notification_source_for_workspace(
                hub_root=self._hub_root,
                raw_config=raw_config,
                workspace_root=workspace_root,
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.ticket_flow.route_lookup_failed",
                exc=exc,
                workspace_root=str(workspace_root),
            )
            return None

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
    ) -> None:
        await self._send_dispatch_text(
            chat_id,
            thread_id,
            run_id=run_id,
            seq=seq,
            content=content,
            workspace_root=workspace_root,
            repo_id=repo_id,
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
    ) -> None:
        run_mirror = (
            ChatRunMirror(workspace_root, logger_=self._logger)
            if isinstance(workspace_root, Path)
            else None
        )
        body = content.strip() or "(no dispatch message)"
        source = self._format_dispatch_source(workspace_root, repo_id)
        header = (
            f"Ticket flow paused (run {run_id}). Latest dispatch #{seq}:\n"
            f"Source: {source}\n\n"
        )
        footer = "\n\nUse /flow resume to continue."
        full_text = f"{header}{body}{footer}"

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
        workspace_label = None
        if isinstance(workspace_root, Path):
            workspace_label = str(workspace_root)
        repo_label = repo_id.strip() if isinstance(repo_id, str) else ""
        if self._hub_root and self._manifest_path and self._manifest_path.exists():
            try:
                manifest = load_manifest(self._manifest_path, self._hub_root)
                if workspace_root:
                    entry = manifest.get_by_path(self._hub_root, workspace_root)
                else:
                    entry = None
                if entry:
                    repo_label = entry.id or repo_label
                    if entry.display_name and entry.display_name != repo_label:
                        repo_label = f"{repo_label} ({entry.display_name})"
                    if entry.kind == "worktree" and entry.worktree_of:
                        repo_label = f"{repo_label} [worktree of {entry.worktree_of}]"
            except Exception as exc:
                self._logger.debug(
                    "telegram.ticket_flow.manifest_label_failed", exc_info=exc
                )
        if repo_label and workspace_label:
            return f"{repo_label} @ {workspace_label}"
        if repo_label:
            return repo_label
        if workspace_label:
            return workspace_label
        return "unknown workspace"

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
        except Exception as exc:
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
            except Exception as exc:
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
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.ticket_flow.terminal_scan_failed",
                exc=exc,
                workspace_root=str(workspace_root),
            )
            return
        if terminal_run is None:
            return
        run_id, status, error_message = terminal_run
        pending = [
            (key, record)
            for key, record in entries
            if getattr(record, "last_terminal_run_id", None) != run_id
        ]
        if not pending:
            return
        primary = self._select_ticket_flow_topic(pending)
        if not primary:
            return
        primary_key, primary_record = primary
        try:
            chat_id, thread_id, _scope = parse_topic_key(primary_key)
        except Exception as exc:
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
        except Exception as exc:
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
            await self._store.update_topic(key, self._set_terminal_run_marker(run_id))
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
    ) -> Optional[tuple[str, str, Optional[str]]]:
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
        return (latest_run.id, latest_run.status.value, latest_run.error_message)

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
    def _set_terminal_run_marker(value: Optional[str]):
        def apply(topic) -> None:
            topic.last_terminal_run_id = value

        return apply
