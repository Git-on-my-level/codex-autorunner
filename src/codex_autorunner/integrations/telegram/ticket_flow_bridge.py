from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional

from ...core.flows import FlowStore
from ...core.flows.controller import FlowController
from ...core.flows.models import FlowRunRecord, FlowRunStatus
from ...core.flows.worker_process import spawn_flow_worker
from ...core.logging_utils import log_event
from ...core.utils import canonicalize_path
from ...flows.ticket_flow import build_ticket_flow_definition
from ...manifest import load_manifest
from ...tickets import AgentPool
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

        primary_key, _primary_record = primary
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
        db_path = workspace_root / ".codex-autorunner" / "flows.db"
        if not db_path.exists():
            return None
        store = FlowStore(db_path)
        try:
            store.initialize()
            runs = store.list_flow_runs(
                flow_type="ticket_flow", status=FlowRunStatus.PAUSED
            )
            if not runs:
                return None
            latest = runs[0]
            runs_dir_raw = latest.input_data.get("runs_dir")
            runs_dir = (
                Path(runs_dir_raw)
                if isinstance(runs_dir_raw, str) and runs_dir_raw
                else Path(".codex-autorunner/runs")
            )
            from ...tickets.outbox import resolve_outbox_paths

            paths = resolve_outbox_paths(
                workspace_root=workspace_root, runs_dir=runs_dir, run_id=latest.id
            )
            history_dir = paths.dispatch_history_dir
            seq = self._latest_dispatch_seq(history_dir)
            if not seq:
                reason = self._format_ticket_flow_pause_reason(latest)
                return latest.id, "paused", reason, None
            message_path = history_dir / seq / "DISPATCH.md"
            try:
                content = message_path.read_text(encoding="utf-8")
            except OSError:
                return None
            return latest.id, seq, content, history_dir / seq
        finally:
            store.close()

    @staticmethod
    def _latest_dispatch_seq(history_dir: Path) -> Optional[str]:
        if not history_dir.exists() or not history_dir.is_dir():
            return None
        seqs = [
            child.name
            for child in history_dir.iterdir()
            if child.is_dir()
            and not child.name.startswith(".")
            and child.name.isdigit()
        ]
        if not seqs:
            return None
        return max(seqs)

    @staticmethod
    def _format_ticket_flow_pause_reason(record: FlowRunRecord) -> str:
        state = record.state or {}
        engine = state.get("ticket_engine") or {}
        reason = (
            engine.get("reason") or record.error_message or "Paused without details."
        )
        return f"Reason: {reason}"

    def get_paused_ticket_flow(
        self, workspace_root: Path, preferred_run_id: Optional[str] = None
    ) -> Optional[tuple[str, FlowRunRecord]]:
        db_path = workspace_root / ".codex-autorunner" / "flows.db"
        if not db_path.exists():
            return None
        store = FlowStore(db_path)
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
            controller = _ticket_controller_for(workspace_root)
            updated = await controller.resume_flow(run_id)
            if updated:
                _spawn_ticket_worker(workspace_root, updated.id, self._logger)
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

    async def _send_full_dispatch(
        self,
        chat_id: int,
        thread_id: Optional[int],
        *,
        run_id: str,
        seq: str,
        content: str,
        archived_dir: Optional[Path],
    ) -> None:
        await self._send_dispatch_text(
            chat_id,
            thread_id,
            run_id=run_id,
            seq=seq,
            content=content,
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
    ) -> None:
        body = content.strip() or "(no dispatch message)"
        header = f"Ticket flow paused (run {run_id}). Latest dispatch #{seq}:\n\n"
        footer = "\n\nUse /flow resume to continue."
        full_text = f"{header}{body}{footer}"

        if self._pause_config.chunk_long_messages:
            chunks = chunk_message(
                full_text,
                max_len=TELEGRAM_MAX_MESSAGE_LENGTH,
                with_numbering=True,
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
            if idx == 0:
                await asyncio.sleep(0)

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


def _ticket_controller_for(repo_root: Path) -> FlowController:
    repo_root = repo_root.resolve()
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    artifacts_root = repo_root / ".codex-autorunner" / "flows"
    from ...core.engine import Engine

    engine = Engine(repo_root)
    agent_pool = AgentPool(engine.config)
    definition = build_ticket_flow_definition(agent_pool=agent_pool)
    definition.validate()
    controller = FlowController(
        definition=definition, db_path=db_path, artifacts_root=artifacts_root
    )
    controller.initialize()
    return controller


def _spawn_ticket_worker(repo_root: Path, run_id: str, logger: logging.Logger) -> None:
    try:
        proc, out, err = spawn_flow_worker(repo_root, run_id)
        out.close()
        err.close()
        logger.info("Started ticket_flow worker for %s (pid=%s)", run_id, proc.pid)
    except Exception as exc:
        logger.warning(
            "ticket_flow.worker.spawn_failed",
            exc_info=exc,
            extra={"run_id": run_id},
        )
