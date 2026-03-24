from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from .....core.config import ConfigError, load_repo_config
from .....core.flows import (
    FLOW_ACTION_TOKENS,
    FlowStore,
    flow_duration_seconds,
    flow_help_lines,
    flow_run_duration_seconds,
    format_flow_duration,
    normalize_flow_action,
)
from .....core.flows.hub_overview import build_hub_flow_overview_entries
from .....core.flows.models import FlowRunStatus
from .....core.flows.reconciler import reconcile_flow_run
from .....core.flows.surface_defaults import should_route_flow_read_to_hub_overview
from .....core.flows.ux_helpers import (
    bootstrap_check,
    build_flow_status_snapshot,
    issue_md_has_content,
    issue_md_path,
    resolve_ticket_flow_archive_mode,
    seed_issue_from_github,
    seed_issue_from_text,
    select_default_ticket_flow_run,
    summarize_flow_freshness,
    ticket_flow_archive_requires_force,
    ticket_progress,
)
from .....core.flows.worker_process import FlowWorkerHealth, check_worker_health
from .....core.logging_utils import log_event
from .....core.orchestration import build_ticket_flow_orchestration_service
from .....core.state import now_iso
from .....core.ticket_flow_summary import build_ticket_flow_display
from .....core.utils import atomic_write, canonicalize_path
from .....manifest import load_manifest
from .....tickets.files import list_ticket_paths
from .....tickets.frontmatter import generate_ticket_id
from ....chat.run_mirror import ChatRunMirror
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
_FLOW_REPO_CONTEXT_CACHE_MAX = 512


def _flow_paths(repo_root: Path) -> tuple[Path, Path]:
    repo_root = repo_root.resolve()
    db_path = repo_root / ".codex-autorunner" / "flows.db"
    artifacts_root = repo_root / ".codex-autorunner" / "flows"
    return db_path, artifacts_root


def _ticket_dir(repo_root: Path) -> Path:
    return repo_root.resolve() / ".codex-autorunner" / "tickets"


def _load_flow_store(repo_root: Path, hub_root: Optional[Path] = None) -> FlowStore:
    try:
        config = load_repo_config(repo_root, hub_root)
        durable_writes = config.durable_writes
    except ConfigError:
        durable_writes = False
    return FlowStore(_flow_paths(repo_root)[0], durable=durable_writes)


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


def _flow_help_lines() -> list[str]:
    lines = flow_help_lines(
        prefix="/flow",
        usage_overrides={
            "start": "[--force-new]",
            "reply": "<message>",
        },
    )
    lines.append("Use /pma to access full flow controls via web app.")
    return lines


def _code(value: object) -> str:
    return f"`{value}`"


def _worktree_suffix(repo_id: str) -> Optional[str]:
    parts = [part for part in repo_id.split("--") if part]
    if len(parts) <= 1:
        return None
    return parts[-1]


def _select_latest_run(
    store: FlowStore, predicate: Callable[[object], bool]
) -> Optional[object]:
    for record in store.list_flow_runs(flow_type="ticket_flow"):
        if predicate(record):
            return record
    return None


