from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import subprocess
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
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
from ...core.config import (
    ConfigError,
    ensure_hub_config_at,
    find_nearest_hub_config_path,
    load_hub_config,
    load_repo_config,
    resolve_env_for_root,
)
from ...core.diagnostics.loop_attribution import track_loop
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
    FlowRunRecord,
    FlowStore,
    flow_run_duration_seconds,
    format_flow_duration,
)
from ...core.flows.hub_overview import build_hub_flow_overview_entries
from ...core.flows.surface_defaults import should_route_flow_read_to_hub_overview
from ...core.flows.ux_helpers import (
    build_flow_status_snapshot,
    ensure_worker,
    select_default_ticket_flow_run,
    select_ticket_flow_run_record,
    ticket_progress,
)
from ...core.git_utils import (  # noqa: F401 - kept for test monkeypatching
    GitError,
    reset_branch_from_origin_main,
)
from ...core.hub_control_plane import (
    HandshakeCompatibility,
    HttpHubControlPlaneClient,
    HubControlPlaneError,
)
from ...core.hub_control_plane.handshake_startup import perform_startup_hub_handshake
from ...core.hub_control_plane.service import (
    CONTROL_PLANE_API_VERSION as _CONTROL_PLANE_API_VERSION,
)
from ...core.logging_utils import log_event
from ...core.managed_processes import reap_managed_processes
from ...core.orchestration import (
    ORCHESTRATION_SCHEMA_VERSION,
    ChatOperationRecoveryAction,
    ChatOperationSnapshot,
    ChatOperationState,
    SQLiteChatOperationLedger,
    build_ticket_flow_orchestration_service,
    plan_chat_operation_recovery,
)
from ...core.state import now_iso
from ...core.state_roots import resolve_global_state_root
from ...core.update import (  # noqa: F401 - kept for test monkeypatching
    UpdateInProgressError,
    _available_update_target_definitions,
    _format_update_confirmation_warning,
    _normalize_update_ref,
    _normalize_update_target,
    _read_update_status,
    _spawn_update_process,
    _update_target_restarts_surface,
)
from ...core.update_paths import resolve_update_paths  # noqa: F401
from ...core.update_targets import (  # noqa: F401
    all_update_target_definitions,
    get_update_target_label,
)
from ...core.utils import (
    canonicalize_path,
    is_within,
)
from ...flows.ticket_flow.runtime_helpers import build_ticket_flow_controller
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
    chat_agent_supports_effort,
    chat_hermes_profile_options,
    format_chat_agent_selection,
    normalize_chat_agent,
    normalize_hermes_profile,
    resolve_chat_agent_and_profile,
    resolve_chat_runtime_agent,
    valid_chat_agent_values,
)
from ...integrations.chat.channel_directory import ChannelDirectoryStore
from ...integrations.chat.collaboration_policy import (
    CollaborationEvaluationContext,
    CollaborationEvaluationResult,
    build_discord_collaboration_policy,
    evaluate_collaboration_admission,
    evaluate_collaboration_policy,
)
from ...integrations.chat.command_diagnostics import (
    ActiveFlowInfo,
)
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
from ...integrations.chat.managed_thread_lifecycle import (
    bind_surface_thread,
    replace_surface_thread,
    resolve_surface_thread_binding,
)
from ...integrations.chat.media import (
    audio_content_type_for_input,
)
from ...integrations.chat.model_selection import (
    REASONING_EFFORT_VALUES,
    _model_list_with_agent_compat,  # noqa: F401 - re-exported for test compatibility
)
from ...integrations.chat.models import (
    ChatEvent,
    ChatInteractionEvent,
    ChatMessageEvent,
    ChatReplyInfo,
)
from ...integrations.chat.picker_filter import (
    filter_picker_items,
)
from ...integrations.chat.queue_control import ChatQueueControlStore
from ...integrations.chat.run_mirror import ChatRunMirror
from ...integrations.chat.session_messages import (
    build_fresh_session_started_lines,
    build_thread_detail_lines,
)
from ...integrations.chat.turn_policy import (
    PlainTextTurnContext,
    should_trigger_plain_text_turn,
)
from ...integrations.chat.update_notifier import (  # noqa: F401 - kept for test monkeypatching
    ChatUpdateStatusNotifier,
    mark_update_status_notified,
)
from ...integrations.github.context_injection import maybe_inject_github_context
from ...manifest import load_manifest
from ...tickets.files import (
    list_ticket_paths,
    read_ticket,
    read_ticket_frontmatter,
    safe_relpath,
)
from ...tickets.frontmatter import parse_markdown_frontmatter
from ...voice import VoiceConfig, VoiceService, VoiceServiceError
from ...voice.provider_catalog import normalize_voice_provider
from ...voice.service import VoiceTransientError
from ..telegram.constants import DEFAULT_SKILLS_LIST_LIMIT
from ..telegram.helpers import _format_skills_list
from .adapter import DiscordChatAdapter
from .car_command_dispatch import handle_car_command as dispatch_car_command
from .collaboration_helpers import (
    collaboration_probe_text,
)
from .command_registry import sync_commands
from .command_runner import CommandRunner as _CommandRunner
from .command_runner import RunnerConfig as _RunnerConfig
from .components import (
    DISCORD_SELECT_OPTION_MAX_OPTIONS,
    build_model_effort_picker,
    build_ticket_filter_picker,
    build_ticket_picker,
)
from .config import DiscordBotConfig
from .effects import (
    DiscordAutocompleteEffect,
    DiscordComponentResponseEffect,
    DiscordComponentUpdateEffect,
    DiscordDeferEffect,
    DiscordEffect,
    DiscordEffectServiceProxy,
    DiscordEffectSink,
    DiscordFollowupEffect,
    DiscordHandlerResult,
    DiscordModalEffect,
    DiscordOriginalMessageEditEffect,
    DiscordResponseEffect,
)
from .errors import DiscordAPIError, is_unknown_interaction_error
from .flow_commands import (
    build_flow_archive_confirmation_components,
    flow_archive_prompt_text,
    handle_flow_archive,
    handle_flow_button,
    handle_flow_issue,
    handle_flow_plan,
    handle_flow_recover,
    handle_flow_reply,
    handle_flow_restart,
    handle_flow_resume,
    handle_flow_runs,
    handle_flow_start,
    handle_flow_status,
    handle_flow_stop,
    handle_tickets,
    prompt_flow_action_picker,
    resolve_flow_run_input,
    write_user_reply,
)
from .flow_watchers import (
    _scan_and_enqueue_pause_notifications as _scan_and_enqueue_pause_notifications_impl,
)
from .flow_watchers import watch_ticket_flow_pauses, watch_ticket_flow_terminals
from .gateway import DiscordGatewayClient
from .ingress import (
    CommandSpec,
    IngressContext,
    IngressTiming,
    InteractionIngress,
    InteractionKind,
    RuntimeInteractionEnvelope,
)
from .interaction_dispatch import (
    handle_component_interaction as _dispatch_component_interaction,
)
from .interaction_registry import (
    MODEL_EFFORT_SELECT_ID,
    TICKETS_MODAL_PREFIX,
    build_application_commands,
    component_admission_ack_policy,
    component_dispatch_ack_policy,
    component_route_for_custom_id,
    component_workspace_lock_policy,
    dispatch_autocomplete,
    modal_admission_ack_policy,
    modal_route_for_custom_id,
    modal_workspace_lock_policy,
    normalize_discord_command_path,
    slash_command_route_for_path,
    slash_command_workspace_lock_policy,
)
from .interaction_runtime import (
    defer_and_update_runtime_component_message,
    ensure_ephemeral_response_deferred,
)
from .interaction_session import (
    DiscordInteractionSession,
    InteractionSessionKind,
)
from .interactions import extract_interaction_id, extract_interaction_token
from .message_turns import (
    DiscordMessageTurnResult,
    bind_discord_progress_task_context,
    build_discord_thread_orchestration_service,
    reconcile_discord_turn_progress_leases,
    resolve_bound_workspace_root,
    run_agent_turn_for_message,
    run_managed_thread_turn_for_message,
)
from .message_turns import (
    handle_message_event as handle_discord_message_event,
)
from .outbox import DiscordOutboxManager
from .picker_helpers import (  # noqa: F401 - re-exported for test compatibility
    _coerce_model_picker_items,
    _format_session_thread_picker_label,
    _truncate_picker_text,
    list_model_items_for_binding,
    list_opencode_models_for_picker,
    list_recent_commits_for_picker,
    list_session_threads_for_picker,
    list_threads_paginated,
)
from .picker_helpers import (
    format_discord_thread_picker_label as _format_discord_thread_picker_label_impl,
)
from .pma_commands import (
    handle_pma_off,
    handle_pma_on,
    handle_pma_status,
)
from .rendering import (
    chunk_discord_message,
    format_discord_message,
    sanitize_discord_outbound_text,
    truncate_for_discord,
)
from .response_helpers import DiscordResponder
from .rest import DISCORD_INTERACTION_CALLBACK_TIMEOUT_SECONDS, DiscordRestClient
from .service_normalization import (
    DiscordAttachmentAdapter,
    SavedDiscordAttachment,
    build_attachment_context_payload,
    build_discord_approval_message,
    build_discord_queue_notice_message,
    format_hub_flow_overview_line,
)
from .state import DiscordStateStore, InteractionLedgerRecord, OutboxRecord
from .workspace_commands import (
    handle_bind,
    handle_bind_page_component,
    handle_bind_selection,
    handle_debug,
    handle_help,
    handle_ids,
    handle_processes,
    handle_status,
)

_INTERACTION_RECOVERY_MAX_ATTEMPTS = 5
_INTERACTION_RECOVERY_INITIAL_BACKOFF_SECONDS = 5.0
_INTERACTION_RECOVERY_MAX_BACKOFF_SECONDS = 300.0
_INTERACTION_RECOVERY_MAX_UNCHANGED_CURSOR_ATTEMPTS = 3
_INTERACTION_RECOVERY_METADATA_KEY = "_recovery"
_PATCH_STATE_UNSET = object()


def _parse_interaction_recovery_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _interaction_recovery_delay_seconds(attempts: int) -> float:
    if attempts <= 0:
        return 0.0
    delay = _INTERACTION_RECOVERY_INITIAL_BACKOFF_SECONDS * (2 ** max(0, attempts - 1))
    return float(min(_INTERACTION_RECOVERY_MAX_BACKOFF_SECONDS, delay))


def _interaction_recovery_snapshot_hash(payload: dict[str, Any]) -> str:
    serialized = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _read_interaction_recovery_metadata(cursor: dict[str, Any]) -> dict[str, Any]:
    raw = cursor.get(_INTERACTION_RECOVERY_METADATA_KEY)
    return dict(raw) if isinstance(raw, dict) else {}


def _write_interaction_recovery_metadata(
    cursor: dict[str, Any],
    *,
    snapshot_hash: str,
    unchanged_attempts: int,
    scheduled_attempt: int,
    scheduled_at: datetime,
) -> dict[str, Any]:
    updated_cursor = dict(cursor)
    delay_seconds = _interaction_recovery_delay_seconds(scheduled_attempt)
    updated_cursor[_INTERACTION_RECOVERY_METADATA_KEY] = {
        "snapshot_hash": snapshot_hash,
        "unchanged_attempts": max(0, int(unchanged_attempts)),
        "scheduled_attempt": max(0, int(scheduled_attempt)),
        "scheduled_at": scheduled_at.astimezone(timezone.utc).isoformat(),
        "next_retry_at": (scheduled_at + timedelta(seconds=delay_seconds))
        .astimezone(timezone.utc)
        .isoformat(),
        "backoff_seconds": delay_seconds,
    }
    return updated_cursor


def _interaction_recovery_backoff_active(
    *,
    updated_at: Optional[str],
    attempt_count: int,
    now: datetime,
) -> bool:
    if attempt_count <= 0:
        return False
    updated_dt = _parse_interaction_recovery_datetime(updated_at)
    if updated_dt is None:
        return False
    retry_at = updated_dt + timedelta(
        seconds=_interaction_recovery_delay_seconds(attempt_count)
    )
    return now < retry_at


