from __future__ import annotations

import logging
from pathlib import Path

from .....core.engine import Engine
from .....core.flows import FlowController, FlowStore
from .....core.flows.models import FlowRunStatus
from .....core.flows.worker_process import (
    check_worker_health,
    spawn_flow_worker,
)
from .....core.utils import canonicalize_path
from .....flows.ticket_flow import build_ticket_flow_definition
from .....tickets import AgentPool
from ....github.service import GitHubService
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
    health = check_worker_health(repo_root, run_id)
    if health.is_alive:
        _logger.info("Worker already active for run %s (pid=%s)", run_id, health.pid)
        return

    proc, out, err = spawn_flow_worker(repo_root, run_id)
    try:
        # We don't track handles in Telegram commands, close in parent after spawn.
        out.close()
        err.close()
    finally:
        if proc.poll() is not None:
            _logger.warning("Flow worker for %s exited immediately", run_id)


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
            issue_path = repo_root / ".codex-autorunner" / "ISSUE.md"
            issue_exists = (
                issue_path.exists() and issue_path.read_text(encoding="utf-8").strip()
                if issue_path.exists()
                else False
            )
            if not first_ticket.exists():
                first_ticket.write_text(
                    """---
agent: codex
done: false
title: Bootstrap ticket flow
goal: Create SPEC.md and additional tickets, then pause for review
---

Create SPEC.md and additional tickets under .codex-autorunner/tickets/. Then write a pause DISPATCH.md for review.
""",
                    encoding="utf-8",
                )
                seeded = True

            flow_record = await controller.start_flow(
                input_data={},
                metadata={"seeded_ticket": seeded, "origin": "telegram"},
            )
            _spawn_flow_worker(repo_root, flow_record.id)

            if not issue_exists:
                gh_status = "GitHub not detected; please describe the work so I can write ISSUE.md."
                try:
                    gh = GitHubService(repo_root=repo_root)
                    gh_available = gh.gh_available() and gh.gh_authenticated()
                    if gh_available:
                        repo_info = gh.repo_info()
                        gh_status = (
                            f"No ISSUE.md found. Reply with a GitHub issue URL or number for {repo_info.name_with_owner} "
                            "and I'll fetch it into .codex-autorunner/ISSUE.md."
                        )
                    else:
                        gh_status = (
                            "No ISSUE.md found and GitHub CLI unavailable. "
                            "Reply with a short plan/requirements so I can seed ISSUE.md."
                        )
                except Exception:
                    pass
                await self._send_message(
                    message.chat_id,
                    gh_status,
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )

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
        text = args.strip()
        if not text:
            await self._send_message(
                message.chat_id,
                "Provide a reply: `/reply <message>`",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        target_run_id = self._ticket_flow_pause_targets.get(str(repo_root))
        paused = self._get_paused_ticket_flow(repo_root, preferred_run_id=target_run_id)
        if not paused:
            await self._send_message(
                message.chat_id,
                "No paused ticket flow run found for this workspace.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        run_id, run_record = paused
        success, result = await self._write_user_reply_from_telegram(
            repo_root, run_id, run_record, message, text
        )
        await self._send_message(
            message.chat_id,
            result,
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
