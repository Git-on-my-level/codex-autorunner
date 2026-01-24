from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Optional

from .....core.engine import Engine
from .....core.flows import FlowController, FlowStore
from .....core.flows.models import FlowRunStatus
from .....core.flows.worker import spawn_flow_worker
from .....core.utils import canonicalize_path
from .....flows.ticket_flow import build_ticket_flow_definition
from .....tickets import AgentPool
from ...adapter import TelegramMessage
from ...helpers import _truncate_text
from .shared import SharedHelpers

_logger = logging.getLogger(__name__)


def _parse_run_id(value: str) -> Optional[str]:
    try:
        return str(uuid.UUID(value))
    except (TypeError, ValueError):
        return None


def _flow_paths(repo_root: Path) -> tuple[Path, Path]:
    repo_root = repo_root.resolve()
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    artifacts_root = repo_root / ".codex-autorunner" / "flows"
    return db_path, artifacts_root


def _get_ticket_controller(repo_root: Path) -> FlowController:
    db_path, artifacts_root = _flow_paths(repo_root)
    engine = Engine(repo_root)
    agent_pool = AgentPool(engine.config)
    definition = build_ticket_flow_definition(agent_pool=agent_pool)
    definition.validate()
    controller = FlowController(
        definition=definition, db_path=db_path, artifacts_root=artifacts_root
    )
    controller.initialize()
    return controller


def _spawn_flow_worker(repo_root: Path, run_id: str) -> None:
    spawn_flow_worker(repo_root, run_id, logger=_logger, start_new_session=True)


class FlowCommands(SharedHelpers):
    async def _handle_flow(self, message: TelegramMessage, args: str) -> None:
        """
        /flow start     - seed tickets if missing and start ticket_flow
        /flow resume    - resume latest paused ticket_flow run
        /flow status    - show latest ticket_flow run status
        """
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        record = await self._store.get_topic(key)
        if not record or not record.workspace_path:
            await self._send_message(
                message.chat_id,
                "No workspace bound. Use /bind to bind this topic to a repo first.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        repo_root = canonicalize_path(Path(record.workspace_path))
        cmd = (args or "").strip().lower().split()
        action = cmd[0] if cmd else "status"

        controller = _get_ticket_controller(repo_root)

        store = FlowStore(_flow_paths(repo_root)[0])
        try:
            store.initialize()
            runs = store.list_flow_runs(flow_type="ticket_flow")
            latest = runs[0] if runs else None
        finally:
            store.close()

        if action == "start":
            if latest and latest.status.is_active():
                await self._send_message(
                    message.chat_id,
                    f"Ticket flow already active (run {latest.id}, status {latest.status.value}).",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            # seed ticket if missing
            ticket_dir = repo_root / ".codex-autorunner" / "tickets"
            ticket_dir.mkdir(parents=True, exist_ok=True)
            first_ticket = ticket_dir / "TICKET-001.md"
            seeded = False
            if not first_ticket.exists():
                first_ticket.write_text(
                    """---
agent: codex
done: false
title: Bootstrap ticket flow
goal: Create SPEC.md and additional tickets, then pause for review
requires:
  - .codex-autorunner/ISSUE.md
---

Create SPEC.md and additional tickets under .codex-autorunner/tickets/. Then write a pause USER_MESSAGE for review.
""",
                    encoding="utf-8",
                )
                seeded = True

            flow_record = await controller.start_flow(
                input_data={},
                metadata={"seeded_ticket": seeded, "origin": "telegram"},
            )
            _spawn_flow_worker(repo_root, flow_record.id)
            await self._send_message(
                message.chat_id,
                f"Started ticket flow run {flow_record.id}.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        if action == "resume":
            if not latest:
                await self._send_message(
                    message.chat_id,
                    "No ticket flow run found.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            if latest.status != FlowRunStatus.PAUSED:
                await self._send_message(
                    message.chat_id,
                    f"Latest run is {latest.status.value}, not paused.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            updated = await controller.resume_flow(latest.id)
            _spawn_flow_worker(repo_root, updated.id)
            await self._send_message(
                message.chat_id,
                f"Resumed run {updated.id}.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        # status (default)
        if not latest:
            await self._send_message(
                message.chat_id,
                "No ticket flow run found. Use /flow start to start.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        state = latest.state or {}
        engine = state.get("ticket_engine") or {}
        current = engine.get("current_ticket") or "–"
        reason = engine.get("reason") or latest.error_message or ""
        text = f"Run {latest.id}\nStatus: {latest.status.value}\nCurrent: {current}"
        if reason:
            text += f"\nReason: {_truncate_text(str(reason), 400)}"
        text += "\n\nUse /flow resume to resume a paused run."
        await self._send_message(
            message.chat_id,
            text,
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_reply(self, message: TelegramMessage, args: str) -> None:
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        record = await self._store.get_topic(key)
        if not record or not record.workspace_path:
            await self._send_message(
                message.chat_id,
                "No workspace bound. Use /bind to bind this topic to a repo first.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        repo_root = canonicalize_path(Path(record.workspace_path))
        raw = args.strip()
        if not raw:
            await self._send_message(
                message.chat_id,
                "Provide a reply: `/reply <run_id> <message>` or `/reply <message>`",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        run_id = None
        message_text = raw
        head, _sep, tail = raw.partition(" ")
        candidate = _parse_run_id(head)
        if candidate:
            if not tail.strip():
                await self._send_message(
                    message.chat_id,
                    "Provide a reply after the run id: `/reply <run_id> <message>`",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            run_id = candidate
            message_text = tail.strip()

        run_record = None
        if run_id:
            store = FlowStore(_flow_paths(repo_root)[0])
            try:
                store.initialize()
                record = store.get_flow_run(run_id)
            finally:
                store.close()
            if record is None or record.flow_type != "ticket_flow":
                await self._send_message(
                    message.chat_id,
                    f"Run {run_id} not found for ticket_flow.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            if record.status != FlowRunStatus.PAUSED:
                await self._send_message(
                    message.chat_id,
                    f"Run {run_id} is {record.status.value}, not paused.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            run_record = record
        else:
            target_run_id = self._ticket_flow_pause_targets.get(str(repo_root))
            paused = self._get_paused_ticket_flow(
                repo_root, preferred_run_id=target_run_id
            )
            if not paused:
                await self._send_message(
                    message.chat_id,
                    "No paused ticket flow run found for this workspace.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            run_id, run_record = paused

        files = []
        if message.photos:
            photos = sorted(
                message.photos,
                key=lambda p: (p.file_size or 0, p.width * p.height),
                reverse=True,
            )
            if photos:
                best = photos[0]
                try:
                    file_info = await self._bot.get_file(best.file_id)
                    data = await self._bot.download_file(file_info.file_path)
                    filename = f"photo_{best.file_id}.jpg"
                    files.append((filename, data))
                except Exception:
                    pass
        elif message.document:
            try:
                file_info = await self._bot.get_file(message.document.file_id)
                data = await self._bot.download_file(file_info.file_path)
                filename = (
                    message.document.file_name or f"document_{message.document.file_id}"
                )
                files.append((filename, data))
            except Exception:
                pass

        success, result = await self._write_user_reply_from_telegram(
            repo_root,
            run_id,
            run_record,
            message,
            message_text,
            files if files else None,
        )
        await self._send_message(
            message.chat_id,
            result,
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
