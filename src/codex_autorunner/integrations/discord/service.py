from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import re
import sqlite3
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from ...core.config import load_repo_config, resolve_env_for_root
from ...core.filebox import (
    inbox_dir,
    outbox_pending_dir,
    outbox_sent_dir,
)
from ...core.flows import (
    FlowRunRecord,
    FlowRunStatus,
    FlowStore,
    archive_flow_run_artifacts,
    load_latest_paused_ticket_flow_dispatch,
)
from ...core.flows.reconciler import reconcile_flow_run
from ...core.flows.surface_defaults import should_route_flow_read_to_hub_overview
from ...core.flows.ux_helpers import (
    build_flow_status_snapshot,
    ensure_worker,
    issue_md_path,
    seed_issue_from_github,
    seed_issue_from_text,
    ticket_progress,
)
from ...core.git_utils import GitError, reset_branch_from_origin_main
from ...core.injected_context import wrap_injected_context
from ...core.logging_utils import log_event
from ...core.pma_context import build_hub_snapshot, format_pma_prompt, load_pma_prompt
from ...core.pma_delivery_targets import PmaDeliveryTargetsStore, target_key
from ...core.pma_target_refs import parse_pma_target_ref, pma_targets_usage
from ...core.ports.run_event import (
    RUN_EVENT_DELTA_TYPE_USER_MESSAGE,
    ApprovalRequested,
    Completed,
    Failed,
    OutputDelta,
    RunNotice,
    Started,
    TokenUsage,
    ToolCall,
)
from ...core.state import RunnerState
from ...core.state_roots import resolve_global_state_root
from ...core.ticket_flow_summary import build_ticket_flow_display
from ...core.update import (
    UpdateInProgressError,
    _normalize_update_ref,
    _normalize_update_target,
    _read_update_status,
    _spawn_update_process,
)
from ...core.update_paths import resolve_update_paths
from ...core.utils import (
    atomic_write,
    canonicalize_path,
)
from ...flows.ticket_flow.runtime_helpers import build_ticket_flow_controller
from ...integrations.agents.backend_orchestrator import BackendOrchestrator
from ...integrations.app_server.client import CodexAppServerClient
from ...integrations.app_server.env import build_app_server_env
from ...integrations.app_server.supervisor import WorkspaceAppServerSupervisor
from ...integrations.app_server.threads import (
    FILE_CHAT_OPENCODE_PREFIX,
    FILE_CHAT_PREFIX,
    PMA_KEY,
    PMA_OPENCODE_KEY,
)
from ...integrations.chat.bootstrap import ChatBootstrapStep, run_chat_bootstrap_steps
from ...integrations.chat.channel_directory import ChannelDirectoryStore
from ...integrations.chat.command_ingress import canonicalize_command_ingress
from ...integrations.chat.dispatcher import (
    ChatDispatcher,
    DispatchContext,
    DispatchResult,
)
from ...integrations.chat.models import (
    ChatEvent,
    ChatInteractionEvent,
    ChatMessageEvent,
)
from ...integrations.chat.run_mirror import ChatRunMirror
from ...integrations.chat.turn_policy import (
    PlainTextTurnContext,
    should_trigger_plain_text_turn,
)
from ...integrations.chat.update_notifier import (
    ChatUpdateStatusNotifier,
    format_update_status_message,
    mark_update_status_notified,
)
from ...integrations.github.context_injection import maybe_inject_github_context
from ...integrations.github.service import (
    GitHubError,
    GitHubService,
)
from ...manifest import load_manifest
from ...tickets.outbox import resolve_outbox_paths
from ...voice import VoiceConfig, VoiceService, VoiceServiceError
from ..telegram.helpers import (
    _coerce_thread_list,
    _extract_context_usage_percent,
    _extract_thread_list_cursor,
    _format_turn_metrics,
)
from ..telegram.progress_stream import TurnProgressTracker, render_progress_text
from .adapter import DiscordChatAdapter
from .allowlist import DiscordAllowlist, allowlist_allows
from .command_registry import sync_commands
from .commands import build_application_commands
from .components import (
    DISCORD_SELECT_OPTION_MAX_OPTIONS,
    build_bind_picker,
    build_cancel_turn_button,
    build_flow_runs_picker,
    build_flow_status_buttons,
)
from .config import DiscordBotConfig
from .errors import DiscordAPIError, DiscordTransientError
from .gateway import DiscordGatewayClient
from .interactions import (
    extract_channel_id,
    extract_command_path_and_options,
    extract_component_custom_id,
    extract_component_values,
    extract_guild_id,
    extract_interaction_id,
    extract_interaction_token,
    extract_user_id,
    is_component_interaction,
)
from .outbox import DiscordOutboxManager
from .rendering import (
    chunk_discord_message,
    format_discord_message,
    truncate_for_discord,
)
from .rest import DiscordRestClient
from .state import DiscordStateStore, OutboxRecord

DISCORD_EPHEMERAL_FLAG = 64
PAUSE_SCAN_INTERVAL_SECONDS = 5.0
FLOW_RUNS_DEFAULT_LIMIT = 5
FLOW_RUNS_MAX_LIMIT = DISCORD_SELECT_OPTION_MAX_OPTIONS
MESSAGE_TURN_APPROVAL_POLICY = "never"
MESSAGE_TURN_SANDBOX_POLICY = "dangerFullAccess"
DEFAULT_UPDATE_REPO_URL = "https://github.com/Git-on-my-level/codex-autorunner.git"
DEFAULT_UPDATE_REPO_REF = "main"
DISCORD_TURN_PROGRESS_MIN_EDIT_INTERVAL_SECONDS = 1.0
DISCORD_TURN_PROGRESS_HEARTBEAT_INTERVAL_SECONDS = 2.0
DISCORD_TURN_PROGRESS_MAX_ACTIONS = 8
DISCORD_TURN_PROGRESS_MAX_OUTPUT_CHARS = 120
SHELL_OUTPUT_TRUNCATION_SUFFIX = "\n...[truncated]..."
DISCORD_ATTACHMENT_MAX_BYTES = 100_000_000
THREAD_LIST_MAX_PAGES = 5
THREAD_LIST_PAGE_LIMIT = 100
APP_SERVER_START_BACKOFF_INITIAL_SECONDS = 1.0
APP_SERVER_START_BACKOFF_MAX_SECONDS = 30.0
DISCORD_QUEUED_PLACEHOLDER_TEXT = "Queued (waiting for available worker...)"
DISCORD_WHISPER_TRANSCRIPT_DISCLAIMER = (
    "Note: transcribed from user voice. If confusing or possibly inaccurate and you "
    "cannot infer the intention please clarify before proceeding."
)


class AppServerUnavailableError(Exception):
    pass


@dataclass(frozen=True)
class DiscordMessageTurnResult:
    final_message: str
    preview_message_id: Optional[str] = None
    token_usage: Optional[dict[str, Any]] = None
    elapsed_seconds: Optional[float] = None


@dataclass(frozen=True)
class _SavedDiscordAttachment:
    original_name: str
    path: Path
    mime_type: Optional[str]
    size_bytes: int
    transcript_text: Optional[str] = None
    transcript_warning: Optional[str] = None


