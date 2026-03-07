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

from ...agents.opencode.harness import OpenCodeHarness
from ...core.config import load_repo_config, resolve_env_for_root
from ...core.context_awareness import (
    maybe_inject_car_awareness,
    maybe_inject_prompt_writing_hint,
)
from ...core.filebox import (
    inbox_dir,
    outbox_dir,
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
from ...core.flows.hub_overview import build_hub_flow_overview_entries
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
from ...core.flows.worker_process import check_worker_health, clear_worker_metadata
from ...core.git_utils import GitError, reset_branch_from_origin_main
from ...core.injected_context import wrap_injected_context
from ...core.logging_utils import log_event
from ...core.pma_context import build_hub_snapshot, format_pma_prompt, load_pma_prompt
from ...core.ports.run_event import (
    RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE,
    RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
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
    is_within,
)
from ...flows.ticket_flow.runtime_helpers import build_ticket_flow_controller
from ...integrations.agents.backend_orchestrator import BackendOrchestrator
from ...integrations.agents.opencode_supervisor_factory import (
    build_opencode_supervisor_from_repo_config,
)
from ...integrations.app_server.client import (
    CodexAppServerClient,
    CodexAppServerResponseError,
)
from ...integrations.app_server.env import app_server_env, build_app_server_env
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
from ...integrations.chat.media import (
    audio_content_type_for_input,
    audio_extension_for_input,
    is_audio_mime_or_path,
    is_image_mime_or_path,
    normalize_mime_type,
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
from ...tickets.files import read_ticket
from ...tickets.outbox import resolve_outbox_paths
from ...voice import VoiceConfig, VoiceService, VoiceServiceError
from ..telegram.helpers import (
    _coerce_thread_list,
    _extract_context_usage_percent,
    _extract_thread_list_cursor,
    _extract_thread_preview_parts,
    _format_turn_metrics,
    _parse_review_commit_log,
)
from ..telegram.progress_stream import TurnProgressTracker, render_progress_text
from .adapter import DiscordChatAdapter
from .allowlist import DiscordAllowlist, allowlist_allows
from .command_registry import sync_commands
from .commands import build_application_commands
from .components import (
    DISCORD_SELECT_OPTION_MAX_OPTIONS,
    build_action_row,
    build_agent_picker,
    build_bind_picker,
    build_button,
    build_cancel_turn_button,
    build_continue_turn_button,
    build_flow_runs_picker,
    build_flow_status_buttons,
    build_model_effort_picker,
    build_model_picker,
    build_review_commit_picker,
    build_session_threads_picker,
    build_update_target_picker,
)
from .config import DiscordBotConfig
from .errors import DiscordAPIError, DiscordTransientError
from .gateway import DiscordGatewayClient
from .interactions import (
    extract_autocomplete_command_context,
    extract_channel_id,
    extract_command_path_and_options,
    extract_component_custom_id,
    extract_component_values,
    extract_guild_id,
    extract_interaction_id,
    extract_interaction_token,
    extract_user_id,
    is_autocomplete_interaction,
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
DISCORD_TURN_PROGRESS_MAX_ACTIONS = 12
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
_MODEL_LIST_INVALID_PARAMS_ERROR_CODES = {-32600, -32602}
SESSION_RESUME_SELECT_ID = "session_resume_select"
FLOW_ACTION_SELECT_PREFIX = "flow_action_select"
UPDATE_TARGET_SELECT_ID = "update_target_select"
REVIEW_COMMIT_SELECT_ID = "review_commit_select"
MODEL_EFFORT_SELECT_ID = "model_effort_select"
BIND_PAGE_CUSTOM_ID_PREFIX = "bind_page"
REPO_AUTOCOMPLETE_TOKEN_PREFIX = "repo@"
WORKSPACE_AUTOCOMPLETE_TOKEN_PREFIX = "workspace@"
FLOW_ACTIONS_WITH_RUN_PICKER = {
    "status",
    "restart",
    "resume",
    "stop",
    "archive",
    "recover",
    "reply",
}


class AppServerUnavailableError(Exception):
    pass


def _normalize_model_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _display_name_is_model_alias(model: str, display_name: Any) -> bool:
    if not isinstance(display_name, str) or not display_name:
        return False
    return _normalize_model_name(display_name) == _normalize_model_name(model)


def _coerce_model_entries(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, list):
        return [entry for entry in result if isinstance(entry, dict)]
    if isinstance(result, dict):
        for key in ("data", "models", "items", "results"):
            value = result.get(key)
            if isinstance(value, list):
                return [entry for entry in value if isinstance(entry, dict)]
    return []


def _coerce_model_picker_items(result: Any) -> list[tuple[str, str]]:
    entries = _coerce_model_entries(result)
    options: list[tuple[str, str]] = []
    seen: set[str] = set()
    limit = max(1, DISCORD_SELECT_OPTION_MAX_OPTIONS - 1)
    for entry in entries:
        model_id = entry.get("model") or entry.get("id")
        if not isinstance(model_id, str):
            continue
        model_id = model_id.strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        display_name = entry.get("displayName")
        label = model_id
        if (
            isinstance(display_name, str)
            and display_name
            and not _display_name_is_model_alias(model_id, display_name)
        ):
            label = f"{model_id} ({display_name})"
        options.append((model_id, label))
        if len(options) >= limit:
            break
    return options


def _is_valid_opencode_model_name(model_name: str) -> bool:
    if "/" not in model_name:
        return False
    provider_id, model_id = model_name.split("/", 1)
    return bool(provider_id.strip() and model_id.strip())


def _path_within(root: Path, target: Path) -> bool:
    try:
        root = canonicalize_path(root)
        target = canonicalize_path(target)
    except Exception:
        return False
    return is_within(root, target)


async def _model_list_with_agent_compat(
    client: CodexAppServerClient,
    *,
    params: dict[str, Any],
) -> Any:
    request_params = {key: value for key, value in params.items() if value is not None}
    requested_agent = request_params.get("agent")
    if not isinstance(requested_agent, str) or not requested_agent:
        requested_agent = None
        request_params.pop("agent", None)
    try:
        return await client.model_list(**request_params)
    except CodexAppServerResponseError as exc:
        if (
            requested_agent is None
            or exc.code not in _MODEL_LIST_INVALID_PARAMS_ERROR_CODES
        ):
            raise
        fallback_params = dict(request_params)
        fallback_params.pop("agent", None)
        return await client.model_list(**fallback_params)


def _flow_action_label(action: str) -> str:
    labels = {
        "status": "status",
        "restart": "restart",
        "resume": "resume",
        "stop": "stop",
        "archive": "archive",
        "recover": "recover",
        "reply": "reply",
    }
    return labels.get(action, action)


def _flow_run_matches_action(record: FlowRunRecord, action: str) -> bool:
    if action == "resume":
        return record.status == FlowRunStatus.PAUSED
    if action == "reply":
        return record.status == FlowRunStatus.PAUSED
    if action == "stop":
        return not record.status.is_terminal()
    if action == "recover":
        return not record.status.is_terminal()
    return True


def _truncate_picker_text(text: str, *, limit: int) -> str:
    value = " ".join(text.split()).strip()
    if len(value) <= limit:
        return value
    if limit <= 3:
        return value[:limit]
    return f"{value[: limit - 3]}..."


def _format_session_thread_picker_label(
    thread_id: str, entry: dict[str, Any], *, is_current: bool
) -> str:
    max_label_len = 100
    current_suffix = " (current)" if is_current else ""
    max_base_len = max_label_len - len(current_suffix)
    user_preview, assistant_preview = _extract_thread_preview_parts(entry)
    user_preview = _truncate_picker_text(user_preview or "", limit=72) or None
    assistant_preview = _truncate_picker_text(assistant_preview or "", limit=72) or None
    preview_label: Optional[str] = None
    if user_preview and assistant_preview:
        preview_label = f"U: {user_preview} | A: {assistant_preview}"
    elif user_preview:
        preview_label = f"U: {user_preview}"
    elif assistant_preview:
        preview_label = f"A: {assistant_preview}"
    if preview_label:
        short_id = thread_id[:8]
        id_prefix = f"[{short_id}] "
        preview_budget = max(1, max_base_len - len(id_prefix))
        base = (
            f"{id_prefix}{_truncate_picker_text(preview_label, limit=preview_budget)}"
        )
    else:
        base = _truncate_picker_text(thread_id, limit=max_base_len)
    return f"{base}{current_suffix}"


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
    is_audio: bool
    is_image: bool
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
        self._opencode_supervisors: dict[str, Any] = {}
        self._opencode_lock = asyncio.Lock()
        self._app_server_state_root = resolve_global_state_root() / "workspaces"
        self._channel_directory_store = ChannelDirectoryStore(self._config.root)
        self._guild_name_cache: dict[str, str] = {}
        self._channel_name_cache: dict[str, str] = {}
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
        self._pending_model_effort: dict[str, str] = {}
        self._pending_flow_reply_text: dict[str, str] = {}
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
            with contextlib.suppress(Exception):
                await self._dispatcher.close()
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
                user_id=event.from_user_id,
            )
            return

        if payload_data.get("type") == "autocomplete":
            command_raw = payload_data.get("command")
            command_path = (
                tuple(part for part in str(command_raw).split(":") if part)
                if isinstance(command_raw, str)
                else ()
            )
            autocomplete_payload = payload_data.get("autocomplete")
            focused_name: Optional[str] = None
            focused_value = ""
            if isinstance(autocomplete_payload, dict):
                focused_name_raw = autocomplete_payload.get("name")
                focused_value_raw = autocomplete_payload.get("value")
                if isinstance(focused_name_raw, str) and focused_name_raw.strip():
                    focused_name = focused_name_raw.strip()
                if isinstance(focused_value_raw, str):
                    focused_value = focused_value_raw
            options = (
                payload_data.get("options")
                if isinstance(payload_data.get("options"), dict)
                else {}
            )
            await self._handle_command_autocomplete(
                interaction_id,
                interaction_token,
                command_path=command_path,
                options=options,
                focused_name=focused_name,
                focused_value=focused_value,
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
                if self._is_user_ticket_pause(workspace_root, paused):
                    log_event(
                        self._logger,
                        logging.INFO,
                        "discord.flow.reply.skipped_for_user_ticket_pause",
                        channel_id=channel_id,
                        run_id=paused.id,
                    )
                else:
                    reply_text = text
                    if has_attachments:
                        (
                            reply_text,
                            saved_attachments,
                            failed_attachments,
                            transcript_message,
                            _native_input_items,
                        ) = await self._with_attachment_context(
                            prompt_text=text,
                            workspace_root=workspace_root,
                            attachments=event.attachments,
                            channel_id=channel_id,
                        )
                        if transcript_message:
                            await self._send_channel_message_safe(
                                channel_id,
                                {
                                    "content": transcript_message,
                                    "allowed_mentions": {"parse": []},
                                },
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

                    reply_path = self._write_user_reply(
                        workspace_root, paused, reply_text
                    )
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
            transcript_message,
            attachment_input_items,
        ) = await self._with_attachment_context(
            prompt_text=prompt_text,
            workspace_root=workspace_root,
            attachments=event.attachments,
            channel_id=channel_id,
        )
        if transcript_message:
            await self._send_channel_message_safe(
                channel_id,
                {
                    "content": transcript_message,
                    "allowed_mentions": {"parse": []},
                },
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

        if not pma_enabled:
            prompt_text, injected = maybe_inject_car_awareness(prompt_text)
            if injected:
                log_event(
                    self._logger,
                    logging.INFO,
                    "discord.car_context.injected",
                    channel_id=channel_id,
                    message_id=event.message.message_id,
                )
            prompt_text, injected = maybe_inject_prompt_writing_hint(prompt_text)
            if injected:
                log_event(
                    self._logger,
                    logging.INFO,
                    "discord.prompt_context.injected",
                    channel_id=channel_id,
                    message_id=event.message.message_id,
                )

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
            link_source_text=text,
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
        turn_input_items: Optional[list[dict[str, Any]]] = None
        if attachment_input_items:
            turn_input_items = [
                {"type": "text", "text": prompt_text},
                *attachment_input_items,
            ]
        run_turn_kwargs: dict[str, Any] = {
            "workspace_root": workspace_root,
            "prompt_text": prompt_text,
            "agent": agent,
            "model_override": model_override,
            "reasoning_effort": reasoning_effort,
            "session_key": session_key,
            "orchestrator_channel_key": (
                channel_id if not pma_enabled else f"pma:{channel_id}"
            ),
        }
        if turn_input_items:
            run_turn_kwargs["input_items"] = turn_input_items
        try:
            turn_result = await self._run_agent_turn_for_message(**run_turn_kwargs)
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
                    response_text = f"(No response text returned.)\n\n{metrics_text}"
        else:
            response_text = str(turn_result or "")
            preview_message_id = None

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
        if isinstance(preview_message_id, str) and preview_message_id:
            await self._delete_channel_message_safe(
                channel_id=channel_id,
                message_id=preview_message_id,
                record_id=f"turn:delete_progress:{session_key}:{uuid.uuid4().hex[:8]}",
            )
        await self._flush_outbox_files(
            workspace_root=workspace_root,
            channel_id=channel_id,
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
        file_name = getattr(attachment, "file_name", None)
        source_url = getattr(attachment, "source_url", None)
        return is_audio_mime_or_path(
            mime_type=mime_type,
            file_name=file_name if isinstance(file_name, str) else None,
            source_url=source_url if isinstance(source_url, str) else None,
            kind=kind if isinstance(kind, str) else None,
        )

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
            source_url = getattr(attachment, "source_url", None)
            content_type = audio_content_type_for_input(
                mime_type=mime_type,
                file_name=file_name,
                source_url=source_url if isinstance(source_url, str) else None,
            )
            result = await voice_service.transcribe_async(
                data,
                client="discord",
                filename=file_name,
                content_type=content_type,
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
    ) -> tuple[str, int, int, Optional[str], Optional[list[dict[str, Any]]]]:
        if not attachments:
            return prompt_text, 0, 0, None, None

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
                transcription_name = str(original_name)
                if not Path(transcription_name).suffix:
                    transcription_name = path.name
                mime_type = getattr(attachment, "mime_type", None)
                is_audio = self._is_audio_attachment(
                    attachment, mime_type if isinstance(mime_type, str) else None
                )
                is_image = is_image_mime_or_path(
                    mime_type if isinstance(mime_type, str) else None,
                    str(original_name),
                )
                transcript_text, transcript_warning = (
                    await self._transcribe_voice_attachment(
                        workspace_root=workspace_root,
                        channel_id=channel_id,
                        attachment=attachment,
                        data=data,
                        file_name=transcription_name,
                        mime_type=mime_type if isinstance(mime_type, str) else None,
                    )
                )
                saved.append(
                    _SavedDiscordAttachment(
                        original_name=str(original_name),
                        path=path,
                        mime_type=mime_type if isinstance(mime_type, str) else None,
                        size_bytes=len(data),
                        is_audio=is_audio,
                        is_image=is_image,
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
            return prompt_text, 0, failed, None, None

        transcript_lines: list[str] = []
        transcript_items = [item for item in saved if item.transcript_text]
        if len(transcript_items) == 1:
            transcript_lines = ["User:", transcript_items[0].transcript_text or ""]
        elif transcript_items:
            transcript_lines = ["User:"]
            for item in transcript_items:
                transcript_lines.append(f"[{item.original_name}]")
                transcript_lines.append(item.transcript_text or "")
                transcript_lines.append("")
            while transcript_lines and not transcript_lines[-1].strip():
                transcript_lines.pop()
        user_visible_transcript = None
        if transcript_lines:
            transcript_text = "\n".join(transcript_lines)
            max_len = max(int(self._config.max_message_length), 32)
            user_visible_transcript = truncate_for_discord(
                format_discord_message(transcript_text),
                max_len=max_len,
            )

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

        if any(not item.is_audio for item in saved):
            details.append("")
            details.append(
                wrap_injected_context(
                    "\n".join(
                        [
                            f"Inbox: {inbox}",
                            f"Outbox: {outbox_dir(workspace_root)}",
                            f"Outbox (pending): {outbox_pending_dir(workspace_root)}",
                            "Use inbox files as local inputs and place reply files in outbox.",
                        ]
                    )
                )
            )
        attachment_context = "\n".join(details)
        native_input_items = [
            {"type": "localImage", "path": str(item.path)}
            for item in saved
            if item.is_image
        ]
        native_input_items_payload: Optional[list[dict[str, Any]]] = (
            native_input_items if native_input_items else None
        )

        if prompt_text.strip():
            separator = "\n" if prompt_text.endswith("\n") else "\n\n"
            return (
                f"{prompt_text}{separator}{attachment_context}",
                len(saved),
                failed,
                user_visible_transcript,
                native_input_items_payload,
            )
        return (
            attachment_context,
            len(saved),
            failed,
            user_visible_transcript,
            native_input_items_payload,
        )

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
                mime_key = normalize_mime_type(mime_type) or ""
                suffix = {
                    "image/png": ".png",
                    "image/jpeg": ".jpg",
                    "image/jpg": ".jpg",
                    "image/gif": ".gif",
                    "image/webp": ".webp",
                    "application/pdf": ".pdf",
                    "text/plain": ".txt",
                }.get(mime_key, "")
        if not suffix:
            source_url = getattr(attachment, "source_url", None)
            is_audio = is_audio_mime_or_path(
                mime_type=getattr(attachment, "mime_type", None),
                file_name=getattr(attachment, "file_name", None),
                source_url=source_url if isinstance(source_url, str) else None,
                kind=getattr(attachment, "kind", None),
            )
            if is_audio:
                suffix = audio_extension_for_input(
                    mime_type=getattr(attachment, "mime_type", None),
                    file_name=getattr(attachment, "file_name", None),
                    source_url=source_url if isinstance(source_url, str) else None,
                    default=".ogg",
                )
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

    def _is_user_ticket_pause(
        self, workspace_root: Path, record: FlowRunRecord
    ) -> bool:
        state = getattr(record, "state", None)
        if not isinstance(state, dict):
            return False
        engine = state.get("ticket_engine")
        if not isinstance(engine, dict):
            return False
        if str(engine.get("reason_code") or "").strip().lower() != "user_pause":
            return False
        current_ticket = engine.get("current_ticket")
        if not isinstance(current_ticket, str) or not current_ticket.strip():
            return False
        ticket_path = (workspace_root / current_ticket).resolve()
        if not ticket_path.is_file() or not is_within(workspace_root, ticket_path):
            return False
        ticket_doc, errors = read_ticket(ticket_path)
        if errors or ticket_doc is None:
            return False
        return ticket_doc.frontmatter.agent == "user" and not bool(
            ticket_doc.frontmatter.done
        )

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

    async def _opencode_supervisor_for_workspace(
        self, workspace_root: Path
    ) -> Optional[Any]:
        key = str(workspace_root)
        async with self._opencode_lock:
            existing = self._opencode_supervisors.get(key)
            if existing is not None:
                return existing
            repo_config = load_repo_config(
                workspace_root,
                hub_path=self._hub_config_path,
            )
            supervisor = build_opencode_supervisor_from_repo_config(
                repo_config,
                workspace_root=workspace_root,
                logger=self._logger,
                base_env=None,
            )
            if supervisor is None:
                return None
            self._opencode_supervisors[key] = supervisor
            return supervisor

    async def _list_opencode_models_for_picker(
        self,
        *,
        workspace_path: Optional[str],
    ) -> Optional[list[tuple[str, str]]]:
        if not isinstance(workspace_path, str) or not workspace_path.strip():
            return None
        try:
            workspace_root = canonicalize_path(Path(workspace_path))
        except Exception:
            return None
        if not workspace_root.exists() or not workspace_root.is_dir():
            return None
        supervisor = await self._opencode_supervisor_for_workspace(workspace_root)
        if supervisor is None:
            raise RuntimeError("OpenCode backend unavailable for this workspace")
        harness = OpenCodeHarness(supervisor)
        catalog = await harness.model_catalog(workspace_root)
        options: list[tuple[str, str]] = []
        seen: set[str] = set()
        for model in catalog.models:
            model_id = model.id.strip() if isinstance(model.id, str) else ""
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            label = model_id
            if (
                isinstance(model.display_name, str)
                and model.display_name
                and not _display_name_is_model_alias(model_id, model.display_name)
            ):
                label = f"{model_id} ({model.display_name})"
            options.append((model_id, label))
        return options

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

    async def _list_session_threads_for_picker(
        self,
        *,
        workspace_root: Path,
        current_thread_id: Optional[str],
    ) -> list[tuple[str, str]]:
        try:
            client = await self._client_for_workspace(str(workspace_root))
        except AppServerUnavailableError:
            return []
        if client is None:
            return []
        try:
            entries, _found = await self._list_threads_paginated(
                client,
                limit=DISCORD_SELECT_OPTION_MAX_OPTIONS,
                max_pages=3,
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.session.threads_picker.failed",
                workspace_root=str(workspace_root),
                exc=exc,
            )
            return []

        items: list[tuple[str, str]] = []
        seen_ids: set[str] = set()
        for entry in entries:
            thread_id = entry.get("id")
            if not isinstance(thread_id, str) or not thread_id:
                continue
            if thread_id in seen_ids:
                continue
            seen_ids.add(thread_id)
            label = _format_session_thread_picker_label(
                thread_id,
                entry,
                is_current=thread_id == current_thread_id,
            )
            items.append((thread_id, label))
            if len(items) >= DISCORD_SELECT_OPTION_MAX_OPTIONS:
                break
        if (
            isinstance(current_thread_id, str)
            and current_thread_id
            and current_thread_id not in seen_ids
        ):
            if len(items) >= DISCORD_SELECT_OPTION_MAX_OPTIONS:
                items.pop()
            items.append(
                (
                    current_thread_id,
                    _format_session_thread_picker_label(
                        current_thread_id, {"id": current_thread_id}, is_current=True
                    ),
                )
            )
        return items

    async def _list_recent_commits_for_picker(
        self,
        workspace_root: Path,
        *,
        limit: int = DISCORD_SELECT_OPTION_MAX_OPTIONS,
    ) -> list[tuple[str, str]]:
        cmd = [
            "git",
            "-C",
            str(workspace_root),
            "log",
            f"-n{max(1, limit)}",
            "--pretty=format:%H%x1f%s%x1e",
        ]
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                cmd,
                text=True,
                capture_output=True,
                check=False,
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.review.commit_list.failed",
                workspace_root=str(workspace_root),
                exc=exc,
            )
            return []
        stdout = result.stdout if isinstance(result.stdout, str) else ""
        if result.returncode not in (0, None) and not stdout.strip():
            return []
        return _parse_review_commit_log(stdout)[:limit]

    async def _prompt_flow_action_picker(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        action: str,
    ) -> None:
        try:
            store = self._open_flow_store(workspace_root)
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            raise DiscordTransientError(
                f"Failed to open flow database: {exc}",
                user_message="Unable to access flow database. Please try again later.",
            ) from None
        try:
            runs = store.list_flow_runs(flow_type="ticket_flow")
        except (sqlite3.Error, OSError) as exc:
            raise DiscordTransientError(
                f"Failed to query flow runs: {exc}",
                user_message="Unable to query flow database. Please try again later.",
            ) from None
        finally:
            store.close()

        filtered = [run for run in runs if _flow_run_matches_action(run, action)]
        if not filtered:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"No ticket_flow runs available for {_flow_action_label(action)}.",
            )
            return
        run_tuples = [(record.id, record.status.value) for record in filtered]
        custom_id = f"{FLOW_ACTION_SELECT_PREFIX}:{action}"
        prompt = f"Select a run to {_flow_action_label(action)}:"
        await self._respond_with_components(
            interaction_id,
            interaction_token,
            prompt,
            [
                build_flow_runs_picker(
                    run_tuples,
                    custom_id=custom_id,
                    placeholder=f"Select run to {_flow_action_label(action)}...",
                )
            ],
        )

    async def _run_agent_turn_for_message(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        input_items: Optional[list[dict[str, Any]]] = None,
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
        max_progress_len = max(int(self._config.max_message_length), 32)
        tracker = TurnProgressTracker(
            started_at=time.monotonic(),
            agent=agent,
            model=model_override or "default",
            label="working",
            max_actions=DISCORD_TURN_PROGRESS_MAX_ACTIONS,
            max_output_chars=max_progress_len,
        )
        progress_message_id: Optional[str] = None
        progress_rendered: Optional[str] = None
        progress_last_updated = 0.0
        progress_failure_count = 0
        progress_heartbeat_task: Optional[asyncio.Task[None]] = None
        active_progress_labels = {"working", "queued", "running", "review"}

        async def _edit_progress(
            *,
            force: bool = False,
            remove_components: bool = False,
            render_mode: str = "live",
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
                tracker,
                max_length=max_progress_len,
                now=now,
                render_mode=render_mode,
            )
            content = truncate_for_discord(rendered, max_len=max_progress_len)
            if not force and content == progress_rendered:
                return
            payload: dict[str, Any] = {"content": content}
            if remove_components:
                payload["components"] = []
            elif tracker.label in active_progress_labels:
                payload["components"] = [build_cancel_turn_button()]
            else:
                payload["components"] = []
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
        assistant_stream_fallback = ""
        completed_seen = False
        token_usage: Optional[dict[str, Any]] = None
        error_message = None
        session_from_events = known_session

        def _merge_assistant_stream(current: str, incoming: str) -> str:
            if not incoming:
                return current
            if not current:
                return incoming
            if len(incoming) > len(current) and incoming.startswith(current):
                return incoming
            # Collapse only partial overlap between current suffix and incoming prefix.
            # Full-length overlap is left intact to avoid dropping legitimate repeats.
            max_overlap = min(len(current), max(len(incoming) - 1, 0))
            for overlap in range(max_overlap, 0, -1):
                if current[-overlap:] == incoming[:overlap]:
                    return f"{current}{incoming[overlap:]}"
            return f"{current}{incoming}"

        try:
            async for run_event in orchestrator.run_turn(
                agent_id=agent,
                state=state,
                prompt=prompt_text,
                input_items=input_items,
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
                    if (
                        run_event.delta_type
                        in {
                            RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
                            RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE,
                        }
                        and isinstance(run_event.content, str)
                        and run_event.content
                    ):
                        assistant_stream_fallback = _merge_assistant_stream(
                            assistant_stream_fallback, run_event.content
                        )
                    if isinstance(run_event.content, str) and run_event.content.strip():
                        if (
                            run_event.delta_type
                            == RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE
                        ):
                            latest_output = tracker.latest_output_text().strip()
                            incoming_output = run_event.content.strip()
                            if latest_output and (
                                incoming_output == latest_output
                                or incoming_output.startswith(latest_output)
                            ):
                                tracker.note_output(run_event.content)
                            else:
                                tracker.note_output(
                                    run_event.content,
                                    new_segment=True,
                                )
                            tracker.end_output_segment()
                        else:
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
                    if final_message.strip():
                        tracker.drop_terminal_output_if_duplicate(final_message)
                    completed_seen = True
                    tracker.clear_transient_action()
                    tracker.set_label("done")
                    await _edit_progress(
                        force=True,
                        remove_components=True,
                        render_mode="final",
                    )
                elif isinstance(run_event, Failed):
                    failed_message = run_event.error_message or "Turn failed"
                    if completed_seen:
                        log_event(
                            self._logger,
                            logging.WARNING,
                            "discord.turn.failed_late_ignored",
                            channel_id=progress_channel_id,
                            session_key=session_key,
                            error_message=failed_message,
                            final_message_length=len(final_message),
                            fallback_stream_length=len(assistant_stream_fallback),
                        )
                        tracker.clear_transient_action()
                        tracker.set_label("done")
                        await _edit_progress(
                            force=True,
                            remove_components=True,
                            render_mode="final",
                        )
                        continue
                    error_message = failed_message
                    tracker.note_error(error_message)
                    tracker.clear_transient_action()
                    tracker.set_label("failed")
                    await _edit_progress(force=True, remove_components=True)
        except Exception as exc:
            error_message = str(exc) or "Turn failed"
            tracker.note_error(error_message)
            tracker.clear_transient_action()
            tracker.set_label("failed")
            await _edit_progress(force=True, remove_components=True)
            raise
        finally:
            if progress_heartbeat_task is not None:
                progress_heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await progress_heartbeat_task
        if not error_message and not completed_seen:
            tracker.clear_transient_action()
            tracker.set_label("done")
            await _edit_progress(
                force=True,
                remove_components=True,
                render_mode="final",
            )
        if session_from_events:
            orchestrator.set_thread_id(session_key, session_from_events)
        if error_message:
            raise RuntimeError(error_message)
        if not final_message.strip() and assistant_stream_fallback.strip():
            final_message = assistant_stream_fallback
            log_event(
                self._logger,
                logging.INFO,
                "discord.turn.final_message.fallback_stream",
                channel_id=progress_channel_id,
                session_key=session_key,
                fallback_length=len(final_message),
            )
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
        shell_command = ["bash", "-lc", command_text]
        shell_env = app_server_env(shell_command, workspace_root)
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                shell_command,
                cwd=workspace_root,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                env=shell_env,
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
                user_id=user_id,
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
            if command_path == ("car", "flow", "start"):
                await self._handle_flow_start(
                    interaction_id,
                    interaction_token,
                    workspace_root=workspace_root,
                    options=options,
                )
                return
            if command_path == ("car", "flow", "restart"):
                await self._handle_flow_restart(
                    interaction_id,
                    interaction_token,
                    workspace_root=workspace_root,
                    options=options,
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
                    user_id=user_id,
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
        async with self._opencode_lock:
            opencode_supervisors = list(self._opencode_supervisors.values())
            self._opencode_supervisors.clear()
        for supervisor in opencode_supervisors:
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
            await self._record_channel_directory_seen_from_message_payload(payload)
            event = self._chat_adapter.parse_message_event(payload)
            if event is not None:
                await self._dispatch_chat_event(event)

    async def _record_channel_directory_seen_from_message_payload(
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
        if channel_label_raw is not None:
            channel_label_raw = channel_label_raw.lstrip("#")
            self._channel_name_cache[channel_id] = channel_label_raw
        else:
            if channel_id in self._channel_name_cache:
                cached_channel = self._channel_name_cache[channel_id]
                channel_label_raw = cached_channel if cached_channel else None
            else:
                channel_label_raw = await self._resolve_channel_name(channel_id)

        if guild_id is not None:
            if guild_label is not None:
                self._guild_name_cache[guild_id] = guild_label
            else:
                if guild_id in self._guild_name_cache:
                    cached_guild = self._guild_name_cache[guild_id]
                    guild_label = cached_guild if cached_guild else None
                else:
                    guild_label = await self._resolve_guild_name(guild_id)

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

    async def _resolve_channel_name(self, channel_id: str) -> Optional[str]:
        fetch = getattr(self._rest, "get_channel", None)
        if not callable(fetch):
            self._channel_name_cache[channel_id] = ""
            return None
        try:
            payload = await fetch(channel_id=channel_id)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.channel_directory.channel_lookup_failed",
                channel_id=channel_id,
                exc=exc,
            )
            self._channel_name_cache[channel_id] = ""
            return None
        if not isinstance(payload, dict):
            self._channel_name_cache[channel_id] = ""
            return None
        channel_label = self._first_non_empty_text(payload.get("name"))
        if channel_label is None:
            self._channel_name_cache[channel_id] = ""
            return None
        normalized = channel_label.lstrip("#")
        self._channel_name_cache[channel_id] = normalized

        return normalized

    async def _resolve_guild_name(self, guild_id: str) -> Optional[str]:
        fetch = getattr(self._rest, "get_guild", None)
        if not callable(fetch):
            self._guild_name_cache[guild_id] = ""
            return None
        try:
            payload = await fetch(guild_id=guild_id)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.channel_directory.guild_lookup_failed",
                guild_id=guild_id,
                exc=exc,
            )
            self._guild_name_cache[guild_id] = ""
            return None
        if not isinstance(payload, dict):
            self._guild_name_cache[guild_id] = ""
            return None
        guild_label = self._first_non_empty_text(payload.get("name"))
        if guild_label is None:
            self._guild_name_cache[guild_id] = ""
            return None
        self._guild_name_cache[guild_id] = guild_label
        return guild_label

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
        if is_autocomplete_interaction(interaction_payload):
            await self._handle_autocomplete_interaction(interaction_payload)
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

    async def _handle_autocomplete_interaction(
        self, interaction_payload: dict[str, Any]
    ) -> None:
        interaction_id = extract_interaction_id(interaction_payload)
        interaction_token = extract_interaction_token(interaction_payload)
        channel_id = extract_channel_id(interaction_payload)

        if not interaction_id or not interaction_token or not channel_id:
            self._logger.warning(
                "handle_autocomplete_interaction: missing required fields (interaction_id=%s, token=%s, channel=%s)",
                bool(interaction_id),
                bool(interaction_token),
                bool(channel_id),
            )
            return

        if not allowlist_allows(interaction_payload, self._allowlist):
            await self._respond_autocomplete(
                interaction_id, interaction_token, choices=[]
            )
            return

        (
            command_path,
            options,
            focused_name,
            focused_value,
        ) = extract_autocomplete_command_context(interaction_payload)
        await self._handle_command_autocomplete(
            interaction_id,
            interaction_token,
            command_path=command_path,
            options=options,
            focused_name=focused_name,
            focused_value=focused_value,
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

        prompt, components = self._build_bind_page_prompt_and_components(repos, page=0)
        await self._respond_with_components(
            interaction_id,
            interaction_token,
            prompt,
            components,
        )

    def _list_manifest_repos(self) -> list[tuple[str, str]]:
        if not self._manifest_path or not self._manifest_path.exists():
            return []
        try:
            manifest = load_manifest(self._manifest_path, self._config.root)
            ordered: list[tuple[int, int, str, str]] = []
            for index, repo in enumerate(manifest.repos):
                if not repo.id:
                    continue
                worktree_priority = 0 if repo.kind == "worktree" else 1
                ordered.append(
                    (
                        worktree_priority,
                        -index,
                        repo.id,
                        str(self._config.root / repo.path),
                    )
                )
            ordered.sort(key=lambda item: (item[0], item[1], item[2]))
            return [(repo_id, path) for _, _, repo_id, path in ordered]
        except Exception:
            return []

    def _list_bind_workspace_candidates(self) -> list[tuple[Optional[str], str]]:
        candidates: list[tuple[Optional[str], str]] = []
        manifest_paths: set[str] = set()

        for repo_id, path in self._list_manifest_repos():
            normalized_path = str(canonicalize_path(Path(path)))
            candidates.append((repo_id, normalized_path))
            manifest_paths.add(normalized_path)

        seen_paths: set[str] = set(manifest_paths)
        try:
            for child in sorted(
                self._config.root.iterdir(),
                key=lambda entry: entry.name.lower(),
            ):
                if not child.is_dir():
                    continue
                if child.name.startswith("."):
                    continue
                normalized_path = str(canonicalize_path(child))
                if normalized_path in seen_paths:
                    continue
                seen_paths.add(normalized_path)
                candidates.append((None, normalized_path))
        except Exception:
            pass

        return candidates

    def _build_bind_page_prompt_and_components(
        self,
        repos: list[tuple[str, str]],
        *,
        page: int,
    ) -> tuple[str, list[dict[str, Any]]]:
        page_size = DISCORD_SELECT_OPTION_MAX_OPTIONS
        total = len(repos)
        total_pages = max(1, (total + page_size - 1) // page_size)
        bounded_page = max(0, min(page, total_pages - 1))
        start = bounded_page * page_size
        end = start + page_size
        page_repos = repos[start:end]

        prompt = "Select a workspace to bind:"
        if total > page_size:
            prompt = (
                "Select a workspace to bind "
                f"(page {bounded_page + 1}/{total_pages}, {total} total; "
                "recent worktrees first). Use `/car bind workspace:<repo_id>` "
                "or `/car bind workspace:<path>` for any repo not listed."
            )

        components: list[dict[str, Any]] = [build_bind_picker(page_repos)]
        if total_pages > 1:
            components.append(
                build_action_row(
                    [
                        build_button(
                            "Prev",
                            f"{BIND_PAGE_CUSTOM_ID_PREFIX}:{bounded_page - 1}",
                            disabled=bounded_page <= 0,
                        ),
                        build_button(
                            f"Page {bounded_page + 1}/{total_pages}",
                            f"{BIND_PAGE_CUSTOM_ID_PREFIX}:noop",
                            disabled=True,
                        ),
                        build_button(
                            "Next",
                            f"{BIND_PAGE_CUSTOM_ID_PREFIX}:{bounded_page + 1}",
                            disabled=bounded_page >= total_pages - 1,
                        ),
                    ]
                )
            )

        return prompt, components

    def _repo_autocomplete_value(self, repo_id: str) -> str:
        normalized_id = repo_id.strip()
        if len(normalized_id) <= 100:
            return normalized_id
        digest = hashlib.sha256(normalized_id.encode("utf-8")).hexdigest()[:24]
        return f"{REPO_AUTOCOMPLETE_TOKEN_PREFIX}{digest}"

    def _workspace_autocomplete_value(self, workspace_path: str) -> str:
        normalized_path = workspace_path.strip()
        if len(normalized_path) <= 100:
            return normalized_path
        digest = hashlib.sha256(normalized_path.encode("utf-8")).hexdigest()[:24]
        return f"{WORKSPACE_AUTOCOMPLETE_TOKEN_PREFIX}{digest}"

    def _resolve_workspace_from_token(
        self,
        token: str,
        candidates: list[tuple[Optional[str], str]],
    ) -> Optional[tuple[Optional[str], str]]:
        normalized = token.strip()
        if not normalized:
            return None

        for repo_id, workspace_path in candidates:
            if repo_id == normalized or workspace_path == normalized:
                return repo_id, workspace_path

        if normalized.startswith(REPO_AUTOCOMPLETE_TOKEN_PREFIX):
            digest = normalized[len(REPO_AUTOCOMPLETE_TOKEN_PREFIX) :]
            if digest:
                matches = [
                    (repo_id, workspace_path)
                    for repo_id, workspace_path in candidates
                    if isinstance(repo_id, str)
                    and hashlib.sha256(repo_id.encode("utf-8"))
                    .hexdigest()
                    .startswith(digest)
                ]
                if len(matches) == 1:
                    return matches[0]

        if normalized.startswith(WORKSPACE_AUTOCOMPLETE_TOKEN_PREFIX):
            digest = normalized[len(WORKSPACE_AUTOCOMPLETE_TOKEN_PREFIX) :]
            if digest:
                matches = [
                    (repo_id, workspace_path)
                    for repo_id, workspace_path in candidates
                    if hashlib.sha256(workspace_path.encode("utf-8"))
                    .hexdigest()
                    .startswith(digest)
                ]
                if len(matches) == 1:
                    return matches[0]

        return None

    def _build_bind_autocomplete_choices(self, query: str) -> list[dict[str, str]]:
        candidates = self._list_bind_workspace_candidates()
        normalized_query = query.strip().lower()
        scored: list[tuple[int, int, Optional[str], str]] = []

        for index, (repo_id, path) in enumerate(candidates):
            rid = repo_id.lower() if isinstance(repo_id, str) else ""
            basename = Path(path).name.lower()
            pth = path.lower()
            if (
                normalized_query
                and normalized_query not in rid
                and normalized_query not in basename
                and normalized_query not in pth
            ):
                continue

            score = 0
            if normalized_query:
                if rid.startswith(normalized_query):
                    score += 40
                elif normalized_query in rid:
                    score += 20
                if basename.startswith(normalized_query):
                    score += 25
                elif normalized_query in basename:
                    score += 15
                if pth.startswith(normalized_query):
                    score += 10
                elif normalized_query in pth:
                    score += 5

            scored.append((score, -index, repo_id, path))

        scored.sort(key=lambda item: (-item[0], -item[1], item[2] or "", item[3]))
        seen: set[str] = set()
        choices: list[dict[str, str]] = []
        for _score, _neg_index, repo_id, path in scored:
            value = (
                self._repo_autocomplete_value(repo_id)
                if isinstance(repo_id, str) and repo_id
                else self._workspace_autocomplete_value(path)
            )
            if not value or value in seen:
                continue
            seen.add(value)
            option_name = (
                f"{repo_id} - {path}" if repo_id else f"{Path(path).name} - {path}"
            )
            choices.append(
                {
                    "name": option_name[:100],
                    "value": value,
                }
            )
            if len(choices) >= DISCORD_SELECT_OPTION_MAX_OPTIONS:
                break
        return choices

    async def _handle_command_autocomplete(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        command_path: tuple[str, ...],
        options: dict[str, Any],
        focused_name: Optional[str],
        focused_value: str,
    ) -> None:
        _ = options
        choices: list[dict[str, str]] = []
        if command_path == ("car", "bind") and focused_name == "workspace":
            choices = self._build_bind_autocomplete_choices(focused_value)
        await self._respond_autocomplete(
            interaction_id,
            interaction_token,
            choices=choices,
        )

    async def _bind_with_path(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        guild_id: Optional[str],
        raw_path: str,
    ) -> None:
        token = raw_path.strip()
        candidates = self._list_bind_workspace_candidates()
        resolved_workspace = self._resolve_workspace_from_token(token, candidates)
        if resolved_workspace:
            selected_repo_id, workspace_path = resolved_workspace
            workspace = canonicalize_path(Path(workspace_path))
        else:
            candidate = Path(token)
            if not candidate.is_absolute():
                candidate = self._config.root / candidate
            workspace = canonicalize_path(candidate)
            selected_repo_id = None

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
            "/car flow start [force_new] - Start flow (or reuse active/paused)",
            "/car flow restart [run_id] - Restart flow with a fresh run",
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
        branch_suffix = hashlib.sha256(str(workspace_root).encode("utf-8")).hexdigest()[
            :10
        ]
        branch_name = f"thread-{safe_channel_id}-{branch_suffix}"

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
            thread_items = await self._list_session_threads_for_picker(
                workspace_root=workspace_root,
                current_thread_id=current_thread_id,
            )
            if thread_items:
                header = (
                    f"Current thread: `{current_thread_id}`\n\n"
                    if current_thread_id
                    else ""
                )
                await self._send_or_respond_ephemeral(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    deferred=deferred,
                    text=format_discord_message(header + "Select a thread to resume:"),
                )
                await self._send_followup_ephemeral(
                    interaction_token=interaction_token,
                    content="Choose one thread from the picker below.",
                    components=[build_session_threads_picker(thread_items)],
                )
                return
            if current_thread_id:
                text = format_discord_message(
                    f"Current thread: `{current_thread_id}`\n\n"
                    "No additional threads found. Use `/car session resume thread_id:<thread_id>` to resume a specific thread."
                )
            else:
                text = format_discord_message(
                    "No thread is currently active.\n\n"
                    "Use `/car session resume thread_id:<thread_id>` to resume a specific thread, "
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
        if not isinstance(raw_target, str) or not raw_target.strip():
            await self._respond_with_components(
                interaction_id,
                interaction_token,
                "Select update target:",
                [build_update_target_picker(custom_id=UPDATE_TARGET_SELECT_ID)],
            )
            return
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
            await self._respond_with_components(
                interaction_id,
                interaction_token,
                f"{exc} Select update target:",
                [build_update_target_picker(custom_id=UPDATE_TARGET_SELECT_ID)],
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
        if not isinstance(current_agent, str):
            current_agent = self.DEFAULT_AGENT
        current_agent = current_agent.strip().lower()
        if current_agent not in self.VALID_AGENT_VALUES:
            current_agent = self.DEFAULT_AGENT
        agent_name = options.get("name")

        if not agent_name:
            await self._respond_with_components(
                interaction_id,
                interaction_token,
                format_discord_message(
                    "\n".join(
                        [
                            f"Current agent: {current_agent}",
                            "",
                            "Select an agent:",
                        ]
                    )
                ),
                [build_agent_picker(current_agent=current_agent)],
            )
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

    def _pending_interaction_scope_key(
        self,
        *,
        channel_id: str,
        user_id: Optional[str],
    ) -> str:
        scoped_user = user_id.strip() if isinstance(user_id, str) else ""
        return f"{channel_id}:{scoped_user or '_'}"

    async def _handle_car_model(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        user_id: Optional[str],
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
        if not isinstance(current_agent, str):
            current_agent = self.DEFAULT_AGENT
        current_agent = current_agent.strip().lower()
        if current_agent not in self.VALID_AGENT_VALUES:
            current_agent = self.DEFAULT_AGENT
        current_model = binding.get("model_override")
        if not isinstance(current_model, str) or not current_model.strip():
            current_model = None
        current_effort = binding.get("reasoning_effort")
        model_name = options.get("name")
        effort = options.get("effort")

        if not model_name:
            deferred = await self._defer_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
            )

            def _fallback_model_text(note: Optional[str] = None) -> str:
                lines = [
                    f"Current agent: {current_agent}",
                    f"Current model: {current_model or '(default)'}",
                ]
                if isinstance(current_effort, str) and current_effort.strip():
                    lines.append(f"Reasoning effort: {current_effort}")
                if note:
                    lines.extend(["", note])
                lines.extend(
                    [
                        "",
                        "Use `/car model name:<id>` to set a model.",
                        "Use `/car model name:<id> effort:<value>` to set model with reasoning effort (codex only).",
                        "",
                        f"Valid efforts: {', '.join(self.VALID_REASONING_EFFORTS)}",
                    ]
                )
                return format_discord_message("\n".join(lines))

            async def _send_model_picker_or_fallback(
                text: str,
                *,
                components: Optional[list[dict[str, Any]]] = None,
            ) -> None:
                if deferred:
                    sent = await self._send_followup_ephemeral(
                        interaction_token=interaction_token,
                        content=text,
                        components=components,
                    )
                    if sent:
                        return
                if components:
                    await self._respond_with_components(
                        interaction_id,
                        interaction_token,
                        text,
                        components,
                    )
                    return
                await self._respond_ephemeral(
                    interaction_id,
                    interaction_token,
                    text,
                )

            if current_agent == "opencode":
                try:
                    model_items = await self._list_opencode_models_for_picker(
                        workspace_path=binding.get("workspace_path")
                    )
                except Exception as exc:
                    log_event(
                        self._logger,
                        logging.WARNING,
                        "discord.model.list.failed",
                        channel_id=channel_id,
                        agent=current_agent,
                        exc=exc,
                    )
                    await _send_model_picker_or_fallback(
                        _fallback_model_text("Failed to list models for picker."),
                    )
                    return
                if model_items is None:
                    await _send_model_picker_or_fallback(
                        _fallback_model_text(
                            "Workspace unavailable for model picker. Re-bind this channel with `/car bind` and try again."
                        ),
                    )
                    return
                if not model_items and not current_model:
                    await _send_model_picker_or_fallback(
                        _fallback_model_text("No models found from OpenCode."),
                    )
                    return
            else:
                try:
                    client = await self._client_for_workspace(
                        binding.get("workspace_path")
                    )
                except AppServerUnavailableError as exc:
                    log_event(
                        self._logger,
                        logging.WARNING,
                        "discord.model.list.failed",
                        channel_id=channel_id,
                        agent=current_agent,
                        exc=exc,
                    )
                    await _send_model_picker_or_fallback(
                        _fallback_model_text(
                            "Model picker unavailable right now (app server unavailable)."
                        ),
                    )
                    return
                if client is None:
                    await _send_model_picker_or_fallback(
                        _fallback_model_text(
                            "Workspace unavailable for model picker. Re-bind this channel with `/car bind` and try again."
                        ),
                    )
                    return
                try:
                    result = await _model_list_with_agent_compat(
                        client,
                        params={
                            "cursor": None,
                            "limit": DISCORD_SELECT_OPTION_MAX_OPTIONS,
                            "agent": current_agent,
                        },
                    )
                    model_items = _coerce_model_picker_items(result)
                except Exception as exc:
                    log_event(
                        self._logger,
                        logging.WARNING,
                        "discord.model.list.failed",
                        channel_id=channel_id,
                        agent=current_agent,
                        exc=exc,
                    )
                    await _send_model_picker_or_fallback(
                        _fallback_model_text("Failed to list models for picker."),
                    )
                    return

                if not model_items and not current_model:
                    await _send_model_picker_or_fallback(
                        _fallback_model_text("No models found from the app server."),
                    )
                    return

            lines = [
                f"Current agent: {current_agent}",
                f"Current model: {current_model or '(default)'}",
            ]
            if isinstance(current_effort, str) and current_effort.strip():
                lines.append(f"Reasoning effort: {current_effort}")
            lines.extend(
                [
                    "",
                    "Select a model override:",
                    "(default model) clears the override.",
                    "Use `/car model name:<id> effort:<value>` to set reasoning effort (codex only).",
                ]
            )
            await _send_model_picker_or_fallback(
                format_discord_message("\n".join(lines)),
                components=[
                    build_model_picker(
                        model_items,
                        current_model=current_model,
                    )
                ],
            )
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

        if current_agent == "opencode" and not _is_valid_opencode_model_name(
            model_name
        ):
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "OpenCode model must be in `provider/model` format.",
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

    async def _handle_model_picker_selection(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        user_id: Optional[str],
        selected_model: str,
    ) -> None:
        model_value = selected_model.strip()
        if not model_value:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Please select a model and try again.",
            )
            return
        if model_value in {"clear", "reset"}:
            pending_key = self._pending_interaction_scope_key(
                channel_id=channel_id,
                user_id=user_id,
            )
            self._pending_model_effort.pop(pending_key, None)
            await self._handle_car_model(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                user_id=user_id,
                options={"name": "clear"},
            )
            return

        binding = await self._store.get_binding(channel_id=channel_id)
        current_agent_raw = (
            binding.get("agent") if binding else None
        ) or self.DEFAULT_AGENT
        if not isinstance(current_agent_raw, str):
            current_agent_raw = self.DEFAULT_AGENT
        current_agent = current_agent_raw.strip().lower()
        if current_agent not in self.VALID_AGENT_VALUES:
            current_agent = self.DEFAULT_AGENT

        if current_agent == "codex":
            pending_key = self._pending_interaction_scope_key(
                channel_id=channel_id,
                user_id=user_id,
            )
            self._pending_model_effort[pending_key] = model_value
            await self._respond_with_components(
                interaction_id,
                interaction_token,
                (
                    f"Selected model: `{model_value}`\n"
                    "Select reasoning effort (or none):"
                ),
                [build_model_effort_picker(custom_id=MODEL_EFFORT_SELECT_ID)],
            )
            return

        await self._handle_car_model(
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            user_id=user_id,
            options={"name": model_value},
        )

    async def _handle_model_effort_selection(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        user_id: Optional[str],
        selected_effort: str,
    ) -> None:
        pending_key = self._pending_interaction_scope_key(
            channel_id=channel_id,
            user_id=user_id,
        )
        model_name = self._pending_model_effort.pop(pending_key, None)
        if not isinstance(model_name, str) or not model_name:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Model selection expired. Please re-run `/car model`.",
            )
            return

        effort_value = selected_effort.strip().lower()
        if effort_value not in self.VALID_REASONING_EFFORTS and effort_value != "none":
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Invalid effort '{selected_effort}'.",
            )
            return

        model_options: dict[str, Any] = {"name": model_name}
        if effort_value != "none":
            model_options["effort"] = effort_value
        await self._handle_car_model(
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            user_id=user_id,
            options=model_options,
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

        raw_config: dict[str, object] = {}
        try:
            repo_config = load_repo_config(self._config.root)
            if isinstance(repo_config.raw, dict):
                raw_config = repo_config.raw
        except Exception:
            raw_config = {}

        overview_entries = build_hub_flow_overview_entries(
            hub_root=self._config.root,
            manifest=manifest,
            raw_config=raw_config,
        )
        display_label_by_repo_id: dict[str, str] = {}
        for repo in manifest.repos:
            if not repo.enabled:
                continue
            label = (
                repo.display_name.strip()
                if isinstance(repo.display_name, str) and repo.display_name.strip()
                else repo.id
            )
            display_label_by_repo_id[repo.id] = label

        lines = ["Hub Flow Overview:"]
        groups: dict[str, list[tuple[str, str]]] = {}
        group_order: list[str] = []
        has_unregistered = any(entry.unregistered for entry in overview_entries)
        for entry in overview_entries:
            line_label = display_label_by_repo_id.get(entry.repo_id, entry.label)
            if entry.group not in groups:
                groups[entry.group] = []
                group_order.append(entry.group)
            try:
                store = self._open_flow_store(entry.repo_root)
            except Exception:
                groups[entry.group].append(
                    (
                        line_label,
                        f"{entry.indent}❓ {line_label}: Error reading state",
                    )
                )
                continue
            try:
                runs = store.list_flow_runs(flow_type="ticket_flow")
                latest = runs[0] if runs else None
                progress = ticket_progress(entry.repo_root)
                display = build_ticket_flow_display(
                    status=latest.status.value if latest else None,
                    done_count=progress.get("done", 0),
                    total_count=progress.get("total", 0),
                    run_id=latest.id if latest else None,
                )
                run_id = display.get("run_id")
                run_suffix = f" run {run_id}" if run_id else ""
                line = (
                    f"{entry.indent}{display['status_icon']} {line_label}: "
                    f"{display['status_label']} {display['done_count']}/{display['total_count']}{run_suffix}"
                )
            except Exception:
                line = f"{entry.indent}❓ {line_label}: Error reading state"
            finally:
                store.close()
            groups[entry.group].append((line_label, line))

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
        update_message: bool = False,
    ) -> None:
        run_id_opt = options.get("run_id")
        if not (isinstance(run_id_opt, str) and run_id_opt.strip()):
            await self._prompt_flow_action_picker(
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
                action="status",
            )
            return
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
                record, _updated, locked = reconcile_flow_run(
                    workspace_root, record, store
                )
                if locked:
                    await self._respond_ephemeral(
                        interaction_id,
                        interaction_token,
                        f"Run {record.id} is locked for reconcile; try again.",
                    )
                    return
            except (sqlite3.Error, OSError) as exc:
                log_event(
                    self._logger,
                    logging.ERROR,
                    "discord.flow.reconcile_failed",
                    exc=exc,
                    run_id=record.id,
                )
                raise DiscordTransientError(
                    f"Failed to reconcile flow run: {exc}",
                    user_message="Unable to reconcile flow run. Please try again later.",
                ) from None
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
            if update_message:
                await self._update_component_message(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    text=response_text,
                    components=status_buttons,
                )
            else:
                await self._respond_with_components(
                    interaction_id,
                    interaction_token,
                    response_text,
                    status_buttons,
                )
        else:
            if update_message:
                await self._update_component_message(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    text=response_text,
                    components=[],
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

    async def _handle_flow_start(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        options: dict[str, Any],
    ) -> None:
        force_new = bool(options.get("force_new"))
        restart_from = options.get("restart_from")

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
        finally:
            store.close()

        if not force_new:
            active_or_paused = next(
                (
                    record
                    for record in runs
                    if record.status in {FlowRunStatus.RUNNING, FlowRunStatus.PAUSED}
                ),
                None,
            )
            if active_or_paused is not None:
                ensure_result = ensure_worker(
                    workspace_root,
                    active_or_paused.id,
                    is_terminal=active_or_paused.status.is_terminal(),
                )
                self._close_worker_handles(ensure_result)
                await self._respond_ephemeral(
                    interaction_id,
                    interaction_token,
                    f"Reusing ticket_flow run {active_or_paused.id} ({active_or_paused.status.value}).",
                )
                return

        metadata: dict[str, Any] = {"origin": "discord", "force_new": force_new}
        if isinstance(restart_from, str) and restart_from.strip():
            metadata["restart_from"] = restart_from.strip()

        controller = build_ticket_flow_controller(workspace_root)
        try:
            started = await controller.start_flow(input_data={}, metadata=metadata)
        except ValueError as exc:
            await self._respond_ephemeral(interaction_id, interaction_token, str(exc))
            return

        ensure_result = ensure_worker(
            workspace_root, started.id, is_terminal=started.status.is_terminal()
        )
        self._close_worker_handles(ensure_result)
        prefix = "Started new" if force_new else "Started"
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"{prefix} ticket_flow run {started.id}.",
        )

    async def _handle_flow_restart(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        options: dict[str, Any],
    ) -> None:
        run_id_opt = options.get("run_id")
        if not (isinstance(run_id_opt, str) and run_id_opt.strip()):
            await self._prompt_flow_action_picker(
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
                action="restart",
            )
            return
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
                    (record for record in runs if record.status.is_active()), None
                )
        finally:
            store.close()

        if isinstance(run_id_opt, str) and run_id_opt.strip() and target is None:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"No ticket_flow run found for run_id {run_id_opt.strip()}.",
            )
            return

        if target is not None and target.status.is_active():
            self._stop_flow_worker(workspace_root, target.id)
            controller = build_ticket_flow_controller(workspace_root)
            try:
                await controller.stop_flow(target.id)
            except ValueError as exc:
                await self._respond_ephemeral(
                    interaction_id, interaction_token, str(exc)
                )
                return
            latest = await self._wait_for_flow_terminal(workspace_root, target.id)
            if latest is None or not latest.status.is_terminal():
                status_value = latest.status.value if latest is not None else "unknown"
                await self._respond_ephemeral(
                    interaction_id,
                    interaction_token,
                    (
                        f"Run {target.id} is still active ({status_value}); "
                        "restart aborted to avoid concurrent workers. Try again after it stops."
                    ),
                )
                return

        await self._handle_flow_start(
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            options={
                "force_new": True,
                "restart_from": target.id if target is not None else None,
            },
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
        if not (isinstance(run_id_opt, str) and run_id_opt.strip()):
            await self._prompt_flow_action_picker(
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
                action="recover",
            )
            return
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

    @staticmethod
    def _stop_flow_worker(workspace_root: Path, run_id: str) -> None:
        health = check_worker_health(workspace_root, run_id)
        if health.is_alive and health.pid:
            try:
                subprocess.run(["kill", str(health.pid)], check=False)
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "Failed to stop worker %s: %s", run_id, exc
                )
        if health.status in {"dead", "mismatch", "invalid"}:
            clear_worker_metadata(health.artifact_path.parent)

    async def _wait_for_flow_terminal(
        self,
        workspace_root: Path,
        run_id: str,
        *,
        timeout_seconds: float = 10.0,
        poll_interval_seconds: float = 0.25,
    ) -> Optional[FlowRunRecord]:
        deadline = time.monotonic() + max(timeout_seconds, poll_interval_seconds)
        latest: Optional[FlowRunRecord] = None

        while time.monotonic() < deadline:
            try:
                store = self._open_flow_store(workspace_root)
            except (sqlite3.Error, OSError, RuntimeError):
                break
            try:
                record = self._resolve_flow_run_by_id(store, run_id=run_id)
                if record is None:
                    return None
                latest = record
                if record.status.is_terminal():
                    return record
                record, _updated, locked = reconcile_flow_run(
                    workspace_root, record, store
                )
                latest = record
                if record.status.is_terminal():
                    return record
                if locked:
                    # Another reconciler holds the lock; keep polling.
                    pass
            finally:
                store.close()
            await asyncio.sleep(poll_interval_seconds)

        return latest

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
        if not (isinstance(run_id_opt, str) and run_id_opt.strip()):
            await self._prompt_flow_action_picker(
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
                action="resume",
            )
            return
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
        if not (isinstance(run_id_opt, str) and run_id_opt.strip()):
            await self._prompt_flow_action_picker(
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
                action="stop",
            )
            return
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
        if not (isinstance(run_id_opt, str) and run_id_opt.strip()):
            await self._prompt_flow_action_picker(
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
                action="archive",
            )
            return
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
        user_id: Optional[str] = None,
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
        if not (isinstance(run_id_opt, str) and run_id_opt.strip()) and channel_id:
            pending_key = self._pending_interaction_scope_key(
                channel_id=channel_id,
                user_id=user_id,
            )
            self._pending_flow_reply_text[pending_key] = text.strip()
            await self._prompt_flow_action_picker(
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
                action="reply",
            )
            return
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

    def _list_paths_in_dir(self, folder: Path) -> list[Path]:
        if not folder.exists():
            return []
        files: list[Path] = []
        for path in folder.iterdir():
            try:
                if path.is_file():
                    files.append(path)
            except OSError:
                continue

        def _mtime(entry: Path) -> float:
            try:
                return entry.stat().st_mtime
            except OSError:
                return 0.0

        return sorted(files, key=_mtime, reverse=True)

    def _list_files_in_dir(self, folder: Path) -> list[tuple[str, int, str]]:
        files: list[tuple[str, int, str]] = []
        for path in self._list_paths_in_dir(folder):
            try:
                stat = path.stat()
                from datetime import datetime, timezone

                mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime(
                    "%Y-%m-%d %H:%M"
                )
                files.append((path.name, stat.st_size, mtime))
            except OSError:
                continue
        return files

    async def _send_outbox_file(
        self,
        path: Path,
        *,
        sent_dir: Path,
        channel_id: str,
    ) -> bool:
        try:
            data = path.read_bytes()
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.files.outbox.read_failed",
                channel_id=channel_id,
                path=str(path),
                exc=exc,
            )
            return False
        try:
            await self._rest.create_channel_message_with_attachment(
                channel_id=channel_id,
                data=data,
                filename=path.name,
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.files.outbox.send_failed",
                channel_id=channel_id,
                path=str(path),
                exc=exc,
            )
            return False
        try:
            sent_dir.mkdir(parents=True, exist_ok=True)
            destination = sent_dir / path.name
            if destination.exists():
                destination = (
                    sent_dir / f"{path.stem}-{uuid.uuid4().hex[:6]}{path.suffix}"
                )
            path.replace(destination)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.files.outbox.move_failed",
                channel_id=channel_id,
                path=str(path),
                exc=exc,
            )
            return False
        log_event(
            self._logger,
            logging.INFO,
            "discord.files.outbox.sent",
            channel_id=channel_id,
            path=str(path),
        )
        return True

    async def _flush_outbox_files(
        self,
        *,
        workspace_root: Path,
        channel_id: str,
    ) -> None:
        outbox_root = outbox_dir(workspace_root)
        pending_dir = outbox_pending_dir(workspace_root)
        candidates: list[tuple[Path, Path]] = []
        if outbox_root.exists():
            for path in self._list_paths_in_dir(outbox_root):
                candidates.append((outbox_root, path))
        if pending_dir.exists():
            for path in self._list_paths_in_dir(pending_dir):
                candidates.append((pending_dir, path))
        if not candidates:
            return

        deduped: dict[str, tuple[Path, Path]] = {}
        for source_dir, path in candidates:
            key = str(path)
            with contextlib.suppress(Exception):
                key = str(canonicalize_path(path))
            existing = deduped.get(key)
            if existing is None:
                deduped[key] = (source_dir, path)
                continue
            existing_source, _existing_path = existing
            existing_is_root = existing_source == outbox_root
            current_is_root = source_dir == outbox_root
            # Preserve outbox-root candidates over pending aliases that resolve
            # to the same canonical target (e.g., pending symlink to root file).
            if existing_is_root and not current_is_root:
                continue
            if current_is_root and not existing_is_root:
                deduped[key] = (source_dir, path)

        def _mtime(item: tuple[Path, Path]) -> float:
            _source, path = item
            with contextlib.suppress(OSError):
                return path.stat().st_mtime
            return 0.0

        files = sorted(deduped.values(), key=_mtime, reverse=True)

        sent_dir = outbox_sent_dir(workspace_root)
        for source_dir, path in files:
            if not _path_within(source_dir, path):
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.files.outbox.skipped_outside_pending",
                    channel_id=channel_id,
                    path=str(path),
                    pending_dir=str(source_dir),
                )
                continue
            await self._send_outbox_file(
                path,
                sent_dir=sent_dir,
                channel_id=channel_id,
            )

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
        outbox_root = outbox_dir(workspace_root)
        pending = outbox_pending_dir(workspace_root)
        sent = outbox_sent_dir(workspace_root)
        root_files = self._list_files_in_dir(outbox_root)
        root_files = [
            entry for entry in root_files if entry[0] not in {"pending", "sent"}
        ]
        pending_files = self._list_files_in_dir(pending)
        sent_files = self._list_files_in_dir(sent)
        lines = []
        if root_files:
            lines.append(f"Outbox root ({len(root_files)} file(s)):")
            for name, size, mtime in root_files[:20]:
                lines.append(f"- {name} ({self._format_file_size(size)}, {mtime})")
            if len(root_files) > 20:
                lines.append(f"... and {len(root_files) - 20} more")
            lines.append("")
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
        outbox_root = outbox_dir(workspace_root)
        pending = outbox_pending_dir(workspace_root)
        sent = outbox_sent_dir(workspace_root)
        deleted = 0
        if target == "inbox":
            deleted = self._delete_files_in_dir(inbox)
        elif target == "outbox":
            deleted = self._delete_files_in_dir(outbox_root)
            deleted += self._delete_files_in_dir(pending)
            deleted += self._delete_files_in_dir(sent)
        elif target == "all":
            deleted = self._delete_files_in_dir(inbox)
            deleted += self._delete_files_in_dir(outbox_root)
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
        if binding is None:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "\n".join(
                    [
                        "PMA mode: disabled",
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

    async def _respond_autocomplete(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        choices: list[dict[str, str]],
    ) -> None:
        sanitized_choices: list[dict[str, str]] = []
        for choice in choices[:DISCORD_SELECT_OPTION_MAX_OPTIONS]:
            name = choice.get("name", "")
            value = choice.get("value", "")
            if not isinstance(name, str) or not isinstance(value, str):
                continue
            normalized_name = name.strip()
            normalized_value = value.strip()
            if not normalized_name or not normalized_value:
                continue
            sanitized_choices.append(
                {
                    "name": normalized_name[:100],
                    "value": normalized_value[:100],
                }
            )

        try:
            await self._rest.create_interaction_response(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                payload={"type": 8, "data": {"choices": sanitized_choices}},
            )
        except DiscordAPIError as exc:
            self._logger.error(
                "Failed to send autocomplete response: %s (interaction_id=%s)",
                exc,
                interaction_id,
            )

    async def _update_component_message(
        self,
        *,
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
                    "type": 7,
                    "data": {
                        "content": content,
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
                    "Failed to update component message: %s (interaction_id=%s)",
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
        user_id = extract_user_id(interaction_payload)

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
            if custom_id.startswith(f"{BIND_PAGE_CUSTOM_ID_PREFIX}:"):
                page_token = custom_id.split(":", 1)[1].strip()
                await self._handle_bind_page_component(
                    interaction_id,
                    interaction_token,
                    page_token=page_token,
                )
                return

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

            if custom_id == "agent_select":
                values = extract_component_values(interaction_payload)
                if not values:
                    await self._respond_ephemeral(
                        interaction_id,
                        interaction_token,
                        "Please select an agent and try again.",
                    )
                    return
                await self._handle_car_agent(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                    options={"name": values[0]},
                )
                return

            if custom_id == "model_select":
                values = extract_component_values(interaction_payload)
                if not values:
                    await self._respond_ephemeral(
                        interaction_id,
                        interaction_token,
                        "Please select a model and try again.",
                    )
                    return
                await self._handle_model_picker_selection(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                    user_id=user_id,
                    selected_model=values[0],
                )
                return

            if custom_id == MODEL_EFFORT_SELECT_ID:
                values = extract_component_values(interaction_payload)
                if not values:
                    await self._respond_ephemeral(
                        interaction_id,
                        interaction_token,
                        "Please select reasoning effort and try again.",
                    )
                    return
                await self._handle_model_effort_selection(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                    user_id=user_id,
                    selected_effort=values[0],
                )
                return

            if custom_id == SESSION_RESUME_SELECT_ID:
                values = extract_component_values(interaction_payload)
                if not values:
                    await self._respond_ephemeral(
                        interaction_id,
                        interaction_token,
                        "Please select a thread and try again.",
                    )
                    return
                await self._handle_car_resume(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                    options={"thread_id": values[0]},
                )
                return

            if custom_id == UPDATE_TARGET_SELECT_ID:
                values = extract_component_values(interaction_payload)
                if not values:
                    await self._respond_ephemeral(
                        interaction_id,
                        interaction_token,
                        "Please select an update target and try again.",
                    )
                    return
                await self._handle_car_update(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                    options={"target": values[0]},
                )
                return

            if custom_id == REVIEW_COMMIT_SELECT_ID:
                values = extract_component_values(interaction_payload)
                if not values:
                    await self._respond_ephemeral(
                        interaction_id,
                        interaction_token,
                        "Please select a commit and try again.",
                    )
                    return
                workspace_root = await self._require_bound_workspace(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                )
                if workspace_root:
                    await self._handle_car_review(
                        interaction_id,
                        interaction_token,
                        channel_id=channel_id,
                        workspace_root=workspace_root,
                        options={"target": f"commit {values[0]}"},
                    )
                return

            if custom_id.startswith(f"{FLOW_ACTION_SELECT_PREFIX}:"):
                action = custom_id.split(":", 1)[1].strip().lower()
                values = extract_component_values(interaction_payload)
                if not values:
                    await self._respond_ephemeral(
                        interaction_id,
                        interaction_token,
                        "Please select a run and try again.",
                    )
                    return
                if action not in FLOW_ACTIONS_WITH_RUN_PICKER:
                    await self._respond_ephemeral(
                        interaction_id,
                        interaction_token,
                        f"Unknown flow action picker: {action}",
                    )
                    return
                workspace_root = await self._require_bound_workspace(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                )
                if not workspace_root:
                    return
                run_id = values[0]
                if action == "status":
                    await self._handle_flow_status(
                        interaction_id,
                        interaction_token,
                        workspace_root=workspace_root,
                        options={"run_id": run_id},
                        channel_id=channel_id,
                        guild_id=extract_guild_id(interaction_payload),
                    )
                    return
                if action == "restart":
                    await self._handle_flow_restart(
                        interaction_id,
                        interaction_token,
                        workspace_root=workspace_root,
                        options={"run_id": run_id},
                    )
                    return
                if action == "resume":
                    await self._handle_flow_resume(
                        interaction_id,
                        interaction_token,
                        workspace_root=workspace_root,
                        options={"run_id": run_id},
                        channel_id=channel_id,
                        guild_id=extract_guild_id(interaction_payload),
                    )
                    return
                if action == "stop":
                    await self._handle_flow_stop(
                        interaction_id,
                        interaction_token,
                        workspace_root=workspace_root,
                        options={"run_id": run_id},
                        channel_id=channel_id,
                        guild_id=extract_guild_id(interaction_payload),
                    )
                    return
                if action == "archive":
                    await self._handle_flow_archive(
                        interaction_id,
                        interaction_token,
                        workspace_root=workspace_root,
                        options={"run_id": run_id},
                        channel_id=channel_id,
                        guild_id=extract_guild_id(interaction_payload),
                    )
                    return
                if action == "recover":
                    await self._handle_flow_recover(
                        interaction_id,
                        interaction_token,
                        workspace_root=workspace_root,
                        options={"run_id": run_id},
                    )
                    return
                if action == "reply":
                    pending_key = self._pending_interaction_scope_key(
                        channel_id=channel_id,
                        user_id=user_id,
                    )
                    pending_text = self._pending_flow_reply_text.pop(pending_key, None)
                    if not isinstance(pending_text, str) or not pending_text.strip():
                        await self._respond_ephemeral(
                            interaction_id,
                            interaction_token,
                            "Reply selection expired. Re-run `/car flow reply text:<...>`.",
                        )
                        return
                    await self._handle_flow_reply(
                        interaction_id,
                        interaction_token,
                        workspace_root=workspace_root,
                        options={"run_id": run_id, "text": pending_text},
                        channel_id=channel_id,
                        guild_id=extract_guild_id(interaction_payload),
                        user_id=user_id,
                    )
                    return
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

            if custom_id == "cancel_turn":
                await self._handle_cancel_turn_button(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                )
                return

            if custom_id == "continue_turn":
                await self._handle_continue_turn_button(
                    interaction_id,
                    interaction_token,
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
        user_id: Optional[str] = None,
    ) -> None:
        try:
            if custom_id.startswith(f"{BIND_PAGE_CUSTOM_ID_PREFIX}:"):
                page_token = custom_id.split(":", 1)[1].strip()
                await self._handle_bind_page_component(
                    interaction_id,
                    interaction_token,
                    page_token=page_token,
                )
                return

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

            if custom_id == "agent_select":
                if not values:
                    await self._respond_ephemeral(
                        interaction_id,
                        interaction_token,
                        "Please select an agent and try again.",
                    )
                    return
                await self._handle_car_agent(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                    options={"name": values[0]},
                )
                return

            if custom_id == "model_select":
                if not values:
                    await self._respond_ephemeral(
                        interaction_id,
                        interaction_token,
                        "Please select a model and try again.",
                    )
                    return
                await self._handle_model_picker_selection(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                    user_id=user_id,
                    selected_model=values[0],
                )
                return

            if custom_id == MODEL_EFFORT_SELECT_ID:
                if not values:
                    await self._respond_ephemeral(
                        interaction_id,
                        interaction_token,
                        "Please select reasoning effort and try again.",
                    )
                    return
                await self._handle_model_effort_selection(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                    user_id=user_id,
                    selected_effort=values[0],
                )
                return

            if custom_id == SESSION_RESUME_SELECT_ID:
                if not values:
                    await self._respond_ephemeral(
                        interaction_id,
                        interaction_token,
                        "Please select a thread and try again.",
                    )
                    return
                await self._handle_car_resume(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                    options={"thread_id": values[0]},
                )
                return

            if custom_id == UPDATE_TARGET_SELECT_ID:
                if not values:
                    await self._respond_ephemeral(
                        interaction_id,
                        interaction_token,
                        "Please select an update target and try again.",
                    )
                    return
                await self._handle_car_update(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                    options={"target": values[0]},
                )
                return

            if custom_id == REVIEW_COMMIT_SELECT_ID:
                if not values:
                    await self._respond_ephemeral(
                        interaction_id,
                        interaction_token,
                        "Please select a commit and try again.",
                    )
                    return
                workspace_root = await self._require_bound_workspace(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                )
                if workspace_root:
                    await self._handle_car_review(
                        interaction_id,
                        interaction_token,
                        channel_id=channel_id,
                        workspace_root=workspace_root,
                        options={"target": f"commit {values[0]}"},
                    )
                return

            if custom_id.startswith(f"{FLOW_ACTION_SELECT_PREFIX}:"):
                action = custom_id.split(":", 1)[1].strip().lower()
                if not values:
                    await self._respond_ephemeral(
                        interaction_id,
                        interaction_token,
                        "Please select a run and try again.",
                    )
                    return
                if action not in FLOW_ACTIONS_WITH_RUN_PICKER:
                    await self._respond_ephemeral(
                        interaction_id,
                        interaction_token,
                        f"Unknown flow action picker: {action}",
                    )
                    return
                workspace_root = await self._require_bound_workspace(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                )
                if not workspace_root:
                    return
                run_id = values[0]
                if action == "status":
                    await self._handle_flow_status(
                        interaction_id,
                        interaction_token,
                        workspace_root=workspace_root,
                        options={"run_id": run_id},
                        channel_id=channel_id,
                        guild_id=guild_id,
                    )
                    return
                if action == "restart":
                    await self._handle_flow_restart(
                        interaction_id,
                        interaction_token,
                        workspace_root=workspace_root,
                        options={"run_id": run_id},
                    )
                    return
                if action == "resume":
                    await self._handle_flow_resume(
                        interaction_id,
                        interaction_token,
                        workspace_root=workspace_root,
                        options={"run_id": run_id},
                        channel_id=channel_id,
                        guild_id=guild_id,
                    )
                    return
                if action == "stop":
                    await self._handle_flow_stop(
                        interaction_id,
                        interaction_token,
                        workspace_root=workspace_root,
                        options={"run_id": run_id},
                        channel_id=channel_id,
                        guild_id=guild_id,
                    )
                    return
                if action == "archive":
                    await self._handle_flow_archive(
                        interaction_id,
                        interaction_token,
                        workspace_root=workspace_root,
                        options={"run_id": run_id},
                        channel_id=channel_id,
                        guild_id=guild_id,
                    )
                    return
                if action == "recover":
                    await self._handle_flow_recover(
                        interaction_id,
                        interaction_token,
                        workspace_root=workspace_root,
                        options={"run_id": run_id},
                    )
                    return
                if action == "reply":
                    pending_key = self._pending_interaction_scope_key(
                        channel_id=channel_id,
                        user_id=user_id,
                    )
                    pending_text = self._pending_flow_reply_text.pop(pending_key, None)
                    if not isinstance(pending_text, str) or not pending_text.strip():
                        await self._respond_ephemeral(
                            interaction_id,
                            interaction_token,
                            "Reply selection expired. Re-run `/car flow reply text:<...>`.",
                        )
                        return
                    await self._handle_flow_reply(
                        interaction_id,
                        interaction_token,
                        workspace_root=workspace_root,
                        options={"run_id": run_id, "text": pending_text},
                        channel_id=channel_id,
                        guild_id=guild_id,
                        user_id=user_id,
                    )
                    return
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

            if custom_id == "continue_turn":
                await self._handle_continue_turn_button(
                    interaction_id,
                    interaction_token,
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

    async def _handle_bind_page_component(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        page_token: str,
    ) -> None:
        if page_token == "noop":
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Already on this page.",
            )
            return
        try:
            requested_page = int(page_token)
        except (TypeError, ValueError):
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Invalid bind page selection.",
            )
            return

        repos = self._list_manifest_repos()
        if not repos:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "No repos found in manifest.",
            )
            return

        prompt, components = self._build_bind_page_prompt_and_components(
            repos,
            page=requested_page,
        )
        await self._update_component_message(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            text=prompt,
            components=components,
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
        elif action == "restart":
            await self._handle_flow_restart(
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
                options={"run_id": run_id},
            )
        elif action == "refresh":
            await self._handle_flow_status(
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
                options={"run_id": run_id},
                channel_id=channel_id,
                guild_id=guild_id,
                update_message=True,
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
        prompt_commit_picker = False

        if isinstance(target_arg, str) and target_arg.strip():
            target_text = target_arg.strip()
            target_lower = target_text.lower()
            if target_lower.startswith("base "):
                branch = target_text[5:].strip()
                if branch:
                    target_type = "baseBranch"
                    target_value = branch
            elif target_lower == "commit" or target_lower.startswith("commit "):
                sha = target_text[6:].strip()
                if sha:
                    target_type = "commit"
                    target_value = sha
                else:
                    prompt_commit_picker = True
            elif target_lower in ("uncommitted", ""):
                pass
            elif target_lower == "custom":
                await self._respond_ephemeral(
                    interaction_id,
                    interaction_token,
                    "Provide custom review instructions after `custom`, for example: "
                    "`/car review target:custom focus on security regressions`.",
                )
                return
            elif target_lower.startswith("custom "):
                custom_instructions = target_text[7:].strip()
                if not custom_instructions:
                    await self._respond_ephemeral(
                        interaction_id,
                        interaction_token,
                        "Provide custom review instructions after `custom`, for example: "
                        "`/car review target:custom focus on security regressions`.",
                    )
                    return
                target_type = "custom"
                target_value = custom_instructions
            else:
                target_type = "custom"
                target_value = target_text

        if prompt_commit_picker:
            commits = await self._list_recent_commits_for_picker(workspace_root)
            if not commits:
                await self._respond_ephemeral(
                    interaction_id,
                    interaction_token,
                    "No recent commits found. Use `/car review target:commit <sha>`.",
                )
                return
            await self._respond_with_components(
                interaction_id,
                interaction_token,
                "Select a commit to review:",
                [
                    build_review_commit_picker(
                        commits, custom_id=REVIEW_COMMIT_SELECT_ID
                    )
                ],
            )
            return

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

        for idx, chunk in enumerate(chunks, 1):
            await self._send_channel_message_safe(
                channel_id,
                {"content": chunk},
                record_id=f"review:{session_key}:{idx}:{uuid.uuid4().hex[:8]}",
            )
        if isinstance(preview_message_id, str) and preview_message_id:
            await self._delete_channel_message_safe(
                channel_id=channel_id,
                message_id=preview_message_id,
                record_id=(
                    f"review:delete_progress:{session_key}:{uuid.uuid4().hex[:8]}"
                ),
            )

        try:
            await self._flush_outbox_files(
                workspace_root=workspace_root,
                channel_id=channel_id,
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.review.outbox_flush_failed",
                channel_id=channel_id,
                target_type=target_type,
                message_id=preview_message_id,
                exc=exc,
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

        usage_text = (
            "Usage:\n"
            "- `/car experimental action:list`\n"
            "- `/car experimental action:enable feature:<feature>`\n"
            "- `/car experimental action:disable feature:<feature>`"
        )

        if not action or action in ("list", "ls", "all"):
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Experimental features listing requires the app server client.\n\n"
                f"{usage_text}",
            )
            return

        if action in ("enable", "on", "true"):
            if not feature:
                await self._respond_ephemeral(
                    interaction_id,
                    interaction_token,
                    f"Missing feature for `enable`.\n\n{usage_text}",
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
                    f"Missing feature for `disable`.\n\n{usage_text}",
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
            f"Unknown action: {action}.\n\nValid actions: list, enable, disable.\n\n{usage_text}",
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
            with_numbering=False,
        )
        if not chunks:
            chunks = ["**Conversation Summary:**\n\n(No summary generated.)"]

        next_chunk_index = 0
        last_chunk_index = len(chunks) - 1
        preview_chunk_applied = False
        preview_message_id = (
            turn_result.preview_message_id
            if isinstance(turn_result.preview_message_id, str)
            and turn_result.preview_message_id
            else None
        )

        if preview_message_id:
            try:
                preview_payload: dict[str, Any] = {"content": chunks[0]}
                if last_chunk_index == 0:
                    preview_payload["components"] = [build_continue_turn_button()]
                else:
                    # This message is not terminal for long compactions.
                    preview_payload["components"] = []
                await self._rest.edit_channel_message(
                    channel_id=channel_id,
                    message_id=preview_message_id,
                    payload=preview_payload,
                )
                preview_chunk_applied = True
                next_chunk_index = 1
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.compact.preview_edit_failed",
                    channel_id=channel_id,
                    message_id=preview_message_id,
                    exc=exc,
                )

        if not preview_chunk_applied:
            first_payload: dict[str, Any] = {"content": chunks[0]}
            if last_chunk_index == 0:
                first_payload["components"] = [build_continue_turn_button()]
            await self._send_channel_message_safe(
                channel_id,
                first_payload,
            )
            next_chunk_index = 1

        for chunk_index, chunk in enumerate(
            chunks[next_chunk_index:], next_chunk_index
        ):
            payload: dict[str, Any] = {"content": chunk}
            if chunk_index == last_chunk_index:
                payload["components"] = [build_continue_turn_button()]
            await self._send_channel_message_safe(channel_id, payload)
        await self._flush_outbox_files(
            workspace_root=workspace_root,
            channel_id=channel_id,
        )

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

    async def _handle_continue_turn_button(
        self,
        interaction_id: str,
        interaction_token: str,
    ) -> None:
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            (
                "Compaction complete. Send your next message to continue this "
                "session, or use `/car new` to start a fresh session."
            ),
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
