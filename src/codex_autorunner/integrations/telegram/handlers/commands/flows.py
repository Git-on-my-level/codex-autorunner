from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Callable, Optional

from .....agents.registry import validate_agent_id
from .....core.config import load_hub_config, load_repo_config
from .....core.flows import FlowController, FlowStore
from .....core.flows.models import FlowRunStatus
from .....core.flows.reconciler import reconcile_flow_run
from .....core.flows.ux_helpers import (
    bootstrap_check,
    build_flow_status_snapshot,
    ensure_worker,
    issue_md_has_content,
    issue_md_path,
    seed_issue_from_github,
    seed_issue_from_text,
    ticket_progress,
)
from .....core.flows.worker_process import (
    FlowWorkerHealth,
    check_worker_health,
    clear_worker_metadata,
)
from .....core.logging_utils import log_event
from .....core.runtime import RuntimeContext
from .....core.state import now_iso
from .....core.utils import atomic_write, canonicalize_path
from .....flows.ticket_flow import build_ticket_flow_definition
from .....integrations.agents import build_backend_orchestrator
from .....integrations.agents.wiring import (
    build_agent_backend_factory,
    build_app_server_supervisor_factory,
)
from .....manifest import load_manifest
from .....tickets import AgentPool
from .....tickets.files import list_ticket_paths
from .....tickets.outbox import resolve_outbox_paths
from ....github.service import GitHubError, GitHubService
from ...adapter import (
    FlowCallback,
    InlineButton,
    TelegramCallbackQuery,
    TelegramMessage,
    build_inline_keyboard,
    encode_flow_callback,
    encode_question_cancel_callback,
)
from ...config import DEFAULT_APPROVAL_TIMEOUT_SECONDS
from ...helpers import _truncate_text
from ...types import PendingQuestion, SelectionState
from .shared import SharedHelpers

_logger = logging.getLogger(__name__)


def _flow_paths(repo_root: Path) -> tuple[Path, Path]:
    repo_root = repo_root.resolve()
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    artifacts_root = repo_root / ".codex-autorunner" / "flows"
    return db_path, artifacts_root


def _ticket_dir(repo_root: Path) -> Path:
    return repo_root.resolve() / ".codex-autorunner" / "tickets"


def _load_flow_store(repo_root: Path, hub_root: Optional[Path] = None) -> FlowStore:
    config = load_repo_config(repo_root, hub_root)
    return FlowStore(_flow_paths(repo_root)[0], durable=config.durable_writes)


def _normalize_run_id(value: str) -> Optional[str]:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        return None


