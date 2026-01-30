from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Optional

from .....agents.registry import validate_agent_id
from .....core.config import load_repo_config
from .....core.engine import Engine
from .....core.flows import FlowController, FlowStore
from .....core.flows.models import FlowRunStatus
from .....core.flows.reconciler import reconcile_flow_run
from .....core.flows.worker_process import (
    check_worker_health,
    clear_worker_metadata,
    spawn_flow_worker,
)
from .....core.state import now_iso
from .....core.utils import atomic_write, canonicalize_path
from .....flows.ticket_flow import build_ticket_flow_definition
from .....integrations.agents.wiring import (
    build_agent_backend_factory,
    build_app_server_supervisor_factory,
)
from .....tickets import AgentPool
from .....tickets.files import list_ticket_paths
from .....tickets.outbox import resolve_outbox_paths
from ....github.service import GitHubError, GitHubService
from ...adapter import (
    InlineButton,
    TelegramMessage,
    build_inline_keyboard,
    encode_question_cancel_callback,
)
from ...config import DEFAULT_APPROVAL_TIMEOUT_SECONDS
from ...helpers import _truncate_text
from ...types import PendingQuestion
from .shared import SharedHelpers

_logger = logging.getLogger(__name__)


def _flow_paths(repo_root: Path) -> tuple[Path, Path]:
    repo_root = repo_root.resolve()
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    artifacts_root = repo_root / ".codex-autorunner" / "flows"
    return db_path, artifacts_root


def _issue_md_path(repo_root: Path) -> Path:
    return repo_root.resolve() / ".codex-autorunner" / "ISSUE.md"


def _ticket_dir(repo_root: Path) -> Path:
    return repo_root.resolve() / ".codex-autorunner" / "tickets"


def _issue_md_has_content(repo_root: Path) -> bool:
    issue_path = _issue_md_path(repo_root)
    if not issue_path.exists():
        return False
    try:
        return bool(issue_path.read_text(encoding="utf-8").strip())
    except OSError:
        return False


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
        "/flow bootstrap [--force-new]",
        "/flow issue <issue#|url>",
        "/flow plan <text>",
        "/flow resume [run_id]",
        "/flow stop [run_id]",
        "/flow recover [run_id]",
        "/flow restart",
        "/flow archive [run_id] [--force]",
        "/flow reply <message>",
        "Aliases: /flow start, /flow_status",
    ]


def _format_issue_as_markdown(issue: dict, repo_slug: Optional[str] = None) -> str:
    number = issue.get("number")
    title = issue.get("title") or ""
    url = issue.get("url") or ""
    state = issue.get("state") or ""
    author = issue.get("author") or {}
    author_name = (
        author.get("login")
        if isinstance(author, dict)
        else str(author or "unknown").strip()
    )
    labels = issue.get("labels")
    label_names = []
    if isinstance(labels, list):
        for label in labels:
            name = ""
            if isinstance(label, dict):
                name = label.get("name") or ""
            else:
                name = str(label or "")
            if name:
                label_names.append(str(name))
    comments = issue.get("comments")
    comment_count = None
    if isinstance(comments, dict):
        total = comments.get("totalCount")
        if isinstance(total, int):
            comment_count = total

    body = issue.get("body") or "(no description)"
    lines = [
        f"# Issue #{number}: {title}".strip(),
        "",
        f"**Repo:** {repo_slug or 'unknown'}",
        f"**URL:** {url}",
        f"**State:** {state}",
        f"**Author:** {author_name}",
    ]
    if label_names:
        lines.append(f"**Labels:** {', '.join(label_names)}")
    if comment_count is not None:
        lines.append(f"**Comments:** {comment_count}")
    lines.extend(["", "## Description", "", str(body).strip(), ""])
    return "\n".join(lines)