class FlowCommands(SharedHelpers):
    def _flow_run_mirror(self, repo_root: Path) -> ChatRunMirror:
        return ChatRunMirror(repo_root, logger_=_logger)

    def _ticket_flow_orchestration_service(self, repo_root: Path):
        return build_ticket_flow_orchestration_service(workspace_root=repo_root)

    def _flow_repo_context_cache(self) -> dict[str, str]:
        cache = getattr(self, "_flow_repo_context", None)
        if isinstance(cache, dict):
            return cache
        cache = {}
        self._flow_repo_context = cache
        return cache

    @staticmethod
    def _flow_archive_prompt_text(record: object) -> str:
        run_id = getattr(record, "id", "unknown")
        status = getattr(getattr(record, "status", None), "value", "unknown")
        return (
            f"Run {_code(run_id)} is {status}. Archiving it will reset the live "
            "tickets/contextspace state and move the current run artifacts into the "
            "archive. Archive it anyway?"
        )

    def _build_flow_archive_confirmation_keyboard(
        self,
        run_id: str,
        *,
        repo_id: Optional[str] = None,
        prompt_variant: bool,
    ) -> dict[str, Any]:
        confirm_action = (
            "archive_confirm_prompt" if prompt_variant else "archive_confirm"
        )
        cancel_action = "archive_cancel_prompt" if prompt_variant else "archive_cancel"
        return build_inline_keyboard(
            [
                [
                    InlineButton(
                        "Archive now",
                        encode_flow_callback(confirm_action, run_id, repo_id=repo_id),
                    ),
                    InlineButton(
                        "Cancel",
                        encode_flow_callback(cancel_action, run_id, repo_id=repo_id),
                    ),
                ]
            ]
        )

    def _resolve_flow_archive_record(
        self,
        store: FlowStore,
        run_id_raw: Optional[str],
    ) -> tuple[Optional[object], Optional[str]]:
        run_id, error = self._resolve_run_id_input(store, run_id_raw)
        record = store.get_flow_run(run_id) if run_id else None
        if run_id_raw and error:
            return None, error
        if error is None and record is None:
            record = select_default_ticket_flow_run(store)
        if error is None and record is None:
            error = "No ticket flow run found."
        return record, error

    def _remember_flow_repo_context(
        self, run_id: Optional[str], repo_id: Optional[str]
    ) -> None:
        run_id = str(run_id or "").strip()
        repo_id = str(repo_id or "").strip()
        if not run_id or not repo_id:
            return
        cache = self._flow_repo_context_cache()
        cache.pop(run_id, None)
        cache[run_id] = repo_id
        while len(cache) > _FLOW_REPO_CONTEXT_CACHE_MAX:
            oldest_run_id = next(iter(cache))
            cache.pop(oldest_run_id, None)

    def _resolve_flow_repo_context(
        self, run_id: Optional[str], repo_id: Optional[str]
    ) -> Optional[str]:
        repo_id = str(repo_id or "").strip() or None
        if repo_id:
            self._remember_flow_repo_context(run_id, repo_id)
            return repo_id
        run_id = str(run_id or "").strip()
        if not run_id:
            return None
        return self._flow_repo_context_cache().get(run_id)

    def _flow_manifest_repos(self) -> list[object]:
        manifest_path = getattr(self, "_manifest_path", None)
        hub_root = getattr(self, "_hub_root", None)
        if not manifest_path or not hub_root:
            return []
        try:
            manifest = load_manifest(manifest_path, hub_root)
        except Exception:
            return []
        return [repo for repo in manifest.repos if getattr(repo, "enabled", True)]

    def _flow_repo_base_id(self, repo: object) -> Optional[str]:
        worktree_of = getattr(repo, "worktree_of", None)
        if isinstance(worktree_of, str) and worktree_of.strip():
            return worktree_of.strip()
        repo_id = getattr(repo, "id", None)
        if not isinstance(repo_id, str) or not repo_id.strip():
            return None
        repo_id = repo_id.strip()
        if "--" in repo_id:
            return repo_id.split("--", 1)[0]
        return repo_id

    def _flow_repo_aliases(self, repo: object) -> set[str]:
        aliases: set[str] = set()
        repo_id = getattr(repo, "id", None)
        if isinstance(repo_id, str) and repo_id.strip():
            repo_id = repo_id.strip()
            aliases.add(repo_id.lower())
            if "--" in repo_id:
                _, suffix = repo_id.split("--", 1)
                if suffix:
                    aliases.add(suffix.lower())
            leaf = _worktree_suffix(repo_id)
            if leaf:
                aliases.add(leaf.lower())
        for attr in ("display_name", "branch", "worktree_of"):
            value = getattr(repo, attr, None)
            if isinstance(value, str) and value.strip():
                aliases.add(value.strip().lower())
        return aliases

    def _flow_matching_manifest_repos(
        self, token: str, repos: list[object]
    ) -> list[object]:
        normalized = (token or "").strip().lower()
        if not normalized:
            return []
        return [repo for repo in repos if normalized in self._flow_repo_aliases(repo)]

    def _flow_manifest_repo_id(self, repo: object) -> Optional[str]:
        repo_id = getattr(repo, "id", None)
        if not isinstance(repo_id, str):
            return None
        repo_id = repo_id.strip()
        return repo_id or None

    def _flow_match_base_selector(
        self, selector_repos: list[object], selector_raw: str, base_repo_id: str
    ) -> bool:
        selector = (selector_raw or "").strip().lower()
        if selector == base_repo_id.lower():
            return True
        for repo in selector_repos:
            repo_id = self._flow_manifest_repo_id(repo)
            if repo_id == base_repo_id:
                return True
            if self._flow_repo_base_id(repo) == base_repo_id:
                return True
        return False

    def _resolve_flow_target_from_args(
        self, argv: list[str]
    ) -> tuple[Optional[Path], Optional[str], int]:
        if not argv:
            return None, None, 0

        resolved = None
        consumed = 0
        if len(argv) >= 2:
            combined_repo_id = f"{argv[0]}--{argv[1]}"
            resolved = self._resolve_workspace(combined_repo_id)
            if resolved:
                consumed = 2
        if not resolved:
            resolved = self._resolve_workspace(argv[0])
            if resolved:
                consumed = 1
        if resolved:
            repo_root = canonicalize_path(Path(resolved[0]))
            return repo_root, resolved[1], consumed

        # Preserve flow actions unless the token resolved as an exact repo/path above.
        if (argv[0] or "").strip().lower() in FLOW_ACTION_TOKENS:
            return None, None, 0

        repos = self._flow_manifest_repos()
        if not repos:
            return None, None, 0

        if len(argv) >= 2:
            repo_candidates = self._flow_matching_manifest_repos(argv[0], repos)
            worktree_candidates = self._flow_matching_manifest_repos(argv[1], repos)
            pair_matches: set[str] = set()
            for candidate in worktree_candidates:
                candidate_id = self._flow_manifest_repo_id(candidate)
                if not candidate_id:
                    continue
                kind = getattr(candidate, "kind", None)
                if kind != "worktree" and "--" not in candidate_id:
                    continue
                base_repo_id = self._flow_repo_base_id(candidate)
                if not base_repo_id:
                    continue
                if self._flow_match_base_selector(
                    repo_candidates, argv[0], base_repo_id
                ):
                    pair_matches.add(candidate_id)
            if len(pair_matches) == 1:
                target_id = next(iter(pair_matches))
                resolved = self._resolve_workspace(target_id)
                if resolved:
                    repo_root = canonicalize_path(Path(resolved[0]))
                    return repo_root, resolved[1], 2

        single_matches = {
            repo_id
            for repo_id in (
                self._flow_manifest_repo_id(repo)
                for repo in self._flow_matching_manifest_repos(argv[0], repos)
            )
            if repo_id
        }
        if len(single_matches) == 1:
            target_id = next(iter(single_matches))
            resolved = self._resolve_workspace(target_id)
            if resolved:
                repo_root = canonicalize_path(Path(resolved[0]))
                return repo_root, resolved[1], 1

        return None, None, 0

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
            requester_user_id=(
                str(message.from_user_id) if message.from_user_id is not None else None
            ),
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

        target_repo_root: Optional[Path] = None
        target_repo_id: Optional[str] = None
        effective_args = args

        if argv:
            (
                target_repo_root,
                target_repo_id,
                consumed,
            ) = self._resolve_flow_target_from_args(argv)
            if target_repo_root:
                argv = argv[consumed:]
                # Reconstruct args for remainder logic (imperfect but sufficient for text commands)
                effective_args = " ".join(argv)

        action_raw = argv[0] if argv else ""
        if target_repo_root and not action_raw:
            action_raw = "status"
            argv = ["status"]
            effective_args = "status"
        action = normalize_flow_action(action_raw)
        _, remainder = _split_flow_action(effective_args)
        rest_argv = argv[1:]

        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        record = await self._store.get_topic(key)
        is_pma = bool(record and getattr(record, "pma_enabled", False))
        has_workspace_binding = bool(record and getattr(record, "workspace_path", None))
        route_to_hub_overview = should_route_flow_read_to_hub_overview(
            action=action_raw,
            pma_enabled=is_pma,
            has_workspace_binding=has_workspace_binding,
            has_explicit_target=bool(target_repo_root),
        )

        if not target_repo_root and not action_raw:
            if route_to_hub_overview:
                await self._send_flow_hub_overview(message)
                return
            action = "status"
            rest_argv = []

        if action == "help":
            await self._send_flow_overview(message, record)
            return

        if target_repo_root:
            repo_root = target_repo_root
        elif record and record.workspace_path:
            repo_root = canonicalize_path(Path(record.workspace_path))
        else:
            if route_to_hub_overview:
                await self._send_flow_hub_overview(message)
                return
            await self._send_message(
                message.chat_id,
                "No workspace bound. Use `/flow <repo-id> <worktree-id>` to inspect a repo worktree without binding, or `/bind <repo-id>` to attach this topic.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
                parse_mode="Markdown",
            )
            return

        try:
            if action == "status":
                await self._handle_flow_status_action(
                    message, repo_root, rest_argv, repo_id=target_repo_id
                )
                return
            if action == "runs":
                await self._handle_flow_runs(
                    message, repo_root, rest_argv, repo_id=target_repo_id
                )
                return
            if action == "start":
                await self._handle_flow_bootstrap(
                    message,
                    repo_root,
                    rest_argv,
                    repo_id=target_repo_id,
                )
                return
            if action == "restart":
                await self._handle_flow_restart(message, repo_root, rest_argv)
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
        *,
        repo_id: Optional[str] = None,
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
            if record is not None:
                record, _updated, locked = reconcile_flow_run(repo_root, record, store)
                if locked:
                    await self._edit_callback_message(
                        callback,
                        f"Run {_code(record.id)} is locked for reconcile; try again.",
                        reply_markup={"inline_keyboard": []},
                    )
                    return
            text, keyboard = self._build_flow_status_card(
                repo_root, record, store, repo_id=repo_id
            )
        finally:
            store.close()
        await self._edit_callback_message(callback, text, reply_markup=keyboard)

    async def _handle_flow_callback(
        self, callback: TelegramCallbackQuery, parsed: FlowCallback
    ) -> None:
        callback_answered = False

        async def _answer_once(text: str) -> None:
            nonlocal callback_answered
            if callback_answered:
                return
            await self._answer_callback(callback, text)
            callback_answered = True

        if callback.chat_id is None:
            return
        key = await self._resolve_topic_key(callback.chat_id, callback.thread_id)
        record = await self._store.get_topic(key)
        repo_root: Optional[Path] = None
        effective_repo_id = self._resolve_flow_repo_context(
            parsed.run_id, parsed.repo_id
        )
        if effective_repo_id:
            resolved = self._resolve_workspace(effective_repo_id)
            if resolved:
                repo_root = canonicalize_path(Path(resolved[0]))
        if repo_root is None and record and record.workspace_path:
            repo_root = canonicalize_path(Path(record.workspace_path))
        if repo_root is None:
            await _answer_once("No workspace bound")
            await self._edit_callback_message(
                callback,
                "No workspace bound. Use /flow <repo-id> <worktree-id>, or /bind to bind this topic to a repo first.",
                reply_markup={"inline_keyboard": []},
            )
            return

        action = (parsed.action or "").strip().lower()
        run_id_raw = parsed.run_id

        if action in {"refresh", "status"}:
            await _answer_once("Refreshing...")
            await self._render_flow_status_callback(
                callback, repo_root, run_id_raw, repo_id=effective_repo_id
            )
            return

        error = None
        notice = None
        flow_service = self._ticket_flow_orchestration_service(repo_root)
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
                try:
                    await _answer_once("Working...")
                    updated = await flow_service.resume_flow_run(record.id)
                except (KeyError, ValueError) as exc:
                    error = str(exc)
                else:
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
                await _answer_once("Working...")
                await flow_service.stop_flow_run(record.id)
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
                    await _answer_once("Working...")
                    record, updated, locked = flow_service.reconcile_flow_run(record.id)
                    if locked:
                        error = (
                            f"Run {record.run_id} is locked for reconcile; try again."
                        )
                    else:
                        notice = "Recovered." if updated else "No changes needed."
            finally:
                store.close()
        elif action in {
            "archive",
            "archive_confirm",
            "archive_cancel",
            "archive_confirm_prompt",
            "archive_cancel_prompt",
        }:
            if action == "archive_cancel":
                await _answer_once("Archive cancelled")
                await self._render_flow_status_callback(
                    callback,
                    repo_root,
                    run_id_raw,
                    repo_id=effective_repo_id,
                )
                return
            if action == "archive_cancel_prompt":
                await _answer_once("Archive cancelled")
                await self._edit_callback_message(
                    callback,
                    "Archive cancelled.",
                    reply_markup={"inline_keyboard": []},
                )
                return

            store = _load_flow_store(repo_root)
            try:
                store.initialize()
                record, error = self._resolve_flow_archive_record(store, run_id_raw)
            finally:
                store.close()

            if error is None:
                archive_mode = resolve_ticket_flow_archive_mode(record)
                if archive_mode == "blocked":
                    error = (
                        f"Run {record.id} is {record.status.value}. "
                        "Stop or pause it before archiving."
                    )
                elif action == "archive" and archive_mode == "confirm":
                    await _answer_once("Confirm archive?")
                    await self._edit_callback_message(
                        callback,
                        self._flow_archive_prompt_text(record),
                        reply_markup=self._build_flow_archive_confirmation_keyboard(
                            record.id,
                            repo_id=effective_repo_id,
                            prompt_variant=False,
                        ),
                    )
                    return
                else:
                    try:
                        await _answer_once("Working...")
                        summary = flow_service.archive_flow_run(
                            record.id,
                            force=ticket_flow_archive_requires_force(record),
                            delete_run=True,
                        )
                        notice = (
                            f"Archived {summary['archived_tickets']} tickets and "
                            f"{'moved' if summary['archived_runs'] else 'skipped'} run artifacts."
                        )
                    except ValueError as exc:
                        error = str(exc)
        else:
            await _answer_once("Unknown action")
            return

        if error:
            await _answer_once(error)
        elif notice:
            await _answer_once(notice)
        await self._render_flow_status_callback(
            callback, repo_root, run_id_raw, repo_id=effective_repo_id
        )

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
            record = select_default_ticket_flow_run(store)
        if record is None:
            return (
                None,
                "No ticket flow run found. Use /pma to start a new flow via web app.",
            )
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
        lines = [f"Run: {_code(run.id)}", f"Status: {status_value}"]
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
        duration_label = format_flow_duration(
            flow_duration_seconds(started_at, finished_at, status_value)
        )
        if duration_label:
            lines.append(f"Elapsed: {duration_label}")
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
            freshness_summary = summarize_flow_freshness(snapshot.get("freshness"))
            if freshness_summary:
                lines.append(f"Freshness: {freshness_summary}")
        if health is None:
            health = snapshot.get("worker_health") if snapshot else None
        if health is None:
            run_id = getattr(run, "id", None)
            if isinstance(run_id, str) and run_id:
                try:
                    health = check_worker_health(repo_root, run_id)
                except Exception:
                    health = None
        if health is None:
            return lines
        worker_line = f"Worker: {health.status}"
        if health.pid:
            worker_line += f" (pid {health.pid})"
        if health.message and health.status not in {"alive"}:
            worker_line += f" - {health.message}"
        lines.append(worker_line)
        if status == FlowRunStatus.PAUSED:
            lines.append(
                "Paused: use `/flow reply <message>` (or send a message in chat) to resume."
            )
        return lines

    def _build_flow_status_keyboard(
        self,
        record: Optional[object],
        *,
        health: Optional[FlowWorkerHealth],
        repo_id: Optional[str] = None,
    ) -> Optional[dict[str, object]]:
        if record is None or health is None:
            return None
        status = getattr(record, "status", None)
        if status is None:
            return None
        run_id = record.id

        def _flow_callback_data(action: str) -> str:
            try:
                return encode_flow_callback(action, run_id, repo_id=repo_id)
            except ValueError as exc:
                if repo_id and "callback_data exceeds Telegram limit" in str(exc):
                    self._remember_flow_repo_context(run_id, repo_id)
                    return encode_flow_callback(action, run_id)
                raise

        rows: list[list[InlineButton]] = []
        if status == FlowRunStatus.PAUSED:
            rows.append(
                [
                    InlineButton(
                        "Resume",
                        _flow_callback_data("resume"),
                    ),
                ]
            )
            rows.append(
                [
                    InlineButton(
                        "Archive",
                        _flow_callback_data("archive"),
                    )
                ]
            )
        elif status.is_terminal():
            rows.append(
                [
                    InlineButton(
                        "Archive",
                        _flow_callback_data("archive"),
                    ),
                ]
            )
            rows.append(
                [
                    InlineButton(
                        "Refresh",
                        _flow_callback_data("refresh"),
                    )
                ]
            )
        else:
            if health.status in {"dead", "mismatch", "invalid", "absent"}:
                rows.append(
                    [
                        InlineButton(
                            "Recover",
                            _flow_callback_data("recover"),
                        ),
                        InlineButton(
                            "Refresh",
                            _flow_callback_data("refresh"),
                        ),
                    ]
                )
            elif status == FlowRunStatus.RUNNING:
                rows.append(
                    [
                        InlineButton(
                            "Stop",
                            _flow_callback_data("stop"),
                        ),
                        InlineButton(
                            "Refresh",
                            _flow_callback_data("refresh"),
                        ),
                    ]
                )
            else:
                rows.append(
                    [
                        InlineButton(
                            "Refresh",
                            _flow_callback_data("refresh"),
                        )
                    ]
                )
        return build_inline_keyboard(rows) if rows else None

    def _build_flow_status_card(
        self,
        repo_root: Path,
        record: Optional[object],
        store: Optional[FlowStore],
        *,
        repo_id: Optional[str] = None,
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
        keyboard = self._build_flow_status_keyboard(
            record, health=health, repo_id=repo_id
        )
        return "\n".join(lines), keyboard

    def _build_flow_start_card(
        self,
        repo_root: Path,
        record: Optional[object],
        store: Optional[FlowStore],
        *,
        prefix: str,
        repo_id: Optional[str] = None,
    ) -> tuple[str, Optional[dict[str, object]]]:
        prefix = prefix.strip()
        if record is None:
            return prefix, None
        status_text, keyboard = self._build_flow_status_card(
            repo_root, record, store, repo_id=repo_id
        )
        return f"{prefix}\n\n{status_text}", keyboard

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
                latest = select_default_ticket_flow_run(store)
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

        def _format_status_line(
            label: str,
            *,
            status_icon: str,
            status_value: str,
            progress_label: str,
            run_id: Optional[str],
            prefix: str = "",
        ) -> str:
            run_suffix = f" run {_code(run_id)}" if run_id else ""
            repo_label = _code(label)
            return (
                f"{prefix}{status_icon} {repo_label}: {status_value} "
                f"{progress_label}{run_suffix}"
            )

        lines = ["Hub Flow Overview:"]
        groups: dict[str, list[tuple[str, str]]] = {}
        group_order: list[str] = []
        raw_config: dict[str, object] = {}
        try:
            repo_config = load_repo_config(self._hub_root)
            if isinstance(repo_config.raw, dict):
                raw_config = repo_config.raw
        except ConfigError:
            raw_config = {}
        overview_entries = build_hub_flow_overview_entries(
            hub_root=self._hub_root,
            manifest=manifest,
            raw_config=raw_config,
        )
        has_unregistered = any(entry.unregistered for entry in overview_entries)

        for entry in overview_entries:
            repo_root = entry.repo_root
            label = entry.label
            line_prefix = "  -> " if entry.is_worktree else ""
            group = entry.group
            if group not in groups:
                groups[group] = []
                group_order.append(group)

            store = _load_flow_store(repo_root)
            try:
                store.initialize()
                latest = select_default_ticket_flow_run(store)
                progress = ticket_progress(repo_root)
                display = build_ticket_flow_display(
                    status=latest.status.value if latest else None,
                    done_count=progress.get("done", 0),
                    total_count=progress.get("total", 0),
                    run_id=latest.id if latest else None,
                )
                duration_suffix = ""
                if latest is not None and latest.finished_at:
                    duration_label = format_flow_duration(
                        flow_run_duration_seconds(latest)
                    )
                    if duration_label:
                        duration_suffix = f" · took {duration_label}"
                freshness_suffix = ""
                if latest is not None:
                    snapshot = build_flow_status_snapshot(repo_root, latest, store)
                    freshness = snapshot.get("freshness")
                    freshness_summary = summarize_flow_freshness(freshness)
                    if (
                        isinstance(freshness, dict)
                        and freshness.get("is_stale") is True
                    ):
                        freshness_suffix = (
                            f" · snapshot {freshness_summary}"
                            if freshness_summary
                            else " · snapshot stale"
                        )
                progress_label = f"{display['done_count']}/{display['total_count']}"
                status_line = _format_status_line(
                    label,
                    status_icon=str(display["status_icon"]),
                    status_value=str(display["status_label"]),
                    progress_label=progress_label,
                    run_id=display.get("run_id"),
                    prefix=line_prefix,
                )
                status_line += duration_suffix + freshness_suffix
            except Exception:
                status_line = f"{line_prefix}❓ {_code(label)}: Error reading state"
            finally:
                store.close()

            groups[group].append((label, status_line))

        for group in group_order:
            group_entries = groups.get(group, [])
            if not group_entries:
                continue
            group_entries.sort(key=lambda pair: (0 if pair[0] == group else 1, pair[0]))
            lines.extend([line for _label, line in group_entries])
        if not overview_entries:
            lines.append("No enabled repositories found.")
        if has_unregistered:
            lines.append(
                "Note: Active chat-bound unregistered worktrees detected. Run `car hub scan` to register them."
            )
        lines.append("Tip: use `/flow <repo-id> <worktree-id>` for repo details.")

        await self._send_message(
            message.chat_id,
            "\n".join(lines),
            thread_id=message.thread_id,
            reply_to=message.message_id,
            parse_mode="Markdown",
        )

    async def _handle_flow_status_action(
        self,
        message: TelegramMessage,
        repo_root: Path,
        argv: list[str],
        *,
        repo_id: Optional[str] = None,
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
            if record is not None:
                record, _updated, locked = reconcile_flow_run(repo_root, record, store)
                if locked:
                    await self._send_message(
                        message.chat_id,
                        f"Run {_code(record.id)} is locked for reconcile; try again.",
                        thread_id=message.thread_id,
                        reply_to=message.message_id,
                        parse_mode="Markdown",
                    )
                    return
            text, keyboard = self._build_flow_status_card(
                repo_root, record, store, repo_id=repo_id
            )
        finally:
            store.close()
        await self._send_message(
            message.chat_id,
            text,
            thread_id=message.thread_id,
            reply_to=message.message_id,
            reply_markup=keyboard,
            parse_mode="Markdown",
        )

    async def _handle_flow_runs(
        self,
        message: TelegramMessage,
        repo_root: Path,
        argv: list[str],
        *,
        repo_id: Optional[str] = None,
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
                "No ticket flow runs found. Use /pma to start a new flow via web app.",
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

        state = SelectionState(
            items=items,
            button_labels=button_labels,
            repo_id=repo_id,
            requester_user_id=(
                str(message.from_user_id) if message.from_user_id is not None else None
            ),
        )
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
            parse_mode="Markdown",
        )

    async def _handle_flow_bootstrap(
        self,
        message: TelegramMessage,
        repo_root: Path,
        argv: list[str],
        *,
        repo_id: Optional[str] = None,
    ) -> None:
        force_new = self._has_flag(argv, "--force-new") or self._has_flag(
            argv, "--force"
        )
        run_mirror = self._flow_run_mirror(repo_root)
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
            run_mirror.mirror_inbound(
                run_id=active_run.id,
                platform="telegram",
                event_type="flow_bootstrap_command",
                kind="command",
                actor="user",
                text=(message.text or "").strip(),
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                message_id=message.message_id,
                meta={"force_new": force_new, "reuse_existing": True},
            )
            self._ticket_flow_orchestration_service(repo_root).ensure_flow_run_worker(
                active_run.id
            )
            store = _load_flow_store(repo_root)
            try:
                store.initialize()
                record = store.get_flow_run(active_run.id)
                if record is not None:
                    record, _updated, locked = reconcile_flow_run(
                        repo_root, record, store
                    )
                    if locked:
                        await self._send_message(
                            message.chat_id,
                            f"Run {_code(record.id)} is locked for reconcile; try again.",
                            thread_id=message.thread_id,
                            reply_to=message.message_id,
                            parse_mode="Markdown",
                        )
                        return
                outbound_text, keyboard = self._build_flow_start_card(
                    repo_root,
                    record,
                    store,
                    prefix=(
                        f"Reusing ticket flow run {_code(active_run.id)} "
                        f"({active_run.status.value})."
                    ),
                    repo_id=repo_id,
                )
            finally:
                store.close()
            await self._send_message(
                message.chat_id,
                outbound_text,
                thread_id=message.thread_id,
                reply_to=message.message_id,
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
            run_mirror.mirror_outbound(
                run_id=active_run.id,
                platform="telegram",
                event_type="flow_bootstrap_reuse_notice",
                kind="notice",
                actor="car",
                text=outbound_text,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                meta={"status": active_run.status.value},
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
                bootstrap_ticket_id = generate_ticket_id()
                template = f"""---
agent: codex
done: false
ticket_id: "{bootstrap_ticket_id}"
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

        flow_record = await self._ticket_flow_orchestration_service(
            repo_root
        ).start_flow_run(
            "ticket_flow",
            input_data={},
            metadata={"seeded_ticket": seeded, "origin": "telegram"},
        )
        run_mirror.mirror_inbound(
            run_id=flow_record.run_id,
            platform="telegram",
            event_type="flow_bootstrap_command",
            kind="command",
            actor="user",
            text=(message.text or "").strip(),
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
            meta={"force_new": force_new, "seeded_ticket": seeded},
        )

        if not issue_exists and not tickets_exist:
            await self._send_flow_issue_hint(message, repo_root)

        store = _load_flow_store(repo_root)
        try:
            store.initialize()
            record = store.get_flow_run(flow_record.run_id)
            if record is not None:
                record, _updated, locked = reconcile_flow_run(repo_root, record, store)
                if locked:
                    await self._send_message(
                        message.chat_id,
                        f"Run {_code(record.id)} is locked for reconcile; try again.",
                        thread_id=message.thread_id,
                        reply_to=message.message_id,
                        parse_mode="Markdown",
                    )
                    return
            outbound_text, keyboard = self._build_flow_start_card(
                repo_root,
                record,
                store,
                prefix=f"Started ticket flow run {_code(flow_record.run_id)}.",
                repo_id=repo_id,
            )
        finally:
            store.close()
        await self._send_message(
            message.chat_id,
            outbound_text,
            thread_id=message.thread_id,
            reply_to=message.message_id,
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
        run_mirror.mirror_outbound(
            run_id=flow_record.run_id,
            platform="telegram",
            event_type="flow_bootstrap_started_notice",
            kind="notice",
            actor="car",
            text=outbound_text,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
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
                f"No ISSUE.md found. Use `/flow issue <issue#|url>` for {_code(repo_label)}, "
                "or `/flow plan <text>`."
            )
        await self._send_message(
            message.chat_id,
            gh_status,
            thread_id=message.thread_id,
            reply_to=message.message_id,
            parse_mode="Markdown",
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
                    f"Run {_code(record.id)} is {record.status.value}, not paused.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                    parse_mode="Markdown",
                )
                return
        finally:
            store.close()

        force = self._has_flag(argv, "--force")
        run_mirror = self._flow_run_mirror(repo_root)
        run_mirror.mirror_inbound(
            run_id=record.id,
            platform="telegram",
            event_type="flow_resume_command",
            kind="command",
            actor="user",
            text=(message.text or "").strip(),
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
            meta={"force": force},
        )
        try:
            updated = await self._ticket_flow_orchestration_service(
                repo_root
            ).resume_flow_run(record.id, force=force)
        except (KeyError, ValueError) as exc:
            await self._send_message(
                message.chat_id,
                str(exc),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        outbound_text = f"Resumed run {_code(updated.run_id)}."
        await self._send_message(
            message.chat_id,
            outbound_text,
            thread_id=message.thread_id,
            reply_to=message.message_id,
            parse_mode="Markdown",
        )
        run_mirror.mirror_outbound(
            run_id=updated.run_id,
            platform="telegram",
            event_type="flow_resume_notice",
            kind="notice",
            actor="car",
            text=outbound_text,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
        )

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
                    f"Run {_code(record.id)} is already {record.status.value}.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                    parse_mode="Markdown",
                )
                return
        finally:
            store.close()

        run_mirror = self._flow_run_mirror(repo_root)
        run_mirror.mirror_inbound(
            run_id=record.id,
            platform="telegram",
            event_type="flow_stop_command",
            kind="command",
            actor="user",
            text=(message.text or "").strip(),
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
        )
        updated = await self._ticket_flow_orchestration_service(
            repo_root
        ).stop_flow_run(record.id)
        outbound_text = f"Stopped run {_code(updated.run_id)} ({updated.status})."
        await self._send_message(
            message.chat_id,
            outbound_text,
            thread_id=message.thread_id,
            reply_to=message.message_id,
            parse_mode="Markdown",
        )
        run_mirror.mirror_outbound(
            run_id=updated.run_id,
            platform="telegram",
            event_type="flow_stop_notice",
            kind="notice",
            actor="car",
            text=outbound_text,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            meta={"status": updated.status},
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
            record, updated, locked = self._ticket_flow_orchestration_service(
                repo_root
            ).reconcile_flow_run(record.id)
            if locked:
                await self._send_message(
                    message.chat_id,
                    f"Run {_code(record.run_id)} is locked for reconcile; try again.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                    parse_mode="Markdown",
                )
                return
            hint = "Recovered" if updated else "No changes needed"
            latest_record = store.get_flow_run(record.run_id)
            if latest_record is None:
                lines = [f"{hint} for run {_code(record.run_id)}."]
            else:
                lines = [f"{hint} for run {_code(latest_record.id)}."]
                record = latest_record
            lines.extend(self._format_flow_status_lines(repo_root, record, store))
        finally:
            store.close()

        await self._send_message(
            message.chat_id,
            "\n".join(lines),
            thread_id=message.thread_id,
            reply_to=message.message_id,
            parse_mode="Markdown",
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
                if error:
                    await self._send_message(
                        message.chat_id,
                        error,
                        thread_id=message.thread_id,
                        reply_to=message.message_id,
                    )
                    return
                if run_id:
                    record = store.get_flow_run(run_id)
                    if record is None:
                        await self._send_message(
                            message.chat_id,
                            f"No ticket flow run found for {_code(run_id)}.",
                            thread_id=message.thread_id,
                            reply_to=message.message_id,
                            parse_mode="Markdown",
                        )
                        return
            else:
                record = _select_latest_run(store, lambda run: run.status.is_active())
        finally:
            store.close()
        if record and not record.status.is_terminal():
            await self._ticket_flow_orchestration_service(repo_root).stop_flow_run(
                record.id
            )
        await self._handle_flow_bootstrap(message, repo_root, argv=["--force-new"])

    async def _handle_flow_archive(
        self, message: TelegramMessage, repo_root: Path, argv: list[str]
    ) -> None:
        force = self._has_flag(argv, "--force")
        store = _load_flow_store(repo_root)
        try:
            store.initialize()
            run_id_raw = self._first_non_flag(argv)
            record, error = self._resolve_flow_archive_record(store, run_id_raw)
            if error:
                await self._send_message(
                    message.chat_id,
                    error,
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
        finally:
            store.close()

        archive_mode = resolve_ticket_flow_archive_mode(record)
        if archive_mode == "blocked":
            await self._send_message(
                message.chat_id,
                f"Run {_code(record.id)} is {record.status.value}. Stop or pause it before archiving.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
                parse_mode="Markdown",
            )
            return
        if archive_mode == "confirm" and not force:
            await self._send_message(
                message.chat_id,
                self._flow_archive_prompt_text(record),
                thread_id=message.thread_id,
                reply_to=message.message_id,
                reply_markup=self._build_flow_archive_confirmation_keyboard(
                    record.id,
                    prompt_variant=True,
                ),
                parse_mode="Markdown",
            )
            return

        summary = self._ticket_flow_orchestration_service(repo_root).archive_flow_run(
            record.id,
            force=ticket_flow_archive_requires_force(record),
            delete_run=True,
        )

        await self._send_message(
            message.chat_id,
            f"Archived run {_code(record.id)} ({summary['archived_tickets']} tickets).",
            thread_id=message.thread_id,
            reply_to=message.message_id,
            parse_mode="Markdown",
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
        run_mirror = self._flow_run_mirror(repo_root)
        run_mirror.mirror_inbound(
            run_id=run_id,
            platform="telegram",
            event_type="flow_reply_command",
            kind="command",
            actor="user",
            text=text,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
        )
        success, result = await self._write_user_reply_from_telegram(
            repo_root, run_id, run_record, message, text
        )
        outbound_text = result
        resume_success = False
        if success:
            try:
                updated = await self._ticket_flow_orchestration_service(
                    repo_root
                ).resume_flow_run(run_id)
            except (KeyError, ValueError) as exc:
                outbound_text = (
                    f"{result} Failed to resume run {run_id}: {exc} "
                    "Use /flow resume to continue."
                )
            except Exception as exc:
                outbound_text = (
                    f"{result} Failed to resume run {run_id}: {exc} "
                    "Use /flow resume to continue."
                )
            else:
                outbound_text = f"{result} Resumed run {updated.run_id}."
                resume_success = True
        await self._send_message(
            message.chat_id,
            outbound_text,
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
        run_mirror.mirror_outbound(
            run_id=run_id,
            platform="telegram",
            event_type="flow_reply_notice",
            kind="notice",
            actor="car",
            text=outbound_text,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            meta={"success": success, "resume_success": resume_success},
        )
