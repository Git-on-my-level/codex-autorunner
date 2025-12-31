from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Iterable, Optional

from .app_server_client import ApprovalDecision, CodexAppServerClient
from .logging_utils import log_event
from .manifest import load_manifest
from .telegram_adapter import (
    ApprovalCallback,
    BindCallback,
    ResumeCallback,
    TelegramAllowlist,
    TelegramBotClient,
    TelegramCallbackQuery,
    TelegramCommand,
    TelegramMessage,
    TelegramUpdate,
    TelegramUpdatePoller,
    allowlist_allows,
    build_approval_keyboard,
    build_bind_keyboard,
    build_resume_keyboard,
    is_interrupt_alias,
    parse_callback_data,
    parse_command,
)
from .telegram_state import (
    APPROVAL_MODE_YOLO,
    TelegramStateStore,
    TopicRouter,
    normalize_approval_mode,
    topic_key,
)
from .utils import subprocess_env

DEFAULT_ALLOWED_UPDATES = ("message", "edited_message", "callback_query")
DEFAULT_POLL_TIMEOUT_SECONDS = 30
DEFAULT_RESUME_LIMIT = 10
DEFAULT_BIND_LIMIT = 12
DEFAULT_SAFE_APPROVAL_POLICY = "on-request"
DEFAULT_YOLO_APPROVAL_POLICY = "never"
DEFAULT_YOLO_SANDBOX_POLICY = "dangerFullAccess"
DEFAULT_STATE_FILE = ".codex-autorunner/telegram_state.json"
DEFAULT_APP_SERVER_COMMAND = ["codex", "app-server"]


class TelegramBotConfigError(Exception):
    """Raised when telegram bot config is invalid."""


@dataclass(frozen=True)
class TelegramBotDefaults:
    approval_mode: str
    approval_policy: Optional[str]
    sandbox_policy: Optional[str]
    yolo_approval_policy: str
    yolo_sandbox_policy: str

    def policies_for_mode(self, mode: str) -> tuple[Optional[str], Optional[str]]:
        normalized = normalize_approval_mode(mode, default=APPROVAL_MODE_YOLO)
        if normalized == APPROVAL_MODE_YOLO:
            return self.yolo_approval_policy, self.yolo_sandbox_policy
        return self.approval_policy, self.sandbox_policy


@dataclass(frozen=True)
class TelegramBotConcurrency:
    max_parallel_turns: int
    per_topic_queue: bool