class DiscordBotService:
    def __init__(
        self,
        config: DiscordBotConfig,
        *,
        logger: logging.Logger,
        rest_client: Optional[DiscordRestClient] = None,
        gateway_client: Optional[DiscordGatewayClient] = None,
        state_store: Optional[DiscordStateStore] = None,
        outbox_manager: Optional[DiscordOutboxManager] = None,
        manifest_path: Optional[Path] = None,
        chat_adapter: Optional[DiscordChatAdapter] = None,
        dispatcher: Optional[ChatDispatcher] = None,
        backend_orchestrator_factory: Optional[
            Callable[[Path], BackendOrchestrator]
        ] = None,
        update_repo_url: Optional[str] = None,
        update_repo_ref: Optional[str] = None,
        update_skip_checks: bool = False,
        update_backend: str = "auto",
        update_linux_service_names: Optional[dict[str, str]] = None,
        voice_config: Optional[VoiceConfig] = None,
        voice_service: Optional[VoiceService] = None,
    ) -> None:
        self._config = config
        self._logger = logger
        self._manifest_path = manifest_path
        self._backend_orchestrator_factory = backend_orchestrator_factory
        self._update_repo_url = update_repo_url
        self._update_repo_ref = update_repo_ref
        self._update_skip_checks = update_skip_checks
        self._update_backend = update_backend
        self._update_linux_service_names = update_linux_service_names or {}
        self._process_env: dict[str, str] = dict(os.environ)
        self._voice_config = voice_config
        self._voice_service = voice_service
        self._voice_configs_by_workspace: dict[Path, VoiceConfig] = {}
        self._voice_services_by_workspace: dict[Path, Optional[VoiceService]] = {}

        self._rest = (
            rest_client
            if rest_client is not None
            else DiscordRestClient(bot_token=config.bot_token or "")
        )
        self._owns_rest = rest_client is None

        self._gateway = (
            gateway_client
            if gateway_client is not None
            else DiscordGatewayClient(
                bot_token=config.bot_token or "",
                intents=config.intents,
                logger=logger,
            )
        )
        self._owns_gateway = gateway_client is None

        self._store = (
            state_store
            if state_store is not None
            else DiscordStateStore(config.state_file)
        )
        self._owns_store = state_store is None

        self._outbox = (
            outbox_manager
            if outbox_manager is not None
            else DiscordOutboxManager(
                self._store,
                send_message=self._send_channel_message,
                delete_message=self._delete_channel_message,
                logger=logger,
            )
        )
        self._allowlist = DiscordAllowlist(
            allowed_guild_ids=config.allowed_guild_ids,
            allowed_channel_ids=config.allowed_channel_ids,
            allowed_user_ids=config.allowed_user_ids,
        )

        self._chat_adapter = (
            chat_adapter
            if chat_adapter is not None
            else DiscordChatAdapter(
                rest_client=self._rest,
                application_id=config.application_id or "",
                logger=logger,
                message_overflow=config.message_overflow,
            )
        )
        self._dispatcher = dispatcher or ChatDispatcher(
            logger=logger,
            allowlist_predicate=lambda event, context: self._allowlist_predicate(
                event, context
            ),
            bypass_predicate=lambda event, context: self._bypass_predicate(
                event, context
            ),
        )
        self._backend_orchestrators: dict[str, BackendOrchestrator] = {}
        self._backend_lock = asyncio.Lock()
        self._app_server_supervisors: dict[str, WorkspaceAppServerSupervisor] = {}
        self._app_server_lock = asyncio.Lock()
        self._app_server_state_root = resolve_global_state_root() / "workspaces"
        self._channel_directory_store = ChannelDirectoryStore(self._config.root)
        self._hub_config_path: Optional[Path] = None
        generated_hub_config = self._config.root / ".codex-autorunner" / "config.yml"
        if generated_hub_config.exists():
            self._hub_config_path = generated_hub_config
        else:
            root_hub_config = self._config.root / "codex-autorunner.yml"
            if root_hub_config.exists():
                self._hub_config_path = root_hub_config

        self._hub_supervisor = None
        try:
            from ...core.hub import HubSupervisor

            self._hub_supervisor = HubSupervisor.from_path(self._config.root)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.pma.hub_supervisor.unavailable",
                hub_root=str(self._config.root),
                exc=exc,
            )
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._update_status_notifier = ChatUpdateStatusNotifier(
            platform="discord",
            logger=self._logger,
            read_status=_read_update_status,
            send_notice=self._send_update_status_notice,
            spawn_task=self._spawn_task,
            mark_notified=self._mark_update_notified,
            format_status=self._format_update_status_message,
            running_message=(
                "Update still running. Use `/car update target:status` for current state."
            ),
        )

    async def run_forever(self) -> None:
        await self._store.initialize()
        await run_chat_bootstrap_steps(
            platform="discord",
            logger=self._logger,
            steps=(
                ChatBootstrapStep(
                    name="sync_application_commands",
                    action=self._sync_application_commands_on_startup,
                    required=True,
                ),
            ),
        )
        self._outbox.start()
        outbox_task = asyncio.create_task(self._outbox.run_loop())
        pause_watch_task = asyncio.create_task(self._watch_ticket_flow_pauses())
        dispatcher_loop_task = asyncio.create_task(self._run_dispatcher_loop())
        try:
            log_event(
                self._logger,
                logging.INFO,
                "discord.bot.starting",
                state_file=str(self._config.state_file),
            )
            try:
                await self._update_status_notifier.maybe_send_notice()
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.update.notify_failed",
                    exc=exc,
                )
            await self._gateway.run(self._on_dispatch)
        finally:
            with contextlib.suppress(Exception):
                await self._dispatcher.wait_idle()
            dispatcher_loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await dispatcher_loop_task
            pause_watch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pause_watch_task
            outbox_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await outbox_task
            await self._shutdown()

    async def _run_dispatcher_loop(self) -> None:
        while True:
            events = await self._chat_adapter.poll_events(timeout_seconds=30.0)
            for event in events:
                await self._dispatch_chat_event(event)

    async def _dispatch_chat_event(self, event: ChatEvent) -> None:
        dispatch_result = await self._dispatcher.dispatch(
            event, self._handle_chat_event
        )
        await self._maybe_send_queued_notice(event, dispatch_result)

    @staticmethod
    def _is_turn_candidate_message_event(event: ChatMessageEvent) -> bool:
        text = (event.text or "").strip()
        has_attachments = bool(event.attachments)
        if not text and not has_attachments:
            return False
        if text.startswith("/"):
            return False
        if text and not should_trigger_plain_text_turn(
            mode="always",
            context=PlainTextTurnContext(text=text),
        ):
            return False
        return True

    async def _maybe_send_queued_notice(
        self, event: ChatEvent, dispatch_result: DispatchResult
    ) -> None:
        if dispatch_result.status != "queued" or not dispatch_result.queued_while_busy:
            return
        if not isinstance(event, ChatMessageEvent):
            return
        if not self._is_turn_candidate_message_event(event):
            return
        channel_id = dispatch_result.context.chat_id
        await self._send_channel_message_safe(
            channel_id,
            {"content": format_discord_message(DISCORD_QUEUED_PLACEHOLDER_TEXT)},
            record_id=f"queue-notice:{channel_id}:{dispatch_result.context.update_id}",
        )
        log_event(
            self._logger,
            logging.INFO,
            "discord.turn.queued_notice",
            channel_id=channel_id,
            conversation_id=dispatch_result.context.conversation_id,
            update_id=dispatch_result.context.update_id,
            pending=dispatch_result.queued_pending,
        )

    async def _handle_chat_event(
        self, event: ChatEvent, context: DispatchContext
    ) -> None:
        if isinstance(event, ChatInteractionEvent):
            await self._handle_normalized_interaction(event, context)
            return
        if isinstance(event, ChatMessageEvent):
            await self._handle_message_event(event, context)
            return

    def _allowlist_predicate(self, event: ChatEvent, context: DispatchContext) -> bool:
        if isinstance(event, ChatInteractionEvent):
            # Interaction denials should return an ephemeral response rather than
            # being dropped at dispatcher level.
            return True
        return self._allowlist_allows_context(context)

    def _allowlist_allows_context(self, context: DispatchContext) -> bool:
        fake_payload = {
            "channel_id": context.chat_id,
            "guild_id": context.thread_id if context.thread_id else None,
            "member": {"user": {"id": context.user_id}} if context.user_id else None,
        }
        return allowlist_allows(fake_payload, self._allowlist)

    def _bypass_predicate(self, event: ChatEvent, context: DispatchContext) -> bool:
        if isinstance(event, ChatInteractionEvent):
            return True
        return False

    async def _handle_normalized_interaction(
        self, event: ChatInteractionEvent, context: DispatchContext
    ) -> None:
        import json

        payload_str = event.payload or "{}"
        try:
            payload_data = json.loads(payload_str)
        except json.JSONDecodeError:
            payload_data = {}

        interaction_id = payload_data.get(
            "_discord_interaction_id", event.interaction.interaction_id
        )
        interaction_token = payload_data.get("_discord_token")
        channel_id = context.chat_id

        if not interaction_id or not interaction_token or not channel_id:
            self._logger.warning(
                "handle_normalized_interaction: missing required fields (interaction_id=%s, token=%s, channel=%s)",
                bool(interaction_id),
                bool(interaction_token),
                bool(channel_id),
            )
            return

        if not self._allowlist_allows_context(context):
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "This Discord command is not authorized for this channel/user/guild.",
            )
            return

        if payload_data.get("type") == "component":
            custom_id = payload_data.get("component_id")
            if not custom_id:
                self._logger.debug(
                    "handle_normalized_interaction: missing component_id (interaction_id=%s)",
                    interaction_id,
                )
                await self._respond_ephemeral(
                    interaction_id,
                    interaction_token,
                    "I could not identify this interaction action. Please retry.",
                )
                return
            await self._handle_component_interaction_normalized(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                channel_id=channel_id,
                custom_id=custom_id,
                values=payload_data.get("values"),
                guild_id=payload_data.get("guild_id"),
            )
            return

        ingress = canonicalize_command_ingress(
            command=payload_data.get("command"),
            options=payload_data.get("options"),
        )
        command = ingress.command if ingress is not None else ""
        guild_id = payload_data.get("guild_id")

        if ingress is None:
            self._logger.warning(
                "handle_normalized_interaction: failed to canonicalize command ingress (payload=%s)",
                payload_data,
            )
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "I could not parse this interaction. Please retry the command.",
            )
            return

        try:
            if ingress.command_path[:1] == ("car",):
                await self._handle_car_command(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                    guild_id=context.thread_id,
                    user_id=event.from_user_id,
                    command_path=ingress.command_path,
                    options=ingress.options,
                )
            elif ingress.command_path[:1] == ("pma",):
                await self._handle_pma_command_from_normalized(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                    guild_id=guild_id,
                    command_path=ingress.command_path,
                    options=ingress.options,
                )
            else:
                await self._respond_ephemeral(
                    interaction_id,
                    interaction_token,
                    "Command not implemented yet for Discord.",
                )
        except DiscordTransientError as exc:
            user_msg = exc.user_message or "An error occurred. Please try again later."
            await self._respond_ephemeral(interaction_id, interaction_token, user_msg)
        except Exception as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "discord.interaction.unhandled_error",
                command=command,
                channel_id=channel_id,
                exc=exc,
            )
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "An unexpected error occurred. Please try again later.",
            )

    async def _handle_message_event(
        self,
        event: ChatMessageEvent,
        context: DispatchContext,
    ) -> None:
        channel_id = context.chat_id
        text = (event.text or "").strip()
        has_attachments = bool(event.attachments)
        if not text and not has_attachments:
            return
        if text.startswith("/"):
            return
        if text and not should_trigger_plain_text_turn(
            mode="always",
            context=PlainTextTurnContext(text=text),
        ):
            return

        binding = await self._store.get_binding(channel_id=channel_id)
        if binding is None:
            content = format_discord_message(
                "This channel is not bound. Run `/car bind path:<workspace>` or `/pma on`."
            )
            await self._send_channel_message_safe(
                channel_id,
                {"content": content},
            )
            return

        pma_enabled = bool(binding.get("pma_enabled", False))
        workspace_raw = binding.get("workspace_path")
        workspace_root: Optional[Path] = None
        if pma_enabled:
            # PMA turns are hub-scoped. Prefer the hub root even when this channel
            # was previously bound to a repo workspace.
            fallback = canonicalize_path(Path(self._config.root))
            if fallback.exists() and fallback.is_dir():
                workspace_root = fallback

        if (
            workspace_root is None
            and isinstance(workspace_raw, str)
            and workspace_raw.strip()
        ):
            candidate = canonicalize_path(Path(workspace_raw))
            if candidate.exists() and candidate.is_dir():
                workspace_root = candidate

        if workspace_root is None:
            content = format_discord_message(
                "Binding is invalid. Run `/car bind path:<workspace>`."
            )
            await self._send_channel_message_safe(
                channel_id,
                {"content": content},
            )
            return

        if not pma_enabled:
            paused = await self._find_paused_flow_run(workspace_root)
            if paused is not None:
                reply_text = text
                if has_attachments:
                    (
                        reply_text,
                        saved_attachments,
                        failed_attachments,
                    ) = await self._with_attachment_context(
                        prompt_text=text,
                        workspace_root=workspace_root,
                        attachments=event.attachments,
                        channel_id=channel_id,
                    )
                    if failed_attachments > 0:
                        warning = (
                            "Some Discord attachments could not be downloaded. "
                            "Continuing with available inputs."
                        )
                        await self._send_channel_message_safe(
                            channel_id,
                            {"content": warning},
                        )
                    if not reply_text.strip() and saved_attachments == 0:
                        await self._send_channel_message_safe(
                            channel_id,
                            {
                                "content": (
                                    "Failed to download attachments from Discord. "
                                    "Please retry."
                                ),
                            },
                        )
                        return

                reply_path = self._write_user_reply(workspace_root, paused, reply_text)
                run_mirror = self._flow_run_mirror(workspace_root)
                run_mirror.mirror_inbound(
                    run_id=paused.id,
                    platform="discord",
                    event_type="flow_reply_message",
                    kind="command",
                    actor="user",
                    text=reply_text,
                    chat_id=channel_id,
                    thread_id=event.thread.thread_id,
                    message_id=event.message.message_id,
                )
                controller = build_ticket_flow_controller(workspace_root)
                try:
                    updated = await controller.resume_flow(paused.id)
                except ValueError as exc:
                    await self._send_channel_message_safe(
                        channel_id,
                        {"content": f"Failed to resume paused run: {exc}"},
                    )
                    return
                ensure_result = ensure_worker(
                    workspace_root,
                    updated.id,
                    is_terminal=updated.status.is_terminal(),
                )
                self._close_worker_handles(ensure_result)
                content = format_discord_message(
                    f"Reply saved to `{reply_path.name}` and resumed paused run `{updated.id}`."
                )
                await self._send_channel_message_safe(
                    channel_id,
                    {"content": content},
                )
                run_mirror.mirror_outbound(
                    run_id=updated.id,
                    platform="discord",
                    event_type="flow_reply_notice",
                    kind="notice",
                    actor="car",
                    text=content,
                    chat_id=channel_id,
                    thread_id=event.thread.thread_id,
                )
                return

        if text.startswith("!") and not event.attachments:
            await self._handle_bang_shell(
                channel_id=channel_id,
                message_id=event.message.message_id,
                text=text,
                workspace_root=workspace_root,
            )
            return

        prompt_text = text
        (
            prompt_text,
            saved_attachments,
            failed_attachments,
        ) = await self._with_attachment_context(
            prompt_text=prompt_text,
            workspace_root=workspace_root,
            attachments=event.attachments,
            channel_id=channel_id,
        )
        if failed_attachments > 0:
            warning = (
                "Some Discord attachments could not be downloaded. "
                "Continuing with available inputs."
            )
            await self._send_channel_message_safe(
                channel_id,
                {"content": warning},
            )
        if not prompt_text.strip():
            if has_attachments and saved_attachments == 0:
                await self._send_channel_message_safe(
                    channel_id,
                    {
                        "content": "Failed to download attachments from Discord. Please retry.",
                    },
                )
            return

        if pma_enabled:
            try:
                snapshot = await build_hub_snapshot(
                    self._hub_supervisor, hub_root=self._config.root
                )
                prompt_base = load_pma_prompt(self._config.root)
                prompt_text = format_pma_prompt(
                    prompt_base,
                    snapshot,
                    prompt_text,
                    hub_root=self._config.root,
                )
                PmaDeliveryTargetsStore(self._config.root).add_target(
                    self._pma_here_target(channel_id)
                )
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.pma.prompt_build.failed",
                    channel_id=channel_id,
                    exc=exc,
                )
                await self._send_channel_message_safe(
                    channel_id,
                    {"content": "Failed to build PMA context. Please try again."},
                )
                return

        prompt_text, github_injected = await self._maybe_inject_github_context(
            prompt_text,
            workspace_root,
            link_source_text=text if pma_enabled else None,
            allow_cross_repo=pma_enabled,
        )

        agent = (binding.get("agent") or self.DEFAULT_AGENT).strip().lower()
        if agent not in self.VALID_AGENT_VALUES:
            agent = self.DEFAULT_AGENT
        model_override = binding.get("model_override")
        if not isinstance(model_override, str) or not model_override.strip():
            model_override = None
        reasoning_effort = binding.get("reasoning_effort")
        if not isinstance(reasoning_effort, str) or not reasoning_effort.strip():
            reasoning_effort = None

        session_key = self._build_message_session_key(
            channel_id=channel_id,
            workspace_root=workspace_root,
            pma_enabled=pma_enabled,
            agent=agent,
        )
        try:
            turn_result = await self._run_agent_turn_for_message(
                workspace_root=workspace_root,
                prompt_text=prompt_text,
                agent=agent,
                model_override=model_override,
                reasoning_effort=reasoning_effort,
                session_key=session_key,
                orchestrator_channel_key=(
                    channel_id if not pma_enabled else f"pma:{channel_id}"
                ),
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.turn.failed",
                channel_id=channel_id,
                conversation_id=context.conversation_id,
                workspace_root=str(workspace_root),
                agent=agent,
                exc=exc,
            )
            await self._send_channel_message_safe(
                channel_id,
                {
                    "content": (
                        f"Turn failed: {exc} "
                        f"(conversation {context.conversation_id})"
                    )
                },
            )
            return

        preview_message_id: Optional[str] = None
        if isinstance(turn_result, DiscordMessageTurnResult):
            response_text = turn_result.final_message
            preview_message_id = turn_result.preview_message_id
            metrics_text = _format_turn_metrics(
                turn_result.token_usage,
                turn_result.elapsed_seconds,
            )
            if metrics_text:
                if response_text.strip():
                    response_text = f"{response_text}\n\n{metrics_text}"
                else:
                    response_text = metrics_text
        else:
            response_text = str(turn_result or "")

        chunks = chunk_discord_message(
            response_text or "(No response text returned.)",
            max_len=self._config.max_message_length,
            with_numbering=False,
        )
        if not chunks:
            chunks = ["(No response text returned.)"]
        for idx, chunk in enumerate(chunks, 1):
            await self._send_channel_message_safe(
                channel_id,
                {"content": chunk},
                record_id=f"turn:{session_key}:{idx}:{uuid.uuid4().hex[:8]}",
            )
        if preview_message_id:
            await self._delete_channel_message_safe(
                channel_id,
                preview_message_id,
                record_id=f"turn-preview-delete:{session_key}:{uuid.uuid4().hex[:8]}",
            )

    def _voice_service_for_workspace(
        self, workspace_root: Path
    ) -> tuple[Optional[VoiceService], Optional[VoiceConfig]]:
        if self._voice_service is not None:
            return self._voice_service, self._voice_config

        resolved_root = workspace_root.resolve()
        if resolved_root in self._voice_services_by_workspace:
            return (
                self._voice_services_by_workspace[resolved_root],
                self._voice_configs_by_workspace.get(resolved_root),
            )

        try:
            repo_config = load_repo_config(
                resolved_root,
                hub_path=self._hub_config_path,
            )
            workspace_env = resolve_env_for_root(
                resolved_root,
                base_env=self._process_env,
            )
            voice_config = VoiceConfig.from_raw(repo_config.voice, env=workspace_env)
            self._voice_configs_by_workspace[resolved_root] = voice_config
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.voice.config_load_failed",
                workspace_root=str(resolved_root),
                exc=exc,
            )
            self._voice_services_by_workspace[resolved_root] = None
            return None, None

        if not voice_config.enabled:
            self._voice_services_by_workspace[resolved_root] = None
            return None, voice_config

        try:
            service = VoiceService(
                voice_config,
                logger=self._logger,
                env=workspace_env,
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.voice.init_failed",
                workspace_root=str(resolved_root),
                provider=voice_config.provider,
                exc=exc,
            )
            self._voice_services_by_workspace[resolved_root] = None
            return None, voice_config

        self._voice_services_by_workspace[resolved_root] = service
        return service, voice_config

    def _is_audio_attachment(self, attachment: Any, mime_type: Optional[str]) -> bool:
        kind = getattr(attachment, "kind", None)
        if isinstance(kind, str) and kind.lower() == "audio":
            return True
        if isinstance(mime_type, str) and mime_type.lower().startswith("audio/"):
            return True
        return False

    async def _transcribe_voice_attachment(
        self,
        *,
        workspace_root: Path,
        channel_id: str,
        attachment: Any,
        data: bytes,
        file_name: str,
        mime_type: Optional[str],
    ) -> tuple[Optional[str], Optional[str]]:
        if not self._config.media.enabled or not self._config.media.voice:
            return None, None
        if not self._is_audio_attachment(attachment, mime_type):
            return None, None
        if len(data) > self._config.media.max_voice_bytes:
            warning = (
                "Voice transcript skipped: attachment exceeds max_voice_bytes "
                f"({len(data)} > {self._config.media.max_voice_bytes})."
            )
            return None, warning

        voice_service, _voice_config = self._voice_service_for_workspace(workspace_root)
        if voice_service is None:
            return (
                None,
                "Voice transcript unavailable: provider is disabled or missing.",
            )

        try:
            result = await voice_service.transcribe_async(
                data,
                client="discord",
                filename=file_name,
                content_type=mime_type,
            )
        except VoiceServiceError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.media.voice.transcribe_failed",
                channel_id=channel_id,
                file_id=getattr(attachment, "file_id", None),
                reason=exc.reason,
            )
            return None, f"Voice transcript unavailable ({exc.reason})."
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.media.voice.transcribe_failed",
                channel_id=channel_id,
                file_id=getattr(attachment, "file_id", None),
                exc=exc,
            )
            return None, "Voice transcript unavailable (provider_error)."

        transcript = ""
        if isinstance(result, dict):
            transcript = str(result.get("text") or "")
        transcript = transcript.strip()
        if not transcript:
            return None, "Voice transcript was empty."

        log_event(
            self._logger,
            logging.INFO,
            "discord.media.voice.transcribed",
            channel_id=channel_id,
            file_id=getattr(attachment, "file_id", None),
            text_len=len(transcript),
        )
        return transcript, None

    async def _with_attachment_context(
        self,
        *,
        prompt_text: str,
        workspace_root: Path,
        attachments: tuple[Any, ...],
        channel_id: str,
    ) -> tuple[str, int, int]:
        if not attachments:
            return prompt_text, 0, 0

        inbox = inbox_dir(workspace_root)
        inbox.mkdir(parents=True, exist_ok=True)
        saved: list[_SavedDiscordAttachment] = []
        failed = 0
        for index, attachment in enumerate(attachments, start=1):
            source_url = getattr(attachment, "source_url", None)
            if not isinstance(source_url, str) or not source_url.strip():
                failed += 1
                continue
            try:
                size_bytes = getattr(attachment, "size_bytes", None)
                if (
                    isinstance(size_bytes, int)
                    and size_bytes > DISCORD_ATTACHMENT_MAX_BYTES
                ):
                    raise RuntimeError(
                        f"attachment exceeds max size ({size_bytes} > {DISCORD_ATTACHMENT_MAX_BYTES})"
                    )
                data = await self._rest.download_attachment(
                    url=source_url,
                    max_size_bytes=DISCORD_ATTACHMENT_MAX_BYTES,
                )
                file_name = self._build_attachment_filename(attachment, index=index)
                path = inbox / file_name
                path.write_bytes(data)
                original_name = getattr(attachment, "file_name", None) or path.name
                mime_type = getattr(attachment, "mime_type", None)
                transcript_text, transcript_warning = (
                    await self._transcribe_voice_attachment(
                        workspace_root=workspace_root,
                        channel_id=channel_id,
                        attachment=attachment,
                        data=data,
                        file_name=str(original_name),
                        mime_type=mime_type if isinstance(mime_type, str) else None,
                    )
                )
                saved.append(
                    _SavedDiscordAttachment(
                        original_name=str(original_name),
                        path=path,
                        mime_type=mime_type if isinstance(mime_type, str) else None,
                        size_bytes=len(data),
                        transcript_text=transcript_text,
                        transcript_warning=transcript_warning,
                    )
                )
            except Exception as exc:
                failed += 1
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.turn.attachment.download_failed",
                    channel_id=channel_id,
                    file_id=getattr(attachment, "file_id", None),
                    exc=exc,
                )

        if not saved:
            return prompt_text, 0, failed

        details: list[str] = ["Inbound Discord attachments:"]
        for item in saved:
            details.append(f"- Name: {item.original_name}")
            details.append(f"  Saved to: {item.path}")
            details.append(f"  Size: {item.size_bytes} bytes")
            if item.mime_type:
                details.append(f"  Mime: {item.mime_type}")
            if item.transcript_text:
                details.append(f"  Transcript: {item.transcript_text}")
            elif item.transcript_warning:
                details.append(f"  Transcript: {item.transcript_warning}")

        if any(item.transcript_text for item in saved):
            _voice_service, voice_config = self._voice_service_for_workspace(
                workspace_root
            )
            provider_name = (voice_config.provider if voice_config else "").strip()
            if provider_name == "openai_whisper":
                details.append("")
                details.append(
                    wrap_injected_context(DISCORD_WHISPER_TRANSCRIPT_DISCLAIMER)
                )

        details.append("")
        details.append(
            wrap_injected_context(
                "\n".join(
                    [
                        f"Inbox: {inbox}",
                        f"Outbox (pending): {outbox_pending_dir(workspace_root)}",
                        "Use inbox files as local inputs and place reply files in outbox (pending).",
                    ]
                )
            )
        )
        attachment_context = "\n".join(details)

        if prompt_text.strip():
            separator = "\n" if prompt_text.endswith("\n") else "\n\n"
            return f"{prompt_text}{separator}{attachment_context}", len(saved), failed
        return attachment_context, len(saved), failed

    def _build_attachment_filename(self, attachment: Any, *, index: int) -> str:
        raw_name = getattr(attachment, "file_name", None) or f"attachment-{index}"
        base_name = Path(str(raw_name)).name.strip()
        if not base_name or base_name in {".", ".."}:
            base_name = f"attachment-{index}"
        safe_name = "".join(
            ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in base_name
        ).strip("._")
        if not safe_name:
            safe_name = f"attachment-{index}"

        path = Path(safe_name)
        stem = path.stem or f"attachment-{index}"
        suffix = path.suffix.lower()
        if not suffix:
            mime_type = getattr(attachment, "mime_type", None)
            if isinstance(mime_type, str):
                mime_key = mime_type.lower().split(";", 1)[0].strip()
                suffix = {
                    "image/png": ".png",
                    "image/jpeg": ".jpg",
                    "image/jpg": ".jpg",
                    "image/gif": ".gif",
                    "image/webp": ".webp",
                    "application/pdf": ".pdf",
                    "text/plain": ".txt",
                }.get(mime_key, "")
        return f"{stem[:64]}-{uuid.uuid4().hex[:8]}{suffix}"

    async def _find_paused_flow_run(
        self, workspace_root: Path
    ) -> Optional[FlowRunRecord]:
        try:
            store = self._open_flow_store(workspace_root)
        except Exception:
            return None
        try:
            runs = store.list_flow_runs(flow_type="ticket_flow")
            return next(
                (record for record in runs if record.status == FlowRunStatus.PAUSED),
                None,
            )
        except Exception:
            return None
        finally:
            store.close()

    async def _maybe_inject_github_context(
        self,
        prompt_text: str,
        workspace_root: Path,
        *,
        link_source_text: Optional[str] = None,
        allow_cross_repo: bool = False,
    ) -> tuple[str, bool]:
        return await maybe_inject_github_context(
            prompt_text=prompt_text,
            link_source_text=link_source_text or prompt_text,
            workspace_root=workspace_root,
            logger=self._logger,
            event_prefix="discord.github_context",
            allow_cross_repo=allow_cross_repo,
        )

    def _build_message_session_key(
        self,
        *,
        channel_id: str,
        workspace_root: Path,
        pma_enabled: bool,
        agent: str,
    ) -> str:
        if pma_enabled:
            return PMA_OPENCODE_KEY if agent == "opencode" else PMA_KEY
        digest = hashlib.sha256(str(workspace_root).encode("utf-8")).hexdigest()[:12]
        prefix = FILE_CHAT_OPENCODE_PREFIX if agent == "opencode" else FILE_CHAT_PREFIX
        return f"{prefix}discord.{channel_id}.{digest}"

    def _build_runner_state(
        self,
        *,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
    ) -> RunnerState:
        return RunnerState(
            last_run_id=None,
            status="idle",
            last_exit_code=None,
            last_run_started_at=None,
            last_run_finished_at=None,
            autorunner_agent_override=agent,
            autorunner_model_override=model_override,
            autorunner_effort_override=reasoning_effort,
            autorunner_approval_policy=MESSAGE_TURN_APPROVAL_POLICY,
            autorunner_sandbox_mode=MESSAGE_TURN_SANDBOX_POLICY,
        )

    async def _orchestrator_for_workspace(
        self, workspace_root: Path, *, channel_id: str
    ) -> BackendOrchestrator:
        key = f"{channel_id}:{workspace_root}"
        async with self._backend_lock:
            existing = self._backend_orchestrators.get(key)
            if existing is not None:
                return existing
            if self._backend_orchestrator_factory is not None:
                orchestrator = self._backend_orchestrator_factory(workspace_root)
            else:
                repo_config = load_repo_config(
                    workspace_root,
                    hub_path=self._hub_config_path,
                )
                orchestrator = BackendOrchestrator(
                    repo_root=workspace_root,
                    config=repo_config,
                    logger=self._logger,
                )
            self._backend_orchestrators[key] = orchestrator
            return orchestrator

    def _build_workspace_env(
        self, workspace_root: Path, workspace_id: str, state_dir: Path
    ) -> dict[str, str]:
        repo_config = load_repo_config(workspace_root, hub_path=self._hub_config_path)
        command = (
            repo_config.app_server.command
            if repo_config and repo_config.app_server and repo_config.app_server.command
            else ["codex", "app-server"]
        )
        return build_app_server_env(
            command,
            workspace_root,
            state_dir,
            logger=self._logger,
            event_prefix="discord",
        )

    async def _app_server_supervisor_for_workspace(
        self, workspace_root: Path
    ) -> WorkspaceAppServerSupervisor:
        key = str(workspace_root)
        async with self._app_server_lock:
            existing = self._app_server_supervisors.get(key)
            if existing is not None:
                return existing
            repo_config = load_repo_config(
                workspace_root,
                hub_path=self._hub_config_path,
            )
            command = (
                repo_config.app_server.command
                if repo_config
                and repo_config.app_server
                and repo_config.app_server.command
                else ["codex", "app-server"]
            )
            supervisor = WorkspaceAppServerSupervisor(
                command,
                state_root=self._app_server_state_root,
                env_builder=self._build_workspace_env,
                logger=self._logger,
            )
            self._app_server_supervisors[key] = supervisor
            return supervisor

    async def _client_for_workspace(
        self, workspace_path: Optional[str]
    ) -> Optional[CodexAppServerClient]:
        if not isinstance(workspace_path, str) or not workspace_path.strip():
            return None
        try:
            workspace_root = canonicalize_path(Path(workspace_path))
        except Exception:
            return None
        if not workspace_root.exists() or not workspace_root.is_dir():
            return None
        delay = APP_SERVER_START_BACKOFF_INITIAL_SECONDS
        timeout = 30.0
        started_at = time.monotonic()
        while True:
            try:
                supervisor = await self._app_server_supervisor_for_workspace(
                    workspace_root
                )
                return await supervisor.get_client(workspace_root)
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.app_server.start_failed",
                    workspace_path=str(workspace_root),
                    exc=exc,
                )
                elapsed = time.monotonic() - started_at
                if elapsed >= timeout:
                    raise AppServerUnavailableError(
                        f"App-server unavailable after {timeout:.1f}s"
                    ) from exc
                sleep_time = min(delay, timeout - elapsed)
                await asyncio.sleep(sleep_time)
                delay = min(delay * 2, APP_SERVER_START_BACKOFF_MAX_SECONDS)

    async def _list_threads_paginated(
        self,
        client: CodexAppServerClient,
        *,
        limit: int,
        max_pages: int,
        needed_ids: Optional[set[str]] = None,
    ) -> tuple[list[dict[str, Any]], set[str]]:
        entries: list[dict[str, Any]] = []
        found_ids: set[str] = set()
        seen_ids: set[str] = set()
        cursor: Optional[str] = None
        page_count = max(1, max_pages)
        for _ in range(page_count):
            payload = await client.thread_list(cursor=cursor, limit=limit)
            page_entries = _coerce_thread_list(payload)
            for entry in page_entries:
                if not isinstance(entry, dict):
                    continue
                thread_id = entry.get("id")
                if isinstance(thread_id, str):
                    if thread_id in seen_ids:
                        continue
                    seen_ids.add(thread_id)
                    found_ids.add(thread_id)
                entries.append(entry)
            if needed_ids is not None and needed_ids.issubset(found_ids):
                break
            cursor = _extract_thread_list_cursor(payload)
            if not cursor:
                break
        return entries, found_ids

    async def _run_agent_turn_for_message(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
    ) -> DiscordMessageTurnResult:
        orchestrator = await self._orchestrator_for_workspace(
            workspace_root, channel_id=orchestrator_channel_key
        )
        progress_channel_id = (
            orchestrator_channel_key.split(":", 1)[1]
            if orchestrator_channel_key.startswith("pma:")
            else orchestrator_channel_key
        )
        tracker = TurnProgressTracker(
            started_at=time.monotonic(),
            agent=agent,
            model=model_override or "default",
            label="working",
            max_actions=DISCORD_TURN_PROGRESS_MAX_ACTIONS,
            max_output_chars=DISCORD_TURN_PROGRESS_MAX_OUTPUT_CHARS,
        )
        progress_message_id: Optional[str] = None
        progress_rendered: Optional[str] = None
        progress_last_updated = 0.0
        progress_failure_count = 0
        progress_heartbeat_task: Optional[asyncio.Task[None]] = None
        max_progress_len = max(int(self._config.max_message_length), 32)

        async def _edit_progress(
            *, force: bool = False, remove_components: bool = False
        ) -> None:
            nonlocal progress_rendered
            nonlocal progress_last_updated
            nonlocal progress_failure_count
            if not progress_message_id:
                return
            now = time.monotonic()
            if (
                not force
                and (now - progress_last_updated)
                < DISCORD_TURN_PROGRESS_MIN_EDIT_INTERVAL_SECONDS
            ):
                return
            rendered = render_progress_text(
                tracker, max_length=max_progress_len, now=now
            )
            content = truncate_for_discord(rendered, max_len=max_progress_len)
            if not force and content == progress_rendered:
                return
            payload: dict[str, Any] = {"content": content}
            if not remove_components:
                payload["components"] = [build_cancel_turn_button()]
            try:
                await self._rest.edit_channel_message(
                    channel_id=progress_channel_id,
                    message_id=progress_message_id,
                    payload=payload,
                )
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.turn.progress.edit_failed",
                    channel_id=progress_channel_id,
                    message_id=progress_message_id,
                    failure_count=progress_failure_count + 1,
                    exc=exc,
                )
                progress_failure_count += 1
                progress_last_updated = now
                return
            progress_failure_count = 0
            progress_rendered = content
            progress_last_updated = now

        async def _progress_heartbeat() -> None:
            while True:
                await asyncio.sleep(DISCORD_TURN_PROGRESS_HEARTBEAT_INTERVAL_SECONDS)
                await _edit_progress()

        try:
            initial_rendered = render_progress_text(
                tracker, max_length=max_progress_len, now=time.monotonic()
            )
            initial_content = truncate_for_discord(
                initial_rendered, max_len=max_progress_len
            )
            response = await self._send_channel_message(
                progress_channel_id,
                {
                    "content": initial_content,
                    "components": [build_cancel_turn_button()],
                },
            )
            message_id = response.get("id")
            if isinstance(message_id, str) and message_id:
                progress_message_id = message_id
                progress_rendered = initial_content
                progress_last_updated = time.monotonic()
                progress_heartbeat_task = asyncio.create_task(_progress_heartbeat())
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.turn.progress.placeholder_failed",
                channel_id=progress_channel_id,
                exc=exc,
            )

        state = self._build_runner_state(
            agent=agent,
            model_override=model_override,
            reasoning_effort=reasoning_effort,
        )
        known_session = orchestrator.get_thread_id(session_key)
        final_message = ""
        token_usage: Optional[dict[str, Any]] = None
        error_message = None
        session_from_events = known_session
        try:
            async for run_event in orchestrator.run_turn(
                agent_id=agent,
                state=state,
                prompt=prompt_text,
                model=model_override,
                reasoning=reasoning_effort,
                session_key=session_key,
                session_id=known_session,
                workspace_root=workspace_root,
            ):
                if isinstance(run_event, Started):
                    if isinstance(run_event.session_id, str) and run_event.session_id:
                        session_from_events = run_event.session_id
                elif isinstance(run_event, OutputDelta):
                    if run_event.delta_type == RUN_EVENT_DELTA_TYPE_USER_MESSAGE:
                        continue
                    if isinstance(run_event.content, str) and run_event.content.strip():
                        tracker.note_output(run_event.content)
                        await _edit_progress()
                elif isinstance(run_event, ToolCall):
                    tool_name = (
                        run_event.tool_name.strip() if run_event.tool_name else ""
                    )
                    tracker.note_tool(tool_name or "Tool call")
                    await _edit_progress()
                elif isinstance(run_event, ApprovalRequested):
                    summary = (
                        run_event.description.strip() if run_event.description else ""
                    )
                    tracker.note_approval(summary or "Approval requested")
                    await _edit_progress()
                elif isinstance(run_event, RunNotice):
                    notice = run_event.message.strip() if run_event.message else ""
                    if not notice:
                        notice = run_event.kind.strip() if run_event.kind else "notice"
                    if run_event.kind in {"thinking", "reasoning"}:
                        tracker.note_thinking(notice)
                    else:
                        tracker.add_action("notice", notice, "update")
                    await _edit_progress()
                elif isinstance(run_event, TokenUsage):
                    usage_payload = run_event.usage
                    if isinstance(usage_payload, dict):
                        token_usage = usage_payload
                        tracker.context_usage_percent = _extract_context_usage_percent(
                            usage_payload
                        )
                elif isinstance(run_event, Completed):
                    final_message = run_event.final_message or final_message
                    tracker.set_label("done")
                    await _edit_progress(force=True, remove_components=True)
                elif isinstance(run_event, Failed):
                    error_message = run_event.error_message or "Turn failed"
                    tracker.note_error(error_message)
                    tracker.set_label("failed")
                    await _edit_progress(force=True, remove_components=True)
        except Exception as exc:
            error_message = str(exc) or "Turn failed"
            tracker.note_error(error_message)
            tracker.set_label("failed")
            await _edit_progress(force=True, remove_components=True)
            raise
        finally:
            if progress_heartbeat_task is not None:
                progress_heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await progress_heartbeat_task
        if session_from_events:
            orchestrator.set_thread_id(session_key, session_from_events)
        if error_message:
            raise RuntimeError(error_message)
        elapsed_seconds = max(0.0, time.monotonic() - tracker.started_at)
        return DiscordMessageTurnResult(
            final_message=final_message,
            preview_message_id=progress_message_id,
            token_usage=token_usage,
            elapsed_seconds=elapsed_seconds,
        )

    @staticmethod
    def _extract_command_result(
        result: subprocess.CompletedProcess[str],
    ) -> tuple[str, str, Optional[int]]:
        stdout = result.stdout if isinstance(result.stdout, str) else ""
        stderr = result.stderr if isinstance(result.stderr, str) else ""
        exit_code = int(result.returncode) if isinstance(result.returncode, int) else 0
        return stdout, stderr, exit_code

    @staticmethod
    def _format_shell_body(
        command: str, stdout: str, stderr: str, exit_code: Optional[int]
    ) -> str:
        lines = [f"$ {command}"]
        if stdout:
            lines.append(stdout.rstrip("\n"))
        if stderr:
            if stdout:
                lines.append("")
            lines.append("[stderr]")
            lines.append(stderr.rstrip("\n"))
        if not stdout and not stderr:
            lines.append("(no output)")
        if exit_code is not None and exit_code != 0:
            lines.append(f"(exit {exit_code})")
        return "\n".join(lines)

    @staticmethod
    def _format_shell_message(body: str, *, note: Optional[str]) -> str:
        if note:
            return f"{note}\n```text\n{body}\n```"
        return f"```text\n{body}\n```"

    def _prepare_shell_response(
        self,
        full_body: str,
        *,
        filename: str,
    ) -> tuple[str, Optional[bytes]]:
        max_output_chars = max(1, int(self._config.shell.max_output_chars))
        max_message_length = max(64, int(self._config.max_message_length))

        message = self._format_shell_message(full_body, note=None)
        if len(full_body) <= max_output_chars and len(message) <= max_message_length:
            return message, None

        note = f"Output too long; attached full output as {filename}. Showing head."
        head = full_body[:max_output_chars].rstrip()
        if len(head) < len(full_body):
            head = f"{head}{SHELL_OUTPUT_TRUNCATION_SUFFIX}"
        message = self._format_shell_message(head, note=note)
        if len(message) > max_message_length:
            overhead = len(self._format_shell_message("", note=note))
            allowed = max(
                0,
                max_message_length - overhead - len(SHELL_OUTPUT_TRUNCATION_SUFFIX),
            )
            head = full_body[:allowed].rstrip()
            if len(head) < len(full_body):
                head = f"{head}{SHELL_OUTPUT_TRUNCATION_SUFFIX}"
            message = self._format_shell_message(head, note=note)
            if len(message) > max_message_length:
                message = truncate_for_discord(message, max_len=max_message_length)

        return message, full_body.encode("utf-8", errors="replace")

    async def _handle_bang_shell(
        self,
        *,
        channel_id: str,
        message_id: str,
        text: str,
        workspace_root: Path,
    ) -> None:
        if not self._config.shell.enabled:
            await self._send_channel_message_safe(
                channel_id,
                {
                    "content": (
                        "Shell commands are disabled. Enable `discord_bot.shell.enabled`."
                    )
                },
                record_id=f"shell:{message_id}:disabled",
            )
            return

        command_text = text[1:].strip()
        if not command_text:
            await self._send_channel_message_safe(
                channel_id,
                {
                    "content": "Prefix a command with `!` to run it locally. Example: `!ls`"
                },
                record_id=f"shell:{message_id}:usage",
            )
            return

        timeout_seconds = max(0.1, self._config.shell.timeout_ms / 1000.0)
        timeout_label = int(timeout_seconds + 0.999)
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["bash", "-lc", command_text],
                cwd=workspace_root,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.shell.timeout",
                channel_id=channel_id,
                command=command_text,
                timeout_seconds=timeout_seconds,
            )
            await self._send_channel_message_safe(
                channel_id,
                {
                    "content": (
                        f"Shell command timed out after {timeout_label}s: `{command_text}`.\n"
                        "Interactive commands (top/htop/watch/tail -f) do not exit. "
                        "Try a one-shot flag like `top -l 1` (macOS) or `top -b -n 1` (Linux)."
                    )
                },
                record_id=f"shell:{message_id}:timeout",
            )
            return
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.shell.failed",
                channel_id=channel_id,
                command=command_text,
                workspace_root=str(workspace_root),
                exc=exc,
            )
            await self._send_channel_message_safe(
                channel_id,
                {"content": "Shell command failed; check logs for details."},
                record_id=f"shell:{message_id}:failed",
            )
            return

        stdout, stderr, exit_code = self._extract_command_result(result)
        full_body = self._format_shell_body(command_text, stdout, stderr, exit_code)
        filename = f"shell-output-{uuid.uuid4().hex[:8]}.txt"
        response_text, attachment = self._prepare_shell_response(
            full_body,
            filename=filename,
        )
        await self._send_channel_message_safe(
            channel_id,
            {"content": response_text},
            record_id=f"shell:{message_id}:result",
        )
        if attachment is None:
            return
        try:
            await self._rest.create_channel_message_with_attachment(
                channel_id=channel_id,
                data=attachment,
                filename=filename,
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.shell.attachment_failed",
                channel_id=channel_id,
                command=command_text,
                filename=filename,
                exc=exc,
            )
            await self._send_channel_message_safe(
                channel_id,
                {
                    "content": "Failed to attach full shell output; showing truncated output."
                },
                record_id=f"shell:{message_id}:attachment_failed",
            )

    async def _handle_car_command(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        guild_id: Optional[str],
        user_id: Optional[str],
        command_path: tuple[str, ...],
        options: dict[str, Any],
    ) -> None:
        primary = command_path[1] if len(command_path) > 1 else ""

        if command_path == ("car", "bind"):
            await self._handle_bind(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                guild_id=guild_id,
                options=options,
            )
            return
        if command_path == ("car", "status"):
            await self._handle_status(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
            )
            return
        if command_path == ("car", "new"):
            await self._handle_car_new(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
            )
            return
        if command_path == ("car", "newt"):
            await self._handle_car_newt(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                guild_id=guild_id,
            )
            return
        if command_path == ("car", "debug"):
            await self._handle_debug(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
            )
            return
        if command_path == ("car", "help"):
            await self._handle_help(
                interaction_id,
                interaction_token,
            )
            return
        if command_path == ("car", "ids"):
            await self._handle_ids(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                guild_id=guild_id,
                user_id=user_id,
            )
            return
        if command_path == ("car", "agent"):
            await self._handle_car_agent(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                options=options,
            )
            return
        if command_path == ("car", "model"):
            await self._handle_car_model(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                options=options,
            )
            return
        if command_path == ("car", "update"):
            await self._handle_car_update(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                options=options,
            )
            return
        if command_path == ("car", "repos"):
            await self._handle_repos(
                interaction_id,
                interaction_token,
            )
            return
        if command_path == ("car", "diff"):
            workspace_root = await self._require_bound_workspace(
                interaction_id, interaction_token, channel_id=channel_id
            )
            if workspace_root is None:
                return
            await self._handle_diff(
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
                options=options,
            )
            return
        if command_path == ("car", "skills"):
            workspace_root = await self._require_bound_workspace(
                interaction_id, interaction_token, channel_id=channel_id
            )
            if workspace_root is None:
                return
            await self._handle_skills(
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
            )
            return
        if command_path == ("car", "mcp"):
            workspace_root = await self._require_bound_workspace(
                interaction_id, interaction_token, channel_id=channel_id
            )
            if workspace_root is None:
                return
            await self._handle_mcp(
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
            )
            return
        if command_path == ("car", "init"):
            workspace_root = await self._require_bound_workspace(
                interaction_id, interaction_token, channel_id=channel_id
            )
            if workspace_root is None:
                return
            await self._handle_init(
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
            )
            return
        if command_path == ("car", "review"):
            workspace_root = await self._require_bound_workspace(
                interaction_id, interaction_token, channel_id=channel_id
            )
            if workspace_root is None:
                return
            await self._handle_car_review(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                workspace_root=workspace_root,
                options=options,
            )
            return
        if command_path == ("car", "approvals"):
            await self._handle_car_approvals(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                options=options,
            )
            return
        if command_path == ("car", "mention"):
            workspace_root = await self._require_bound_workspace(
                interaction_id, interaction_token, channel_id=channel_id
            )
            if workspace_root is None:
                return
            await self._handle_car_mention(
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
                options=options,
            )
            return
        if command_path == ("car", "experimental"):
            workspace_root = await self._require_bound_workspace(
                interaction_id, interaction_token, channel_id=channel_id
            )
            if workspace_root is None:
                return
            await self._handle_car_experimental(
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
                options=options,
            )
            return
        if command_path == ("car", "rollout"):
            await self._handle_car_rollout(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
            )
            return
        if command_path == ("car", "feedback"):
            workspace_root = await self._require_bound_workspace(
                interaction_id, interaction_token, channel_id=channel_id
            )
            if workspace_root is None:
                return
            await self._handle_car_feedback(
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
                options=options,
                channel_id=channel_id,
            )
            return

        if command_path[:2] == ("car", "session"):
            if command_path == ("car", "session", "resume"):
                await self._handle_car_resume(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                    options=options,
                )
                return
            if command_path == ("car", "session", "reset"):
                await self._handle_car_reset(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                )
                return
            if command_path == ("car", "session", "compact"):
                await self._handle_car_compact(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                )
                return
            if command_path == ("car", "session", "interrupt"):
                await self._handle_car_interrupt(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                )
                return
            if command_path == ("car", "session", "logout"):
                workspace_root = await self._require_bound_workspace(
                    interaction_id, interaction_token, channel_id=channel_id
                )
                if workspace_root is None:
                    return
                await self._handle_car_logout(
                    interaction_id,
                    interaction_token,
                    workspace_root=workspace_root,
                )
                return
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Unknown car session subcommand: {primary}",
            )
            return

        if command_path[:2] == ("car", "flow"):
            if command_path in {("car", "flow", "status"), ("car", "flow", "runs")}:
                action = command_path[2]
                workspace_root = await self._resolve_workspace_for_flow_read(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                    action=action,
                )
                if workspace_root is None:
                    return
                if action == "status":
                    await self._handle_flow_status(
                        interaction_id,
                        interaction_token,
                        workspace_root=workspace_root,
                        options=options,
                    )
                else:
                    await self._handle_flow_runs(
                        interaction_id,
                        interaction_token,
                        workspace_root=workspace_root,
                        options=options,
                    )
                return
            workspace_root = await self._require_bound_workspace(
                interaction_id, interaction_token, channel_id=channel_id
            )
            if workspace_root is None:
                return
            if command_path == ("car", "flow", "issue"):
                await self._handle_flow_issue(
                    interaction_id,
                    interaction_token,
                    workspace_root=workspace_root,
                    options=options,
                    channel_id=channel_id,
                    guild_id=guild_id,
                )
                return
            if command_path == ("car", "flow", "plan"):
                await self._handle_flow_plan(
                    interaction_id,
                    interaction_token,
                    workspace_root=workspace_root,
                    options=options,
                    channel_id=channel_id,
                    guild_id=guild_id,
                )
                return
            if command_path == ("car", "flow", "resume"):
                await self._handle_flow_resume(
                    interaction_id,
                    interaction_token,
                    workspace_root=workspace_root,
                    options=options,
                    channel_id=channel_id,
                    guild_id=guild_id,
                )
                return
            if command_path == ("car", "flow", "stop"):
                await self._handle_flow_stop(
                    interaction_id,
                    interaction_token,
                    workspace_root=workspace_root,
                    options=options,
                    channel_id=channel_id,
                    guild_id=guild_id,
                )
                return
            if command_path == ("car", "flow", "archive"):
                await self._handle_flow_archive(
                    interaction_id,
                    interaction_token,
                    workspace_root=workspace_root,
                    options=options,
                    channel_id=channel_id,
                    guild_id=guild_id,
                )
                return
            if command_path == ("car", "flow", "recover"):
                await self._handle_flow_recover(
                    interaction_id,
                    interaction_token,
                    workspace_root=workspace_root,
                    options=options,
                )
                return
            if command_path == ("car", "flow", "reply"):
                await self._handle_flow_reply(
                    interaction_id,
                    interaction_token,
                    workspace_root=workspace_root,
                    options=options,
                    channel_id=channel_id,
                    guild_id=guild_id,
                )
                return
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Unknown car flow subcommand: {primary}",
            )
            return

        if command_path[:2] == ("car", "files"):
            workspace_root = await self._require_bound_workspace(
                interaction_id, interaction_token, channel_id=channel_id
            )
            if workspace_root is None:
                return

            if command_path == ("car", "files", "inbox"):
                await self._handle_files_inbox(
                    interaction_id,
                    interaction_token,
                    workspace_root=workspace_root,
                )
                return
            if command_path == ("car", "files", "outbox"):
                await self._handle_files_outbox(
                    interaction_id,
                    interaction_token,
                    workspace_root=workspace_root,
                )
                return
            if command_path == ("car", "files", "clear"):
                await self._handle_files_clear(
                    interaction_id,
                    interaction_token,
                    workspace_root=workspace_root,
                    options=options,
                )
                return
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Unknown car files subcommand: {primary}",
            )
            return

        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"Unknown car subcommand: {primary}",
        )

    async def _handle_pma_command_from_normalized(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        guild_id: Optional[str],
        command_path: tuple[str, ...],
        options: dict[str, Any],
    ) -> None:
        subcommand = command_path[1] if len(command_path) > 1 else "status"
        if subcommand == "on":
            pass
        elif subcommand == "off":
            pass
        elif subcommand == "status":
            pass
        elif subcommand == "targets":
            pass
        elif subcommand == "target":
            pass
        else:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Unknown PMA subcommand. Use on, off, or status.",
            )
            return
        await self._handle_pma_command(
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            guild_id=guild_id,
            command_path=command_path,
            options=options,
        )

    async def _sync_application_commands_on_startup(self) -> None:
        registration = self._config.command_registration
        if not registration.enabled:
            log_event(
                self._logger,
                logging.INFO,
                "discord.commands.sync.disabled",
            )
            return

        application_id = (self._config.application_id or "").strip()
        if not application_id:
            raise ValueError("missing Discord application id for command sync")
        if registration.scope == "guild" and not registration.guild_ids:
            raise ValueError("guild scope requires at least one guild_id")

        commands = build_application_commands()
        try:
            await sync_commands(
                self._rest,
                application_id=application_id,
                commands=commands,
                scope=registration.scope,
                guild_ids=registration.guild_ids,
                logger=self._logger,
            )
        except ValueError:
            raise
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.commands.sync.startup_failed",
                scope=registration.scope,
                command_count=len(commands),
                exc=exc,
            )

    async def _shutdown(self) -> None:
        if self._background_tasks:
            for task in list(self._background_tasks):
                task.cancel()
            await asyncio.gather(*list(self._background_tasks), return_exceptions=True)
            self._background_tasks.clear()
        if self._owns_gateway:
            with contextlib.suppress(Exception):
                await self._gateway.stop()
        if self._owns_rest and hasattr(self._rest, "close"):
            with contextlib.suppress(Exception):
                await self._rest.close()
        if self._owns_store:
            with contextlib.suppress(Exception):
                await self._store.close()
        async with self._backend_lock:
            orchestrators = list(self._backend_orchestrators.values())
            self._backend_orchestrators.clear()
        for orchestrator in orchestrators:
            with contextlib.suppress(Exception):
                await orchestrator.close_all()
        async with self._app_server_lock:
            supervisors = list(self._app_server_supervisors.values())
            self._app_server_supervisors.clear()
        for supervisor in supervisors:
            with contextlib.suppress(Exception):
                await supervisor.close_all()

    async def _watch_ticket_flow_pauses(self) -> None:
        while True:
            try:
                await self._scan_and_enqueue_pause_notifications()
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.pause_watch.scan_failed",
                    exc=exc,
                )
            await asyncio.sleep(PAUSE_SCAN_INTERVAL_SECONDS)

    async def _scan_and_enqueue_pause_notifications(self) -> None:
        bindings = await self._store.list_bindings()
        for binding in bindings:
            channel_id = binding.get("channel_id")
            workspace_raw = binding.get("workspace_path")
            if not isinstance(channel_id, str) or not isinstance(workspace_raw, str):
                continue
            workspace_root = canonicalize_path(Path(workspace_raw))
            run_mirror = self._flow_run_mirror(workspace_root)
            snapshot = await asyncio.to_thread(
                load_latest_paused_ticket_flow_dispatch, workspace_root
            )
            if snapshot is None:
                continue

            if (
                binding.get("last_pause_run_id") == snapshot.run_id
                and binding.get("last_pause_dispatch_seq") == snapshot.dispatch_seq
            ):
                continue

            chunks = chunk_discord_message(
                snapshot.dispatch_markdown,
                max_len=self._config.max_message_length,
                with_numbering=False,
            )
            if not chunks:
                chunks = ["(pause notification had no content)"]

            enqueued = True
            for index, chunk in enumerate(chunks, start=1):
                record_id = f"pause:{channel_id}:{snapshot.run_id}:{snapshot.dispatch_seq}:{index}"
                try:
                    await self._store.enqueue_outbox(
                        OutboxRecord(
                            record_id=record_id,
                            channel_id=channel_id,
                            message_id=None,
                            operation="send",
                            payload_json={"content": chunk},
                        )
                    )
                    run_mirror.mirror_outbound(
                        run_id=snapshot.run_id,
                        platform="discord",
                        event_type="flow_pause_dispatch_notice",
                        kind="dispatch",
                        actor="car",
                        text=chunk,
                        chat_id=channel_id,
                        thread_id=binding.get("guild_id"),
                        message_id=record_id,
                        meta={
                            "dispatch_seq": snapshot.dispatch_seq,
                            "chunk_index": index,
                        },
                    )
                except Exception as exc:
                    enqueued = False
                    log_event(
                        self._logger,
                        logging.WARNING,
                        "discord.pause_watch.enqueue_failed",
                        exc=exc,
                        channel_id=channel_id,
                        run_id=snapshot.run_id,
                        dispatch_seq=snapshot.dispatch_seq,
                    )
                    break

            if not enqueued:
                continue

            await self._store.mark_pause_dispatch_seen(
                channel_id=channel_id,
                run_id=snapshot.run_id,
                dispatch_seq=snapshot.dispatch_seq,
            )
            log_event(
                self._logger,
                logging.INFO,
                "discord.pause_watch.notified",
                channel_id=channel_id,
                run_id=snapshot.run_id,
                dispatch_seq=snapshot.dispatch_seq,
                chunk_count=len(chunks),
            )

    async def _send_channel_message(
        self, channel_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._rest.create_channel_message(
            channel_id=channel_id, payload=payload
        )

    async def _delete_channel_message(self, channel_id: str, message_id: str) -> None:
        await self._rest.delete_channel_message(
            channel_id=channel_id,
            message_id=message_id,
        )

    async def _send_channel_message_safe(
        self,
        channel_id: str,
        payload: dict[str, Any],
        *,
        record_id: Optional[str] = None,
    ) -> None:
        try:
            await self._send_channel_message(channel_id, payload)
            return
        except Exception as exc:
            outbox_record_id = (
                record_id or f"retry:{channel_id}:{uuid.uuid4().hex[:12]}"
            )
            log_event(
                self._logger,
                logging.WARNING,
                "discord.channel_message.send_failed",
                channel_id=channel_id,
                record_id=outbox_record_id,
                exc=exc,
            )
            try:
                await self._store.enqueue_outbox(
                    OutboxRecord(
                        record_id=outbox_record_id,
                        channel_id=channel_id,
                        message_id=None,
                        operation="send",
                        payload_json=dict(payload),
                    )
                )
            except Exception as enqueue_exc:
                log_event(
                    self._logger,
                    logging.ERROR,
                    "discord.channel_message.enqueue_failed",
                    channel_id=channel_id,
                    record_id=outbox_record_id,
                    exc=enqueue_exc,
                )

    async def _delete_channel_message_safe(
        self,
        channel_id: str,
        message_id: str,
        *,
        record_id: Optional[str] = None,
    ) -> None:
        if not isinstance(message_id, str) or not message_id:
            return
        try:
            await self._delete_channel_message(channel_id, message_id)
            return
        except Exception as exc:
            outbox_record_id = (
                record_id or f"retry:delete:{channel_id}:{uuid.uuid4().hex[:12]}"
            )
            log_event(
                self._logger,
                logging.WARNING,
                "discord.channel_message.delete_failed",
                channel_id=channel_id,
                message_id=message_id,
                record_id=outbox_record_id,
                exc=exc,
            )
            try:
                await self._store.enqueue_outbox(
                    OutboxRecord(
                        record_id=outbox_record_id,
                        channel_id=channel_id,
                        message_id=message_id,
                        operation="delete",
                        payload_json={},
                    )
                )
            except Exception as enqueue_exc:
                log_event(
                    self._logger,
                    logging.ERROR,
                    "discord.channel_message.delete_enqueue_failed",
                    channel_id=channel_id,
                    message_id=message_id,
                    record_id=outbox_record_id,
                    exc=enqueue_exc,
                )

    def _spawn_task(self, coro: Awaitable[None]) -> asyncio.Task[Any]:
        task: asyncio.Task[Any] = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._on_background_task_done)
        return task

    def _on_background_task_done(self, task: asyncio.Task[Any]) -> None:
        self._background_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.background_task.failed",
                exc=exc,
            )

    async def _on_dispatch(self, event_type: str, payload: dict[str, Any]) -> None:
        if event_type == "INTERACTION_CREATE":
            event = self._chat_adapter.parse_interaction_event(payload)
            if event is not None:
                await self._dispatcher.dispatch(event, self._handle_chat_event)
        elif event_type == "MESSAGE_CREATE":
            self._record_channel_directory_seen_from_message_payload(payload)
            event = self._chat_adapter.parse_message_event(payload)
            if event is not None:
                await self._dispatch_chat_event(event)

    def _record_channel_directory_seen_from_message_payload(
        self, payload: dict[str, Any]
    ) -> None:
        channel_id = self._coerce_id(payload.get("channel_id"))
        if channel_id is None:
            return
        guild_id = self._coerce_id(payload.get("guild_id"))

        guild_label = self._first_non_empty_text(
            payload.get("guild_name"),
            self._nested_text(payload, "guild", "name"),
        )
        channel_label_raw = self._first_non_empty_text(
            payload.get("channel_name"),
            self._nested_text(payload, "channel", "name"),
        )
        channel_label = (
            f"#{channel_label_raw.lstrip('#')}"
            if channel_label_raw is not None
            else f"#{channel_id}"
        )

        if guild_id is not None:
            display = f"{guild_label or f'guild:{guild_id}'} / {channel_label}"
        else:
            display = channel_label if channel_label_raw is not None else channel_id

        meta: dict[str, Any] = {}
        if guild_id is not None:
            meta["guild_id"] = guild_id

        try:
            self._channel_directory_store.record_seen(
                "discord",
                channel_id,
                None,
                display,
                meta,
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.channel_directory.record_failed",
                channel_id=channel_id,
                guild_id=guild_id,
                exc=exc,
            )

    @staticmethod
    def _nested_text(payload: dict[str, Any], key: str, field: str) -> Optional[str]:
        candidate = payload.get(key)
        if not isinstance(candidate, dict):
            return None
        return DiscordBotService._first_non_empty_text(candidate.get(field))

    @staticmethod
    def _first_non_empty_text(*values: Any) -> Optional[str]:
        for value in values:
            if isinstance(value, str):
                normalized = value.strip()
                if normalized:
                    return normalized
        return None

    @staticmethod
    def _coerce_id(value: Any) -> Optional[str]:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return str(value)
        if isinstance(value, str):
            normalized = value.strip()
            if normalized:
                return normalized
        return None

    async def _handle_interaction(self, interaction_payload: dict[str, Any]) -> None:
        if is_component_interaction(interaction_payload):
            await self._handle_component_interaction(interaction_payload)
            return

        interaction_id = extract_interaction_id(interaction_payload)
        interaction_token = extract_interaction_token(interaction_payload)
        channel_id = extract_channel_id(interaction_payload)
        guild_id = extract_guild_id(interaction_payload)

        if not interaction_id or not interaction_token or not channel_id:
            self._logger.warning(
                "handle_interaction: missing required fields (interaction_id=%s, token=%s, channel=%s)",
                bool(interaction_id),
                bool(interaction_token),
                bool(channel_id),
            )
            return

        if not allowlist_allows(interaction_payload, self._allowlist):
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "This Discord command is not authorized for this channel/user/guild.",
            )
            return

        command_path, options = extract_command_path_and_options(interaction_payload)
        ingress = canonicalize_command_ingress(
            command_path=command_path,
            options=options,
        )
        if ingress is None:
            self._logger.warning(
                "handle_interaction: failed to canonicalize command ingress (command_path=%s, options=%s)",
                command_path,
                options,
            )
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "I could not parse this interaction. Please retry the command.",
            )
            return

        try:
            if ingress.command_path[:1] == ("car",):
                await self._handle_car_command(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                    guild_id=guild_id,
                    user_id=extract_user_id(interaction_payload),
                    command_path=ingress.command_path,
                    options=ingress.options,
                )
                return

            if ingress.command_path[:1] == ("pma",):
                await self._handle_pma_command(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                    guild_id=guild_id,
                    command_path=ingress.command_path,
                    options=ingress.options,
                )
                return

            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Command not implemented yet for Discord.",
            )
        except DiscordTransientError as exc:
            user_msg = exc.user_message or "An error occurred. Please try again later."
            await self._respond_ephemeral(interaction_id, interaction_token, user_msg)
        except Exception as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "discord.interaction.unhandled_error",
                command_path=ingress.command,
                channel_id=channel_id,
                exc=exc,
            )
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "An unexpected error occurred. Please try again later.",
            )

    async def _handle_bind(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        guild_id: Optional[str],
        options: dict[str, Any],
    ) -> None:
        raw_path = options.get("workspace")
        if isinstance(raw_path, str) and raw_path.strip():
            await self._bind_with_path(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                guild_id=guild_id,
                raw_path=raw_path.strip(),
            )
            return

        repos = self._list_manifest_repos()
        if not repos:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "No repos found in manifest. Use /car bind workspace:<workspace> to bind manually.",
            )
            return

        components = [build_bind_picker(repos)]
        await self._respond_with_components(
            interaction_id,
            interaction_token,
            "Select a workspace to bind:",
            components,
        )

    def _list_manifest_repos(self) -> list[tuple[str, str]]:
        if not self._manifest_path or not self._manifest_path.exists():
            return []
        try:
            manifest = load_manifest(self._manifest_path, self._config.root)
            return [
                (repo.id, str(self._config.root / repo.path))
                for repo in manifest.repos
                if repo.id
            ]
        except Exception:
            return []

    async def _bind_with_path(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        guild_id: Optional[str],
        raw_path: str,
    ) -> None:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = self._config.root / candidate
        workspace = canonicalize_path(candidate)
        if not workspace.exists() or not workspace.is_dir():
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Workspace path does not exist: {workspace}",
            )
            return

        await self._store.upsert_binding(
            channel_id=channel_id,
            guild_id=guild_id,
            workspace_path=str(workspace),
            repo_id=None,
        )
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"Bound this channel to workspace: {workspace}",
        )

    async def _handle_status(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
    ) -> None:
        binding = await self._store.get_binding(channel_id=channel_id)
        if binding is None:
            text = (
                "This channel is not bound. Use /car bind workspace:<workspace>. "
                "Then use /car flow status once flow commands are enabled."
            )
            await self._respond_ephemeral(interaction_id, interaction_token, text)
            return

        lines = []
        is_pma = binding.get("pma_enabled", False)
        workspace_path = binding.get("workspace_path", "unknown")
        repo_id = binding.get("repo_id")
        guild_id = binding.get("guild_id")
        updated_at = binding.get("updated_at", "unknown")

        if is_pma:
            lines.append("Mode: PMA (hub)")
            prev_workspace = binding.get("pma_prev_workspace_path")
            if prev_workspace:
                lines.append(f"Previous binding: {prev_workspace}")
                lines.append("Use /pma off to restore previous binding.")
        else:
            lines.append("Mode: workspace")
            lines.append("Channel is bound.")

        lines.extend(
            [
                f"Workspace: {workspace_path}",
                f"Repo ID: {repo_id or 'none'}",
                f"Guild ID: {guild_id or 'none'}",
                f"Channel ID: {channel_id}",
                f"Last updated: {updated_at}",
            ]
        )

        active_flow_info = await self._get_active_flow_info(workspace_path)
        if active_flow_info:
            lines.append(f"Active flow: {active_flow_info}")

        lines.append("Use /car flow status for ticket flow details.")
        await self._respond_ephemeral(
            interaction_id, interaction_token, "\n".join(lines)
        )

    async def _get_active_flow_info(self, workspace_path: str) -> Optional[str]:
        if not workspace_path or workspace_path == "unknown":
            return None
        try:
            workspace_root = canonicalize_path(Path(workspace_path))
            if not workspace_root.exists():
                return None
            store = self._open_flow_store(workspace_root)
            try:
                runs = store.list_flow_runs(flow_type="ticket_flow")
                for record in runs:
                    if record.status == FlowRunStatus.RUNNING:
                        return f"{record.id} (running)"
                    if record.status == FlowRunStatus.PAUSED:
                        return f"{record.id} (paused)"
            finally:
                store.close()
        except Exception:
            pass
        return None

    async def _handle_debug(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
    ) -> None:
        binding = await self._store.get_binding(channel_id=channel_id)
        lines = [
            f"Channel ID: {channel_id}",
        ]
        if binding is None:
            lines.append("Binding: none (unbound)")
            lines.append("Use /car bind path:<workspace> to bind this channel.")
            await self._respond_ephemeral(
                interaction_id, interaction_token, "\n".join(lines)
            )
            return

        workspace_path = binding.get("workspace_path", "unknown")
        lines.extend(
            [
                f"Guild ID: {binding.get('guild_id') or 'none'}",
                f"Workspace: {workspace_path}",
                f"Repo ID: {binding.get('repo_id') or 'none'}",
                f"PMA enabled: {binding.get('pma_enabled', False)}",
                f"PMA prev workspace: {binding.get('pma_prev_workspace_path') or 'none'}",
                f"Updated at: {binding.get('updated_at', 'unknown')}",
            ]
        )

        if workspace_path and workspace_path != "unknown":
            try:
                workspace_root = canonicalize_path(Path(workspace_path))
                lines.append(f"Canonical path: {workspace_root}")
                lines.append(f"Path exists: {workspace_root.exists()}")
                if workspace_root.exists():
                    car_dir = workspace_root / ".codex-autorunner"
                    lines.append(f".codex-autorunner exists: {car_dir.exists()}")
                    flows_db = car_dir / "flows.db"
                    lines.append(f"flows.db exists: {flows_db.exists()}")
            except Exception as exc:
                lines.append(f"Path resolution error: {exc}")

        outbox_items = await self._store.list_outbox()
        pending_outbox = [r for r in outbox_items if r.channel_id == channel_id]
        lines.append(f"Pending outbox items: {len(pending_outbox)}")

        await self._respond_ephemeral(
            interaction_id, interaction_token, "\n".join(lines)
        )

    async def _handle_help(
        self,
        interaction_id: str,
        interaction_token: str,
    ) -> None:
        lines = [
            "**CAR Commands:**",
            "",
            "/car bind [path] - Bind channel to workspace",
            "/car status - Show binding status",
            "/car new - Start a fresh chat session",
            "/car newt - Reset current workspace branch from origin default branch and start session",
            "/car debug - Show debug info",
            "/car help - Show this help",
            "/car ids - Show channel/user IDs for debugging",
            "/car diff [path] - Show git diff",
            "/car skills - List available skills",
            "/car mcp - Show MCP server status",
            "/car init - Generate AGENTS.md",
            "/car repos - List available repos",
            "/car agent [name] - Set or show agent",
            "/car model [name] - Set or show model",
            "/car update [target] - Start update or check status",
            "/car review [target] - Run a code review",
            "/car approvals [mode] - Set approval and sandbox policy",
            "/car mention <path> [request] - Include a file in a request",
            "/car experimental [action] [feature] - Toggle experimental features",
            "/car rollout - Show current thread rollout path",
            "/car feedback <reason> - Send feedback and logs",
            "",
            "**Session Commands:**",
            "/car session resume [thread_id] - Resume a previous chat thread",
            "/car session reset - Reset PMA thread state",
            "/car session compact - Compact the conversation",
            "/car session interrupt - Stop the active turn",
            "/car session logout - Log out of the Codex account",
            "",
            "**Flow Commands:**",
            "/car flow status [run_id] - Show flow status",
            "/car flow runs [limit] - List flow runs",
            "/car flow issue <issue#|url> - Seed ISSUE.md from GitHub",
            "/car flow plan <text> - Seed ISSUE.md from plan text",
            "/car flow resume [run_id] - Resume a paused flow",
            "/car flow stop [run_id] - Stop a flow",
            "/car flow archive [run_id] - Archive a flow",
            "/car flow recover [run_id] - Reconcile active flow run state",
            "/car flow reply <text> [run_id] - Reply to paused flow",
            "",
            "**File Commands:**",
            "/car files inbox - List inbox files",
            "/car files outbox - List pending outbox files",
            "/car files clear [target] - Clear inbox/outbox",
            "",
            "**PMA Commands:**",
            "/pma on - Enable PMA mode",
            "/pma off - Disable PMA mode",
            "/pma status - Show PMA status",
            "/pma targets - List PMA delivery targets",
            "/pma target add <ref> - Add a PMA delivery target",
            "/pma target rm <ref> - Remove a PMA delivery target",
            "/pma target clear - Clear PMA delivery targets",
            "/pma target active [ref|key] - Show/set active PMA delivery target",
            "",
            "Direct shell:",
            "!<cmd> - run a bash command in the bound workspace",
        ]
        content = format_discord_message("\n".join(lines))
        await self._respond_ephemeral(interaction_id, interaction_token, content)

    async def _handle_ids(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        guild_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> None:
        lines = [
            f"Channel ID: {channel_id}",
            f"Guild ID: {guild_id or 'none'}",
            f"User ID: {user_id or 'unknown'}",
            "",
            "Allowlist example:",
            f"discord_bot.allowed_channel_ids: [{channel_id}]",
        ]
        if guild_id:
            lines.append(f"discord_bot.allowed_guild_ids: [{guild_id}]")
        if user_id:
            lines.append(f"discord_bot.allowed_user_ids: [{user_id}]")
        await self._respond_ephemeral(
            interaction_id, interaction_token, "\n".join(lines)
        )

    async def _handle_diff(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        options: dict[str, Any],
    ) -> None:
        import subprocess

        path_arg = options.get("path")
        cwd = workspace_root
        if isinstance(path_arg, str) and path_arg.strip():
            candidate = Path(path_arg.strip())
            if not candidate.is_absolute():
                candidate = workspace_root / candidate
            try:
                cwd = canonicalize_path(candidate)
            except Exception:
                cwd = workspace_root

        git_check = ["git", "rev-parse", "--is-inside-work-tree"]
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                git_check,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                await self._respond_ephemeral(
                    interaction_id,
                    interaction_token,
                    "Not a git repository.",
                )
                return
        except subprocess.TimeoutExpired:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Git check timed out.",
            )
            return
        except Exception as exc:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Git check failed: {exc}",
            )
            return

        diff_cmd = [
            "bash",
            "-lc",
            "git diff --color; git ls-files --others --exclude-standard | "
            'while read -r f; do git diff --color --no-index -- /dev/null "$f"; done',
        ]
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                diff_cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            output = result.stdout
            if not output.strip():
                output = "(No diff output.)"
        except subprocess.TimeoutExpired:
            output = "Git diff timed out after 30 seconds."
        except Exception as exc:
            output = f"Failed to run git diff: {exc}"

        from .rendering import truncate_for_discord

        output = truncate_for_discord(output, self._config.max_message_length - 100)
        await self._respond_ephemeral(interaction_id, interaction_token, output)

    async def _handle_skills(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
    ) -> None:
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            "Skills listing requires the app server client. "
            "This command is not yet available in Discord.",
        )

    async def _handle_mcp(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
    ) -> None:
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            "MCP server status requires the app server client. "
            "This command is not yet available in Discord.",
        )

    async def _handle_init(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
    ) -> None:
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            "AGENTS.md generation requires the app server client. "
            "This command is not yet available in Discord.",
        )

    async def _handle_repos(
        self,
        interaction_id: str,
        interaction_token: str,
    ) -> None:
        if not self._manifest_path or not self._manifest_path.exists():
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Hub manifest not configured.",
            )
            return

        try:
            manifest = load_manifest(self._manifest_path, self._config.root)
        except Exception as exc:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Failed to load manifest: {exc}",
            )
            return

        lines = ["Repositories:"]
        for repo in manifest.repos:
            if not repo.enabled:
                continue
            lines.append(f"- `{repo.id}` ({repo.path})")

        if len(lines) == 1:
            lines.append("No enabled repositories found.")

        lines.append("\nUse /car bind to select a workspace.")

        content = format_discord_message("\n".join(lines))
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            content,
        )

    async def _handle_car_new(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
    ) -> None:
        deferred = await self._defer_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )
        binding = await self._store.get_binding(channel_id=channel_id)
        if binding is None:
            text = format_discord_message(
                "This channel is not bound. Run `/car bind path:<...>` first."
            )
            await self._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=text,
            )
            return

        pma_enabled = bool(binding.get("pma_enabled", False))
        workspace_raw = binding.get("workspace_path")
        workspace_root: Optional[Path] = None
        if isinstance(workspace_raw, str) and workspace_raw.strip():
            workspace_root = canonicalize_path(Path(workspace_raw))
            if not workspace_root.exists() or not workspace_root.is_dir():
                workspace_root = None
        if workspace_root is None:
            if pma_enabled:
                workspace_root = canonicalize_path(Path(self._config.root))
            else:
                text = format_discord_message(
                    "Binding is invalid. Run `/car bind path:<workspace>`."
                )
                await self._send_or_respond_ephemeral(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    deferred=deferred,
                    text=text,
                )
                return

        agent = (binding.get("agent") or self.DEFAULT_AGENT).strip().lower()
        if agent not in self.VALID_AGENT_VALUES:
            agent = self.DEFAULT_AGENT

        session_key = self._build_message_session_key(
            channel_id=channel_id,
            workspace_root=workspace_root,
            pma_enabled=pma_enabled,
            agent=agent,
        )
        orchestrator_channel_key = (
            channel_id if not pma_enabled else f"pma:{channel_id}"
        )
        orchestrator = await self._orchestrator_for_workspace(
            workspace_root, channel_id=orchestrator_channel_key
        )
        had_previous = orchestrator.reset_thread_id(session_key)
        mode_label = "PMA" if pma_enabled else "repo"
        state_label = "cleared previous thread" if had_previous else "new thread ready"

        text = format_discord_message(
            f"Started a fresh {mode_label} session for `{agent}` ({state_label})."
        )
        await self._send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=text,
        )

    async def _handle_car_newt(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        guild_id: Optional[str],
    ) -> None:
        deferred = await self._defer_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )
        binding = await self._store.get_binding(channel_id=channel_id)
        if binding is None:
            text = format_discord_message(
                "This channel is not bound. Run `/car bind path:<...>` first."
            )
            await self._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=text,
            )
            return

        pma_enabled = bool(binding.get("pma_enabled", False))
        if pma_enabled:
            text = format_discord_message(
                "/car newt is not available in PMA mode. Use `/car new` instead."
            )
            await self._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=text,
            )
            return

        workspace_raw = binding.get("workspace_path")
        workspace_root: Optional[Path] = None
        if isinstance(workspace_raw, str) and workspace_raw.strip():
            workspace_root = canonicalize_path(Path(workspace_raw))
            if not workspace_root.exists() or not workspace_root.is_dir():
                workspace_root = None
        if workspace_root is None:
            text = format_discord_message(
                "Binding is invalid. Run `/car bind path:<workspace>`."
            )
            await self._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=text,
            )
            return

        safe_channel_id = re.sub(r"[^a-zA-Z0-9]+", "-", channel_id).strip("-")
        if not safe_channel_id:
            safe_channel_id = "channel"
        branch_name = f"thread-{safe_channel_id}"

        try:
            default_branch = await asyncio.to_thread(
                reset_branch_from_origin_main,
                workspace_root,
                branch_name,
            )
        except GitError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.newt.branch_reset.failed",
                channel_id=channel_id,
                branch=branch_name,
                exc=exc,
            )
            text = format_discord_message(
                f"Failed to reset branch `{branch_name}` from origin default branch: {exc}"
            )
            await self._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=text,
            )
            return

        setup_command_count = 0
        hub_supervisor = getattr(self, "_hub_supervisor", None)
        if hub_supervisor is not None:
            repo_id_raw = binding.get("repo_id")
            repo_id_hint = (
                repo_id_raw.strip()
                if isinstance(repo_id_raw, str) and repo_id_raw
                else None
            )
            try:
                setup_command_count = await asyncio.to_thread(
                    hub_supervisor.run_setup_commands_for_workspace,
                    workspace_root,
                    repo_id_hint=repo_id_hint,
                )
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.newt.setup.failed",
                    channel_id=channel_id,
                    workspace_path=str(workspace_root),
                    exc=exc,
                )
                text = format_discord_message(
                    f"Reset branch `{branch_name}` to `origin/{default_branch}` but setup commands failed: {exc}"
                )
                await self._send_or_respond_ephemeral(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    deferred=deferred,
                    text=text,
                )
                return

        agent = (binding.get("agent") or self.DEFAULT_AGENT).strip().lower()
        if agent not in self.VALID_AGENT_VALUES:
            agent = self.DEFAULT_AGENT

        session_key = self._build_message_session_key(
            channel_id=channel_id,
            workspace_root=workspace_root,
            pma_enabled=pma_enabled,
            agent=agent,
        )
        orchestrator_channel_key = (
            channel_id if not pma_enabled else f"pma:{channel_id}"
        )
        orchestrator = await self._orchestrator_for_workspace(
            workspace_root, channel_id=orchestrator_channel_key
        )
        had_previous = orchestrator.reset_thread_id(session_key)
        mode_label = "PMA" if pma_enabled else "repo"
        state_label = "cleared previous thread" if had_previous else "new thread ready"
        setup_note = (
            f" Ran {setup_command_count} setup command(s)."
            if setup_command_count
            else ""
        )

        text = format_discord_message(
            f"Reset branch `{branch_name}` to `origin/{default_branch}` in current workspace and started fresh {mode_label} session for `{agent}` ({state_label}).{setup_note}"
        )
        await self._send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=text,
        )

    async def _handle_car_resume(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        options: dict[str, Any],
    ) -> None:
        deferred = await self._defer_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )
        binding = await self._store.get_binding(channel_id=channel_id)
        if binding is None:
            text = format_discord_message(
                "This channel is not bound. Run `/car bind path:<...>` first."
            )
            await self._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=text,
            )
            return

        pma_enabled = bool(binding.get("pma_enabled", False))
        workspace_raw = binding.get("workspace_path")
        workspace_root: Optional[Path] = None
        if isinstance(workspace_raw, str) and workspace_raw.strip():
            workspace_root = canonicalize_path(Path(workspace_raw))
            if not workspace_root.exists() or not workspace_root.is_dir():
                workspace_root = None
        if workspace_root is None:
            if pma_enabled:
                workspace_root = canonicalize_path(Path(self._config.root))
            else:
                text = format_discord_message(
                    "Binding is invalid. Run `/car bind path:<workspace>`."
                )
                await self._send_or_respond_ephemeral(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    deferred=deferred,
                    text=text,
                )
                return

        agent = (binding.get("agent") or self.DEFAULT_AGENT).strip().lower()
        if agent not in self.VALID_AGENT_VALUES:
            agent = self.DEFAULT_AGENT

        session_key = self._build_message_session_key(
            channel_id=channel_id,
            workspace_root=workspace_root,
            pma_enabled=pma_enabled,
            agent=agent,
        )
        orchestrator_channel_key = (
            channel_id if not pma_enabled else f"pma:{channel_id}"
        )
        orchestrator = await self._orchestrator_for_workspace(
            workspace_root, channel_id=orchestrator_channel_key
        )

        raw_thread_id = options.get("thread_id")
        thread_id = raw_thread_id.strip() if isinstance(raw_thread_id, str) else None

        if thread_id:
            orchestrator.set_thread_id(session_key, thread_id)
            mode_label = "PMA" if pma_enabled else "repo"
            text = format_discord_message(
                f"Resumed {mode_label} session for `{agent}` with thread `{thread_id}`."
            )
        else:
            current_thread_id = orchestrator.get_thread_id(session_key)
            if current_thread_id:
                text = format_discord_message(
                    f"Current thread: `{current_thread_id}`\n\n"
                    "Use `/car resume thread_id:<thread_id>` to resume a specific thread."
                )
            else:
                text = format_discord_message(
                    "No thread is currently active.\n\n"
                    "Use `/car resume thread_id:<thread_id>` to resume a specific thread, "
                    "or start a new conversation to begin."
                )

        await self._send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=text,
        )

    async def _handle_car_update(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        options: dict[str, Any],
    ) -> None:
        raw_target = options.get("target")
        if isinstance(raw_target, str) and raw_target.strip().lower() == "status":
            await self._handle_car_update_status(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
            )
            return

        try:
            update_target = _normalize_update_target(
                raw_target if isinstance(raw_target, str) else None
            )
        except ValueError as exc:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"{exc} Use target:status to check progress.",
            )
            return

        repo_url = (self._update_repo_url or DEFAULT_UPDATE_REPO_URL).strip()
        if not repo_url:
            repo_url = DEFAULT_UPDATE_REPO_URL
        repo_ref = _normalize_update_ref(
            self._update_repo_ref or DEFAULT_UPDATE_REPO_REF
        )
        update_dir = resolve_update_paths().cache_dir
        notify_metadata = self._update_status_notifier.build_spawn_metadata(
            chat_id=channel_id
        )

        linux_hub_service_name: Optional[str] = None
        linux_telegram_service_name: Optional[str] = None
        linux_discord_service_name: Optional[str] = None
        update_services = self._update_linux_service_names
        if isinstance(update_services, dict):
            hub_service = update_services.get("hub")
            telegram_service = update_services.get("telegram")
            discord_service = update_services.get("discord")
            if isinstance(hub_service, str) and hub_service.strip():
                linux_hub_service_name = hub_service.strip()
            if isinstance(telegram_service, str) and telegram_service.strip():
                linux_telegram_service_name = telegram_service.strip()
            if isinstance(discord_service, str) and discord_service.strip():
                linux_discord_service_name = discord_service.strip()

        try:
            await asyncio.to_thread(
                _spawn_update_process,
                repo_url=repo_url,
                repo_ref=repo_ref,
                update_dir=update_dir,
                logger=self._logger,
                update_target=update_target,
                skip_checks=bool(self._update_skip_checks),
                update_backend=self._update_backend,
                linux_hub_service_name=linux_hub_service_name,
                linux_telegram_service_name=linux_telegram_service_name,
                linux_discord_service_name=linux_discord_service_name,
                **notify_metadata,
            )
        except UpdateInProgressError as exc:
            text = format_discord_message(
                f"{exc} Use `/car update target:status` for current state."
            )
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                text,
            )
            return
        except Exception as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "discord.update.failed_start",
                update_target=update_target,
                exc=exc,
            )
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Update failed to start. Check logs for details.",
            )
            return

        text = format_discord_message(
            f"Update started ({update_target}). The selected service(s) will restart. "
            "I will post completion status in this channel. "
            "Use `/car update target:status` for progress."
        )
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            text,
        )
        self._update_status_notifier.schedule_watch({"chat_id": channel_id})

    def _update_status_path(self) -> Path:
        return resolve_update_paths().status_path

    def _format_update_status_message(self, status: Optional[dict[str, Any]]) -> str:
        rendered = format_update_status_message(status)
        if not status:
            return rendered
        lines = [rendered]
        target = status.get("update_target")
        if isinstance(target, str) and target.strip():
            lines.append(f"Target: {target.strip()}")
        repo_ref = status.get("repo_ref")
        if isinstance(repo_ref, str) and repo_ref.strip():
            lines.append(f"Ref: {repo_ref.strip()}")
        log_path = status.get("log_path")
        if isinstance(log_path, str) and log_path.strip():
            lines.append(f"Log: {log_path.strip()}")
        return "\n".join(lines)

    async def _send_update_status_notice(
        self, notify_context: dict[str, Any], text: str
    ) -> None:
        channel_raw = notify_context.get("chat_id")
        if isinstance(channel_raw, int) and not isinstance(channel_raw, bool):
            channel_id = str(channel_raw)
        elif isinstance(channel_raw, str) and channel_raw.strip():
            channel_id = channel_raw.strip()
        else:
            return
        await self._send_channel_message_safe(
            channel_id,
            {"content": format_discord_message(text)},
        )

    def _mark_update_notified(self, status: dict[str, Any]) -> None:
        mark_update_status_notified(
            path=self._update_status_path(),
            status=status,
            logger=self._logger,
            log_event_name="discord.update.notify_write_failed",
        )

    async def _handle_car_update_status(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
    ) -> None:
        status = await asyncio.to_thread(_read_update_status)
        if not isinstance(status, dict):
            status = None
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            self._format_update_status_message(status),
        )

    VALID_AGENT_VALUES = ("codex", "opencode")
    DEFAULT_AGENT = "codex"

    async def _handle_car_agent(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        options: dict[str, Any],
    ) -> None:
        binding = await self._store.get_binding(channel_id=channel_id)
        if binding is None:
            text = format_discord_message(
                "This channel is not bound. Run `/car bind path:<...>` first."
            )
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                text,
            )
            return

        current_agent = binding.get("agent") or self.DEFAULT_AGENT
        agent_name = options.get("name")

        if not agent_name:
            lines = [
                f"Current agent: {current_agent}",
                "",
                "Available agents:",
                "  codex - Default Codex agent",
                "  opencode - OpenCode agent (requires opencode binary)",
                "",
                "Use `/car agent <name>` to switch.",
            ]
            text = format_discord_message("\n".join(lines))
            await self._respond_ephemeral(interaction_id, interaction_token, text)
            return

        desired = agent_name.lower().strip()
        if desired not in self.VALID_AGENT_VALUES:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Invalid agent '{agent_name}'. Valid options: {', '.join(self.VALID_AGENT_VALUES)}",
            )
            return

        if desired == current_agent:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Agent already set to {current_agent}.",
            )
            return

        await self._store.update_agent_state(channel_id=channel_id, agent=desired)
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"Agent set to {desired}. Will apply on the next turn.",
        )

    VALID_REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh")

    async def _handle_car_model(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        options: dict[str, Any],
    ) -> None:
        binding = await self._store.get_binding(channel_id=channel_id)
        if binding is None:
            text = format_discord_message(
                "This channel is not bound. Run `/car bind path:<...>` first."
            )
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                text,
            )
            return

        current_agent = binding.get("agent") or self.DEFAULT_AGENT
        current_model = binding.get("model_override")
        current_effort = binding.get("reasoning_effort")
        model_name = options.get("name")
        effort = options.get("effort")

        if not model_name:
            lines = [
                f"Current agent: {current_agent}",
                f"Current model: {current_model or '(default)'}",
            ]
            if current_effort:
                lines.append(f"Reasoning effort: {current_effort}")
            lines.extend(
                [
                    "",
                    "Use `/car model <name>` to set a model.",
                    "Use `/car model <name> effort:<value>` to set model with reasoning effort (codex only).",
                    "",
                    f"Valid efforts: {', '.join(self.VALID_REASONING_EFFORTS)}",
                ]
            )
            text = format_discord_message("\n".join(lines))
            await self._respond_ephemeral(interaction_id, interaction_token, text)
            return

        model_name = model_name.strip()
        if model_name.lower() in ("clear", "reset"):
            await self._store.update_model_state(
                channel_id=channel_id, clear_model=True
            )
            await self._respond_ephemeral(
                interaction_id, interaction_token, "Model override cleared."
            )
            return

        if effort:
            if current_agent != "codex":
                await self._respond_ephemeral(
                    interaction_id,
                    interaction_token,
                    "Reasoning effort is only supported for the codex agent.",
                )
                return
            effort = effort.lower().strip()
            if effort not in self.VALID_REASONING_EFFORTS:
                await self._respond_ephemeral(
                    interaction_id,
                    interaction_token,
                    f"Invalid effort '{effort}'. Valid options: {', '.join(self.VALID_REASONING_EFFORTS)}",
                )
                return

        await self._store.update_model_state(
            channel_id=channel_id,
            model_override=model_name,
            reasoning_effort=effort,
        )

        effort_note = f" (effort={effort})" if effort else ""
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"Model set to {model_name}{effort_note}. Will apply on the next turn.",
        )

    async def _resolve_workspace_for_flow_read(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        action: str,
    ) -> Optional[Path]:
        binding = await self._store.get_binding(channel_id=channel_id)
        pma_enabled = bool(binding and binding.get("pma_enabled", False))
        workspace_raw = binding.get("workspace_path") if binding else None
        has_workspace_binding = isinstance(workspace_raw, str) and bool(
            workspace_raw.strip()
        )

        if should_route_flow_read_to_hub_overview(
            action=action,
            pma_enabled=pma_enabled,
            has_workspace_binding=has_workspace_binding,
        ):
            await self._send_hub_flow_overview(interaction_id, interaction_token)
            return None

        if binding is None:
            text = format_discord_message(
                "This channel is not bound. Run `/car bind path:<...>` first."
            )
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                text,
            )
            return None
        if pma_enabled:
            text = format_discord_message(
                "PMA mode is enabled for this channel. Run `/pma off` to use workspace-scoped `/car` commands."
            )
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                text,
            )
            return None
        if not has_workspace_binding:
            text = format_discord_message(
                "Binding is invalid. Run `/car bind path:<...>` first."
            )
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                text,
            )
            return None
        return canonicalize_path(Path(str(workspace_raw)))

    async def _send_hub_flow_overview(
        self, interaction_id: str, interaction_token: str
    ) -> None:
        if not self._manifest_path or not self._manifest_path.exists():
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Hub manifest not configured.",
            )
            return

        try:
            manifest = load_manifest(self._manifest_path, self._config.root)
        except Exception as exc:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Failed to load manifest: {exc}",
            )
            return

        lines = ["Hub Flow Overview:"]
        seen_any = False
        for repo in manifest.repos:
            if not repo.enabled:
                continue
            seen_any = True
            repo_root = (self._config.root / repo.path).resolve()
            label = repo.display_name or repo.id
            try:
                store = self._open_flow_store(repo_root)
            except Exception:
                lines.append(f"❓ {label}: Error reading state")
                continue
            try:
                runs = store.list_flow_runs(flow_type="ticket_flow")
                latest = runs[0] if runs else None
                progress = ticket_progress(repo_root)
                display = build_ticket_flow_display(
                    status=latest.status.value if latest else None,
                    done_count=progress.get("done", 0),
                    total_count=progress.get("total", 0),
                    run_id=latest.id if latest else None,
                )
                run_id = display.get("run_id")
                run_suffix = f" run {run_id}" if run_id else ""
                lines.append(
                    f"{display['status_icon']} {label}: {display['status_label']} "
                    f"{display['done_count']}/{display['total_count']}{run_suffix}"
                )
            except Exception:
                lines.append(f"❓ {label}: Error reading state")
            finally:
                store.close()

        if not seen_any:
            lines.append("No enabled repositories found.")
        lines.append("Use `/car bind` for repo-specific flow actions.")
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            "\n".join(lines),
        )

    async def _require_bound_workspace(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
    ) -> Optional[Path]:
        binding = await self._store.get_binding(channel_id=channel_id)
        if binding is None:
            text = format_discord_message(
                "This channel is not bound. Run `/car bind path:<...>` first."
            )
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                text,
            )
            return None
        if bool(binding.get("pma_enabled", False)):
            text = format_discord_message(
                "PMA mode is enabled for this channel. Run `/pma off` to use workspace-scoped `/car` commands."
            )
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                text,
            )
            return None
        workspace_raw = binding.get("workspace_path")
        if not isinstance(workspace_raw, str) or not workspace_raw.strip():
            text = format_discord_message(
                "Binding is invalid. Run `/car bind path:<...>` first."
            )
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                text,
            )
            return None
        return canonicalize_path(Path(workspace_raw))

    def _open_flow_store(self, workspace_root: Path) -> FlowStore:
        config = load_repo_config(workspace_root)
        store = FlowStore(
            workspace_root / ".codex-autorunner" / "flows.db",
            durable=config.durable_writes,
        )
        store.initialize()
        return store

    def _resolve_flow_run_by_id(
        self,
        store: FlowStore,
        *,
        run_id: str,
    ) -> Optional[FlowRunRecord]:
        record = store.get_flow_run(run_id)
        if record is None or record.flow_type != "ticket_flow":
            return None
        return record

    def _flow_run_mirror(self, workspace_root: Path) -> ChatRunMirror:
        return ChatRunMirror(workspace_root, logger_=self._logger)

    @staticmethod
    def _select_default_status_run(
        records: list[FlowRunRecord],
    ) -> Optional[FlowRunRecord]:
        if not records:
            return None
        for record in records:
            if record.status in {FlowRunStatus.RUNNING, FlowRunStatus.PAUSED}:
                return record
        return records[0]

    async def _handle_flow_status(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        options: dict[str, Any],
        channel_id: Optional[str] = None,
        guild_id: Optional[str] = None,
    ) -> None:
        run_id_opt = options.get("run_id")
        try:
            store = self._open_flow_store(workspace_root)
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "discord.flow.store_open_failed",
                workspace_root=str(workspace_root),
                exc=exc,
            )
            raise DiscordTransientError(
                f"Failed to open flow database: {exc}",
                user_message="Unable to access flow database. Please try again later.",
            ) from None
        try:
            record: Optional[FlowRunRecord]
            if isinstance(run_id_opt, str) and run_id_opt.strip():
                try:
                    record = self._resolve_flow_run_by_id(
                        store, run_id=run_id_opt.strip()
                    )
                except (sqlite3.Error, OSError) as exc:
                    log_event(
                        self._logger,
                        logging.ERROR,
                        "discord.flow.query_failed",
                        exc=exc,
                        run_id=run_id_opt.strip(),
                    )
                    raise DiscordTransientError(
                        f"Failed to query flow run: {exc}",
                        user_message="Unable to query flow database. Please try again later.",
                    ) from None
            else:
                try:
                    runs = store.list_flow_runs(flow_type="ticket_flow")
                except (sqlite3.Error, OSError) as exc:
                    log_event(
                        self._logger,
                        logging.ERROR,
                        "discord.flow.query_failed",
                        exc=exc,
                        workspace_root=str(workspace_root),
                    )
                    raise DiscordTransientError(
                        f"Failed to query flow runs: {exc}",
                        user_message="Unable to query flow database. Please try again later.",
                    ) from None
                record = self._select_default_status_run(runs)
            if record is None:
                await self._respond_ephemeral(
                    interaction_id, interaction_token, "No ticket_flow runs found."
                )
                return
            try:
                snapshot = build_flow_status_snapshot(workspace_root, record, store)
            except (sqlite3.Error, OSError) as exc:
                log_event(
                    self._logger,
                    logging.ERROR,
                    "discord.flow.snapshot_failed",
                    exc=exc,
                    run_id=record.id,
                )
                raise DiscordTransientError(
                    f"Failed to build flow snapshot: {exc}",
                    user_message="Unable to build flow snapshot. Please try again later.",
                ) from None
        finally:
            store.close()

        worker = snapshot.get("worker_health")
        worker_status = getattr(worker, "status", "unknown")
        worker_pid = getattr(worker, "pid", None)
        worker_text = (
            f"{worker_status} (pid={worker_pid})"
            if isinstance(worker_pid, int)
            else str(worker_status)
        )
        last_event_seq = snapshot.get("last_event_seq")
        last_event_at = snapshot.get("last_event_at")
        current_ticket = snapshot.get("effective_current_ticket")
        lines = [
            f"Run: {record.id}",
            f"Status: {record.status.value}",
            f"Last event: {last_event_seq if last_event_seq is not None else '-'} at {last_event_at or '-'}",
            f"Worker: {worker_text}",
            f"Current ticket: {current_ticket or '-'}",
        ]
        response_text = "\n".join(lines)
        run_mirror = self._flow_run_mirror(workspace_root)
        run_mirror.mirror_inbound(
            run_id=record.id,
            platform="discord",
            event_type="flow_status_command",
            kind="command",
            actor="user",
            text="/car flow status",
            chat_id=channel_id,
            thread_id=guild_id,
            message_id=interaction_id,
            meta={"interaction_token": interaction_token},
        )
        run_mirror.mirror_outbound(
            run_id=record.id,
            platform="discord",
            event_type="flow_status_notice",
            kind="notice",
            actor="car",
            text=response_text,
            chat_id=channel_id,
            thread_id=guild_id,
            meta={"response_type": "ephemeral"},
        )

        status_buttons = build_flow_status_buttons(
            record.id,
            record.status.value,
            include_refresh=True,
        )
        if status_buttons:
            await self._respond_with_components(
                interaction_id,
                interaction_token,
                response_text,
                status_buttons,
            )
        else:
            await self._respond_ephemeral(
                interaction_id, interaction_token, response_text
            )

    async def _handle_flow_runs(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        options: dict[str, Any],
    ) -> None:
        raw_limit = options.get("limit")
        limit = FLOW_RUNS_DEFAULT_LIMIT
        if isinstance(raw_limit, int):
            limit = raw_limit
        limit = max(1, min(limit, FLOW_RUNS_MAX_LIMIT))

        try:
            store = self._open_flow_store(workspace_root)
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "discord.flow.store_open_failed",
                workspace_root=str(workspace_root),
                exc=exc,
            )
            raise DiscordTransientError(
                f"Failed to open flow database: {exc}",
                user_message="Unable to access flow database. Please try again later.",
            ) from None
        try:
            try:
                runs = store.list_flow_runs(flow_type="ticket_flow")[:limit]
            except (sqlite3.Error, OSError) as exc:
                log_event(
                    self._logger,
                    logging.ERROR,
                    "discord.flow.query_failed",
                    exc=exc,
                    workspace_root=str(workspace_root),
                )
                raise DiscordTransientError(
                    f"Failed to query flow runs: {exc}",
                    user_message="Unable to query flow database. Please try again later.",
                ) from None
        finally:
            store.close()

        if not runs:
            await self._respond_ephemeral(
                interaction_id, interaction_token, "No ticket_flow runs found."
            )
            return

        run_tuples = [(record.id, record.status.value) for record in runs]
        components = [build_flow_runs_picker(run_tuples)]
        lines = [f"Recent ticket_flow runs (limit={limit}):"]
        for record in runs:
            lines.append(f"- {record.id} [{record.status.value}]")
        await self._respond_with_components(
            interaction_id, interaction_token, "\n".join(lines), components
        )

    async def _handle_flow_issue(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        options: dict[str, Any],
        channel_id: Optional[str] = None,
        guild_id: Optional[str] = None,
    ) -> None:
        _ = channel_id, guild_id
        issue_ref = options.get("issue_ref")
        if not isinstance(issue_ref, str) or not issue_ref.strip():
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Provide an issue reference: `/car flow issue issue_ref:<issue#|url>`",
            )
            return
        issue_ref = issue_ref.strip()
        try:
            seed = seed_issue_from_github(
                workspace_root,
                issue_ref,
                github_service_factory=GitHubService,
            )
            atomic_write(issue_md_path(workspace_root), seed.content)
        except GitHubError as exc:
            await self._respond_ephemeral(
                interaction_id, interaction_token, f"GitHub error: {exc}"
            )
            return
        except RuntimeError as exc:
            await self._respond_ephemeral(interaction_id, interaction_token, str(exc))
            return
        except Exception as exc:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Failed to fetch issue: {exc}",
            )
            return
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"Seeded ISSUE.md from GitHub issue {seed.issue_number}.",
        )

    async def _handle_flow_plan(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        options: dict[str, Any],
        channel_id: Optional[str] = None,
        guild_id: Optional[str] = None,
    ) -> None:
        _ = channel_id, guild_id
        plan_text = options.get("text")
        if not isinstance(plan_text, str) or not plan_text.strip():
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Provide a plan: `/car flow plan text:<plan>`",
            )
            return
        content = seed_issue_from_text(plan_text.strip())
        atomic_write(issue_md_path(workspace_root), content)
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            "Seeded ISSUE.md from your plan.",
        )

    async def _handle_flow_recover(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        options: dict[str, Any],
    ) -> None:
        run_id_opt = options.get("run_id")
        try:
            store = self._open_flow_store(workspace_root)
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "discord.flow.store_open_failed",
                workspace_root=str(workspace_root),
                exc=exc,
            )
            raise DiscordTransientError(
                f"Failed to open flow database: {exc}",
                user_message="Unable to access flow database. Please try again later.",
            ) from None
        try:
            target: Optional[FlowRunRecord] = None
            if isinstance(run_id_opt, str) and run_id_opt.strip():
                try:
                    target = self._resolve_flow_run_by_id(
                        store, run_id=run_id_opt.strip()
                    )
                except (sqlite3.Error, OSError) as exc:
                    log_event(
                        self._logger,
                        logging.ERROR,
                        "discord.flow.query_failed",
                        exc=exc,
                        run_id=run_id_opt.strip(),
                    )
                    raise DiscordTransientError(
                        f"Failed to query flow run: {exc}",
                        user_message="Unable to query flow database. Please try again later.",
                    ) from None
            else:
                try:
                    runs = store.list_flow_runs(flow_type="ticket_flow")
                except (sqlite3.Error, OSError) as exc:
                    log_event(
                        self._logger,
                        logging.ERROR,
                        "discord.flow.query_failed",
                        exc=exc,
                        workspace_root=str(workspace_root),
                    )
                    raise DiscordTransientError(
                        f"Failed to query flow runs: {exc}",
                        user_message="Unable to query flow database. Please try again later.",
                    ) from None
                target = next(
                    (record for record in runs if record.status.is_active()),
                    None,
                )

            if target is None:
                await self._respond_ephemeral(
                    interaction_id,
                    interaction_token,
                    "No active ticket_flow run found.",
                )
                return

            target, updated, locked = reconcile_flow_run(workspace_root, target, store)
            if locked:
                await self._respond_ephemeral(
                    interaction_id,
                    interaction_token,
                    f"Run {target.id} is locked for reconcile; try again.",
                )
                return
            verdict = "Recovered" if updated else "No changes needed"
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"{verdict} for run {target.id} ({target.status.value}).",
            )
        finally:
            store.close()

    @staticmethod
    def _close_worker_handles(ensure_result: dict[str, Any]) -> None:
        for key in ("stdout", "stderr"):
            handle = ensure_result.get(key)
            close = getattr(handle, "close", None)
            if callable(close):
                close()

    async def _handle_flow_resume(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        options: dict[str, Any],
        channel_id: Optional[str] = None,
        guild_id: Optional[str] = None,
    ) -> None:
        run_id_opt = options.get("run_id")
        try:
            store = self._open_flow_store(workspace_root)
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "discord.flow.store_open_failed",
                workspace_root=str(workspace_root),
                exc=exc,
            )
            raise DiscordTransientError(
                f"Failed to open flow database: {exc}",
                user_message="Unable to access flow database. Please try again later.",
            ) from None
        try:
            if isinstance(run_id_opt, str) and run_id_opt.strip():
                try:
                    target = self._resolve_flow_run_by_id(
                        store, run_id=run_id_opt.strip()
                    )
                except (sqlite3.Error, OSError) as exc:
                    log_event(
                        self._logger,
                        logging.ERROR,
                        "discord.flow.query_failed",
                        exc=exc,
                        run_id=run_id_opt.strip(),
                    )
                    raise DiscordTransientError(
                        f"Failed to query flow run: {exc}",
                        user_message="Unable to query flow database. Please try again later.",
                    ) from None
            else:
                try:
                    runs = store.list_flow_runs(flow_type="ticket_flow")
                except (sqlite3.Error, OSError) as exc:
                    log_event(
                        self._logger,
                        logging.ERROR,
                        "discord.flow.query_failed",
                        exc=exc,
                        workspace_root=str(workspace_root),
                    )
                    raise DiscordTransientError(
                        f"Failed to query flow runs: {exc}",
                        user_message="Unable to query flow database. Please try again later.",
                    ) from None
                target = next(
                    (
                        record
                        for record in runs
                        if record.status == FlowRunStatus.PAUSED
                    ),
                    None,
                )
        finally:
            store.close()

        if target is None:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "No paused ticket_flow run found to resume.",
            )
            return

        run_mirror = self._flow_run_mirror(workspace_root)
        run_mirror.mirror_inbound(
            run_id=target.id,
            platform="discord",
            event_type="flow_resume_command",
            kind="command",
            actor="user",
            text="/car flow resume",
            chat_id=channel_id,
            thread_id=guild_id,
            message_id=interaction_id,
        )
        controller = build_ticket_flow_controller(workspace_root)
        try:
            updated = await controller.resume_flow(target.id)
        except ValueError as exc:
            await self._respond_ephemeral(interaction_id, interaction_token, str(exc))
            return

        ensure_result = ensure_worker(
            workspace_root, updated.id, is_terminal=updated.status.is_terminal()
        )
        self._close_worker_handles(ensure_result)
        outbound_text = f"Resumed run {updated.id}."
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            outbound_text,
        )
        run_mirror.mirror_outbound(
            run_id=updated.id,
            platform="discord",
            event_type="flow_resume_notice",
            kind="notice",
            actor="car",
            text=outbound_text,
            chat_id=channel_id,
            thread_id=guild_id,
        )

    async def _handle_flow_stop(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        options: dict[str, Any],
        channel_id: Optional[str] = None,
        guild_id: Optional[str] = None,
    ) -> None:
        run_id_opt = options.get("run_id")
        try:
            store = self._open_flow_store(workspace_root)
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "discord.flow.store_open_failed",
                workspace_root=str(workspace_root),
                exc=exc,
            )
            raise DiscordTransientError(
                f"Failed to open flow database: {exc}",
                user_message="Unable to access flow database. Please try again later.",
            ) from None
        try:
            if isinstance(run_id_opt, str) and run_id_opt.strip():
                try:
                    target = self._resolve_flow_run_by_id(
                        store, run_id=run_id_opt.strip()
                    )
                except (sqlite3.Error, OSError) as exc:
                    log_event(
                        self._logger,
                        logging.ERROR,
                        "discord.flow.query_failed",
                        exc=exc,
                        run_id=run_id_opt.strip(),
                    )
                    raise DiscordTransientError(
                        f"Failed to query flow run: {exc}",
                        user_message="Unable to query flow database. Please try again later.",
                    ) from None
            else:
                try:
                    runs = store.list_flow_runs(flow_type="ticket_flow")
                except (sqlite3.Error, OSError) as exc:
                    log_event(
                        self._logger,
                        logging.ERROR,
                        "discord.flow.query_failed",
                        exc=exc,
                        workspace_root=str(workspace_root),
                    )
                    raise DiscordTransientError(
                        f"Failed to query flow runs: {exc}",
                        user_message="Unable to query flow database. Please try again later.",
                    ) from None
                target = next(
                    (record for record in runs if not record.status.is_terminal()),
                    None,
                )
        finally:
            store.close()

        if target is None:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "No active ticket_flow run found to stop.",
            )
            return

        run_mirror = self._flow_run_mirror(workspace_root)
        run_mirror.mirror_inbound(
            run_id=target.id,
            platform="discord",
            event_type="flow_stop_command",
            kind="command",
            actor="user",
            text="/car flow stop",
            chat_id=channel_id,
            thread_id=guild_id,
            message_id=interaction_id,
        )
        controller = build_ticket_flow_controller(workspace_root)
        try:
            updated = await controller.stop_flow(target.id)
        except ValueError as exc:
            await self._respond_ephemeral(interaction_id, interaction_token, str(exc))
            return

        outbound_text = f"Stop requested for run {updated.id} ({updated.status.value})."
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            outbound_text,
        )
        run_mirror.mirror_outbound(
            run_id=updated.id,
            platform="discord",
            event_type="flow_stop_notice",
            kind="notice",
            actor="car",
            text=outbound_text,
            chat_id=channel_id,
            thread_id=guild_id,
            meta={"status": updated.status.value},
        )

    async def _handle_flow_archive(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        options: dict[str, Any],
        channel_id: Optional[str] = None,
        guild_id: Optional[str] = None,
    ) -> None:
        run_id_opt = options.get("run_id")
        try:
            store = self._open_flow_store(workspace_root)
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "discord.flow.store_open_failed",
                workspace_root=str(workspace_root),
                exc=exc,
            )
            raise DiscordTransientError(
                f"Failed to open flow database: {exc}",
                user_message="Unable to access flow database. Please try again later.",
            ) from None
        try:
            if isinstance(run_id_opt, str) and run_id_opt.strip():
                try:
                    target = self._resolve_flow_run_by_id(
                        store, run_id=run_id_opt.strip()
                    )
                except (sqlite3.Error, OSError) as exc:
                    log_event(
                        self._logger,
                        logging.ERROR,
                        "discord.flow.query_failed",
                        exc=exc,
                        run_id=run_id_opt.strip(),
                    )
                    raise DiscordTransientError(
                        f"Failed to query flow run: {exc}",
                        user_message="Unable to query flow database. Please try again later.",
                    ) from None
            else:
                try:
                    runs = store.list_flow_runs(flow_type="ticket_flow")
                except (sqlite3.Error, OSError) as exc:
                    log_event(
                        self._logger,
                        logging.ERROR,
                        "discord.flow.query_failed",
                        exc=exc,
                        workspace_root=str(workspace_root),
                    )
                    raise DiscordTransientError(
                        f"Failed to query flow runs: {exc}",
                        user_message="Unable to query flow database. Please try again later.",
                    ) from None
                target = runs[0] if runs else None
        finally:
            store.close()

        if target is None:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "No ticket_flow run found to archive.",
            )
            return

        run_mirror = self._flow_run_mirror(workspace_root)
        run_mirror.mirror_inbound(
            run_id=target.id,
            platform="discord",
            event_type="flow_archive_command",
            kind="command",
            actor="user",
            text="/car flow archive",
            chat_id=channel_id,
            thread_id=guild_id,
            message_id=interaction_id,
        )
        try:
            summary = archive_flow_run_artifacts(
                workspace_root,
                run_id=target.id,
                force=False,
                delete_run=False,
            )
        except ValueError as exc:
            await self._respond_ephemeral(interaction_id, interaction_token, str(exc))
            return

        outbound_text = f"Archived run {summary['run_id']} (runs_archived={summary['archived_runs']})."
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            outbound_text,
        )
        run_mirror.mirror_outbound(
            run_id=target.id,
            platform="discord",
            event_type="flow_archive_notice",
            kind="notice",
            actor="car",
            text=outbound_text,
            chat_id=channel_id,
            thread_id=guild_id,
            meta={"archived_runs": summary.get("archived_runs")},
        )

    async def _handle_flow_reply(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        options: dict[str, Any],
        channel_id: Optional[str] = None,
        guild_id: Optional[str] = None,
    ) -> None:
        text = options.get("text")
        if not isinstance(text, str) or not text.strip():
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Missing required option: text",
            )
            return

        run_id_opt = options.get("run_id")
        try:
            store = self._open_flow_store(workspace_root)
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "discord.flow.store_open_failed",
                workspace_root=str(workspace_root),
                exc=exc,
            )
            raise DiscordTransientError(
                f"Failed to open flow database: {exc}",
                user_message="Unable to access flow database. Please try again later.",
            ) from None
        try:
            if isinstance(run_id_opt, str) and run_id_opt.strip():
                try:
                    target = self._resolve_flow_run_by_id(
                        store, run_id=run_id_opt.strip()
                    )
                except (sqlite3.Error, OSError) as exc:
                    log_event(
                        self._logger,
                        logging.ERROR,
                        "discord.flow.query_failed",
                        exc=exc,
                        run_id=run_id_opt.strip(),
                    )
                    raise DiscordTransientError(
                        f"Failed to query flow run: {exc}",
                        user_message="Unable to query flow database. Please try again later.",
                    ) from None
                if target and target.status != FlowRunStatus.PAUSED:
                    target = None
            else:
                try:
                    runs = store.list_flow_runs(flow_type="ticket_flow")
                except (sqlite3.Error, OSError) as exc:
                    log_event(
                        self._logger,
                        logging.ERROR,
                        "discord.flow.query_failed",
                        exc=exc,
                        workspace_root=str(workspace_root),
                    )
                    raise DiscordTransientError(
                        f"Failed to query flow runs: {exc}",
                        user_message="Unable to query flow database. Please try again later.",
                    ) from None
                target = next(
                    (
                        record
                        for record in runs
                        if record.status == FlowRunStatus.PAUSED
                    ),
                    None,
                )
        finally:
            store.close()

        if target is None:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "No paused ticket_flow run found for reply.",
            )
            return

        run_mirror = self._flow_run_mirror(workspace_root)
        run_mirror.mirror_inbound(
            run_id=target.id,
            platform="discord",
            event_type="flow_reply_command",
            kind="command",
            actor="user",
            text=text,
            chat_id=channel_id,
            thread_id=guild_id,
            message_id=interaction_id,
        )
        reply_path = self._write_user_reply(workspace_root, target, text)

        controller = build_ticket_flow_controller(workspace_root)
        try:
            updated = await controller.resume_flow(target.id)
        except ValueError as exc:
            await self._respond_ephemeral(interaction_id, interaction_token, str(exc))
            return

        ensure_result = ensure_worker(
            workspace_root, updated.id, is_terminal=updated.status.is_terminal()
        )
        self._close_worker_handles(ensure_result)
        outbound_text = (
            f"Reply saved to {reply_path.name} and resumed run {updated.id}."
        )
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            outbound_text,
        )
        run_mirror.mirror_outbound(
            run_id=updated.id,
            platform="discord",
            event_type="flow_reply_notice",
            kind="notice",
            actor="car",
            text=outbound_text,
            chat_id=channel_id,
            thread_id=guild_id,
        )

    def _write_user_reply(
        self,
        workspace_root: Path,
        record: FlowRunRecord,
        text: str,
    ) -> Path:
        runs_dir_raw = record.input_data.get("runs_dir")
        runs_dir = (
            Path(runs_dir_raw)
            if isinstance(runs_dir_raw, str) and runs_dir_raw
            else Path(".codex-autorunner/runs")
        )
        run_paths = resolve_outbox_paths(
            workspace_root=workspace_root,
            runs_dir=runs_dir,
            run_id=record.id,
        )
        try:
            run_paths.run_dir.mkdir(parents=True, exist_ok=True)
            reply_path = run_paths.run_dir / "USER_REPLY.md"
            reply_path.write_text(text.strip() + "\n", encoding="utf-8")
            return reply_path
        except OSError as exc:
            self._logger.error(
                "Failed to write user reply (run_id=%s, path=%s): %s",
                record.id,
                run_paths.run_dir,
                exc,
            )
            raise

    def _format_file_size(self, size: int) -> str:
        if size < 1024:
            return f"{size} B"
        value = size / 1024
        for unit in ("KB", "MB", "GB"):
            if value < 1024:
                return f"{value:.1f} {unit}"
            value /= 1024
        return f"{value:.1f} TB"

    def _list_files_in_dir(self, folder: Path) -> list[tuple[str, int, str]]:
        if not folder.exists():
            return []
        files: list[tuple[str, int, str]] = []
        for path in folder.iterdir():
            try:
                if path.is_file():
                    stat = path.stat()
                    from datetime import datetime, timezone

                    mtime = datetime.fromtimestamp(
                        stat.st_mtime, tz=timezone.utc
                    ).strftime("%Y-%m-%d %H:%M")
                    files.append((path.name, stat.st_size, mtime))
            except OSError:
                continue
        return sorted(files, key=lambda x: x[2], reverse=True)

    def _delete_files_in_dir(self, folder: Path) -> int:
        if not folder.exists():
            return 0
        deleted = 0
        for path in folder.iterdir():
            try:
                if path.is_file():
                    path.unlink()
                    deleted += 1
            except OSError:
                continue
        return deleted

    async def _handle_files_inbox(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
    ) -> None:
        inbox = inbox_dir(workspace_root)
        files = self._list_files_in_dir(inbox)
        if not files:
            await self._respond_ephemeral(
                interaction_id, interaction_token, "Inbox: (empty)"
            )
            return
        lines = [f"Inbox ({len(files)} file(s)):"]
        for name, size, mtime in files[:20]:
            lines.append(f"- {name} ({self._format_file_size(size)}, {mtime})")
        if len(files) > 20:
            lines.append(f"... and {len(files) - 20} more")
        await self._respond_ephemeral(
            interaction_id, interaction_token, "\n".join(lines)
        )

    async def _handle_files_outbox(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
    ) -> None:
        pending = outbox_pending_dir(workspace_root)
        sent = outbox_sent_dir(workspace_root)
        pending_files = self._list_files_in_dir(pending)
        sent_files = self._list_files_in_dir(sent)
        lines = []
        if pending_files:
            lines.append(f"Outbox pending ({len(pending_files)} file(s)):")
            for name, size, mtime in pending_files[:20]:
                lines.append(f"- {name} ({self._format_file_size(size)}, {mtime})")
            if len(pending_files) > 20:
                lines.append(f"... and {len(pending_files) - 20} more")
        else:
            lines.append("Outbox pending: (empty)")
        lines.append("")
        if sent_files:
            lines.append(f"Outbox sent ({len(sent_files)} file(s)):")
            for name, size, mtime in sent_files[:10]:
                lines.append(f"- {name} ({self._format_file_size(size)}, {mtime})")
            if len(sent_files) > 10:
                lines.append(f"... and {len(sent_files) - 10} more")
        else:
            lines.append("Outbox sent: (empty)")
        await self._respond_ephemeral(
            interaction_id, interaction_token, "\n".join(lines)
        )

    async def _handle_files_clear(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        options: dict[str, Any],
    ) -> None:
        target = (options.get("target") or "all").lower().strip()
        inbox = inbox_dir(workspace_root)
        pending = outbox_pending_dir(workspace_root)
        sent = outbox_sent_dir(workspace_root)
        deleted = 0
        if target == "inbox":
            deleted = self._delete_files_in_dir(inbox)
        elif target == "outbox":
            deleted = self._delete_files_in_dir(pending)
            deleted += self._delete_files_in_dir(sent)
        elif target == "all":
            deleted = self._delete_files_in_dir(inbox)
            deleted += self._delete_files_in_dir(pending)
            deleted += self._delete_files_in_dir(sent)
        else:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Invalid target. Use: inbox, outbox, or all",
            )
            return
        await self._respond_ephemeral(
            interaction_id, interaction_token, f"Deleted {deleted} file(s)."
        )

    async def _handle_pma_command(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        guild_id: Optional[str],
        command_path: tuple[str, ...],
        options: Optional[dict[str, Any]] = None,
    ) -> None:
        if not self._config.pma_enabled:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "PMA is disabled in hub config. Set pma.enabled: true to enable.",
            )
            return

        command_options = options or {}
        subcommand = command_path[1] if len(command_path) > 1 else "status"

        if subcommand == "on":
            await self._handle_pma_on(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                guild_id=guild_id,
            )
        elif subcommand == "off":
            await self._handle_pma_off(
                interaction_id, interaction_token, channel_id=channel_id
            )
        elif subcommand == "status":
            await self._handle_pma_status(
                interaction_id, interaction_token, channel_id=channel_id
            )
        elif subcommand == "targets":
            await self._handle_pma_targets_list(
                interaction_id,
                interaction_token,
            )
        elif subcommand == "target":
            action = command_path[2] if len(command_path) > 2 else ""
            await self._handle_pma_target_mutation(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                action=action,
                options=command_options,
            )
        else:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Unknown PMA subcommand. Use on, off, or status.",
            )

    async def _handle_pma_on(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        guild_id: Optional[str],
    ) -> None:
        binding = await self._store.get_binding(channel_id=channel_id)
        if binding is not None and binding.get("pma_enabled", False):
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "PMA mode is already enabled for this channel. Use /pma off to exit.",
            )
            return

        prev_workspace = binding.get("workspace_path") if binding is not None else None
        prev_repo_id = binding.get("repo_id") if binding is not None else None

        if binding is None:
            # Match Telegram behavior: /pma on can activate PMA on unbound channels.
            await self._store.upsert_binding(
                channel_id=channel_id,
                guild_id=guild_id,
                workspace_path=str(self._config.root),
                repo_id=None,
            )

        await self._store.update_pma_state(
            channel_id=channel_id,
            pma_enabled=True,
            pma_prev_workspace_path=prev_workspace,
            pma_prev_repo_id=prev_repo_id,
        )

        targets_store = PmaDeliveryTargetsStore(self._config.root)
        here_target = self._pma_here_target(channel_id)
        state = targets_store.load()
        current_targets = [
            target for target in state.get("targets", []) if isinstance(target, dict)
        ]
        if len(current_targets) > 1:
            targets_store.add_target(here_target)
        else:
            targets_store.set_targets([here_target])

        hint = (
            "Use /pma off to exit. Previous binding saved."
            if prev_workspace
            else "Use /pma off to exit."
        )
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"PMA mode enabled. {hint}",
        )

    async def _handle_pma_off(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
    ) -> None:
        binding = await self._store.get_binding(channel_id=channel_id)
        if binding is None:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "PMA mode disabled. Back to repo mode.",
            )
            return

        prev_workspace = binding.get("pma_prev_workspace_path")
        prev_repo_id = binding.get("pma_prev_repo_id")

        await self._store.update_pma_state(
            channel_id=channel_id,
            pma_enabled=False,
            pma_prev_workspace_path=None,
            pma_prev_repo_id=None,
        )

        if prev_workspace:
            await self._store.upsert_binding(
                channel_id=channel_id,
                guild_id=binding.get("guild_id"),
                workspace_path=prev_workspace,
                repo_id=prev_repo_id,
            )
            hint = f"Restored binding to {prev_workspace}."
        else:
            await self._store.delete_binding(channel_id=channel_id)
            hint = "Back to repo mode."

        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"PMA mode disabled. {hint}",
        )

    async def _handle_pma_status(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
    ) -> None:
        binding = await self._store.get_binding(channel_id=channel_id)
        here_key = target_key(self._pma_here_target(channel_id))
        targets = PmaDeliveryTargetsStore(self._config.root).load().get("targets", [])
        active_here = isinstance(here_key, str) and any(
            target_key(candidate) == here_key
            for candidate in targets
            if isinstance(candidate, dict)
        )
        if binding is None:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "\n".join(
                    [
                        "PMA mode: disabled",
                        f"Delivery targets include this channel: {active_here}",
                        "Current workspace: unbound",
                    ]
                ),
            )
            return

        pma_enabled = binding.get("pma_enabled", False)
        status = "enabled" if pma_enabled else "disabled"

        if pma_enabled:
            lines = [
                f"PMA mode: {status}",
                f"Delivery targets include this channel: {active_here}",
            ]
        else:
            workspace = binding.get("workspace_path", "unknown")
            lines = [
                f"PMA mode: {status}",
                f"Current workspace: {workspace}",
            ]

        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            "\n".join(lines),
        )

    async def _handle_pma_targets_list(
        self,
        interaction_id: str,
        interaction_token: str,
    ) -> None:
        store = PmaDeliveryTargetsStore(self._config.root)
        state = store.load()
        active_key = store.get_active_target_key()
        targets = [
            target for target in state.get("targets", []) if isinstance(target, dict)
        ]
        if not targets:
            text = "PMA delivery targets: (none)"
        else:
            lines = ["PMA delivery targets:"]
            for target in targets:
                key = target_key(target)
                if not isinstance(key, str):
                    continue
                label = self._format_pma_target_label(target)
                active_suffix = " [active]" if key == active_key else ""
                if label:
                    lines.append(f"- {key} ({label}){active_suffix}")
                else:
                    lines.append(f"- {key}{active_suffix}")
            text = "\n".join(lines)
        await self._respond_ephemeral(interaction_id, interaction_token, text)

    async def _handle_pma_target_mutation(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        action: str,
        options: dict[str, Any],
    ) -> None:
        action_norm = action.strip().lower()
        store = PmaDeliveryTargetsStore(self._config.root)
        if action_norm == "clear":
            store.set_targets([])
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Cleared PMA delivery targets.",
            )
            return

        if action_norm == "active":
            raw_ref = options.get("ref")
            ref_or_key = raw_ref.strip() if isinstance(raw_ref, str) else ""
            if not ref_or_key:
                await self._respond_ephemeral(
                    interaction_id,
                    interaction_token,
                    self._format_pma_active_target(store),
                )
                return
            key, error = self._resolve_pma_target_key_for_active(
                channel_id=channel_id,
                store=store,
                ref_or_key=ref_or_key,
            )
            if error:
                await self._respond_ephemeral(
                    interaction_id,
                    interaction_token,
                    error,
                )
                return
            if not isinstance(key, str):
                await self._respond_ephemeral(
                    interaction_id,
                    interaction_token,
                    "Unable to resolve target key.",
                )
                return
            changed = store.set_active_target(key)
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                (
                    f"Set active PMA delivery target: {key}"
                    if changed
                    else f"PMA delivery target already active: {key}"
                ),
            )
            return

        if action_norm not in {"add", "rm", "remove"}:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                self._pma_usage_text(),
            )
            return

        raw_ref = options.get("ref")
        ref = raw_ref.strip() if isinstance(raw_ref, str) else ""
        if not ref:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                self._pma_usage_text(),
            )
            return
        target = self._parse_pma_target_ref(channel_id=channel_id, ref=ref)
        if target is None:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Invalid target ref '{ref}'.\n{self._pma_usage_text()}",
            )
            return
        key = target_key(target)
        if not isinstance(key, str):
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Invalid target ref '{ref}'.",
            )
            return

        if action_norm == "add":
            store.add_target(target)
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Added PMA delivery target: {key}",
            )
            return

        removed = store.remove_target(target)
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            (
                f"Removed PMA delivery target: {key}"
                if removed
                else f"PMA delivery target not found: {key}"
            ),
        )

    def _pma_usage_text(self) -> str:
        return "\n".join(
            [
                "Usage:",
                "/pma on|off|status|targets",
                "/pma target add <ref>",
                "/pma target rm <ref>",
                "/pma target clear",
                "/pma target active [ref|key]",
                pma_targets_usage(include_here=True),
            ]
        )

    def _format_pma_active_target(self, store: PmaDeliveryTargetsStore) -> str:
        active_key = store.get_active_target_key()
        if not isinstance(active_key, str):
            return "Active PMA delivery target: (none)"
        state = store.load()
        active_target = next(
            (
                target
                for target in state.get("targets", [])
                if isinstance(target, dict) and target_key(target) == active_key
            ),
            None,
        )
        label = (
            self._format_pma_target_label(active_target)
            if isinstance(active_target, dict)
            else ""
        )
        if label:
            return f"Active PMA delivery target: {active_key} ({label})"
        return f"Active PMA delivery target: {active_key}"

    def _resolve_pma_target_key_for_active(
        self,
        *,
        channel_id: str,
        store: PmaDeliveryTargetsStore,
        ref_or_key: str,
    ) -> tuple[Optional[str], Optional[str]]:
        candidate = ref_or_key.strip()
        if not candidate:
            return None, self._pma_usage_text()
        state = store.load()
        known_keys = {
            key
            for key in (target_key(target) for target in state.get("targets", []))
            if isinstance(key, str)
        }
        target = self._parse_pma_target_ref(channel_id=channel_id, ref=candidate)
        if target is None:
            if candidate in known_keys:
                return candidate, None
            return None, f"Invalid target ref '{candidate}'.\n{self._pma_usage_text()}"
        key = target_key(target)
        if not isinstance(key, str):
            return None, f"Invalid target ref '{candidate}'."
        if key not in known_keys:
            return None, f"PMA delivery target not found: {key}"
        return key, None

    def _pma_here_target(self, channel_id: str) -> dict[str, Any]:
        return {
            "kind": "chat",
            "platform": "discord",
            "chat_id": channel_id,
        }

    def _parse_pma_target_ref(
        self, *, channel_id: str, ref: str
    ) -> Optional[dict[str, Any]]:
        return parse_pma_target_ref(ref, here_target=self._pma_here_target(channel_id))

    def _format_pma_target_label(self, target: dict[str, Any]) -> str:
        label = target.get("label")
        if isinstance(label, str) and label.strip():
            return label.strip()
        kind = target.get("kind")
        if kind == "chat":
            platform = target.get("platform")
            chat_id = target.get("chat_id")
            thread_id = target.get("thread_id")
            if (
                isinstance(platform, str)
                and isinstance(chat_id, str)
                and platform
                and chat_id
            ):
                if isinstance(thread_id, str) and thread_id:
                    return f"{platform} {chat_id} thread {thread_id}"
                return f"{platform} {chat_id}"
        if kind == "local":
            path = target.get("path")
            if isinstance(path, str) and path:
                return path
        return ""

    async def _respond_ephemeral(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
    ) -> None:
        max_len = max(int(self._config.max_message_length), 32)
        content = truncate_for_discord(text, max_len=max_len)
        try:
            await self._rest.create_interaction_response(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                payload={
                    "type": 4,
                    "data": {
                        "content": content,
                        "flags": DISCORD_EPHEMERAL_FLAG,
                    },
                },
            )
        except DiscordAPIError as exc:
            sent_followup = await self._send_followup_ephemeral(
                interaction_token=interaction_token,
                content=content,
            )
            if not sent_followup:
                self._logger.error(
                    "Failed to send ephemeral response: %s (interaction_id=%s)",
                    exc,
                    interaction_id,
                )

    async def _defer_ephemeral(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
    ) -> bool:
        try:
            await self._rest.create_interaction_response(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                payload={
                    "type": 5,
                    "data": {
                        "flags": DISCORD_EPHEMERAL_FLAG,
                    },
                },
            )
        except DiscordAPIError as exc:
            self._logger.warning(
                "Failed to defer ephemeral response: %s (interaction_id=%s)",
                exc,
                interaction_id,
            )
            return False
        return True

    async def _send_or_respond_ephemeral(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        deferred: bool,
        text: str,
    ) -> None:
        if deferred:
            max_len = max(int(self._config.max_message_length), 32)
            sent = await self._send_followup_ephemeral(
                interaction_token=interaction_token,
                content=truncate_for_discord(text, max_len=max_len),
            )
            if sent:
                return
        await self._respond_ephemeral(interaction_id, interaction_token, text)

    async def _respond_with_components(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
        components: list[dict[str, Any]],
    ) -> None:
        max_len = max(int(self._config.max_message_length), 32)
        content = truncate_for_discord(text, max_len=max_len)
        try:
            await self._rest.create_interaction_response(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                payload={
                    "type": 4,
                    "data": {
                        "content": content,
                        "flags": DISCORD_EPHEMERAL_FLAG,
                        "components": components,
                    },
                },
            )
        except DiscordAPIError as exc:
            sent_followup = await self._send_followup_ephemeral(
                interaction_token=interaction_token,
                content=content,
                components=components,
            )
            if not sent_followup:
                self._logger.error(
                    "Failed to send component response: %s (interaction_id=%s)",
                    exc,
                    interaction_id,
                )

    async def _send_followup_ephemeral(
        self,
        *,
        interaction_token: str,
        content: str,
        components: Optional[list[dict[str, Any]]] = None,
    ) -> bool:
        application_id = (self._config.application_id or "").strip()
        if not application_id:
            return False
        payload: dict[str, Any] = {
            "content": content,
            "flags": DISCORD_EPHEMERAL_FLAG,
        }
        if components:
            payload["components"] = components
        try:
            await self._rest.create_followup_message(
                application_id=application_id,
                interaction_token=interaction_token,
                payload=payload,
            )
        except Exception:
            return False
        return True

    async def _handle_component_interaction(
        self, interaction_payload: dict[str, Any]
    ) -> None:
        interaction_id = extract_interaction_id(interaction_payload)
        interaction_token = extract_interaction_token(interaction_payload)
        channel_id = extract_channel_id(interaction_payload)

        if not interaction_id or not interaction_token or not channel_id:
            self._logger.warning(
                "handle_component_interaction: missing required fields (interaction_id=%s, token=%s, channel=%s)",
                bool(interaction_id),
                bool(interaction_token),
                bool(channel_id),
            )
            return

        if not allowlist_allows(interaction_payload, self._allowlist):
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "This Discord interaction is not authorized.",
            )
            return

        custom_id = extract_component_custom_id(interaction_payload)
        if not custom_id:
            self._logger.debug(
                "handle_component_interaction: missing custom_id (interaction_id=%s)",
                interaction_id,
            )
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "I could not identify this interaction action. Please retry.",
            )
            return

        try:
            if custom_id == "bind_select":
                values = extract_component_values(interaction_payload)
                if not values:
                    await self._respond_ephemeral(
                        interaction_id,
                        interaction_token,
                        "Please select a repository and try again.",
                    )
                    return
                await self._handle_bind_selection(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                    guild_id=extract_guild_id(interaction_payload),
                    selected_repo_id=values[0],
                )
                return

            if custom_id == "flow_runs_select":
                values = extract_component_values(interaction_payload)
                if not values:
                    await self._respond_ephemeral(
                        interaction_id,
                        interaction_token,
                        "Please select a run and try again.",
                    )
                    return
                workspace_root = await self._require_bound_workspace(
                    interaction_id, interaction_token, channel_id=channel_id
                )
                if workspace_root:
                    await self._handle_flow_status(
                        interaction_id,
                        interaction_token,
                        workspace_root=workspace_root,
                        options={"run_id": values[0]},
                        channel_id=channel_id,
                        guild_id=extract_guild_id(interaction_payload),
                    )
                return

            if custom_id.startswith("flow:"):
                workspace_root = await self._require_bound_workspace(
                    interaction_id, interaction_token, channel_id=channel_id
                )
                if workspace_root:
                    await self._handle_flow_button(
                        interaction_id,
                        interaction_token,
                        workspace_root=workspace_root,
                        custom_id=custom_id,
                        channel_id=channel_id,
                        guild_id=extract_guild_id(interaction_payload),
                    )
                return

            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Unknown component: {custom_id}",
            )
        except DiscordTransientError as exc:
            user_msg = exc.user_message or "An error occurred. Please try again later."
            await self._respond_ephemeral(interaction_id, interaction_token, user_msg)
        except Exception as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "discord.component.unhandled_error",
                custom_id=custom_id,
                channel_id=channel_id,
                exc=exc,
            )
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "An unexpected error occurred. Please try again later.",
            )

    async def _handle_component_interaction_normalized(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        custom_id: str,
        values: Optional[list[str]] = None,
        guild_id: Optional[str] = None,
    ) -> None:
        try:
            if custom_id == "bind_select":
                if not values:
                    await self._respond_ephemeral(
                        interaction_id,
                        interaction_token,
                        "Please select a repository and try again.",
                    )
                    return
                await self._handle_bind_selection(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                    guild_id=guild_id,
                    selected_repo_id=values[0],
                )
                return

            if custom_id == "flow_runs_select":
                if not values:
                    await self._respond_ephemeral(
                        interaction_id,
                        interaction_token,
                        "Please select a run and try again.",
                    )
                    return
                workspace_root = await self._require_bound_workspace(
                    interaction_id, interaction_token, channel_id=channel_id
                )
                if workspace_root:
                    await self._handle_flow_status(
                        interaction_id,
                        interaction_token,
                        workspace_root=workspace_root,
                        options={"run_id": values[0]},
                        channel_id=channel_id,
                        guild_id=guild_id,
                    )
                return

            if custom_id.startswith("flow:"):
                workspace_root = await self._require_bound_workspace(
                    interaction_id, interaction_token, channel_id=channel_id
                )
                if workspace_root:
                    await self._handle_flow_button(
                        interaction_id,
                        interaction_token,
                        workspace_root=workspace_root,
                        custom_id=custom_id,
                        channel_id=channel_id,
                        guild_id=guild_id,
                    )
                return

            if custom_id == "cancel_turn":
                await self._handle_cancel_turn_button(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                )
                return

            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Unknown component: {custom_id}",
            )
        except DiscordTransientError as exc:
            user_msg = exc.user_message or "An error occurred. Please try again later."
            await self._respond_ephemeral(interaction_id, interaction_token, user_msg)
        except Exception as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "discord.component.normalized.unhandled_error",
                custom_id=custom_id,
                channel_id=channel_id,
                exc=exc,
            )
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "An unexpected error occurred. Please try again later.",
            )

    async def _handle_bind_selection(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        guild_id: Optional[str],
        selected_repo_id: str,
    ) -> None:
        if selected_repo_id == "none":
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "No workspace selected.",
            )
            return

        repos = self._list_manifest_repos()
        matching = [(rid, path) for rid, path in repos if rid == selected_repo_id]
        if not matching:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Repo not found: {selected_repo_id}",
            )
            return

        _, workspace_path = matching[0]
        workspace = canonicalize_path(Path(workspace_path))
        if not workspace.exists() or not workspace.is_dir():
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Workspace path does not exist: {workspace}",
            )
            return

        await self._store.upsert_binding(
            channel_id=channel_id,
            guild_id=guild_id,
            workspace_path=str(workspace),
            repo_id=selected_repo_id,
        )
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"Bound this channel to: {selected_repo_id} ({workspace})",
        )

    async def _handle_flow_button(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        custom_id: str,
        channel_id: Optional[str] = None,
        guild_id: Optional[str] = None,
    ) -> None:
        parts = custom_id.split(":")
        if len(parts) < 3:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Invalid button action: {custom_id}",
            )
            return

        run_id = parts[1]
        action = parts[2]

        if action == "resume":
            controller = build_ticket_flow_controller(workspace_root)
            try:
                updated = await controller.resume_flow(run_id)
            except ValueError as exc:
                await self._respond_ephemeral(
                    interaction_id, interaction_token, str(exc)
                )
                return

            ensure_result = ensure_worker(
                workspace_root, updated.id, is_terminal=updated.status.is_terminal()
            )
            self._close_worker_handles(ensure_result)
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Resumed run {updated.id}.",
            )
        elif action == "stop":
            controller = build_ticket_flow_controller(workspace_root)
            try:
                updated = await controller.stop_flow(run_id)
            except ValueError as exc:
                await self._respond_ephemeral(
                    interaction_id, interaction_token, str(exc)
                )
                return

            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Stop requested for run {updated.id} ({updated.status.value}).",
            )
        elif action == "archive":
            try:
                summary = archive_flow_run_artifacts(
                    workspace_root,
                    run_id=run_id,
                    force=False,
                    delete_run=False,
                )
            except ValueError as exc:
                await self._respond_ephemeral(
                    interaction_id, interaction_token, str(exc)
                )
                return

            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Archived run {summary['run_id']} (runs_archived={summary['archived_runs']}).",
            )
        elif action == "refresh":
            await self._handle_flow_status(
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
                options={"run_id": run_id},
                channel_id=channel_id,
                guild_id=guild_id,
            )
        else:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Unknown action: {action}",
            )

    async def _handle_car_reset(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
    ) -> None:
        binding = await self._store.get_binding(channel_id=channel_id)
        if binding is None:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Channel not bound. Use /car bind first.",
            )
            return

        pma_enabled = bool(binding.get("pma_enabled", False))
        workspace_raw = binding.get("workspace_path")
        workspace_root: Optional[Path] = None
        if isinstance(workspace_raw, str) and workspace_raw.strip():
            workspace_root = canonicalize_path(Path(workspace_raw))
            if not workspace_root.exists() or not workspace_root.is_dir():
                workspace_root = None

        if workspace_root is None:
            if pma_enabled:
                workspace_root = canonicalize_path(Path(self._config.root))
            else:
                await self._respond_ephemeral(
                    interaction_id,
                    interaction_token,
                    "Binding is invalid. Run `/car bind path:<workspace>`.",
                )
                return

        agent = (binding.get("agent") or self.DEFAULT_AGENT).strip().lower()
        if agent not in self.VALID_AGENT_VALUES:
            agent = self.DEFAULT_AGENT

        session_key = self._build_message_session_key(
            channel_id=channel_id,
            workspace_root=workspace_root,
            pma_enabled=pma_enabled,
            agent=agent,
        )
        orchestrator_channel_key = (
            channel_id if not pma_enabled else f"pma:{channel_id}"
        )
        orchestrator = await self._orchestrator_for_workspace(
            workspace_root, channel_id=orchestrator_channel_key
        )
        had_previous = orchestrator.reset_thread_id(session_key)
        mode_label = "PMA" if pma_enabled else "repo"
        state_label = "cleared previous thread" if had_previous else "fresh state"

        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"Reset {mode_label} thread state ({state_label}) for `{agent}`.",
        )

    async def _handle_car_review(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        workspace_root: Path,
        options: dict[str, Any],
    ) -> None:
        binding = await self._store.get_binding(channel_id=channel_id)
        if binding is None:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Channel binding not found.",
            )
            return

        target_arg = options.get("target", "")
        target_type = "uncommittedChanges"
        target_value: Optional[str] = None

        if isinstance(target_arg, str) and target_arg.strip():
            target_text = target_arg.strip()
            if target_text.lower().startswith("base "):
                branch = target_text[5:].strip()
                if branch:
                    target_type = "baseBranch"
                    target_value = branch
            elif target_text.lower().startswith("commit "):
                sha = target_text[7:].strip()
                if sha:
                    target_type = "commit"
                    target_value = sha
            elif target_text.lower() in ("uncommitted", ""):
                pass
            else:
                target_type = "custom"
                target_value = target_text

        await self._defer_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )

        prompt_parts: list[str] = ["Please perform a code review."]
        if target_type == "uncommittedChanges":
            prompt_parts.append(
                "Review the uncommitted changes in the working directory. "
                "Use `git diff` to see what has changed and provide feedback."
            )
        elif target_type == "baseBranch" and target_value:
            prompt_parts.append(
                f"Review the changes compared to the base branch `{target_value}`. "
                f"Use `git diff {target_value}...HEAD` to see the diff and provide feedback."
            )
        elif target_type == "commit" and target_value:
            prompt_parts.append(
                f"Review the commit `{target_value}`. "
                f"Use `git show {target_value}` to see the changes and provide feedback."
            )
        elif target_type == "custom" and target_value:
            prompt_parts.append(f"Review instructions: {target_value}")

        prompt_parts.append(
            "\n\nProvide actionable feedback focusing on: bugs, security issues, "
            "performance problems, and significant code quality issues. "
            "Include file paths and line numbers where relevant."
        )
        prompt_text = "\n".join(prompt_parts)

        agent = (binding.get("agent") or self.DEFAULT_AGENT).strip().lower()
        if agent not in self.VALID_AGENT_VALUES:
            agent = self.DEFAULT_AGENT
        model_override = binding.get("model_override")
        if not isinstance(model_override, str) or not model_override.strip():
            model_override = None
        reasoning_effort = binding.get("reasoning_effort")
        if not isinstance(reasoning_effort, str) or not reasoning_effort.strip():
            reasoning_effort = None

        session_key = self._build_message_session_key(
            channel_id=channel_id,
            workspace_root=workspace_root,
            pma_enabled=False,
            agent=agent,
        )

        log_event(
            self._logger,
            logging.INFO,
            "discord.review.starting",
            channel_id=channel_id,
            workspace_root=str(workspace_root),
            target_type=target_type,
            target_value=target_value,
            agent=agent,
        )

        try:
            turn_result = await self._run_agent_turn_for_message(
                workspace_root=workspace_root,
                prompt_text=prompt_text,
                agent=agent,
                model_override=model_override,
                reasoning_effort=reasoning_effort,
                session_key=session_key,
                orchestrator_channel_key=channel_id,
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.review.failed",
                channel_id=channel_id,
                workspace_root=str(workspace_root),
                target_type=target_type,
                exc=exc,
            )
            await self._send_channel_message_safe(
                channel_id,
                {"content": f"Review failed: {exc}"},
            )
            return

        if isinstance(turn_result, DiscordMessageTurnResult):
            response_text = turn_result.final_message
            preview_message_id = turn_result.preview_message_id
        else:
            response_text = str(turn_result or "")
            preview_message_id = None

        if not response_text or not response_text.strip():
            response_text = "(Review completed with no output.)"

        chunks = chunk_discord_message(
            response_text,
            max_len=self._config.max_message_length,
            with_numbering=False,
        )
        if not chunks:
            chunks = ["(Review completed with no output.)"]

        if preview_message_id:
            if len(chunks) == 1:
                try:
                    await self._rest.edit_channel_message(
                        channel_id=channel_id,
                        message_id=preview_message_id,
                        payload={"content": chunks[0]},
                    )
                    log_event(
                        self._logger,
                        logging.INFO,
                        "discord.review.completed",
                        channel_id=channel_id,
                        target_type=target_type,
                    )
                    return
                except Exception as exc:
                    log_event(
                        self._logger,
                        logging.WARNING,
                        "discord.review.preview_edit_failed",
                        channel_id=channel_id,
                        message_id=preview_message_id,
                        exc=exc,
                    )
            try:
                await self._rest.delete_channel_message(
                    channel_id=channel_id,
                    message_id=preview_message_id,
                )
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.review.preview_delete_failed",
                    channel_id=channel_id,
                    message_id=preview_message_id,
                    exc=exc,
                )

        for idx, chunk in enumerate(chunks, 1):
            await self._send_channel_message_safe(
                channel_id,
                {"content": chunk},
                record_id=f"review:{session_key}:{idx}:{uuid.uuid4().hex[:8]}",
            )

        log_event(
            self._logger,
            logging.INFO,
            "discord.review.completed",
            channel_id=channel_id,
            target_type=target_type,
            chunk_count=len(chunks),
        )

    async def _handle_car_approvals(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        options: dict[str, Any],
    ) -> None:
        mode = options.get("mode", "")
        if not isinstance(mode, str):
            mode = ""
        mode = mode.strip().lower()

        binding = await self._store.get_binding(channel_id=channel_id)
        if binding is None:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Channel not bound. Use /car bind first.",
            )
            return

        if not mode:
            current_mode = binding.get("approval_mode", "yolo")
            approval_policy = binding.get("approval_policy", "default")
            sandbox_policy = binding.get("sandbox_policy", "default")

            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "\n".join(
                    [
                        f"Approval mode: {current_mode}",
                        f"Approval policy: {approval_policy}",
                        f"Sandbox policy: {sandbox_policy}",
                        "",
                        "Usage: /car approvals yolo|safe|read-only|auto|full-access",
                    ]
                ),
            )
            return

        if mode in ("yolo", "off", "disable"):
            new_mode = "yolo"
        elif mode in ("safe", "on", "enable"):
            new_mode = "safe"
        elif mode in ("read-only", "auto", "full-access"):
            new_mode = mode
        else:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Unknown mode: {mode}. Valid options: yolo, safe, read-only, auto, full-access",
            )
            return

        await self._store.update_approval_mode(channel_id=channel_id, mode=new_mode)
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"Approval mode set to {new_mode}.",
        )

    async def _handle_car_mention(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        options: dict[str, Any],
    ) -> None:
        path_arg = options.get("path", "")
        request_arg = options.get("request", "Please review this file.")

        if not isinstance(path_arg, str) or not path_arg.strip():
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Usage: /car mention path:<file> [request:<text>]",
            )
            return

        path = Path(path_arg.strip())
        if not path.is_absolute():
            path = workspace_root / path

        try:
            path = canonicalize_path(path)
        except Exception:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Could not resolve path: {path_arg}",
            )
            return

        if not path.exists() or not path.is_file():
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"File not found: {path}",
            )
            return

        try:
            path.relative_to(workspace_root)
        except ValueError:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "File must be within the bound workspace.",
            )
            return

        max_bytes = 100000
        try:
            data = path.read_bytes()
        except Exception:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Failed to read file.",
            )
            return

        if len(data) > max_bytes:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"File too large (max {max_bytes} bytes).",
            )
            return

        null_count = data.count(b"\x00")
        if null_count > 0 and null_count > len(data) * 0.1:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "File appears to be binary; refusing to include it.",
            )
            return

        try:
            display_path = str(path.relative_to(workspace_root))
        except ValueError:
            display_path = str(path)

        if not isinstance(request_arg, str) or not request_arg.strip():
            request_arg = "Please review this file."

        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"File `{display_path}` ready for mention.\n\n"
            f"To include it in a request, send a message starting with:\n"
            f"```\n"
            f'<file path="{display_path}">\n'
            f"...\n"
            f"</file>\n"
            f"```\n\n"
            f"Your request: {request_arg}",
        )

    async def _handle_car_experimental(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        options: dict[str, Any],
    ) -> None:
        action = options.get("action", "")
        feature = options.get("feature", "")

        if not isinstance(action, str):
            action = ""
        action = action.strip().lower()

        if not isinstance(feature, str):
            feature = ""
        feature = feature.strip()

        if not action or action in ("list", "ls", "all"):
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Experimental features listing requires the app server client.\n\n"
                "To enable/disable features, use:\n"
                "- `/car experimental list` - list features\n"
                "- `/car experimental enable <feature>` - enable a feature\n"
                "- `/car experimental disable <feature>` - disable a feature",
            )
            return

        if action in ("enable", "on", "true"):
            if not feature:
                await self._respond_ephemeral(
                    interaction_id,
                    interaction_token,
                    "Usage: /car experimental enable <feature>",
                )
                return
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Feature `{feature}` enable requested.\n\n"
                "Note: Feature toggling requires the app server client.",
            )
            return

        if action in ("disable", "off", "false"):
            if not feature:
                await self._respond_ephemeral(
                    interaction_id,
                    interaction_token,
                    "Usage: /car experimental disable <feature>",
                )
                return
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Feature `{feature}` disable requested.\n\n"
                "Note: Feature toggling requires the app server client.",
            )
            return

        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"Unknown action: {action}. Use list, enable, or disable.",
        )

    COMPACT_SUMMARY_PROMPT = (
        "Summarize the conversation so far into a concise context block I can paste into "
        "a new thread. Include goals, constraints, decisions, and current state."
    )

    async def _handle_car_compact(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
    ) -> None:
        binding = await self._store.get_binding(channel_id=channel_id)
        if binding is None:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Channel not bound. Use /car bind first.",
            )
            return

        workspace_raw = binding.get("workspace_path")
        workspace_root: Optional[Path] = None
        if isinstance(workspace_raw, str) and workspace_raw.strip():
            candidate = canonicalize_path(Path(workspace_raw))
            if candidate.exists() and candidate.is_dir():
                workspace_root = candidate

        pma_enabled = bool(binding.get("pma_enabled", False))
        if workspace_root is None and pma_enabled:
            fallback = canonicalize_path(Path(self._config.root))
            if fallback.exists() and fallback.is_dir():
                workspace_root = fallback

        if workspace_root is None:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Binding is invalid. Run /car bind first.",
            )
            return

        agent = (binding.get("agent") or self.DEFAULT_AGENT).strip().lower()
        if agent not in self.VALID_AGENT_VALUES:
            agent = self.DEFAULT_AGENT
        model_override = binding.get("model_override")
        if not isinstance(model_override, str) or not model_override.strip():
            model_override = None
        reasoning_effort = binding.get("reasoning_effort")
        if not isinstance(reasoning_effort, str) or not reasoning_effort.strip():
            reasoning_effort = None

        session_key = self._build_message_session_key(
            channel_id=channel_id,
            workspace_root=workspace_root,
            pma_enabled=pma_enabled,
            agent=agent,
        )
        orchestrator_channel_key = (
            channel_id if not pma_enabled else f"pma:{channel_id}"
        )
        orchestrator = await self._orchestrator_for_workspace(
            workspace_root, channel_id=orchestrator_channel_key
        )

        existing_session = orchestrator.get_thread_id(session_key)
        if not existing_session:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "No active session to compact. Send a message first to start a conversation.",
            )
            return

        await self._defer_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )

        try:
            turn_result = await self._run_agent_turn_for_message(
                workspace_root=workspace_root,
                prompt_text=self.COMPACT_SUMMARY_PROMPT,
                agent=agent,
                model_override=model_override,
                reasoning_effort=reasoning_effort,
                session_key=session_key,
                orchestrator_channel_key=orchestrator_channel_key,
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.compact.turn_failed",
                channel_id=channel_id,
                workspace_root=str(workspace_root),
                exc=exc,
            )
            await self._send_channel_message_safe(
                channel_id,
                {"content": f"Compact failed: {exc}"},
            )
            return

        response_text = (
            turn_result.final_message.strip() if turn_result.final_message else ""
        )
        if not response_text:
            response_text = "(No summary generated.)"

        chunks = chunk_discord_message(
            f"**Conversation Summary:**\n\n{response_text}",
            max_len=self._config.max_message_length,
            with_numbering=True,
        )
        for chunk in chunks:
            await self._send_channel_message_safe(channel_id, {"content": chunk})

    async def _handle_car_rollout(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
    ) -> None:
        binding = await self._store.get_binding(channel_id=channel_id)
        if binding is None:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Channel not bound. Use /car bind first.",
            )
            return

        rollout_path = binding.get("rollout_path")
        workspace_path = binding.get("workspace_path", "unknown")

        if rollout_path:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Rollout path: {rollout_path}\nWorkspace: {workspace_path}",
            )
        else:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"No rollout path available.\nWorkspace: {workspace_path}\n\n"
                "The rollout path is set after a conversation turn completes.",
            )

    async def _handle_car_logout(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
    ) -> None:
        client = await self._client_for_workspace(str(workspace_root))
        if client is None:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                format_discord_message(
                    "Topic not bound. Use `/car bind path:<workspace>` first."
                ),
            )
            return
        try:
            await client.request("account/logout", params=None)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.logout.failed",
                workspace_root=str(workspace_root),
                exc=exc,
            )
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                format_discord_message("Logout failed; check logs for details."),
            )
            return
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            "Logged out.",
        )

    async def _handle_car_feedback(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        options: dict[str, Any],
        channel_id: Optional[str] = None,
    ) -> None:
        reason = options.get("reason", "")
        if not isinstance(reason, str):
            reason = ""
        reason = reason.strip()

        if not reason:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Usage: /car feedback reason:<description>",
            )
            return

        client = await self._client_for_workspace(str(workspace_root))
        if client is None:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                format_discord_message(
                    "Topic not bound. Use `/car bind path:<workspace>` first."
                ),
            )
            return

        params: dict[str, Any] = {
            "classification": "bug",
            "reason": reason,
            "includeLogs": True,
        }
        if channel_id:
            binding = await self._store.get_binding(channel_id=channel_id)
            if binding:
                active_thread_id = binding.get("active_thread_id")
                if isinstance(active_thread_id, str) and active_thread_id:
                    params["threadId"] = active_thread_id

        try:
            result = await client.request("feedback/upload", params)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.feedback.failed",
                workspace_root=str(workspace_root),
                exc=exc,
            )
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                format_discord_message(
                    "Feedback upload failed; check logs for details."
                ),
            )
            return

        report_id = None
        if isinstance(result, dict):
            report_id = result.get("threadId") or result.get("id")
        message_text = "Feedback sent."
        if isinstance(report_id, str):
            message_text = f"Feedback sent (report {report_id})."
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            message_text,
        )

    async def _handle_car_interrupt(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
    ) -> None:
        binding = await self._store.get_binding(channel_id=channel_id)
        if binding is None:
            text = format_discord_message(
                "This channel is not bound. Run `/car bind path:<workspace>` first."
            )
            await self._respond_ephemeral(interaction_id, interaction_token, text)
            return

        pma_enabled = bool(binding.get("pma_enabled", False))
        workspace_raw = binding.get("workspace_path")
        workspace_root: Optional[Path] = None
        if isinstance(workspace_raw, str) and workspace_raw.strip():
            candidate = canonicalize_path(Path(workspace_raw))
            if candidate.exists() and candidate.is_dir():
                workspace_root = candidate

        if workspace_root is None and pma_enabled:
            fallback = canonicalize_path(Path(self._config.root))
            if fallback.exists() and fallback.is_dir():
                workspace_root = fallback

        if workspace_root is None:
            text = format_discord_message(
                "Binding is invalid. Run `/car bind path:<workspace>` first."
            )
            await self._respond_ephemeral(interaction_id, interaction_token, text)
            return

        agent = (binding.get("agent") or self.DEFAULT_AGENT).strip().lower()
        if agent not in self.VALID_AGENT_VALUES:
            agent = self.DEFAULT_AGENT

        orchestrator_channel_key = (
            channel_id if not pma_enabled else f"pma:{channel_id}"
        )
        orchestrator = await self._orchestrator_for_workspace(
            workspace_root, channel_id=orchestrator_channel_key
        )

        context = orchestrator.get_context()
        if context is None or context.session_id is None:
            text = format_discord_message("No active turn to interrupt.")
            await self._respond_ephemeral(interaction_id, interaction_token, text)
            return

        state = self._build_runner_state(
            agent=agent,
            model_override=None,
            reasoning_effort=None,
        )

        try:
            await orchestrator.interrupt(agent, state)
            text = format_discord_message("Stopping current turn...")
            await self._respond_ephemeral(interaction_id, interaction_token, text)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.interrupt.failed",
                channel_id=channel_id,
                workspace_root=str(workspace_root),
                agent=agent,
                exc=exc,
            )
            text = format_discord_message("Interrupt failed. Please try again.")
            await self._respond_ephemeral(interaction_id, interaction_token, text)

    async def _handle_cancel_turn_button(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
    ) -> None:
        await self._handle_car_interrupt(
            interaction_id,
            interaction_token,
            channel_id=channel_id,
        )


def create_discord_bot_service(
    config: DiscordBotConfig,
    *,
    logger: logging.Logger,
    manifest_path: Optional[Path] = None,
    update_repo_url: Optional[str] = None,
    update_repo_ref: Optional[str] = None,
    update_skip_checks: bool = False,
    update_backend: str = "auto",
    update_linux_service_names: Optional[dict[str, str]] = None,
) -> DiscordBotService:
    return DiscordBotService(
        config,
        logger=logger,
        manifest_path=manifest_path,
        update_repo_url=update_repo_url,
        update_repo_ref=update_repo_ref,
        update_skip_checks=update_skip_checks,
        update_backend=update_backend,
        update_linux_service_names=update_linux_service_names,
    )
