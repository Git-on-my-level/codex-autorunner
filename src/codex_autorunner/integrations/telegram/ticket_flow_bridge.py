from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from ...core.flows import FlowStore
from ...core.flows.controller import FlowController
from ...core.flows.models import FlowRunRecord, FlowRunStatus
from ...core.flows.worker_process import spawn_flow_worker
from ...core.utils import canonicalize_path
from ...flows.ticket_flow import build_ticket_flow_definition
from ...tickets import AgentPool
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
    ) -> None:
        self._logger = logger
        self._store = store
        self._pause_targets = pause_targets
        self._send_message_with_outbox = send_message_with_outbox

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
                self._logger.warning("telegram.ticket_flow.watch_failed", exc_info=exc)
            await asyncio.sleep(interval)

    async def _scan_and_notify_pauses(self) -> None:
        topics = await self._store.list_topics()
        if not topics:
            return
        workspace_topics: dict[Path, list[tuple[str, object]]] = {}
        for key, record in topics.items():
            if not isinstance(record.workspace_path, str) or not record.workspace_path:
                continue
            workspace_root = canonicalize_path(Path(record.workspace_path))
            workspace_topics.setdefault(workspace_root, []).append((key, record))

        tasks = [
            asyncio.create_task(self._notify_ticket_flow_pause(workspace_root, entries))
            for workspace_root, entries in workspace_topics.items()
        ]
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
            self._logger.warning(
                "telegram.ticket_flow.scan_failed",
                exc_info=exc,
                workspace_root=str(workspace_root),
            )
            return
        if pause is None:
            return
        run_id, seq, content = pause
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
        message_text = self._format_ticket_flow_pause_message(run_id, seq, content)
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
            await self._send_message_with_outbox(
                chat_id,
                message_text,
                thread_id=thread_id,
                reply_to=None,
            )
            self._pause_targets[str(workspace_root)] = run_id
        except Exception as exc:
            self._logger.warning(
                "telegram.ticket_flow.notify_failed",
                exc_info=exc,
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

    def _load_ticket_flow_pause(
        self, workspace_root: Path
    ) -> Optional[tuple[str, str, str]]:
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
                return latest.id, "paused", reason
            message_path = history_dir / seq / "DISPATCH.md"
            try:
                content = message_path.read_text(encoding="utf-8")
            except OSError:
                return None
            return latest.id, seq, content
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

    def _format_ticket_flow_pause_message(
        self, run_id: str, seq: str, content: str
    ) -> str:
        from .helpers import _truncate_text

        trimmed = _truncate_text(content.strip() or "(no dispatch message)", 3000)
        return (
            f"Ticket flow paused (run {run_id}). Latest dispatch #{seq}:\n\n"
            f"{trimmed}\n\nUse /flow resume to continue."
        )

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
            self._logger.warning(
                "telegram.ticket_flow.auto_resume_failed",
                exc=exc,
                run_id=run_id,
                workspace_root=str(workspace_root),
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
