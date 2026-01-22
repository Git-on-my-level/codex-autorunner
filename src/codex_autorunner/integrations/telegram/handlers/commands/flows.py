from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

from .....core.engine import Engine
from .....core.flows import FlowController, FlowStore
from .....core.flows.models import FlowRunStatus
from .....core.utils import canonicalize_path
from .....flows.ticket_flow import build_ticket_flow_definition
from .....tickets import AgentPool
from ...adapter import TelegramMessage
from ...helpers import _truncate_text
from .shared import SharedHelpers

_logger = logging.getLogger(__name__)


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
    # Run detached worker (best-effort). stdout/stderr to files to avoid pipe deadlocks.
    logs_dir = repo_root / ".codex-autorunner" / "runs" / run_id
    logs_dir.mkdir(parents=True, exist_ok=True)
    out = (logs_dir / "worker.stdout.log").open("ab")
    err = (logs_dir / "worker.stderr.log").open("ab")
    try:
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "codex_autorunner.cli",
                "flow",
                "worker",
                "--run-id",
                run_id,
            ],
            cwd=str(repo_root),
            stdout=out,
            stderr=err,
            start_new_session=True,
        )
    finally:
        out.close()
        err.close()


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