def _get_ticket_controller(repo_root: Path) -> FlowController:
    db_path, artifacts_root = _flow_paths(repo_root)
    config = load_repo_config(repo_root)
    engine = Engine(
        repo_root,
        config=config,
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
    def _github_bootstrap_status(self, repo_root: Path) -> tuple[bool, Optional[str]]:
        try:
            gh = GitHubService(repo_root=repo_root)
            gh_available = gh.gh_available() and gh.gh_authenticated()
            if gh_available:
                repo_info = gh.repo_info()
                return True, repo_info.name_with_owner
        except Exception:
            pass
        return False, None

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
        gh = GitHubService(repo_root=repo_root)
        if not (gh.gh_available() and gh.gh_authenticated()):
            raise RuntimeError(
                "GitHub CLI is not available or not authenticated. Use /flow plan <text> instead."
            )
        number = gh.validate_issue_same_repo(issue_ref)
        issue = gh.issue_view(number=number)
        repo_info = gh.repo_info()
        content = _format_issue_as_markdown(issue, repo_info.name_with_owner)
        atomic_write(_issue_md_path(repo_root), content)
        return number, repo_info.name_with_owner

    def _seed_issue_from_plan(self, repo_root: Path, plan_text: str) -> None:
        content = f"# Issue\n\n{plan_text.strip()}\n"
        atomic_write(_issue_md_path(repo_root), content)

    async def _handle_flow_status(self, message: TelegramMessage, args: str) -> None:
        text = args.strip()
        if text:
            await self._handle_flow(message, f"status {text}")
        else:
            await self._handle_flow(message, "status")

    async def _handle_flow(self, message: TelegramMessage, args: str) -> None:
        argv = self._parse_command_args(args)
        action_raw = argv[0] if argv else ""
        action = _normalize_flow_action(action_raw)
        _, remainder = _split_flow_action(args)
        rest_argv = argv[1:]

        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        record = await self._store.get_topic(key)

        if action == "help":
            await self._send_flow_overview(message, record)
            return

        if not record or not record.workspace_path:
            await self._send_message(
                message.chat_id,
                "No workspace bound. Use /bind to bind this topic to a repo first.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        repo_root = canonicalize_path(Path(record.workspace_path))

        if action == "status":
            await self._handle_flow_status_action(message, repo_root, rest_argv)
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
            await self._handle_flow_restart(message, repo_root)
            return
        if action == "archive":
            await self._handle_flow_archive(message, repo_root, rest_argv)
            return
        if action == "reply":
            await self._handle_reply(message, remainder)
            return

        await self._send_message(
            message.chat_id,
            f"Unknown /flow command: {action_raw or action}. Use /flow help.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
        await self._send_flow_help_block(message)
        return

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

    def _format_flow_status_lines(
        self, repo_root: Path, record: Optional[object], store: Optional[FlowStore]
    ) -> list[str]:
        if record is None:
            return ["Run: none"]
        run = record
        status = getattr(run, "status", None)
        status_value = status.value if status else "unknown"
        lines = [f"Run: {run.id}", f"Status: {status_value}"]
        state = run.state or {}
        engine = state.get("ticket_engine") if isinstance(state, dict) else None
        engine = engine if isinstance(engine, dict) else {}
        current = engine.get("current_ticket")
        if not (isinstance(current, str) and current.strip()) and store:
            effective = store.get_latest_step_progress_current_ticket(run.id)
            if effective:
                current = effective
        if current:
            lines.append(f"Current: {current}")
        reason = engine.get("reason") if isinstance(engine, dict) else None
        if not reason:
            reason = run.error_message or ""
        if reason:
            lines.append(f"Reason: {_truncate_text(str(reason), 300)}")
        if store:
            last_seq, last_at = store.get_last_event_meta(run.id)
            if last_seq or last_at:
                seq_label = str(last_seq) if last_seq is not None else "?"
                at_label = last_at or "unknown time"
                lines.append(f"Last event: {seq_label} @ {at_label}")
        health = check_worker_health(repo_root, run.id)
        worker_line = f"Worker: {health.status}"
        if health.pid:
            worker_line += f" (pid {health.pid})"
        if health.message and health.status not in {"alive"}:
            worker_line += f" - {health.message}"
        lines.append(worker_line)
        if status == FlowRunStatus.PAUSED:
            lines.append("Paused: use /flow reply <message>, then /flow resume.")
        return lines

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
            store = FlowStore(_flow_paths(repo_root)[0])
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

    async def _handle_flow_status_action(
        self, message: TelegramMessage, repo_root: Path, argv: list[str]
    ) -> None:
        store = FlowStore(_flow_paths(repo_root)[0])
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
                runs = store.list_flow_runs(flow_type="ticket_flow")
                record = runs[0] if runs else None
            if record is None:
                await self._send_message(
                    message.chat_id,
                    "No ticket flow run found. Use /flow bootstrap to start.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            lines = self._format_flow_status_lines(repo_root, record, store)
        finally:
            store.close()
        await self._send_message(
            message.chat_id,
            "\n".join(lines),
            thread_id=message.thread_id,
            reply_to=message.message_id,
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
        issue_exists = _issue_md_has_content(repo_root)

        store = FlowStore(_flow_paths(repo_root)[0])
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
                    "Enter GitHub issue number or URL" f"{repo_label} to seed ISSUE.md:"
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
- If helpful, create or update workspace docs under `.codex-autorunner/workspace/`:
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
        store = FlowStore(_flow_paths(repo_root)[0])
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
                paused_runs = store.list_flow_runs(
                    flow_type="ticket_flow", status=FlowRunStatus.PAUSED
                )
                record = paused_runs[0] if paused_runs else None
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
        store = FlowStore(_flow_paths(repo_root)[0])
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
                runs = store.list_flow_runs(flow_type="ticket_flow")
                record = runs[0] if runs else None
            if record is None:
                await self._send_message(
                    message.chat_id,
                    "No ticket flow run found.",
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
        store = FlowStore(_flow_paths(repo_root)[0])
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
                runs = store.list_flow_runs(flow_type="ticket_flow")
                record = runs[0] if runs else None
            if record is None:
                await self._send_message(
                    message.chat_id,
                    "No ticket flow run found.",
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
        self, message: TelegramMessage, repo_root: Path
    ) -> None:
        store = FlowStore(_flow_paths(repo_root)[0])
        latest = None
        try:
            store.initialize()
            runs = store.list_flow_runs(flow_type="ticket_flow")
            latest = runs[0] if runs else None
        finally:
            store.close()
        if latest and not latest.status.is_terminal():
            controller = _get_ticket_controller(repo_root)
            self._stop_flow_worker(repo_root, latest.id)
            await controller.stop_flow(latest.id)
        await self._handle_flow_bootstrap(message, repo_root, argv=["--force-new"])

    async def _handle_flow_archive(
        self, message: TelegramMessage, repo_root: Path, argv: list[str]
    ) -> None:
        force = self._has_flag(argv, "--force")
        store = FlowStore(_flow_paths(repo_root)[0])
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
                runs = store.list_flow_runs(flow_type="ticket_flow")
                record = runs[0] if runs else None
            if record is None:
                await self._send_message(
                    message.chat_id,
                    "No ticket flow run found.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            if not record.status.is_terminal():
                if force and record.status in (
                    FlowRunStatus.STOPPING,
                    FlowRunStatus.PAUSED,
                ):
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

        store = FlowStore(_flow_paths(repo_root)[0])
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