def _plan_delivery_recovery_cursor(
    *,
    cursor: dict[str, Any],
    attempt_count: int,
    now: datetime,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    metadata = _read_interaction_recovery_metadata(cursor)
    next_retry_at = _parse_interaction_recovery_datetime(metadata.get("next_retry_at"))
    if next_retry_at is not None and now < next_retry_at:
        return None, None
    snapshot_source = {
        key: value
        for key, value in cursor.items()
        if key != _INTERACTION_RECOVERY_METADATA_KEY
    }
    snapshot_hash = _interaction_recovery_snapshot_hash(snapshot_source)
    prior_hash = str(metadata.get("snapshot_hash") or "").strip()
    unchanged_attempts = (
        max(0, int(metadata.get("unchanged_attempts") or 0)) + 1
        if prior_hash == snapshot_hash
        else 1
    )
    if unchanged_attempts > _INTERACTION_RECOVERY_MAX_UNCHANGED_CURSOR_ATTEMPTS:
        return None, "unchanged_delivery_cursor"
    scheduled_attempt = max(1, int(attempt_count) + 1)
    return (
        _write_interaction_recovery_metadata(
            snapshot_source,
            snapshot_hash=snapshot_hash,
            unchanged_attempts=unchanged_attempts,
            scheduled_attempt=scheduled_attempt,
            scheduled_at=now,
        ),
        None,
    )


DISCORD_EPHEMERAL_FLAG = 64
CHAT_QUEUE_RESET_POLL_INTERVAL_SECONDS = 2.0
CHAT_QUEUE_RESET_POLL_MAX_INTERVAL_SECONDS = 30.0
CHAT_QUEUE_RESET_POLL_BACKOFF_GROW_FACTOR = 1.5
DISCORD_TURN_PROGRESS_MIN_EDIT_INTERVAL_SECONDS = 1.0
DISCORD_TURN_PROGRESS_HEARTBEAT_INTERVAL_SECONDS = 2.0
DISCORD_TURN_PROGRESS_MAX_ACTIONS = 12
DISCORD_TYPING_HEARTBEAT_INTERVAL_SECONDS = 5.0
DISCORD_BACKGROUND_TASK_SHUTDOWN_GRACE_SECONDS = 10.0
DISCORD_INTERACTION_COLD_START_WINDOW_SECONDS = 120.0
DISCORD_HUB_HANDSHAKE_RETRY_WINDOW_SECONDS = 45.0
DISCORD_HUB_HANDSHAKE_RETRY_DELAY_SECONDS = 1.0
DISCORD_HUB_HANDSHAKE_RETRY_MAX_DELAY_SECONDS = 5.0
SHELL_OUTPUT_TRUNCATION_SUFFIX = "\n...[truncated]..."
DISCORD_ATTACHMENT_MAX_BYTES = 100_000_000
THREAD_LIST_MAX_PAGES = 5
THREAD_LIST_PAGE_LIMIT = 100
APP_SERVER_START_BACKOFF_INITIAL_SECONDS = 1.0
APP_SERVER_START_BACKOFF_MAX_SECONDS = 30.0
DISCORD_OPENCODE_PRUNE_FALLBACK_INTERVAL_SECONDS = 300.0
DISCORD_OPENCODE_PRUNE_EMPTY_INTERVAL_SECONDS = 600.0
# Kept for test compatibility; queued notice payloads are shaped in
# service_normalization.py.
DISCORD_QUEUED_PLACEHOLDER_TEXT = "Queued (waiting for available worker...)"
DISCORD_WHISPER_TRANSCRIPT_DISCLAIMER = (
    "Note: transcribed from user voice. If confusing or possibly inaccurate and you "
    "cannot infer the intention please clarify before proceeding."
)
TICKET_PICKER_TOKEN_PREFIX = "ticket@"
TICKETS_BODY_INPUT_ID = "ticket_body"


class AppServerUnavailableError(Exception):
    pass


def _path_within(*, root: Path, target: Path) -> bool:
    try:
        root = canonicalize_path(root)
        target = canonicalize_path(target)
    except (OSError, ValueError, RuntimeError):
        return False
    return is_within(root=root, target=target)


def _opencode_prune_interval(idle_ttl_seconds: Optional[int]) -> Optional[float]:
    if not idle_ttl_seconds or idle_ttl_seconds <= 0:
        return None
    return float(min(600.0, max(60.0, idle_ttl_seconds / 2)))


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
        self._chat_operation_store = SQLiteChatOperationLedger(self._config.root)

        self._outbox = (
            outbox_manager
            if outbox_manager is not None
            else DiscordOutboxManager(
                self._store,
                send_message=self._send_channel_message,
                delete_message=self._delete_channel_message,
                on_delivered=self._handle_discord_outbox_delivery,
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
        self._chat_queue_control_store = ChatQueueControlStore(self._config.root)
        self._dispatcher = dispatcher or ChatDispatcher(
            logger=logger,
            queue_control_store=self._chat_queue_control_store,
            allowlist_predicate=lambda event, context: self._allowlist_predicate(
                event, context
            ),
            bypass_predicate=lambda event, context: self._bypass_predicate(
                event, context
            ),
            busy_predicate=lambda event, context: self._busy_predicate(event, context),
            handler_timeout_seconds=config.dispatch.handler_timeout_seconds,
            handler_stalled_warning_seconds=(
                config.dispatch.handler_stalled_warning_seconds
            ),
        )
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
        self._guild_name_lookups: dict[str, asyncio.Task[Optional[str]]] = {}
        self._channel_name_lookups: dict[str, asyncio.Task[Optional[str]]] = {}
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
        self._hub_client: Optional[HttpHubControlPlaneClient] = None
        self._hub_handshake_compatibility: Optional[HandshakeCompatibility] = None
        try:
            hub_config = load_hub_config(self._config.root)
            base_path = hub_config.server_base_path or ""
            if base_path.endswith("/"):
                base_path = base_path[:-1]
            hub_base_url = (
                f"http://{hub_config.server_host}:{hub_config.server_port}{base_path}"
            )
            self._hub_client = HttpHubControlPlaneClient(base_url=hub_base_url)
        except (ConfigError, OSError, ValueError, ImportError, RuntimeError) as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.hub_control_plane.client_init_failed",
                hub_root=str(self._config.root),
                exc=exc,
            )
        self._pending_model_effort: dict[str, str] = {}
        self._pending_flow_reply_text: dict[str, str] = {}
        self._pending_ticket_context: dict[str, dict[str, str]] = {}
        self._pending_ticket_filters: dict[str, str] = {}
        self._pending_ticket_search_queries: dict[str, str] = {}
        self._responder = DiscordResponder(
            rest=self._rest,
            config=self._config,
            logger=self._logger,
            hydrate_ack_mode=self._load_interaction_ack_mode,
            record_ack=self._record_interaction_ack,
            record_delivery=self._record_interaction_delivery,
            record_delivery_cursor=self._record_interaction_delivery_cursor,
        )
        self._effect_sink = DiscordEffectSink(self)
        self._queued_notice_messages: dict[tuple[str, str], str] = {}
        self._queued_notice_messages_by_source: dict[
            tuple[str, str], tuple[str, str]
        ] = {}
        self._discord_turn_progress_reuse_requests: dict[str, Any] = {}
        self._discord_reusable_progress_messages: dict[str, Any] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._background_shutdown_wait_tasks: set[asyncio.Task[Any]] = set()
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
        self._ingress = InteractionIngress(self, logger=self._logger)
        self._ingress_pre_ack_reservations: set[str] = set()
        self._ingress_pre_ack_reservations_lock = asyncio.Lock()
        self._command_runner = _CommandRunner(
            self,
            config=_RunnerConfig(
                timeout_seconds=config.dispatch.handler_timeout_seconds,
                stalled_warning_seconds=config.dispatch.handler_stalled_warning_seconds,
                max_concurrent_interaction_handlers=(
                    config.dispatch.max_concurrent_interactions
                ),
            ),
            logger=self._logger,
            on_scheduler_conversation_idle=self._wake_dispatcher_conversation,
        )
        self._service_started_at_monotonic: Optional[float] = None
        self._hub_handshake_retry_window_seconds = (
            DISCORD_HUB_HANDSHAKE_RETRY_WINDOW_SECONDS
        )
        self._hub_handshake_retry_delay_seconds = (
            DISCORD_HUB_HANDSHAKE_RETRY_DELAY_SECONDS
        )
        self._hub_handshake_retry_max_delay_seconds = (
            DISCORD_HUB_HANDSHAKE_RETRY_MAX_DELAY_SECONDS
        )

    async def run_forever(self) -> None:
        self._service_started_at_monotonic = time.monotonic()
        handshake_ok = await self._perform_hub_handshake()
        if not handshake_ok:
            raise SystemExit(1)
        self._reap_managed_processes(stage="startup")
        await self._store.initialize()
        await self._reconcile_discord_progress_leases_on_startup()
        await self._resume_interaction_recovery()
        self._validate_command_sync_config()
        self._outbox.start()
        outbox_task = asyncio.create_task(self._outbox.run_loop())
        self._opencode_prune_task = asyncio.create_task(self._run_opencode_prune_loop())
        if self._filebox_housekeeping_enabled():
            self._filebox_prune_task = asyncio.create_task(
                self._run_filebox_prune_loop()
            )
        chat_queue_reset_task = asyncio.create_task(self._run_chat_queue_reset_loop())
        pause_watch_task = asyncio.create_task(self._watch_ticket_flow_pauses())
        terminal_watch_task = asyncio.create_task(self._watch_ticket_flow_terminals())
        dispatcher_loop_task = asyncio.create_task(self._run_dispatcher_loop())
        self._spawn_task(
            self._run_startup_command_sync_background(),
            await_on_shutdown=True,
        )
        try:
            log_event(
                self._logger,
                logging.INFO,
                "discord.bot.starting",
                state_file=str(self._config.state_file),
                command_sync_mode="background",
            )
            try:
                await self._update_status_notifier.maybe_send_notice()
            except Exception as exc:  # intentional: top-level error handler
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.update.notify_failed",
                    exc=exc,
                )
            await self._gateway.run(self._on_dispatch)
        finally:
            await self._command_runner.shutdown()
            with contextlib.suppress(Exception):  # intentional: shutdown cleanup
                await self._dispatcher.wait_idle()
            with contextlib.suppress(Exception):  # intentional: shutdown cleanup
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
            chat_queue_reset_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await chat_queue_reset_task
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

    def _service_uptime_ms(self, *, now: Optional[float] = None) -> Optional[float]:
        started_at_raw = getattr(self, "_service_started_at_monotonic", None)
        started_at = (
            float(started_at_raw) if isinstance(started_at_raw, (int, float)) else None
        )
        if started_at is None:
            return None
        current = time.monotonic() if now is None else now
        return round(max(0.0, (current - started_at) * 1000), 1)

    async def _perform_hub_handshake(self) -> bool:
        expected_schema_generation = ORCHESTRATION_SCHEMA_VERSION
        if self._hub_client is None:
            log_event(
                self._logger,
                logging.ERROR,
                "discord.hub_control_plane.client_not_configured",
                hub_root=str(self._config.root),
                expected_schema_generation=expected_schema_generation,
            )
            return False

        ok, compatibility = await perform_startup_hub_handshake(
            hub_client=self._hub_client,
            log_event_name_prefix="discord",
            handshake_client_name="discord",
            hub_root_str=str(self._config.root),
            startup_monotonic=self._service_started_at_monotonic,
            retry_window_seconds=self._hub_handshake_retry_window_seconds,
            retry_delay_seconds=self._hub_handshake_retry_delay_seconds,
            retry_max_delay_seconds=self._hub_handshake_retry_max_delay_seconds,
            client_api_version=_CONTROL_PLANE_API_VERSION,
            logger=self._logger,
        )
        if compatibility is not None:
            self._hub_handshake_compatibility = compatibility
        return ok

    @property
    def hub_client(self) -> Optional[HttpHubControlPlaneClient]:
        return self._hub_client

    def _is_within_cold_start_window(self, *, now: Optional[float] = None) -> bool:
        started_at_raw = getattr(self, "_service_started_at_monotonic", None)
        started_at = (
            float(started_at_raw) if isinstance(started_at_raw, (int, float)) else None
        )
        if started_at is None:
            return False
        current = time.monotonic() if now is None else now
        return current - started_at <= DISCORD_INTERACTION_COLD_START_WINDOW_SECONDS

    def _interaction_telemetry_fields(
        self,
        ctx: IngressContext,
        *,
        now: Optional[float] = None,
        envelope: Optional[RuntimeInteractionEnvelope] = None,
    ) -> dict[str, Any]:
        current = time.monotonic() if now is None else now
        route_key = self._interaction_route_key(ctx)
        handler_id = self._interaction_handler_id(ctx)
        fields: dict[str, Any] = {
            "interaction_id": ctx.interaction_id,
            "kind": ctx.kind.value,
            "channel_id": ctx.channel_id,
            "guild_id": ctx.guild_id,
            "user_id": ctx.user_id,
            "route_key": route_key,
            "handler_id": handler_id,
            "service_uptime_ms": self._service_uptime_ms(now=current),
            "cold_start_window": self._is_within_cold_start_window(now=current),
        }
        if ctx.command_spec is not None:
            fields["command_path"] = list(ctx.command_spec.path)
            fields["command"] = "/" + " ".join(ctx.command_spec.path)
            fields["ack_policy"] = ctx.command_spec.ack_policy
            fields["ack_timing"] = ctx.command_spec.ack_timing
        if envelope is not None:
            fields["dispatch_ack_policy"] = envelope.dispatch_ack_policy
            fields["queue_wait_ack_policy"] = envelope.queue_wait_ack_policy
            fields["resource_keys"] = list(envelope.resource_keys)
            fields["conversation_id"] = envelope.conversation_id
        if ctx.timing.interaction_created_at is not None:
            fields["gateway_age_ms"] = round(
                max(0.0, (time.time() - ctx.timing.interaction_created_at) * 1000),
                1,
            )
        return fields

    def _initial_ack_budget_seconds(self) -> float:
        dispatch_cfg = getattr(self._config, "dispatch", None)
        budget_ms = getattr(dispatch_cfg, "ack_budget_ms", None)
        if isinstance(budget_ms, int) and budget_ms > 0:
            return float(
                min(
                    float(budget_ms) / 1000.0,
                    DISCORD_INTERACTION_CALLBACK_TIMEOUT_SECONDS,
                )
            )
        for attr_name in ("ack_budget_seconds", "ack_timeout_seconds"):
            budget_seconds = getattr(dispatch_cfg, attr_name, None)
            if isinstance(budget_seconds, (int, float)) and budget_seconds > 0:
                return float(
                    min(
                        float(budget_seconds),
                        DISCORD_INTERACTION_CALLBACK_TIMEOUT_SECONDS,
                    )
                )
        return float(DISCORD_INTERACTION_CALLBACK_TIMEOUT_SECONDS)

    async def _run_dispatcher_loop(self) -> None:
        while True:
            events = await self._chat_adapter.poll_events(timeout_seconds=30.0)
            for event in events:
                await self._dispatch_chat_event(event)

    async def _dispatch_chat_event(self, event: ChatEvent) -> None:
        if isinstance(event, ChatInteractionEvent):
            log_event(
                self._logger,
                logging.WARNING,
                "discord.interaction.dispatcher_path_ignored",
                update_id=event.update_id,
                interaction_id=event.interaction.interaction_id,
            )
            return

        async def _handle_dispatched_event(
            queued_event: ChatEvent, context: DispatchContext
        ) -> None:
            try:
                await self._handle_chat_event(queued_event, context)
            finally:
                if isinstance(queued_event, ChatMessageEvent):
                    await self._clear_queued_notice(
                        conversation_id=context.conversation_id,
                        source_message_id=queued_event.message.message_id,
                        channel_id=context.chat_id,
                    )

        dispatch_result = await self._dispatcher.dispatch(
            event, _handle_dispatched_event
        )
        await self._maybe_send_queued_notice(event, dispatch_result)

    async def dispatch_chat_event(self, event: ChatEvent) -> None:
        await self._dispatch_chat_event(event)

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

    def _is_message_turn_candidate_shape(self, event: ChatMessageEvent) -> bool:
        text = (event.text or "").strip()
        has_attachments = bool(event.attachments)
        has_forwarded_content = event.forwarded_from is not None
        if not text and not has_attachments and not has_forwarded_content:
            return False
        if text.startswith("/"):
            return False
        if text.startswith("!"):
            return False
        return True

    def _busy_predicate(self, event: ChatEvent, context: DispatchContext) -> bool:
        if not isinstance(event, ChatMessageEvent):
            return False
        if not self._is_message_turn_candidate_shape(event):
            return False
        return self._command_runner.is_busy(context.conversation_id)

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
        if not self._is_message_turn_candidate_shape(event):
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

    def _interaction_conversation_scheduler_key(
        self,
        *,
        channel_id: str,
        guild_id: Optional[str],
    ) -> tuple[str, str]:
        conversation_id = self._dispatcher_conversation_id(
            channel_id=channel_id,
            guild_id=guild_id,
        )
        return conversation_id, f"conversation:{conversation_id}"

    @staticmethod
    def _workspace_scheduler_key(workspace_path: str) -> str:
        return f"workspace:{canonicalize_path(Path(workspace_path))}"

    async def _scheduler_bound_workspace_root(
        self, *, channel_id: str
    ) -> Optional[Path]:
        _binding, workspace_root = await resolve_bound_workspace_root(
            self,
            channel_id=channel_id,
        )
        return workspace_root

    async def _scheduler_bind_target_workspace_root(self, token: Any) -> Optional[Path]:
        if not isinstance(token, str) or not token.strip():
            return None
        normalized = token.strip()
        candidate = Path(normalized)
        # Keep bind-target resolution pre-ack strictly local and cheap. The full
        # bind handler can do slower candidate enumeration after Discord has been
        # acknowledged, but scheduler lock resolution must not spend the ack budget
        # on live hub calls.
        if candidate.is_absolute() or "/" in normalized or "\\" in normalized:
            if not candidate.is_absolute():
                candidate = self._config.root / candidate
            workspace_root = canonicalize_path(candidate)
            return (
                workspace_root
                if workspace_root.exists() and workspace_root.is_dir()
                else None
            )

        cheap_candidates: list[tuple[Optional[str], Optional[str], str]] = [
            ("repo", repo_id, str(canonicalize_path(Path(path))))
            for repo_id, path in self._list_manifest_repos()
        ]
        cheap_candidates.extend(
            (
                "agent_workspace",
                workspace_id,
                workspace_path,
            )
            for workspace_id, workspace_path, _display_name in self._list_agent_workspaces_from_cache()
        )
        resolved = self._resolve_workspace_from_token(normalized, cheap_candidates)
        if resolved is not None:
            resolved_root = canonicalize_path(Path(resolved[2]))
            return (
                resolved_root
                if resolved_root.exists() and resolved_root.is_dir()
                else None
            )

        workspace_root = canonicalize_path(self._config.root / candidate)
        return (
            workspace_root
            if workspace_root.exists() and workspace_root.is_dir()
            else None
        )

    def _interaction_requires_workspace_lock(self, ctx: IngressContext) -> bool:
        if ctx.kind == InteractionKind.MODAL_SUBMIT:
            custom_id = str(ctx.custom_id or "").strip()
            return modal_workspace_lock_policy(custom_id) != "none"
        if ctx.kind == InteractionKind.COMPONENT:
            custom_id = str(ctx.custom_id or "").strip()
            return component_workspace_lock_policy(custom_id) != "none"
        if ctx.kind != InteractionKind.SLASH_COMMAND or ctx.command_spec is None:
            return False
        command_path = self._normalize_discord_command_path(ctx.command_spec.path)
        return slash_command_workspace_lock_policy(command_path) != "none"

    async def _interaction_workspace_scheduler_key(
        self, ctx: IngressContext
    ) -> Optional[str]:
        if not self._interaction_requires_workspace_lock(ctx):
            return None
        workspace_root: Optional[Path] = None
        if ctx.kind == InteractionKind.SLASH_COMMAND and ctx.command_spec is not None:
            command_path = self._normalize_discord_command_path(ctx.command_spec.path)
            if (
                slash_command_workspace_lock_policy(command_path)
                == "bind_target_workspace"
            ):
                workspace_root = await self._scheduler_bind_target_workspace_root(
                    ctx.command_spec.options.get("workspace")
                )
            else:
                workspace_root = await self._scheduler_bound_workspace_root(
                    channel_id=ctx.channel_id
                )
        elif (
            ctx.kind == InteractionKind.COMPONENT
            and component_workspace_lock_policy(str(ctx.custom_id or "").strip())
            == "bind_target_workspace"
        ):
            selected_value = ctx.values[0] if ctx.values else None
            workspace_root = await self._scheduler_bind_target_workspace_root(
                selected_value
            )
        else:
            workspace_root = await self._scheduler_bound_workspace_root(
                channel_id=ctx.channel_id
            )
        if workspace_root is None:
            return None
        return self._workspace_scheduler_key(str(workspace_root))

    async def _acknowledge_runtime_envelope(
        self,
        envelope: RuntimeInteractionEnvelope,
        *,
        stage: str,
    ) -> bool:
        ctx = envelope.context
        ack_policy = (
            envelope.dispatch_ack_policy
            if stage == "dispatch"
            else envelope.queue_wait_ack_policy
        )
        if ack_policy in (None, "immediate"):
            return True
        ack_started_at = time.monotonic()
        session = self._ensure_interaction_session(
            ctx.interaction_id,
            ctx.interaction_token,
            kind=ctx.kind,
        )
        budget_seconds = self._initial_ack_budget_seconds()
        durable_ack_mode = await self._load_interaction_ack_mode(ctx.interaction_id)
        if isinstance(durable_ack_mode, str) and durable_ack_mode.strip():
            session.restore_initial_response(durable_ack_mode)
        if session.has_initial_response():
            ctx.deferred = session.is_deferred()
            finished_at = time.monotonic()
            ctx.timing = replace(ctx.timing, ack_finished_at=finished_at)
            log_event(
                self._logger,
                logging.INFO,
                "discord.interaction.ack.reused",
                stage=stage,
                runtime_ack_policy=ack_policy,
                durable_ack_mode=durable_ack_mode,
                ack_latency_ms=round((finished_at - ack_started_at) * 1000, 1),
                ack_budget_seconds=budget_seconds,
                **self._interaction_telemetry_fields(
                    ctx,
                    now=finished_at,
                    envelope=envelope,
                ),
            )
            return True
        ingress_started_at = ctx.timing.ingress_started_at or ack_started_at
        ack_deadline_at = ingress_started_at + budget_seconds
        current_at = time.monotonic()
        if current_at >= ack_deadline_at and ack_policy not in (None, "immediate"):
            self._mark_interaction_session_expired(
                session,
                error="expired_before_ack",
            )
            ctx.timing = replace(ctx.timing, ack_finished_at=current_at)
            log_event(
                self._logger,
                logging.WARNING,
                "discord.interaction.ack.expired_before_ack",
                stage=stage,
                runtime_ack_policy=ack_policy,
                ack_budget_seconds=budget_seconds,
                budget_overrun_ms=round((current_at - ack_deadline_at) * 1000, 1),
                cause="deadline_exceeded_before_attempt",
                **self._interaction_telemetry_fields(
                    ctx,
                    now=current_at,
                    envelope=envelope,
                ),
            )
            return False
        log_event(
            self._logger,
            logging.INFO,
            "discord.interaction.ack.start",
            stage=stage,
            runtime_ack_policy=ack_policy,
            **self._interaction_telemetry_fields(
                ctx,
                now=ack_started_at,
                envelope=envelope,
            ),
        )
        remaining_seconds = max(0.0, ack_deadline_at - time.monotonic())
        try:
            if ack_policy == "defer_public":
                acknowledged = await asyncio.wait_for(
                    self._defer_public(
                        interaction_id=ctx.interaction_id,
                        interaction_token=ctx.interaction_token,
                    ),
                    timeout=remaining_seconds,
                )
            elif ack_policy == "defer_component_update":
                acknowledged = await asyncio.wait_for(
                    self._defer_component_update(
                        interaction_id=ctx.interaction_id,
                        interaction_token=ctx.interaction_token,
                    ),
                    timeout=remaining_seconds,
                )
            else:
                acknowledged = await asyncio.wait_for(
                    self._defer_ephemeral(
                        interaction_id=ctx.interaction_id,
                        interaction_token=ctx.interaction_token,
                    ),
                    timeout=remaining_seconds,
                )
        except asyncio.TimeoutError:
            acknowledged = False
            current_at = time.monotonic()
            self._mark_interaction_session_expired(
                session,
                error="expired_before_ack",
            )
            ctx.timing = replace(ctx.timing, ack_finished_at=current_at)
            log_event(
                self._logger,
                logging.WARNING,
                "discord.interaction.ack.failed",
                stage=stage,
                runtime_ack_policy=ack_policy,
                ack_latency_ms=round((current_at - ack_started_at) * 1000, 1),
                ack_budget_seconds=budget_seconds,
                budget_overrun_ms=round(
                    max(0.0, current_at - ack_deadline_at) * 1000, 1
                ),
                expired_before_ack=True,
                cause="deadline_exceeded_during_ack",
                delivery_status=session.last_delivery_status,
                delivery_error=session.last_delivery_error,
                **self._interaction_telemetry_fields(
                    ctx,
                    now=current_at,
                    envelope=envelope,
                ),
            )
            return False
        except Exception as exc:
            acknowledged = False
            current_at = time.monotonic()
            if current_at >= ack_deadline_at or is_unknown_interaction_error(exc):
                self._mark_interaction_session_expired(
                    session,
                    error=str(exc) or "expired_before_ack",
                )
            ctx.timing = replace(ctx.timing, ack_finished_at=current_at)
            log_event(
                self._logger,
                logging.WARNING,
                "discord.interaction.ack.failed",
                stage=stage,
                runtime_ack_policy=ack_policy,
                ack_latency_ms=round((current_at - ack_started_at) * 1000, 1),
                ack_budget_seconds=budget_seconds,
                budget_overrun_ms=round(
                    max(0.0, current_at - ack_deadline_at) * 1000, 1
                ),
                expired_before_ack=True,
                cause="ack_error",
                exc=exc,
                delivery_status=session.last_delivery_status,
                delivery_error=session.last_delivery_error,
                **self._interaction_telemetry_fields(
                    ctx,
                    now=current_at,
                    envelope=envelope,
                ),
            )
            return False
        if acknowledged:
            finished_at = time.monotonic()
            ctx.deferred = True
            ctx.timing = replace(
                ctx.timing,
                ack_finished_at=finished_at,
            )
            log_event(
                self._logger,
                logging.INFO,
                "discord.interaction.ack.succeeded",
                stage=stage,
                runtime_ack_policy=ack_policy,
                ack_latency_ms=round((finished_at - ack_started_at) * 1000, 1),
                ack_budget_seconds=budget_seconds,
                delivery_status=session.last_delivery_status,
                **self._interaction_telemetry_fields(
                    ctx,
                    now=finished_at,
                    envelope=envelope,
                ),
            )
        else:
            finished_at = time.monotonic()
            if is_unknown_interaction_error(session.last_delivery_error):
                self._mark_interaction_session_expired(
                    session,
                    error=session.last_delivery_error or "expired_before_ack",
                )
            ctx.timing = replace(ctx.timing, ack_finished_at=finished_at)
            log_event(
                self._logger,
                logging.WARNING,
                "discord.interaction.ack.failed",
                stage=stage,
                runtime_ack_policy=ack_policy,
                ack_latency_ms=round((finished_at - ack_started_at) * 1000, 1),
                ack_budget_seconds=budget_seconds,
                expired_before_ack=True,
                delivery_status=session.last_delivery_status,
                delivery_error=session.last_delivery_error,
                **self._interaction_telemetry_fields(
                    ctx,
                    now=finished_at,
                    envelope=envelope,
                ),
            )
        return acknowledged

    def _dispatch_ack_failure_confirms_expiry(
        self,
        ctx: IngressContext,
        envelope: RuntimeInteractionEnvelope,
    ) -> bool:
        ack_policy = envelope.dispatch_ack_policy
        if ack_policy in (None, "immediate"):
            return False

        ack_finished_at = ctx.timing.ack_finished_at
        ingress_started_at = ctx.timing.ingress_started_at
        if ack_finished_at is not None and ingress_started_at is not None:
            ack_deadline_at = ingress_started_at + self._initial_ack_budget_seconds()
            if ack_finished_at >= ack_deadline_at:
                return True

        session = self._get_interaction_session(ctx.interaction_token)
        if session is None:
            return False

        delivery_status = (session.last_delivery_status or "").strip()
        if delivery_status != "ack_failed":
            return False
        return is_unknown_interaction_error(session.last_delivery_error)

    @staticmethod
    def _mark_interaction_session_expired(session: Any, *, error: str) -> None:
        mark_expired = getattr(session, "mark_expired", None)
        if callable(mark_expired):
            mark_expired(error=error)

    async def acknowledge_runtime_envelope(
        self,
        envelope: RuntimeInteractionEnvelope,
        *,
        stage: str,
    ) -> bool:
        return await self._acknowledge_runtime_envelope(envelope, stage=stage)

    async def _build_runtime_interaction_envelope(
        self,
        ctx: IngressContext,
    ) -> RuntimeInteractionEnvelope:
        conversation_id: Optional[str] = None
        resource_keys: list[str] = []
        dispatch_ack_policy = None
        queue_wait_ack_policy = None

        if ctx.kind != InteractionKind.AUTOCOMPLETE:
            conversation_id, conversation_key = (
                self._interaction_conversation_scheduler_key(
                    channel_id=ctx.channel_id,
                    guild_id=ctx.guild_id,
                )
            )
            resource_keys.append(conversation_key)

        if ctx.kind == InteractionKind.SLASH_COMMAND and ctx.command_spec is not None:
            if ctx.command_spec.ack_timing == "dispatch":
                dispatch_ack_policy = ctx.command_spec.ack_policy
        elif ctx.kind == InteractionKind.COMPONENT:
            dispatch_ack_policy = component_dispatch_ack_policy(
                str(ctx.custom_id or "").strip()
            )
            queue_wait_ack_policy = component_admission_ack_policy(
                str(ctx.custom_id or "").strip()
            )
        elif ctx.kind == InteractionKind.MODAL_SUBMIT:
            queue_wait_ack_policy = modal_admission_ack_policy(
                str(ctx.custom_id or "").strip()
            )

        workspace_key = await self._interaction_workspace_scheduler_key(ctx)
        if workspace_key is not None:
            resource_keys.append(workspace_key)

        return RuntimeInteractionEnvelope(
            context=ctx,
            conversation_id=conversation_id,
            resource_keys=tuple(resource_keys),
            dispatch_ack_policy=dispatch_ack_policy,
            queue_wait_ack_policy=queue_wait_ack_policy,
        )

    def _interaction_route_key(self, ctx: IngressContext) -> Optional[str]:
        if ctx.kind == InteractionKind.SLASH_COMMAND and ctx.command_spec is not None:
            return "/".join(ctx.command_spec.path)
        if ctx.kind == InteractionKind.COMPONENT and ctx.custom_id:
            return str(ctx.custom_id)
        if ctx.kind == InteractionKind.MODAL_SUBMIT and ctx.custom_id:
            return str(ctx.custom_id)
        if ctx.kind == InteractionKind.AUTOCOMPLETE and ctx.command_spec is not None:
            return "/".join(ctx.command_spec.path)
        return None

    def _interaction_handler_id(self, ctx: IngressContext) -> Optional[str]:
        if ctx.kind == InteractionKind.SLASH_COMMAND and ctx.command_spec is not None:
            slash_route = slash_command_route_for_path(ctx.command_spec.path)
            return (
                slash_route.id
                if slash_route is not None
                else self._interaction_route_key(ctx)
            )
        if ctx.kind == InteractionKind.COMPONENT and ctx.custom_id:
            component_route = component_route_for_custom_id(str(ctx.custom_id))
            return (
                component_route.id
                if component_route is not None
                else str(ctx.custom_id)
            )
        if ctx.kind == InteractionKind.MODAL_SUBMIT and ctx.custom_id:
            modal_route = modal_route_for_custom_id(str(ctx.custom_id))
            return modal_route.id if modal_route is not None else str(ctx.custom_id)
        return self._interaction_route_key(ctx)

    def _serialize_runtime_envelope(
        self,
        envelope: RuntimeInteractionEnvelope,
    ) -> dict[str, Any]:
        ctx = envelope.context
        return {
            "interaction_id": ctx.interaction_id,
            "interaction_token": ctx.interaction_token,
            "channel_id": ctx.channel_id,
            "guild_id": ctx.guild_id,
            "user_id": ctx.user_id,
            "kind": ctx.kind.value,
            "deferred": ctx.deferred,
            "command_spec": (
                {
                    "path": list(ctx.command_spec.path),
                    "options": ctx.command_spec.options,
                    "ack_policy": ctx.command_spec.ack_policy,
                    "ack_timing": ctx.command_spec.ack_timing,
                    "requires_workspace": ctx.command_spec.requires_workspace,
                }
                if ctx.command_spec is not None
                else None
            ),
            "custom_id": ctx.custom_id,
            "values": ctx.values,
            "modal_values": ctx.modal_values,
            "focused_name": ctx.focused_name,
            "focused_value": ctx.focused_value,
            "message_id": ctx.message_id,
            "conversation_id": envelope.conversation_id,
            "resource_keys": list(envelope.resource_keys),
            "dispatch_ack_policy": envelope.dispatch_ack_policy,
            "queue_wait_ack_policy": envelope.queue_wait_ack_policy,
        }

    async def _persist_runtime_interaction(
        self,
        envelope: RuntimeInteractionEnvelope,
        payload: dict[str, Any],
        *,
        scheduler_state: str,
    ) -> None:
        await self._register_chat_operation_received(
            envelope.context,
            conversation_id=envelope.conversation_id,
        )
        await self._store.persist_interaction_runtime(
            envelope.context.interaction_id,
            route_key=self._interaction_route_key(envelope.context),
            handler_id=self._interaction_handler_id(envelope.context),
            conversation_id=envelope.conversation_id,
            scheduler_state=scheduler_state,
            resource_keys=envelope.resource_keys,
            payload_json=payload,
            envelope_json=self._serialize_runtime_envelope(envelope),
        )
        await self._patch_chat_operation(
            envelope.context.interaction_id,
            state=self._discord_chat_operation_state_for_scheduler(scheduler_state),
            conversation_id=envelope.conversation_id,
            validate_transition=False,
            metadata_updates={
                "route_key": self._interaction_route_key(envelope.context),
                "handler_id": self._interaction_handler_id(envelope.context),
                "resource_keys": list(envelope.resource_keys),
                "dispatch_ack_policy": envelope.dispatch_ack_policy,
                "queue_wait_ack_policy": envelope.queue_wait_ack_policy,
            },
        )

    async def _wake_dispatcher_conversation(self, conversation_id: str) -> None:
        wake = getattr(self._dispatcher, "wake_conversation", None)
        if callable(wake):
            await wake(conversation_id)

    async def _clear_queued_notice(
        self,
        *,
        conversation_id: str,
        source_message_id: str,
        channel_id: str,
    ) -> None:
        key = (conversation_id, source_message_id)
        notice_message_id = self._queued_notice_messages.pop(key, None)
        source_key = (channel_id, source_message_id)
        source_entry = self._queued_notice_messages_by_source.pop(source_key, None)
        if not notice_message_id and isinstance(source_entry, tuple):
            _, source_notice_message_id = source_entry
            notice_message_id = source_notice_message_id
        if not notice_message_id:
            return
        await self._delete_channel_message_safe(
            channel_id,
            notice_message_id,
            record_id=f"queue-notice-delete:{channel_id}:{source_message_id}",
        )

    def _claim_queued_notice_progress_message(
        self,
        *,
        channel_id: str,
        source_message_id: str,
    ) -> Optional[str]:
        source_key = (channel_id, source_message_id)
        source_entry = self._queued_notice_messages_by_source.pop(source_key, None)
        if not isinstance(source_entry, tuple):
            return None
        conversation_id, notice_message_id = source_entry
        if conversation_id:
            self._queued_notice_messages.pop((conversation_id, source_message_id), None)
        normalized_notice_message_id = str(notice_message_id or "").strip()
        return normalized_notice_message_id or None

    async def _queued_notice_config_for_conversation(
        self, conversation_id: str
    ) -> tuple[Optional[str], bool]:
        describe_busy = getattr(self._command_runner, "describe_busy", None)
        if not callable(describe_busy):
            return None, True
        command_label = describe_busy(conversation_id)
        if not isinstance(command_label, str) or not command_label.strip():
            return None, True
        queue_status = await self._dispatcher.queue_status(conversation_id)
        has_active_message_turn = bool(
            queue_status.get("active") if isinstance(queue_status, dict) else False
        )
        return (
            f"Queued behind {command_label}; will run when it finishes.",
            has_active_message_turn,
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
        (
            notice_content,
            allow_interrupt,
        ) = await self._queued_notice_config_for_conversation(
            dispatch_result.context.conversation_id
        )
        source_message_id = event.message.message_id
        queued_notice_payload = build_discord_queue_notice_message(
            source_message_id=source_message_id,
            content=notice_content,
            allow_interrupt=allow_interrupt,
        )
        try:
            response = await self._send_channel_message(
                channel_id,
                queued_notice_payload.to_payload(),
            )
            notice_message_id = response.get("id")
            if isinstance(notice_message_id, str) and notice_message_id:
                conversation_id = dispatch_result.context.conversation_id
                self._queued_notice_messages[(conversation_id, source_message_id)] = (
                    notice_message_id
                )
                self._queued_notice_messages_by_source[
                    (channel_id, source_message_id)
                ] = (conversation_id, notice_message_id)
        except (DiscordAPIError, OSError, TypeError, ValueError):
            await self._send_channel_message_safe(
                channel_id,
                build_discord_queue_notice_message(
                    source_message_id=None,
                    content=notice_content,
                    allow_interrupt=allow_interrupt,
                ).to_payload(),
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
                except (DiscordAPIError, OSError) as exc:
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
            except (RuntimeError, TypeError):
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
        except (RuntimeError, TypeError) as exc:
            log_event(
                self._logger,
                logging.DEBUG,
                "discord.typing.begin.failed",
                channel_id=channel_id,
                exc=exc,
            )
        except BaseException:
            with contextlib.suppress(
                RuntimeError,
                TypeError,
                asyncio.CancelledError,
            ):
                await asyncio.shield(self._end_typing_indicator(channel_id))
            raise
        else:
            began = True
        try:
            await work()
        finally:
            if began:
                try:
                    await self._end_typing_indicator(channel_id)
                except (RuntimeError, TypeError) as exc:
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

    def _queues_command_after_dispatch_ack(self, command_path: tuple[str, ...]) -> bool:
        normalized_path = self._normalize_discord_command_path(command_path)
        return normalized_path in {
            ("car", "new"),
            ("car", "newt"),
            ("car", "session", "compact"),
        }

    def _bypass_predicate(self, event: ChatEvent, context: DispatchContext) -> bool:
        if isinstance(event, ChatInteractionEvent):
            import json

            payload_str = event.payload or "{}"
            try:
                payload_data = json.loads(payload_str)
            except json.JSONDecodeError:
                return True
            payload_type = payload_data.get("type")
            if not isinstance(payload_type, str) or payload_type != "command":
                return True
            command_raw = payload_data.get("command")
            command_path = (
                tuple(part for part in str(command_raw).split(":") if part)
                if isinstance(command_raw, str)
                else ()
            )
            return not self._queues_command_after_dispatch_ack(command_path)
        return False

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
        except (DiscordAPIError, OSError) as exc:
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
        except (ConfigError, OSError, ValueError, TypeError) as exc:
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
        except (OSError, ValueError, RuntimeError) as exc:
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
        normalized = DiscordAttachmentAdapter.from_raw(attachment)
        return normalized.is_audio(mime_type=mime_type)

    def _transcription_filename_for_attachment(
        self,
        attachment: Any,
        *,
        saved_name: str,
        mime_type: Optional[str],
    ) -> str:
        normalized = DiscordAttachmentAdapter.from_raw(attachment)
        return normalized.transcription_filename(
            saved_name=saved_name,
            mime_type=mime_type,
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
            if isinstance(exc, VoiceTransientError):
                detail = getattr(exc, "detail", None)
                if isinstance(detail, str) and detail.strip():
                    return None, detail.strip()
            user_message = getattr(exc, "user_message", None)
            if isinstance(user_message, str) and user_message.strip():
                return None, user_message.strip()
            detail = getattr(exc, "detail", None)
            if isinstance(detail, str) and detail.strip():
                return None, detail.strip()
            return None, f"Voice transcript unavailable ({exc.reason})."
        except (
            Exception
        ) as exc:  # intentional: transcribe can fail in unpredictable ways
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
        saved: list[SavedDiscordAttachment] = []
        failed = 0
        for index, attachment in enumerate(attachments, start=1):
            normalized = DiscordAttachmentAdapter.from_raw(attachment)
            if normalized.source_url is None:
                failed += 1
                continue
            try:
                if (
                    normalized.size_bytes is not None
                    and normalized.size_bytes > DISCORD_ATTACHMENT_MAX_BYTES
                ):
                    raise RuntimeError(
                        "attachment exceeds max size "
                        f"({normalized.size_bytes} > {DISCORD_ATTACHMENT_MAX_BYTES})"
                    )
                data = await self._rest.download_attachment(
                    url=normalized.source_url,
                    max_size_bytes=DISCORD_ATTACHMENT_MAX_BYTES,
                )
                file_name = self._build_attachment_filename(attachment, index=index)
                path = inbox / file_name
                path.write_bytes(data)
                original_name = normalized.file_name or path.name
                mime_type = normalized.mime_type
                is_audio = normalized.is_audio(mime_type=mime_type)
                transcription_name = self._transcription_filename_for_attachment(
                    attachment,
                    saved_name=path.name,
                    mime_type=mime_type,
                )
                is_image = normalized.is_image(original_name=str(original_name))
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
                    SavedDiscordAttachment(
                        original_name=str(original_name),
                        path=path,
                        mime_type=mime_type,
                        size_bytes=len(data),
                        is_audio=is_audio,
                        is_image=is_image,
                        transcript_text=transcript_text,
                        transcript_warning=transcript_warning,
                    )
                )
            except (DiscordAPIError, OSError, RuntimeError, ValueError) as exc:
                failed += 1
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.turn.attachment.download_failed",
                    channel_id=channel_id,
                    file_id=normalized.file_id,
                    exc=exc,
                )

        if not saved:
            return prompt_text, 0, failed, None, None

        provider_name: Optional[str] = None
        if any(item.transcript_text for item in saved):
            voice_service, voice_config = self._voice_service_for_workspace(
                workspace_root
            )
            if voice_service is not None:
                with contextlib.suppress(RuntimeError, AttributeError, ValueError):
                    provider_name = voice_service.effective_provider_name()
            if (
                not provider_name
                and voice_config
                and isinstance(voice_config.provider, str)
            ):
                provider_name = normalize_voice_provider(voice_config.provider)

        payload = build_attachment_context_payload(
            prompt_text=prompt_text,
            saved=saved,
            failed=failed,
            inbox_path=inbox,
            outbox_path=outbox_dir(workspace_root),
            outbox_pending_path=outbox_pending_dir(workspace_root),
            max_message_length=int(self._config.max_message_length),
            voice_provider_name=provider_name,
            whisper_transcript_disclaimer=DISCORD_WHISPER_TRANSCRIPT_DISCLAIMER,
        )
        return (
            payload.prompt_text,
            payload.saved_count,
            payload.failed_count,
            payload.user_visible_transcript,
            cast(Optional[list[dict[str, Any]]], payload.native_input_items_payload),
        )

    def _build_attachment_filename(self, attachment: Any, *, index: int) -> str:
        normalized = DiscordAttachmentAdapter.from_raw(attachment)
        return normalized.build_saved_name(index=index, token=uuid.uuid4().hex[:8])

    async def _find_paused_flow_run(
        self, workspace_root: Path
    ) -> Optional[FlowRunRecord]:
        try:
            store = self._open_flow_store(workspace_root)
        except (OSError, ValueError):
            return None
        try:
            runs = store.list_flow_runs(flow_type="ticket_flow")
            return select_ticket_flow_run_record(runs, selection="paused")
        except (OSError, ValueError, KeyError):
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
        agent_profile: Optional[str] = None,
    ) -> str:
        resolved_agent, resolved_profile = resolve_chat_agent_and_profile(
            agent,
            agent_profile,
            default=self.DEFAULT_AGENT,
            context=self,
        )
        if pma_enabled:
            return pma_base_key(resolved_agent, resolved_profile)
        session_agent = resolve_chat_runtime_agent(
            agent,
            agent_profile,
            default=self.DEFAULT_AGENT,
            context=self,
        )
        return file_chat_discord_key(session_agent, channel_id, str(workspace_root))

    def _discord_thread_service(self) -> Any:
        return build_discord_thread_orchestration_service(self)

    def _get_discord_thread_binding(
        self,
        *,
        channel_id: str,
        mode: Optional[str] = None,
    ) -> tuple[Any, Any, Any]:
        orchestration_service = self._discord_thread_service()
        if orchestration_service is None:
            raise RuntimeError(
                "Discord orchestration service unavailable: hub control-plane client not connected"
            )
        resolved = resolve_surface_thread_binding(
            orchestration_service,
            surface_kind="discord",
            surface_key=channel_id,
            mode=mode,
        )
        return orchestration_service, resolved.binding, resolved.thread

    def _format_discord_thread_picker_label(
        self,
        thread: Any,
        *,
        is_current: bool,
    ) -> str:
        return _format_discord_thread_picker_label_impl(
            self, thread, is_current=is_current
        )

    async def _reset_discord_thread_binding(
        self,
        *,
        channel_id: str,
        workspace_root: Path,
        agent: str,
        agent_profile: Optional[str] = None,
        repo_id: Optional[str],
        resource_kind: Optional[str],
        resource_id: Optional[str],
        pma_enabled: bool,
    ) -> tuple[bool, str]:
        mode = "pma" if pma_enabled else "repo"
        orchestration_service, binding, current_thread = (
            self._get_discord_thread_binding(
                channel_id=channel_id,
                mode=mode,
            )
        )
        previous_thread_id = (
            str(getattr(current_thread, "thread_target_id", "") or "").strip() or None
        )
        if current_thread is not None:
            from .message_turns import clear_discord_turn_progress_reuse

            log_event(
                self._logger,
                logging.INFO,
                "discord.thread.reset.stop_requested",
                channel_id=channel_id,
                mode=mode,
                thread_target_id=current_thread.thread_target_id,
            )
            clear_discord_turn_progress_reuse(
                self,
                thread_target_id=current_thread.thread_target_id,
            )
        owner_kind, owner_id, normalized_repo_id = self._resource_owner_for_workspace(
            workspace_root,
            repo_id=repo_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
        )
        thread_metadata: Optional[dict[str, Any]] = (
            {"agent_profile": agent_profile} if agent_profile else None
        )
        replacement = await replace_surface_thread(
            orchestration_service,
            surface_kind="discord",
            surface_key=channel_id,
            workspace_root=workspace_root,
            agent_id=agent,
            repo_id=normalized_repo_id,
            resource_kind=owner_kind,
            resource_id=owner_id,
            mode=mode,
            display_name=f"discord:{channel_id}",
            binding_metadata={"channel_id": channel_id, "pma_enabled": pma_enabled},
            thread_metadata=thread_metadata,
            binding=binding,
            thread=current_thread,
        )
        stop_outcome = replacement.stop_outcome
        if previous_thread_id and stop_outcome is not None:
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
                thread_target_id=previous_thread_id,
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
                    thread_target_id=previous_thread_id,
                )
        return (
            replacement.had_previous,
            replacement.replacement_thread.thread_target_id,
        )

    def _attach_discord_thread_binding(
        self,
        *,
        channel_id: str,
        thread_target_id: str,
        agent: str,
        agent_profile: Optional[str] = None,
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
        return bind_surface_thread(
            orchestration_service,
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
        agent_profile: Optional[str] = None,
        current_thread_id: Optional[str],
        mode: str,
        repo_id: Optional[str] = None,
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
        limit: int = DISCORD_SELECT_OPTION_MAX_OPTIONS,
    ) -> list[tuple[str, str]]:
        agent_ids = self._discord_thread_agent_ids(
            agent=agent,
            agent_profile=agent_profile,
        )
        orchestration_service = self._discord_thread_service()
        owner_kind, owner_id, normalized_repo_id = self._resource_owner_for_workspace(
            workspace_root,
            repo_id=repo_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
        )
        threads: list[Any] = []
        seen_thread_ids: set[str] = set()
        query_limit = max(limit * 4, limit)
        for agent_id in agent_ids:
            for thread in orchestration_service.list_thread_targets(
                agent_id=agent_id,
                repo_id=normalized_repo_id,
                resource_kind=owner_kind,
                resource_id=owner_id,
                limit=query_limit,
            ):
                thread_id = str(getattr(thread, "thread_target_id", "") or "").strip()
                if not thread_id or thread_id in seen_thread_ids:
                    continue
                seen_thread_ids.add(thread_id)
                threads.append(thread)
        canonical_workspace = str(workspace_root.resolve())
        filtered: list[Any] = []
        bound_modes_by_thread_id: dict[str, set[str]] = {}
        binding_limit = max(limit * 8, limit)
        for agent_id in agent_ids:
            for binding in orchestration_service.list_bindings(
                agent_id=agent_id,
                repo_id=normalized_repo_id,
                resource_kind=owner_kind,
                resource_id=owner_id,
                surface_kind="discord",
                limit=binding_limit,
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
            if not self._discord_thread_matches_agent(
                thread,
                agent=agent,
                agent_profile=agent_profile,
            ):
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
        approval_message = build_discord_approval_message(request, token=token)
        pending = _DiscordPendingApproval(
            token=token,
            request_id=request_id,
            turn_id=turn_id,
            channel_id=context.channel_id,
            message_id=None,
            prompt=approval_message.content,
            future=future,
        )
        try:
            response = await self._send_channel_message(
                context.channel_id,
                approval_message.to_payload(),
            )
        except (DiscordAPIError, OSError) as exc:
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
        except (DiscordAPIError, OSError, RuntimeError):
            with contextlib.suppress(DiscordAPIError, OSError, RuntimeError):
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
            log_event(
                self._logger,
                logging.INFO,
                "discord.app_server.supervisor.created",
                workspace_path=str(workspace_root),
                command=command,
                service_uptime_ms=self._service_uptime_ms(),
                cold_start_window=self._is_within_cold_start_window(),
            )
            return supervisor

    async def _client_for_workspace(
        self, workspace_path: Optional[str]
    ) -> Optional[CodexAppServerClient]:
        if not isinstance(workspace_path, str) or not workspace_path.strip():
            return None
        try:
            workspace_root = canonicalize_path(Path(workspace_path))
        except (OSError, ValueError):
            return None
        if not workspace_root.exists() or not workspace_root.is_dir():
            return None
        delay = APP_SERVER_START_BACKOFF_INITIAL_SECONDS
        timeout = 30.0
        started_at = time.monotonic()
        attempts = 0
        while True:
            try:
                attempts += 1
                supervisor = await self._app_server_supervisor_for_workspace(
                    workspace_root
                )
                client = await supervisor.get_client(workspace_root)
                log_event(
                    self._logger,
                    logging.INFO,
                    "discord.app_server.client.ready",
                    workspace_path=str(workspace_root),
                    attempts=attempts,
                    elapsed_ms=round((time.monotonic() - started_at) * 1000, 1),
                    service_uptime_ms=self._service_uptime_ms(),
                    cold_start_window=self._is_within_cold_start_window(),
                )
                return client
            except ConfigError:
                return None
            except (RuntimeError, ConnectionError, OSError) as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.app_server.start_failed",
                    workspace_path=str(workspace_root),
                    attempts=attempts,
                    elapsed_ms=round((time.monotonic() - started_at) * 1000, 1),
                    service_uptime_ms=self._service_uptime_ms(),
                    cold_start_window=self._is_within_cold_start_window(),
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
        except (OSError, RuntimeError, ValueError, TypeError) as exc:
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
        except (ConfigError, OSError, ValueError, TypeError) as exc:
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
            has_supervisors = bool(self._opencode_supervisors)
        if intervals:
            return min(intervals)
        if not has_supervisors:
            return DISCORD_OPENCODE_PRUNE_EMPTY_INTERVAL_SECONDS
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
        except (OSError, ValueError, KeyError) as exc:
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
            except (ConfigError, OSError, ValueError, TypeError) as exc:
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
            except (OSError, ValueError, RuntimeError) as exc:
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
                except (OSError, RuntimeError, ValueError) as exc:
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
            except (OSError, RuntimeError, ValueError) as exc:
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
                with contextlib.suppress(Exception):  # intentional: shutdown cleanup
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
        return await list_opencode_models_for_picker(
            self, workspace_path=workspace_path
        )

    async def _list_threads_paginated(
        self,
        client: CodexAppServerClient,
        *,
        limit: int,
        max_pages: int,
        needed_ids: Optional[set[str]] = None,
    ) -> tuple[list[dict[str, Any]], set[str]]:
        return await list_threads_paginated(
            self,
            client,
            limit=limit,
            max_pages=max_pages,
            needed_ids=needed_ids,
        )

    async def _list_session_threads_for_picker(
        self,
        *,
        workspace_root: Path,
        current_thread_id: Optional[str],
    ) -> list[tuple[str, str]]:
        return await list_session_threads_for_picker(
            self,
            workspace_root=workspace_root,
            current_thread_id=current_thread_id,
        )

    async def _list_recent_commits_for_picker(
        self,
        workspace_root: Path,
        *,
        limit: int = DISCORD_SELECT_OPTION_MAX_OPTIONS,
    ) -> list[tuple[str, str]]:
        return await list_recent_commits_for_picker(workspace_root, limit=limit)

    async def _prompt_flow_action_picker(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        action: str,
        deferred: bool = False,
    ) -> None:
        await prompt_flow_action_picker(
            self,
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            action=action,
            deferred=deferred,
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
        return await resolve_flow_run_input(
            self,
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            action=action,
            run_id_opt=run_id_opt,
            deferred=deferred,
        )

    @staticmethod
    def _flow_archive_prompt_text(record: FlowRunRecord) -> str:
        return flow_archive_prompt_text(record)

    @staticmethod
    def _build_flow_archive_confirmation_components(
        run_id: str,
        *,
        prompt_variant: bool,
    ) -> list[dict[str, Any]]:
        return build_flow_archive_confirmation_components(
            run_id,
            prompt_variant=prompt_variant,
        )

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
        managed_thread_surface_key: Optional[str] = None,
        supervision: Optional[Any] = None,
        chat_ux_snapshot: Optional[Any] = None,
    ) -> DiscordMessageTurnResult:
        async def _run_turn() -> DiscordMessageTurnResult:
            if orchestrator_channel_key.startswith("pma:"):
                return await run_managed_thread_turn_for_message(
                    self,
                    workspace_root=workspace_root,
                    prompt_text=prompt_text,
                    input_items=input_items,
                    source_message_id=source_message_id,
                    agent=agent,
                    model_override=model_override,
                    reasoning_effort=reasoning_effort,
                    session_key=session_key,
                    orchestrator_channel_key=orchestrator_channel_key,
                    managed_thread_surface_key=managed_thread_surface_key,
                    supervision=supervision,
                    chat_ux_snapshot=chat_ux_snapshot,
                )
            return await run_agent_turn_for_message(
                self,
                workspace_root=workspace_root,
                prompt_text=prompt_text,
                input_items=input_items,
                source_message_id=source_message_id,
                agent=agent,
                model_override=model_override,
                reasoning_effort=reasoning_effort,
                session_key=session_key,
                orchestrator_channel_key=orchestrator_channel_key,
                max_actions=DISCORD_TURN_PROGRESS_MAX_ACTIONS,
                min_edit_interval_seconds=DISCORD_TURN_PROGRESS_MIN_EDIT_INTERVAL_SECONDS,
                heartbeat_interval_seconds=DISCORD_TURN_PROGRESS_HEARTBEAT_INTERVAL_SECONDS,
                log_event_fn=log_event,
                chat_ux_snapshot=chat_ux_snapshot,
            )
            return await run_agent_turn_for_message(
                self,
                workspace_root=workspace_root,
                prompt_text=prompt_text,
                input_items=input_items,
                source_message_id=source_message_id,
                agent=agent,
                model_override=model_override,
                reasoning_effort=reasoning_effort,
                session_key=session_key,
                orchestrator_channel_key=orchestrator_channel_key,
                max_actions=DISCORD_TURN_PROGRESS_MAX_ACTIONS,
                min_edit_interval_seconds=DISCORD_TURN_PROGRESS_MIN_EDIT_INTERVAL_SECONDS,
                heartbeat_interval_seconds=DISCORD_TURN_PROGRESS_HEARTBEAT_INTERVAL_SECONDS,
                log_event_fn=log_event,
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
        except subprocess.SubprocessError as exc:
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
        except (DiscordAPIError, OSError) as exc:
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
        if subcommand not in ("on", "off", "status"):
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

    def _validate_command_sync_config(self) -> None:
        registration = self._config.command_registration
        if not registration.enabled:
            return

        application_id = (self._config.application_id or "").strip()
        if not application_id:
            raise ValueError("missing Discord application id for command sync")
        if registration.scope == "guild" and not registration.guild_ids:
            raise ValueError("guild scope requires at least one guild_id")

    async def _sync_application_commands_on_startup(self) -> None:
        registration = self._config.command_registration
        if not registration.enabled:
            log_event(
                self._logger,
                logging.INFO,
                "discord.commands.sync.disabled",
            )
            return

        self._validate_command_sync_config()

        application_id = (self._config.application_id or "").strip()
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
        except (DiscordAPIError, OSError, RuntimeError) as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.commands.sync.startup_failed",
                scope=registration.scope,
                command_count=len(commands),
                exc=exc,
            )

    async def _run_startup_command_sync_background(self) -> None:
        started_at = time.monotonic()
        log_event(
            self._logger,
            logging.INFO,
            "discord.commands.sync.startup_scheduled",
            service_uptime_ms=self._service_uptime_ms(now=started_at),
        )
        try:
            await self._sync_application_commands_on_startup()
        except Exception as exc:  # intentional: background startup sync is best-effort
            log_event(
                self._logger,
                logging.WARNING,
                "discord.commands.sync.startup_background_failed",
                elapsed_ms=round((time.monotonic() - started_at) * 1000, 1),
                service_uptime_ms=self._service_uptime_ms(),
                exc=exc,
            )
            return
        finished_at = time.monotonic()
        log_event(
            self._logger,
            logging.INFO,
            "discord.commands.sync.startup_finished",
            elapsed_ms=round((finished_at - started_at) * 1000, 1),
            service_uptime_ms=self._service_uptime_ms(now=finished_at),
        )

    async def _shutdown(self) -> None:
        shutdown_deadline = (
            time.monotonic() + DISCORD_BACKGROUND_TASK_SHUTDOWN_GRACE_SECONDS
        )
        pending_shutdown_tasks: list[asyncio.Task[Any]] = []
        while True:
            drainable_tasks = [
                task
                for task in list(self._background_shutdown_wait_tasks)
                if not task.done()
            ]
            if not drainable_tasks:
                break
            remaining = shutdown_deadline - time.monotonic()
            if remaining <= 0:
                pending_shutdown_tasks = drainable_tasks
                break
            done, pending = await asyncio.wait(
                drainable_tasks,
                timeout=remaining,
            )
            self._background_shutdown_wait_tasks.difference_update(done)
            pending_shutdown_tasks = list(pending)
            if pending:
                break
        if pending_shutdown_tasks:
            shutdown_reconcile_contexts: list[dict[str, Any]] = []
            for task in pending_shutdown_tasks:
                task_context = getattr(task, "_discord_progress_task_context", None)
                if not isinstance(task_context, dict) or not task_context:
                    continue
                shutdown_context = dict(task_context)
                shutdown_note = shutdown_context.get("shutdown_note")
                if isinstance(shutdown_note, str) and shutdown_note.strip():
                    shutdown_context["failure_note"] = shutdown_note.strip()
                shutdown_reconcile_contexts.append(shutdown_context)
            if shutdown_reconcile_contexts:
                await asyncio.gather(
                    *(
                        self._reconcile_background_task_failure(
                            task_context,
                            allow_channel_fallback=False,
                        )
                        for task_context in shutdown_reconcile_contexts
                    ),
                    return_exceptions=True,
                )
            log_event(
                self._logger,
                logging.WARNING,
                "discord.background_task.shutdown_timeout",
                timeout_seconds=DISCORD_BACKGROUND_TASK_SHUTDOWN_GRACE_SECONDS,
                pending_count=len(pending_shutdown_tasks),
            )
        if self._background_tasks:
            for task in list(self._background_tasks):
                task.cancel()
            await asyncio.gather(*list(self._background_tasks), return_exceptions=True)
            self._background_tasks.clear()
        self._background_shutdown_wait_tasks.clear()
        await self._command_runner.shutdown()
        if self._owns_gateway:
            with contextlib.suppress(Exception):  # intentional: shutdown cleanup
                await self._gateway.stop()
        if self._owns_rest and hasattr(self._rest, "close"):
            with contextlib.suppress(Exception):  # intentional: shutdown cleanup
                await self._rest.close()
        if self._owns_store:
            with contextlib.suppress(Exception):  # intentional: shutdown cleanup
                await self._store.close()
        await self._close_all_app_server_supervisors()
        await self._close_all_opencode_supervisors()
        if self._hub_client is not None:
            with contextlib.suppress(Exception):
                await self._hub_client.aclose()
        self._reap_managed_processes(stage="shutdown")

    async def _close_all_app_server_supervisors(self) -> None:
        async with self._app_server_lock:
            supervisors = list(self._app_server_supervisors.values())
            self._app_server_supervisors.clear()
        for supervisor in supervisors:
            with contextlib.suppress(Exception):  # intentional: shutdown cleanup
                await supervisor.close_all()

    async def _close_all_opencode_supervisors(self) -> None:
        async with self._opencode_lock:
            opencode_supervisors = [
                entry.supervisor for entry in self._opencode_supervisors.values()
            ]
            self._opencode_supervisors.clear()
        for supervisor in opencode_supervisors:
            with contextlib.suppress(Exception):  # intentional: shutdown cleanup
                await supervisor.close_all()

    async def _watch_ticket_flow_pauses(self) -> None:
        await watch_ticket_flow_pauses(self)

    async def _scan_and_enqueue_pause_notifications(self) -> None:
        await _scan_and_enqueue_pause_notifications_impl(self)

    async def _run_chat_queue_reset_loop(self) -> None:
        idle_streak = 0
        poll_interval = CHAT_QUEUE_RESET_POLL_INTERVAL_SECONDS
        while True:
            with track_loop("discord.chat_queue_reset_poll") as scope:
                try:
                    if not self._chat_queue_control_store.has_reset_requests(
                        platform="discord"
                    ):
                        idle_streak += 1
                        poll_interval = min(
                            CHAT_QUEUE_RESET_POLL_INTERVAL_SECONDS
                            * (CHAT_QUEUE_RESET_POLL_BACKOFF_GROW_FACTOR**idle_streak),
                            CHAT_QUEUE_RESET_POLL_MAX_INTERVAL_SECONDS,
                        )
                    else:
                        scope.record_disk_read(1)
                        requests = self._chat_queue_control_store.take_reset_requests(
                            platform="discord"
                        )
                        if requests:
                            scope.mark_productive()
                            idle_streak = 0
                            poll_interval = CHAT_QUEUE_RESET_POLL_INTERVAL_SECONDS
                        for request in requests:
                            conversation_id = str(
                                request.get("conversation_id") or ""
                            ).strip()
                            if not conversation_id:
                                continue
                            result = await self._dispatcher.force_reset(conversation_id)
                            log_event(
                                self._logger,
                                logging.WARNING,
                                "discord.chat_queue.reset_applied",
                                conversation_id=conversation_id,
                                chat_id=request.get("chat_id"),
                                thread_id=request.get("thread_id"),
                                requested_at=request.get("requested_at"),
                                requested_by=request.get("requested_by"),
                                cancelled_pending=result.get("cancelled_pending"),
                                cancelled_active=result.get("cancelled_active"),
                            )
                except (
                    Exception
                ) as exc:  # intentional: long-running loop must not crash
                    log_event(
                        self._logger,
                        logging.WARNING,
                        "discord.chat_queue.reset_scan_failed",
                        exc=exc,
                    )
            await asyncio.sleep(poll_interval)

    async def _watch_ticket_flow_terminals(self) -> None:
        await watch_ticket_flow_terminals(self)

    async def _send_channel_message(
        self, channel_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        payload = dict(payload)
        content = payload.get("content")
        if isinstance(content, str):
            payload["content"] = sanitize_discord_outbound_text(content)
        content_len = len(payload.get("content", "") or "")
        log_event(
            self._logger,
            logging.DEBUG,
            "discord.channel_message.sending",
            channel_id=channel_id,
            content_len=content_len,
        )
        response = await self._rest.create_channel_message(
            channel_id=channel_id, payload=payload
        )
        message_id = response.get("id") if isinstance(response, dict) else None
        log_event(
            self._logger,
            logging.DEBUG,
            "discord.channel_message.sent",
            channel_id=channel_id,
            content_len=content_len,
            message_id=message_id,
        )
        return response

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
        except (DiscordAPIError, OSError, RuntimeError) as exc:
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
            except (OSError, ValueError, TypeError) as enqueue_exc:
                log_event(
                    self._logger,
                    logging.ERROR,
                    "discord.channel_message.enqueue_failed",
                    channel_id=channel_id,
                    record_id=outbox_record_id,
                    exc=enqueue_exc,
                )

    async def _handle_discord_outbox_delivery(
        self, record: OutboxRecord, delivered_message_id: Optional[str]
    ) -> None:
        if not isinstance(delivered_message_id, str) or not delivered_message_id:
            return
        if self._hub_client is None:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.outbox.delivery_mark.hub_client_unavailable",
                record_id=record.record_id,
            )
            return
        from ...core.hub_control_plane import (
            NotificationDeliveryMarkRequest as _CPDeliveryMarkRequest,
        )

        try:
            await self._hub_client.mark_notification_delivered(
                _CPDeliveryMarkRequest(
                    delivery_record_id=record.record_id,
                    delivered_message_id=delivered_message_id,
                )
            )
        except (HubControlPlaneError, OSError, ValueError) as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.outbox.delivery_mark.control_plane_failed",
                record_id=record.record_id,
                exc=exc,
            )

    async def _delete_channel_message_safe(
        self,
        channel_id: str,
        message_id: str,
        *,
        record_id: Optional[str] = None,
    ) -> bool:
        if not isinstance(message_id, str) or not message_id:
            return False
        try:
            await self._delete_channel_message(channel_id, message_id)
            return True
        except (DiscordAPIError, OSError, RuntimeError) as exc:
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
            except (OSError, ValueError) as enqueue_exc:
                log_event(
                    self._logger,
                    logging.ERROR,
                    "discord.channel_message.delete_enqueue_failed",
                    channel_id=channel_id,
                    message_id=message_id,
                    record_id=outbox_record_id,
                    exc=enqueue_exc,
                )
        return False

    def _spawn_task(
        self, coro: Awaitable[None], *, await_on_shutdown: bool = False
    ) -> asyncio.Task[Any]:
        task = cast(asyncio.Task[Any], asyncio.ensure_future(coro))
        self._background_tasks.add(task)
        if await_on_shutdown:
            self._background_shutdown_wait_tasks.add(task)
        task.add_done_callback(self._on_background_task_done)
        return task

    async def _reconcile_discord_progress_leases_on_startup(self) -> None:
        try:
            reconciled = await reconcile_discord_turn_progress_leases(
                self,
                orphaned=True,
                startup=True,
            )
        except Exception as exc:  # intentional: startup reconciliation is best-effort
            log_event(
                self._logger,
                logging.WARNING,
                "discord.turn.progress_reconcile_startup_failed",
                exc=exc,
            )
            return
        if reconciled:
            log_event(
                self._logger,
                logging.INFO,
                "discord.turn.progress_reconcile_startup_finished",
                reconciled=reconciled,
            )

    async def _reconcile_background_task_failure(
        self,
        task_context: dict[str, Any],
        *,
        allow_channel_fallback: bool = True,
    ) -> int:
        failure_note = task_context.get("failure_note")
        if not isinstance(failure_note, str) or not failure_note.strip():
            failure_note = "Status: this progress message lost its worker."
        reconciled = await reconcile_discord_turn_progress_leases(
            self,
            lease_id=(
                task_context.get("lease_id")
                if isinstance(task_context.get("lease_id"), str)
                else None
            ),
            managed_thread_id=(
                task_context.get("managed_thread_id")
                if isinstance(task_context.get("managed_thread_id"), str)
                else None
            ),
            execution_id=(
                task_context.get("execution_id")
                if isinstance(task_context.get("execution_id"), str)
                else None
            ),
            channel_id=(
                task_context.get("channel_id")
                if isinstance(task_context.get("channel_id"), str)
                else None
            ),
            message_id=(
                task_context.get("message_id")
                if isinstance(task_context.get("message_id"), str)
                else None
            ),
            failure_note=failure_note,
            orphaned=bool(task_context.get("orphaned")),
        )
        if reconciled:
            return int(reconciled)
        if not allow_channel_fallback:
            return 0
        fallback_channel_id = (
            task_context.get("channel_id")
            if isinstance(task_context.get("channel_id"), str)
            else None
        )
        if fallback_channel_id:
            await self._send_channel_message_safe(
                fallback_channel_id,
                {"content": failure_note},
            )
        return 0

    def _on_background_task_done(self, task: asyncio.Task[Any]) -> None:
        self._background_tasks.discard(task)
        self._background_shutdown_wait_tasks.discard(task)
        task_context = getattr(task, "_discord_progress_task_context", None)
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except (
            Exception
        ) as exc:  # intentional: top-level error handler for background tasks
            log_event(
                self._logger,
                logging.WARNING,
                "discord.background_task.failed",
                exc=exc,
            )
            if isinstance(task_context, dict) and task_context:

                async def _reconcile_failure() -> None:
                    await self._reconcile_background_task_failure(task_context)

                reconcile_task = self._spawn_task(_reconcile_failure())
                bind_discord_progress_task_context(reconcile_task)

    async def _on_dispatch(self, event_type: str, payload: dict[str, Any]) -> None:
        if event_type == "INTERACTION_CREATE":
            dispatch_started_at = time.monotonic()
            submission_order = (
                payload.get("__car_dispatch_order")
                if isinstance(payload.get("__car_dispatch_order"), int)
                else None
            )
            ingress_result = await self._ingress.process_raw_payload(payload)
            if not ingress_result.accepted:
                if ingress_result.context is not None:
                    log_event(
                        self._logger,
                        logging.INFO,
                        "discord.interaction.rejected",
                        rejection_reason=ingress_result.rejection_reason,
                        **self._interaction_telemetry_fields(
                            ingress_result.context,
                            now=dispatch_started_at,
                        ),
                    )
                if ingress_result.rejection_reason == "normalization_failed":
                    interaction_id = extract_interaction_id(payload)
                    interaction_token = extract_interaction_token(payload)
                    if interaction_id and interaction_token:
                        await self._respond_ephemeral(
                            interaction_id,
                            interaction_token,
                            "I could not parse this interaction. Please retry the command.",
                        )
                elif (
                    ingress_result.rejection_reason == "unauthorized"
                    and ingress_result.context is not None
                ):
                    ctx = ingress_result.context
                    if ctx.kind == InteractionKind.AUTOCOMPLETE:
                        await self.respond_autocomplete(
                            ctx.interaction_id,
                            ctx.interaction_token,
                            choices=[],
                        )
                    else:
                        await self.respond_ephemeral(
                            ctx.interaction_id,
                            ctx.interaction_token,
                            "This Discord command is not authorized for this channel/user/guild.",
                        )
                self._command_runner.skip_submission_order(submission_order)
                return
            if ingress_result.context is not None:
                ctx = ingress_result.context
                submitted_to_runner = False
                try:
                    envelope = await self._build_runtime_interaction_envelope(ctx)
                    log_event(
                        self._logger,
                        logging.INFO,
                        "discord.interaction.admitted",
                        **self._interaction_telemetry_fields(
                            ctx,
                            now=dispatch_started_at,
                            envelope=envelope,
                        ),
                    )
                    await self._register_chat_operation_received(
                        ctx,
                        conversation_id=envelope.conversation_id,
                    )
                    acked = await self._acknowledge_runtime_envelope(
                        envelope,
                        stage="dispatch",
                    )
                    if not acked and self._dispatch_ack_failure_confirms_expiry(
                        ctx,
                        envelope,
                    ):
                        log_event(
                            self._logger,
                            logging.WARNING,
                            "discord.interaction.delivery_expired_before_dispatch",
                            expired_before_ack=True,
                            ack_budget_seconds=self._initial_ack_budget_seconds(),
                            **self._interaction_telemetry_fields(
                                ctx,
                                envelope=envelope,
                            ),
                        )
                        # The interaction callback window is already gone. Trying to
                        # answer again only produces a second stale-callback failure
                        # that can bubble back into gateway reconnect handling.
                        ctx.timing = replace(
                            ctx.timing,
                            ack_finished_at=time.monotonic(),
                            ingress_finished_at=time.monotonic(),
                        )
                        return
                    if not acked and envelope.dispatch_ack_policy not in (
                        None,
                        "immediate",
                    ):
                        await self._respond_ephemeral(
                            ctx.interaction_id,
                            ctx.interaction_token,
                            "Discord interaction did not acknowledge. Please retry.",
                        )
                        ctx.timing = replace(
                            ctx.timing,
                            ack_finished_at=time.monotonic(),
                            ingress_finished_at=time.monotonic(),
                        )
                        return

                    duplicate_after_ack = await self._register_interaction_ingress(ctx)
                    if duplicate_after_ack:
                        return
                    await self._persist_runtime_interaction(
                        envelope,
                        payload,
                        scheduler_state="acknowledged",
                    )
                    self._ingress.finalize_success(ctx)
                    log_event(
                        self._logger,
                        logging.INFO,
                        "discord.interaction.enqueued",
                        ingress_elapsed_ms=(
                            round(
                                (
                                    ctx.timing.ingress_finished_at
                                    - ctx.timing.ingress_started_at
                                )
                                * 1000,
                                1,
                            )
                            if (
                                ctx.timing.ingress_started_at is not None
                                and ctx.timing.ingress_finished_at is not None
                            )
                            else None
                        ),
                        **self._interaction_telemetry_fields(
                            ctx,
                            envelope=envelope,
                        ),
                    )
                    self._command_runner.submit(
                        envelope.context,
                        payload,
                        resource_keys=envelope.resource_keys,
                        conversation_id=envelope.conversation_id,
                        queue_wait_ack_policy=envelope.queue_wait_ack_policy,
                        submission_order=submission_order,
                    )
                    submitted_to_runner = True
                    # Let the admitted interaction task start before the next gateway
                    # interaction is processed so deferred command ordering stays stable.
                    await asyncio.sleep(0)
                finally:
                    if not submitted_to_runner:
                        self._command_runner.skip_submission_order(submission_order)
                    await self._release_interaction_ingress(ctx.interaction_id)
            return
        if event_type == "MESSAGE_CREATE":
            # Keep MESSAGE_CREATE handling off the gateway hot path. Channel/guild
            # name enrichment can perform Discord REST lookups and must not delay
            # later interaction callbacks that need to ack within ~3 seconds.
            self._spawn_task(
                self._record_channel_directory_seen_from_message_payload(payload)
            )
            message_event = self._chat_adapter.parse_message_event(payload)
            if message_event is not None:
                self._command_runner.submit_event(message_event)

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
        except (OSError, ValueError, TypeError) as exc:
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
        in_flight = self._channel_name_lookups.get(channel_id)
        if in_flight is None:

            async def _load_channel_name() -> Optional[str]:
                try:
                    payload = await fetch(channel_id=channel_id)
                except (DiscordAPIError, OSError) as exc:
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

            in_flight = asyncio.create_task(_load_channel_name())
            self._channel_name_lookups[channel_id] = in_flight
        try:
            return await in_flight
        finally:
            if self._channel_name_lookups.get(channel_id) is in_flight:
                self._channel_name_lookups.pop(channel_id, None)

    async def _resolve_guild_name(self, guild_id: str) -> Optional[str]:
        fetch = getattr(self._rest, "get_guild", None)
        if not callable(fetch):
            self._guild_name_cache[guild_id] = ""
            return None
        in_flight = self._guild_name_lookups.get(guild_id)
        if in_flight is None:

            async def _load_guild_name() -> Optional[str]:
                try:
                    payload = await fetch(guild_id=guild_id)
                except (DiscordAPIError, OSError) as exc:
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

            in_flight = asyncio.create_task(_load_guild_name())
            self._guild_name_lookups[guild_id] = in_flight
        try:
            return await in_flight
        finally:
            if self._guild_name_lookups.get(guild_id) is in_flight:
                self._guild_name_lookups.pop(guild_id, None)

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

    async def _handle_bind(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        guild_id: Optional[str],
        options: dict[str, Any],
    ) -> None:
        await self._run_effectful_handler(
            handle_bind,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            guild_id=guild_id,
            options=options,
        )

    def _list_manifest_repos(self) -> list[tuple[str, str]]:
        from .workspace_commands import _list_manifest_repos as _impl

        return _impl(self)

    def _list_agent_workspaces(self) -> list[tuple[str, str, str]]:
        from .workspace_commands import _list_agent_workspaces as _impl

        return _impl(self)

    def _list_agent_workspaces_from_cache(self) -> list[tuple[str, str, str]]:
        from .workspace_commands import _list_agent_workspaces_from_cache as _impl

        return _impl(self)

    def _resource_owner_for_workspace(
        self,
        workspace_root: Path,
        *,
        repo_id: Optional[str] = None,
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        from .workspace_commands import _resource_owner_for_workspace as _impl

        return _impl(
            self,
            workspace_root,
            repo_id=repo_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
        )

    def _list_bind_workspace_candidates(
        self,
    ) -> list[tuple[Optional[str], Optional[str], str]]:
        from .workspace_commands import _list_bind_workspace_candidates as _impl

        return _impl(self)

    @staticmethod
    def _bind_candidate_value(
        resource_kind: Optional[str],
        resource_id: Optional[str],
        workspace_path: str,
    ) -> str:
        from .workspace_commands import _bind_candidate_value as _impl

        return _impl(resource_kind, resource_id, workspace_path)

    @staticmethod
    def _bind_candidate_label(
        resource_kind: Optional[str],
        resource_id: Optional[str],
        workspace_path: str,
    ) -> str:
        from .workspace_commands import _bind_candidate_label as _impl

        return _impl(resource_kind, resource_id, workspace_path)

    def _build_bind_picker_items(
        self,
        candidates: list[tuple[Optional[str], Optional[str], str]],
    ) -> list[tuple[str, str] | tuple[str, str, Optional[str]]]:
        from .workspace_commands import _build_bind_picker_items as _impl

        return _impl(self, candidates)

    def _build_bind_search_items(
        self,
        candidates: list[tuple[Optional[str], Optional[str], str]],
    ) -> tuple[
        list[tuple[str, str]],
        dict[str, tuple[str, ...]],
        dict[str, tuple[str, ...]],
    ]:
        from .workspace_commands import _build_bind_search_items as _impl

        return _impl(self, candidates)

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
        from .workspace_commands import _resolve_picker_query_or_prompt as _impl

        return await _impl(
            self,
            query=query,
            items=items,
            limit=limit,
            prompt_filtered_items=prompt_filtered_items,
            exact_aliases=exact_aliases,
            aliases=aliases,
        )

    def _build_bind_page_prompt_and_components(
        self,
        candidates: list[tuple[Optional[str], Optional[str], str]],
        *,
        page: int,
    ) -> tuple[str, list[dict[str, Any]]]:
        from .workspace_commands import _build_bind_page_prompt_and_components as _impl

        return _impl(self, candidates, page=page)

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
        deferred = await self._defer_component_update(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )
        if not deferred:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Discord interaction acknowledgement failed. Please retry.",
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
            ticket_text = await asyncio.wait_for(
                asyncio.to_thread(candidate.read_text, encoding="utf-8"),
                timeout=1.5,
            )
        except asyncio.TimeoutError:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                (
                    "Ticket load timed out before opening the modal. "
                    "Try again or edit the file directly."
                ),
            )
            return
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

    async def _handle_tickets(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        workspace_root: Path,
        options: dict[str, Any],
    ) -> None:
        await self._run_effectful_handler(
            handle_tickets,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            workspace_root=workspace_root,
            options=options,
        )

    def _resolve_workspace_from_token(
        self,
        token: str,
        candidates: list[tuple[Optional[str], Optional[str], str]],
    ) -> Optional[tuple[Optional[str], Optional[str], str]]:
        from .workspace_commands import _resolve_workspace_from_token as _impl

        return _impl(token, candidates)

    def _normalize_agent(self, value: Any) -> str:
        return (
            normalize_chat_agent(value, default=self.DEFAULT_AGENT, context=self)
            or self.DEFAULT_AGENT
        )

    def _normalize_agent_profile(self, value: Any) -> Optional[str]:
        return normalize_hermes_profile(value, context=self)

    def _resolve_agent_state(
        self, binding: Optional[Mapping[str, Any]]
    ) -> tuple[str, Optional[str]]:
        if binding is None:
            return self.DEFAULT_AGENT, None
        return resolve_chat_agent_and_profile(
            binding.get("agent"),
            binding.get("agent_profile"),
            default=self.DEFAULT_AGENT,
            context=self,
        )

    def _discord_thread_agent_ids(
        self,
        *,
        agent: str,
        agent_profile: Optional[str] = None,
    ) -> tuple[str, ...]:
        agent_ids: list[str] = []
        seen: set[str] = set()

        def _add_agent_id(value: object) -> None:
            normalized = str(value or "").strip().lower()
            if not normalized or normalized in seen:
                return
            seen.add(normalized)
            agent_ids.append(normalized)

        runtime_agent = resolve_chat_runtime_agent(
            agent,
            agent_profile,
            default=self.DEFAULT_AGENT,
            context=self,
        )
        _add_agent_id(agent)
        _add_agent_id(runtime_agent)
        if agent == "hermes":
            normalized_profile = normalize_hermes_profile(
                agent_profile,
                context=self,
            )
            if normalized_profile is not None:
                for option in chat_hermes_profile_options(self):
                    if option.profile == normalized_profile:
                        _add_agent_id(option.runtime_agent)
        return tuple(agent_ids)

    def _discord_thread_matches_agent(
        self,
        thread: Any,
        *,
        agent: str,
        agent_profile: Optional[str] = None,
    ) -> bool:
        expected_agent, expected_profile = resolve_chat_agent_and_profile(
            agent,
            agent_profile,
            default=self.DEFAULT_AGENT,
            context=self,
        )
        raw_thread_agent = getattr(thread, "agent_id", None)
        thread_agent = normalize_chat_agent(
            raw_thread_agent,
            default=None,
            context=self,
        )
        thread_profile = None
        if thread_agent == "hermes":
            thread_profile = normalize_hermes_profile(
                getattr(thread, "agent_profile", None),
                context=self,
            )
        elif thread_agent is None:
            legacy_profile = normalize_hermes_profile(
                raw_thread_agent,
                context=self,
            )
            if legacy_profile is None:
                return False
            thread_agent = "hermes"
            thread_profile = (
                normalize_hermes_profile(
                    getattr(thread, "agent_profile", None),
                    context=self,
                )
                or legacy_profile
            )
        if thread_agent != expected_agent:
            return False
        if expected_agent != "hermes":
            return True
        return (thread_profile or None) == (expected_profile or None)

    def _runtime_agent_for_binding(self, binding: Optional[Mapping[str, Any]]) -> str:
        agent, profile = self._resolve_agent_state(binding)
        return resolve_chat_runtime_agent(
            agent,
            profile,
            default=self.DEFAULT_AGENT,
            context=self,
        )

    def _format_agent_state(self, agent: str, profile: Optional[str]) -> str:
        return format_chat_agent_selection(agent, profile)

    def _agent_supports_effort(self, agent: str) -> bool:
        return chat_agent_supports_effort(agent, self)

    def _agent_supports_resume(self, agent: str) -> bool:
        return self._agent_supports_capability(agent, "durable_threads")

    def _status_model_label(self, binding: dict[str, Any]) -> str:
        from .workspace_commands import _status_model_label as _impl

        return _impl(binding)

    def _status_effort_label(self, binding: dict[str, Any], agent: str) -> str:
        from .workspace_commands import _status_effort_label as _impl

        return _impl(self, binding, agent)

    async def _read_status_rate_limits(
        self, workspace_path: Optional[str], *, agent: str
    ) -> Optional[dict[str, Any]]:
        from .workspace_commands import _read_status_rate_limits as _impl

        return await _impl(self, workspace_path, agent=agent)

    async def _list_model_items_for_binding(
        self,
        *,
        binding: dict[str, Any],
        agent: str,
        limit: int,
    ) -> Optional[list[tuple[str, str]]]:
        return await list_model_items_for_binding(
            self,
            binding=binding,
            agent=agent,
            limit=limit,
        )

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
        except (
            Exception
        ) as exc:  # intentional: client.request can raise arbitrary errors
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
        await self._run_effectful_handler(
            dispatch_autocomplete,
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
        return normalize_discord_command_path(command_path)

    def _interaction_session_kind(
        self,
        kind: InteractionKind | InteractionSessionKind | str | None,
    ) -> InteractionSessionKind:
        if isinstance(kind, InteractionSessionKind):
            return kind
        if isinstance(kind, InteractionKind):
            return InteractionSessionKind(kind.value)
        if isinstance(kind, str):
            try:
                return InteractionSessionKind(kind)
            except ValueError:
                return InteractionSessionKind.UNKNOWN
        return InteractionSessionKind.UNKNOWN

    def _ensure_interaction_session(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        kind: InteractionKind | InteractionSessionKind | str | None = None,
    ) -> DiscordInteractionSession:
        return self._responder.start_session(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            kind=self._interaction_session_kind(kind),
        )

    def _get_interaction_session(
        self,
        interaction_token: str,
    ) -> Optional[DiscordInteractionSession]:
        return self._responder.get_session(interaction_token)

    def interaction_has_initial_response(
        self,
        interaction_token: str,
    ) -> bool:
        session = self._get_interaction_session(interaction_token)
        return bool(session and session.has_initial_response())

    def interaction_is_deferred(
        self,
        interaction_token: str,
    ) -> bool:
        session = self._get_interaction_session(interaction_token)
        return bool(session and session.is_deferred())

    def _interaction_has_initial_response(
        self,
        interaction_token: str,
    ) -> bool:
        return self.interaction_has_initial_response(interaction_token)

    def _interaction_is_deferred(
        self,
        interaction_token: str,
    ) -> bool:
        return self.interaction_is_deferred(interaction_token)

    async def _load_interaction_ack_mode(self, interaction_id: str) -> Optional[str]:
        record = await self._store.get_interaction(interaction_id)
        if record is None:
            return None
        if isinstance(record.ack_mode, str) and record.ack_mode.strip():
            return record.ack_mode
        cursor = record.delivery_cursor_json or {}
        if not isinstance(cursor, dict):
            return None
        hint = cursor.get("ack_mode_hint")
        return hint if isinstance(hint, str) and hint.strip() else None

    def _chat_operation_store_or_none(self) -> Optional[SQLiteChatOperationLedger]:
        store = getattr(self, "_chat_operation_store", None)
        if isinstance(store, SQLiteChatOperationLedger):
            return store
        return None

    def _chat_operation_terminal_duplicate(
        self, snapshot: ChatOperationSnapshot
    ) -> bool:
        if snapshot.terminal_outcome in {"abandoned", "expired"}:
            return True
        return snapshot.state in {
            ChatOperationState.COMPLETED,
            ChatOperationState.INTERRUPTED,
            ChatOperationState.FAILED,
            ChatOperationState.CANCELLED,
        }

    def _discord_chat_operation_state_for_scheduler(
        self, scheduler_state: str
    ) -> Optional[ChatOperationState]:
        normalized = str(scheduler_state or "").strip().lower()
        if normalized in {
            "received",
            "dispatch_ready",
            "dispatch_ack_pending",
            "queue_wait_ack_pending",
        }:
            return ChatOperationState.RECEIVED
        if normalized == "acknowledged":
            return ChatOperationState.ACKNOWLEDGED
        if normalized in {"scheduled", "waiting_on_resources", "recovery_scheduled"}:
            return ChatOperationState.QUEUED
        if normalized == "executing":
            return ChatOperationState.RUNNING
        if normalized in {"delivery_pending", "delivery_replaying"}:
            return ChatOperationState.DELIVERING
        if normalized == "completed":
            return ChatOperationState.COMPLETED
        if normalized == "abandoned":
            return ChatOperationState.FAILED
        if normalized == "delivery_expired":
            return ChatOperationState.CANCELLED
        if "interrupt" in normalized:
            return ChatOperationState.INTERRUPTING
        return None

    async def _register_chat_operation_received(
        self,
        ctx: IngressContext,
        *,
        conversation_id: Optional[str] = None,
    ) -> Optional[ChatOperationSnapshot]:
        store = self._chat_operation_store_or_none()
        if store is None:
            return None
        registration = store.register_operation(
            operation_id=ctx.interaction_id,
            surface_kind="discord",
            surface_operation_key=ctx.interaction_id,
            state=ChatOperationState.RECEIVED,
            conversation_id=conversation_id,
            metadata=self._interaction_ledger_metadata(ctx),
        )
        store.patch_operation(
            registration.snapshot.operation_id,
            ack_requested_at=now_iso(),
        )
        if registration.inserted:
            return registration.snapshot
        return store.patch_operation(
            ctx.interaction_id,
            conversation_id=conversation_id or registration.snapshot.conversation_id,
            metadata_updates=self._interaction_ledger_metadata(ctx),
        )

    async def _patch_chat_operation(
        self,
        interaction_id: str,
        *,
        state: Optional[ChatOperationState] = None,
        validate_transition: bool = True,
        metadata_updates: Optional[Mapping[str, Any]] = None,
        **changes: Any,
    ) -> Optional[ChatOperationSnapshot]:
        store = self._chat_operation_store_or_none()
        if store is None:
            return None
        patch_state = state if state is not None else _PATCH_STATE_UNSET
        try:
            return store.patch_operation(
                interaction_id,
                state=patch_state,
                validate_transition=validate_transition,
                metadata_updates=metadata_updates,
                **changes,
            )
        except ValueError:
            current = store.get_operation(interaction_id)
            if current is None:
                return None
            if current.first_visible_feedback_at is not None:
                changes["first_visible_feedback_at"] = current.first_visible_feedback_at
            fallback_state = state or current.state
            merged_metadata = dict(current.metadata)
            if metadata_updates:
                merged_metadata.update(dict(metadata_updates))
            return store.upsert_operation(
                replace(
                    current,
                    state=fallback_state,
                    execution_id=changes.get("execution_id", current.execution_id),
                    backend_turn_id=changes.get(
                        "backend_turn_id", current.backend_turn_id
                    ),
                    status_message=changes.get(
                        "status_message", current.status_message
                    ),
                    blocking_reason=changes.get(
                        "blocking_reason", current.blocking_reason
                    ),
                    conversation_id=changes.get(
                        "conversation_id", current.conversation_id
                    ),
                    ack_requested_at=changes.get(
                        "ack_requested_at", current.ack_requested_at
                    ),
                    ack_completed_at=changes.get(
                        "ack_completed_at", current.ack_completed_at
                    ),
                    first_visible_feedback_at=changes.get(
                        "first_visible_feedback_at",
                        current.first_visible_feedback_at,
                    ),
                    anchor_ref=changes.get("anchor_ref", current.anchor_ref),
                    interrupt_ref=changes.get("interrupt_ref", current.interrupt_ref),
                    delivery_state=changes.get(
                        "delivery_state", current.delivery_state
                    ),
                    delivery_cursor=changes.get(
                        "delivery_cursor", current.delivery_cursor
                    ),
                    delivery_attempt_count=int(
                        changes.get(
                            "delivery_attempt_count",
                            current.delivery_attempt_count,
                        )
                        or 0
                    ),
                    delivery_claimed_at=changes.get(
                        "delivery_claimed_at", current.delivery_claimed_at
                    ),
                    terminal_outcome=changes.get(
                        "terminal_outcome", current.terminal_outcome
                    ),
                    terminal_detail=changes.get(
                        "terminal_detail", current.terminal_detail
                    ),
                    updated_at=changes.get("updated_at", now_iso()),
                    metadata=merged_metadata,
                )
            )

    async def _record_interaction_ack(
        self,
        interaction_id: str,
        interaction_token: str,
        ack_mode: str,
        original_response_message_id: Optional[str],
    ) -> None:
        await self._store.mark_interaction_acknowledged(
            interaction_id,
            ack_mode=ack_mode,
            original_response_message_id=original_response_message_id,
        )
        visible_delivery = ack_mode in {"immediate_message", "component_update"}
        await self._patch_chat_operation(
            interaction_id,
            state=(
                ChatOperationState.VISIBLE
                if visible_delivery
                else ChatOperationState.ACKNOWLEDGED
            ),
            ack_completed_at=now_iso(),
            first_visible_feedback_at=(now_iso() if visible_delivery else None),
            anchor_ref=original_response_message_id,
        )

    async def _record_interaction_delivery(
        self,
        interaction_id: str,
        delivery_status: str,
        delivery_error: Optional[str],
        original_response_message_id: Optional[str],
    ) -> None:
        await self._store.record_interaction_delivery(
            interaction_id,
            delivery_status=delivery_status,
            delivery_error=delivery_error,
            original_response_message_id=original_response_message_id,
        )
        state = None
        delivery_state = "delivered"
        terminal_outcome = None
        record = await self._store.get_interaction(interaction_id)
        if delivery_status in {
            "initial_response_sent",
            "followup_sent",
            "original_message_edited",
            "component_message_updated",
        }:
            state = (
                ChatOperationState.COMPLETED
                if record is not None and record.execution_status == "completed"
                else ChatOperationState.VISIBLE
            )
        elif delivery_status in {
            "ack_deferred_ephemeral",
            "ack_deferred_public",
            "ack_deferred_component_update",
        }:
            state = ChatOperationState.ACKNOWLEDGED
        elif delivery_status.endswith("_failed"):
            state = ChatOperationState.DELIVERING
            delivery_state = "failed"
            terminal_outcome = "delivery_failed"
        await self._patch_chat_operation(
            interaction_id,
            state=state,
            anchor_ref=original_response_message_id,
            delivery_state=delivery_state,
            terminal_outcome=terminal_outcome,
            terminal_detail=delivery_error,
        )

    async def _record_interaction_delivery_cursor(
        self,
        interaction_id: str,
        cursor: Optional[dict[str, Any]],
    ) -> None:
        scheduler_state = None
        if isinstance(cursor, dict):
            state = str(cursor.get("state") or "").strip()
            if state == "pending":
                scheduler_state = "delivery_pending"
            elif state == "failed":
                scheduler_state = "delivery_pending"
            elif state == "completed":
                record = await self._store.get_interaction(interaction_id)
                if record is not None and record.execution_status == "completed":
                    scheduler_state = "completed"
        elif cursor is None:
            record = await self._store.get_interaction(interaction_id)
            if (
                record is not None
                and record.execution_status == "completed"
                and record.scheduler_state
                not in {"completed", "delivery_expired", "abandoned"}
            ):
                scheduler_state = "completed"
        await self._store.update_interaction_delivery_cursor(
            interaction_id,
            delivery_cursor_json=cursor,
            scheduler_state=scheduler_state,
        )
        shared_state = None
        delivery_state = None
        delivery_attempt_count = None
        if isinstance(cursor, dict):
            delivery_state = str(cursor.get("state") or "").strip() or None
            if delivery_state in {"pending", "failed"}:
                shared_state = ChatOperationState.DELIVERING
            elif delivery_state == "completed":
                record = await self._store.get_interaction(interaction_id)
                if record is not None and record.execution_status == "completed":
                    shared_state = ChatOperationState.COMPLETED
            record = await self._store.get_interaction(interaction_id)
            if record is not None:
                delivery_attempt_count = int(record.attempt_count or 0)
        elif cursor is None:
            record = await self._store.get_interaction(interaction_id)
            if record is not None and record.execution_status == "completed":
                shared_state = ChatOperationState.COMPLETED
            delivery_state = "delivered"
        await self._patch_chat_operation(
            interaction_id,
            state=shared_state,
            delivery_state=delivery_state,
            delivery_cursor=cursor,
            delivery_attempt_count=delivery_attempt_count,
            validate_transition=False,
        )

    async def _mark_interaction_scheduler_state(
        self,
        ctx: IngressContext,
        *,
        scheduler_state: str,
        increment_attempt_count: bool = False,
    ) -> None:
        await self._store.mark_interaction_scheduler_state(
            ctx.interaction_id,
            scheduler_state=scheduler_state,
            increment_attempt_count=increment_attempt_count,
        )
        record = await self._store.get_interaction(ctx.interaction_id)
        terminal_outcome = None
        if scheduler_state == "abandoned":
            terminal_outcome = "abandoned"
        elif scheduler_state == "delivery_expired":
            terminal_outcome = "expired"
        await self._patch_chat_operation(
            ctx.interaction_id,
            state=self._discord_chat_operation_state_for_scheduler(scheduler_state),
            delivery_attempt_count=(
                int(record.attempt_count or 0) if record is not None else None
            ),
            terminal_outcome=terminal_outcome,
            validate_transition=False,
        )

    async def mark_interaction_scheduler_state(
        self,
        ctx: IngressContext,
        *,
        scheduler_state: str,
        increment_attempt_count: bool = False,
    ) -> None:
        await self._mark_interaction_scheduler_state(
            ctx,
            scheduler_state=scheduler_state,
            increment_attempt_count=increment_attempt_count,
        )

    def _envelope_from_ledger_record(
        self,
        record: InteractionLedgerRecord,
    ) -> Optional[RuntimeInteractionEnvelope]:
        envelope_json = record.envelope_json or {}
        if not envelope_json:
            return None
        raw_kind = str(envelope_json.get("kind") or record.interaction_kind).strip()
        try:
            kind = InteractionKind(raw_kind)
        except ValueError:
            return None
        ack_mode = record.ack_mode
        if ack_mode is None and isinstance(record.delivery_cursor_json, dict):
            hint = record.delivery_cursor_json.get("ack_mode_hint")
            if isinstance(hint, str) and hint.strip():
                ack_mode = hint
        command_spec = None
        raw_command_spec = envelope_json.get("command_spec")
        if isinstance(raw_command_spec, dict):
            raw_path = raw_command_spec.get("path")
            raw_options = raw_command_spec.get("options")
            if isinstance(raw_path, list) and all(
                isinstance(item, str) and item.strip() for item in raw_path
            ):
                command_spec = CommandSpec(
                    path=tuple(raw_path),
                    options=raw_options if isinstance(raw_options, dict) else {},
                    ack_policy=(
                        raw_command_spec.get("ack_policy")
                        if isinstance(raw_command_spec.get("ack_policy"), str)
                        else None
                    ),
                    ack_timing=cast(
                        Any,
                        raw_command_spec.get("ack_timing") or "dispatch",
                    ),
                    requires_workspace=bool(
                        raw_command_spec.get("requires_workspace", False)
                    ),
                )
        ctx = IngressContext(
            interaction_id=record.interaction_id,
            interaction_token=record.interaction_token,
            channel_id=str(envelope_json.get("channel_id") or record.channel_id),
            guild_id=(
                envelope_json.get("guild_id")
                if isinstance(envelope_json.get("guild_id"), str)
                else record.guild_id
            ),
            user_id=(
                envelope_json.get("user_id")
                if isinstance(envelope_json.get("user_id"), str)
                else record.user_id
            ),
            kind=kind,
            deferred=bool(
                ack_mode
                in {"defer_public", "defer_ephemeral", "defer_component_update"}
            ),
            command_spec=command_spec,
            custom_id=(
                envelope_json.get("custom_id")
                if isinstance(envelope_json.get("custom_id"), str)
                else None
            ),
            values=(
                envelope_json.get("values")
                if isinstance(envelope_json.get("values"), list)
                else None
            ),
            modal_values=(
                envelope_json.get("modal_values")
                if isinstance(envelope_json.get("modal_values"), dict)
                else None
            ),
            focused_name=(
                envelope_json.get("focused_name")
                if isinstance(envelope_json.get("focused_name"), str)
                else None
            ),
            focused_value=(
                envelope_json.get("focused_value")
                if isinstance(envelope_json.get("focused_value"), str)
                else None
            ),
            message_id=(
                envelope_json.get("message_id")
                if isinstance(envelope_json.get("message_id"), str)
                else None
            ),
            timing=IngressTiming(),
        )
        return RuntimeInteractionEnvelope(
            context=ctx,
            conversation_id=(
                envelope_json.get("conversation_id")
                if isinstance(envelope_json.get("conversation_id"), str)
                else record.conversation_id
            ),
            resource_keys=record.resource_keys,
            dispatch_ack_policy=(
                envelope_json.get("dispatch_ack_policy")
                if isinstance(envelope_json.get("dispatch_ack_policy"), str)
                else None
            ),
            queue_wait_ack_policy=(
                envelope_json.get("queue_wait_ack_policy")
                if isinstance(envelope_json.get("queue_wait_ack_policy"), str)
                else None
            ),
        )

    async def _mark_interaction_recovery_terminal(
        self,
        record: InteractionLedgerRecord,
        *,
        scheduler_state: str,
        reason: str,
        log_level: int = logging.WARNING,
    ) -> None:
        await self._store.mark_interaction_scheduler_state(
            record.interaction_id,
            scheduler_state=scheduler_state,
        )
        await self._patch_chat_operation(
            record.interaction_id,
            state=self._discord_chat_operation_state_for_scheduler(scheduler_state),
            terminal_outcome=(
                "abandoned"
                if scheduler_state == "abandoned"
                else ("expired" if scheduler_state == "delivery_expired" else None)
            ),
            terminal_detail=reason,
            validate_transition=False,
        )
        log_event(
            self._logger,
            log_level,
            "discord.interaction.recovery.abandoned",
            interaction_id=record.interaction_id,
            scheduler_state=scheduler_state,
            execution_status=record.execution_status,
            reason=reason,
        )

    async def _resume_interaction_recovery(self) -> None:
        records = await self._store.list_recoverable_interactions()
        now = datetime.now(timezone.utc)
        shared_store = self._chat_operation_store_or_none()
        for record in records:
            envelope = self._envelope_from_ledger_record(record)
            if envelope is None or record.payload_json is None:
                await self._mark_interaction_recovery_terminal(
                    record,
                    scheduler_state="abandoned",
                    reason="missing_runtime_envelope",
                    log_level=logging.ERROR,
                )
                continue
            snapshot = (
                shared_store.get_operation(record.interaction_id)
                if shared_store is not None
                else None
            )
            if int(record.attempt_count or 0) >= _INTERACTION_RECOVERY_MAX_ATTEMPTS:
                await self._mark_interaction_recovery_terminal(
                    record,
                    scheduler_state="abandoned",
                    reason="max_recovery_attempts_exceeded",
                )
                continue
            cursor = record.delivery_cursor_json or {}
            cursor_state = (
                str(cursor.get("state") or "").strip()
                if isinstance(cursor, dict)
                else ""
            )
            cursor_operation = (
                str(cursor.get("operation") or "").strip()
                if isinstance(cursor, dict)
                else ""
            )
            has_pending_delivery = cursor_state in {"pending", "failed"}
            ack_mode_hint = (
                cursor.get("ack_mode_hint")
                if isinstance(cursor.get("ack_mode_hint"), str)
                else None
            )
            if (
                record.execution_status == "received"
                and cursor_state == "pending"
                and cursor_operation
                in {"defer_ephemeral", "defer_public", "defer_component_update"}
                and ack_mode_hint
                in {"defer_ephemeral", "defer_public", "defer_component_update"}
            ):
                has_pending_delivery = False
            if (
                has_pending_delivery
                and ack_mode_hint
                and record.execution_status != "completed"
            ):
                has_pending_delivery = False
            durable_ack_mode = record.ack_mode or ack_mode_hint
            if not durable_ack_mode and not has_pending_delivery:
                await self._mark_interaction_recovery_terminal(
                    record,
                    scheduler_state="delivery_expired",
                    reason="initial_ack_not_durable",
                )
                continue
            should_replay_execution = record.execution_status in {
                "acknowledged",
                "running",
            } or (
                record.execution_status == "received"
                and cursor_state == "pending"
                and ack_mode_hint
                in {"defer_ephemeral", "defer_public", "defer_component_update"}
            )
            if snapshot is not None:
                decision = plan_chat_operation_recovery(
                    snapshot,
                    now=now,
                    max_delivery_attempts=_INTERACTION_RECOVERY_MAX_ATTEMPTS,
                )
                if decision.action == ChatOperationRecoveryAction.MARK_EXPIRED:
                    await self._mark_interaction_recovery_terminal(
                        record,
                        scheduler_state="delivery_expired",
                        reason=decision.reason,
                    )
                    continue
                if decision.action == ChatOperationRecoveryAction.MARK_ABANDONED:
                    await self._mark_interaction_recovery_terminal(
                        record,
                        scheduler_state="abandoned",
                        reason=decision.reason,
                    )
                    continue
                if (
                    decision.action == ChatOperationRecoveryAction.NOOP
                    and not has_pending_delivery
                    and not should_replay_execution
                ):
                    continue
            if has_pending_delivery:
                updated_cursor, terminal_reason = _plan_delivery_recovery_cursor(
                    cursor=cursor if isinstance(cursor, dict) else {},
                    attempt_count=int(record.attempt_count or 0),
                    now=now,
                )
                if terminal_reason is not None:
                    await self._mark_interaction_recovery_terminal(
                        record,
                        scheduler_state="abandoned",
                        reason=terminal_reason,
                    )
                    continue
                if updated_cursor is None:
                    continue
                await self._store.update_interaction_delivery_cursor(
                    record.interaction_id,
                    delivery_cursor_json=updated_cursor,
                    scheduler_state="delivery_replaying",
                    increment_attempt_count=True,
                )
                updated_record = await self._store.get_interaction(
                    record.interaction_id
                )
                await self._patch_chat_operation(
                    record.interaction_id,
                    state=ChatOperationState.DELIVERING,
                    delivery_state="pending",
                    delivery_cursor=updated_cursor,
                    delivery_attempt_count=(
                        int(updated_record.attempt_count or 0)
                        if updated_record is not None
                        else (
                            snapshot.delivery_attempt_count
                            if snapshot is not None
                            else 0
                        )
                    ),
                    validate_transition=False,
                )
                self._command_runner.submit_recovery(
                    envelope.context,
                    record.payload_json,
                    resource_keys=envelope.resource_keys,
                    conversation_id=envelope.conversation_id,
                    replay_mode="delivery_replay",
                )
                continue
            if should_replay_execution:
                if _interaction_recovery_backoff_active(
                    updated_at=record.updated_at,
                    attempt_count=int(record.attempt_count or 0),
                    now=now,
                ):
                    continue
                await self._mark_interaction_scheduler_state(
                    envelope.context,
                    scheduler_state="recovery_scheduled",
                    increment_attempt_count=True,
                )
                self._command_runner.submit_recovery(
                    envelope.context,
                    record.payload_json,
                    resource_keys=envelope.resource_keys,
                    conversation_id=envelope.conversation_id,
                    replay_mode="execution_replay",
                )

    async def _begin_interaction_recovery_execution(self, ctx: IngressContext) -> bool:
        await self._store.mark_interaction_scheduler_state(
            ctx.interaction_id,
            scheduler_state="executing",
        )
        await self._patch_chat_operation(
            ctx.interaction_id,
            state=ChatOperationState.RUNNING,
            validate_transition=False,
        )
        return True

    async def begin_interaction_recovery_execution(self, ctx: IngressContext) -> bool:
        return await self._begin_interaction_recovery_execution(ctx)

    async def _replay_interaction_delivery(self, ctx: IngressContext) -> None:
        record = await self._store.get_interaction(ctx.interaction_id)
        if record is None or not isinstance(record.delivery_cursor_json, dict):
            await self._mark_interaction_recovery_terminal(
                record
                or InteractionLedgerRecord(
                    interaction_id=ctx.interaction_id,
                    interaction_token=ctx.interaction_token,
                    interaction_kind=ctx.kind.value,
                    channel_id=ctx.channel_id,
                    guild_id=ctx.guild_id,
                    user_id=ctx.user_id,
                    metadata_json={},
                ),
                scheduler_state="abandoned",
                reason="missing_delivery_cursor",
                log_level=logging.ERROR,
            )
            return
        replayed = await self._responder.replay_delivery_cursor(
            interaction_id=ctx.interaction_id,
            interaction_token=ctx.interaction_token,
            cursor=record.delivery_cursor_json,
        )
        if not replayed:
            await self._mark_interaction_recovery_terminal(
                record,
                scheduler_state="abandoned",
                reason="unsupported_delivery_cursor",
                log_level=logging.ERROR,
            )

    async def replay_interaction_delivery(self, ctx: IngressContext) -> None:
        await self._replay_interaction_delivery(ctx)

    async def _apply_discord_effect(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        effect: DiscordEffect,
    ) -> None:
        await self._effect_sink.apply(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            effect=effect,
        )

    async def _apply_discord_result(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        result: DiscordHandlerResult,
    ) -> None:
        await self._effect_sink.apply_result(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            result=result,
        )

    async def _run_effectful_handler(
        self,
        handler: Callable[..., Awaitable[object | None]],
        interaction_id: str,
        interaction_token: str,
        *args: object,
        **kwargs: object,
    ) -> object | None:
        if not hasattr(self, "_responder") or not hasattr(self, "_effect_sink"):
            return await handler(
                self,
                *args,
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                **kwargs,
            )
        proxy = DiscordEffectServiceProxy(
            self,
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )
        returned = await handler(
            proxy,
            *args,
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            **kwargs,
        )
        result = proxy.result
        if isinstance(returned, DiscordHandlerResult):
            result.extend(returned.effects)
        await DiscordBotService._apply_discord_result(
            self,
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            result=result,
        )
        return returned

    async def _check_interaction_ingress_duplicate(self, ctx: IngressContext) -> bool:
        reservations = getattr(self, "_ingress_pre_ack_reservations", None)
        if not isinstance(reservations, set):
            reservations = set()
            self._ingress_pre_ack_reservations = reservations
        reservation_lock = getattr(self, "_ingress_pre_ack_reservations_lock", None)
        if reservation_lock is None:
            reservation_lock = asyncio.Lock()
            self._ingress_pre_ack_reservations_lock = reservation_lock
        async with reservation_lock:
            if ctx.interaction_id in reservations:
                return True
            reservations.add(ctx.interaction_id)
        snapshot = None
        store = self._chat_operation_store_or_none()
        if store is not None:
            snapshot = store.get_operation(ctx.interaction_id)
        record = await self._store.get_interaction(ctx.interaction_id)
        if snapshot is None and record is None:
            return False

        has_pending_delivery = bool(
            record is not None
            and isinstance(record.delivery_cursor_json, dict)
            and str(record.delivery_cursor_json.get("state") or "").strip()
            in {"pending", "failed"}
        )
        if snapshot is not None and self._chat_operation_terminal_duplicate(snapshot):
            await self._release_interaction_ingress(ctx.interaction_id)
            return True
        if record is not None and record.scheduler_state in {
            "completed",
            "delivery_expired",
            "abandoned",
        }:
            await self._release_interaction_ingress(ctx.interaction_id)
            return True
        if (
            record is not None
            and record.execution_status
            in {"completed", "failed", "timeout", "cancelled"}
            and not has_pending_delivery
        ):
            await self._release_interaction_ingress(ctx.interaction_id)
            return True
        log_event(
            self._logger,
            logging.INFO,
            "discord.interaction.duplicate_resuming",
            interaction_id=ctx.interaction_id,
            scheduler_state=(record.scheduler_state if record is not None else None),
            execution_status=(record.execution_status if record is not None else None),
            shared_state=snapshot.state.value if snapshot is not None else None,
        )
        return False

    async def _release_interaction_ingress(self, interaction_id: str) -> None:
        reservations = getattr(self, "_ingress_pre_ack_reservations", None)
        if not isinstance(reservations, set):
            return
        reservation_lock = getattr(self, "_ingress_pre_ack_reservations_lock", None)
        if reservation_lock is None:
            reservation_lock = asyncio.Lock()
            self._ingress_pre_ack_reservations_lock = reservation_lock
        async with reservation_lock:
            reservations.discard(interaction_id)

    async def _register_interaction_ingress(self, ctx: IngressContext) -> bool:
        registration = await self._store.register_interaction(
            interaction_id=ctx.interaction_id,
            interaction_token=ctx.interaction_token,
            interaction_kind=ctx.kind.value,
            channel_id=ctx.channel_id,
            guild_id=ctx.guild_id,
            user_id=ctx.user_id,
            metadata_json=self._interaction_ledger_metadata(ctx),
        )
        if registration.inserted:
            return False
        record = registration.record
        snapshot = None
        store = self._chat_operation_store_or_none()
        if store is not None:
            snapshot = store.get_operation(ctx.interaction_id)
        has_pending_delivery = bool(
            isinstance(record.delivery_cursor_json, dict)
            and str(record.delivery_cursor_json.get("state") or "").strip()
            in {"pending", "failed"}
        )
        if snapshot is not None and self._chat_operation_terminal_duplicate(snapshot):
            return True
        if record.scheduler_state in {"completed", "delivery_expired", "abandoned"}:
            return True
        if (
            record.execution_status in {"completed", "failed", "timeout", "cancelled"}
            and not has_pending_delivery
        ):
            return True
        log_event(
            self._logger,
            logging.INFO,
            "discord.interaction.duplicate_resuming",
            interaction_id=ctx.interaction_id,
            scheduler_state=record.scheduler_state,
            execution_status=record.execution_status,
            shared_state=snapshot.state.value if snapshot is not None else None,
        )
        return False

    async def _begin_interaction_execution(self, ctx: IngressContext) -> bool:
        claimed = await self._store.claim_interaction_execution(ctx.interaction_id)
        if claimed:
            await self._patch_chat_operation(
                ctx.interaction_id,
                state=ChatOperationState.RUNNING,
                validate_transition=False,
            )
        return claimed

    async def begin_interaction_execution(self, ctx: IngressContext) -> bool:
        return await self._begin_interaction_execution(ctx)

    async def _finish_interaction_execution(
        self,
        ctx: IngressContext,
        *,
        execution_status: str,
        execution_error: Optional[str] = None,
    ) -> None:
        await self._store.mark_interaction_execution(
            ctx.interaction_id,
            execution_status=execution_status,
            execution_error=execution_error,
        )
        shared_state: Optional[ChatOperationState]
        terminal_outcome = None
        if execution_status == "completed":
            record = await self._store.get_interaction(ctx.interaction_id)
            has_pending_delivery = bool(
                record is not None
                and isinstance(record.delivery_cursor_json, dict)
                and str(record.delivery_cursor_json.get("state") or "").strip()
                in {"pending", "failed"}
            )
            shared_state = (
                ChatOperationState.DELIVERING
                if has_pending_delivery
                else ChatOperationState.COMPLETED
            )
        elif execution_status == "cancelled":
            shared_state = ChatOperationState.CANCELLED
        elif execution_status == "timeout":
            shared_state = ChatOperationState.FAILED
            terminal_outcome = "timeout"
        elif execution_status == "failed":
            shared_state = ChatOperationState.FAILED
        elif execution_status == "running":
            shared_state = ChatOperationState.RUNNING
        else:
            shared_state = None
        await self._patch_chat_operation(
            ctx.interaction_id,
            state=shared_state,
            terminal_outcome=terminal_outcome,
            terminal_detail=execution_error,
            validate_transition=False,
        )

    async def finish_interaction_execution(
        self,
        ctx: IngressContext,
        *,
        execution_status: str,
        execution_error: Optional[str] = None,
    ) -> None:
        await self._finish_interaction_execution(
            ctx,
            execution_status=execution_status,
            execution_error=execution_error,
        )

    def _interaction_ledger_metadata(self, ctx: IngressContext) -> dict[str, Any]:
        command_path = list(ctx.command_spec.path) if ctx.command_spec else None
        command_options = ctx.command_spec.options if ctx.command_spec else None
        return {
            "kind": ctx.kind.value,
            "channel_id": ctx.channel_id,
            "guild_id": ctx.guild_id,
            "user_id": ctx.user_id,
            "command_path": command_path,
            "command_options": command_options,
            "custom_id": ctx.custom_id,
            "values": ctx.values,
            "modal_values": ctx.modal_values,
            "focused_name": ctx.focused_name,
            "focused_value": ctx.focused_value,
            "message_id": ctx.message_id,
        }

    async def _bind_with_path(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        guild_id: Optional[str],
        raw_path: str,
    ) -> None:
        from .workspace_commands import _bind_with_path as _impl

        await self._run_effectful_handler(
            _impl,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            guild_id=guild_id,
            raw_path=raw_path,
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
        await DiscordBotService._run_effectful_handler(
            self,
            handle_status,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            guild_id=guild_id,
            user_id=user_id,
        )

    async def _handle_processes(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
    ) -> None:
        await DiscordBotService._run_effectful_handler(
            self,
            handle_processes,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
        )

    async def _get_active_flow_info(
        self, workspace_path: str
    ) -> Optional[ActiveFlowInfo]:
        from .workspace_commands import _get_active_flow_info as _impl

        return await _impl(self, workspace_path)

    async def _handle_debug(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        guild_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> None:
        await self._run_effectful_handler(
            handle_debug,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            guild_id=guild_id,
            user_id=user_id,
        )

    async def _handle_help(
        self,
        interaction_id: str,
        interaction_token: str,
    ) -> None:
        await self._run_effectful_handler(
            handle_help,
            interaction_id,
            interaction_token,
        )

    async def _handle_ids(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        guild_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> None:
        await self._run_effectful_handler(
            handle_ids,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            guild_id=guild_id,
            user_id=user_id,
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
            except (OSError, ValueError):
                cwd = workspace_root

        session = self._ensure_interaction_session(
            interaction_id,
            interaction_token,
        )
        deferred = session.has_initial_response()
        if not deferred:
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
        except subprocess.SubprocessError as exc:
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
        except subprocess.SubprocessError as exc:
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
        except (OSError, ValueError, TypeError) as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.ticket.write_failed",
                path=str(candidate),
                exc=exc,
            )
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                f"Failed to write ticket: {exc}",
            )
            return

        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"Saved {safe_relpath(candidate, workspace_root)}.",
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
        await self._apply_discord_effect(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            effect=DiscordModalEffect(
                kind=InteractionSessionKind.COMPONENT,
                custom_id=custom_id,
                title=title,
                components=[
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
            ),
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
        except (OSError, RuntimeError, ValueError, TypeError) as exc:
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
        except (OSError, ValueError) as exc:
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
        session = self._ensure_interaction_session(
            interaction_id,
            interaction_token,
        )
        deferred = session.has_initial_response()
        if not deferred:
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

        agent, agent_profile = self._resolve_agent_state(binding)
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
                agent_profile=agent_profile,
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
        except (
            Exception
        ) as exc:  # intentional: top-level error handler for thread reset
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
        actor_label = self._format_agent_state(agent, agent_profile)
        text = format_discord_message(
            "\n".join(
                [
                    *build_fresh_session_started_lines(
                        mode_label=mode_label,
                        actor_label=actor_label,
                        state_label=state_label,
                    ),
                    *build_thread_detail_lines(
                        thread_id=_new_thread_id,
                        workspace_path=str(workspace_root),
                        actor_label=actor_label,
                        model=self._status_model_label(binding),
                        effort=self._status_effort_label(binding, agent),
                    ),
                ]
            )
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
        from .car_handlers.session_commands import handle_car_newt

        await self._run_effectful_handler(
            handle_car_newt,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            guild_id=guild_id,
        )

    async def _handle_car_newt_hard_reset(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        expected_workspace_token: str | None,
    ) -> None:
        from .car_handlers.session_commands import handle_car_newt_hard_reset

        await self._run_effectful_handler(
            handle_car_newt_hard_reset,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            expected_workspace_token=expected_workspace_token,
        )

    async def _handle_car_newt_cancel(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        expected_workspace_token: str | None = None,
    ) -> None:
        from .car_handlers.session_commands import handle_car_newt_cancel

        await self._run_effectful_handler(
            handle_car_newt_cancel,
            interaction_id,
            interaction_token,
            expected_workspace_token=expected_workspace_token,
        )

    async def _handle_car_resume(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        options: dict[str, Any],
    ) -> None:
        from .car_handlers.session_commands import handle_car_resume

        await self._run_effectful_handler(
            handle_car_resume,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            options=options,
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
        from .car_handlers.system_commands import handle_car_update

        await self._run_effectful_handler(
            handle_car_update,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            options=options,
            response_mode=response_mode,
        )

    @staticmethod
    def _update_thread_blocks_restart_warning(thread: Any) -> bool:
        from .car_handlers.system_commands import (
            _update_thread_blocks_restart_warning as update_thread_blocks_restart_warning,
        )

        return update_thread_blocks_restart_warning(thread)

    def _active_update_session_count(self) -> int:
        from .car_handlers.system_commands import (
            _active_update_session_count as active_update_session_count,
        )

        return active_update_session_count(self)

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
        except (OSError, ValueError, RuntimeError):
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
            except (OSError, ValueError):
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
                except (OSError, ValueError, RuntimeError):
                    return None
            if str(getattr(thread, "status", "") or "").strip().lower() == "running":
                return True
        return False

    def _build_update_confirmation_components(
        self,
        *,
        update_target: str,
    ) -> list[dict[str, Any]]:
        from .car_handlers.system_commands import (
            _build_update_confirmation_components as build_update_confirmation_components,
        )

        return build_update_confirmation_components(self, update_target=update_target)

    def _update_status_path(self) -> Path:
        from .car_handlers.system_commands import (
            _update_status_path as update_status_path,
        )

        return update_status_path(self)

    def _format_update_status_message(self, status: Optional[dict[str, Any]]) -> str:
        from .car_handlers.system_commands import (
            _format_update_status_message as fmt_status_msg,
        )

        return fmt_status_msg(self, status)

    def _dynamic_update_target_definitions(self):
        from .car_handlers.system_commands import (
            _dynamic_update_target_definitions as dynamic_update_target_definitions,
        )

        return dynamic_update_target_definitions(self)

    async def _send_update_status_notice(
        self, notify_context: dict[str, Any], text: str
    ) -> None:
        from .car_handlers.system_commands import (
            _send_update_status_notice as send_update_status_notice,
        )

        await send_update_status_notice(self, notify_context, text)

    def _mark_update_notified(self, status: dict[str, Any]) -> None:
        from .car_handlers.system_commands import (
            _mark_update_notified as mark_update_notified,
        )

        mark_update_notified(self, status)

    async def _handle_car_update_status(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        component_response: bool = False,
    ) -> None:
        from .car_handlers.system_commands import handle_car_update_status

        await self._run_effectful_handler(
            handle_car_update_status,
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            component_response=component_response,
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
        from .car_handlers.agent_commands import handle_car_agent

        await self._run_effectful_handler(
            handle_car_agent,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            options=options,
        )

    VALID_REASONING_EFFORTS = REASONING_EFFORT_VALUES

    async def _handle_agent_profile_picker_selection(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        selected_profile: str,
    ) -> None:
        profile_value = selected_profile.strip()
        if not profile_value:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Please select a Hermes profile and try again.",
            )
            return
        profile_option = (
            "clear" if profile_value in {"clear", "reset"} else profile_value
        )
        await self._handle_car_agent(
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            options={"profile": profile_option},
        )

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
        from .car_handlers.agent_commands import handle_car_model

        await self._run_effectful_handler(
            handle_car_model,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            user_id=user_id,
            options=options,
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
        current_agent, _current_profile = self._resolve_agent_state(binding)

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
        except (OSError, ValueError) as exc:
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
        except (ConfigError, OSError, ValueError):
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
            except (OSError, ValueError):
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
                duration_label: Optional[str] = None
                if latest is not None and latest.finished_at:
                    duration_label = format_flow_duration(
                        flow_run_duration_seconds(latest)
                    )
                freshness = None
                if latest is not None:
                    snapshot = build_flow_status_snapshot(
                        entry.repo_root, latest, store
                    )
                    freshness = (
                        snapshot.get("freshness")
                        if isinstance(snapshot, dict)
                        else None
                    )
                line = format_hub_flow_overview_line(
                    line_label=line_label,
                    is_worktree=entry.is_worktree,
                    status=latest.status.value if latest else None,
                    done_count=progress.get("done", 0),
                    total_count=progress.get("total", 0),
                    run_id=latest.id if latest else None,
                    duration_label=duration_label,
                    freshness=cast(Optional[dict[str, Any]], freshness),
                )
            except (OSError, ValueError, RuntimeError, KeyError):
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
        session = self._ensure_interaction_session(
            interaction_id,
            interaction_token,
        )
        deferred = session.is_deferred()
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
            return None
        if bool(binding.get("pma_enabled", False)):
            text = format_discord_message(
                "PMA mode is enabled for this channel. Run `/pma off` to use workspace-scoped `/car` commands."
            )
            await self._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=text,
            )
            return None
        workspace_raw = binding.get("workspace_path")
        if not isinstance(workspace_raw, str) or not workspace_raw.strip():
            text = format_discord_message(
                "Binding is invalid. Run `/car bind path:<...>` first."
            )
            await self._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=text,
            )
            return None
        return canonicalize_path(Path(workspace_raw))

    def _open_flow_store(self, workspace_root: Path) -> FlowStore:
        try:
            config = load_repo_config(workspace_root)
            durable = config.durable_writes
        except ConfigError:
            durable = False
        store = FlowStore(
            workspace_root / ".codex-autorunner" / "flows.db",
            durable=durable,
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

    def _ticket_flow_orchestration_service(self, workspace_root: Path):
        return build_ticket_flow_orchestration_service(workspace_root=workspace_root)

    @staticmethod
    def _close_worker_handles(ensure_result: dict[str, Any]) -> None:
        for key in ("stdout", "stderr"):
            handle = ensure_result.get(key)
            close = getattr(handle, "close", None)
            if callable(close):
                close()

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
        await self._run_effectful_handler(
            handle_flow_status,
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            options=options,
            channel_id=channel_id,
            guild_id=guild_id,
            update_message=update_message,
        )

    async def _handle_flow_runs(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        options: dict[str, Any],
    ) -> None:
        await self._run_effectful_handler(
            handle_flow_runs,
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            options=options,
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
        await self._run_effectful_handler(
            handle_flow_issue,
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            options=options,
            channel_id=channel_id,
            guild_id=guild_id,
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
        await self._run_effectful_handler(
            handle_flow_plan,
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            options=options,
            channel_id=channel_id,
            guild_id=guild_id,
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
        await self._run_effectful_handler(
            handle_flow_start,
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            options=options,
            deferred_public=deferred_public,
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
        await self._run_effectful_handler(
            handle_flow_restart,
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            options=options,
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
        await self._run_effectful_handler(
            handle_flow_recover,
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            options=options,
        )

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
        await self._run_effectful_handler(
            handle_flow_resume,
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            options=options,
            channel_id=channel_id,
            guild_id=guild_id,
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
        await self._run_effectful_handler(
            handle_flow_stop,
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            options=options,
            channel_id=channel_id,
            guild_id=guild_id,
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
        await self._run_effectful_handler(
            handle_flow_archive,
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            options=options,
            channel_id=channel_id,
            guild_id=guild_id,
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
        component_response: bool = False,
    ) -> None:
        await self._run_effectful_handler(
            handle_flow_reply,
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            options=options,
            channel_id=channel_id,
            guild_id=guild_id,
            user_id=user_id,
            component_response=component_response,
        )

    def _write_user_reply(
        self,
        workspace_root: Path,
        record: Any,
        text: str,
    ) -> Path:
        return write_user_reply(self, workspace_root, record, text)

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
        except OSError as exc:
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
        except (DiscordAPIError, OSError, ValueError) as exc:
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
        except OSError as exc:
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
            with contextlib.suppress(OSError, ValueError):
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
        await self._run_effectful_handler(
            handle_pma_on,
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
        await self._run_effectful_handler(
            handle_pma_off,
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
        await self._run_effectful_handler(
            handle_pma_status,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
        )

    async def respond_ephemeral(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
    ) -> None:
        await self._apply_discord_effect(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            effect=DiscordResponseEffect(
                text=text,
                ephemeral=True,
                prefer_followup=False,
            ),
        )

    async def defer_ephemeral(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
    ) -> bool:
        try:
            await self._apply_discord_effect(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                effect=DiscordDeferEffect(mode="ephemeral"),
            )
        except Exception:
            return False
        return True

    async def defer_component_update(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
    ) -> bool:
        session = self._ensure_interaction_session(
            interaction_id,
            interaction_token,
            kind=InteractionSessionKind.COMPONENT,
        )
        if session.has_initial_response():
            return True
        try:
            await self._apply_discord_effect(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                effect=DiscordDeferEffect(mode="component_update"),
            )
        except Exception:
            return False
        return True

    async def send_or_respond_ephemeral(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        deferred: bool,
        text: str,
    ) -> None:
        _ = deferred
        await self._apply_discord_effect(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            effect=DiscordResponseEffect(
                text=text,
                ephemeral=True,
                prefer_followup=True,
            ),
        )

    async def send_or_respond_ephemeral_with_components(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        deferred: bool,
        text: str,
        components: list[dict[str, Any]],
    ) -> None:
        _ = deferred
        await self._apply_discord_effect(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            effect=DiscordResponseEffect(
                text=text,
                ephemeral=True,
                components=components,
                prefer_followup=True,
            ),
        )

    async def send_or_update_component_message(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        deferred: bool,
        text: str,
        components: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        await self._apply_discord_effect(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            effect=DiscordComponentResponseEffect(
                text=truncate_for_discord(
                    text,
                    max_len=max(int(self._config.max_message_length), 32),
                ),
                deferred=deferred,
                components=components,
            ),
        )

    async def respond_ephemeral_with_components(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
        components: list[dict[str, Any]],
    ) -> None:
        await self._apply_discord_effect(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            effect=DiscordResponseEffect(
                text=text,
                ephemeral=True,
                components=components,
                prefer_followup=False,
            ),
        )

    async def respond_autocomplete(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        choices: list[dict[str, str]],
    ) -> None:
        await self._apply_discord_effect(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            effect=DiscordAutocompleteEffect(choices=choices),
        )

    async def respond_modal(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        kind: InteractionSessionKind,
        custom_id: str,
        title: str,
        components: list[dict[str, Any]],
    ) -> None:
        await self._apply_discord_effect(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            effect=DiscordModalEffect(
                kind=kind,
                custom_id=custom_id,
                title=title,
                components=components,
            ),
        )

    async def update_component_message(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        text: str,
        components: list[dict[str, Any]],
    ) -> None:
        await self._apply_discord_effect(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            effect=DiscordComponentUpdateEffect(text=text, components=components),
        )

    async def edit_original_component_message(
        self,
        *,
        interaction_token: str,
        text: str,
        components: Optional[list[dict[str, Any]]] = None,
    ) -> bool:
        session = self._responder.get_session(interaction_token)
        await self._apply_discord_effect(
            interaction_id=session.interaction_id if session is not None else "",
            interaction_token=interaction_token,
            effect=DiscordOriginalMessageEditEffect(text=text, components=components),
        )
        return True

    async def send_followup_ephemeral(
        self,
        *,
        interaction_token: str,
        content: str,
        components: Optional[list[dict[str, Any]]] = None,
    ) -> bool:
        session = self._responder.get_session(interaction_token)
        await self._apply_discord_effect(
            interaction_id=session.interaction_id if session is not None else "",
            interaction_token=interaction_token,
            effect=DiscordFollowupEffect(
                content=content,
                ephemeral=True,
                components=components,
            ),
        )
        return True

    async def respond_public(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
    ) -> None:
        await self._apply_discord_effect(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            effect=DiscordResponseEffect(
                text=text,
                ephemeral=False,
                prefer_followup=False,
            ),
        )

    async def defer_public(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
    ) -> bool:
        try:
            await self._apply_discord_effect(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                effect=DiscordDeferEffect(mode="public"),
            )
        except Exception:
            return False
        return True

    async def send_or_respond_public(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        deferred: bool,
        text: str,
    ) -> None:
        _ = deferred
        await self._apply_discord_effect(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            effect=DiscordResponseEffect(
                text=text,
                ephemeral=False,
                prefer_followup=True,
            ),
        )

    async def send_or_respond_public_with_components(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        deferred: bool,
        text: str,
        components: list[dict[str, Any]],
    ) -> None:
        _ = deferred
        await self._apply_discord_effect(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            effect=DiscordResponseEffect(
                text=text,
                ephemeral=False,
                components=components,
                prefer_followup=True,
            ),
        )

    async def respond_public_with_components(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
        components: list[dict[str, Any]],
    ) -> None:
        await self._apply_discord_effect(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            effect=DiscordResponseEffect(
                text=text,
                ephemeral=False,
                components=components,
                prefer_followup=False,
            ),
        )

    async def send_followup_public(
        self,
        *,
        interaction_token: str,
        content: str,
        components: Optional[list[dict[str, Any]]] = None,
    ) -> bool:
        session = self._responder.get_session(interaction_token)
        await self._apply_discord_effect(
            interaction_id=session.interaction_id if session is not None else "",
            interaction_token=interaction_token,
            effect=DiscordFollowupEffect(
                content=content,
                ephemeral=False,
                components=components,
            ),
        )
        return True

    async def _respond_ephemeral(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
    ) -> None:
        await self.respond_ephemeral(interaction_id, interaction_token, text)

    async def _defer_ephemeral(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
    ) -> bool:
        return await self.defer_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )

    async def _defer_component_update(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
    ) -> bool:
        return await self.defer_component_update(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )

    async def _send_or_respond_ephemeral(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        deferred: bool,
        text: str,
    ) -> None:
        await self.send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=text,
        )

    async def _send_or_respond_with_components_ephemeral(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        deferred: bool,
        text: str,
        components: list[dict[str, Any]],
    ) -> None:
        await self.send_or_respond_ephemeral_with_components(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=text,
            components=components,
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
        await self.send_or_update_component_message(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=text,
            components=components,
        )

    async def _respond_with_components(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
        components: list[dict[str, Any]],
    ) -> None:
        await self.respond_ephemeral_with_components(
            interaction_id,
            interaction_token,
            text,
            components,
        )

    async def _respond_autocomplete(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        choices: list[dict[str, str]],
    ) -> None:
        await self.respond_autocomplete(
            interaction_id,
            interaction_token,
            choices=choices,
        )

    async def _update_component_message(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        text: str,
        components: list[dict[str, Any]],
    ) -> None:
        await self.update_component_message(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            text=text,
            components=components,
        )

    async def _edit_original_component_message(
        self,
        *,
        interaction_token: str,
        text: str,
        components: Optional[list[dict[str, Any]]] = None,
    ) -> bool:
        return await self.edit_original_component_message(
            interaction_token=interaction_token,
            text=text,
            components=components,
        )

    async def _send_followup_ephemeral(
        self,
        *,
        interaction_token: str,
        content: str,
        components: Optional[list[dict[str, Any]]] = None,
    ) -> bool:
        return await self.send_followup_ephemeral(
            interaction_token=interaction_token,
            content=content,
            components=components,
        )

    async def _respond_public(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
    ) -> None:
        await self.respond_public(interaction_id, interaction_token, text)

    async def _defer_public(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
    ) -> bool:
        return await self.defer_public(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
        )

    async def _send_or_respond_public(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        deferred: bool,
        text: str,
    ) -> None:
        await self.send_or_respond_public(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=text,
        )

    async def _send_or_respond_with_components_public(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        deferred: bool,
        text: str,
        components: list[dict[str, Any]],
    ) -> None:
        await self.send_or_respond_public_with_components(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=text,
            components=components,
        )

    async def _respond_with_components_public(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
        components: list[dict[str, Any]],
    ) -> None:
        await self.respond_public_with_components(
            interaction_id,
            interaction_token,
            text,
            components,
        )

    async def _send_followup_public(
        self,
        *,
        interaction_token: str,
        content: str,
        components: Optional[list[dict[str, Any]]] = None,
    ) -> bool:
        return await self.send_followup_public(
            interaction_token=interaction_token,
            content=content,
            components=components,
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
        from .ingress import IngressContext, IngressTiming, InteractionKind

        ctx = IngressContext(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            channel_id=channel_id,
            guild_id=guild_id,
            user_id=user_id,
            kind=InteractionKind.COMPONENT,
            custom_id=custom_id,
            values=values,
            message_id=message_id,
            timing=IngressTiming(),
        )
        await _dispatch_component_interaction(self, ctx)

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
        from .workspace_commands import _bind_to_workspace_candidate as _impl

        await self._run_effectful_handler(
            _impl,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            guild_id=guild_id,
            selected_resource_kind=selected_resource_kind,
            selected_resource_id=selected_resource_id,
            workspace_path=workspace_path,
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
        await self._run_effectful_handler(
            handle_bind_selection,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            guild_id=guild_id,
            selected_workspace_value=selected_workspace_value,
        )

    async def _handle_bind_page_component(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        page_token: str,
    ) -> None:
        await self._run_effectful_handler(
            handle_bind_page_component,
            interaction_id,
            interaction_token,
            page_token=page_token,
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
        await self._run_effectful_handler(
            handle_flow_button,
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            custom_id=custom_id,
            channel_id=channel_id,
            guild_id=guild_id,
        )

    async def _handle_car_reset(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
    ) -> None:
        from .car_handlers.session_commands import handle_car_reset

        await self._run_effectful_handler(
            handle_car_reset,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
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
        from .car_handlers.review_commands import handle_car_review

        await self._run_effectful_handler(
            handle_car_review,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            workspace_root=workspace_root,
            options=options,
        )

    async def _handle_car_approvals(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        options: dict[str, Any],
    ) -> None:
        from .car_handlers.review_commands import handle_car_approvals

        await DiscordBotService._run_effectful_handler(
            self,
            handle_car_approvals,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            options=options,
        )

    async def _handle_car_mention(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        options: dict[str, Any],
    ) -> None:
        from .car_handlers.system_commands import handle_car_mention

        await self._run_effectful_handler(
            handle_car_mention,
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            options=options,
        )

    async def _handle_car_experimental(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        options: dict[str, Any],
    ) -> None:
        from .car_handlers.agent_commands import handle_car_experimental

        await self._run_effectful_handler(
            handle_car_experimental,
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            options=options,
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
        from .car_handlers.compact_commands import handle_car_compact

        await self._run_effectful_handler(
            handle_car_compact,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
        )

    async def _handle_car_rollout(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
    ) -> None:
        from .car_handlers.agent_commands import handle_car_rollout

        await self._run_effectful_handler(
            handle_car_rollout,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
        )

    async def _handle_car_logout(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
    ) -> None:
        from .car_handlers.system_commands import handle_car_logout

        await self._run_effectful_handler(
            handle_car_logout,
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
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
        from .car_handlers.system_commands import handle_car_feedback

        await self._run_effectful_handler(
            handle_car_feedback,
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            options=options,
            channel_id=channel_id,
        )

    async def _handle_car_archive(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
    ) -> None:
        from .car_handlers.session_commands import handle_car_archive

        await self._run_effectful_handler(
            handle_car_archive,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
        )

    async def _handle_car_interrupt(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        active_turn_text: str = "Interrupt succeeded.",
        cancel_queued: bool = True,
        allow_promoted_no_active_success: bool = False,
        thread_target_id: Optional[str] = None,
        execution_id: Optional[str] = None,
        progress_reuse_source_message_id: Optional[str] = None,
        progress_reuse_acknowledgement: Optional[str] = None,
        source: str = "unknown",
        source_custom_id: Optional[str] = None,
        source_message_id: Optional[str] = None,
        source_command: Optional[str] = None,
        source_user_id: Optional[str] = None,
    ) -> None:
        from .car_handlers.session_commands import handle_car_interrupt

        await self._run_effectful_handler(
            handle_car_interrupt,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            active_turn_text=active_turn_text,
            cancel_queued=cancel_queued,
            allow_promoted_no_active_success=allow_promoted_no_active_success,
            thread_target_id=thread_target_id,
            execution_id=execution_id,
            progress_reuse_source_message_id=progress_reuse_source_message_id,
            progress_reuse_acknowledgement=progress_reuse_acknowledgement,
            source=source,
            source_custom_id=source_custom_id,
            source_message_id=source_message_id,
            source_command=source_command,
            source_user_id=source_user_id,
        )

    async def _send_interrupt_component_response(
        self,
        interaction_id: str,
        interaction_token: str,
        text: str,
    ) -> None:
        deferred = await ensure_ephemeral_response_deferred(
            self,
            interaction_id,
            interaction_token,
        )
        await self.send_or_respond_ephemeral(
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
        from .components import parse_cancel_turn_custom_id

        thread_target_id, execution_id = parse_cancel_turn_custom_id(custom_id)
        await defer_and_update_runtime_component_message(
            self,
            interaction_id,
            interaction_token,
            "Stopping current turn...",
            components=[],
        )
        from ..chat.chat_ux_telemetry import ChatUxMilestone, ChatUxTimingSnapshot

        cancel_snapshot = ChatUxTimingSnapshot(
            platform="discord", channel_id=channel_id
        )
        cancel_snapshot.record(ChatUxMilestone.RAW_EVENT_RECEIVED)
        cancel_snapshot.record(ChatUxMilestone.INTERRUPT_REQUESTED_VISIBLE)
        log_event(
            self._logger,
            logging.INFO,
            "discord.turn.cancel_acknowledged",
            channel_id=channel_id,
            thread_target_id=thread_target_id,
            execution_id=execution_id,
            **cancel_snapshot.to_log_fields(),
        )
        await self._handle_car_interrupt(
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            source="component",
            thread_target_id=thread_target_id,
            execution_id=execution_id,
            source_custom_id=custom_id,
            source_message_id=message_id,
            source_user_id=user_id,
        )

    async def _handle_cancel_queued_turn_button(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        custom_id: str,
        message_id: Optional[str] = None,
    ) -> None:
        from .components import parse_cancel_queued_turn_custom_id
        from .message_turns import clear_discord_turn_progress_leases

        execution_id = parse_cancel_queued_turn_custom_id(custom_id)
        if not execution_id:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Queued request is unavailable.",
            )
            return
        binding = await self._store.get_binding(channel_id=channel_id)
        pma_enabled = bool(binding.get("pma_enabled", False)) if binding else False
        mode = "pma" if pma_enabled else "repo"
        orchestration_service, _binding_row, current_thread = (
            self._get_discord_thread_binding(channel_id=channel_id, mode=mode)
        )
        if current_thread is None:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Queued request is unavailable.",
            )
            return
        cancelled = orchestration_service.cancel_queued_execution(
            current_thread.thread_target_id,
            execution_id,
        )
        if not cancelled:
            await self._respond_ephemeral(
                interaction_id,
                interaction_token,
                "Queued request is no longer pending.",
            )
            return
        await defer_and_update_runtime_component_message(
            self,
            interaction_id,
            interaction_token,
            "Cancelling queued request...",
            components=[],
        )
        await clear_discord_turn_progress_leases(
            self,
            managed_thread_id=current_thread.thread_target_id,
            execution_id=execution_id,
        )
        if message_id:
            with contextlib.suppress(
                DiscordAPIError,
                RuntimeError,
                ConnectionError,
                OSError,
                ValueError,
            ):
                await self._rest.edit_channel_message(
                    channel_id=channel_id,
                    message_id=message_id,
                    payload={
                        "content": "Queued request cancelled.",
                        "components": [],
                    },
                )
        await self._respond_ephemeral(
            interaction_id,
            interaction_token,
            "Queued request cancelled.",
        )

    async def _handle_queued_turn_interrupt_send_button(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        custom_id: str,
        user_id: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> None:
        from .components import parse_queued_turn_interrupt_send_custom_id

        execution_id, source_message_id = parse_queued_turn_interrupt_send_custom_id(
            custom_id
        )
        if not execution_id or not source_message_id:
            await self._send_interrupt_component_response(
                interaction_id,
                interaction_token,
                "Queued request is unavailable.",
            )
            return
        binding = await self._store.get_binding(channel_id=channel_id)
        pma_enabled = bool(binding.get("pma_enabled", False)) if binding else False
        mode = "pma" if pma_enabled else "repo"
        orchestration_service, _binding_row, current_thread = (
            self._get_discord_thread_binding(channel_id=channel_id, mode=mode)
        )
        if current_thread is None:
            await self._send_interrupt_component_response(
                interaction_id,
                interaction_token,
                "Queued request is unavailable.",
            )
            return
        promoted = orchestration_service.promote_queued_execution(
            current_thread.thread_target_id,
            execution_id,
        )
        if not promoted:
            await self._send_interrupt_component_response(
                interaction_id,
                interaction_token,
                "Queued request is no longer pending.",
            )
            return
        get_running_execution = getattr(
            orchestration_service,
            "get_running_execution",
            None,
        )
        if callable(get_running_execution):
            running_execution = get_running_execution(current_thread.thread_target_id)
            if running_execution is None:
                await self._send_interrupt_component_response(
                    interaction_id,
                    interaction_token,
                    "Queued request moved to the front.",
                )
                return
        await defer_and_update_runtime_component_message(
            self,
            interaction_id,
            interaction_token,
            "Message received. Switching to it now...",
            components=[],
        )
        await self._handle_car_interrupt(
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            active_turn_text="Message received. Switching to it now...",
            cancel_queued=False,
            allow_promoted_no_active_success=True,
            progress_reuse_source_message_id=source_message_id,
            progress_reuse_acknowledgement="Message received. Switching to it now...",
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
        await defer_and_update_runtime_component_message(
            self,
            interaction_id,
            interaction_token,
            "Cancelling queued request...",
            components=[],
        )
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
            await self._send_interrupt_component_response(
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
            await self._send_interrupt_component_response(
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
            await self._send_interrupt_component_response(
                interaction_id,
                interaction_token,
                "Queued request moved to the front.",
            )
            return
        await defer_and_update_runtime_component_message(
            self,
            interaction_id,
            interaction_token,
            "Message received. Switching to it now...",
            components=[],
        )
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