def _split_flow_action(args: str) -> tuple[str, str]:
    trimmed = (args or "").strip()
    if not trimmed:
        return "", ""
    parts = trimmed.split(None, 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def _normalize_flow_action(action: str) -> str:
    normalized = (action or "").strip().lower()
    if not normalized:
        return "help"
    if normalized == "start":
        return "bootstrap"
    return normalized


def _flow_help_lines() -> list[str]:
    return [
        "Flow commands:",
        "/flow status [run_id]",
        "/flow runs [N]",
        "/flow bootstrap [--force-new]",
        "/flow issue <issue#|url>",
        "/flow plan <text>",
        "/flow resume [run_id]",
        "/flow stop [run_id]",
        "/flow recover [run_id]",
        "/flow restart",
        "/flow archive [run_id] [--force]",
        "/flow reply <message>",
        "Alias: /flow start",
    ]


def _discover_unregistered_worktrees(
    manifest, hub_root: Optional[Path]
) -> list[dict[str, object]]:
    if not hub_root:
        return []
    try:
        hub_config = load_hub_config(hub_root)
    except Exception:
        return []
    worktrees_root = hub_config.worktrees_root
    if not worktrees_root.exists() or not worktrees_root.is_dir():
        return []

    known_paths = {(hub_root / repo.path).resolve() for repo in manifest.repos}
    known_ids = {repo.id for repo in manifest.repos}
    extras: list[dict[str, object]] = []
    for child in sorted(worktrees_root.iterdir()):
        if not child.is_dir():
            continue
        if not (child / ".git").exists():
            continue
        resolved = child.resolve()
        if resolved in known_paths:
            continue

        flows_root = child / ".codex-autorunner" / "flows"
        flows_db = child / ".codex-autorunner" / "flows.db"
        if not flows_root.exists() and not flows_db.exists():
            continue

        repo_id = child.name
        label = repo_id
        indent = ""
        if "--" in repo_id:
            _, suffix = repo_id.split("--", 1)
            label = suffix or repo_id
            indent = "  - "
        label = f"{label} (unregistered)"
        if repo_id in known_ids:
            label = f"{label} (duplicate id)"
        extras.append(
            {
                "repo_id": repo_id,
                "repo_root": resolved,
                "label": label,
                "indent": indent,
                "unregistered": True,
            }
        )
    return extras


def _get_ticket_controller(repo_root: Path) -> FlowController:
    db_path, artifacts_root = _flow_paths(repo_root)
    config = load_repo_config(repo_root)
    backend_orchestrator = build_backend_orchestrator(repo_root, config)
    engine = RuntimeContext(
        repo_root,
        config=config,
        backend_orchestrator=backend_orchestrator,
        backend_factory=build_agent_backend_factory(repo_root, config),
        app_server_supervisor_factory=build_app_server_supervisor_factory(config),
        agent_id_validator=validate_agent_id,
    )
    agent_pool = AgentPool(engine.config)
    definition = build_ticket_flow_definition(agent_pool=agent_pool)
    definition.validate()
    controller = FlowController(
        definition=definition, db_path=db_path, artifacts_root=artifacts_root
    )
    controller.initialize()
    return controller


def _spawn_flow_worker(repo_root: Path, run_id: str) -> None:
    result = ensure_worker(repo_root, run_id)
    if result["status"] == "reused":
        health = result["health"]
        _logger.info("Worker already active for run %s (pid=%s)", run_id, health.pid)
        return
    proc = result["proc"]
    out = result["stdout"]
    err = result["stderr"]
    try:
        # We don't track handles in Telegram commands, close in parent after spawn.
        out.close()
        err.close()
    finally:
        if proc.poll() is not None:
            _logger.warning("Flow worker for %s exited immediately", run_id)


def _select_latest_run(
    store: FlowStore, predicate: Callable[[object], bool]
) -> Optional[object]:
    for record in store.list_flow_runs(flow_type="ticket_flow"):
        if predicate(record):
            return record
    return None


class FlowCommands(SharedHelpers):
    def _github_bootstrap_status(self, repo_root: Path) -> tuple[bool, Optional[str]]:
        result = bootstrap_check(repo_root, github_service_factory=GitHubService)
        return bool(result.github_available), result.repo_slug

    async def _prompt_flow_text_input(
        self,
        message: TelegramMessage,
        prompt_text: str,
    ) -> Optional[str]:
        request_id = str(uuid.uuid4())
        topic_key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        payload_text, parse_mode = self._prepare_outgoing_text(
            prompt_text,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            reply_to=message.message_id,
            topic_key=topic_key,
        )
        keyboard = build_inline_keyboard(
            [[InlineButton("Cancel", encode_question_cancel_callback(request_id))]]
        )
        response = await self._bot.send_message(
            message.chat_id,
            payload_text,
            message_thread_id=message.thread_id,
            reply_to_message_id=message.message_id,
            reply_markup=keyboard,
            parse_mode=parse_mode,
        )
        message_id = response.get("message_id") if isinstance(response, dict) else None
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Optional[str]] = loop.create_future()
        pending = PendingQuestion(
            request_id=request_id,
            turn_id=f"flow-bootstrap:{request_id}",
            codex_thread_id=None,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            topic_key=topic_key,
            message_id=message_id if isinstance(message_id, int) else None,
            created_at=now_iso(),
            question_index=0,
            prompt=prompt_text,
            options=[],
            future=future,
            multiple=False,
            custom=True,
            selected_indices=set(),
            awaiting_custom_input=True,
        )
        self._pending_questions[request_id] = pending
        self._touch_cache_timestamp("pending_questions", request_id)
        try:
            result = await asyncio.wait_for(
                future, timeout=DEFAULT_APPROVAL_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            self._pending_questions.pop(request_id, None)
            if pending.message_id is not None:
                await self._edit_message_text(
                    pending.chat_id,
                    pending.message_id,
                    "Question timed out.",
                    reply_markup={"inline_keyboard": []},
                )
            return None
        if not result:
            return None
        return result.strip() or None

    async def _seed_issue_from_ref(
        self, repo_root: Path, issue_ref: str
    ) -> tuple[int, str]:
        seed = seed_issue_from_github(
            repo_root, issue_ref, github_service_factory=GitHubService
        )
        atomic_write(issue_md_path(repo_root), seed.content)
        return seed.issue_number, seed.repo_slug

    def _seed_issue_from_plan(self, repo_root: Path, plan_text: str) -> None:
        content = seed_issue_from_text(plan_text)
        atomic_write(issue_md_path(repo_root), content)

    async def _handle_flow_status(self, message: TelegramMessage, args: str) -> None:
        text = args.strip()
        if text:
            await self._handle_flow(message, f"status {text}")
        else:
            await self._handle_flow(message, "status")

    async def _handle_flow(self, message: TelegramMessage, args: str) -> None:
        argv = self._parse_command_args(args)

        target_repo_root = None
        effective_args = args

        if argv:
            resolved = self._resolve_workspace(argv[0])
            if resolved:
                target_repo_root = Path(resolved[0])
                argv = argv[1:]
                # Reconstruct args for remainder logic (imperfect but sufficient for text commands)
                effective_args = " ".join(argv)

        action_raw = argv[0] if argv else ""
        if target_repo_root and not action_raw:
            action_raw = "status"
            argv = ["status"]
            effective_args = "status"
        action = _normalize_flow_action(action_raw)
        _, remainder = _split_flow_action(effective_args)
        rest_argv = argv[1:]

        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        record = await self._store.get_topic(key)
        is_pma = bool(record and getattr(record, "pma_enabled", False))
        is_unbound = bool(not record or not getattr(record, "workspace_path", None))

        if not target_repo_root and not action_raw:
            # Check if we should show Hub Overview
            if is_pma or is_unbound:
                await self._send_flow_hub_overview(message)
                return
            action = "status"
            rest_argv = []

        if action == "help":
            await self._send_flow_overview(message, record)
            return

        if target_repo_root:
            repo_root = canonicalize_path(target_repo_root)
        elif record and record.workspace_path:
            repo_root = canonicalize_path(Path(record.workspace_path))
        else:
            if action == "status" and (is_pma or is_unbound):
                await self._send_flow_hub_overview(message)
                return
            await self._send_message(
                message.chat_id,
                "No workspace bound. Use /flow <repo-id> status to inspect a repo without binding, or /bind <repo-id> to attach this topic.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        try:
            if action == "status":
                await self._handle_flow_status_action(message, repo_root, rest_argv)
                return
            if action == "runs":
                await self._handle_flow_runs(message, repo_root, rest_argv)
                return
            if action == "bootstrap":
                await self._handle_flow_bootstrap(message, repo_root, rest_argv)
                return
            if action == "issue":
                await self._handle_flow_issue(message, repo_root, remainder)
                return
            if action == "plan":
                await self._handle_flow_plan(message, repo_root, remainder)
                return
            if action == "resume":
                await self._handle_flow_resume(message, repo_root, rest_argv)
                return
            if action == "stop":
                await self._handle_flow_stop(message, repo_root, rest_argv)
                return
            if action == "recover":
                await self._handle_flow_recover(message, repo_root, rest_argv)
                return
            if action == "restart":
                await self._handle_flow_restart(message, repo_root, rest_argv)
                return
            if action == "archive":
                await self._handle_flow_archive(message, repo_root, rest_argv)
                return
            if action == "reply":
                await self._handle_reply(message, remainder)
                return
        except (asyncio.CancelledError, KeyboardInterrupt):
            # Let cancellations propagate so shutdowns/timeouts are not masked.
            raise
        except Exception as exc:  # pragma: no cover - defensive
            log_event(
                _logger,
                logging.WARNING,
                "telegram.flow.command_failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                action=action or "unknown",
                exc=exc,
            )
            format_msg = getattr(self, "_with_conversation_id", None)
            error_text = (
                format_msg(
                    "Flow command failed; check logs for details.",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                )
                if callable(format_msg)
                else "Flow command failed; check logs for details."
            )
            await self._send_message(
                message.chat_id,
                error_text,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        await self._send_message(
            message.chat_id,
            f"Unknown /flow command: {action_raw or action}. Use /flow help.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
        await self._send_flow_help_block(message)
        return

    async def _render_flow_status_callback(
        self,
        callback: TelegramCallbackQuery,
        repo_root: Path,
        run_id_raw: Optional[str],
    ) -> None:
        store = _load_flow_store(repo_root)
        try:
            store.initialize()
            record, error = self._resolve_status_record(store, run_id_raw)
            if error:
                await self._edit_callback_message(
                    callback, error, reply_markup={"inline_keyboard": []}
                )
                return
            text, keyboard = self._build_flow_status_card(repo_root, record, store)
        finally:
            store.close()
        await self._edit_callback_message(callback, text, reply_markup=keyboard)

    async def _handle_flow_callback(
        self, callback: TelegramCallbackQuery, parsed: FlowCallback
    ) -> None:
        if callback.chat_id is None:
            return
        key = await self._resolve_topic_key(callback.chat_id, callback.thread_id)
        record = await self._store.get_topic(key)
        if not record or not record.workspace_path:
            await self._answer_callback(callback, "No workspace bound")
            await self._edit_callback_message(
                callback,
                "No workspace bound. Use /bind to bind this topic to a repo first.",
                reply_markup={"inline_keyboard": []},
            )
            return

        repo_root = canonicalize_path(Path(record.workspace_path))
        action = (parsed.action or "").strip().lower()
        run_id_raw = parsed.run_id

        if action in {"refresh", "status"}:
            await self._answer_callback(callback, "Refreshing...")
            await self._render_flow_status_callback(callback, repo_root, run_id_raw)
            return

        error = None
        notice = None
        if action == "resume":
            store = _load_flow_store(repo_root)
            try:
                store.initialize()
                run_id, error = self._resolve_run_id_input(store, run_id_raw)
                record = store.get_flow_run(run_id) if run_id else None
                if run_id_raw and error:
                    record = None
                if error is None and record is None:
                    record = _select_latest_run(
                        store, lambda run: run.status == FlowRunStatus.PAUSED
                    )
                if error is None and record is None:
                    error = "No paused ticket flow run found."
                if error is None and record.status != FlowRunStatus.PAUSED:
                    error = f"Run {record.id} is {record.status.value}, not paused."
            finally:
                store.close()
            if error is None:
                controller = _get_ticket_controller(repo_root)
                updated = await controller.resume_flow(record.id)
                _spawn_flow_worker(repo_root, updated.id)
                notice = "Resumed."
        elif action == "stop":
            store = _load_flow_store(repo_root)
            try:
                store.initialize()
                run_id, error = self._resolve_run_id_input(store, run_id_raw)
                record = store.get_flow_run(run_id) if run_id else None
                if run_id_raw and error:
                    record = None
                if error is None and record is None:
                    record = _select_latest_run(
                        store, lambda run: run.status.is_active()
                    )
                if error is None and record is None:
                    error = "No active ticket flow run found."
                if error is None and record.status.is_terminal():
                    error = f"Run {record.id} is already {record.status.value}."
            finally:
                store.close()
            if error is None:
                controller = _get_ticket_controller(repo_root)
                self._stop_flow_worker(repo_root, record.id)
                await controller.stop_flow(record.id)
                notice = "Stopped."
        elif action == "recover":
            store = _load_flow_store(repo_root)
            try:
                store.initialize()
                run_id, error = self._resolve_run_id_input(store, run_id_raw)
                record = store.get_flow_run(run_id) if run_id else None
                if run_id_raw and error:
                    record = None
                if error is None and record is None:
                    record = _select_latest_run(
                        store, lambda run: run.status.is_active()
                    )
                if error is None and record is None:
                    error = "No active ticket flow run found."
                if error is None:
                    record, updated, locked = reconcile_flow_run(
                        repo_root, record, store
                    )
                    if locked:
                        error = f"Run {record.id} is locked for reconcile; try again."
                    else:
                        notice = "Recovered." if updated else "No changes needed."
            finally:
                store.close()
        elif action == "archive":
            store = _load_flow_store(repo_root)
            record = None
            try:
                store.initialize()
                run_id, error = self._resolve_run_id_input(store, run_id_raw)
                record = store.get_flow_run(run_id) if run_id else None
                if run_id_raw and error:
                    record = None
                if error is None and record is None:
                    record = _select_latest_run(
                        store,
                        lambda run: run.status.is_terminal()
                        or run.status == FlowRunStatus.PAUSED,
                    )
                if error is None and record is None:
                    error = "No paused or terminal ticket flow run found."
                if error is None and not record.status.is_terminal():
                    if record.status in (FlowRunStatus.STOPPING, FlowRunStatus.PAUSED):
                        self._stop_flow_worker(repo_root, record.id)
                    else:
                        error = "Can only archive completed/stopped/failed runs (use --force for stuck flows)."
            finally:
                store.close()

            if error is None:
                _, artifacts_root = _flow_paths(repo_root)
                archive_dir = artifacts_root / record.id / "archived_tickets"
                archive_dir.mkdir(parents=True, exist_ok=True)
                ticket_dir = _ticket_dir(repo_root)
                for ticket_path in list_ticket_paths(ticket_dir):
                    dest = archive_dir / ticket_path.name
                    shutil.move(str(ticket_path), str(dest))

                runs_dir = Path(
                    record.input_data.get("runs_dir") or ".codex-autorunner/runs"
                )
                outbox_paths = resolve_outbox_paths(
                    workspace_root=repo_root, runs_dir=runs_dir, run_id=record.id
                )
                run_dir = outbox_paths.run_dir
                if run_dir.exists() and run_dir.is_dir():
                    archived_runs_dir = artifacts_root / record.id / "archived_runs"
                    shutil.move(str(run_dir), str(archived_runs_dir))

                store = _load_flow_store(repo_root)
                try:
                    store.initialize()
                    store.delete_flow_run(record.id)
                finally:
                    store.close()
                notice = "Archived."
        elif action == "restart":
            message = TelegramMessage(
                update_id=callback.update_id,
                message_id=callback.message_id or 0,
                chat_id=callback.chat_id,
                thread_id=callback.thread_id,
                from_user_id=callback.from_user_id,
                text=None,
                date=None,
                is_topic_message=callback.thread_id is not None,
            )
            argv = [run_id_raw] if run_id_raw else []
            await self._handle_flow_restart(message, repo_root, argv)
            notice = "Restarted."
        else:
            await self._answer_callback(callback, "Unknown action")
            return

        if error:
            await self._answer_callback(callback, error)
        elif notice:
            await self._answer_callback(callback, notice)
        await self._render_flow_status_callback(callback, repo_root, run_id_raw)

    def _resolve_run_id_input(
        self, store: FlowStore, raw_run_id: Optional[str]
    ) -> tuple[Optional[str], Optional[str]]:
        if not raw_run_id:
            return None, None
        normalized = _normalize_run_id(raw_run_id)
        if normalized:
            return normalized, None
        matches = [
            record.id
            for record in store.list_flow_runs(flow_type="ticket_flow")
            if record.id.startswith(raw_run_id)
        ]
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            return None, "Run ID prefix is ambiguous. Use the full run_id."
        return None, "Invalid run_id."

    def _first_non_flag(self, argv: list[str]) -> Optional[str]:
        for part in argv:
            if not part.startswith("--"):
                return part
        return None

    def _has_flag(self, argv: list[str], name: str) -> bool:
        prefix = f"{name}="
        return any(part == name or part.startswith(prefix) for part in argv)

    def _resolve_status_record(
        self, store: FlowStore, run_id_raw: Optional[str]
    ) -> tuple[Optional[object], Optional[str]]:
        run_id, error = self._resolve_run_id_input(store, run_id_raw)
        if run_id_raw and error:
            return None, error
        record = store.get_flow_run(run_id) if run_id else None
        if record is None:
            runs = store.list_flow_runs(flow_type="ticket_flow")
            record = runs[0] if runs else None
        if record is None:
            return None, "No ticket flow run found. Use /flow bootstrap to start."
        return record, None

    def _format_flow_status_lines(
        self,
        repo_root: Path,
        record: Optional[object],
        store: Optional[FlowStore],
        *,
        health: Optional[FlowWorkerHealth] = None,
        snapshot: Optional[dict] = None,
    ) -> list[str]:
        if record is None:
            return ["Run: none"]
        if snapshot is None:
            snapshot = build_flow_status_snapshot(repo_root, record, store)
        run = record
        status = getattr(run, "status", None)
        status_value = status.value if status else "unknown"
        progress = snapshot.get("ticket_progress") if snapshot else None
        progress_label = None
        if isinstance(progress, dict):
            done = progress.get("done")
            total = progress.get("total")
            if isinstance(done, int) and isinstance(total, int) and total >= 0:
                progress_label = f"{done}/{total}"
        lines = [f"Run: {run.id}", f"Status: {status_value}"]
        if progress_label:
            lines.append(f"Tickets: {progress_label}")
        flow_type = getattr(run, "flow_type", None)
        if flow_type:
            lines.append(f"Flow: {flow_type}")
        created_at = getattr(run, "created_at", None)
        if created_at:
            lines.append(f"Created: {created_at}")
        started_at = getattr(run, "started_at", None)
        if started_at:
            lines.append(f"Started: {started_at}")
        finished_at = getattr(run, "finished_at", None)
        if finished_at:
            lines.append(f"Finished: {finished_at}")
        current_step = getattr(run, "current_step", None)
        if current_step:
            lines.append(f"Step: {current_step}")
        state = run.state or {}
        engine = state.get("ticket_engine") if isinstance(state, dict) else None
        engine = engine if isinstance(engine, dict) else {}
        current = snapshot.get("effective_current_ticket") if snapshot else None
        if isinstance(current, str) and current.strip():
            lines.append(f"Current: {current.strip()}")
        reason_summary = None
        if isinstance(state, dict):
            value = state.get("reason_summary")
            if isinstance(value, str) and value.strip():
                reason_summary = value.strip()
        if reason_summary:
            lines.append(f"Summary: {_truncate_text(reason_summary, 300)}")
        reason = engine.get("reason") if isinstance(engine, dict) else None
        if isinstance(reason, str) and reason.strip():
            if reason_summary and reason.strip() == reason_summary:
                pass
            else:
                lines.append(f"Reason: {_truncate_text(reason.strip(), 300)}")
        error_message = getattr(run, "error_message", None)
        if isinstance(error_message, str) and error_message.strip():
            lines.append(f"Error: {_truncate_text(error_message.strip(), 300)}")
        if snapshot:
            last_seq = snapshot.get("last_event_seq")
            last_at = snapshot.get("last_event_at")
            if last_seq or last_at:
                seq_label = str(last_seq) if last_seq is not None else "?"
                at_label = last_at or "unknown time"
                lines.append(f"Last event: {seq_label} @ {at_label}")
        if health is None:
            health = snapshot.get("worker_health") if snapshot else None
        if health is None:
            return lines
        worker_line = f"Worker: {health.status}"
        if health.pid:
            worker_line += f" (pid {health.pid})"
        if health.message and health.status not in {"alive"}:
            worker_line += f" - {health.message}"
        lines.append(worker_line)
        if status == FlowRunStatus.PAUSED:
            lines.append("Paused: use /flow reply <message>, then /flow resume.")
        return lines

    def _build_flow_status_keyboard(
        self, record: Optional[object], *, health: Optional[FlowWorkerHealth]
    ) -> Optional[dict[str, object]]:
        if record is None or health is None:
            return None
        status = getattr(record, "status", None)
        if status is None:
            return None
        run_id = record.id
        rows: list[list[InlineButton]] = []
        if status == FlowRunStatus.PAUSED:
            rows.append(
                [
                    InlineButton("Resume", encode_flow_callback("resume", run_id)),
                    InlineButton("Restart", encode_flow_callback("restart", run_id)),
                ]
            )
            rows.append(
                [InlineButton("Archive", encode_flow_callback("archive", run_id))]
            )
        elif status.is_terminal():
            rows.append(
                [
                    InlineButton("Restart", encode_flow_callback("restart", run_id)),
                    InlineButton("Archive", encode_flow_callback("archive", run_id)),
                ]
            )
            rows.append(
                [InlineButton("Refresh", encode_flow_callback("refresh", run_id))]
            )
        else:
            if health.status in {"dead", "mismatch", "invalid", "absent"}:
                rows.append(
                    [
                        InlineButton(
                            "Recover", encode_flow_callback("recover", run_id)
                        ),
                        InlineButton(
                            "Refresh", encode_flow_callback("refresh", run_id)
                        ),
                    ]
                )
            elif status == FlowRunStatus.RUNNING:
                rows.append(
                    [
                        InlineButton("Stop", encode_flow_callback("stop", run_id)),
                        InlineButton(
                            "Refresh", encode_flow_callback("refresh", run_id)
                        ),
                    ]
                )
            else:
                rows.append(
                    [InlineButton("Refresh", encode_flow_callback("refresh", run_id))]
                )
        return build_inline_keyboard(rows) if rows else None

    def _build_flow_status_card(
        self, repo_root: Path, record: Optional[object], store: Optional[FlowStore]
    ) -> tuple[str, Optional[dict[str, object]]]:
        if record is None:
            return (
                "\n".join(self._format_flow_status_lines(repo_root, record, store)),
                None,
            )
        snapshot = build_flow_status_snapshot(repo_root, record, store)
        health = snapshot.get("worker_health")
        lines = self._format_flow_status_lines(
            repo_root, record, store, health=health, snapshot=snapshot
        )
        keyboard = self._build_flow_status_keyboard(record, health=health)
        return "\n".join(lines), keyboard

    async def _send_flow_help_block(self, message: TelegramMessage) -> None:
        await self._send_message(
            message.chat_id,
            "\n".join(_flow_help_lines()),
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _send_flow_overview(
        self, message: TelegramMessage, record: Optional[object]
    ) -> None:
        repo_root = (
            canonicalize_path(Path(record.workspace_path))
            if record and record.workspace_path
            else None
        )
        lines = [
            f"Workspace: {repo_root}" if repo_root else "Workspace: unbound",
        ]
        if repo_root:
            store = _load_flow_store(repo_root)
            try:
                store.initialize()
                runs = store.list_flow_runs(flow_type="ticket_flow")
                latest = runs[0] if runs else None
                lines.extend(self._format_flow_status_lines(repo_root, latest, store))
            finally:
                store.close()
        else:
            lines.append("Run: none")
            lines.append("Use /bind <repo_id> or /bind <path>.")
        lines.append("")
        lines.extend(_flow_help_lines())
        await self._send_message(
            message.chat_id,
            "\n".join(lines),
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _send_flow_hub_overview(self, message: TelegramMessage) -> None:
        if not self._manifest_path or not self._hub_root:
            await self._send_message(
                message.chat_id,
                "Hub manifest not configured.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        try:
            manifest = load_manifest(self._manifest_path, self._hub_root)
        except Exception:
            await self._send_message(
                message.chat_id,
                "Failed to load manifest.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        def _group_key(repo_id: str) -> tuple[str, Optional[str]]:
            parts = repo_id.split("--", 1)
            if len(parts) == 1:
                return repo_id, None
            return parts[0], parts[1]

        def _format_status_line(
            label: str,
            *,
            status_icon: str,
            status_value: str,
            progress_label: str,
            run_id: Optional[str],
            indent: str = "",
        ) -> str:
            run_suffix = f" run {run_id}" if run_id else ""
            return f"{indent}{status_icon} {label}: {status_value} {progress_label}{run_suffix}"

        lines = ["Hub Flow Overview:"]
        groups: dict[str, list[tuple[str, str]]] = {}
        group_order: list[str] = []

        entries: list[dict[str, object]] = []
        for repo in manifest.repos:
            if not repo.enabled:
                continue
            repo_root = (self._hub_root / repo.path).resolve()
            group, suffix = _group_key(repo.id)
            label = suffix or repo.id
            indent = "  - " if suffix else ""
            entries.append(
                {
                    "repo_id": repo.id,
                    "repo_root": repo_root,
                    "label": label,
                    "indent": indent,
                    "group": group,
                    "unregistered": False,
                }
            )

        extras = _discover_unregistered_worktrees(manifest, self._hub_root)
        for extra in extras:
            repo_id = str(extra["repo_id"])
            group, _ = _group_key(repo_id)
            extra["group"] = group
            entries.append(extra)

        for entry in entries:
            repo_id = str(entry["repo_id"])
            repo_root = Path(entry["repo_root"])
            label = str(entry["label"])
            indent = str(entry.get("indent", ""))
            group = str(entry.get("group", repo_id))
            if group not in groups:
                groups[group] = []
                group_order.append(group)

            store = _load_flow_store(repo_root)
            try:
                store.initialize()
                progress = ticket_progress(repo_root)
                done = progress.get("done", 0)
                total = progress.get("total", 0)
                progress_label = f"{done}/{total}"
                active = _select_latest_run(store, lambda run: run.status.is_active())
                if active:
                    status_icon = (
                        "🟢" if active.status == FlowRunStatus.RUNNING else "🟡"
                    )
                    status_line = _format_status_line(
                        label,
                        status_icon=status_icon,
                        status_value=active.status.value,
                        progress_label=progress_label,
                        run_id=active.id,
                        indent=indent,
                    )
                else:
                    paused = _select_latest_run(
                        store, lambda run: run.status == FlowRunStatus.PAUSED
                    )
                    if paused:
                        status_line = _format_status_line(
                            label,
                            status_icon="🔴",
                            status_value="PAUSED",
                            progress_label=progress_label,
                            run_id=paused.id,
                            indent=indent,
                        )
                    else:
                        status_line = _format_status_line(
                            label,
                            status_icon="⚪",
                            status_value="Idle",
                            progress_label=progress_label,
                            run_id=None,
                            indent=indent,
                        )
            except Exception:
                status_line = f"{indent}❓ {label}: Error reading state"
            finally:
                store.close()

            groups[group].append((label, status_line))

        for group in group_order:
            entries = groups.get(group, [])
            if not entries:
                continue
            entries.sort(key=lambda pair: (0 if pair[0] == group else 1, pair[0]))
            lines.extend([line for _label, line in entries])
            lines.append("")

        if lines and lines[-1] == "":
            lines.pop()
        if extras:
            lines.append("")
            lines.append(
                "Note: Unregistered worktrees detected. Run 'car hub scan' to register them."
            )
        lines.append("")
        lines.append("Tip: use /flow <repo-id> status for repo details.")

        await self._send_message(
            message.chat_id,
            "\n".join(lines),
            thread_id=message.thread_id,
            reply_to=message.message_id,
            parse_mode=None,
        )

    async def _handle_flow_status_action(
        self, message: TelegramMessage, repo_root: Path, argv: list[str]
    ) -> None:
        store = _load_flow_store(repo_root)
        try:
            store.initialize()
            run_id_raw = self._first_non_flag(argv)
            record, error = self._resolve_status_record(store, run_id_raw)
            if error:
                await self._send_message(
                    message.chat_id,
                    error,
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            text, keyboard = self._build_flow_status_card(repo_root, record, store)
        finally:
            store.close()
        await self._send_message(
            message.chat_id,
            text,
            thread_id=message.thread_id,
            reply_to=message.message_id,
            reply_markup=keyboard,
        )

    async def _handle_flow_runs(
        self, message: TelegramMessage, repo_root: Path, argv: list[str]
    ) -> None:
        limit = 5
        limit_raw = self._first_non_flag(argv)
        if limit_raw:
            limit_value = self._coerce_int(limit_raw)
            if limit_value is None or limit_value <= 0:
                await self._send_message(
                    message.chat_id,
                    "Provide a positive integer for /flow runs [N].",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            limit = min(limit_value, 50)

        store = _load_flow_store(repo_root)
        try:
            store.initialize()
            runs = store.list_flow_runs(flow_type="ticket_flow")
        finally:
            store.close()

        if not runs:
            await self._send_message(
                message.chat_id,
                "No ticket flow runs found. Use /flow bootstrap to start.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        items: list[tuple[str, str]] = []
        button_labels: dict[str, str] = {}
        for run in runs[:limit]:
            created_at = getattr(run, "created_at", None) or "unknown"
            status = getattr(run, "status", None)
            status_label = status.value if status is not None else "unknown"
            items.append((run.id, f"{status_label} • {created_at}"))
            short_id = run.id.split("-")[0]
            button_label = f"{short_id} {status_label}"
            button_labels[run.id] = _truncate_text(button_label, 32)

        state = SelectionState(items=items, button_labels=button_labels)
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        self._flow_run_options[key] = state
        self._touch_cache_timestamp("flow_run_options", key)
        prompt = self._flow_runs_prompt(state)
        keyboard = self._build_flow_runs_keyboard(state)
        await self._send_message(
            message.chat_id,
            prompt,
            thread_id=message.thread_id,
            reply_to=message.message_id,
            reply_markup=keyboard,
        )

    async def _handle_flow_bootstrap(
        self, message: TelegramMessage, repo_root: Path, argv: list[str]
    ) -> None:
        force_new = self._has_flag(argv, "--force-new") or self._has_flag(
            argv, "--force"
        )
        ticket_dir = _ticket_dir(repo_root)
        ticket_dir.mkdir(parents=True, exist_ok=True)
        existing_tickets = list_ticket_paths(ticket_dir)
        tickets_exist = bool(existing_tickets)
        issue_exists = issue_md_has_content(repo_root)

        store = _load_flow_store(repo_root)
        active_run = None
        try:
            store.initialize()
            runs = store.list_flow_runs(flow_type="ticket_flow")
            for record in runs:
                if record.status in (FlowRunStatus.RUNNING, FlowRunStatus.PAUSED):
                    active_run = record
                    break
        finally:
            store.close()

        if not force_new and active_run:
            _spawn_flow_worker(repo_root, active_run.id)
            await self._send_message(
                message.chat_id,
                f"Reusing ticket flow run {active_run.id} ({active_run.status.value}).",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        if not tickets_exist and not issue_exists:
            gh_available, repo_slug = self._github_bootstrap_status(repo_root)
            if gh_available:
                repo_label = f" for {repo_slug}" if repo_slug else ""
                prompt = (
                    f"Enter GitHub issue number or URL{repo_label} to seed ISSUE.md:"
                )
                issue_ref = await self._prompt_flow_text_input(message, prompt)
                if not issue_ref:
                    await self._send_message(
                        message.chat_id,
                        "Bootstrap cancelled (no issue provided).",
                        thread_id=message.thread_id,
                        reply_to=message.message_id,
                    )
                    return
                try:
                    number, _repo = await self._seed_issue_from_ref(
                        repo_root, issue_ref
                    )
                except GitHubError as exc:
                    await self._send_message(
                        message.chat_id,
                        f"GitHub error: {exc}",
                        thread_id=message.thread_id,
                        reply_to=message.message_id,
                    )
                    return
                except Exception as exc:
                    await self._send_message(
                        message.chat_id,
                        f"Failed to fetch issue: {exc}",
                        thread_id=message.thread_id,
                        reply_to=message.message_id,
                    )
                    return
                await self._send_message(
                    message.chat_id,
                    f"Seeded ISSUE.md from GitHub issue {number}.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                issue_exists = True
            else:
                prompt = "Describe the work to seed ISSUE.md:"
                plan_text = await self._prompt_flow_text_input(message, prompt)
                if not plan_text:
                    await self._send_message(
                        message.chat_id,
                        "Bootstrap cancelled (no description provided).",
                        thread_id=message.thread_id,
                        reply_to=message.message_id,
                    )
                    return
                self._seed_issue_from_plan(repo_root, plan_text)
                await self._send_message(
                    message.chat_id,
                    "Seeded ISSUE.md from your plan.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                issue_exists = True

        seeded = False
        if not tickets_exist:
            first_ticket = ticket_dir / "TICKET-001.md"
            if not first_ticket.exists():
                template = """---
agent: codex
done: false
title: Bootstrap ticket plan
goal: Capture scope and seed follow-up tickets
---

You are the first ticket in a new ticket_flow run.

- Read `.codex-autorunner/ISSUE.md`. If it is missing:
  - If GitHub is available, ask the user for the issue/PR URL or number and create `.codex-autorunner/ISSUE.md` from it.
  - If GitHub is not available, write `DISPATCH.md` with `mode: pause` asking the user to describe the work (or share a doc). After the reply, create `.codex-autorunner/ISSUE.md` with their input.
- If helpful, create or update contextspace docs under `.codex-autorunner/contextspace/`:
  - `active_context.md` for current context and links
  - `decisions.md` for decisions/rationale
  - `spec.md` for requirements and constraints
- Break the work into additional `TICKET-00X.md` files with clear owners/goals; keep this ticket open until they exist.
- Place any supporting artifacts in `.codex-autorunner/runs/<run_id>/dispatch/` if needed.
- Write `DISPATCH.md` to dispatch a message to the user:
  - Use `mode: pause` (handoff) to wait for user response. This pauses execution.
  - Use `mode: notify` (informational) to message the user but keep running.
"""
                first_ticket.write_text(template, encoding="utf-8")
                seeded = True

        controller = _get_ticket_controller(repo_root)
        flow_record = await controller.start_flow(
            input_data={},
            metadata={"seeded_ticket": seeded, "origin": "telegram"},
        )
        _spawn_flow_worker(repo_root, flow_record.id)

        if not issue_exists and not tickets_exist:
            await self._send_flow_issue_hint(message, repo_root)

        await self._send_message(
            message.chat_id,
            f"Started ticket flow run {flow_record.id}.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _send_flow_issue_hint(
        self, message: TelegramMessage, repo_root: Path
    ) -> None:
        gh_status = (
            "No ISSUE.md found. Use /flow plan <text> to seed it from a short plan."
        )
        gh_available, repo_slug = self._github_bootstrap_status(repo_root)
        if gh_available:
            repo_label = repo_slug or "your repo"
            gh_status = (
                f"No ISSUE.md found. Use /flow issue <issue#|url> for {repo_label}, "
                "or /flow plan <text>."
            )
        await self._send_message(
            message.chat_id,
            gh_status,
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_flow_issue(
        self, message: TelegramMessage, repo_root: Path, issue_ref: str
    ) -> None:
        issue_ref = issue_ref.strip()
        if not issue_ref:
            await self._send_message(
                message.chat_id,
                "Provide an issue reference: /flow issue <issue#|url>",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        try:
            number, _repo = await self._seed_issue_from_ref(repo_root, issue_ref)
        except GitHubError as exc:
            await self._send_message(
                message.chat_id,
                f"GitHub error: {exc}",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        except RuntimeError as exc:
            await self._send_message(
                message.chat_id,
                str(exc),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        except Exception as exc:
            await self._send_message(
                message.chat_id,
                f"Failed to fetch issue: {exc}",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        await self._send_message(
            message.chat_id,
            f"Seeded ISSUE.md from GitHub issue {number}.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_flow_plan(
        self, message: TelegramMessage, repo_root: Path, plan_text: str
    ) -> None:
        plan_text = plan_text.strip()
        if not plan_text:
            await self._send_message(
                message.chat_id,
                "Provide a plan: /flow plan <text>",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        self._seed_issue_from_plan(repo_root, plan_text)
        await self._send_message(
            message.chat_id,
            "Seeded ISSUE.md from your plan.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_flow_resume(
        self, message: TelegramMessage, repo_root: Path, argv: list[str]
    ) -> None:
        store = _load_flow_store(repo_root)
        try:
            store.initialize()
            run_id_raw = self._first_non_flag(argv)
            run_id, error = self._resolve_run_id_input(store, run_id_raw)
            record = store.get_flow_run(run_id) if run_id else None
            if run_id_raw and error:
                await self._send_message(
                    message.chat_id,
                    error,
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            if record is None:
                record = _select_latest_run(
                    store, lambda run: run.status == FlowRunStatus.PAUSED
                )
            if record is None:
                await self._send_message(
                    message.chat_id,
                    "No paused ticket flow run found.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            if record.status != FlowRunStatus.PAUSED:
                await self._send_message(
                    message.chat_id,
                    f"Run {record.id} is {record.status.value}, not paused.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
        finally:
            store.close()

        controller = _get_ticket_controller(repo_root)
        updated = await controller.resume_flow(record.id)
        _spawn_flow_worker(repo_root, updated.id)
        await self._send_message(
            message.chat_id,
            f"Resumed run {updated.id}.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    def _stop_flow_worker(self, repo_root: Path, run_id: str) -> None:
        health = check_worker_health(repo_root, run_id)
        if health.is_alive and health.pid:
            try:
                subprocess.run(["kill", str(health.pid)], check=False)
            except Exception as exc:
                _logger.warning("Failed to stop worker %s: %s", run_id, exc)
        if health.status in {"dead", "mismatch", "invalid"}:
            clear_worker_metadata(health.artifact_path.parent)

    async def _handle_flow_stop(
        self, message: TelegramMessage, repo_root: Path, argv: list[str]
    ) -> None:
        store = _load_flow_store(repo_root)
        try:
            store.initialize()
            run_id_raw = self._first_non_flag(argv)
            run_id, error = self._resolve_run_id_input(store, run_id_raw)
            record = store.get_flow_run(run_id) if run_id else None
            if run_id_raw and error:
                await self._send_message(
                    message.chat_id,
                    error,
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            if record is None:
                record = _select_latest_run(store, lambda run: run.status.is_active())
            if record is None:
                await self._send_message(
                    message.chat_id,
                    "No active ticket flow run found.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            if record.status.is_terminal():
                await self._send_message(
                    message.chat_id,
                    f"Run {record.id} is already {record.status.value}.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
        finally:
            store.close()

        controller = _get_ticket_controller(repo_root)
        self._stop_flow_worker(repo_root, record.id)
        updated = await controller.stop_flow(record.id)
        await self._send_message(
            message.chat_id,
            f"Stopped run {updated.id} ({updated.status.value}).",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_flow_recover(
        self, message: TelegramMessage, repo_root: Path, argv: list[str]
    ) -> None:
        store = _load_flow_store(repo_root)
        try:
            store.initialize()
            run_id_raw = self._first_non_flag(argv)
            run_id, error = self._resolve_run_id_input(store, run_id_raw)
            record = store.get_flow_run(run_id) if run_id else None
            if run_id_raw and error:
                await self._send_message(
                    message.chat_id,
                    error,
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            if record is None:
                record = _select_latest_run(store, lambda run: run.status.is_active())
            if record is None:
                await self._send_message(
                    message.chat_id,
                    "No active ticket flow run found.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            record, updated, locked = reconcile_flow_run(repo_root, record, store)
            if locked:
                await self._send_message(
                    message.chat_id,
                    f"Run {record.id} is locked for reconcile; try again.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            hint = "Recovered" if updated else "No changes needed"
            lines = [f"{hint} for run {record.id}."]
            lines.extend(self._format_flow_status_lines(repo_root, record, store))
        finally:
            store.close()

        await self._send_message(
            message.chat_id,
            "\n".join(lines),
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_flow_restart(
        self,
        message: TelegramMessage,
        repo_root: Path,
        argv: Optional[list[str]] = None,
    ) -> None:
        argv = argv or []
        store = _load_flow_store(repo_root)
        record = None
        try:
            store.initialize()
            run_id_raw = self._first_non_flag(argv)
            if run_id_raw:
                run_id, error = self._resolve_run_id_input(store, run_id_raw)
                if error is None and run_id:
                    record = store.get_flow_run(run_id)
            else:
                record = _select_latest_run(store, lambda run: run.status.is_active())
        finally:
            store.close()
        if record and not record.status.is_terminal():
            controller = _get_ticket_controller(repo_root)
            self._stop_flow_worker(repo_root, record.id)
            await controller.stop_flow(record.id)
        await self._handle_flow_bootstrap(message, repo_root, argv=["--force-new"])

    async def _handle_flow_archive(
        self, message: TelegramMessage, repo_root: Path, argv: list[str]
    ) -> None:
        force = self._has_flag(argv, "--force")
        store = _load_flow_store(repo_root)
        record = None
        try:
            store.initialize()
            run_id_raw = self._first_non_flag(argv)
            run_id, error = self._resolve_run_id_input(store, run_id_raw)
            record = store.get_flow_run(run_id) if run_id else None
            if run_id_raw and error:
                await self._send_message(
                    message.chat_id,
                    error,
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            if record is None:
                record = _select_latest_run(
                    store,
                    lambda run: run.status.is_terminal()
                    or run.status == FlowRunStatus.PAUSED
                    or (force and run.status == FlowRunStatus.STOPPING),
                )
            if record is None:
                await self._send_message(
                    message.chat_id,
                    "No paused or terminal ticket flow run found.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            if not record.status.is_terminal():
                if record.status in (FlowRunStatus.STOPPING, FlowRunStatus.PAUSED):
                    self._stop_flow_worker(repo_root, record.id)
                else:
                    await self._send_message(
                        message.chat_id,
                        "Can only archive completed/stopped/failed runs (use --force for stuck flows).",
                        thread_id=message.thread_id,
                        reply_to=message.message_id,
                    )
                    return
        finally:
            store.close()

        _, artifacts_root = _flow_paths(repo_root)
        archive_dir = artifacts_root / record.id / "archived_tickets"
        archive_dir.mkdir(parents=True, exist_ok=True)
        ticket_dir = _ticket_dir(repo_root)
        archived_count = 0
        for ticket_path in list_ticket_paths(ticket_dir):
            dest = archive_dir / ticket_path.name
            shutil.move(str(ticket_path), str(dest))
            archived_count += 1

        runs_dir = Path(record.input_data.get("runs_dir") or ".codex-autorunner/runs")
        outbox_paths = resolve_outbox_paths(
            workspace_root=repo_root, runs_dir=runs_dir, run_id=record.id
        )
        run_dir = outbox_paths.run_dir
        if run_dir.exists() and run_dir.is_dir():
            archived_runs_dir = artifacts_root / record.id / "archived_runs"
            shutil.move(str(run_dir), str(archived_runs_dir))

        store = _load_flow_store(repo_root)
        try:
            store.initialize()
            store.delete_flow_run(record.id)
        finally:
            store.close()

        await self._send_message(
            message.chat_id,
            f"Archived run {record.id} ({archived_count} tickets).",
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
                "Provide a reply: /flow reply <message> (or /reply <message>).",
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
