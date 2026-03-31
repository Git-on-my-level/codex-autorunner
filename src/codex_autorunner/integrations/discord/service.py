from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import logging
import os
import re
import sqlite3
import subprocess
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    Mapping,
    Optional,
    Sequence,
    cast,
)

from ...agents.opencode.harness import OpenCodeHarness
from ...agents.opencode.supervisor import OpenCodeSupervisor
from ...agents.opencode.supervisor_protocol import (
    OpenCodeHarnessSupervisorProtocol,
)
from ...agents.registry import (
    AgentDescriptor,
    get_agent_descriptor,
    get_registered_agents,
    normalize_agent_capabilities,
)
from ...bootstrap import seed_repo_files
from ...core.chat_bindings import (
    preferred_non_pma_chat_notification_source_for_workspace,
    preferred_non_pma_chat_notification_sources_by_workspace,
)
from ...core.config import (
    ConfigError,
    ensure_hub_config_at,
    find_nearest_hub_config_path,
    load_hub_config,
    load_repo_config,
    resolve_env_for_root,
)
from ...core.filebox import (
    delete_regular_files,
    inbox_dir,
    list_regular_files,
    outbox_dir,
    outbox_pending_dir,
    outbox_sent_dir,
)
from ...core.filebox_retention import (
    prune_filebox_root,
    resolve_filebox_retention_policy,
)
from ...core.flows import (
    FLOW_ACTIONS_WITH_RUN_PICKER,
    FlowRunRecord,
    FlowRunStatus,
    FlowStore,
    flow_action_label,
    flow_run_duration_seconds,
    format_flow_duration,
    list_unseen_ticket_flow_dispatches,
)
from ...core.flows.hub_overview import build_hub_flow_overview_entries
from ...core.flows.reconciler import reconcile_flow_run
from ...core.flows.surface_defaults import should_route_flow_read_to_hub_overview
from ...core.flows.ux_helpers import (
    GitHubServiceProtocol,
    build_flow_status_snapshot,
    ensure_worker,
    format_ticket_flow_status_lines,
    issue_md_path,
    resolve_ticket_flow_archive_mode,
    seed_issue_from_github,
    seed_issue_from_text,
    select_default_ticket_flow_run,
    select_ticket_flow_run_record,
    summarize_flow_freshness,
    ticket_flow_archive_requires_force,
    ticket_progress,
)
from ...core.git_utils import GitError, reset_branch_from_origin_main
from ...core.injected_context import wrap_injected_context
from ...core.logging_utils import log_event
from ...core.managed_processes import reap_managed_processes
from ...core.orchestration import build_ticket_flow_orchestration_service
from ...core.state import RunnerState
from ...core.state_roots import resolve_global_state_root
from ...core.ticket_flow_summary import (
    build_ticket_flow_display,
    format_ticket_flow_summary_lines,
)
from ...core.update import (
    UpdateInProgressError,
    _available_update_target_definitions,
    _format_update_confirmation_warning,
    _normalize_update_ref,
    _normalize_update_target,
    _read_update_status,
    _spawn_update_process,
    _update_target_restarts_surface,
)
from ...core.update_paths import resolve_update_paths
from ...core.update_targets import get_update_target_label
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
from ...integrations.app_server.client import ApprovalDecision, CodexAppServerClient
from ...integrations.app_server.env import app_server_env, build_app_server_env
from ...integrations.app_server.event_buffer import AppServerEventBuffer
from ...integrations.app_server.supervisor import WorkspaceAppServerSupervisor
from ...integrations.app_server.threads import (
    file_chat_discord_key,
    pma_base_key,
)
from ...integrations.chat.agents import (
    DEFAULT_CHAT_AGENT,
    build_agent_switch_state,
    chat_agent_supports_effort,
    normalize_chat_agent,
    valid_chat_agent_values,
)
from ...integrations.chat.bootstrap import ChatBootstrapStep, run_chat_bootstrap_steps
from ...integrations.chat.channel_directory import ChannelDirectoryStore
from ...integrations.chat.collaboration_policy import (
    CollaborationEvaluationContext,
    CollaborationEvaluationResult,
    build_discord_collaboration_policy,
    evaluate_collaboration_admission,
    evaluate_collaboration_policy,
)
from ...integrations.chat.command_contract import command_contract_entry_for_path
from ...integrations.chat.command_diagnostics import (
    ActiveFlowInfo,
    build_status_text,
)
from ...integrations.chat.command_ingress import canonicalize_command_ingress
from ...integrations.chat.compaction import build_compact_seed_prompt
from ...integrations.chat.dispatcher import (
    ChatDispatcher,
    DispatchContext,
    DispatchResult,
    conversation_id_for,
)
from ...integrations.chat.forwarding import compose_forwarded_message_text
from ...integrations.chat.handlers.approvals import (
    normalize_backend_approval_request,
)
from ...integrations.chat.help_catalog import build_discord_help_lines
from ...integrations.chat.media import (
    audio_content_type_for_input,
    audio_extension_for_input,
    is_audio_mime_or_path,
    is_image_mime_or_path,
    normalize_mime_type,
)
from ...integrations.chat.model_selection import (
    REASONING_EFFORT_VALUES,
    _coerce_model_entries,
    _display_name_is_model_alias,
    _is_valid_opencode_model_name,
    _model_list_with_agent_compat,
    format_model_set_message,
)
from ...integrations.chat.models import (
    ChatEvent,
    ChatInteractionEvent,
    ChatMessageEvent,
    ChatReplyInfo,
)
from ...integrations.chat.pause_notifications import (
    format_pause_notification_source,
    format_pause_notification_text,
)
from ...integrations.chat.picker_filter import (
    filter_picker_items,
    resolve_picker_query,
)
from ...integrations.chat.run_mirror import ChatRunMirror
from ...integrations.chat.status_diagnostics import (
    StatusBlockContext,
    build_status_block_lines,
    extract_rate_limits,
)
from ...integrations.chat.text_sanitization import collapse_local_markdown_links
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
from ...tickets.files import (
    list_ticket_paths,
    read_ticket,
    read_ticket_frontmatter,
    safe_relpath,
)
from ...tickets.frontmatter import parse_markdown_frontmatter
from ...tickets.outbox import resolve_outbox_paths
from ...voice import VoiceConfig, VoiceService, VoiceServiceError
from ..chat.approval_modes import (
    APPROVAL_MODE_USAGE,
    normalize_approval_mode,
    resolve_approval_mode_policies,
)
from ..chat.review_commits import _parse_review_commit_log
from ..chat.thread_summaries import (
    _coerce_thread_list,
    _extract_thread_list_cursor,
    _extract_thread_preview_parts,
    _format_resume_picker_label,
    _format_resume_timestamp,
)
from ..telegram.constants import DEFAULT_SKILLS_LIST_LIMIT
from ..telegram.helpers import _format_skills_list
from .adapter import DiscordChatAdapter
from .car_autocomplete import (
    agent_workspace_autocomplete_value,
    repo_autocomplete_value,
    resolve_workspace_from_token,
    workspace_autocomplete_value,
)
from .car_autocomplete import (
    handle_command_autocomplete as handle_car_command_autocomplete,
)
from .car_command_dispatch import handle_car_command as dispatch_car_command
from .collaboration_helpers import (
    build_collaboration_snippet_lines,
    collaboration_probe_text,
    collaboration_summary_lines,
    evaluate_collaboration_summary,
)
from .command_registry import sync_commands
from .commands import build_application_commands
from .components import (
    DISCORD_BUTTON_STYLE_DANGER,
    DISCORD_BUTTON_STYLE_SUCCESS,
    DISCORD_SELECT_OPTION_MAX_OPTIONS,
    build_action_row,
    build_agent_picker,
    build_bind_picker,
    build_button,
    build_flow_runs_picker,
    build_flow_status_buttons,
    build_model_effort_picker,
    build_model_picker,
    build_queue_notice_buttons,
    build_review_commit_picker,
    build_session_threads_picker,
    build_ticket_filter_picker,
    build_ticket_picker,
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
    extract_modal_custom_id,
    extract_modal_values,
    extract_user_id,
    is_autocomplete_interaction,
    is_component_interaction,
    is_modal_submit_interaction,
)
from .message_turns import (
    DiscordMessageTurnResult,
    build_discord_thread_orchestration_service,
    clear_discord_turn_progress_reuse,
    request_discord_turn_progress_reuse,
    resolve_bound_workspace_root,
    run_agent_turn_for_message,
    run_managed_thread_turn_for_message,
)
from .message_turns import (
    handle_message_event as handle_discord_message_event,
)
from .outbox import DiscordOutboxManager
from .pma_commands import handle_pma_off, handle_pma_on, handle_pma_status
from .rendering import (
    chunk_discord_message,
    format_discord_message,
    sanitize_discord_outbound_text,
    truncate_for_discord,
)
from .rest import DiscordRestClient
from .state import DiscordStateStore, OutboxRecord

DISCORD_EPHEMERAL_FLAG = 64
PAUSE_SCAN_INTERVAL_SECONDS = 5.0
TERMINAL_SCAN_INTERVAL_SECONDS = 5.0
FLOW_RUNS_DEFAULT_LIMIT = 5
FLOW_RUNS_MAX_LIMIT = DISCORD_SELECT_OPTION_MAX_OPTIONS
MESSAGE_TURN_APPROVAL_POLICY = "never"
MESSAGE_TURN_SANDBOX_POLICY = "dangerFullAccess"
DEFAULT_UPDATE_REPO_URL = "https://github.com/Git-on-my-level/codex-autorunner.git"
DEFAULT_UPDATE_REPO_REF = "main"
DISCORD_TURN_PROGRESS_MIN_EDIT_INTERVAL_SECONDS = 1.0
DISCORD_TURN_PROGRESS_HEARTBEAT_INTERVAL_SECONDS = 2.0
DISCORD_TURN_PROGRESS_MAX_ACTIONS = 12
DISCORD_TYPING_HEARTBEAT_INTERVAL_SECONDS = 5.0
SHELL_OUTPUT_TRUNCATION_SUFFIX = "\n...[truncated]..."
DISCORD_ATTACHMENT_MAX_BYTES = 100_000_000
THREAD_LIST_MAX_PAGES = 5
THREAD_LIST_PAGE_LIMIT = 100
APP_SERVER_START_BACKOFF_INITIAL_SECONDS = 1.0
APP_SERVER_START_BACKOFF_MAX_SECONDS = 30.0
DISCORD_OPENCODE_PRUNE_FALLBACK_INTERVAL_SECONDS = 300.0
DISCORD_QUEUED_PLACEHOLDER_TEXT = "Queued (waiting for available worker...)"
DISCORD_WHISPER_TRANSCRIPT_DISCLAIMER = (
    "Note: transcribed from user voice. If confusing or possibly inaccurate and you "
    "cannot infer the intention please clarify before proceeding."
)
MODEL_SEARCH_FETCH_LIMIT = 200
SESSION_RESUME_SELECT_ID = "session_resume_select"
FLOW_ACTION_SELECT_PREFIX = "flow_action_select"
UPDATE_TARGET_SELECT_ID = "update_target_select"
UPDATE_CONFIRM_PREFIX = "update_confirm"
UPDATE_CANCEL_PREFIX = "update_cancel"
REVIEW_COMMIT_SELECT_ID = "review_commit_select"
MODEL_EFFORT_SELECT_ID = "model_effort_select"
BIND_PAGE_CUSTOM_ID_PREFIX = "bind_page"
TICKET_PICKER_TOKEN_PREFIX = "ticket@"
TICKETS_FILTER_SELECT_ID = "tickets_filter_select"
TICKETS_SELECT_ID = "tickets_select"
TICKETS_MODAL_PREFIX = "tickets_modal"
TICKETS_BODY_INPUT_ID = "ticket_body"
PREPARED_INTERACTION_CACHE_LIMIT = 512


class AppServerUnavailableError(Exception):
    pass


def _coerce_model_picker_items(
    result: Any,
    *,
    limit: Optional[int] = None,
) -> list[tuple[str, str]]:
    entries = _coerce_model_entries(result)
    options: list[tuple[str, str]] = []
    seen: set[str] = set()
    item_limit = max(
        1,
        limit if isinstance(limit, int) else DISCORD_SELECT_OPTION_MAX_OPTIONS - 1,
    )
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
        if len(options) >= item_limit:
            break
    return options


def _path_within(*, root: Path, target: Path) -> bool:
    try:
        root = canonicalize_path(root)
        target = canonicalize_path(target)
    except Exception:
        return False
    return is_within(root=root, target=target)


def _opencode_prune_interval(idle_ttl_seconds: Optional[int]) -> Optional[float]:
    if not idle_ttl_seconds or idle_ttl_seconds <= 0:
        return None
    return float(min(600.0, max(60.0, idle_ttl_seconds / 2)))


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
    if _format_resume_timestamp(entry):
        preview_label: Optional[str] = None
        user_preview, assistant_preview = _extract_thread_preview_parts(entry)
        user_preview = _truncate_picker_text(user_preview or "", limit=72) or None
        assistant_preview = (
            _truncate_picker_text(assistant_preview or "", limit=72) or None
        )
        if user_preview and assistant_preview:
            preview_label = f"U: {user_preview} | A: {assistant_preview}"
        elif user_preview:
            preview_label = f"U: {user_preview}"
        elif assistant_preview:
            preview_label = f"A: {assistant_preview}"
        base = _format_resume_picker_label(
            thread_id,
            entry,
            limit=max_base_len,
            fallback_preview=preview_label,
        )
        return f"{base}{current_suffix}"
    user_preview, assistant_preview = _extract_thread_preview_parts(entry)
    user_preview = _truncate_picker_text(user_preview or "", limit=72) or None
    assistant_preview = _truncate_picker_text(assistant_preview or "", limit=72) or None
    fallback_preview_label: Optional[str] = None
    if user_preview and assistant_preview:
        fallback_preview_label = f"U: {user_preview} | A: {assistant_preview}"
    elif user_preview:
        fallback_preview_label = f"U: {user_preview}"
    elif assistant_preview:
        fallback_preview_label = f"A: {assistant_preview}"
    if fallback_preview_label:
        short_id = thread_id[:8]
        id_prefix = f"[{short_id}] "
        preview_budget = max(1, max_base_len - len(id_prefix))
        base = f"{id_prefix}{_truncate_picker_text(fallback_preview_label, limit=preview_budget)}"
    else:
        base = _truncate_picker_text(thread_id, limit=max_base_len)
    return f"{base}{current_suffix}"


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


@dataclass
class _OpenCodeSupervisorCacheEntry:
    supervisor: OpenCodeSupervisor
    prune_interval_seconds: Optional[float]
    last_requested_at: float


@dataclass(frozen=True)
class _DiscordTurnApprovalContext:
    channel_id: str


@dataclass
class _DiscordPendingApproval:
    token: str
    request_id: str
    turn_id: str
    channel_id: str
    message_id: Optional[str]
    prompt: str
    future: asyncio.Future[ApprovalDecision]


class _DiscordBackendNotificationRouter:
    def __init__(
        self,
        *,
        notification_handler: Callable[[Mapping[str, object]], Awaitable[None]],
        approval_handler: Callable[[dict[str, Any]], Awaitable[ApprovalDecision]],
    ) -> None:
        self._notification_handler = notification_handler
        self.approval_handler = approval_handler

    async def __call__(self, payload: Mapping[str, object]) -> None:
        await self._notification_handler(payload)


class _DiscordAppServerSupervisorAdapter:
    def __init__(self, service: "DiscordBotService") -> None:
        self._service = service

    async def get_client(self, workspace_root: Path) -> CodexAppServerClient:
        canonical_root = canonicalize_path(Path(workspace_root))
        supervisor = await self._service._app_server_supervisor_for_workspace(
            canonical_root
        )
        return await supervisor.get_client(canonical_root)

    async def close_all(self) -> None:
        await self._service._close_all_app_server_supervisors()