@dataclass(frozen=True)
class TelegramBotConfig:
    root: Path
    enabled: bool
    mode: str
    bot_token_env: str
    chat_id_env: str
    bot_token: Optional[str]
    allowed_chat_ids: set[int]
    allowed_user_ids: set[int]
    require_topics: bool
    defaults: TelegramBotDefaults
    concurrency: TelegramBotConcurrency
    state_file: Path
    app_server_command_env: str
    app_server_command: list[str]
    poll_timeout_seconds: int
    poll_allowed_updates: list[str]

    @classmethod
    def from_raw(
        cls,
        raw: Optional[dict[str, Any]],
        *,
        root: Path,
        env: Optional[dict[str, str]] = None,
    ) -> "TelegramBotConfig":
        env = env or dict(os.environ)
        cfg = raw if isinstance(raw, dict) else {}
        enabled = bool(cfg.get("enabled", False))
        mode = str(cfg.get("mode", "polling"))
        bot_token_env = str(cfg.get("bot_token_env", "CAR_TELEGRAM_BOT_TOKEN"))
        chat_id_env = str(cfg.get("chat_id_env", "CAR_TELEGRAM_CHAT_ID"))
        bot_token = env.get(bot_token_env)

        allowed_chat_ids = set(_parse_int_list(cfg.get("allowed_chat_ids")))
        allowed_chat_ids.update(_parse_int_list(env.get(chat_id_env)))
        allowed_user_ids = set(_parse_int_list(cfg.get("allowed_user_ids")))

        require_topics = bool(cfg.get("require_topics", True))

        defaults_raw = cfg.get("defaults") if isinstance(cfg.get("defaults"), dict) else {}
        approval_mode = normalize_approval_mode(
            defaults_raw.get("approval_mode"), default=APPROVAL_MODE_YOLO
        )
        approval_policy = defaults_raw.get("approval_policy", DEFAULT_SAFE_APPROVAL_POLICY)
        sandbox_policy = defaults_raw.get("sandbox_policy")
        if sandbox_policy is not None:
            sandbox_policy = str(sandbox_policy)
        yolo_approval_policy = str(
            defaults_raw.get("yolo_approval_policy", DEFAULT_YOLO_APPROVAL_POLICY)
        )
        yolo_sandbox_policy = str(
            defaults_raw.get("yolo_sandbox_policy", DEFAULT_YOLO_SANDBOX_POLICY)
        )
        defaults = TelegramBotDefaults(
            approval_mode=approval_mode,
            approval_policy=str(approval_policy) if approval_policy is not None else None,
            sandbox_policy=sandbox_policy,
            yolo_approval_policy=yolo_approval_policy,
            yolo_sandbox_policy=yolo_sandbox_policy,
        )

        concurrency_raw = (
            cfg.get("concurrency") if isinstance(cfg.get("concurrency"), dict) else {}
        )
        max_parallel_turns = int(concurrency_raw.get("max_parallel_turns", 2))
        if max_parallel_turns <= 0:
            max_parallel_turns = 1
        per_topic_queue = bool(concurrency_raw.get("per_topic_queue", True))
        concurrency = TelegramBotConcurrency(
            max_parallel_turns=max_parallel_turns,
            per_topic_queue=per_topic_queue,
        )

        state_file = Path(cfg.get("state_file", DEFAULT_STATE_FILE))
        if not state_file.is_absolute():
            state_file = (root / state_file).resolve()

        app_server_command_env = str(
            cfg.get("app_server_command_env", "CAR_TELEGRAM_APP_SERVER_COMMAND")
        )
        app_server_command: list[str] = []
        if app_server_command_env:
            env_command = env.get(app_server_command_env)
            if env_command:
                app_server_command = _parse_command(env_command)
        if not app_server_command:
            app_server_command = _parse_command(cfg.get("app_server_command"))
        if not app_server_command:
            app_server_command = list(DEFAULT_APP_SERVER_COMMAND)

        polling_raw = (
            cfg.get("polling") if isinstance(cfg.get("polling"), dict) else {}
        )
        poll_timeout_seconds = int(
            polling_raw.get("timeout_seconds", DEFAULT_POLL_TIMEOUT_SECONDS)
        )
        allowed_updates = polling_raw.get("allowed_updates")
        if isinstance(allowed_updates, list):
            poll_allowed_updates = [str(item) for item in allowed_updates if item]
        else:
            poll_allowed_updates = list(DEFAULT_ALLOWED_UPDATES)

        return cls(
            root=root,
            enabled=enabled,
            mode=mode,
            bot_token_env=bot_token_env,
            chat_id_env=chat_id_env,
            bot_token=bot_token,
            allowed_chat_ids=allowed_chat_ids,
            allowed_user_ids=allowed_user_ids,
            require_topics=require_topics,
            defaults=defaults,
            concurrency=concurrency,
            state_file=state_file,
            app_server_command_env=app_server_command_env,
            app_server_command=app_server_command,
            poll_timeout_seconds=poll_timeout_seconds,
            poll_allowed_updates=poll_allowed_updates,
        )

    def validate(self) -> None:
        issues: list[str] = []
        if not self.bot_token:
            issues.append(f"missing bot token env '{self.bot_token_env}'")
        if not self.allowed_chat_ids:
            issues.append(
                "no allowed chat ids configured (set allowed_chat_ids or chat_id_env)"
            )
        if not self.allowed_user_ids:
            issues.append("no allowed user ids configured (set allowed_user_ids)")
        if not self.app_server_command:
            issues.append("app_server_command must be set")
        if issues:
            raise TelegramBotConfigError("; ".join(issues))

    def allowlist(self) -> TelegramAllowlist:
        return TelegramAllowlist(
            allowed_chat_ids=self.allowed_chat_ids,
            allowed_user_ids=self.allowed_user_ids,
            require_topic=self.require_topics,
        )


@dataclass
class PendingApproval:
    request_id: str
    chat_id: int
    thread_id: Optional[int]
    message_id: Optional[int]
    future: asyncio.Future[ApprovalDecision]


@dataclass
class TurnContext:
    topic_key: str
    chat_id: int
    thread_id: Optional[int]
    reply_to_message_id: Optional[int]