class _DiscordOpenCodeSupervisorAdapter:
    def __init__(self, service: "DiscordBotService") -> None:
        self._service = service

    async def _resolve_supervisor(
        self, workspace_root: Path
    ) -> Optional[OpenCodeSupervisor]:
        canonical_root = canonicalize_path(Path(workspace_root))
        return await self._service._opencode_supervisor_for_workspace(canonical_root)

    async def get_client(self, workspace_root: Path) -> Any:
        canonical_root = canonicalize_path(Path(workspace_root))
        supervisor = await self._resolve_supervisor(canonical_root)
        if supervisor is None:
            raise RuntimeError("OpenCode supervisor unavailable")
        return await supervisor.get_client(canonical_root)

    async def get_client_for_turn(self, workspace_root: Path) -> Any:
        canonical_root = canonicalize_path(Path(workspace_root))
        supervisor = await self._resolve_supervisor(canonical_root)
        if supervisor is None:
            raise RuntimeError("OpenCode supervisor unavailable")
        getter = getattr(supervisor, "get_client_for_turn", None)
        if callable(getter):
            return await getter(canonical_root)
        client = await supervisor.get_client(canonical_root)
        await supervisor.mark_turn_started(canonical_root)
        return client

    async def backend_runtime_instance_id_for_workspace(
        self, workspace_root: Path
    ) -> Optional[str]:
        canonical_root = canonicalize_path(Path(workspace_root))
        supervisor = await self._resolve_supervisor(canonical_root)
        if supervisor is None:
            return None
        resolver = getattr(
            supervisor, "backend_runtime_instance_id_for_workspace", None
        )
        if not callable(resolver):
            return None
        runtime_instance_id = await resolver(canonical_root)
        if not isinstance(runtime_instance_id, str):
            return None
        normalized = runtime_instance_id.strip()
        return normalized or None

    async def session_stall_timeout_seconds_for_workspace(
        self, workspace_root: Path
    ) -> Optional[float]:
        canonical_root = canonicalize_path(Path(workspace_root))
        supervisor = await self._resolve_supervisor(canonical_root)
        if supervisor is None:
            return None
        return supervisor.session_stall_timeout_seconds

    async def mark_turn_started(self, workspace_root: Path) -> None:
        canonical_root = canonicalize_path(Path(workspace_root))
        supervisor = await self._resolve_supervisor(canonical_root)
        if supervisor is None:
            return
        await supervisor.mark_turn_started(canonical_root)

    async def mark_turn_finished(self, workspace_root: Path) -> None:
        canonical_root = canonicalize_path(Path(workspace_root))
        supervisor = await self._resolve_supervisor(canonical_root)
        if supervisor is None:
            return
        await supervisor.mark_turn_finished(canonical_root)

    async def close_all(self) -> None:
        await self._service._close_all_opencode_supervisors()


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
        self._collaboration_policy = (
            config.collaboration_policy
            or build_discord_collaboration_policy(
                allowed_guild_ids=config.allowed_guild_ids,
                allowed_channel_ids=config.allowed_channel_ids,
                allowed_user_ids=config.allowed_user_ids,
            )
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
        self._opencode_supervisors: dict[str, _OpenCodeSupervisorCacheEntry] = {}
        self._opencode_lock = asyncio.Lock()
        self.app_server_events = AppServerEventBuffer()
        self.app_server_supervisor = _DiscordAppServerSupervisorAdapter(self)
        self.opencode_supervisor: OpenCodeHarnessSupervisorProtocol = (
            _DiscordOpenCodeSupervisorAdapter(self)
        )
        self._opencode_prune_task: Optional[asyncio.Task[None]] = None
        self._filebox_prune_task: Optional[asyncio.Task[None]] = None
        self._app_server_state_root = resolve_global_state_root() / "workspaces"
        self._channel_directory_store = ChannelDirectoryStore(self._config.root)
        self._guild_name_cache: dict[str, str] = {}
        self._channel_name_cache: dict[str, str] = {}
        self._hub_raw_config_cache: Optional[dict[str, Any]] = None
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
            from ...integrations.github.polling import build_hub_scm_poll_processor

            self._hub_supervisor = HubSupervisor.from_path(
                self._config.root,
                scm_poll_processor=build_hub_scm_poll_processor(
                    hub_root=self._config.root,
                    raw_config=load_hub_config(self._config.root).raw,
                ),
            )
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
        self._pending_ticket_context: dict[str, dict[str, str]] = {}
        self._pending_ticket_filters: dict[str, str] = {}
        self._pending_ticket_search_queries: dict[str, str] = {}
        self._prepared_interaction_policies: OrderedDict[str, str] = OrderedDict()
        self._queued_notice_messages: dict[tuple[str, str], str] = {}
        self._discord_turn_progress_reuse_requests: dict[str, Any] = {}
        self._discord_reusable_progress_messages: dict[str, Any] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._typing_sessions: dict[str, int] = {}
        self._typing_tasks: dict[str, asyncio.Task[Any]] = {}
        self._typing_lock: Optional[asyncio.Lock] = None
        self._discord_turn_approval_contexts: dict[str, _DiscordTurnApprovalContext] = (
            {}
        )
        self._discord_pending_approvals: dict[str, _DiscordPendingApproval] = {}
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
        self._reap_managed_processes(stage="startup")
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
        self._opencode_prune_task = asyncio.create_task(self._run_opencode_prune_loop())
        if self._filebox_housekeeping_enabled():
            self._filebox_prune_task = asyncio.create_task(
                self._run_filebox_prune_loop()
            )
        pause_watch_task = asyncio.create_task(self._watch_ticket_flow_pauses())
        terminal_watch_task = asyncio.create_task(self._watch_ticket_flow_terminals())
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
            if self._opencode_prune_task is not None:
                self._opencode_prune_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._opencode_prune_task
                self._opencode_prune_task = None
            if self._filebox_prune_task is not None:
                self._filebox_prune_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._filebox_prune_task
                self._filebox_prune_task = None
            pause_watch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pause_watch_task
            terminal_watch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await terminal_watch_task
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
        async def _handle_dispatched_event(
            queued_event: ChatEvent, context: DispatchContext
        ) -> None:
            if isinstance(queued_event, ChatMessageEvent):
                await self._clear_queued_notice(
                    conversation_id=context.conversation_id,
                    source_message_id=queued_event.message.message_id,
                    channel_id=context.chat_id,
                )
            await self._handle_chat_event(queued_event, context)

        dispatch_result = await self._dispatcher.dispatch(
            event, _handle_dispatched_event
        )
        await self._maybe_send_queued_notice(event, dispatch_result)

    @staticmethod
    def _is_explicit_message_command(event: ChatMessageEvent) -> bool:
        text = (event.text or "").strip()
        if not text:
            return False
        if text.startswith("/"):
            return True
        return text.startswith("!")

    def _evaluate_message_collaboration_policy(
        self,
        event: ChatMessageEvent,
        *,
        is_explicit_command: bool,
    ) -> CollaborationEvaluationResult:
        text = compose_forwarded_message_text(event.text, event.forwarded_from)
        return evaluate_collaboration_policy(
            self._collaboration_policy,
            CollaborationEvaluationContext(
                actor_id=event.from_user_id,
                container_id=event.thread.thread_id,
                destination_id=event.thread.chat_id,
                is_explicit_command=is_explicit_command,
                plain_text=self._build_plain_text_turn_context(
                    text=text,
                    guild_id=event.thread.thread_id,
                    reply_to_is_bot=(
                        event.reply_context.is_bot
                        if event.reply_context is not None
                        else False
                    ),
                    reply_to_message_id=(
                        event.reply_context.message.message_id
                        if event.reply_context is not None
                        else None
                    ),
                ),
            ),
            plain_text_turn_fn=should_trigger_plain_text_turn,
        )

    def _build_plain_text_turn_context(
        self,
        *,
        text: str,
        guild_id: Optional[str],
        reply_to_is_bot: bool = False,
        reply_to_message_id: Optional[str] = None,
    ) -> PlainTextTurnContext:
        application_id = str(self._config.application_id or "").strip()
        normalized_text = text
        bot_username: Optional[str] = None
        if application_id:
            bot_username = "codexautorunner"
            normalized_text = normalized_text.replace(
                f"<@{application_id}>",
                f"@{bot_username}",
            ).replace(
                f"<@!{application_id}>",
                f"@{bot_username}",
            )
        return PlainTextTurnContext(
            text=normalized_text,
            chat_type="private" if guild_id is None else "group",
            bot_username=bot_username,
            reply_to_is_bot=reply_to_is_bot,
            reply_to_message_id=reply_to_message_id,
        )

    def _evaluate_plain_text_collaboration_policy(
        self,
        *,
        channel_id: Optional[str],
        guild_id: Optional[str],
        user_id: Optional[str],
        text: str,
    ) -> CollaborationEvaluationResult:
        return evaluate_collaboration_policy(
            self._collaboration_policy,
            CollaborationEvaluationContext(
                actor_id=user_id,
                container_id=guild_id,
                destination_id=channel_id,
                plain_text=self._build_plain_text_turn_context(
                    text=text,
                    guild_id=guild_id,
                ),
            ),
            plain_text_turn_fn=should_trigger_plain_text_turn,
        )

    def _evaluate_channel_collaboration_summary(
        self,
        *,
        channel_id: str,
        guild_id: Optional[str],
        user_id: Optional[str],
    ) -> tuple[CollaborationEvaluationResult, CollaborationEvaluationResult]:
        return (
            self._evaluate_interaction_collaboration_policy(
                channel_id=channel_id,
                guild_id=guild_id,
                user_id=user_id,
            ),
            self._evaluate_plain_text_collaboration_policy(
                channel_id=channel_id,
                guild_id=guild_id,
                user_id=user_id,
                text=collaboration_probe_text(self._config.application_id),
            ),
        )

    def _evaluate_context_admission(
        self,
        *,
        chat_id: Optional[str],
        guild_id: Optional[str],
        user_id: Optional[str],
    ) -> CollaborationEvaluationResult:
        return evaluate_collaboration_admission(
            self._collaboration_policy,
            CollaborationEvaluationContext(
                actor_id=user_id,
                container_id=guild_id,
                destination_id=chat_id,
            ),
        )

    def _evaluate_interaction_collaboration_policy(
        self,
        *,
        channel_id: Optional[str],
        guild_id: Optional[str],
        user_id: Optional[str],
    ) -> CollaborationEvaluationResult:
        return evaluate_collaboration_policy(
            self._collaboration_policy,
            CollaborationEvaluationContext(
                actor_id=user_id,
                container_id=guild_id,
                destination_id=channel_id,
                is_explicit_command=True,
            ),
        )

    def _log_collaboration_policy_result(
        self,
        *,
        channel_id: Optional[str],
        guild_id: Optional[str],
        user_id: Optional[str],
        message_id: Optional[str] = None,
        interaction_id: Optional[str] = None,
        result: CollaborationEvaluationResult,
    ) -> None:
        log_event(
            self._logger,
            logging.INFO,
            "discord.collaboration_policy.evaluated",
            channel_id=channel_id,
            guild_id=guild_id,
            user_id=user_id,
            message_id=message_id,
            interaction_id=interaction_id,
            **result.log_fields(),
        )

    def _is_turn_candidate_message_event(self, event: ChatMessageEvent) -> bool:
        text = (event.text or "").strip()
        has_attachments = bool(event.attachments)
        has_forwarded_content = event.forwarded_from is not None
        if not text and not has_attachments and not has_forwarded_content:
            return False
        if text.startswith("/"):
            return False
        result = self._evaluate_message_collaboration_policy(
            event,
            is_explicit_command=False,
        )
        return result.should_start_turn

    async def _can_start_message_turn_in_channel(self, event: ChatMessageEvent) -> bool:
        if not self._is_turn_candidate_message_event(event):
            return False
        binding, workspace_root = await resolve_bound_workspace_root(
            self,
            channel_id=event.thread.chat_id,
        )
        return binding is not None and workspace_root is not None

    def _dispatcher_conversation_id(
        self, *, channel_id: str, guild_id: Optional[str]
    ) -> str:
        return conversation_id_for("discord", channel_id, guild_id)

    async def _clear_queued_notice(
        self,
        *,
        conversation_id: str,
        source_message_id: str,
        channel_id: str,
    ) -> None:
        key = (conversation_id, source_message_id)
        notice_message_id = self._queued_notice_messages.pop(key, None)
        if not notice_message_id:
            return
        await self._delete_channel_message_safe(
            channel_id,
            notice_message_id,
            record_id=f"queue-notice-delete:{channel_id}:{source_message_id}",
        )

    async def _maybe_send_queued_notice(
        self, event: ChatEvent, dispatch_result: DispatchResult
    ) -> None:
        if dispatch_result.status != "queued" or not dispatch_result.queued_while_busy:
            return
        if not isinstance(event, ChatMessageEvent):
            return
        if not await self._can_start_message_turn_in_channel(event):
            return
        channel_id = dispatch_result.context.chat_id
        source_message_id = event.message.message_id
        try:
            response = await self._send_channel_message(
                channel_id,
                {
                    "content": format_discord_message(DISCORD_QUEUED_PLACEHOLDER_TEXT),
                    "components": [build_queue_notice_buttons(source_message_id)],
                },
            )
            notice_message_id = response.get("id")
            if isinstance(notice_message_id, str) and notice_message_id:
                self._queued_notice_messages[
                    (dispatch_result.context.conversation_id, source_message_id)
                ] = notice_message_id
        except Exception:
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
            await self._run_with_typing_indicator(
                channel_id=context.chat_id,
                work=lambda: self._handle_message_event(event, context),
            )
            return

    def _ensure_typing_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        lock = self._typing_lock
        lock_loop = getattr(lock, "_loop", None) if lock else None
        if (
            lock is None
            or lock_loop is None
            or lock_loop is not loop
            or lock_loop.is_closed()
        ):
            lock = asyncio.Lock()
            self._typing_lock = lock
        return lock

    async def _typing_session_active(self, channel_id: str) -> bool:
        lock = self._ensure_typing_lock()
        async with lock:
            return self._typing_sessions.get(channel_id, 0) > 0

    async def _typing_indicator_loop(self, channel_id: str) -> None:
        trigger_typing = getattr(self._rest, "trigger_typing", None)
        if not callable(trigger_typing):
            return
        try:
            while True:
                try:
                    await trigger_typing(channel_id=channel_id)
                except Exception as exc:
                    log_event(
                        self._logger,
                        logging.DEBUG,
                        "discord.typing.send.failed",
                        channel_id=channel_id,
                        exc=exc,
                    )
                await asyncio.sleep(DISCORD_TYPING_HEARTBEAT_INTERVAL_SECONDS)
                if not await self._typing_session_active(channel_id):
                    return
        finally:
            lock = self._ensure_typing_lock()
            async with lock:
                task = self._typing_tasks.get(channel_id)
                if task is asyncio.current_task():
                    self._typing_tasks.pop(channel_id, None)

    async def _begin_typing_indicator(self, channel_id: str) -> None:
        lock = self._ensure_typing_lock()
        async with lock:
            self._typing_sessions[channel_id] = (
                self._typing_sessions.get(channel_id, 0) + 1
            )
            task = self._typing_tasks.get(channel_id)
            if task is not None and not task.done():
                return
            typing_coro = self._typing_indicator_loop(channel_id)
            try:
                self._typing_tasks[channel_id] = self._spawn_task(typing_coro)
            except Exception:
                typing_coro.close()
                count = self._typing_sessions.get(channel_id, 0)
                if count <= 1:
                    self._typing_sessions.pop(channel_id, None)
                else:
                    self._typing_sessions[channel_id] = count - 1
                raise

    async def _end_typing_indicator(self, channel_id: str) -> None:
        task_to_cancel: Optional[asyncio.Task[Any]] = None
        lock = self._ensure_typing_lock()
        async with lock:
            count = self._typing_sessions.get(channel_id)
            if count is None:
                return
            if count > 1:
                self._typing_sessions[channel_id] = count - 1
                return
            self._typing_sessions.pop(channel_id, None)
            task_to_cancel = self._typing_tasks.pop(channel_id, None)
        if task_to_cancel is not None and not task_to_cancel.done():
            task_to_cancel.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task_to_cancel

    async def _run_with_typing_indicator(
        self,
        *,
        channel_id: Optional[str],
        work: Callable[[], Awaitable[None]],
    ) -> None:
        if not channel_id:
            await work()
            return
        began = False
        try:
            await self._begin_typing_indicator(channel_id)
            began = True
        except Exception as exc:
            log_event(
                self._logger,
                logging.DEBUG,
                "discord.typing.begin.failed",
                channel_id=channel_id,
                exc=exc,
            )
        try:
            await work()
        finally:
            if began:
                try:
                    await self._end_typing_indicator(channel_id)
                except Exception as exc:
                    log_event(
                        self._logger,
                        logging.DEBUG,
                        "discord.typing.end.failed",
                        channel_id=channel_id,
                        exc=exc,
                    )

    def _allowlist_predicate(self, event: ChatEvent, context: DispatchContext) -> bool:
        if isinstance(event, ChatInteractionEvent):
            # Interaction denials should return an ephemeral response rather than
            # being dropped at dispatcher level.
            return True
        result = self._evaluate_context_admission(
            chat_id=context.chat_id,
            guild_id=context.thread_id,
            user_id=context.user_id,
        )
        if not result.command_allowed:
            self._log_collaboration_policy_result(
                channel_id=context.chat_id,
                guild_id=context.thread_id,
                user_id=context.user_id,
                message_id=context.message_id,
                result=result,
            )
        return result.command_allowed

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

        policy_result = self._evaluate_interaction_collaboration_policy(
            channel_id=context.chat_id,
            guild_id=context.thread_id,
            user_id=context.user_id,
        )
        if not policy_result.command_allowed:
            self._log_collaboration_policy_result(
                channel_id=context.chat_id,
                guild_id=context.thread_id,
                user_id=context.user_id,
                interaction_id=interaction_id,
                result=policy_result,
            )
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
                message_id=context.message_id,
            )
            return

        if payload_data.get("type") == "modal_submit":
            custom_id_raw = payload_data.get("custom_id")
            modal_values_raw = payload_data.get("values")
            custom_id = custom_id_raw if isinstance(custom_id_raw, str) else ""
            modal_values = (
                modal_values_raw if isinstance(modal_values_raw, dict) else {}
            )
            await self._handle_ticket_modal_submit(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                custom_id=custom_id,
                values=modal_values,
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
                channel_id=channel_id,
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

        ingress = replace(
            ingress,
            command_path=self._normalize_discord_command_path(ingress.command_path),
        )
        await self._prepare_command_interaction(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            command_path=ingress.command_path,
            timing="dispatch",
        )

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
        event = await self._maybe_hydrate_reply_context(event)
        channel_id = context.chat_id
        text = (event.text or "").strip()
        has_attachments = bool(event.attachments)
        has_forwarded_content = event.forwarded_from is not None
        if not text and not has_attachments and not has_forwarded_content:
            return
        if text.startswith("/"):
            return
        if text.startswith("!") and not event.attachments:
            policy_result = self._evaluate_message_collaboration_policy(
                event,
                is_explicit_command=True,
            )
            if not policy_result.command_allowed:
                self._log_collaboration_policy_result(
                    channel_id=channel_id,
                    guild_id=context.thread_id,
                    user_id=event.from_user_id,
                    message_id=event.message.message_id,
                    result=policy_result,
                )
                return
            binding, workspace_root = await resolve_bound_workspace_root(
                self,
                channel_id=channel_id,
            )
            if binding is None:
                content = format_discord_message(
                    "This channel is not bound. Run `/car bind path:<workspace>` or `/pma on`."
                )
                await self._send_channel_message_safe(
                    channel_id,
                    {"content": content},
                )
                return
            if workspace_root is None:
                content = format_discord_message(
                    "Binding is invalid. Run `/car bind path:<workspace>`."
                )
                await self._send_channel_message_safe(
                    channel_id,
                    {"content": content},
                )
                return
            if not bool(binding.get("pma_enabled", False)):
                paused = await self._find_paused_flow_run(workspace_root)
                if paused is not None:
                    await handle_discord_message_event(
                        self,
                        event,
                        context,
                        channel_id=channel_id,
                        text=text,
                        has_attachments=has_attachments,
                        policy_result=policy_result,
                        log_event_fn=log_event,
                        build_ticket_flow_controller_fn=build_ticket_flow_controller,
                        ensure_worker_fn=ensure_worker,
                    )
                    return
            await self._handle_bang_shell(
                channel_id=channel_id,
                message_id=event.message.message_id,
                text=text,
                workspace_root=workspace_root,
            )
            return
        policy_result = self._evaluate_message_collaboration_policy(
            event,
            is_explicit_command=False,
        )
        if not policy_result.should_start_turn:
            self._log_collaboration_policy_result(
                channel_id=channel_id,
                guild_id=context.thread_id,
                user_id=event.from_user_id,
                message_id=event.message.message_id,
                result=policy_result,
            )
            return
        await handle_discord_message_event(
            self,
            event,
            context,
            channel_id=channel_id,
            text=text,
            has_attachments=has_attachments,
            policy_result=policy_result,
            log_event_fn=log_event,
            build_ticket_flow_controller_fn=build_ticket_flow_controller,
            ensure_worker_fn=ensure_worker,
        )

    async def _maybe_hydrate_reply_context(
        self,
        event: ChatMessageEvent,
    ) -> ChatMessageEvent:
        if event.reply_to is None or event.reply_context is not None:
            return event
        try:
            payload = await self._rest.get_channel_message(
                channel_id=event.reply_to.thread.chat_id,
                message_id=event.reply_to.message_id,
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.DEBUG,
                "discord.reply_context.fetch_failed",
                channel_id=event.thread.chat_id,
                reply_channel_id=event.reply_to.thread.chat_id,
                reply_message_id=event.reply_to.message_id,
                exc=exc,
            )
            return event
        if not isinstance(payload, dict):
            return event
        content = payload.get("content")
        text = content.strip() if isinstance(content, str) and content.strip() else None
        author = payload.get("author")
        author_label = None
        is_bot = False
        if isinstance(author, dict):
            for key in ("global_name", "username"):
                value = author.get(key)
                if isinstance(value, str) and value.strip():
                    author_label = value.strip()
                    break
            is_bot = bool(author.get("bot", False))
        return replace(
            event,
            reply_context=ChatReplyInfo(
                message=event.reply_to,
                text=text,
                author_label=author_label,
                is_bot=is_bot,
            ),
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
                (
                    transcript_text,
                    transcript_warning,
                ) = await self._transcribe_voice_attachment(
                    workspace_root=workspace_root,
                    channel_id=channel_id,
                    attachment=attachment,
                    data=data,
                    file_name=transcription_name,
                    mime_type=mime_type if isinstance(mime_type, str) else None,
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
            provider_name = (
                voice_config.provider.strip()
                if voice_config and isinstance(voice_config.provider, str)
                else ""
            )
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
            return select_ticket_flow_run_record(runs, selection="paused")
        except Exception:
            return None
        finally:
            store.close()

    def _is_user_ticket_pause(
        self, workspace_root: Path, record: FlowRunRecord
    ) -> bool:
        resolved_workspace_root = workspace_root.resolve()
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
        ticket_path = (resolved_workspace_root / current_ticket).resolve()
        if not ticket_path.is_file() or not is_within(
            root=resolved_workspace_root,
            target=ticket_path,
        ):
            return False
        ticket_doc, errors = read_ticket(ticket_path)
        if not errors and ticket_doc is not None:
            return ticket_doc.frontmatter.agent == "user" and not bool(
                ticket_doc.frontmatter.done
            )
        try:
            raw = ticket_path.read_text(encoding="utf-8")
        except OSError:
            return False
        data, _body = parse_markdown_frontmatter(raw)
        if not isinstance(data, dict):
            return False
        return str(data.get("agent") or "").strip().lower() == "user" and (
            data.get("done") is False
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
            return pma_base_key(agent)
        return file_chat_discord_key(agent, channel_id, str(workspace_root))

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

    def _discord_thread_service(self) -> Any:
        return build_discord_thread_orchestration_service(self)

    def _get_discord_thread_binding(
        self,
        *,
        channel_id: str,
        mode: Optional[str] = None,
    ) -> tuple[Any, Any, Any]:
        orchestration_service = self._discord_thread_service()
        binding = orchestration_service.get_binding(
            surface_kind="discord",
            surface_key=channel_id,
        )
        normalized_mode = (
            mode.strip().lower() if isinstance(mode, str) and mode.strip() else None
        )
        if binding is None:
            return orchestration_service, None, None
        if normalized_mode is not None:
            binding_mode = str(getattr(binding, "mode", "") or "").strip().lower()
            if binding_mode != normalized_mode:
                return orchestration_service, binding, None
        thread = orchestration_service.get_thread_target(binding.thread_target_id)
        return orchestration_service, binding, thread

    def _format_discord_thread_picker_label(
        self,
        thread: Any,
        *,
        is_current: bool,
    ) -> str:
        thread_id = str(getattr(thread, "thread_target_id", "") or "").strip()
        last_preview = str(getattr(thread, "last_message_preview", "") or "").strip()
        compact_seed = str(getattr(thread, "compact_seed", "") or "").strip()
        entry = {
            "created_at": getattr(thread, "created_at", None),
            "updated_at": getattr(thread, "updated_at", None),
            "status_changed_at": getattr(thread, "status_changed_at", None),
        }
        if _format_resume_timestamp(entry):
            label = _format_resume_picker_label(
                thread_id,
                entry,
                limit=100,
                fallback_preview=last_preview or compact_seed or None,
            )
        else:
            short_id = thread_id[:8] if thread_id else "unknown"
            agent = str(getattr(thread, "agent_id", "") or "").strip() or "agent"
            lifecycle_status = (
                str(getattr(thread, "lifecycle_status", "") or "").strip().lower()
            )
            display_name = str(getattr(thread, "display_name", "") or "").strip()
            base = display_name or f"{agent} {short_id}"
            parts = [base]
            if lifecycle_status and lifecycle_status not in {"active", "running"}:
                parts.append(lifecycle_status)
            if last_preview:
                preview = truncate_for_discord(last_preview, max_len=60)
                parts.append(preview)
            label = " · ".join(parts)
        if is_current:
            label = f"{label} (current)"
        return truncate_for_discord(label, max_len=100)

    async def _reset_discord_thread_binding(
        self,
        *,
        channel_id: str,
        workspace_root: Path,
        agent: str,
        repo_id: Optional[str],
        resource_kind: Optional[str],
        resource_id: Optional[str],
        pma_enabled: bool,
    ) -> tuple[bool, str]:
        mode = "pma" if pma_enabled else "repo"
        orchestration_service, _binding, current_thread = (
            self._get_discord_thread_binding(
                channel_id=channel_id,
                mode=mode,
            )
        )
        had_previous = current_thread is not None
        if current_thread is not None:
            log_event(
                self._logger,
                logging.INFO,
                "discord.thread.reset.stop_requested",
                channel_id=channel_id,
                mode=mode,
                thread_target_id=current_thread.thread_target_id,
            )
            stop_outcome = await orchestration_service.stop_thread(
                current_thread.thread_target_id
            )
            interrupted_active = bool(
                getattr(stop_outcome, "interrupted_active", False)
            )
            recovered_lost_backend = bool(
                getattr(stop_outcome, "recovered_lost_backend", False)
            )
            cancelled_queued = int(getattr(stop_outcome, "cancelled_queued", 0) or 0)
            execution_record = getattr(stop_outcome, "execution", None)
            log_event(
                self._logger,
                logging.INFO,
                "discord.thread.reset.stop_completed",
                channel_id=channel_id,
                mode=mode,
                thread_target_id=current_thread.thread_target_id,
                interrupted_active=interrupted_active,
                recovered_lost_backend=recovered_lost_backend,
                cancelled_queued=cancelled_queued,
                execution_id=(
                    execution_record.execution_id
                    if execution_record is not None
                    else None
                ),
                execution_status=(
                    execution_record.status if execution_record is not None else None
                ),
                execution_backend_turn_id=(
                    execution_record.backend_id
                    if execution_record is not None
                    else None
                ),
            )
            if recovered_lost_backend:
                log_event(
                    self._logger,
                    logging.INFO,
                    "discord.thread.recovered_lost_backend",
                    channel_id=channel_id,
                    thread_target_id=current_thread.thread_target_id,
                )
            orchestration_service.archive_thread_target(current_thread.thread_target_id)
        owner_kind, owner_id, normalized_repo_id = self._resource_owner_for_workspace(
            workspace_root,
            repo_id=repo_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
        )
        replacement = orchestration_service.create_thread_target(
            agent,
            workspace_root,
            repo_id=normalized_repo_id,
            resource_kind=owner_kind,
            resource_id=owner_id,
            display_name=f"discord:{channel_id}",
        )
        orchestration_service.upsert_binding(
            surface_kind="discord",
            surface_key=channel_id,
            thread_target_id=replacement.thread_target_id,
            agent_id=agent,
            repo_id=normalized_repo_id,
            resource_kind=owner_kind,
            resource_id=owner_id,
            mode=mode,
            metadata={"channel_id": channel_id, "pma_enabled": pma_enabled},
        )
        return had_previous, replacement.thread_target_id

    def _attach_discord_thread_binding(
        self,
        *,
        channel_id: str,
        thread_target_id: str,
        agent: str,
        repo_id: Optional[str],
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
        workspace_root: Optional[Path] = None,
        pma_enabled: bool,
    ) -> Any:
        mode = "pma" if pma_enabled else "repo"
        orchestration_service = self._discord_thread_service()
        owner_kind, owner_id, normalized_repo_id = self._resource_owner_for_workspace(
            workspace_root or Path(self._config.root),
            repo_id=repo_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
        )
        return orchestration_service.upsert_binding(
            surface_kind="discord",
            surface_key=channel_id,
            thread_target_id=thread_target_id,
            agent_id=agent,
            repo_id=normalized_repo_id,
            resource_kind=owner_kind,
            resource_id=owner_id,
            mode=mode,
            metadata={"channel_id": channel_id, "pma_enabled": pma_enabled},
        )

    def _list_discord_thread_targets_for_picker(
        self,
        *,
        workspace_root: Path,
        agent: str,
        current_thread_id: Optional[str],
        mode: str,
        repo_id: Optional[str] = None,
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
        limit: int = DISCORD_SELECT_OPTION_MAX_OPTIONS,
    ) -> list[tuple[str, str]]:
        orchestration_service = self._discord_thread_service()
        owner_kind, owner_id, normalized_repo_id = self._resource_owner_for_workspace(
            workspace_root,
            repo_id=repo_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
        )
        threads = orchestration_service.list_thread_targets(
            agent_id=agent,
            repo_id=normalized_repo_id,
            resource_kind=owner_kind,
            resource_id=owner_id,
            limit=max(limit * 4, limit),
        )
        canonical_workspace = str(workspace_root.resolve())
        filtered: list[Any] = []
        bound_modes_by_thread_id: dict[str, set[str]] = {}
        for binding in orchestration_service.list_bindings(
            agent_id=agent,
            repo_id=normalized_repo_id,
            resource_kind=owner_kind,
            resource_id=owner_id,
            surface_kind="discord",
            limit=max(limit * 8, limit),
        ):
            binding_thread_id = str(
                getattr(binding, "thread_target_id", "") or ""
            ).strip()
            binding_mode = str(getattr(binding, "mode", "") or "").strip()
            if not binding_thread_id or not binding_mode:
                continue
            bound_modes_by_thread_id.setdefault(binding_thread_id, set()).add(
                binding_mode
            )
        for thread in threads:
            thread_id = str(getattr(thread, "thread_target_id", "") or "").strip()
            if not thread_id:
                continue
            if (
                str(getattr(thread, "workspace_root", "") or "").strip()
                != canonical_workspace
            ):
                continue
            bound_modes = bound_modes_by_thread_id.get(thread_id)
            if (
                thread_id != current_thread_id
                and bound_modes
                and mode not in bound_modes
            ):
                continue
            filtered.append(thread)
        current_thread = None
        if isinstance(current_thread_id, str) and current_thread_id:
            current_thread = orchestration_service.get_thread_target(current_thread_id)
            if current_thread is not None and all(
                str(getattr(thread, "thread_target_id", "") or "").strip()
                != current_thread_id
                for thread in filtered
            ):
                filtered.insert(0, current_thread)
        items: list[tuple[str, str]] = []
        seen_ids: set[str] = set()
        for thread in filtered:
            thread_id = str(getattr(thread, "thread_target_id", "") or "").strip()
            if not thread_id or thread_id in seen_ids:
                continue
            seen_ids.add(thread_id)
            items.append(
                (
                    thread_id,
                    self._format_discord_thread_picker_label(
                        thread,
                        is_current=thread_id == current_thread_id,
                    ),
                )
            )
            if len(items) >= limit:
                break
        return items

    async def _orchestrator_for_workspace(
        self,
        workspace_root: Path,
        *,
        channel_id: str,
        agent_id: Optional[str] = None,
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
                shared_opencode_supervisor = None
                if agent_id == "opencode":
                    # Share the Discord service's workspace supervisor with the
                    # backend orchestrator so idle pruning sees the same
                    # active-turn bookkeeping as the running managed turn.
                    shared_opencode_supervisor = (
                        await self._opencode_supervisor_for_workspace(workspace_root)
                    )
                orchestrator = BackendOrchestrator(
                    repo_root=workspace_root,
                    config=repo_config,
                    notification_handler=_DiscordBackendNotificationRouter(
                        notification_handler=cast(
                            Callable[[Mapping[str, object]], Awaitable[None]],
                            self.app_server_events.handle_notification,
                        ),
                        approval_handler=self._handle_backend_approval_request,
                    ),
                    logger=self._logger,
                    shared_opencode_supervisor=shared_opencode_supervisor,
                )
            self._backend_orchestrators[key] = orchestrator
            return orchestrator

    def _register_discord_turn_approval_context(
        self, *, started_execution: Any, channel_id: str
    ) -> None:
        for turn_id in (
            str(getattr(started_execution.execution, "backend_id", "") or "").strip(),
            str(getattr(started_execution.execution, "execution_id", "") or "").strip(),
        ):
            if turn_id:
                self._discord_turn_approval_contexts[turn_id] = (
                    _DiscordTurnApprovalContext(channel_id=channel_id)
                )

    def _clear_discord_turn_approval_context(self, *, started_execution: Any) -> None:
        for turn_id in (
            str(getattr(started_execution.execution, "backend_id", "") or "").strip(),
            str(getattr(started_execution.execution, "execution_id", "") or "").strip(),
        ):
            if turn_id:
                self._discord_turn_approval_contexts.pop(turn_id, None)

    @staticmethod
    def _format_discord_approval_prompt(request: dict[str, Any]) -> str:
        method = request.get("method")
        params_value = request.get("params")
        params: dict[str, Any] = params_value if isinstance(params_value, dict) else {}
        lines = ["Approval required"]
        reason = params.get("reason")
        if isinstance(reason, str) and reason:
            lines.append(f"Reason: {reason}")
        if method == "item/commandExecution/requestApproval":
            command = params.get("command")
            if isinstance(command, list):
                command = " ".join(str(part) for part in command).strip()
            if isinstance(command, str) and command:
                lines.append(f"Command: {command}")
        elif method == "item/fileChange/requestApproval":
            files = params.get("paths")
            if isinstance(files, list):
                normalized = [str(path).strip() for path in files if str(path).strip()]
                if len(normalized) == 1:
                    lines.append(f"File: {normalized[0]}")
                elif normalized:
                    lines.append("Files:")
                    lines.extend(f"- {path}" for path in normalized[:10])
                    if len(normalized) > 10:
                        lines.append("- ...")
        return "\n".join(lines)

    @staticmethod
    def _build_discord_approval_components(token: str) -> list[dict[str, Any]]:
        return [
            build_action_row(
                [
                    build_button(
                        "Accept",
                        f"approval:{token}:accept",
                        style=DISCORD_BUTTON_STYLE_SUCCESS,
                    ),
                    build_button(
                        "Decline",
                        f"approval:{token}:decline",
                    ),
                ]
            ),
            build_action_row(
                [
                    build_button(
                        "Cancel",
                        f"approval:{token}:cancel",
                    )
                ]
            ),
        ]

    async def _handle_backend_approval_request(
        self, request: dict[str, Any]
    ) -> ApprovalDecision:
        request_data = normalize_backend_approval_request(request)
        if request_data is None:
            return "cancel"
        request_id = request_data.request_id
        turn_id = request_data.turn_id
        context = self._discord_turn_approval_contexts.get(turn_id)
        if context is None:
            return "cancel"

        loop = asyncio.get_running_loop()
        future: asyncio.Future[ApprovalDecision] = loop.create_future()
        token = uuid.uuid4().hex[:12]
        prompt = self._format_discord_approval_prompt(request)
        pending = _DiscordPendingApproval(
            token=token,
            request_id=request_id,
            turn_id=turn_id,
            channel_id=context.channel_id,
            message_id=None,
            prompt=prompt,
            future=future,
        )
        try:
            response = await self._send_channel_message(
                context.channel_id,
                {
                    "content": format_discord_message(prompt),
                    "components": self._build_discord_approval_components(token),
                },
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.approval.send_failed",
                channel_id=context.channel_id,
                request_id=request_id,
                turn_id=turn_id,
                exc=exc,
            )
            await self._send_channel_message_safe(
                context.channel_id,
                {
                    "content": format_discord_message(
                        "Approval prompt failed to send; canceling approval. Please retry."
                    )
                },
            )
            return "cancel"

        message_id = response.get("id")
        if isinstance(message_id, str) and message_id:
            pending.message_id = message_id
        self._discord_pending_approvals[token] = pending
        try:
            return await future
        finally:
            self._discord_pending_approvals.pop(token, None)

    async def _handle_approval_component(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        custom_id: str,
    ) -> None:
        _prefix, token, decision = (custom_id.split(":", 2) + ["", "", ""])[:3]
        pending = self._discord_pending_approvals.pop(token, None)
        if pending is None:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Approval already handled",
            )
            return
        if not pending.future.done():
            pending.future.set_result(decision)
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"Decision: {decision}",
        )
        if not pending.message_id:
            return
        try:
            await self._delete_channel_message(
                pending.channel_id,
                pending.message_id,
            )
        except Exception:
            with contextlib.suppress(Exception):
                await self._rest.edit_channel_message(
                    channel_id=pending.channel_id,
                    message_id=pending.message_id,
                    payload={
                        "content": format_discord_message(f"Approval {decision}."),
                        "components": [],
                    },
                )

    def _build_workspace_env(
        self, workspace_root: Path, workspace_id: str, state_dir: Path
    ) -> dict[str, str]:
        repo_config = load_repo_config(workspace_root, hub_path=self._hub_config_path)
        command = (
            list(repo_config.app_server.command)
            if repo_config and repo_config.app_server and repo_config.app_server.command
            else []
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
                list(repo_config.app_server.command)
                if repo_config
                and repo_config.app_server
                and repo_config.app_server.command
                else []
            )
            supervisor = WorkspaceAppServerSupervisor(
                command,
                state_root=self._app_server_state_root,
                env_builder=self._build_workspace_env,
                notification_handler=cast(
                    Callable[[Mapping[str, object]], Awaitable[None]],
                    self.app_server_events.handle_notification,
                ),
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
    ) -> Optional[OpenCodeSupervisor]:
        key = str(workspace_root)
        async with self._opencode_lock:
            existing = self._opencode_supervisors.get(key)
            if existing is not None:
                existing.last_requested_at = time.monotonic()
                return existing.supervisor
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
            self._opencode_supervisors[key] = _OpenCodeSupervisorCacheEntry(
                supervisor=supervisor,
                prune_interval_seconds=_opencode_prune_interval(
                    repo_config.opencode.idle_ttl_seconds
                ),
                last_requested_at=time.monotonic(),
            )
            return supervisor

    def _reap_managed_processes(self, *, stage: str) -> None:
        try:
            cleanup = reap_managed_processes(self._config.root)
            if cleanup.killed or cleanup.signaled or cleanup.removed:
                log_event(
                    self._logger,
                    logging.INFO,
                    "discord.process_reaper.cleaned",
                    stage=stage,
                    killed=cleanup.killed,
                    signaled=cleanup.signaled,
                    removed=cleanup.removed,
                    skipped=cleanup.skipped,
                )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.process_reaper.failed",
                stage=stage,
                exc=exc,
            )

    def _filebox_housekeeping_enabled(self) -> bool:
        try:
            repo_config = load_repo_config(
                self._config.root,
                hub_path=self._hub_config_path,
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.filebox.config_load_failed",
                repo_root=str(self._config.root),
                exc=exc,
            )
            return False
        return bool(repo_config.housekeeping.enabled)

    async def _next_opencode_prune_interval_seconds(self) -> float:
        async with self._opencode_lock:
            intervals = [
                entry.prune_interval_seconds
                for entry in self._opencode_supervisors.values()
                if entry.prune_interval_seconds is not None
            ]
        if intervals:
            return min(intervals)
        return DISCORD_OPENCODE_PRUNE_FALLBACK_INTERVAL_SECONDS

    async def _run_opencode_prune_loop(self) -> None:
        while True:
            await asyncio.sleep(await self._next_opencode_prune_interval_seconds())
            await self._prune_opencode_supervisors()

    async def _run_filebox_prune_loop(self) -> None:
        while True:
            await asyncio.sleep(await self._run_filebox_prune_cycle())

    async def _filebox_prune_roots(self) -> list[Path]:
        roots: set[Path] = {self._config.root.resolve()}
        try:
            bindings = await self._store.list_bindings()
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.filebox.bindings_load_failed",
                repo_root=str(self._config.root),
                exc=exc,
            )
            return sorted(roots)
        for binding in bindings:
            workspace_raw = binding.get("workspace_path")
            if not isinstance(workspace_raw, str) or not workspace_raw.strip():
                continue
            workspace_root = canonicalize_path(Path(workspace_raw))
            if workspace_root.exists() and workspace_root.is_dir():
                roots.add(workspace_root)
        return sorted(roots)

    async def _run_filebox_prune_cycle(self) -> float:
        interval_seconds = 3600.0
        roots = await self._filebox_prune_roots()
        for root in roots:
            try:
                repo_config = load_repo_config(
                    root,
                    hub_path=self._hub_config_path,
                )
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.filebox.config_load_failed",
                    repo_root=str(root),
                    exc=exc,
                )
                continue
            interval_seconds = min(
                interval_seconds,
                float(max(repo_config.housekeeping.interval_seconds, 1)),
            )
            try:
                summary = await asyncio.to_thread(
                    prune_filebox_root,
                    root,
                    policy=resolve_filebox_retention_policy(repo_config.pma),
                )
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.filebox.cleanup_failed",
                    repo_root=str(root),
                    exc=exc,
                )
                continue
            if summary.inbox_pruned or summary.outbox_pruned:
                log_event(
                    self._logger,
                    logging.INFO,
                    "discord.filebox.cleanup",
                    repo_root=str(root),
                    inbox_pruned=summary.inbox_pruned,
                    outbox_pruned=summary.outbox_pruned,
                    bytes_before=summary.bytes_before,
                    bytes_after=summary.bytes_after,
                )
        return interval_seconds

    async def _prune_opencode_supervisors(self) -> None:
        async with self._opencode_lock:
            cached_entries = list(self._opencode_supervisors.items())
        cached_supervisors = len(cached_entries)
        if not cached_entries:
            log_event(
                self._logger,
                logging.DEBUG,
                "discord.opencode.prune_sweep",
                cached_supervisors=0,
                cached_supervisors_after=0,
                live_handles=0,
                killed_processes=0,
                evicted_supervisors=0,
            )
            return

        now = time.monotonic()
        live_handles = 0
        killed_processes = 0
        eviction_candidates: list[tuple[str, _OpenCodeSupervisorCacheEntry]] = []

        for workspace_path, entry in cached_entries:
            workspace_root = canonicalize_path(Path(workspace_path))
            execution_running = self._workspace_has_running_opencode_execution(
                workspace_root
            )
            if execution_running is not False:
                entry.last_requested_at = now
                try:
                    snapshot = await entry.supervisor.lifecycle_snapshot()
                except Exception as exc:
                    log_event(
                        self._logger,
                        logging.WARNING,
                        "discord.opencode.prune_failed",
                        workspace_path=workspace_path,
                        exc=exc,
                    )
                else:
                    live_handles += snapshot.cached_handles
                log_event(
                    self._logger,
                    logging.DEBUG,
                    "discord.opencode.prune_deferred",
                    workspace_path=workspace_path,
                    reason=(
                        "active_runtime_execution"
                        if execution_running
                        else "execution_state_unknown"
                    ),
                )
                continue
            try:
                killed_processes += await entry.supervisor.prune_idle()
                snapshot = await entry.supervisor.lifecycle_snapshot()
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.opencode.prune_failed",
                    workspace_path=workspace_path,
                    exc=exc,
                )
                continue
            live_handles += snapshot.cached_handles
            idle_for = max(0.0, now - entry.last_requested_at)
            eviction_delay = (
                entry.prune_interval_seconds
                or DISCORD_OPENCODE_PRUNE_FALLBACK_INTERVAL_SECONDS
            )
            if snapshot.cached_handles == 0 and idle_for >= eviction_delay:
                eviction_candidates.append((workspace_path, entry))

        evicted_supervisors = 0
        evicted_objects: list[OpenCodeSupervisor] = []
        if eviction_candidates:
            async with self._opencode_lock:
                for workspace_path, entry in eviction_candidates:
                    current = self._opencode_supervisors.get(workspace_path)
                    if current is not entry:
                        continue
                    self._opencode_supervisors.pop(workspace_path, None)
                    evicted_supervisors += 1
                    evicted_objects.append(entry.supervisor)
            for supervisor in evicted_objects:
                with contextlib.suppress(Exception):
                    await supervisor.close_all()

        async with self._opencode_lock:
            cached_supervisors_after = len(self._opencode_supervisors)
        log_event(
            self._logger,
            logging.DEBUG,
            "discord.opencode.prune_sweep",
            cached_supervisors=cached_supervisors,
            cached_supervisors_after=cached_supervisors_after,
            live_handles=live_handles,
            killed_processes=killed_processes,
            evicted_supervisors=evicted_supervisors,
        )

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
        deferred: bool = False,
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
            await self._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=f"No ticket_flow runs available for {flow_action_label(action)}.",
            )
            return
        run_tuples = [(record.id, record.status.value) for record in filtered]
        custom_id = f"{FLOW_ACTION_SELECT_PREFIX}:{action}"
        prompt = f"Select a run to {flow_action_label(action)}:"
        await self._send_or_respond_with_components_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=prompt,
            components=[
                build_flow_runs_picker(
                    run_tuples,
                    custom_id=custom_id,
                    placeholder=f"Select run to {flow_action_label(action)}...",
                )
            ],
        )

    async def _resolve_flow_run_input(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        action: str,
        run_id_opt: Any,
        deferred: bool = False,
    ) -> Optional[str]:
        if not (isinstance(run_id_opt, str) and run_id_opt.strip()):
            if action == "status":
                return ""
            await self._prompt_flow_action_picker(
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
                action=action,
                deferred=deferred,
            )
            return None

        run_id_value = run_id_opt.strip()
        try:
            store = self._open_flow_store(workspace_root)
        except (sqlite3.Error, OSError, RuntimeError):
            return run_id_value
        try:
            runs = store.list_flow_runs(flow_type="ticket_flow")
        except (sqlite3.Error, OSError):
            return run_id_value
        finally:
            store.close()

        matching_runs = [run for run in runs if _flow_run_matches_action(run, action)]
        if not matching_runs:
            return run_id_value

        items = [(record.id, record.status.value) for record in matching_runs]
        search_items = [(record.id, record.id) for record in matching_runs]
        aliases = {record.id: (record.status.value,) for record in matching_runs}
        custom_id = f"{FLOW_ACTION_SELECT_PREFIX}:{action}"

        async def _prompt_run_matches(
            query_text: str,
            filtered_search_items: list[tuple[str, str]],
        ) -> None:
            status_by_run_id = {run_id: status for run_id, status in items}
            filtered_items = [
                (run_id, status_by_run_id.get(run_id, ""))
                for run_id, _label in filtered_search_items
            ]
            prompt = (
                f"Matched {len(filtered_items)} runs for `{query_text}`. "
                f"Select a run to {flow_action_label(action)}:"
            )
            await self._send_or_respond_with_components_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=prompt,
                components=[
                    build_flow_runs_picker(
                        filtered_items,
                        custom_id=custom_id,
                        placeholder=f"Select run to {flow_action_label(action)}...",
                    )
                ],
            )

        resolved_run_id = await self._resolve_picker_query_or_prompt(
            query=run_id_value,
            items=search_items,
            limit=DISCORD_SELECT_OPTION_MAX_OPTIONS,
            aliases=aliases,
            prompt_filtered_items=_prompt_run_matches,
        )
        if resolved_run_id is None:
            return None
        return resolved_run_id

    @staticmethod
    def _flow_archive_prompt_text(record: FlowRunRecord) -> str:
        return (
            f"Run {record.id} is {record.status.value}. "
            "Archiving it will reset the live tickets/contextspace state and move the "
            "current run artifacts into the archive. Archive it anyway?"
        )

    @staticmethod
    def _build_flow_archive_confirmation_components(
        run_id: str,
        *,
        prompt_variant: bool,
    ) -> list[dict[str, Any]]:
        confirm_action = (
            "archive_confirm_prompt" if prompt_variant else "archive_confirm"
        )
        cancel_action = "archive_cancel_prompt" if prompt_variant else "archive_cancel"
        return [
            build_action_row(
                [
                    build_button(
                        "Archive now",
                        f"flow:{run_id}:{confirm_action}",
                        style=DISCORD_BUTTON_STYLE_DANGER,
                    ),
                    build_button("Cancel", f"flow:{run_id}:{cancel_action}"),
                ]
            )
        ]

    async def _run_agent_turn_for_message(
        self,
        *,
        workspace_root: Path,
        prompt_text: str,
        input_items: Optional[list[dict[str, Any]]] = None,
        source_message_id: Optional[str] = None,
        agent: str,
        model_override: Optional[str],
        reasoning_effort: Optional[str],
        session_key: str,
        orchestrator_channel_key: str,
    ) -> DiscordMessageTurnResult:
        async def _run_turn() -> DiscordMessageTurnResult:
            if orchestrator_channel_key.startswith("pma:"):
                managed_turn_kwargs: dict[str, Any] = {
                    "workspace_root": workspace_root,
                    "prompt_text": prompt_text,
                    "input_items": input_items,
                    "agent": agent,
                    "model_override": model_override,
                    "reasoning_effort": reasoning_effort,
                    "session_key": session_key,
                    "orchestrator_channel_key": orchestrator_channel_key,
                }
                try:
                    if (
                        "source_message_id"
                        in inspect.signature(
                            run_managed_thread_turn_for_message
                        ).parameters
                    ):
                        managed_turn_kwargs["source_message_id"] = source_message_id
                except (TypeError, ValueError):
                    pass
                return await run_managed_thread_turn_for_message(
                    self,
                    **managed_turn_kwargs,
                )
            repo_turn_kwargs: dict[str, Any] = {
                "workspace_root": workspace_root,
                "prompt_text": prompt_text,
                "input_items": input_items,
                "agent": agent,
                "model_override": model_override,
                "reasoning_effort": reasoning_effort,
                "session_key": session_key,
                "orchestrator_channel_key": orchestrator_channel_key,
                "max_actions": DISCORD_TURN_PROGRESS_MAX_ACTIONS,
                "min_edit_interval_seconds": DISCORD_TURN_PROGRESS_MIN_EDIT_INTERVAL_SECONDS,
                "heartbeat_interval_seconds": DISCORD_TURN_PROGRESS_HEARTBEAT_INTERVAL_SECONDS,
                "log_event_fn": log_event,
            }
            try:
                if (
                    "source_message_id"
                    in inspect.signature(run_agent_turn_for_message).parameters
                ):
                    repo_turn_kwargs["source_message_id"] = source_message_id
            except (TypeError, ValueError):
                pass
            return await run_agent_turn_for_message(
                self,
                **repo_turn_kwargs,
            )

        turn_result: Optional[DiscordMessageTurnResult] = None

        async def _wrapped() -> None:
            nonlocal turn_result
            turn_result = await _run_turn()

        resolved_channel_id = (
            orchestrator_channel_key[4:]
            if orchestrator_channel_key.startswith("pma:")
            else orchestrator_channel_key
        )
        await self._run_with_typing_indicator(
            channel_id=resolved_channel_id or None,
            work=_wrapped,
        )
        if turn_result is None:
            raise RuntimeError("Discord turn finished without a result")
        return turn_result

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
        await dispatch_car_command(
            self,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            guild_id=guild_id,
            user_id=user_id,
            command_path=command_path,
            options=options,
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

        commands = build_application_commands(self)
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
        await self._close_all_app_server_supervisors()
        await self._close_all_opencode_supervisors()
        self._reap_managed_processes(stage="shutdown")

    async def _close_all_app_server_supervisors(self) -> None:
        async with self._app_server_lock:
            supervisors = list(self._app_server_supervisors.values())
            self._app_server_supervisors.clear()
        for supervisor in supervisors:
            with contextlib.suppress(Exception):
                await supervisor.close_all()

    async def _close_all_opencode_supervisors(self) -> None:
        async with self._opencode_lock:
            opencode_supervisors = [
                entry.supervisor for entry in self._opencode_supervisors.values()
            ]
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
        preferred_sources = self._preferred_bound_sources_by_workspace()
        for binding in bindings:
            channel_id = binding.get("channel_id")
            workspace_raw = binding.get("workspace_path")
            if not isinstance(channel_id, str) or not isinstance(workspace_raw, str):
                continue
            workspace_root = canonicalize_path(Path(workspace_raw))
            preferred_source = preferred_sources.get(str(workspace_root))
            if preferred_source is None:
                preferred_source = self._preferred_bound_source_for_workspace(
                    workspace_root
                )
            if preferred_source == "telegram":
                continue
            run_mirror = self._flow_run_mirror(workspace_root)
            snapshots = await asyncio.to_thread(
                list_unseen_ticket_flow_dispatches,
                workspace_root,
                last_run_id=binding.get("last_dispatch_run_id"),
                last_dispatch_seq=binding.get("last_dispatch_seq"),
            )
            if not snapshots:
                continue

            for snapshot in snapshots:
                content = self._format_ticket_flow_dispatch_notification(
                    run_id=snapshot.run_id,
                    dispatch_seq=snapshot.dispatch_seq,
                    content=snapshot.dispatch_markdown,
                    source=self._format_pause_notification_source(
                        workspace_root=workspace_root,
                        repo_id=binding.get("repo_id"),
                    ),
                    allow_resume_hint=snapshot.allow_resume_hint,
                    is_handoff=snapshot.is_handoff,
                )
                chunks = chunk_discord_message(
                    content,
                    max_len=self._config.max_message_length,
                    with_numbering=False,
                )
                if not chunks:
                    chunks = ["(dispatch notification had no content)"]

                record_prefix = "pause" if snapshot.is_handoff else "dispatch"
                event_type = (
                    "flow_pause_dispatch_notice"
                    if snapshot.is_handoff
                    else "flow_dispatch_notice"
                )
                enqueued = True
                for index, chunk in enumerate(chunks, start=1):
                    record_id = (
                        f"{record_prefix}:{channel_id}:{snapshot.run_id}:"
                        f"{snapshot.dispatch_seq}:{index}"
                    )
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
                            event_type=event_type,
                            kind="dispatch",
                            actor="car",
                            text=chunk,
                            chat_id=channel_id,
                            thread_id=binding.get("guild_id"),
                            message_id=record_id,
                            meta={
                                "dispatch_seq": snapshot.dispatch_seq,
                                "chunk_index": index,
                                "mode": snapshot.mode,
                            },
                        )
                    except Exception as exc:
                        enqueued = False
                        log_event(
                            self._logger,
                            logging.WARNING,
                            "discord.dispatch_watch.enqueue_failed",
                            exc=exc,
                            channel_id=channel_id,
                            run_id=snapshot.run_id,
                            dispatch_seq=snapshot.dispatch_seq,
                            mode=snapshot.mode,
                        )
                        break

                if not enqueued:
                    break

                await self._store.mark_dispatch_seen(
                    channel_id=channel_id,
                    run_id=snapshot.run_id,
                    dispatch_seq=snapshot.dispatch_seq,
                )
                if snapshot.is_handoff:
                    await self._store.mark_pause_dispatch_seen(
                        channel_id=channel_id,
                        run_id=snapshot.run_id,
                        dispatch_seq=snapshot.dispatch_seq,
                    )
                log_event(
                    self._logger,
                    logging.INFO,
                    "discord.dispatch_watch.notified",
                    channel_id=channel_id,
                    run_id=snapshot.run_id,
                    dispatch_seq=snapshot.dispatch_seq,
                    mode=snapshot.mode,
                    chunk_count=len(chunks),
                )

    def _format_pause_notification_source(
        self, *, workspace_root: Optional[Path], repo_id: Optional[str]
    ) -> str:
        return format_pause_notification_source(
            workspace_root=workspace_root,
            repo_id=repo_id,
            hub_root=self._config.root,
            manifest_path=self._manifest_path,
            logger=self._logger,
            debug_label="discord.pause_watch.manifest_label_failed",
        )

    def _format_ticket_flow_dispatch_notification(
        self,
        *,
        run_id: str,
        dispatch_seq: str,
        content: str,
        source: Optional[str],
        allow_resume_hint: bool,
        is_handoff: bool,
    ) -> str:
        if is_handoff:
            return format_pause_notification_text(
                run_id=run_id,
                dispatch_seq=dispatch_seq,
                content=content,
                source=source,
                resume_hint="`/flow resume`" if allow_resume_hint else "",
            )
        body = content.strip() or "(no dispatch message)"
        header_lines = [
            f"Ticket flow dispatch (run {run_id}). Dispatch #{dispatch_seq}:"
        ]
        if isinstance(source, str) and source.strip():
            header_lines.append(f"Source: {source.strip()}")
        return "\n\n".join(("\n".join(header_lines), body))

    def _preferred_bound_source_for_workspace(self, workspace_root: Path) -> str | None:
        raw_config = self._hub_raw_config_cache
        if raw_config is None:
            try:
                raw_config = load_hub_config(self._config.root).raw
            except Exception:
                raw_config = {}
            self._hub_raw_config_cache = raw_config
        try:
            return preferred_non_pma_chat_notification_source_for_workspace(
                hub_root=self._config.root,
                raw_config=raw_config,
                workspace_root=workspace_root,
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.pause_watch.route_lookup_failed",
                exc=exc,
                workspace_root=str(workspace_root),
            )
            return None

    def _preferred_bound_sources_by_workspace(self) -> dict[str, str]:
        raw_config = self._hub_raw_config_cache
        if raw_config is None:
            try:
                raw_config = load_hub_config(self._config.root).raw
            except Exception:
                raw_config = {}
            self._hub_raw_config_cache = raw_config
        try:
            return preferred_non_pma_chat_notification_sources_by_workspace(
                hub_root=self._config.root,
                raw_config=raw_config,
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.pause_watch.route_lookup_failed",
                exc=exc,
            )
            return {}

    async def _watch_ticket_flow_terminals(self) -> None:
        while True:
            try:
                await self._scan_and_enqueue_terminal_notifications()
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.terminal_watch.scan_failed",
                    exc=exc,
                )
            await asyncio.sleep(TERMINAL_SCAN_INTERVAL_SECONDS)

    async def _scan_and_enqueue_terminal_notifications(self) -> None:
        bindings = await self._store.list_bindings()
        for binding in bindings:
            channel_id = binding.get("channel_id")
            workspace_raw = binding.get("workspace_path")
            if not isinstance(channel_id, str) or not isinstance(workspace_raw, str):
                continue
            workspace_root = canonicalize_path(Path(workspace_raw))
            terminal_run = await asyncio.to_thread(
                self._load_latest_terminal_ticket_flow_run, workspace_root
            )
            if terminal_run is None:
                continue
            run_id, status, error_message = terminal_run
            if binding.get("last_terminal_run_id") == run_id:
                continue
            message = self._format_terminal_notification(
                run_id=run_id, status=status, error_message=error_message
            )
            record_id = f"terminal:{channel_id}:{run_id}"
            try:
                await self._store.enqueue_outbox(
                    OutboxRecord(
                        record_id=record_id,
                        channel_id=channel_id,
                        message_id=None,
                        operation="send",
                        payload_json={"content": message},
                    )
                )
                run_mirror = self._flow_run_mirror(workspace_root)
                run_mirror.mirror_outbound(
                    run_id=run_id,
                    platform="discord",
                    event_type="flow_terminal_notice",
                    kind="notification",
                    actor="car",
                    text=message,
                    chat_id=channel_id,
                    message_id=record_id,
                    meta={"status": status},
                )
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.terminal_watch.enqueue_failed",
                    exc=exc,
                    channel_id=channel_id,
                    run_id=run_id,
                )
                continue
            await self._store.mark_terminal_run_seen(
                channel_id=channel_id, run_id=run_id
            )
            log_event(
                self._logger,
                logging.INFO,
                "discord.terminal_watch.notified",
                channel_id=channel_id,
                run_id=run_id,
                status=status,
            )

    def _load_latest_terminal_ticket_flow_run(
        self, workspace_root: Path
    ) -> Optional[tuple[str, str, Optional[str]]]:
        db_path = workspace_root / ".codex-autorunner" / "flows.db"
        if not db_path.exists():
            return None
        config = load_repo_config(workspace_root)
        terminal_statuses = (
            FlowRunStatus.COMPLETED,
            FlowRunStatus.FAILED,
            FlowRunStatus.STOPPED,
        )
        latest_run: Optional[FlowRunRecord] = None
        with FlowStore(db_path, durable=config.durable_writes) as store:
            for status in terminal_statuses:
                runs = store.list_flow_runs(flow_type="ticket_flow", status=status)
                for run in runs:
                    if latest_run is None:
                        latest_run = run
                    elif run.finished_at and latest_run.finished_at:
                        if run.finished_at > latest_run.finished_at:
                            latest_run = run
                    elif run.created_at > latest_run.created_at:
                        latest_run = run
        if latest_run is None:
            return None
        return (latest_run.id, latest_run.status.value, latest_run.error_message)

    def _format_terminal_notification(
        self, *, run_id: str, status: str, error_message: Optional[str]
    ) -> str:
        if status == FlowRunStatus.COMPLETED.value:
            return f"Ticket flow completed successfully (run {run_id})."
        elif status == FlowRunStatus.FAILED.value:
            error_text = self._truncate_error(error_message)
            return f"Ticket flow failed (run {run_id}). Error: {error_text}"
        elif status == FlowRunStatus.STOPPED.value:
            return f"Ticket flow stopped (run {run_id})."
        return f"Ticket flow ended (run {run_id}, status: {status})."

    def _truncate_error(self, error_message: Optional[str], limit: int = 200) -> str:
        if not error_message:
            return "Unknown error"
        normalized = " ".join(error_message.split())
        if len(normalized) > limit:
            return f"{normalized[: limit - 3]}..."
        return normalized

    async def _send_channel_message(
        self, channel_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        payload = dict(payload)
        content = payload.get("content")
        if isinstance(content, str):
            payload["content"] = sanitize_discord_outbound_text(content)
        return await self._rest.create_channel_message(
            channel_id=channel_id, payload=payload
        )

    @staticmethod
    def _sanitize_outgoing_channel_payload(payload: dict[str, Any]) -> dict[str, Any]:
        sanitized = dict(payload)
        content = sanitized.get("content")
        if isinstance(content, str):
            sanitized["content"] = collapse_local_markdown_links(content)
        return sanitized

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
        task = cast(asyncio.Task[Any], asyncio.ensure_future(coro))
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
            interaction_event = self._chat_adapter.parse_interaction_event(payload)
            if interaction_event is not None:
                await self._dispatcher.dispatch(
                    interaction_event, self._handle_chat_event
                )
        elif event_type == "MESSAGE_CREATE":
            await self._record_channel_directory_seen_from_message_payload(payload)
            message_event = self._chat_adapter.parse_message_event(payload)
            if message_event is not None:
                await self._dispatch_chat_event(message_event)

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
        if is_modal_submit_interaction(interaction_payload):
            await self._handle_modal_submit_interaction(interaction_payload)
            return
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

        policy_result = self._evaluate_interaction_collaboration_policy(
            channel_id=channel_id,
            guild_id=guild_id,
            user_id=extract_user_id(interaction_payload),
        )
        if not policy_result.command_allowed:
            self._log_collaboration_policy_result(
                channel_id=channel_id,
                guild_id=guild_id,
                user_id=extract_user_id(interaction_payload),
                interaction_id=interaction_id,
                result=policy_result,
            )
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

        ingress = replace(
            ingress,
            command_path=self._normalize_discord_command_path(ingress.command_path),
        )
        await self._prepare_command_interaction(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            command_path=ingress.command_path,
            timing="dispatch",
        )

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

        policy_result = self._evaluate_interaction_collaboration_policy(
            channel_id=channel_id,
            guild_id=extract_guild_id(interaction_payload),
            user_id=extract_user_id(interaction_payload),
        )
        if not policy_result.command_allowed:
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
            channel_id=channel_id,
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

        candidates = self._list_bind_workspace_candidates()
        if not candidates:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "No workspaces found. Use /car bind workspace:<workspace> to bind manually.",
            )
            return

        prompt, components = self._build_bind_page_prompt_and_components(
            candidates, page=0
        )
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

    def _list_agent_workspaces(self) -> list[tuple[str, str, str]]:
        supervisor = getattr(self, "_hub_supervisor", None)
        if supervisor is None:
            return []
        try:
            snapshots = supervisor.list_agent_workspaces()
        except Exception:
            return []
        workspaces: list[tuple[str, str, str]] = []
        for snapshot in snapshots:
            workspace_id = str(getattr(snapshot, "id", "") or "").strip()
            workspace_path = getattr(snapshot, "path", None)
            if isinstance(workspace_path, str):
                workspace_path = Path(workspace_path)
            if not workspace_id or not isinstance(workspace_path, Path):
                continue
            display_name = str(
                getattr(snapshot, "display_name", "") or workspace_id
            ).strip()
            workspaces.append(
                (workspace_id, str(canonicalize_path(workspace_path)), display_name)
            )
        workspaces.sort(key=lambda item: (item[2].lower(), item[0]))
        return workspaces

    def _resource_owner_for_workspace(
        self,
        workspace_root: Path,
        *,
        repo_id: Optional[str] = None,
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        normalized_repo_id = (
            repo_id.strip() if isinstance(repo_id, str) and repo_id.strip() else None
        )
        normalized_resource_kind = (
            resource_kind.strip()
            if isinstance(resource_kind, str) and resource_kind.strip()
            else None
        )
        normalized_resource_id = (
            resource_id.strip()
            if isinstance(resource_id, str) and resource_id.strip()
            else None
        )
        if normalized_resource_kind == "repo" and normalized_resource_id:
            return "repo", normalized_resource_id, normalized_resource_id
        if normalized_resource_kind == "agent_workspace" and normalized_resource_id:
            return "agent_workspace", normalized_resource_id, None
        if normalized_repo_id:
            return "repo", normalized_repo_id, normalized_repo_id

        canonical_workspace = str(canonicalize_path(workspace_root))
        for (
            workspace_id,
            workspace_path,
            _display_name,
        ) in self._list_agent_workspaces():
            if workspace_path == canonical_workspace:
                return "agent_workspace", workspace_id, None
        for listed_repo_id, listed_path in self._list_manifest_repos():
            if str(canonicalize_path(Path(listed_path))) == canonical_workspace:
                return "repo", listed_repo_id, listed_repo_id
        return None, None, None

    def _list_bind_workspace_candidates(
        self,
    ) -> list[tuple[Optional[str], Optional[str], str]]:
        candidates: list[tuple[Optional[str], Optional[str], str]] = []
        manifest_paths: set[str] = set()

        for repo_id, path in self._list_manifest_repos():
            normalized_path = str(canonicalize_path(Path(path)))
            candidates.append(("repo", repo_id, normalized_path))
            manifest_paths.add(normalized_path)

        for (
            workspace_id,
            workspace_path,
            _display_name,
        ) in self._list_agent_workspaces():
            if workspace_path in manifest_paths:
                continue
            candidates.append(("agent_workspace", workspace_id, workspace_path))
            manifest_paths.add(workspace_path)

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
                candidates.append((None, None, normalized_path))
        except Exception:
            pass

        return candidates

    @staticmethod
    def _bind_candidate_value(
        resource_kind: Optional[str],
        resource_id: Optional[str],
        workspace_path: str,
    ) -> str:
        if resource_kind == "repo" and isinstance(resource_id, str) and resource_id:
            return repo_autocomplete_value(resource_id)
        if (
            resource_kind == "agent_workspace"
            and isinstance(resource_id, str)
            and resource_id
        ):
            return agent_workspace_autocomplete_value(resource_id)
        return workspace_autocomplete_value(workspace_path)

    @staticmethod
    def _bind_candidate_label(
        resource_kind: Optional[str],
        resource_id: Optional[str],
        workspace_path: str,
    ) -> str:
        if isinstance(resource_id, str) and resource_id:
            return resource_id
        return Path(workspace_path).name or workspace_path

    def _build_bind_picker_items(
        self,
        candidates: list[tuple[Optional[str], Optional[str], str]],
    ) -> list[tuple[str, str] | tuple[str, str, Optional[str]]]:
        items: list[tuple[str, str] | tuple[str, str, Optional[str]]] = []
        for resource_kind, resource_id, workspace_path in candidates:
            value = self._bind_candidate_value(
                resource_kind, resource_id, workspace_path
            )
            label = self._bind_candidate_label(
                resource_kind, resource_id, workspace_path
            )
            description = workspace_path
            if resource_kind == "agent_workspace":
                description = f"agent workspace · {workspace_path}"
            items.append((value, label, description))
        return items

    def _build_bind_search_items(
        self,
        candidates: list[tuple[Optional[str], Optional[str], str]],
    ) -> tuple[
        list[tuple[str, str]],
        dict[str, tuple[str, ...]],
        dict[str, tuple[str, ...]],
    ]:
        search_items: list[tuple[str, str]] = []
        exact_aliases: dict[str, tuple[str, ...]] = {}
        filter_aliases: dict[str, tuple[str, ...]] = {}
        for resource_kind, resource_id, workspace_path in candidates:
            value = self._bind_candidate_value(
                resource_kind, resource_id, workspace_path
            )
            label = (
                resource_id
                if isinstance(resource_id, str) and resource_id
                else workspace_path
            )
            search_items.append((value, label))
            exact_aliases[value] = (workspace_path,)
            alias_values = [workspace_path]
            if isinstance(resource_id, str) and resource_id:
                alias_values.append(resource_id)
            if isinstance(resource_kind, str) and resource_kind:
                alias_values.append(resource_kind.replace("_", " "))
            basename = Path(workspace_path).name
            if basename:
                alias_values.append(basename)
            filter_aliases[value] = tuple(alias_values)
        return search_items, exact_aliases, filter_aliases

    async def _resolve_picker_query_or_prompt(
        self,
        *,
        query: str,
        items: list[tuple[str, str]],
        limit: int,
        prompt_filtered_items: Callable[
            [str, list[tuple[str, str]]],
            Awaitable[None],
        ],
        exact_aliases: Optional[Mapping[str, Sequence[str]]] = None,
        aliases: Optional[Mapping[str, Sequence[str]]] = None,
    ) -> Optional[str]:
        normalized_query = query.strip()
        if not normalized_query:
            return None

        resolution = resolve_picker_query(
            items,
            normalized_query,
            limit=limit,
            exact_aliases=exact_aliases,
            aliases=aliases,
        )
        if resolution.selected_value is not None:
            return resolution.selected_value
        if resolution.filtered_items:
            await prompt_filtered_items(normalized_query, resolution.filtered_items)
            return None
        return normalized_query

    def _build_bind_page_prompt_and_components(
        self,
        candidates: list[tuple[Optional[str], Optional[str], str]],
        *,
        page: int,
    ) -> tuple[str, list[dict[str, Any]]]:
        page_size = DISCORD_SELECT_OPTION_MAX_OPTIONS
        total = len(candidates)
        total_pages = max(1, (total + page_size - 1) // page_size)
        bounded_page = max(0, min(page, total_pages - 1))
        start = bounded_page * page_size
        end = start + page_size
        page_candidates = candidates[start:end]

        prompt = "Select a workspace to bind:"
        if total > page_size:
            prompt = (
                "Select a workspace to bind "
                f"(page {bounded_page + 1}/{total_pages}, {total} total; "
                "recent worktrees first). Use `/car bind workspace:<repo_id>` "
                "or `/car bind workspace:<path>` for any repo not listed."
            )

        components: list[dict[str, Any]] = [
            build_bind_picker(self._build_bind_picker_items(page_candidates))
        ]
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

    def _ticket_dir(self, workspace_root: Path) -> Path:
        return workspace_root / ".codex-autorunner" / "tickets"

    def _list_ticket_choices(
        self,
        workspace_root: Path,
        *,
        status_filter: str,
        search_query: str = "",
    ) -> list[tuple[str, str, str]]:
        ticket_dir = self._ticket_dir(workspace_root)
        choices: list[tuple[str, str, str]] = []
        normalized_filter = status_filter.strip().lower()
        if normalized_filter not in {"all", "open", "done"}:
            normalized_filter = "all"
        for path in list_ticket_paths(ticket_dir):
            frontmatter, errors = read_ticket_frontmatter(path)
            is_done = bool(frontmatter and frontmatter.done and not errors)
            if normalized_filter == "open" and is_done:
                continue
            if normalized_filter == "done" and not is_done:
                continue
            title = frontmatter.title if frontmatter and frontmatter.title else ""
            label = f"{path.name}{' - ' + title if title else ''}"
            description = "done" if is_done else "open"
            rel_path = safe_relpath(path, workspace_root)
            choices.append((rel_path, label, description))
        normalized_query = search_query.strip()
        if not normalized_query or not choices:
            return choices
        search_items = [
            (value, f"{label} {description}".strip())
            for value, label, description in choices
        ]
        filtered_items = filter_picker_items(
            search_items,
            normalized_query,
            limit=len(search_items),
        )
        choice_by_value = {
            value: (value, label, description) for value, label, description in choices
        }
        return [
            choice_by_value[value]
            for value, _label in filtered_items
            if value in choice_by_value
        ]

    @staticmethod
    def _normalize_search_query(value: Any) -> str:
        if not isinstance(value, str):
            return ""
        return value.strip()

    @staticmethod
    def _ticket_prompt_text(*, search_query: str = "") -> str:
        normalized_query = search_query.strip()
        if not normalized_query:
            return "Select a ticket to view or edit."
        return f"Select a ticket to view or edit. Search: `{normalized_query}`"

    def _ticket_picker_value(self, ticket_rel: str) -> str:
        normalized = ticket_rel.strip()
        if len(normalized) <= 100:
            return normalized
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        return f"{TICKET_PICKER_TOKEN_PREFIX}{digest}"

    def _resolve_ticket_picker_value(
        self,
        selected_value: str,
        *,
        workspace_root: Path,
    ) -> Optional[str]:
        normalized = selected_value.strip()
        if not normalized:
            return None
        if not normalized.startswith(TICKET_PICKER_TOKEN_PREFIX):
            return normalized

        digest = normalized[len(TICKET_PICKER_TOKEN_PREFIX) :]
        if not digest:
            return None

        for path in list_ticket_paths(self._ticket_dir(workspace_root)):
            rel_path = safe_relpath(path, workspace_root)
            candidate = self._ticket_picker_value(rel_path)
            if candidate == normalized:
                return rel_path
        return None

    def _build_ticket_components(
        self,
        workspace_root: Path,
        *,
        status_filter: str,
        search_query: str = "",
    ) -> list[dict[str, Any]]:
        ticket_choices = self._list_ticket_choices(
            workspace_root,
            status_filter=status_filter,
            search_query=search_query,
        )
        return [
            build_ticket_filter_picker(current_filter=status_filter),
            build_ticket_picker(
                [
                    (self._ticket_picker_value(value), label, description)
                    for value, label, description in ticket_choices
                ]
            ),
        ]

    async def _handle_tickets(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        workspace_root: Path,
        options: dict[str, Any],
    ) -> None:
        status_filter = self._pending_ticket_filters.get(channel_id, "all")
        normalized_filter = status_filter.strip().lower() if status_filter else "all"
        if normalized_filter not in {"all", "open", "done"}:
            normalized_filter = "all"
        self._pending_ticket_filters[channel_id] = normalized_filter
        search_query = self._normalize_search_query(options.get("search"))
        if search_query:
            self._pending_ticket_search_queries[channel_id] = search_query
        else:
            self._pending_ticket_search_queries.pop(channel_id, None)
        await self._respond_with_components(
            interaction_id,
            interaction_token,
            self._ticket_prompt_text(search_query=search_query),
            self._build_ticket_components(
                workspace_root,
                status_filter=normalized_filter,
                search_query=search_query,
            ),
        )

    def _resolve_workspace_from_token(
        self,
        token: str,
        candidates: list[tuple[Optional[str], Optional[str], str]],
    ) -> Optional[tuple[Optional[str], Optional[str], str]]:
        return resolve_workspace_from_token(token, candidates)

    def _normalize_agent(self, value: Any) -> str:
        return (
            normalize_chat_agent(value, default=self.DEFAULT_AGENT, context=self)
            or self.DEFAULT_AGENT
        )

    def _agent_supports_effort(self, agent: str) -> bool:
        return chat_agent_supports_effort(agent, self)

    def _agent_supports_resume(self, agent: str) -> bool:
        return self._agent_supports_capability(agent, "durable_threads")

    def _status_model_label(self, binding: dict[str, Any]) -> str:
        model = binding.get("model_override")
        if isinstance(model, str):
            model = model.strip()
            if model:
                return model
        return "default"

    def _status_effort_label(self, binding: dict[str, Any], agent: str) -> str:
        if not self._agent_supports_effort(agent):
            return "n/a"
        effort = binding.get("reasoning_effort")
        if isinstance(effort, str):
            effort = effort.strip()
            if effort:
                return effort
        return "default"

    async def _read_status_rate_limits(
        self, workspace_path: Optional[str], *, agent: str
    ) -> Optional[dict[str, Any]]:
        if not self._agent_supports_effort(agent):
            return None
        try:
            client = await self._client_for_workspace(workspace_path)
        except AppServerUnavailableError:
            return None
        if client is None:
            return None
        for method in ("account/rateLimits/read", "account/read"):
            try:
                result = await client.request(method, params=None, timeout=5.0)
            except Exception:
                continue
            rate_limits = extract_rate_limits(result)
            if rate_limits:
                return rate_limits
        return None

    async def _list_model_items_for_binding(
        self,
        *,
        binding: dict[str, Any],
        agent: str,
        limit: int,
    ) -> Optional[list[tuple[str, str]]]:
        if agent == "opencode":
            return await self._list_opencode_models_for_picker(
                workspace_path=binding.get("workspace_path")
            )
        client = await self._client_for_workspace(binding.get("workspace_path"))
        if client is None:
            return None
        result = await _model_list_with_agent_compat(
            client,
            params={
                "cursor": None,
                "limit": max(1, limit),
                "agent": agent,
            },
        )
        return _coerce_model_picker_items(result, limit=max(1, limit))

    async def _bound_workspace_root_for_channel(
        self, channel_id: str
    ) -> Optional[Path]:
        binding = await self._store.get_binding(channel_id=channel_id)
        if binding is None or bool(binding.get("pma_enabled", False)):
            return None
        workspace_raw = binding.get("workspace_path")
        if not isinstance(workspace_raw, str) or not workspace_raw.strip():
            return None
        workspace_root = canonicalize_path(Path(workspace_raw))
        if not workspace_root.exists() or not workspace_root.is_dir():
            return None
        return workspace_root

    def _extract_skill_entries(
        self,
        result: Any,
        *,
        workspace_root: Path,
    ) -> list[tuple[str, str]]:
        entries: list[dict[str, Any]] = []
        if isinstance(result, dict):
            data = result.get("data")
            if isinstance(data, list):
                entries = [entry for entry in data if isinstance(entry, dict)]
        elif isinstance(result, list):
            entries = [entry for entry in result if isinstance(entry, dict)]

        skills: list[tuple[str, str]] = []
        seen_names: set[str] = set()
        resolved_workspace = workspace_root.expanduser().resolve()
        for entry in entries:
            cwd = entry.get("cwd")
            if isinstance(cwd, str):
                if Path(cwd).expanduser().resolve() != resolved_workspace:
                    continue
            items = entry.get("skills")
            if not isinstance(items, list):
                continue
            for skill in items:
                if not isinstance(skill, dict):
                    continue
                name = skill.get("name")
                if not isinstance(name, str):
                    continue
                normalized_name = name.strip()
                if not normalized_name or normalized_name in seen_names:
                    continue
                description = skill.get("shortDescription") or skill.get("description")
                desc_text = (
                    description.strip()
                    if isinstance(description, str) and description
                    else ""
                )
                skills.append((normalized_name, desc_text))
                seen_names.add(normalized_name)
        return skills

    @staticmethod
    def _filter_skill_entries(
        skill_entries: list[tuple[str, str]],
        query: str,
        *,
        limit: int,
    ) -> list[tuple[str, str]]:
        if limit <= 0:
            return []
        if not query.strip():
            return skill_entries[:limit]
        search_items = [
            (name, f"{name} - {description}" if description else name)
            for name, description in skill_entries
        ]
        filtered_items = filter_picker_items(search_items, query, limit=limit)
        skill_by_name = {
            name: (name, description) for name, description in skill_entries
        }
        return [
            skill_by_name[name]
            for name, _label in filtered_items
            if name in skill_by_name
        ]

    async def _list_skill_entries_for_workspace(
        self, workspace_root: Path
    ) -> Optional[list[tuple[str, str]]]:
        try:
            client = await self._client_for_workspace(str(workspace_root))
        except AppServerUnavailableError:
            return None
        if client is None:
            return None
        try:
            result = await client.request(
                "skills/list",
                {"cwds": [str(workspace_root)], "forceReload": False},
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.skills.failed",
                workspace_path=str(workspace_root),
                exc=exc,
            )
            return None
        return self._extract_skill_entries(result, workspace_root=workspace_root)

    async def _handle_command_autocomplete(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        command_path: tuple[str, ...],
        options: dict[str, Any],
        focused_name: Optional[str],
        focused_value: str,
    ) -> None:
        command_path = self._normalize_discord_command_path(command_path)
        await handle_car_command_autocomplete(
            self,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            command_path=command_path,
            options=options,
            focused_name=focused_name,
            focused_value=focused_value,
        )

    @staticmethod
    def _normalize_discord_command_path(
        command_path: tuple[str, ...],
    ) -> tuple[str, ...]:
        if command_path[:1] == ("flow",):
            return ("car", "flow", *command_path[1:])
        if len(command_path) == 3 and command_path[:2] == ("car", "admin"):
            admin_aliases = {
                "help",
                "debug",
                "ids",
                "mcp",
                "init",
                "repos",
                "experimental",
                "rollout",
                "feedback",
            }
            if command_path[2] in admin_aliases:
                return ("car", command_path[2])
        return command_path

    def _prepared_interaction_policy(
        self,
        interaction_token: str,
    ) -> Optional[str]:
        token = interaction_token.strip()
        if not token:
            return None
        policy = self._prepared_interaction_policies.get(token)
        if policy is None:
            return None
        self._prepared_interaction_policies.move_to_end(token)
        return policy

    def _remember_prepared_interaction_policy(
        self,
        *,
        interaction_token: str,
        policy: str,
    ) -> None:
        token = interaction_token.strip()
        if not token:
            return
        self._prepared_interaction_policies[token] = policy
        self._prepared_interaction_policies.move_to_end(token)
        while (
            len(self._prepared_interaction_policies) > PREPARED_INTERACTION_CACHE_LIMIT
        ):
            self._prepared_interaction_policies.popitem(last=False)

    async def _prepare_command_interaction(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        command_path: tuple[str, ...],
        timing: str = "dispatch",
    ) -> bool:
        entry = command_contract_entry_for_path(command_path)
        if entry is None or entry.discord_ack_policy in (None, "immediate"):
            return False
        if entry.discord_ack_timing != timing:
            return False
        if entry.discord_ack_policy == "defer_public":
            return await self._defer_public(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
            )
        if entry.discord_ack_policy == "defer_component_update":
            return await self._defer_component_update(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
            )
        return await self._defer_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
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
        if resolved_workspace is None:
            search_items, exact_aliases, filter_aliases = self._build_bind_search_items(
                candidates
            )

            async def _prompt_bind_matches(
                query_text: str,
                filtered_items: list[tuple[str, str]],
            ) -> None:
                filtered_values = {value for value, _label in filtered_items}
                filtered_candidates = [
                    candidate
                    for candidate in candidates
                    if self._bind_candidate_value(
                        candidate[0],
                        candidate[1],
                        candidate[2],
                    )
                    in filtered_values
                ]
                await self._respond_with_components(
                    interaction_id,
                    interaction_token,
                    (
                        f"Matched {len(filtered_candidates)} workspaces for `{query_text}`. "
                        "Select a workspace to bind:"
                    ),
                    [
                        build_bind_picker(
                            self._build_bind_picker_items(filtered_candidates)
                        )
                    ],
                )

            resolved_value = await self._resolve_picker_query_or_prompt(
                query=token,
                items=search_items,
                limit=DISCORD_SELECT_OPTION_MAX_OPTIONS,
                exact_aliases=exact_aliases,
                aliases=filter_aliases,
                prompt_filtered_items=_prompt_bind_matches,
            )
            if resolved_value is None:
                return
            resolved_workspace = self._resolve_workspace_from_token(
                resolved_value,
                candidates,
            )

        if resolved_workspace is not None:
            await self._bind_to_workspace_candidate(
                interaction_id,
                interaction_token,
                channel_id=channel_id,
                guild_id=guild_id,
                selected_resource_kind=resolved_workspace[0],
                selected_resource_id=resolved_workspace[1],
                workspace_path=resolved_workspace[2],
            )
            return

        candidate = Path(token)
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

        await self._bind_to_workspace_candidate(
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            guild_id=guild_id,
            selected_resource_kind=None,
            selected_resource_id=None,
            workspace_path=str(workspace),
        )

    async def _handle_status(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        guild_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> None:
        binding = await self._store.get_binding(channel_id=channel_id)
        command_result, plain_text_result = cast(
            tuple[CollaborationEvaluationResult, CollaborationEvaluationResult],
            evaluate_collaboration_summary(
                self,
                channel_id=channel_id,
                guild_id=guild_id,
                user_id=user_id,
            ),
        )
        active_flow = None
        workspace_path = None
        if isinstance(binding, dict):
            workspace_raw = binding.get("workspace_path")
            if isinstance(workspace_raw, str) and workspace_raw.strip():
                workspace_path = workspace_raw.strip()
                active_flow = await self._get_active_flow_info(workspace_path)
        lines = build_status_text(
            binding,
            collaboration_summary_lines(
                channel_id=channel_id,
                command_result=command_result,
                plain_text_result=plain_text_result,
                binding=binding,
            ),
            active_flow,
            channel_id,
            include_flow_hint=False,
        )
        if binding is None:
            await self._respond_ephemeral(
                interaction_id, interaction_token, "\n".join(lines)
            )
            return

        agent = self._normalize_agent(binding.get("agent"))
        rate_limits = await self._read_status_rate_limits(workspace_path, agent=agent)
        approval_mode = normalize_approval_mode(
            binding.get("approval_mode"),
            default="yolo",
            include_command_aliases=True,
        )
        if approval_mode is None:
            approval_mode = "yolo"
        approval_policy, sandbox_policy = resolve_approval_mode_policies(approval_mode)
        explicit_approval_policy = binding.get("approval_policy")
        if (
            isinstance(explicit_approval_policy, str)
            and explicit_approval_policy.strip()
        ):
            approval_policy = explicit_approval_policy.strip()
        explicit_sandbox_policy = binding.get("sandbox_policy")
        if explicit_sandbox_policy is not None:
            sandbox_policy = explicit_sandbox_policy
        model_label = self._status_model_label(binding)
        effort_label = self._status_effort_label(binding, agent)
        dispatcher = getattr(self, "_dispatcher", None)
        pending_queue = 0
        if dispatcher is not None:
            pending_queue = await dispatcher.pending(
                self._dispatcher_conversation_id(
                    channel_id=channel_id, guild_id=guild_id
                )
            )
        extra_lines = [f"Queued requests: {pending_queue}"]
        if pending_queue:
            extra_lines.append(
                "Queued messages include Cancel and Interrupt + Send buttons."
            )
        status_block = StatusBlockContext(
            agent=agent,
            resume="supported" if self._agent_supports_resume(agent) else "unsupported",
            model=model_label,
            effort=effort_label,
            approval_mode=approval_mode,
            approval_policy=approval_policy or "default",
            sandbox_policy=sandbox_policy,
            rate_limits=rate_limits,
            extra_lines=tuple(extra_lines),
        )
        lines.extend(build_status_block_lines(status_block))
        lines.append("Use /flow status for ticket flow details.")
        await self._respond_ephemeral(
            interaction_id, interaction_token, "\n".join(lines)
        )

    async def _get_active_flow_info(
        self, workspace_path: str
    ) -> Optional[ActiveFlowInfo]:
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
                        return ActiveFlowInfo(flow_id=record.id, status="running")
                    if record.status == FlowRunStatus.PAUSED:
                        return ActiveFlowInfo(flow_id=record.id, status="paused")
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
        guild_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> None:
        binding = await self._store.get_binding(channel_id=channel_id)
        command_result, plain_text_result = cast(
            tuple[CollaborationEvaluationResult, CollaborationEvaluationResult],
            evaluate_collaboration_summary(
                self,
                channel_id=channel_id,
                guild_id=guild_id,
                user_id=user_id,
            ),
        )
        lines = [
            f"Channel ID: {channel_id}",
        ]
        if binding is None:
            lines.append("Binding: none (unbound)")
            lines.append("Use /car bind path:<workspace> to bind this channel.")
            lines.extend(
                collaboration_summary_lines(
                    channel_id=channel_id,
                    command_result=command_result,
                    plain_text_result=plain_text_result,
                    binding=None,
                )
            )
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
        lines.extend(
            collaboration_summary_lines(
                channel_id=channel_id,
                command_result=command_result,
                plain_text_result=plain_text_result,
                binding=binding,
            )
        )

        await self._respond_ephemeral(
            interaction_id, interaction_token, "\n".join(lines)
        )

    async def _handle_help(
        self,
        interaction_id: str,
        interaction_token: str,
    ) -> None:
        lines = build_discord_help_lines()
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
        command_result, plain_text_result = cast(
            tuple[CollaborationEvaluationResult, CollaborationEvaluationResult],
            evaluate_collaboration_summary(
                self,
                channel_id=channel_id,
                guild_id=guild_id,
                user_id=user_id,
            ),
        )
        binding = await self._store.get_binding(channel_id=channel_id)
        lines.extend(
            [
                "",
                *collaboration_summary_lines(
                    channel_id=channel_id,
                    command_result=command_result,
                    plain_text_result=plain_text_result,
                    binding=binding,
                ),
                "",
                *build_collaboration_snippet_lines(
                    channel_id=channel_id,
                    guild_id=guild_id,
                    user_id=user_id,
                ),
            ]
        )
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

        deferred = await self._defer_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )
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
                await self._send_or_respond_ephemeral(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    deferred=deferred,
                    text="Not a git repository.",
                )
                return
        except subprocess.TimeoutExpired:
            await self._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text="Git check timed out.",
            )
            return
        except Exception as exc:
            await self._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=f"Git check failed: {exc}",
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
        await self._send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=output,
        )

    async def _handle_skills(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        options: dict[str, Any],
    ) -> None:
        skill_entries = await self._list_skill_entries_for_workspace(workspace_root)
        if skill_entries is None:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Workspace unavailable. Re-bind this channel and try again.",
            )
            return

        search_query = self._normalize_search_query(options.get("search"))
        if search_query:
            filtered_entries = self._filter_skill_entries(
                skill_entries,
                search_query,
                limit=max(len(skill_entries), DEFAULT_SKILLS_LIST_LIMIT),
            )
            if not filtered_entries:
                await self._respond_ephemeral(
                    interaction_id,
                    interaction_token,
                    f"No skills found matching `{search_query}`.",
                )
                return
            lines = [f"Skills matching `{search_query}`:"]
            for name, description in filtered_entries[:DEFAULT_SKILLS_LIST_LIMIT]:
                if description:
                    lines.append(f"{name} - {description}")
                else:
                    lines.append(name)
            if len(filtered_entries) > DEFAULT_SKILLS_LIST_LIMIT:
                lines.append(
                    f"...and {len(filtered_entries) - DEFAULT_SKILLS_LIST_LIMIT} more matches."
                )
            lines.append("Use $<SkillName> in your next message to invoke a skill.")
            skills_text = "\n".join(lines)
        else:
            if not skill_entries:
                await self._respond_ephemeral(
                    interaction_id,
                    interaction_token,
                    "No skills found.",
                )
                return
            skills_text = _format_skills_list(
                [
                    {
                        "cwd": str(workspace_root),
                        "skills": [
                            {
                                "name": name,
                                "shortDescription": description,
                            }
                            for name, description in skill_entries
                        ],
                    }
                ],
                str(workspace_root),
            )

        styled_lines: list[str] = []
        for line in skills_text.splitlines():
            if (
                not line
                or line == "Skills:"
                or line.startswith("Skills matching ")
                or line.startswith("...and ")
                or line.startswith("Use $")
            ):
                styled_lines.append(line)
                continue
            if " - " in line:
                name, description = line.split(" - ", 1)
                styled_lines.append(f"**{name}** - {description}")
            else:
                styled_lines.append(f"**{line}**")
        rendered = format_discord_message("\n".join(styled_lines))
        chunks = chunk_discord_message(
            rendered,
            max_len=self._config.max_message_length,
            with_numbering=False,
        )
        if not chunks:
            chunks = ["No skills found."]

        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            chunks[0],
        )
        for chunk in chunks[1:]:
            sent = await self._send_followup_ephemeral(
                interaction_token=interaction_token,
                content=chunk,
            )
            if not sent:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.skills.followup_failed",
                    workspace_path=str(workspace_root),
                )
                break

    async def _handle_modal_submit_interaction(
        self, interaction_payload: dict[str, Any]
    ) -> None:
        interaction_id = extract_interaction_id(interaction_payload)
        interaction_token = extract_interaction_token(interaction_payload)
        channel_id = extract_channel_id(interaction_payload)

        if not interaction_id or not interaction_token or not channel_id:
            self._logger.warning(
                "handle_modal_submit_interaction: missing required fields (interaction_id=%s, token=%s, channel=%s)",
                bool(interaction_id),
                bool(interaction_token),
                bool(channel_id),
            )
            return

        policy_result = self._evaluate_interaction_collaboration_policy(
            channel_id=channel_id,
            guild_id=extract_guild_id(interaction_payload),
            user_id=extract_user_id(interaction_payload),
        )
        if not policy_result.command_allowed:
            self._log_collaboration_policy_result(
                channel_id=channel_id,
                guild_id=extract_guild_id(interaction_payload),
                user_id=extract_user_id(interaction_payload),
                interaction_id=interaction_id,
                result=policy_result,
            )
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "This Discord interaction is not authorized.",
            )
            return

        custom_id = extract_modal_custom_id(interaction_payload)
        if not custom_id:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "I could not identify this modal submission. Please retry.",
            )
            return

        values = extract_modal_values(interaction_payload)
        await self._handle_ticket_modal_submit(
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            custom_id=custom_id,
            values=values,
        )

    async def _handle_ticket_modal_submit(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        custom_id: str,
        values: dict[str, Any],
    ) -> None:
        if not custom_id.startswith(f"{TICKETS_MODAL_PREFIX}:"):
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Unknown modal submission.",
            )
            return

        token = custom_id.split(":", 1)[1].strip()
        context = self._pending_ticket_context.pop(token, None)
        if not context:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "This ticket modal has expired. Re-open it and try again.",
            )
            return

        ticket_rel = context.get("ticket_rel")
        if not isinstance(ticket_rel, str) or not ticket_rel or ticket_rel == "none":
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "This ticket selection expired. Re-run `/car tickets` and choose one.",
            )
            return

        ticket_body_raw = values.get(TICKETS_BODY_INPUT_ID)
        ticket_body = ticket_body_raw if isinstance(ticket_body_raw, str) else None
        if ticket_body is None:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Ticket content is missing. Please try again.",
            )
            return

        workspace_root = Path(context.get("workspace_root", "")).expanduser()
        ticket_dir = self._ticket_dir(workspace_root).resolve()
        candidate = (workspace_root / ticket_rel).resolve()
        try:
            candidate.relative_to(ticket_dir)
        except ValueError:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Ticket path is invalid. Re-open the ticket and try again.",
            )
            return

        try:
            candidate.write_text(ticket_body, encoding="utf-8")
        except Exception as exc:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Failed to save ticket: {exc}",
            )
            return

        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"Saved {safe_relpath(candidate, workspace_root)}.",
        )

    @staticmethod
    def _extract_modal_single_select(
        values: dict[str, Any], custom_id: str
    ) -> Optional[str]:
        raw = values.get(custom_id)
        if isinstance(raw, str):
            normalized = raw.strip()
            return normalized or None
        if isinstance(raw, list):
            for value in raw:
                if isinstance(value, (str, int, float)):
                    normalized = str(value).strip()
                    if normalized:
                        return normalized
        return None

    async def _handle_ticket_filter_component(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        values: Optional[list[str]],
    ) -> None:
        if not values:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Please select a filter and try again.",
            )
            return
        workspace_root = await self._require_bound_workspace(
            interaction_id, interaction_token, channel_id=channel_id
        )
        if not workspace_root:
            return
        status_filter = values[0].strip().lower()
        if status_filter not in {"all", "open", "done"}:
            status_filter = "all"
        search_query = self._pending_ticket_search_queries.get(channel_id, "")
        self._pending_ticket_filters[channel_id] = status_filter
        await self._update_component_message(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            text=self._ticket_prompt_text(search_query=search_query),
            components=self._build_ticket_components(
                workspace_root,
                status_filter=status_filter,
                search_query=search_query,
            ),
        )

    async def _handle_ticket_select_component(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        values: Optional[list[str]],
    ) -> None:
        if not values:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Please select a ticket and try again.",
            )
            return
        if values[0] == "none":
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "No tickets available for this filter.",
            )
            return
        workspace_root = await self._require_bound_workspace(
            interaction_id, interaction_token, channel_id=channel_id
        )
        if not workspace_root:
            return
        ticket_rel = self._resolve_ticket_picker_value(
            values[0],
            workspace_root=workspace_root,
        )
        if not ticket_rel:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Ticket selection is invalid. Re-open the ticket list and try again.",
            )
            return
        await self._open_ticket_modal(
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            ticket_rel=ticket_rel,
        )

    async def _open_ticket_modal(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        ticket_rel: str,
    ) -> None:
        ticket_dir = self._ticket_dir(workspace_root).resolve()
        candidate = (workspace_root / ticket_rel).resolve()
        try:
            candidate.relative_to(ticket_dir)
        except ValueError:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Ticket path is invalid. Re-open the ticket list and try again.",
            )
            return
        if not candidate.exists() or not candidate.is_file():
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Ticket file not found. Re-open the ticket list and try again.",
            )
            return
        try:
            ticket_text = candidate.read_text(encoding="utf-8")
        except Exception as exc:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Failed to read ticket: {exc}",
            )
            return
        max_len = 4000
        if len(ticket_text) > max_len:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                (
                    f"`{ticket_rel}` is too large to edit in a Discord modal "
                    f"({len(ticket_text)} characters; limit {max_len}). "
                    "Use the web UI or edit the file directly."
                ),
            )
            return

        token = uuid.uuid4().hex[:12]
        self._pending_ticket_context[token] = {
            "workspace_root": str(workspace_root),
            "ticket_rel": ticket_rel,
        }

        title = "Edit ticket"
        await self._respond_modal(
            interaction_id,
            interaction_token,
            custom_id=f"{TICKETS_MODAL_PREFIX}:{token}",
            title=title,
            field_label="Ticket",
            field_value=ticket_text,
        )

    async def _respond_modal(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        custom_id: str,
        title: str,
        field_label: str,
        field_value: str,
    ) -> None:
        payload = {
            "type": 9,
            "data": {
                "custom_id": custom_id[:100],
                "title": title[:45],
                "components": [
                    {
                        "type": 18,
                        "label": field_label[:45],
                        "component": {
                            "type": 4,
                            "custom_id": TICKETS_BODY_INPUT_ID,
                            "style": 2,
                            "value": field_value[:4000],
                            "required": True,
                            "max_length": 4000,
                        },
                    },
                ],
            },
        }
        try:
            await self._rest.create_interaction_response(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                payload=payload,
            )
        except DiscordAPIError as exc:
            self._logger.error(
                "Failed to send modal response: %s (interaction_id=%s)",
                exc,
                interaction_id,
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
        target_root = canonicalize_path(workspace_root)
        ca_dir = target_root / ".codex-autorunner"

        try:
            hub_initialized = False
            if (target_root / ".git").exists():
                await asyncio.to_thread(
                    seed_repo_files,
                    target_root,
                    False,
                    True,
                )
                if find_nearest_hub_config_path(target_root) is None:
                    _, hub_initialized = await asyncio.to_thread(
                        ensure_hub_config_at,
                        target_root,
                    )
            elif self._has_nested_git(target_root):
                _, hub_initialized = await asyncio.to_thread(
                    ensure_hub_config_at,
                    target_root,
                )
            else:
                await self._respond_ephemeral(
                    interaction_id,
                    interaction_token,
                    "No .git directory found. Run git init or use the CLI `car init --git-init`.",
                )
                return
        except ConfigError as exc:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Init failed: {exc}",
            )
            return
        except Exception as exc:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Init failed: {exc}",
            )
            return

        lines = [f"Initialized repo at {ca_dir}"]
        if hub_initialized:
            lines.append(f"Initialized hub at {ca_dir}")
        lines.append("Init complete")
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            "\n".join(lines),
        )

    @staticmethod
    def _has_nested_git(path: Path) -> bool:
        try:
            for child in path.iterdir():
                if not child.is_dir() or child.is_symlink():
                    continue
                if (child / ".git").exists():
                    return True
                if DiscordBotService._has_nested_git(child):
                    return True
        except OSError:
            return False
        return False

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
        deferred = await self._defer_public(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )
        binding = await self._store.get_binding(channel_id=channel_id)
        if binding is None:
            text = format_discord_message(
                "This channel is not bound. Run `/car bind path:<...>` first."
            )
            await self._send_or_respond_public(
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
                await self._send_or_respond_public(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    deferred=deferred,
                    text=text,
                )
                return

        agent = self._normalize_agent(binding.get("agent"))
        resource_kind = (
            str(binding.get("resource_kind")).strip()
            if isinstance(binding.get("resource_kind"), str)
            and str(binding.get("resource_kind")).strip()
            else None
        )
        resource_id = (
            str(binding.get("resource_id")).strip()
            if isinstance(binding.get("resource_id"), str)
            and str(binding.get("resource_id")).strip()
            else None
        )

        try:
            had_previous, _new_thread_id = await self._reset_discord_thread_binding(
                channel_id=channel_id,
                workspace_root=workspace_root,
                agent=agent,
                repo_id=(
                    str(binding.get("repo_id")).strip()
                    if isinstance(binding.get("repo_id"), str)
                    and str(binding.get("repo_id")).strip()
                    else None
                ),
                resource_kind=resource_kind,
                resource_id=resource_id,
                pma_enabled=pma_enabled,
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.new.reset_failed",
                channel_id=channel_id,
                workspace_root=str(workspace_root),
                agent=agent,
                exc=exc,
            )
            text = format_discord_message("Failed to start a fresh session.")
            await self._send_or_respond_public(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=text,
            )
            return
        await self._store.clear_pending_compact_seed(channel_id=channel_id)
        mode_label = "PMA" if pma_enabled else "repo"
        state_label = "cleared previous thread" if had_previous else "new thread ready"

        text = format_discord_message(
            f"Started a fresh {mode_label} session for `{agent}` ({state_label})."
        )
        await self._send_or_respond_public(
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
        deferred = await self._defer_public(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )
        binding = await self._store.get_binding(channel_id=channel_id)
        if binding is None:
            text = format_discord_message(
                "This channel is not bound. Run `/car bind path:<...>` first."
            )
            await self._send_or_respond_public(
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
            await self._send_or_respond_public(
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
            await self._send_or_respond_public(
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
            await self._send_or_respond_public(
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
                await self._send_or_respond_public(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    deferred=deferred,
                    text=text,
                )
                return

        agent = self._normalize_agent(binding.get("agent"))
        resource_kind = (
            str(binding.get("resource_kind")).strip()
            if isinstance(binding.get("resource_kind"), str)
            and str(binding.get("resource_kind")).strip()
            else None
        )
        resource_id = (
            str(binding.get("resource_id")).strip()
            if isinstance(binding.get("resource_id"), str)
            and str(binding.get("resource_id")).strip()
            else None
        )

        try:
            had_previous, _new_thread_id = await self._reset_discord_thread_binding(
                channel_id=channel_id,
                workspace_root=workspace_root,
                agent=agent,
                repo_id=(
                    str(binding.get("repo_id")).strip()
                    if isinstance(binding.get("repo_id"), str)
                    and str(binding.get("repo_id")).strip()
                    else None
                ),
                resource_kind=resource_kind,
                resource_id=resource_id,
                pma_enabled=pma_enabled,
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.newt.thread_reset.failed",
                channel_id=channel_id,
                workspace_root=str(workspace_root),
                agent=agent,
                exc=exc,
            )
            text = format_discord_message(
                "Branch reset succeeded, but starting a fresh session failed."
            )
            await self._send_or_respond_public(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=text,
            )
            return
        await self._store.clear_pending_compact_seed(channel_id=channel_id)
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
        await self._send_or_respond_public(
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

        agent = self._normalize_agent(binding.get("agent"))

        repo_id = (
            str(binding.get("repo_id")).strip()
            if isinstance(binding.get("repo_id"), str)
            and str(binding.get("repo_id")).strip()
            else None
        )
        resource_kind = (
            str(binding.get("resource_kind")).strip()
            if isinstance(binding.get("resource_kind"), str)
            and str(binding.get("resource_kind")).strip()
            else None
        )
        resource_id = (
            str(binding.get("resource_id")).strip()
            if isinstance(binding.get("resource_id"), str)
            and str(binding.get("resource_id")).strip()
            else None
        )
        mode = "pma" if pma_enabled else "repo"
        orchestration_service, _current_binding, current_thread = (
            self._get_discord_thread_binding(channel_id=channel_id, mode=mode)
        )

        raw_thread_id = options.get("thread_id")
        thread_id = raw_thread_id.strip() if isinstance(raw_thread_id, str) else None
        current_thread_id = (
            str(getattr(current_thread, "thread_target_id", "") or "").strip() or None
        )

        if thread_id:
            thread_items = self._list_discord_thread_targets_for_picker(
                workspace_root=workspace_root,
                agent=agent,
                current_thread_id=current_thread_id,
                mode=mode,
                repo_id=repo_id,
                resource_kind=resource_kind,
                resource_id=resource_id,
            )
            if thread_items:

                async def _prompt_thread_matches(
                    query_text: str,
                    filtered_items: list[tuple[str, str]],
                ) -> None:
                    header = (
                        f"Current thread: `{current_thread_id}`\n\n"
                        if current_thread_id
                        else ""
                    )
                    await self._send_or_respond_ephemeral(
                        interaction_id=interaction_id,
                        interaction_token=interaction_token,
                        deferred=deferred,
                        text=format_discord_message(
                            header
                            + (
                                f"Matched {len(filtered_items)} threads for "
                                f"`{query_text}`. Select a thread to resume:"
                            )
                        ),
                    )
                    await self._send_followup_ephemeral(
                        interaction_token=interaction_token,
                        content="Choose one thread from the filtered picker below.",
                        components=[build_session_threads_picker(filtered_items)],
                    )

                resolved_thread_id = await self._resolve_picker_query_or_prompt(
                    query=thread_id,
                    items=thread_items,
                    limit=DISCORD_SELECT_OPTION_MAX_OPTIONS,
                    prompt_filtered_items=_prompt_thread_matches,
                )
                if resolved_thread_id is None:
                    return
                thread_id = resolved_thread_id
            target_thread = orchestration_service.get_thread_target(thread_id)
            if target_thread is None:
                await self._send_or_respond_ephemeral(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    deferred=deferred,
                    text=format_discord_message(
                        f"Unknown thread `{thread_id}` for this workspace."
                    ),
                )
                return
            if str(getattr(target_thread, "workspace_root", "") or "").strip() != str(
                workspace_root.resolve()
            ):
                await self._send_or_respond_ephemeral(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    deferred=deferred,
                    text=format_discord_message(
                        "Selected thread belongs to a different workspace."
                    ),
                )
                return
            if str(getattr(target_thread, "agent_id", "") or "").strip() != agent:
                await self._send_or_respond_ephemeral(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    deferred=deferred,
                    text=format_discord_message(
                        f"Selected thread belongs to a different agent. Current agent: `{agent}`."
                    ),
                )
                return
            lifecycle_status = (
                str(getattr(target_thread, "lifecycle_status", "") or "")
                .strip()
                .lower()
            )
            if lifecycle_status and lifecycle_status != "active":
                try:
                    orchestration_service.resume_thread_target(thread_id)
                except Exception:
                    pass
            self._attach_discord_thread_binding(
                channel_id=channel_id,
                thread_target_id=thread_id,
                agent=agent,
                repo_id=repo_id,
                resource_kind=resource_kind,
                resource_id=resource_id,
                workspace_root=workspace_root,
                pma_enabled=pma_enabled,
            )
            await self._store.clear_pending_compact_seed(channel_id=channel_id)
            mode_label = "PMA" if pma_enabled else "repo"
            text = format_discord_message(
                f"Resumed {mode_label} session for `{agent}` with thread `{thread_id}`."
            )
        else:
            thread_items = self._list_discord_thread_targets_for_picker(
                workspace_root=workspace_root,
                agent=agent,
                current_thread_id=current_thread_id,
                mode=mode,
                repo_id=repo_id,
                resource_kind=resource_kind,
                resource_id=resource_id,
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
        response_mode: str = "command",
    ) -> None:
        component_response = response_mode == "component"
        raw_target = options.get("target")
        confirmed = bool(options.get("confirmed"))
        if not isinstance(raw_target, str) or not raw_target.strip():
            components = [
                build_update_target_picker(
                    custom_id=UPDATE_TARGET_SELECT_ID,
                    target_definitions=self._dynamic_update_target_definitions(),
                )
            ]
            if component_response:
                await self._update_component_message(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    text="Select update target:",
                    components=components,
                )
            else:
                await self._respond_with_components(
                    interaction_id,
                    interaction_token,
                    "Select update target:",
                    components,
                )
            return
        if isinstance(raw_target, str) and raw_target.strip().lower() == "status":
            await self._handle_car_update_status(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                component_response=component_response,
            )
            return

        try:
            update_target = _normalize_update_target(
                raw_target if isinstance(raw_target, str) else None
            )
        except ValueError as exc:
            components = [
                build_update_target_picker(
                    custom_id=UPDATE_TARGET_SELECT_ID,
                    target_definitions=self._dynamic_update_target_definitions(),
                )
            ]
            text = f"{exc} Select update target:"
            if component_response:
                await self._update_component_message(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    text=text,
                    components=components,
                )
            else:
                await self._respond_with_components(
                    interaction_id,
                    interaction_token,
                    text,
                    components,
                )
            return
        deferred = (
            await self._defer_component_update(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
            )
            if component_response
            else await self._defer_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
            )
        )
        if not confirmed and _update_target_restarts_surface(
            update_target, surface="discord"
        ):
            warning = _format_update_confirmation_warning(
                active_count=self._active_update_session_count(),
                singular_label="Codex session",
            )
            if warning:
                warning_text = format_discord_message(warning)
                components = self._build_update_confirmation_components(
                    update_target=update_target
                )
                if component_response:
                    await self._send_or_update_component_message(
                        interaction_id=interaction_id,
                        interaction_token=interaction_token,
                        deferred=deferred,
                        text=warning_text,
                        components=components,
                    )
                else:
                    await self._send_or_respond_with_components_ephemeral(
                        interaction_id=interaction_id,
                        interaction_token=interaction_token,
                        deferred=deferred,
                        text=warning_text,
                        components=components,
                    )
                return

        target_label = get_update_target_label(update_target)
        restarts_discord = _update_target_restarts_surface(
            update_target, surface="discord"
        )
        if restarts_discord:
            text = format_discord_message(
                f"Preparing update ({target_label}). Checking whether the update can start now. "
                "If it does, the selected service(s) will restart shortly and I will post completion status in this channel. "
                "Use `/car update target:status` for progress."
            )
            if component_response:
                await self._send_or_update_component_message(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    deferred=deferred,
                    text=text,
                )
            else:
                await self._send_or_respond_ephemeral(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    deferred=deferred,
                    text=text,
                )
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
            if component_response:
                await self._send_or_update_component_message(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    deferred=deferred,
                    text=text,
                )
            else:
                await self._send_or_respond_ephemeral(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    deferred=deferred,
                    text=text,
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
            if component_response:
                await self._send_or_update_component_message(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    deferred=deferred,
                    text="Update failed to start. Check logs for details.",
                )
            else:
                await self._send_or_respond_ephemeral(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    deferred=deferred,
                    text="Update failed to start. Check logs for details.",
                )
            return

        if restarts_discord:
            self._update_status_notifier.schedule_watch({"chat_id": channel_id})
            return
        text = format_discord_message(
            f"Update started ({target_label}). The selected service(s) will restart. "
            "I will post completion status in this channel. "
            "Use `/car update target:status` for progress."
        )
        if component_response:
            await self._send_or_update_component_message(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=text,
            )
        else:
            await self._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=text,
            )
        self._update_status_notifier.schedule_watch({"chat_id": channel_id})

    @staticmethod
    def _update_thread_blocks_restart_warning(thread: Any) -> bool:
        thread_kind = str(getattr(thread, "thread_kind", "") or "").strip().lower()
        return thread_kind != "ticket_flow"

    def _active_update_session_count(self) -> int:
        try:
            orchestration_service = self._discord_thread_service()
            threads = orchestration_service.list_thread_targets(
                lifecycle_status="active"
            )
        except Exception:
            return 0
        get_running_execution = getattr(
            orchestration_service, "get_running_execution", None
        )
        if not callable(get_running_execution):
            return sum(
                1
                for thread in threads
                if self._update_thread_blocks_restart_warning(thread)
                if str(getattr(thread, "status", "") or "").strip().lower() == "running"
            )

        active_count = 0
        for thread in threads:
            if not self._update_thread_blocks_restart_warning(thread):
                continue
            thread_target_id = str(
                getattr(thread, "thread_target_id", "") or ""
            ).strip()
            if not thread_target_id:
                continue
            try:
                if get_running_execution(thread_target_id) is not None:
                    active_count += 1
            except Exception:
                if (
                    str(getattr(thread, "status", "") or "").strip().lower()
                    == "running"
                ):
                    active_count += 1
        return active_count

    def _workspace_has_running_opencode_execution(
        self, workspace_root: Path
    ) -> Optional[bool]:
        try:
            orchestration_service = self._discord_thread_service()
            threads = orchestration_service.list_thread_targets(
                agent_id="opencode",
                lifecycle_status="active",
                limit=10_000,
            )
        except Exception:
            return None
        get_running_execution = getattr(
            orchestration_service, "get_running_execution", None
        )
        canonical_workspace = str(canonicalize_path(Path(workspace_root)).resolve())
        for thread in threads:
            thread_workspace = str(getattr(thread, "workspace_root", "") or "").strip()
            if not thread_workspace:
                continue
            try:
                normalized_thread_workspace = str(
                    canonicalize_path(Path(thread_workspace)).resolve()
                )
            except Exception:
                normalized_thread_workspace = thread_workspace
            if normalized_thread_workspace != canonical_workspace:
                continue
            thread_target_id = str(
                getattr(thread, "thread_target_id", "") or ""
            ).strip()
            if not thread_target_id:
                continue
            if callable(get_running_execution):
                try:
                    if get_running_execution(thread_target_id) is not None:
                        return True
                except Exception:
                    return None
            if str(getattr(thread, "status", "") or "").strip().lower() == "running":
                return True
        return False

    def _build_update_confirmation_components(
        self,
        *,
        update_target: str,
    ) -> list[dict[str, Any]]:
        return [
            build_action_row(
                [
                    build_button(
                        "Update anyway",
                        f"{UPDATE_CONFIRM_PREFIX}:{update_target}",
                        style=DISCORD_BUTTON_STYLE_DANGER,
                    ),
                    build_button("Cancel", f"{UPDATE_CANCEL_PREFIX}:{update_target}"),
                ]
            )
        ]

    def _update_status_path(self) -> Path:
        return resolve_update_paths().status_path

    def _format_update_status_message(self, status: Optional[dict[str, Any]]) -> str:
        rendered = format_update_status_message(status)
        if not status:
            return rendered
        lines = [rendered]
        repo_ref = status.get("repo_ref")
        if isinstance(repo_ref, str) and repo_ref.strip():
            lines.append(f"Ref: {repo_ref.strip()}")
        log_path = status.get("log_path")
        if isinstance(log_path, str) and log_path.strip():
            lines.append(f"Log: {log_path.strip()}")
        return "\n".join(lines)

    def _dynamic_update_target_definitions(self):
        raw_config = self._hub_raw_config_cache
        if raw_config is None:
            try:
                raw_config = load_hub_config(self._config.root).raw
            except Exception:
                raw_config = {}
            self._hub_raw_config_cache = raw_config
        return _available_update_target_definitions(
            raw_config=raw_config if isinstance(raw_config, dict) else None,
            update_backend=self._update_backend,
            linux_service_names=(
                self._update_linux_service_names
                if isinstance(self._update_linux_service_names, dict)
                else None
            ),
        )

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
        component_response: bool = False,
    ) -> None:
        if component_response:
            status = await asyncio.to_thread(_read_update_status)
            if not isinstance(status, dict):
                status = None
            text = self._format_update_status_message(status)
            await self._update_component_message(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                text=text,
                components=[],
            )
            return
        deferred = await self._defer_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )
        status = await asyncio.to_thread(_read_update_status)
        if not isinstance(status, dict):
            status = None
        text = self._format_update_status_message(status)
        await self._send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=text,
        )

    def _agent_descriptor(self, agent: object) -> AgentDescriptor | None:
        normalized = self._normalize_agent(agent)
        return get_agent_descriptor(normalized, self)

    def _agent_display_name(self, agent: object) -> str:
        descriptor = self._agent_descriptor(agent)
        if descriptor is not None:
            return descriptor.name
        normalized = self._normalize_agent(agent)
        if normalized:
            return normalized
        return "This agent"

    def _agent_supports_capability(self, agent: object, capability: str) -> bool:
        descriptor = self._agent_descriptor(agent)
        if descriptor is None:
            return False
        normalized = normalize_agent_capabilities([capability])
        if not normalized:
            return False
        return next(iter(normalized)) in descriptor.capabilities

    def _agents_supporting_capability(self, capability: str) -> list[str]:
        normalized = normalize_agent_capabilities([capability])
        if not normalized:
            return []
        resolved = next(iter(normalized))
        return sorted(
            descriptor.id
            for descriptor in get_registered_agents(self).values()
            if resolved in descriptor.capabilities
        )

    DEFAULT_AGENT = DEFAULT_CHAT_AGENT

    def _known_agent_values(self) -> tuple[str, ...]:
        return valid_chat_agent_values(self)

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

        current_agent = self._normalize_agent(binding.get("agent"))
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
                [build_agent_picker(current_agent=current_agent, context=self)],
            )
            return

        desired = normalize_chat_agent(agent_name, context=self)
        if desired is None:
            available = ", ".join(self._known_agent_values())
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Invalid agent '{agent_name}'. Valid options: {available}",
            )
            return

        if desired == current_agent:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Agent already set to {current_agent}.",
            )
            return

        switch_state = build_agent_switch_state(
            desired,
            model_reset="clear",
            context=self,
        )
        await self._store.update_agent_state(
            channel_id=channel_id,
            agent=switch_state.agent,
            model_override=switch_state.model,
            reasoning_effort=switch_state.effort,
        )
        await self._store.clear_pending_compact_seed(channel_id=channel_id)
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"Agent set to {switch_state.agent}. Will apply on the next turn.",
        )

    VALID_REASONING_EFFORTS = REASONING_EFFORT_VALUES

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

        current_agent = self._normalize_agent(binding.get("agent"))
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
                        "Use `/car model name:<id> effort:<value>` when the current agent supports reasoning effort.",
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

            supports_model_listing = self._agent_supports_capability(
                current_agent, "model_listing"
            )
            if not supports_model_listing:
                supported_agents = self._agents_supporting_capability("model_listing")
                supported_hint = (
                    f" Switch to an agent with model picker support: {', '.join(supported_agents)}."
                    if supported_agents
                    else ""
                )
                await _send_model_picker_or_fallback(
                    _fallback_model_text(
                        f"{self._agent_display_name(current_agent)} does not expose a model catalog in Discord.{supported_hint}"
                    ),
                )
                return

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
                    "Use `/car model name:<id> effort:<value>` when the current agent supports reasoning effort.",
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

        available_model_items: Optional[list[tuple[str, str]]] = None
        try:
            available_model_items = await self._list_model_items_for_binding(
                binding=binding,
                agent=current_agent,
                limit=MODEL_SEARCH_FETCH_LIMIT,
            )
        except Exception:
            available_model_items = None

        if available_model_items:

            async def _prompt_model_matches(
                query_text: str,
                filtered_items: list[tuple[str, str]],
            ) -> None:
                lines = [
                    f"Current agent: {current_agent}",
                    f"Current model: {current_model or '(default)'}",
                    "",
                    f"Matched {len(filtered_items)} models for `{query_text}`:",
                    "Select a model override:",
                    "(default model) clears the override.",
                ]
                if isinstance(current_effort, str) and current_effort.strip():
                    lines.insert(2, f"Reasoning effort: {current_effort}")
                await self._respond_with_components(
                    interaction_id,
                    interaction_token,
                    format_discord_message("\n".join(lines)),
                    [
                        build_model_picker(
                            filtered_items,
                            current_model=current_model,
                        )
                    ],
                )

            resolved_model_name = await self._resolve_picker_query_or_prompt(
                query=model_name,
                items=available_model_items,
                limit=max(1, DISCORD_SELECT_OPTION_MAX_OPTIONS - 1),
                prompt_filtered_items=_prompt_model_matches,
            )
            if resolved_model_name is None:
                return
            model_name = resolved_model_name

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
            if not self._agent_supports_effort(current_agent):
                await self._respond_ephemeral(
                    interaction_id,
                    interaction_token,
                    f"Reasoning effort is not supported for {self._agent_display_name(current_agent)}.",
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

        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            format_model_set_message(model_name, effort=effort),
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
        current_agent = self._normalize_agent(binding.get("agent") if binding else None)

        if self._agent_supports_effort(current_agent):
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
            line_prefix = "  -> " if entry.is_worktree else ""
            if entry.group not in groups:
                groups[entry.group] = []
                group_order.append(entry.group)
            try:
                store = self._open_flow_store(entry.repo_root)
            except Exception:
                groups[entry.group].append(
                    (
                        line_label,
                        f"{line_prefix}❓ {line_label}: Error reading state",
                    )
                )
                continue
            try:
                latest = select_default_ticket_flow_run(store)
                progress = ticket_progress(entry.repo_root)
                display = build_ticket_flow_display(
                    status=latest.status.value if latest else None,
                    done_count=progress.get("done", 0),
                    total_count=progress.get("total", 0),
                    run_id=latest.id if latest else None,
                )
                run_id = display.get("run_id")
                run_suffix = f" run {run_id}" if run_id else ""
                duration_suffix = ""
                if latest is not None and latest.finished_at:
                    duration_label = format_flow_duration(
                        flow_run_duration_seconds(latest)
                    )
                    if duration_label:
                        duration_suffix = f" · took {duration_label}"
                freshness_suffix = ""
                if latest is not None:
                    snapshot = build_flow_status_snapshot(
                        entry.repo_root, latest, store
                    )
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
                line = (
                    f"{line_prefix}{display['status_icon']} {line_label}: "
                    f"{display['status_label']} {display['done_count']}/{display['total_count']}{run_suffix}{duration_suffix}{freshness_suffix}"
                )
            except Exception:
                line = f"{line_prefix}❓ {line_label}: Error reading state"
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

    def _delete_flow_run_record(self, workspace_root: Path, run_id: str) -> bool:
        store = self._open_flow_store(workspace_root)
        try:
            return bool(store.delete_flow_run(run_id))
        finally:
            store.close()

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

    def _ticket_flow_orchestration_service(self, workspace_root: Path):
        return build_ticket_flow_orchestration_service(workspace_root=workspace_root)

    @staticmethod
    def _close_worker_handles(ensure_result: dict[str, Any]) -> None:
        for key in ("stdout", "stderr"):
            handle = ensure_result.get(key)
            close = getattr(handle, "close", None)
            if callable(close):
                close()

    @staticmethod
    def _select_default_status_run(
        records: list[FlowRunRecord],
    ) -> Optional[FlowRunRecord]:
        if not records:
            return None
        return select_ticket_flow_run_record(records, selection="authoritative")

    @staticmethod
    def _build_historical_runs_picker(
        runs: list[FlowRunRecord],
    ) -> list[dict[str, Any]]:
        if not runs:
            return []
        run_tuples = [(run.id, run.status.value) for run in runs]
        return [
            build_flow_runs_picker(
                run_tuples,
                placeholder="Select a historical run...",
            )
        ]

    @staticmethod
    def _build_flow_status_components(
        record: FlowRunRecord,
        runs: list[FlowRunRecord],
    ) -> list[dict[str, Any]]:
        components = build_flow_status_buttons(
            record.id,
            record.status.value,
            include_refresh=True,
        )
        run_tuples = [(run.id, run.status.value) for run in runs]
        if len(run_tuples) > 1:
            components.append(
                build_flow_runs_picker(
                    run_tuples,
                    placeholder="Select another run...",
                    current_run_id=record.id,
                )
            )
        return components

    @staticmethod
    def _format_flow_status_response_text(
        record: FlowRunRecord,
        snapshot: dict[str, Any],
    ) -> str:
        return "\n".join(format_ticket_flow_status_lines(record, snapshot))

    def _build_flow_status_message(
        self,
        *,
        record: FlowRunRecord,
        runs: list[FlowRunRecord],
        snapshot: dict[str, Any],
        prefix: Optional[str] = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        response_text = self._format_flow_status_response_text(record, snapshot)
        if isinstance(prefix, str) and prefix.strip():
            response_text = f"{prefix.strip()}\n\n{response_text}"
        return response_text, self._build_flow_status_components(record, runs)

    @staticmethod
    def _build_flow_status_summary_fallback(
        workspace_root: Path,
    ) -> Optional[str]:
        progress = ticket_progress(workspace_root)
        if int(progress.get("total", 0)) <= 0:
            return None
        display = build_ticket_flow_display(
            status=None,
            done_count=int(progress.get("done", 0)),
            total_count=int(progress.get("total", 0)),
            run_id=None,
        )
        summary: Mapping[str, Any] = {**display, "current_step": None}
        lines = format_ticket_flow_summary_lines(summary)
        if not lines:
            return None
        return "\n".join(lines)

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
        deferred_public = False
        if not update_message:
            deferred_public = await self._defer_public(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
            )
        run_id_opt = await self._resolve_flow_run_input(
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            action="status",
            run_id_opt=options.get("run_id"),
            deferred=deferred_public,
        )
        if run_id_opt is None:
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
            runs: list[FlowRunRecord] = []
            if isinstance(run_id_opt, str) and run_id_opt.strip():
                try:
                    record = self._resolve_flow_run_by_id(
                        store, run_id=run_id_opt.strip()
                    )
                    runs = store.list_flow_runs(flow_type="ticket_flow")
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
            explicit_run_requested = isinstance(run_id_opt, str) and bool(
                run_id_opt.strip()
            )
            if record is None:
                if runs and not explicit_run_requested:
                    content = (
                        "No ticket_flow run found.\n\n"
                        "Use the picker below to inspect historical runs."
                    )
                    components = self._build_historical_runs_picker(runs)
                    if update_message:
                        await self._update_component_message(
                            interaction_id=interaction_id,
                            interaction_token=interaction_token,
                            text=content,
                            components=components,
                        )
                    else:
                        await self._respond_with_components_public(
                            interaction_id,
                            interaction_token,
                            content,
                            components,
                        )
                    return
                if not explicit_run_requested:
                    summary_text = self._build_flow_status_summary_fallback(
                        workspace_root
                    )
                    if summary_text:
                        if update_message:
                            await self._update_component_message(
                                interaction_id=interaction_id,
                                interaction_token=interaction_token,
                                text=summary_text,
                                components=[],
                            )
                        else:
                            await self._send_or_respond_with_components_public(
                                interaction_id=interaction_id,
                                interaction_token=interaction_token,
                                deferred=deferred_public,
                                text=summary_text,
                                components=[],
                            )
                        return
                message = (
                    f"Ticket_flow run {run_id_opt.strip()} not found."
                    if explicit_run_requested
                    else "No ticket_flow runs found."
                )
                await self._send_or_respond_ephemeral(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    deferred=deferred_public,
                    text=message,
                )
                return
            try:
                record, _updated, locked = reconcile_flow_run(
                    workspace_root, record, store
                )
                if locked:
                    await self._send_or_respond_ephemeral(
                        interaction_id=interaction_id,
                        interaction_token=interaction_token,
                        deferred=deferred_public,
                        text=f"Run {record.id} is locked for reconcile; try again.",
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

        response_text, status_buttons = self._build_flow_status_message(
            record=record,
            runs=runs,
            snapshot=snapshot,
        )
        run_mirror = self._flow_run_mirror(workspace_root)
        run_mirror.mirror_inbound(
            run_id=record.id,
            platform="discord",
            event_type="flow_status_command",
            kind="command",
            actor="user",
            text="/flow status",
            chat_id=channel_id,
            thread_id=guild_id,
            message_id=interaction_id,
            meta={"interaction_token": interaction_token},
        )
        response_type = "component_update" if update_message else "channel"
        run_mirror.mirror_outbound(
            run_id=record.id,
            platform="discord",
            event_type="flow_status_notice",
            kind="notice",
            actor="car",
            text=response_text,
            chat_id=channel_id,
            thread_id=guild_id,
            meta={"response_type": response_type},
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
                await self._send_or_respond_with_components_public(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    deferred=deferred_public,
                    text=response_text,
                    components=status_buttons,
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
                await self._send_or_respond_public(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    deferred=deferred_public,
                    text=response_text,
                )

    async def _handle_flow_runs(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        options: dict[str, Any],
    ) -> None:
        deferred = await self._defer_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )
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
            await self._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text="No ticket_flow runs found.",
            )
            return

        run_tuples = [(record.id, record.status.value) for record in runs]
        components = [build_flow_runs_picker(run_tuples)]
        lines = [f"Recent ticket_flow runs (limit={limit}):"]
        for record in runs:
            lines.append(f"- {record.id} [{record.status.value}]")
        await self._send_or_respond_with_components_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text="\n".join(lines),
            components=components,
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
                "Provide an issue reference: `/flow issue issue_ref:<issue#|url>`",
            )
            return
        issue_ref = issue_ref.strip()
        deferred = await self._defer_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )
        try:

            def _seed_issue() -> Any:
                seeded = seed_issue_from_github(
                    workspace_root,
                    issue_ref,
                    github_service_factory=lambda repo_root: cast(
                        GitHubServiceProtocol, GitHubService(repo_root)
                    ),
                )
                atomic_write(issue_md_path(workspace_root), seeded.content)
                return seeded

            seed = await asyncio.to_thread(
                _seed_issue,
            )
        except GitHubError as exc:
            await self._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=f"GitHub error: {exc}",
            )
            return
        except RuntimeError as exc:
            await self._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=str(exc),
            )
            return
        except Exception as exc:
            await self._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=f"Failed to fetch issue: {exc}",
            )
            return
        await self._send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=f"Seeded ISSUE.md from GitHub issue {seed.issue_number}.",
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
                "Provide a plan: `/flow plan text:<plan>`",
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
        deferred_public: Optional[bool] = None,
    ) -> None:
        force_new = bool(options.get("force_new"))
        restart_from = options.get("restart_from")
        flow_service = self._ticket_flow_orchestration_service(workspace_root)
        if deferred_public is None:
            deferred_public = await self._defer_public(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
            )

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
                flow_service.ensure_flow_run_worker(
                    active_or_paused.id,
                    is_terminal=active_or_paused.status.is_terminal(),
                )
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
                        record = self._resolve_flow_run_by_id(
                            store, run_id=active_or_paused.id
                        )
                        runs = store.list_flow_runs(flow_type="ticket_flow")
                    except (sqlite3.Error, OSError) as exc:
                        log_event(
                            self._logger,
                            logging.ERROR,
                            "discord.flow.query_failed",
                            exc=exc,
                            run_id=active_or_paused.id,
                        )
                        raise DiscordTransientError(
                            f"Failed to query flow run: {exc}",
                            user_message="Unable to query flow database. Please try again later.",
                        ) from None
                    if record is None:
                        await self._send_or_respond_public(
                            interaction_id=interaction_id,
                            interaction_token=interaction_token,
                            deferred=deferred_public,
                            text=(
                                "Reusing ticket_flow run "
                                f"{active_or_paused.id} ({active_or_paused.status.value})."
                            ),
                        )
                        return
                    try:
                        record, _updated, locked = reconcile_flow_run(
                            workspace_root, record, store
                        )
                        if locked:
                            await self._send_or_respond_ephemeral(
                                interaction_id=interaction_id,
                                interaction_token=interaction_token,
                                deferred=deferred_public,
                                text=f"Run {record.id} is locked for reconcile; try again.",
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
                        snapshot = build_flow_status_snapshot(
                            workspace_root, record, store
                        )
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
                response_text, status_buttons = self._build_flow_status_message(
                    record=record,
                    runs=runs,
                    snapshot=snapshot,
                    prefix=(
                        f"Reusing ticket_flow run {record.id} ({record.status.value})."
                    ),
                )
                if status_buttons:
                    await self._send_or_respond_with_components_public(
                        interaction_id=interaction_id,
                        interaction_token=interaction_token,
                        deferred=deferred_public,
                        text=response_text,
                        components=status_buttons,
                    )
                else:
                    await self._send_or_respond_public(
                        interaction_id=interaction_id,
                        interaction_token=interaction_token,
                        deferred=deferred_public,
                        text=response_text,
                    )
                return

        metadata: dict[str, Any] = {"origin": "discord", "force_new": force_new}
        if isinstance(restart_from, str) and restart_from.strip():
            metadata["restart_from"] = restart_from.strip()

        try:
            started = await flow_service.start_flow_run(
                "ticket_flow",
                input_data={},
                metadata=metadata,
            )
        except ValueError as exc:
            await self._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred_public,
                text=str(exc),
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
            try:
                record = self._resolve_flow_run_by_id(store, run_id=started.run_id)
                runs = store.list_flow_runs(flow_type="ticket_flow")
            except (sqlite3.Error, OSError) as exc:
                log_event(
                    self._logger,
                    logging.ERROR,
                    "discord.flow.query_failed",
                    exc=exc,
                    run_id=started.run_id,
                )
                raise DiscordTransientError(
                    f"Failed to query flow run: {exc}",
                    user_message="Unable to query flow database. Please try again later.",
                ) from None
            if record is None:
                prefix = "Started new" if force_new else "Started"
                await self._send_or_respond_public(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    deferred=deferred_public,
                    text=f"{prefix} ticket_flow run {started.run_id}.",
                )
                return
            try:
                record, _updated, locked = reconcile_flow_run(
                    workspace_root, record, store
                )
                if locked:
                    await self._send_or_respond_ephemeral(
                        interaction_id=interaction_id,
                        interaction_token=interaction_token,
                        deferred=deferred_public,
                        text=f"Run {record.id} is locked for reconcile; try again.",
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

        prefix = "Started new" if force_new else "Started"
        response_text, status_buttons = self._build_flow_status_message(
            record=record,
            runs=runs,
            snapshot=snapshot,
            prefix=f"{prefix} ticket_flow run {record.id}.",
        )
        if status_buttons:
            await self._send_or_respond_with_components_public(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred_public,
                text=response_text,
                components=status_buttons,
            )
        else:
            await self._send_or_respond_public(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred_public,
                text=response_text,
            )

    async def _handle_flow_restart(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        options: dict[str, Any],
        deferred_public: Optional[bool] = None,
    ) -> None:
        if deferred_public is None:
            deferred_public = await self._defer_public(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
            )
        run_id_opt = await self._resolve_flow_run_input(
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            action="restart",
            run_id_opt=options.get("run_id"),
            deferred=deferred_public,
        )
        if run_id_opt is None:
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
                target = select_ticket_flow_run_record(runs, selection="active")
        finally:
            store.close()

        if isinstance(run_id_opt, str) and run_id_opt.strip() and target is None:
            await self._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred_public,
                text=f"No ticket_flow run found for run_id {run_id_opt.strip()}.",
            )
            return

        flow_service = self._ticket_flow_orchestration_service(workspace_root)
        if target is not None and target.status.is_active():
            try:
                await flow_service.stop_flow_run(target.id)
            except ValueError as exc:
                await self._send_or_respond_ephemeral(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    deferred=deferred_public,
                    text=str(exc),
                )
                return
            latest = await flow_service.wait_for_flow_run_terminal(target.id)
            if latest is None or latest.status not in {
                "completed",
                "stopped",
                "failed",
            }:
                status_value = latest.status if latest is not None else "unknown"
                await self._send_or_respond_public(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    deferred=deferred_public,
                    text=(
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
            deferred_public=deferred_public,
        )

    async def _handle_flow_recover(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        options: dict[str, Any],
    ) -> None:
        deferred = await self._defer_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )
        run_id_opt = await self._resolve_flow_run_input(
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            action="recover",
            run_id_opt=options.get("run_id"),
            deferred=deferred,
        )
        if run_id_opt is None:
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
                target = select_ticket_flow_run_record(runs, selection="active")

            if target is None:
                await self._send_or_respond_ephemeral(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    deferred=deferred,
                    text="No active ticket_flow run found.",
                )
                return

            flow_service = self._ticket_flow_orchestration_service(workspace_root)
            target_run, updated, locked = flow_service.reconcile_flow_run(target.id)
            if locked:
                await self._send_or_respond_ephemeral(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    deferred=deferred,
                    text=f"Run {target_run.run_id} is locked for reconcile; try again.",
                )
                return
            verdict = "Recovered" if updated else "No changes needed"
            await self._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=f"{verdict} for run {target_run.run_id} ({target_run.status}).",
            )
        finally:
            store.close()

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
        deferred = await self._defer_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )
        run_id_opt = await self._resolve_flow_run_input(
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            action="resume",
            run_id_opt=options.get("run_id"),
            deferred=deferred,
        )
        if run_id_opt is None:
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
                target = select_ticket_flow_run_record(runs, selection="paused")
        finally:
            store.close()

        if target is None:
            await self._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text="No paused ticket_flow run found to resume.",
            )
            return

        run_mirror = self._flow_run_mirror(workspace_root)
        run_mirror.mirror_inbound(
            run_id=target.id,
            platform="discord",
            event_type="flow_resume_command",
            kind="command",
            actor="user",
            text="/flow resume",
            chat_id=channel_id,
            thread_id=guild_id,
            message_id=interaction_id,
        )
        flow_service = self._ticket_flow_orchestration_service(workspace_root)
        try:
            updated = await flow_service.resume_flow_run(target.id)
        except ValueError as exc:
            await self._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=str(exc),
            )
            return

        outbound_text = f"Resumed run {updated.run_id}."
        await self._send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=outbound_text,
        )
        run_mirror.mirror_outbound(
            run_id=updated.run_id,
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
        deferred = await self._defer_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )
        run_id_opt = await self._resolve_flow_run_input(
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            action="stop",
            run_id_opt=options.get("run_id"),
            deferred=deferred,
        )
        if run_id_opt is None:
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
                target = select_ticket_flow_run_record(runs, selection="non_terminal")
        finally:
            store.close()

        if target is None:
            await self._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text="No active ticket_flow run found to stop.",
            )
            return

        run_mirror = self._flow_run_mirror(workspace_root)
        run_mirror.mirror_inbound(
            run_id=target.id,
            platform="discord",
            event_type="flow_stop_command",
            kind="command",
            actor="user",
            text="/flow stop",
            chat_id=channel_id,
            thread_id=guild_id,
            message_id=interaction_id,
        )
        flow_service = self._ticket_flow_orchestration_service(workspace_root)
        try:
            updated = await flow_service.stop_flow_run(target.id)
        except ValueError as exc:
            await self._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=str(exc),
            )
            return

        outbound_text = f"Stop requested for run {updated.run_id} ({updated.status})."
        await self._send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=outbound_text,
        )
        run_mirror.mirror_outbound(
            run_id=updated.run_id,
            platform="discord",
            event_type="flow_stop_notice",
            kind="notice",
            actor="car",
            text=outbound_text,
            chat_id=channel_id,
            thread_id=guild_id,
            meta={"status": updated.status},
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
        confirmed = bool(options.get("confirmed"))
        run_id_opt = options.get("run_id")
        if isinstance(run_id_opt, str) and run_id_opt.strip():
            run_id_opt = await self._resolve_flow_run_input(
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
                action="archive",
                run_id_opt=run_id_opt,
            )
            if run_id_opt is None:
                return
        else:
            run_id_opt = ""
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
                    target = select_default_ticket_flow_run(store)
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

        if target is None:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "No ticket_flow run found to archive.",
            )
            return

        archive_mode = resolve_ticket_flow_archive_mode(target)
        if archive_mode == "blocked":
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                (
                    f"Run {target.id} is {target.status.value}. "
                    "Stop or pause it before archiving."
                ),
            )
            return

        if archive_mode == "confirm" and not confirmed:
            await self._respond_with_components(
                interaction_id,
                interaction_token,
                self._flow_archive_prompt_text(target),
                self._build_flow_archive_confirmation_components(
                    target.id,
                    prompt_variant=True,
                ),
            )
            return

        run_mirror = self._flow_run_mirror(workspace_root)
        run_mirror.mirror_inbound(
            run_id=target.id,
            platform="discord",
            event_type="flow_archive_command",
            kind="command",
            actor="user",
            text="/flow archive",
            chat_id=channel_id,
            thread_id=guild_id,
            message_id=interaction_id,
        )
        flow_service = self._ticket_flow_orchestration_service(workspace_root)
        deferred = await self._defer_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )
        try:
            summary = await asyncio.to_thread(
                flow_service.archive_flow_run,
                target.id,
                force=ticket_flow_archive_requires_force(target),
                delete_run=True,
            )
        except KeyError:
            await self._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=(
                    f"Run {target.id} no longer exists. "
                    "Use /flow status or /flow runs to inspect historical runs."
                ),
            )
            return
        except ValueError as exc:
            await self._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=str(exc),
            )
            return

        outbound_text = (
            f"Archived run {summary['run_id']} "
            f"(tickets={summary['archived_tickets']}, "
            f"runs_archived={summary['archived_runs']}, "
            f"contextspace={summary['archived_contextspace']})."
        )
        await self._send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=outbound_text,
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

        deferred = await self._defer_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )
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
                deferred=deferred,
            )
            return
        if isinstance(run_id_opt, str) and run_id_opt.strip():
            run_id_opt = await self._resolve_flow_run_input(
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
                action="reply",
                run_id_opt=run_id_opt,
                deferred=deferred,
            )
            if run_id_opt is None:
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
            await self._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text="No paused ticket_flow run found for reply.",
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

        flow_service = self._ticket_flow_orchestration_service(workspace_root)
        try:
            updated = await flow_service.resume_flow_run(target.id)
        except ValueError as exc:
            await self._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=str(exc),
            )
            return

        outbound_text = (
            f"Reply saved to {reply_path.name} and resumed run {updated.run_id}."
        )
        await self._send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=outbound_text,
        )
        run_mirror.mirror_outbound(
            run_id=updated.run_id,
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
        run_paths = resolve_outbox_paths(
            workspace_root=workspace_root,
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
        return list_regular_files(folder)

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
            if not _path_within(root=source_dir, target=path):
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
        return delete_regular_files(folder)

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
        await handle_pma_on(
            self,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            guild_id=guild_id,
        )

    async def _handle_pma_off(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
    ) -> None:
        await handle_pma_off(
            self,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
        )

    async def _handle_pma_status(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
    ) -> None:
        await handle_pma_status(
            self,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
        )

    async def _respond_ephemeral(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
    ) -> None:
        max_len = max(int(self._config.max_message_length), 32)
        content = truncate_for_discord(text, max_len=max_len)
        if self._prepared_interaction_policy(interaction_token) is not None:
            sent_followup = await self._send_followup_ephemeral(
                interaction_token=interaction_token,
                content=content,
            )
            if sent_followup:
                return
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
        if self._prepared_interaction_policy(interaction_token) is not None:
            return True
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
        self._remember_prepared_interaction_policy(
            interaction_token=interaction_token,
            policy="defer_ephemeral",
        )
        return True

    async def _defer_component_update(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
    ) -> bool:
        if self._prepared_interaction_policy(interaction_token) is not None:
            return True
        try:
            await self._rest.create_interaction_response(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                payload={"type": 6},
            )
        except DiscordAPIError as exc:
            self._logger.warning(
                "Failed to defer component update: %s (interaction_id=%s)",
                exc,
                interaction_id,
            )
            return False
        self._remember_prepared_interaction_policy(
            interaction_token=interaction_token,
            policy="defer_component_update",
        )
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

    async def _send_or_respond_with_components_ephemeral(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        deferred: bool,
        text: str,
        components: list[dict[str, Any]],
    ) -> None:
        if deferred:
            max_len = max(int(self._config.max_message_length), 32)
            sent = await self._send_followup_ephemeral(
                interaction_token=interaction_token,
                content=truncate_for_discord(text, max_len=max_len),
                components=components,
            )
            if sent:
                return
        await self._respond_with_components(
            interaction_id, interaction_token, text, components
        )

    async def _send_or_update_component_message(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        deferred: bool,
        text: str,
        components: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        if deferred:
            updated = await self._edit_original_component_message(
                interaction_token=interaction_token,
                text=text,
                components=components,
            )
            if updated:
                return
            max_len = max(int(self._config.max_message_length), 32)
            sent = await self._send_followup_ephemeral(
                interaction_token=interaction_token,
                content=truncate_for_discord(text, max_len=max_len),
                components=components,
            )
            if sent:
                return
            return
        await self._update_component_message(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            text=text,
            components=components or [],
        )

    async def _respond_with_components(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
        components: list[dict[str, Any]],
    ) -> None:
        max_len = max(int(self._config.max_message_length), 32)
        content = truncate_for_discord(text, max_len=max_len)
        prepared_policy = self._prepared_interaction_policy(interaction_token)
        if prepared_policy == "defer_component_update":
            updated = await self._edit_original_component_message(
                interaction_token=interaction_token,
                text=content,
                components=components,
            )
            if updated:
                return
        if prepared_policy is not None:
            sent_followup = await self._send_followup_ephemeral(
                interaction_token=interaction_token,
                content=content,
                components=components,
            )
            if sent_followup:
                return
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
        prepared_policy = self._prepared_interaction_policy(interaction_token)
        if prepared_policy == "defer_component_update":
            updated = await self._edit_original_component_message(
                interaction_token=interaction_token,
                text=content,
                components=components,
            )
            if updated:
                return
        if prepared_policy is not None:
            sent_followup = await self._send_followup_ephemeral(
                interaction_token=interaction_token,
                content=content,
                components=components,
            )
            if sent_followup:
                return
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

    async def _edit_original_component_message(
        self,
        *,
        interaction_token: str,
        text: str,
        components: Optional[list[dict[str, Any]]] = None,
    ) -> bool:
        application_id = (self._config.application_id or "").strip()
        if not application_id:
            return False
        max_len = max(int(self._config.max_message_length), 32)
        payload: dict[str, Any] = {
            "content": truncate_for_discord(text, max_len=max_len),
            "components": components or [],
        }
        try:
            await self._rest.edit_original_interaction_response(
                application_id=application_id,
                interaction_token=interaction_token,
                payload=payload,
            )
        except DiscordAPIError as exc:
            self._logger.error(
                "Failed to edit original interaction response: %s",
                exc,
            )
            return False
        return True

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

    async def _respond_public(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
    ) -> None:
        max_len = max(int(self._config.max_message_length), 32)
        content = truncate_for_discord(text, max_len=max_len)
        prepared_policy = self._prepared_interaction_policy(interaction_token)
        if prepared_policy == "defer_component_update":
            updated = await self._edit_original_component_message(
                interaction_token=interaction_token,
                text=content,
                components=[],
            )
            if updated:
                return
        if prepared_policy is not None:
            sent_followup = await self._send_followup_public(
                interaction_token=interaction_token,
                content=content,
            )
            if sent_followup:
                return
        try:
            await self._rest.create_interaction_response(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                payload={
                    "type": 4,
                    "data": {
                        "content": content,
                    },
                },
            )
        except DiscordAPIError as exc:
            sent_followup = await self._send_followup_public(
                interaction_token=interaction_token,
                content=content,
            )
            if not sent_followup:
                self._logger.error(
                    "Failed to send public response: %s (interaction_id=%s)",
                    exc,
                    interaction_id,
                )

    async def _defer_public(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
    ) -> bool:
        if self._prepared_interaction_policy(interaction_token) is not None:
            return True
        try:
            await self._rest.create_interaction_response(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                payload={"type": 5},
            )
        except DiscordAPIError as exc:
            self._logger.warning(
                "Failed to defer public response: %s (interaction_id=%s)",
                exc,
                interaction_id,
            )
            return False
        self._remember_prepared_interaction_policy(
            interaction_token=interaction_token,
            policy="defer_public",
        )
        return True

    async def _send_or_respond_public(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        deferred: bool,
        text: str,
    ) -> None:
        if deferred:
            max_len = max(int(self._config.max_message_length), 32)
            sent = await self._send_followup_public(
                interaction_token=interaction_token,
                content=truncate_for_discord(text, max_len=max_len),
            )
            if sent:
                return
        await self._respond_public(interaction_id, interaction_token, text)

    async def _send_or_respond_with_components_public(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        deferred: bool,
        text: str,
        components: list[dict[str, Any]],
    ) -> None:
        if deferred:
            max_len = max(int(self._config.max_message_length), 32)
            sent = await self._send_followup_public(
                interaction_token=interaction_token,
                content=truncate_for_discord(text, max_len=max_len),
                components=components,
            )
            if sent:
                return
        await self._respond_with_components_public(
            interaction_id, interaction_token, text, components
        )

    async def _respond_with_components_public(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
        components: list[dict[str, Any]],
    ) -> None:
        max_len = max(int(self._config.max_message_length), 32)
        content = truncate_for_discord(text, max_len=max_len)
        prepared_policy = self._prepared_interaction_policy(interaction_token)
        if prepared_policy == "defer_component_update":
            updated = await self._edit_original_component_message(
                interaction_token=interaction_token,
                text=content,
                components=components,
            )
            if updated:
                return
        if prepared_policy is not None:
            sent_followup = await self._send_followup_public(
                interaction_token=interaction_token,
                content=content,
                components=components,
            )
            if sent_followup:
                return
        try:
            await self._rest.create_interaction_response(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                payload={
                    "type": 4,
                    "data": {
                        "content": content,
                        "components": components,
                    },
                },
            )
        except DiscordAPIError as exc:
            sent_followup = await self._send_followup_public(
                interaction_token=interaction_token,
                content=content,
                components=components,
            )
            if not sent_followup:
                self._logger.error(
                    "Failed to send public component response: %s (interaction_id=%s)",
                    exc,
                    interaction_id,
                )

    async def _send_followup_public(
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

        policy_result = self._evaluate_interaction_collaboration_policy(
            channel_id=channel_id,
            guild_id=extract_guild_id(interaction_payload),
            user_id=user_id,
        )
        if not policy_result.command_allowed:
            self._log_collaboration_policy_result(
                channel_id=channel_id,
                guild_id=extract_guild_id(interaction_payload),
                user_id=user_id,
                interaction_id=interaction_id,
                result=policy_result,
            )
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

        interaction_message_id: Optional[str] = None
        interaction_message = interaction_payload.get("message")
        if isinstance(interaction_message, dict):
            message_id_raw = interaction_message.get("id")
            if isinstance(message_id_raw, str) and message_id_raw.strip():
                interaction_message_id = message_id_raw.strip()

        try:
            if custom_id == TICKETS_FILTER_SELECT_ID:
                values = extract_component_values(interaction_payload)
                await self._handle_ticket_filter_component(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                    values=values,
                )
                return

            if custom_id == TICKETS_SELECT_ID:
                values = extract_component_values(interaction_payload)
                await self._handle_ticket_select_component(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                    values=values,
                )
                return

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
                    selected_workspace_value=values[0],
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
                    response_mode="component",
                )
                return

            if custom_id.startswith(f"{UPDATE_CONFIRM_PREFIX}:"):
                raw_target = custom_id.split(":", 1)[1].strip()
                if not raw_target:
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
                    options={"target": raw_target, "confirmed": True},
                    response_mode="component",
                )
                return

            if custom_id.startswith(f"{UPDATE_CANCEL_PREFIX}:"):
                await self._update_component_message(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    text="Update cancelled.",
                    components=[],
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
                            "Reply selection expired. Re-run `/flow reply text:<...>`.",
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

            if custom_id.startswith("approval:"):
                await self._handle_approval_component(
                    interaction_id,
                    interaction_token,
                    custom_id=custom_id,
                )
                return

            if custom_id.startswith("queue_cancel:"):
                await self._handle_queue_cancel_button(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                    custom_id=custom_id,
                    message_id=interaction_message_id,
                    guild_id=extract_guild_id(interaction_payload),
                )
                return

            if custom_id.startswith("queue_interrupt_send:"):
                await self._handle_queue_interrupt_send_button(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                    custom_id=custom_id,
                    message_id=interaction_message_id,
                    guild_id=extract_guild_id(interaction_payload),
                    user_id=user_id,
                )
                return

            if custom_id == "cancel_turn":
                await self._handle_cancel_turn_button(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                    user_id=user_id,
                    message_id=interaction_message_id,
                    custom_id=custom_id,
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
        message_id: Optional[str] = None,
    ) -> None:
        try:
            if custom_id == TICKETS_FILTER_SELECT_ID:
                await self._handle_ticket_filter_component(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                    values=values,
                )
                return

            if custom_id == TICKETS_SELECT_ID:
                await self._handle_ticket_select_component(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                    values=values,
                )
                return

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
                    selected_workspace_value=values[0],
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
                    response_mode="component",
                )
                return

            if custom_id.startswith(f"{UPDATE_CONFIRM_PREFIX}:"):
                raw_target = custom_id.split(":", 1)[1].strip()
                if not raw_target:
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
                    options={"target": raw_target, "confirmed": True},
                    response_mode="component",
                )
                return

            if custom_id.startswith(f"{UPDATE_CANCEL_PREFIX}:"):
                await self._update_component_message(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    text="Update cancelled.",
                    components=[],
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
                            "Reply selection expired. Re-run `/flow reply text:<...>`.",
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

            if custom_id.startswith("approval:"):
                await self._handle_approval_component(
                    interaction_id,
                    interaction_token,
                    custom_id=custom_id,
                )
                return

            if custom_id.startswith("queue_cancel:"):
                await self._handle_queue_cancel_button(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                    custom_id=custom_id,
                    message_id=message_id,
                    guild_id=guild_id,
                )
                return

            if custom_id.startswith("queue_interrupt_send:"):
                await self._handle_queue_interrupt_send_button(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                    custom_id=custom_id,
                    message_id=message_id,
                    guild_id=guild_id,
                    user_id=user_id,
                )
                return

            if custom_id == "cancel_turn":
                await self._handle_cancel_turn_button(
                    interaction_id,
                    interaction_token,
                    channel_id=channel_id,
                    user_id=user_id,
                    message_id=message_id,
                    custom_id=custom_id,
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

    async def _bind_to_workspace_candidate(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        guild_id: Optional[str],
        selected_resource_kind: Optional[str],
        selected_resource_id: Optional[str],
        workspace_path: str,
    ) -> None:
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
            repo_id=(
                selected_resource_id if selected_resource_kind == "repo" else None
            ),
            resource_kind=selected_resource_kind,
            resource_id=selected_resource_id,
        )
        await self._store.clear_pending_compact_seed(channel_id=channel_id)

        if selected_resource_kind == "agent_workspace" and selected_resource_id:
            message = (
                f"Bound this channel to agent workspace: "
                f"{selected_resource_id} ({workspace})"
            )
        elif selected_resource_id:
            message = f"Bound this channel to: {selected_resource_id} ({workspace})"
        else:
            message = f"Bound this channel to workspace: {workspace}"
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            message,
        )

    async def _handle_bind_selection(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        guild_id: Optional[str],
        selected_workspace_value: str,
    ) -> None:
        if selected_workspace_value == "none":
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "No workspace selected.",
            )
            return

        candidates = self._list_bind_workspace_candidates()
        resolved_workspace = self._resolve_workspace_from_token(
            selected_workspace_value,
            candidates,
        )
        if resolved_workspace is None:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Workspace not found: {selected_workspace_value}",
            )
            return

        await self._bind_to_workspace_candidate(
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            guild_id=guild_id,
            selected_resource_kind=resolved_workspace[0],
            selected_resource_id=resolved_workspace[1],
            workspace_path=resolved_workspace[2],
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

        candidates = self._list_bind_workspace_candidates()
        if not candidates:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "No workspaces available to bind.",
            )
            return

        prompt, components = self._build_bind_page_prompt_and_components(
            candidates, page=requested_page
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
        flow_service = self._ticket_flow_orchestration_service(workspace_root)

        if action == "resume":
            try:
                updated = await flow_service.resume_flow_run(run_id)
            except (KeyError, ValueError) as exc:
                await self._respond_ephemeral(
                    interaction_id, interaction_token, str(exc)
                )
                return

            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Resumed run {updated.run_id}.",
            )
        elif action == "stop":
            try:
                updated = await flow_service.stop_flow_run(run_id)
            except (KeyError, ValueError) as exc:
                await self._respond_ephemeral(
                    interaction_id, interaction_token, str(exc)
                )
                return

            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Stop requested for run {updated.run_id} ({updated.status}).",
            )
        elif action in {
            "archive",
            "archive_confirm",
            "archive_cancel",
            "archive_confirm_prompt",
            "archive_cancel_prompt",
        }:
            if action == "archive_cancel":
                await self._handle_flow_status(
                    interaction_id,
                    interaction_token,
                    workspace_root=workspace_root,
                    options={"run_id": run_id},
                    channel_id=channel_id,
                    guild_id=guild_id,
                    update_message=True,
                )
                return
            if action == "archive_cancel_prompt":
                await self._update_component_message(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    text="Archive cancelled.",
                    components=[],
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
                target = self._resolve_flow_run_by_id(store, run_id=run_id)
            except (sqlite3.Error, OSError) as exc:
                log_event(
                    self._logger,
                    logging.ERROR,
                    "discord.flow.query_failed",
                    exc=exc,
                    run_id=run_id,
                )
                raise DiscordTransientError(
                    f"Failed to query flow run: {exc}",
                    user_message="Unable to query flow database. Please try again later.",
                ) from None
            finally:
                store.close()

            if target is None:
                stale_text = (
                    f"Run {run_id} no longer exists. "
                    "Use /flow status or /flow runs to inspect historical runs."
                )
                await self._respond_ephemeral(
                    interaction_id, interaction_token, stale_text
                )
                return

            archive_mode = resolve_ticket_flow_archive_mode(target)
            blocked_text = f"Run {target.id} is {target.status.value}. Stop or pause it before archiving."
            if archive_mode == "blocked":
                if action.endswith("_prompt"):
                    await self._update_component_message(
                        interaction_id=interaction_id,
                        interaction_token=interaction_token,
                        text=blocked_text,
                        components=[],
                    )
                else:
                    await self._respond_ephemeral(
                        interaction_id,
                        interaction_token,
                        blocked_text,
                    )
                return

            if action == "archive" and archive_mode == "confirm":
                await self._update_component_message(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    text=self._flow_archive_prompt_text(target),
                    components=self._build_flow_archive_confirmation_components(
                        target.id,
                        prompt_variant=False,
                    ),
                )
                return

            deferred = await self._defer_component_update(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
            )
            try:
                summary = await asyncio.to_thread(
                    flow_service.archive_flow_run,
                    run_id,
                    force=ticket_flow_archive_requires_force(target),
                    delete_run=True,
                )
            except KeyError:
                stale_text = (
                    f"Run {run_id} no longer exists. "
                    "Use /flow status or /flow runs to inspect historical runs."
                )
                if deferred:
                    updated = await self._edit_original_component_message(
                        interaction_token=interaction_token,
                        text=stale_text,
                        components=[],
                    )
                    if updated:
                        return
                await self._respond_ephemeral(
                    interaction_id, interaction_token, stale_text
                )
                return
            except ValueError as exc:
                await self._send_or_respond_ephemeral(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    deferred=deferred,
                    text=str(exc),
                )
                return

            archived_text = (
                f"Archived run {summary['run_id']} "
                f"(tickets={summary['archived_tickets']}, "
                f"runs_archived={summary['archived_runs']}, "
                f"contextspace={summary['archived_contextspace']}).\n\n"
                "Use /flow status or /flow runs to inspect historical runs."
            )
            if deferred:
                updated = await self._edit_original_component_message(
                    interaction_token=interaction_token,
                    text=archived_text,
                    components=[],
                )
                if updated:
                    return
            await self._update_component_message(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                text=archived_text,
                components=[],
            )
        elif action == "restart":
            await self._handle_flow_restart(
                interaction_id,
                interaction_token,
                workspace_root=workspace_root,
                options={"run_id": run_id},
                deferred_public=False,
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

        agent = self._normalize_agent(binding.get("agent"))
        resource_kind = (
            str(binding.get("resource_kind")).strip()
            if isinstance(binding.get("resource_kind"), str)
            and str(binding.get("resource_kind")).strip()
            else None
        )
        resource_id = (
            str(binding.get("resource_id")).strip()
            if isinstance(binding.get("resource_id"), str)
            and str(binding.get("resource_id")).strip()
            else None
        )

        try:
            had_previous, _new_thread_id = await self._reset_discord_thread_binding(
                channel_id=channel_id,
                workspace_root=workspace_root,
                agent=agent,
                repo_id=(
                    str(binding.get("repo_id")).strip()
                    if isinstance(binding.get("repo_id"), str)
                    and str(binding.get("repo_id")).strip()
                    else None
                ),
                resource_kind=resource_kind,
                resource_id=resource_id,
                pma_enabled=pma_enabled,
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.reset.failed",
                channel_id=channel_id,
                workspace_root=str(workspace_root),
                agent=agent,
                exc=exc,
            )
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Failed to reset Discord thread state.",
            )
            return
        await self._store.clear_pending_compact_seed(channel_id=channel_id)
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

        current_agent = self._normalize_agent(binding.get("agent"))

        supports_review = self._agent_supports_capability(current_agent, "review")
        if not supports_review:
            supported_agents = self._agents_supporting_capability("review")
            supported_hint = (
                f" Switch to an agent with review support: {', '.join(supported_agents)}."
                if supported_agents
                else ""
            )
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"{self._agent_display_name(current_agent)} does not support code review in Discord.{supported_hint}",
            )
            return

        deferred = await self._defer_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )
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
                    commits = await self._list_recent_commits_for_picker(workspace_root)
                    commit_subjects = {
                        commit_sha: subject for commit_sha, subject in commits
                    }
                    search_items = [
                        (commit_sha, commit_sha) for commit_sha, _ in commits
                    ]
                    aliases = {
                        commit_sha: (subject,) if subject else ()
                        for commit_sha, subject in commits
                    }

                    async def _prompt_commit_matches(
                        query_text: str,
                        filtered_search_items: list[tuple[str, str]],
                    ) -> None:
                        filtered_commits = [
                            (commit_sha, commit_subjects.get(commit_sha, ""))
                            for commit_sha, _label in filtered_search_items
                        ]
                        await self._send_or_respond_with_components_ephemeral(
                            interaction_id=interaction_id,
                            interaction_token=interaction_token,
                            deferred=deferred,
                            text=(
                                f"Matched {len(filtered_commits)} commits for `{query_text}`. "
                                "Select a commit to review:"
                            ),
                            components=[
                                build_review_commit_picker(
                                    filtered_commits,
                                    custom_id=REVIEW_COMMIT_SELECT_ID,
                                )
                            ],
                        )

                    resolved_commit = await self._resolve_picker_query_or_prompt(
                        query=sha,
                        items=search_items,
                        limit=DISCORD_SELECT_OPTION_MAX_OPTIONS,
                        aliases=aliases,
                        prompt_filtered_items=_prompt_commit_matches,
                    )
                    if resolved_commit is not None:
                        target_type = "commit"
                        target_value = resolved_commit
                    elif commits:
                        return
                    else:
                        target_type = "commit"
                        target_value = sha
                else:
                    prompt_commit_picker = True
            elif target_lower in ("uncommitted", ""):
                pass
            elif target_lower == "custom":
                await self._send_or_respond_ephemeral(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    deferred=deferred,
                    text=(
                        "Provide custom review instructions after `custom`, for example: "
                        "`/car review target:custom focus on security regressions`."
                    ),
                )
                return
            elif target_lower.startswith("custom "):
                custom_instructions = target_text[7:].strip()
                if not custom_instructions:
                    await self._send_or_respond_ephemeral(
                        interaction_id=interaction_id,
                        interaction_token=interaction_token,
                        deferred=deferred,
                        text=(
                            "Provide custom review instructions after `custom`, for example: "
                            "`/car review target:custom focus on security regressions`."
                        ),
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
                await self._send_or_respond_ephemeral(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    deferred=deferred,
                    text="No recent commits found. Use `/car review target:commit <sha>`.",
                )
                return
            await self._send_or_respond_with_components_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text="Select a commit to review:",
                components=[
                    build_review_commit_picker(
                        commits, custom_id=REVIEW_COMMIT_SELECT_ID
                    )
                ],
            )
            return

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

        agent = self._normalize_agent(binding.get("agent"))
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
                        f"Usage: /car approvals {APPROVAL_MODE_USAGE}",
                    ]
                ),
            )
            return

        new_mode = normalize_approval_mode(mode, include_command_aliases=True)
        if new_mode is None:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Unknown mode: {mode}. Valid options: {APPROVAL_MODE_USAGE}",
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

        deferred = await self._defer_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )
        max_bytes = 100000
        try:
            data = path.read_bytes()
        except Exception:
            await self._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text="Failed to read file.",
            )
            return

        if len(data) > max_bytes:
            await self._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=f"File too large (max {max_bytes} bytes).",
            )
            return

        null_count = data.count(b"\x00")
        if null_count > 0 and null_count > len(data) * 0.1:
            await self._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text="File appears to be binary; refusing to include it.",
            )
            return

        try:
            display_path = str(path.relative_to(workspace_root))
        except ValueError:
            display_path = str(path)

        if not isinstance(request_arg, str) or not request_arg.strip():
            request_arg = "Please review this file."

        await self._send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=(
                f"File `{display_path}` ready for mention.\n\n"
                f"To include it in a request, send a message starting with:\n"
                f"```\n"
                f'<file path="{display_path}">\n'
                f"...\n"
                f"</file>\n"
                f"```\n\n"
                f"Your request: {request_arg}"
            ),
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
            "- `/car admin experimental action:list`\n"
            "- `/car admin experimental action:enable feature:<feature>`\n"
            "- `/car admin experimental action:disable feature:<feature>`"
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
        async def _finish_compact_interaction(text: str) -> None:
            if deferred:
                updated = await self._edit_original_component_message(
                    interaction_token=interaction_token,
                    text=text,
                    components=[],
                )
                if updated:
                    return
                max_len = max(int(self._config.max_message_length), 32)
                sent = await self._send_followup_ephemeral(
                    interaction_token=interaction_token,
                    content=truncate_for_discord(text, max_len=max_len),
                )
                if sent:
                    return
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                text,
            )

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

        agent = self._normalize_agent(binding.get("agent"))
        model_override = binding.get("model_override")
        if not isinstance(model_override, str) or not model_override.strip():
            model_override = None
        reasoning_effort = binding.get("reasoning_effort")
        if not isinstance(reasoning_effort, str) or not reasoning_effort.strip():
            reasoning_effort = None

        mode = "pma" if pma_enabled else "repo"
        _orchestration_service, _binding_row, current_thread = (
            self._get_discord_thread_binding(channel_id=channel_id, mode=mode)
        )
        lifecycle_status = (
            str(getattr(current_thread, "lifecycle_status", "") or "").strip().lower()
            if current_thread is not None
            else ""
        )
        if current_thread is None or lifecycle_status != "active":
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "No active session to compact. Send a message first to start a conversation.",
            )
            return

        deferred = await self._defer_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )

        interaction_text: Optional[str] = None
        previous_thread_id = current_thread.thread_target_id
        next_thread_id: Optional[str] = None
        try:
            try:
                turn_result = await self._run_agent_turn_for_message(
                    workspace_root=workspace_root,
                    prompt_text=self.COMPACT_SUMMARY_PROMPT,
                    agent=agent,
                    model_override=model_override,
                    reasoning_effort=reasoning_effort,
                    session_key=current_thread.thread_target_id,
                    orchestrator_channel_key=(
                        channel_id if not pma_enabled else f"pma:{channel_id}"
                    ),
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
                interaction_text = (
                    "Compaction failed. Check the channel for the error message."
                )
                return

            response_text = (
                turn_result.final_message.strip() if turn_result.final_message else ""
            )
            if not response_text:
                response_text = "(No summary generated.)"
            try:
                (
                    _had_previous,
                    next_thread_id,
                ) = await self._reset_discord_thread_binding(
                    channel_id=channel_id,
                    workspace_root=workspace_root,
                    agent=agent,
                    repo_id=(
                        str(binding.get("repo_id")).strip()
                        if isinstance(binding.get("repo_id"), str)
                        and str(binding.get("repo_id")).strip()
                        else None
                    ),
                    resource_kind=(
                        str(binding.get("resource_kind")).strip()
                        if isinstance(binding.get("resource_kind"), str)
                        and str(binding.get("resource_kind")).strip()
                        else None
                    ),
                    resource_id=(
                        str(binding.get("resource_id")).strip()
                        if isinstance(binding.get("resource_id"), str)
                        and str(binding.get("resource_id")).strip()
                        else None
                    ),
                    pma_enabled=pma_enabled,
                )
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.compact.reset_failed",
                    channel_id=channel_id,
                    workspace_root=str(workspace_root),
                    exc=exc,
                )
                await self._send_channel_message_safe(
                    channel_id,
                    {
                        "content": "Compact summary generated, but starting a fresh thread failed."
                    },
                )
                interaction_text = "Compaction generated a summary, but starting a fresh thread failed."
                return
            try:
                await self._store.set_pending_compact_seed(
                    channel_id=channel_id,
                    seed_text=build_compact_seed_prompt(response_text),
                    session_key=next_thread_id,
                )
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.compact.seed_save_failed",
                    channel_id=channel_id,
                    workspace_root=str(workspace_root),
                    next_thread_id=next_thread_id,
                    previous_thread_id=previous_thread_id,
                    exc=exc,
                )
                rollback_failed = False
                try:
                    orchestration_service = self._discord_thread_service()
                    if next_thread_id:
                        with contextlib.suppress(Exception):
                            orchestration_service.archive_thread_target(next_thread_id)
                    restored = orchestration_service.resume_thread_target(
                        previous_thread_id
                    )
                    self._attach_discord_thread_binding(
                        channel_id=channel_id,
                        thread_target_id=restored.thread_target_id,
                        agent=agent,
                        repo_id=(
                            str(binding.get("repo_id")).strip()
                            if isinstance(binding.get("repo_id"), str)
                            and str(binding.get("repo_id")).strip()
                            else None
                        ),
                        resource_kind=(
                            str(binding.get("resource_kind")).strip()
                            if isinstance(binding.get("resource_kind"), str)
                            and str(binding.get("resource_kind")).strip()
                            else None
                        ),
                        resource_id=(
                            str(binding.get("resource_id")).strip()
                            if isinstance(binding.get("resource_id"), str)
                            and str(binding.get("resource_id")).strip()
                            else None
                        ),
                        workspace_root=workspace_root,
                        pma_enabled=pma_enabled,
                    )
                except Exception as rollback_exc:
                    rollback_failed = True
                    log_event(
                        self._logger,
                        logging.ERROR,
                        "discord.compact.seed_save_rollback_failed",
                        channel_id=channel_id,
                        workspace_root=str(workspace_root),
                        next_thread_id=next_thread_id,
                        previous_thread_id=previous_thread_id,
                        exc=rollback_exc,
                    )
                await self._send_channel_message_safe(
                    channel_id,
                    {
                        "content": (
                            "Compaction summary generated, but saving the compacted context failed. "
                            "Restored the previous thread; please retry."
                            if not rollback_failed
                            else "Compaction summary generated, but saving the compacted context failed and restoring the previous thread also failed."
                        )
                    },
                )
                interaction_text = (
                    "Compaction summary generated, but saving the compacted context failed. "
                    "Restored the previous thread; please retry."
                    if not rollback_failed
                    else "Compaction summary generated, but saving the compacted context failed and restoring the previous thread also failed."
                )
                return

            try:
                chunks = chunk_discord_message(
                    f"**Conversation Summary:**\n\n{response_text}",
                    max_len=self._config.max_message_length,
                    with_numbering=False,
                )
                if not chunks:
                    chunks = ["**Conversation Summary:**\n\n(No summary generated.)"]

                next_chunk_index = 0
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
                    await self._send_channel_message_safe(
                        channel_id,
                        first_payload,
                    )
                    next_chunk_index = 1

                for chunk in chunks[next_chunk_index:]:
                    payload: dict[str, Any] = {"content": chunk}
                    await self._send_channel_message_safe(channel_id, payload)
                await self._flush_outbox_files(
                    workspace_root=workspace_root,
                    channel_id=channel_id,
                )
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.compact.finalize_failed",
                    channel_id=channel_id,
                    workspace_root=str(workspace_root),
                    exc=exc,
                )
                await self._send_channel_message_safe(
                    channel_id,
                    {
                        "content": "Compaction summary posted, but finalizing the fresh thread failed."
                    },
                )
                interaction_text = (
                    "Compaction summary posted, but finalizing the fresh thread failed."
                )
                return
            interaction_text = (
                "Compaction complete. Summary posted in the channel. "
                "Send your next message to continue this session."
            )
        finally:
            if interaction_text:
                await _finish_compact_interaction(interaction_text)

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
        deferred = await self._defer_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )
        client = await self._client_for_workspace(str(workspace_root))
        if client is None:
            await self._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=format_discord_message(
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
            await self._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=format_discord_message("Logout failed; check logs for details."),
            )
            return
        await self._send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text="Logged out.",
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
                "Usage: /car admin feedback reason:<description>",
            )
            return

        deferred = await self._defer_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )
        client = await self._client_for_workspace(str(workspace_root))
        if client is None:
            await self._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=format_discord_message(
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
            await self._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=format_discord_message(
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
        await self._send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=message_text,
        )

    async def _handle_car_archive(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
    ) -> None:
        from ...core.archive import (
            archive_workspace_for_fresh_start,
            resolve_workspace_archive_target,
        )

        workspace_root = await self._require_bound_workspace(
            interaction_id,
            interaction_token,
            channel_id=channel_id,
        )
        if workspace_root is None:
            return

        deferred = await self._defer_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )

        try:
            target = resolve_workspace_archive_target(
                workspace_root,
                hub_root=self._config.root,
                manifest_path=self._manifest_path,
            )
            result = await asyncio.to_thread(
                archive_workspace_for_fresh_start,
                hub_root=self._config.root,
                base_repo_root=target.base_repo_root,
                base_repo_id=target.base_repo_id,
                worktree_repo_root=workspace_root,
                worktree_repo_id=target.workspace_repo_id,
                branch=None,
                worktree_of=target.worktree_of,
                note="Discord /car archive",
                source_path=target.source_path,
            )
        except ValueError as exc:
            await self._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=str(exc),
            )
            return
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.archive_state.failed",
                workspace_root=str(workspace_root),
                exc=exc,
            )
            await self._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=format_discord_message("Archive failed; check logs for details."),
            )
            return

        await self._send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=format_discord_message(
                "\n".join(
                    [
                        (
                            f"Archived workspace state to snapshot `{result.snapshot_id}`."
                            if result.snapshot_id
                            else "Workspace CAR state was already clean."
                        ),
                        f"Archived paths: {', '.join(result.archived_paths) or 'none'}",
                        (
                            f"Archived {len(result.archived_thread_ids)} managed thread"
                            f"{'' if len(result.archived_thread_ids) == 1 else 's'}."
                        ),
                        "The binding remains active for fresh work.",
                    ]
                )
            ),
        )

    async def _handle_car_interrupt(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        active_turn_text: str = "Stopping current turn...",
        progress_reuse_source_message_id: Optional[str] = None,
        progress_reuse_acknowledgement: Optional[str] = None,
        source: str = "unknown",
        source_custom_id: Optional[str] = None,
        source_message_id: Optional[str] = None,
        source_command: Optional[str] = None,
        source_user_id: Optional[str] = None,
    ) -> None:
        log_event(
            self._logger,
            logging.INFO,
            "discord.interrupt.requested",
            channel_id=channel_id,
            interaction_id=interaction_id,
            source=source,
            source_user_id=source_user_id,
            source_command=source_command,
            source_custom_id=source_custom_id,
            source_message_id=source_message_id,
        )
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

        mode = "pma" if pma_enabled else "repo"
        orchestration_service, _binding_row, current_thread = (
            self._get_discord_thread_binding(channel_id=channel_id, mode=mode)
        )
        if current_thread is None:
            if progress_reuse_source_message_id or progress_reuse_acknowledgement:
                clear_discord_turn_progress_reuse(
                    self,
                    thread_target_id=(
                        getattr(_binding_row, "thread_target_id", None) or ""
                    ),
                )
            log_event(
                self._logger,
                logging.INFO,
                "discord.interrupt.no_active_turn",
                channel_id=channel_id,
                interaction_id=interaction_id,
                source=source,
                source_user_id=source_user_id,
                source_command=source_command,
                source_custom_id=source_custom_id,
                source_message_id=source_message_id,
            )
            text = format_discord_message("No active turn to interrupt.")
            await self._respond_ephemeral(interaction_id, interaction_token, text)
            return
        deferred = await self._defer_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )
        try:
            stop_outcome = await orchestration_service.stop_thread(
                current_thread.thread_target_id
            )
            interrupted_active = bool(
                getattr(stop_outcome, "interrupted_active", False)
            )
            recovered_lost_backend = bool(
                getattr(stop_outcome, "recovered_lost_backend", False)
            )
            cancelled_queued = int(getattr(stop_outcome, "cancelled_queued", 0) or 0)
            execution_record = getattr(stop_outcome, "execution", None)
            log_event(
                self._logger,
                logging.INFO,
                "discord.interrupt.completed",
                channel_id=channel_id,
                interaction_id=interaction_id,
                source=source,
                source_user_id=source_user_id,
                source_command=source_command,
                source_custom_id=source_custom_id,
                source_message_id=source_message_id,
                thread_target_id=current_thread.thread_target_id,
                interrupted_active=interrupted_active,
                recovered_lost_backend=recovered_lost_backend,
                cancelled_queued=cancelled_queued,
                execution_id=(
                    execution_record.execution_id
                    if execution_record is not None
                    else None
                ),
                execution_status=(
                    execution_record.status if execution_record is not None else None
                ),
                execution_backend_turn_id=(
                    execution_record.backend_id
                    if execution_record is not None
                    else None
                ),
            )
            if (
                not interrupted_active
                and not recovered_lost_backend
                and not cancelled_queued
            ):
                if progress_reuse_source_message_id or progress_reuse_acknowledgement:
                    clear_discord_turn_progress_reuse(
                        self,
                        thread_target_id=current_thread.thread_target_id,
                    )
                text = format_discord_message("No active turn to interrupt.")
                await self._send_or_respond_ephemeral(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    deferred=deferred,
                    text=text,
                )
                return
            if interrupted_active:
                request_discord_turn_progress_reuse(
                    self,
                    thread_target_id=current_thread.thread_target_id,
                    source_message_id=str(progress_reuse_source_message_id or ""),
                    acknowledgement=str(progress_reuse_acknowledgement or ""),
                )
            elif progress_reuse_source_message_id or progress_reuse_acknowledgement:
                clear_discord_turn_progress_reuse(
                    self,
                    thread_target_id=current_thread.thread_target_id,
                )
            parts = []
            if interrupted_active:
                parts.append(active_turn_text)
            elif recovered_lost_backend:
                parts.append("Recovered stale session after backend thread was lost.")
            if cancelled_queued:
                parts.append(f"Cancelled {cancelled_queued} queued turn(s).")
            text = format_discord_message(
                "Recovered stale session after backend thread was lost."
                if recovered_lost_backend
                else active_turn_text
            )
            if parts:
                text = format_discord_message(" ".join(parts))
            await self._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=text,
            )
        except Exception as exc:
            if progress_reuse_source_message_id or progress_reuse_acknowledgement:
                clear_discord_turn_progress_reuse(
                    self,
                    thread_target_id=current_thread.thread_target_id,
                )
            log_event(
                self._logger,
                logging.WARNING,
                "discord.interrupt.failed",
                channel_id=channel_id,
                interaction_id=interaction_id,
                source=source,
                source_user_id=source_user_id,
                source_command=source_command,
                source_custom_id=source_custom_id,
                source_message_id=source_message_id,
                workspace_root=str(workspace_root),
                thread_target_id=current_thread.thread_target_id,
                exc=exc,
            )
            text = format_discord_message("Interrupt failed. Please try again.")
            await self._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=text,
            )

    async def _handle_cancel_turn_button(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        user_id: Optional[str] = None,
        message_id: Optional[str] = None,
        custom_id: str = "cancel_turn",
    ) -> None:
        await self._handle_car_interrupt(
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            source="component",
            source_custom_id=custom_id,
            source_message_id=message_id,
            source_user_id=user_id,
        )

    async def _handle_queue_cancel_button(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        custom_id: str,
        guild_id: Optional[str],
        message_id: Optional[str] = None,
    ) -> None:
        source_message_id = custom_id.split(":", 1)[1].strip()
        if not source_message_id:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Queued request is unavailable.",
            )
            return
        conversation_id = self._dispatcher_conversation_id(
            channel_id=channel_id,
            guild_id=guild_id,
        )
        cancelled = await self._dispatcher.cancel_pending_message(
            conversation_id,
            source_message_id,
        )
        if not cancelled:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Queued request is no longer pending.",
            )
            return
        await self._clear_queued_notice(
            conversation_id=conversation_id,
            source_message_id=source_message_id,
            channel_id=channel_id,
        )
        if message_id:
            self._queued_notice_messages.pop((conversation_id, source_message_id), None)
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            "Queued request cancelled.",
        )

    async def _handle_queue_interrupt_send_button(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        custom_id: str,
        guild_id: Optional[str],
        user_id: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> None:
        source_message_id = custom_id.split(":", 1)[1].strip()
        if not source_message_id:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Queued request is unavailable.",
            )
            return
        conversation_id = self._dispatcher_conversation_id(
            channel_id=channel_id,
            guild_id=guild_id,
        )
        promoted = await self._dispatcher.promote_pending_message(
            conversation_id,
            source_message_id,
        )
        if not promoted:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Queued request is no longer pending.",
            )
            return
        binding = await self._store.get_binding(channel_id=channel_id)
        pma_enabled = bool(binding.get("pma_enabled", False)) if binding else False
        mode = "pma" if pma_enabled else "repo"
        _orchestration_service, _binding_row, current_thread = (
            self._get_discord_thread_binding(channel_id=channel_id, mode=mode)
        )
        if current_thread is None:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Queued request moved to the front.",
            )
            return
        await self._handle_car_interrupt(
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            active_turn_text="Message received. Switching to it now...",
            progress_reuse_source_message_id=source_message_id,
            progress_reuse_acknowledgement="Message received. Switching to it now...",
            source="component",
            source_custom_id=custom_id,
            source_message_id=message_id,
            source_user_id=user_id,
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