class TelegramBotService:
    def __init__(
        self,
        config: TelegramBotConfig,
        *,
        logger: Optional[logging.Logger] = None,
        hub_root: Optional[Path] = None,
        manifest_path: Optional[Path] = None,
    ) -> None:
        self._config = config
        self._logger = logger or logging.getLogger(__name__)
        self._hub_root = hub_root
        self._manifest_path = manifest_path
        self._allowlist = config.allowlist()
        self._store = TelegramStateStore(
            config.state_file, default_approval_mode=config.defaults.approval_mode
        )
        self._router = TopicRouter(self._store)
        self._client = CodexAppServerClient(
            config.app_server_command,
            cwd=hub_root or config.root,
            env=subprocess_env(),
            approval_handler=self._handle_approval_request,
            logger=self._logger,
        )
        self._bot = TelegramBotClient(config.bot_token or "", logger=self._logger)
        self._poller = TelegramUpdatePoller(
            self._bot, allowed_updates=config.poll_allowed_updates
        )
        self._turn_semaphore = asyncio.Semaphore(config.concurrency.max_parallel_turns)
        self._turn_contexts: dict[str, TurnContext] = {}
        self._pending_approvals: dict[str, PendingApproval] = {}
        self._resume_options: dict[str, list[str]] = {}
        self._bind_options: dict[str, list[str]] = {}
        self._bot_username: Optional[str] = None

    async def run_polling(self) -> None:
        if self._config.mode != "polling":
            raise TelegramBotConfigError(
                f"Unsupported telegram_bot.mode '{self._config.mode}'"
            )
        self._config.validate()
        await self._client.start()
        await self._prime_bot_identity()
        log_event(
            self._logger,
            logging.INFO,
            "telegram.bot.started",
            mode=self._config.mode,
            poll_timeout=self._config.poll_timeout_seconds,
            allowed_updates=list(self._config.poll_allowed_updates),
            allowed_chats=len(self._config.allowed_chat_ids),
            allowed_users=len(self._config.allowed_user_ids),
            require_topics=self._config.require_topics,
        )
        try:
            while True:
                updates = []
                try:
                    updates = await self._poller.poll(
                        timeout=self._config.poll_timeout_seconds
                    )
                except Exception as exc:
                    log_event(
                        self._logger,
                        logging.WARNING,
                        "telegram.poll.failed",
                        exc=exc,
                    )
                    await asyncio.sleep(1.0)
                    continue
                for update in updates:
                    self._spawn_task(self._dispatch_update(update))
        finally:
            await self._bot.close()
            await self._client.close()

    async def _prime_bot_identity(self) -> None:
        try:
            payload = await self._bot.get_me()
        except Exception:
            return
        if isinstance(payload, dict):
            username = payload.get("username")
            if isinstance(username, str) and username:
                self._bot_username = username

    def _spawn_task(self, coro: Awaitable[Any]) -> None:
        task = asyncio.create_task(coro)
        task.add_done_callback(self._log_task_result)

    def _log_task_result(self, task: asyncio.Future) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log_event(self._logger, logging.WARNING, "telegram.task.failed", exc=exc)

    async def _dispatch_update(self, update: TelegramUpdate) -> None:
        chat_id = None
        user_id = None
        thread_id = None
        message_id = None
        is_topic = None
        if update.message:
            chat_id = update.message.chat_id
            user_id = update.message.from_user_id
            thread_id = update.message.thread_id
            message_id = update.message.message_id
            is_topic = update.message.is_topic_message
        elif update.callback:
            chat_id = update.callback.chat_id
            user_id = update.callback.from_user_id
            thread_id = update.callback.thread_id
            message_id = update.callback.message_id
        log_event(
            self._logger,
            logging.INFO,
            "telegram.update.received",
            update_id=update.update_id,
            chat_id=chat_id,
            user_id=user_id,
            thread_id=thread_id,
            message_id=message_id,
            is_topic=is_topic,
            has_message=bool(update.message),
            has_callback=bool(update.callback),
        )
        if not allowlist_allows(update, self._allowlist):
            self._log_denied(update)
            return
        if update.callback:
            await self._handle_callback(update.callback)
            return
        if update.message:
            await self._handle_message(update.message)

    def _log_denied(self, update: TelegramUpdate) -> None:
        chat_id = None
        user_id = None
        thread_id = None
        if update.message:
            chat_id = update.message.chat_id
            user_id = update.message.from_user_id
            thread_id = update.message.thread_id
        elif update.callback:
            chat_id = update.callback.chat_id
            user_id = update.callback.from_user_id
            thread_id = update.callback.thread_id
        log_event(
            self._logger,
            logging.INFO,
            "telegram.allowlist.denied",
            chat_id=chat_id,
            user_id=user_id,
            thread_id=thread_id,
        )

    async def _handle_message(self, message: TelegramMessage) -> None:
        text = (message.text or "").strip()
        if not text:
            return
        key = topic_key(message.chat_id, message.thread_id)
        runtime = self._router.runtime_for(key)

        if self._handle_pending_resume(key, text):
            return
        if self._handle_pending_bind(key, text):
            return

        if is_interrupt_alias(text):
            await self._handle_interrupt(message, runtime)
            return

        command = parse_command(text, bot_username=self._bot_username)
        if command:
            if command.name not in ("resume", "bind"):
                self._resume_options.pop(key, None)
                self._bind_options.pop(key, None)
        else:
            self._resume_options.pop(key, None)
            self._bind_options.pop(key, None)
        if command:
            self._enqueue_topic_work(
                key,
                lambda: self._handle_command(command, message, runtime),
            )
            return

        self._enqueue_topic_work(
            key,
            lambda: self._handle_normal_message(message, runtime),
        )

    def _handle_pending_resume(self, key: str, text: str) -> bool:
        if not text.isdigit():
            return False
        options = self._resume_options.get(key)
        if not options:
            return False
        choice = int(text)
        if choice <= 0 or choice > len(options):
            return False
        thread_id = options[choice - 1]
        self._resume_options.pop(key, None)
        self._enqueue_topic_work(
            key,
            lambda: self._resume_thread_by_id(key, thread_id),
        )
        return True

    def _handle_pending_bind(self, key: str, text: str) -> bool:
        if not text.isdigit():
            return False
        options = self._bind_options.get(key)
        if not options:
            return False
        choice = int(text)
        if choice <= 0 or choice > len(options):
            return False
        repo_id = options[choice - 1]
        self._bind_options.pop(key, None)
        self._enqueue_topic_work(
            key,
            lambda: self._bind_topic_by_repo_id(key, repo_id),
        )
        return True

    async def _handle_callback(self, callback: TelegramCallbackQuery) -> None:
        parsed = parse_callback_data(callback.data)
        if parsed is None:
            return
        key = None
        if callback.chat_id is not None:
            key = topic_key(callback.chat_id, callback.thread_id)
        if isinstance(parsed, ApprovalCallback):
            await self._handle_approval_callback(callback, parsed)
        elif isinstance(parsed, ResumeCallback):
            if key:
                options = self._resume_options.get(key)
                if not options or parsed.thread_id not in options:
                    await self._answer_callback(callback, "Selection expired")
                    return
                await self._resume_thread_by_id(key, parsed.thread_id, callback)
        elif isinstance(parsed, BindCallback):
            if key:
                options = self._bind_options.get(key)
                if not options or parsed.repo_id not in options:
                    await self._answer_callback(callback, "Selection expired")
                    return
                await self._bind_topic_by_repo_id(key, parsed.repo_id, callback)

    def _enqueue_topic_work(self, key: str, work: Any) -> None:
        runtime = self._router.runtime_for(key)
        if self._config.concurrency.per_topic_queue:
            self._spawn_task(runtime.queue.enqueue(work))
        else:
            self._spawn_task(work())

    async def _handle_command(
        self,
        command: TelegramCommand,
        message: TelegramMessage,
        runtime: Any,
    ) -> None:
        name = command.name
        args = command.args
        log_event(
            self._logger,
            logging.INFO,
            "telegram.command",
            name=name,
            args_len=len(args),
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
        )
        key = topic_key(message.chat_id, message.thread_id)
        if name == "bind":
            await self._handle_bind(message, args)
            return
        if name == "new":
            await self._handle_new(message)
            return
        if name == "resume":
            await self._handle_resume(message, args)
            return
        if name == "status":
            await self._handle_status(message)
            return
        if name == "approvals":
            await self._handle_approvals(message, args)
            return
        if name == "help":
            await self._send_message(
                message.chat_id,
                _help_text(),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if name == "interrupt":
            await self._handle_interrupt(message, runtime)
            return
        self._resume_options.pop(key, None)
        self._bind_options.pop(key, None)
        await self._send_message(
            message.chat_id,
            f"Unsupported command: /{name}. Send /help for options.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_normal_message(
        self, message: TelegramMessage, runtime: Any
    ) -> None:
        key = topic_key(message.chat_id, message.thread_id)
        record = self._router.get_topic(key)
        if record is None or not record.workspace_path:
            await self._send_message(
                message.chat_id,
                "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        thread_id = record.active_thread_id
        turn_handle = None
        try:
            if not thread_id:
                thread = await self._client.thread_start(record.workspace_path)
                thread_id = thread.get("id") if isinstance(thread, dict) else None
                if not thread_id:
                    await self._send_message(
                        message.chat_id,
                        "Failed to start a new Codex thread.",
                        thread_id=message.thread_id,
                        reply_to=message.message_id,
                    )
                    return
            self._router.set_active_thread(
                message.chat_id, message.thread_id, thread_id
            )
            approval_policy, sandbox_policy = self._config.defaults.policies_for_mode(
                record.approval_mode
            )
            log_event(
                self._logger,
                logging.INFO,
                "telegram.turn.starting",
                topic_key=key,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                codex_thread_id=thread_id,
                approval_mode=record.approval_mode,
                approval_policy=approval_policy,
                sandbox_policy=sandbox_policy,
            )

            async with self._turn_semaphore:
                turn_handle = await self._client.turn_start(
                    thread_id,
                    message.text or "",
                    approval_policy=approval_policy,
                    sandbox_policy=sandbox_policy,
                )
                runtime.current_turn_id = turn_handle.turn_id
                self._turn_contexts[turn_handle.turn_id] = TurnContext(
                    topic_key=key,
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    reply_to_message_id=message.message_id,
                )
                result = await turn_handle.wait()
        except Exception as exc:
            if turn_handle is not None:
                self._turn_contexts.pop(turn_handle.turn_id, None)
            runtime.current_turn_id = None
            runtime.interrupt_requested = False
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.turn.failed",
                topic_key=key,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                "Codex turn failed; check logs for details.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        finally:
            if turn_handle is not None:
                self._turn_contexts.pop(turn_handle.turn_id, None)
            runtime.current_turn_id = None
            runtime.interrupt_requested = False

        response = _compose_agent_response(result.agent_messages)
        if result.status == "interrupted" or runtime.interrupt_requested:
            response = _compose_interrupt_response(response)
            runtime.interrupt_requested = False
        log_event(
            self._logger,
            logging.INFO,
            "telegram.turn.completed",
            topic_key=key,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            turn_id=turn_handle.turn_id if turn_handle else None,
            status=result.status,
            agent_message_count=len(result.agent_messages),
        )
        await self._send_message(
            message.chat_id,
            response,
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_interrupt(self, message: TelegramMessage, runtime: Any) -> None:
        turn_id = runtime.current_turn_id
        if not turn_id:
            log_event(
                self._logger,
                logging.INFO,
                "telegram.interrupt.none",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                message_id=message.message_id,
            )
            await self._send_message(
                message.chat_id,
                "No active turn to interrupt.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        runtime.interrupt_requested = True
        log_event(
            self._logger,
            logging.INFO,
            "telegram.interrupt.requested",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
            turn_id=turn_id,
        )
        await self._client.turn_interrupt(turn_id)

    async def _handle_bind(self, message: TelegramMessage, args: str) -> None:
        key = topic_key(message.chat_id, message.thread_id)
        if not args:
            options = self._list_manifest_repos()
            if not options:
                await self._send_message(
                    message.chat_id,
                    "Usage: /bind <repo_id> or /bind <path>.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            labels = [f"{idx}) {repo_id}" for idx, repo_id in enumerate(options, 1)]
            keyboard = build_bind_keyboard(list(zip(options, labels)))
            self._bind_options[key] = list(options)
            await self._send_message(
                message.chat_id,
                "Select a repo to bind:\n" + "\n".join(labels),
                thread_id=message.thread_id,
                reply_to=message.message_id,
                reply_markup=keyboard,
            )
            return
        await self._bind_topic_with_arg(key, args, message)

    async def _bind_topic_by_repo_id(
        self,
        key: str,
        repo_id: str,
        callback: Optional[TelegramCallbackQuery] = None,
    ) -> None:
        self._bind_options.pop(key, None)
        resolved = self._resolve_workspace(repo_id)
        if resolved is None:
            await self._answer_callback(callback, "Repo not found")
            return
        workspace_path, resolved_repo_id = resolved
        chat_id, thread_id = _split_topic_key(key)
        self._router.bind_topic(
            chat_id,
            thread_id,
            workspace_path,
            repo_id=resolved_repo_id,
        )
        await self._answer_callback(callback, "Bound to repo")
        await self._send_message(
            chat_id,
            f"Bound to {resolved_repo_id or workspace_path}.",
            thread_id=thread_id,
        )

    async def _bind_topic_with_arg(
        self, key: str, arg: str, message: TelegramMessage
    ) -> None:
        self._bind_options.pop(key, None)
        resolved = self._resolve_workspace(arg)
        if resolved is None:
            await self._send_message(
                message.chat_id,
                "Unknown repo or path. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        workspace_path, repo_id = resolved
        self._router.bind_topic(
            message.chat_id,
            message.thread_id,
            workspace_path,
            repo_id=repo_id,
        )
        await self._send_message(
            message.chat_id,
            f"Bound to {repo_id or workspace_path}.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_new(self, message: TelegramMessage) -> None:
        key = topic_key(message.chat_id, message.thread_id)
        record = self._router.get_topic(key)
        if record is None or not record.workspace_path:
            await self._send_message(
                message.chat_id,
                "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        thread = await self._client.thread_start(record.workspace_path)
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        if not thread_id:
            await self._send_message(
                message.chat_id,
                "Failed to start a new Codex thread.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        self._router.set_active_thread(message.chat_id, message.thread_id, thread_id)
        await self._send_message(
            message.chat_id,
            f"Started new thread {thread_id}.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_resume(self, message: TelegramMessage, args: str) -> None:
        key = topic_key(message.chat_id, message.thread_id)
        if args.strip().isdigit():
            options = self._resume_options.get(key)
            if options:
                choice = int(args.strip())
                if 0 < choice <= len(options):
                    thread_id = options[choice - 1]
                    await self._resume_thread_by_id(key, thread_id)
                    return
        if args.strip() and not args.strip().isdigit():
            await self._resume_thread_by_id(key, args.strip())
            return
        record = self._router.get_topic(key)
        if record is None or not record.workspace_path:
            await self._send_message(
                message.chat_id,
                "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        try:
            threads = await self._client.thread_list()
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.resume.failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                "Failed to list threads; check logs for details.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if isinstance(threads, dict) and isinstance(threads.get("threads"), list):
            threads = threads.get("threads")
        filtered = _filter_threads(threads, record.workspace_path)
        if not filtered:
            await self._send_message(
                message.chat_id,
                "No previous threads found for this workspace.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        options = filtered[:DEFAULT_RESUME_LIMIT]
        labels = [
            f"{idx}) {_compact_preview(entry.get('preview'))}"
            for idx, entry in enumerate(options, 1)
        ]
        thread_ids = [entry.get("id") for entry in options if entry.get("id")]
        if not thread_ids:
            await self._send_message(
                message.chat_id,
                "No resumable threads found.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        keyboard = build_resume_keyboard(list(zip(thread_ids, labels)))
        self._resume_options[key] = list(thread_ids)
        await self._send_message(
            message.chat_id,
            "Select a thread to resume:\n" + "\n".join(labels),
            thread_id=message.thread_id,
            reply_to=message.message_id,
            reply_markup=keyboard,
        )

    async def _resume_thread_by_id(
        self,
        key: str,
        thread_id: str,
        callback: Optional[TelegramCallbackQuery] = None,
    ) -> None:
        self._resume_options.pop(key, None)
        try:
            await self._client.thread_resume(thread_id)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.resume.failed",
                topic_key=key,
                thread_id=thread_id,
                exc=exc,
            )
            await self._answer_callback(callback, "Resume failed")
            chat_id, thread_id_val = _split_topic_key(key)
            await self._send_message(
                chat_id,
                "Failed to resume thread; check logs for details.",
                thread_id=thread_id_val,
            )
            return
        chat_id, thread_id_val = _split_topic_key(key)
        self._router.set_active_thread(chat_id, thread_id_val, thread_id)
        await self._answer_callback(callback, "Resumed thread")
        await self._send_message(
            chat_id,
            f"Resumed thread {thread_id}.",
            thread_id=thread_id_val,
        )

    async def _handle_status(self, message: TelegramMessage) -> None:
        key = topic_key(message.chat_id, message.thread_id)
        record = self._router.get_topic(key)
        if record is None:
            await self._send_message(
                message.chat_id,
                "Topic not bound.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        lines = [
            f"Workspace: {record.workspace_path or 'unbound'}",
            f"Active thread: {record.active_thread_id or 'none'}",
            f"Approval mode: {record.approval_mode}",
        ]
        await self._send_message(
            message.chat_id,
            "\n".join(lines),
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_approvals(self, message: TelegramMessage, args: str) -> None:
        key = topic_key(message.chat_id, message.thread_id)
        mode = args.strip().lower()
        if not mode:
            record = self._router.get_topic(key)
            current = record.approval_mode if record else "unknown"
            await self._send_message(
                message.chat_id,
                f"Approval mode: {current}. Use /approvals yolo|safe.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if mode in ("yolo", "off", "disable", "disabled"):
            target = "yolo"
        elif mode in ("safe", "on", "enable", "enabled"):
            target = "safe"
        else:
            await self._send_message(
                message.chat_id,
                "Usage: /approvals yolo|safe",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        self._router.set_approval_mode(message.chat_id, message.thread_id, target)
        await self._send_message(
            message.chat_id,
            f"Approval mode set to {target}.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_approval_request(self, message: dict[str, Any]) -> ApprovalDecision:
        req_id = message.get("id")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        turn_id = params.get("turnId") if isinstance(params, dict) else None
        if not req_id or not turn_id:
            return "cancel"
        ctx = self._turn_contexts.get(str(turn_id))
        if ctx is None:
            return "cancel"
        request_id = str(req_id)
        prompt = _format_approval_prompt(message)
        log_event(
            self._logger,
            logging.INFO,
            "telegram.approval.requested",
            request_id=request_id,
            turn_id=turn_id,
            chat_id=ctx.chat_id,
            thread_id=ctx.thread_id,
        )
        try:
            keyboard = build_approval_keyboard(request_id, include_session=False)
        except ValueError:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.approval.callback_too_long",
                request_id=request_id,
            )
            return "cancel"
        response = await self._bot.send_message(
            ctx.chat_id,
            prompt,
            message_thread_id=ctx.thread_id,
            reply_to_message_id=ctx.reply_to_message_id,
            reply_markup=keyboard,
        )
        message_id = response.get("message_id") if isinstance(response, dict) else None
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ApprovalDecision] = loop.create_future()
        self._pending_approvals[request_id] = PendingApproval(
            request_id=request_id,
            chat_id=ctx.chat_id,
            thread_id=ctx.thread_id,
            message_id=message_id if isinstance(message_id, int) else None,
            future=future,
        )
        return await future

    async def _handle_approval_callback(
        self, callback: TelegramCallbackQuery, parsed: ApprovalCallback
    ) -> None:
        pending = self._pending_approvals.pop(parsed.request_id, None)
        if pending is None:
            await self._answer_callback(callback, "Approval already handled")
            return
        if not pending.future.done():
            pending.future.set_result(parsed.decision)
        log_event(
            self._logger,
            logging.INFO,
            "telegram.approval.decision",
            request_id=parsed.request_id,
            decision=parsed.decision,
            chat_id=callback.chat_id,
            thread_id=callback.thread_id,
            message_id=callback.message_id,
        )
        await self._answer_callback(callback, f"Decision: {parsed.decision}")
        if pending.message_id is not None:
            try:
                await self._bot.edit_message_text(
                    pending.chat_id,
                    pending.message_id,
                    _format_approval_decision(parsed.decision),
                )
            except Exception:
                return

    async def _send_message(
        self,
        chat_id: int,
        text: str,
        *,
        thread_id: Optional[int] = None,
        reply_to: Optional[int] = None,
        reply_markup: Optional[dict[str, Any]] = None,
    ) -> None:
        await self._bot.send_message_chunks(
            chat_id,
            text,
            message_thread_id=thread_id,
            reply_to_message_id=reply_to,
            reply_markup=reply_markup,
        )

    async def _answer_callback(
        self, callback: Optional[TelegramCallbackQuery], text: str
    ) -> None:
        if callback is None:
            return
        await self._bot.answer_callback_query(callback.callback_id, text=text)

    def _list_manifest_repos(self) -> list[str]:
        if not self._manifest_path or not self._hub_root:
            return []
        try:
            manifest = load_manifest(self._manifest_path, self._hub_root)
        except Exception:
            return []
        repo_ids = [repo.id for repo in manifest.repos if repo.enabled]
        return repo_ids[:DEFAULT_BIND_LIMIT]

    def _resolve_workspace(self, arg: str) -> Optional[tuple[str, Optional[str]]]:
        arg = (arg or "").strip()
        if not arg:
            return None
        if self._manifest_path and self._hub_root:
            try:
                manifest = load_manifest(self._manifest_path, self._hub_root)
                repo = manifest.get(arg)
                if repo:
                    workspace = (self._hub_root / repo.path).resolve()
                    return str(workspace), repo.id
            except Exception:
                pass
        path = Path(arg)
        if not path.is_absolute():
            path = (self._config.root / path).resolve()
        if path.exists():
            return str(path), None
        return None


def _parse_command(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item) for item in raw if item]
    if isinstance(raw, str):
        return [part for part in shlex.split(raw) if part]
    return []


def _parse_int_list(raw: Any) -> list[int]:
    values: list[int] = []
    if raw is None:
        return values
    if isinstance(raw, int):
        return [raw]
    if isinstance(raw, str):
        parts = [part for part in re.split(r"[,\s]+", raw.strip()) if part]
        for part in parts:
            try:
                values.append(int(part))
            except ValueError:
                continue
        return values
    if isinstance(raw, Iterable):
        for item in raw:
            values.extend(_parse_int_list(item))
    return values


def _filter_threads(threads: Any, workspace_path: str) -> list[dict[str, Any]]:
    if not isinstance(threads, list):
        return []
    workspace = Path(workspace_path).resolve()
    filtered: list[dict[str, Any]] = []
    for entry in threads:
        if not isinstance(entry, dict):
            continue
        cwd = entry.get("cwd") or entry.get("path")
        if not isinstance(cwd, str):
            continue
        try:
            candidate = Path(cwd).resolve()
            if _path_within(workspace, candidate):
                filtered.append(entry)
        except Exception:
            continue
    return filtered


def _path_within(root: Path, target: Path) -> bool:
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


def _compact_preview(text: Any, limit: int = 60) -> str:
    preview = " ".join(str(text or "").split())
    if len(preview) > limit:
        return preview[: limit - 3] + "..."
    return preview or "(no preview)"


def _compose_agent_response(messages: list[str]) -> str:
    cleaned = [msg.strip() for msg in messages if isinstance(msg, str) and msg.strip()]
    if not cleaned:
        return "(No agent response.)"
    return "\n\n".join(cleaned)


def _compose_interrupt_response(agent_text: str) -> str:
    base = "Interrupted."
    if agent_text and agent_text != "(No agent response.)":
        return f"{base}\n\n{agent_text}"
    return base


def _format_approval_prompt(message: dict[str, Any]) -> str:
    method = message.get("method")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    lines = ["Approval required"]
    reason = params.get("reason")
    if isinstance(reason, str) and reason:
        lines.append(f"Reason: {reason}")
    if method == "item/commandExecution/requestApproval":
        command = params.get("command")
        if command:
            lines.append(f"Command: {command}")
    elif method == "item/fileChange/requestApproval":
        files = _extract_files(params)
        if files:
            if len(files) == 1:
                lines.append(f"File: {files[0]}")
            else:
                lines.append("Files:")
                lines.extend([f"- {path}" for path in files[:10]])
                if len(files) > 10:
                    lines.append("- ...")
    return "\n".join(lines)


def _format_approval_decision(decision: str) -> str:
    return f"Approval {decision}."


def _extract_files(params: dict[str, Any]) -> list[str]:
    files: list[str] = []
    for key in ("files", "fileChanges", "paths"):
        payload = params.get(key)
        if isinstance(payload, list):
            for entry in payload:
                if isinstance(entry, str) and entry:
                    files.append(entry)
                elif isinstance(entry, dict):
                    path = entry.get("path") or entry.get("file") or entry.get("name")
                    if isinstance(path, str) and path:
                        files.append(path)
    return files


def _split_topic_key(key: str) -> tuple[int, Optional[int]]:
    chat_raw, _, thread_raw = key.partition(":")
    chat_id = int(chat_raw)
    thread_id = None
    if thread_raw and thread_raw != "root":
        thread_id = int(thread_raw)
    return chat_id, thread_id


def _help_text() -> str:
    return "\n".join(
        [
            "Commands:",
            "/bind <repo_id|path> - bind this topic to a workspace",
            "/new - start a new session",
            "/resume - pick a previous session",
            "/interrupt - stop a running turn",
            "/approvals yolo|safe - toggle approvals",
            "/status - show current binding",
            "/help - show this message",
        ]
    )
