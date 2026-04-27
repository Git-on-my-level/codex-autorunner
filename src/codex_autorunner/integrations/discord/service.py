from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
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

from ...agents import registry as agent_registry
from ...agents.opencode.supervisor import OpenCodeSupervisor
from ...agents.opencode.supervisor_protocol import (
    OpenCodeHarnessSupervisorProtocol,
)
from ...agents.registry import AgentDescriptor
from ...bootstrap import seed_repo_files
from ...core.config import (
    ConfigError,
    ensure_hub_config_at,
    find_nearest_hub_config_path,
    load_hub_config,
    load_repo_config,
)
from ...core.config_env import resolve_env_for_root
from ...core.filebox import (
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
)
from ...core.hub_control_plane.handshake_startup import perform_startup_hub_handshake
from ...core.hub_control_plane.service import (
    CONTROL_PLANE_API_VERSION as _CONTROL_PLANE_API_VERSION,
)
from ...core.logging_utils import log_event
from ...core.managed_processes import (
    reap_managed_processes as _reap_managed_processes_core,
)
from ...core.managed_thread_identity import (
    file_chat_discord_key,
    pma_base_key,
)
from ...core.orchestration import (
    ORCHESTRATION_SCHEMA_VERSION,
    ChatOperationRecoveryAction,
    ChatOperationSnapshot,
    ChatOperationState,
    SQLiteChatOperationLedger,
    build_ticket_flow_orchestration_service,
    plan_chat_operation_recovery,
)
from ...core.orchestration.managed_thread_delivery_ledger import (
    SQLiteManagedThreadDeliveryEngine,
)
from ...core.state import now_iso
from ...core.state_roots import resolve_global_state_root
from ...core.update import (
    UpdateInProgressError,  # noqa: F401 - re-exported for test monkeypatching
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
from ...integrations.app_server.env import build_app_server_env
from ...integrations.app_server.event_buffer import AppServerEventBuffer
from ...integrations.app_server.supervisor import WorkspaceAppServerSupervisor
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
from ...integrations.chat.bound_live_progress import (
    bound_chat_progress_delivered_message_id,
    mark_bound_chat_progress_delivered,
    retire_bound_chat_progress_outbox_records,
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
from ...integrations.chat.managed_thread_delivery_worker import (
    ManagedThreadDeliveryWorker,
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
from ...integrations.chat.queue_status import (
    QUEUE_STATUS_ITEM_LIMIT,
    coerce_queue_status_items,
    format_queue_status_text,
)
from ...integrations.chat.run_mirror import ChatRunMirror
from ...integrations.chat.turn_policy import (
    PlainTextTurnContext,
    should_trigger_plain_text_turn,
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
from .adapter import DiscordChatAdapter
from .car_command_dispatch import handle_car_command as dispatch_car_command
from .channel_messaging import (
    _coerce_id as _cm_coerce_id,
)
from .channel_messaging import (
    _first_non_empty_text as _cm_first_non_empty_text,
)
from .channel_messaging import (
    _nested_text as _cm_nested_text,
)
from .channel_messaging import (
    delete_channel_message as _delete_channel_message_impl,
)
from .channel_messaging import (
    handle_discord_outbox_delivery as _handle_discord_outbox_delivery_impl,
)
from .channel_messaging import (
    record_channel_directory_seen as _record_channel_directory_seen_impl,
)
from .channel_messaging import (
    resolve_channel_name as _resolve_channel_name_impl,
)
from .channel_messaging import (
    resolve_guild_name as _resolve_guild_name_impl,
)
from .channel_messaging import (
    send_channel_message as _send_channel_message_impl,
)
from .collaboration_helpers import (
    collaboration_probe_text,
)
from .command_runner import CommandRunner as _CommandRunner
from .command_runner import RunnerConfig as _RunnerConfig
from .components import (
    DISCORD_SELECT_OPTION_MAX_OPTIONS,
    build_ticket_filter_picker,
    build_ticket_picker,
)
from .config import DiscordBotConfig
from .document_browser import (
    handle_contextspace_back_component,
    handle_contextspace_browser,
    handle_contextspace_chunk_component,
    handle_contextspace_page_component,
    handle_contextspace_select_component,
    handle_ticket_back_component,
    handle_ticket_browser,
    handle_ticket_chunk_component,
    handle_ticket_page_component,
    handle_ticket_select_component,
)
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
from .interaction_scheduler import (
    schedule_ingressed_interaction as _schedule_ingressed_interaction,
)
from .interaction_session import (
    DiscordInteractionSession,
    InteractionSessionKind,
)
from .interactions import extract_interaction_id, extract_interaction_token
from .managed_thread_delivery import deliver_discord_managed_thread_record
from .managed_thread_routing import build_discord_thread_orchestration_service
from .managed_thread_startup_recovery import (
    recover_managed_thread_executions_on_startup as _recover_managed_thread_executions_on_startup_impl,
)
from .message_turns import (
    DiscordMessageTurnResult,
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
    format_discord_message,
    truncate_for_discord,
)
from .response_helpers import DiscordResponder
from .rest import DISCORD_INTERACTION_CALLBACK_TIMEOUT_SECONDS, DiscordRestClient
from .service_lifecycle import (
    apply_pending_chat_queue_resets as _apply_pending_chat_queue_resets_impl,
)
from .service_lifecycle import (
    close_all_app_server_supervisors as _close_all_app_server_supervisors_impl,
)
from .service_lifecycle import (
    close_all_opencode_supervisors as _close_all_opencode_supervisors_impl,
)
from .service_lifecycle import (
    is_within_cold_start_window as _is_within_cold_start_window_impl,
)
from .service_lifecycle import (
    on_background_task_done as _on_background_task_done_impl,
)
from .service_lifecycle import (
    reconcile_background_task_failure as _reconcile_background_task_failure_impl,
)
from .service_lifecycle import (
    reconcile_progress_leases_on_startup as _reconcile_progress_leases_on_startup_impl,
)
from .service_lifecycle import (
    run_chat_queue_reset_loop as _run_chat_queue_reset_loop_impl,
)
from .service_lifecycle import (
    run_opencode_prune_loop as _run_opencode_prune_loop_impl,
)
from .service_lifecycle import (
    service_uptime_ms as _service_uptime_ms_impl,
)
from .service_lifecycle import (
    shutdown_service as _shutdown_impl,
)
from .service_lifecycle import (
    sync_application_commands_on_startup as _sync_application_commands_on_startup_impl,
)
from .service_lifecycle import (
    validate_command_sync_config as _validate_command_sync_config_impl,
)
from .service_normalization import (
    DiscordAttachmentAdapter,
    SavedDiscordAttachment,
    build_attachment_context_payload,
    build_discord_approval_message,
    build_discord_queue_status_message,
    format_hub_flow_overview_line,
)
from .state import DiscordStateStore, InteractionLedgerRecord, OutboxRecord
from .update_service import (  # noqa: F401 - re-exported for test monkeypatching
    ChatUpdateStatusNotifier,
    _available_update_target_definitions,
    _format_update_confirmation_warning,
    _normalize_update_ref,
    _normalize_update_target,
    _read_update_status,
    _spawn_update_process,
    _update_target_restarts_surface,
    mark_update_status_notified,
)
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
DISCORD_INTERACTION_COLD_START_WINDOW_SECONDS = 120.0
DISCORD_BACKGROUND_TASK_SHUTDOWN_GRACE_SECONDS = (
    10.0  # noqa: F401 - re-exported for test monkeypatching
)
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


@dataclass(frozen=True)
class DiscordServiceDependencies:
    load_repo_config: Callable[..., Any] = load_repo_config
    resolve_filebox_retention_policy: Callable[..., Any] = (
        resolve_filebox_retention_policy
    )
    prune_filebox_root: Callable[..., Any] = prune_filebox_root
    reap_managed_processes: Callable[[Path], Any] = _reap_managed_processes_core


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
        await _close_all_app_server_supervisors_impl(self._service)


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
        await _close_all_opencode_supervisors_impl(self._service)


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
        dependencies: Optional[DiscordServiceDependencies] = None,
    ) -> None:
        self._config = config
        self._logger = logger
        self._dependencies = dependencies or DiscordServiceDependencies()
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
                edit_message=self._edit_channel_message,
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
        self._document_browser_sessions: dict[str, Any] = {}
        self._responder = DiscordResponder(
            rest=self._rest,
            config=self._config,
            logger=self._logger,
            hydrate_ack_mode=self._load_interaction_ack_mode,
            record_ack=self._record_interaction_ack,
            record_delivery=self._record_interaction_delivery,
            record_delivery_cursor=self._record_interaction_delivery_cursor,
        )
        self._effect_sink = DiscordEffectSink(self._responder)
        self._queue_status_messages: dict[str, tuple[str, str]] = {}
        self._discord_turn_progress_reuse_requests: dict[str, Any] = {}
        self._discord_reusable_progress_messages: dict[str, Any] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._background_shutdown_wait_tasks: set[asyncio.Task[Any]] = set()
        self._background_shutdown_in_progress = False
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
        self._chat_operation_write_lock_guard = asyncio.Lock()
        self._chat_operation_write_locks: dict[str, asyncio.Lock] = {}
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
        self._delivery_worker: Optional[ManagedThreadDeliveryWorker] = None
        self._delivery_worker_task: Optional[asyncio.Task[None]] = None
        self._hub_handshake_retry_window_seconds = (
            DISCORD_HUB_HANDSHAKE_RETRY_WINDOW_SECONDS
        )
        self._hub_handshake_retry_delay_seconds = (
            DISCORD_HUB_HANDSHAKE_RETRY_DELAY_SECONDS
        )
        self._hub_handshake_retry_max_delay_seconds = (
            DISCORD_HUB_HANDSHAKE_RETRY_MAX_DELAY_SECONDS
        )

    def _build_delivery_worker(self) -> ManagedThreadDeliveryWorker:
        service = self

        class _DiscordDeliveryAdapter:
            @property
            def adapter_key(self) -> str:
                return "discord"

            async def deliver_managed_thread_record(
                self, record: Any, *, claim: Any
            ) -> Any:
                return await deliver_discord_managed_thread_record(
                    service,
                    record,
                    claim=claim,
                    channel_id_fallback=None,
                    base_record_label="discord-delivery",
                    error_record_label="discord-delivery-error",
                    default_execution_error="execution error",
                )

        engine = SQLiteManagedThreadDeliveryEngine(self._config.root)
        return ManagedThreadDeliveryWorker(
            engine=engine,
            adapter=_DiscordDeliveryAdapter(),
            logger=self._logger,
        )

    async def run_forever(self) -> None:
        self._service_started_at_monotonic = time.monotonic()
        handshake_ok = await self._perform_hub_handshake()
        if not handshake_ok:
            raise SystemExit(1)
        self._reap_managed_processes(stage="startup")
        await self._store.initialize()
        await _reconcile_progress_leases_on_startup_impl(self)
        await self._resume_interaction_recovery()
        _validate_command_sync_config_impl(self)
        self._outbox.start()
        outbox_task = asyncio.create_task(self._outbox.run_loop())
        await _recover_managed_thread_executions_on_startup_impl(self)
        self._opencode_prune_task = asyncio.create_task(
            _run_opencode_prune_loop_impl(self)
        )
        if self._filebox_housekeeping_enabled():
            self._filebox_prune_task = asyncio.create_task(
                self._run_filebox_prune_loop()
            )
        chat_queue_reset_task = asyncio.create_task(
            _run_chat_queue_reset_loop_impl(self)
        )
        pause_watch_task = asyncio.create_task(self._watch_ticket_flow_pauses())
        terminal_watch_task = asyncio.create_task(self._watch_ticket_flow_terminals())
        dispatcher_loop_task = asyncio.create_task(self._run_dispatcher_loop())
        self._spawn_task(
            self._run_startup_command_sync_background(),
            await_on_shutdown=True,
        )
        self._delivery_worker = self._build_delivery_worker()
        self._delivery_worker_task = asyncio.create_task(
            self._delivery_worker.run_loop()
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
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.update.notify_failed",
                    exc=exc,
                )
            await self._gateway.run(self._on_dispatch)
        finally:
            await self._command_runner.shutdown()
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
            if self._delivery_worker_task is not None:
                self._delivery_worker_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._delivery_worker_task
                self._delivery_worker_task = None
            await _shutdown_impl(self)

    def _service_uptime_ms(self, *, now: Optional[float] = None) -> Optional[float]:
        return _service_uptime_ms_impl(self, now=now)

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
        return _is_within_cold_start_window_impl(self, now=now)

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
                if isinstance(queued_event, ChatMessageEvent):
                    await self._refresh_queue_status_message(
                        conversation_id=context.conversation_id,
                        channel_id=context.chat_id,
                    )
                await self._handle_chat_event(queued_event, context)
            finally:
                if isinstance(queued_event, ChatMessageEvent):
                    await self._refresh_queue_status_message(
                        conversation_id=context.conversation_id,
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
        seen_paths = {workspace_path for _kind, _id, workspace_path in cheap_candidates}
        cheap_candidates.extend(
            (
                "agent_workspace",
                workspace_id,
                workspace_path,
            )
            for workspace_id, workspace_path, _display_name in self._list_agent_workspaces_from_cache()
        )
        seen_paths.update(
            workspace_path for _kind, _id, workspace_path in cheap_candidates
        )
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
                cheap_candidates.append((None, None, normalized_path))
        except OSError:
            self._logger.debug(
                "failed to scan root directory for scheduler bind candidates",
                exc_info=True,
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

    async def _clear_queue_status_message(
        self,
        *,
        conversation_id: str,
        channel_id: Optional[str] = None,
    ) -> None:
        entry = self._queue_status_messages.pop(conversation_id, None)
        if not isinstance(entry, tuple):
            return
        stored_channel_id, message_id = entry
        resolved_channel_id = channel_id or stored_channel_id
        if not resolved_channel_id or not message_id:
            return
        await self._delete_channel_message_safe(
            resolved_channel_id,
            message_id,
            record_id=f"queue-status-delete:{resolved_channel_id}:{conversation_id}",
        )

    async def _queued_notice_config_for_conversation(
        self, conversation_id: str, queue_status: Optional[dict[str, Any]] = None
    ) -> tuple[Optional[str], bool]:
        describe_busy = getattr(self._command_runner, "describe_busy", None)
        if not callable(describe_busy):
            return None, True
        command_label = describe_busy(conversation_id)
        if not isinstance(command_label, str) or not command_label.strip():
            return None, True
        if queue_status is None:
            queue_status = await self._dispatcher.queue_status(conversation_id)
        has_active_message_turn = bool(
            queue_status.get("active") if isinstance(queue_status, dict) else False
        )
        return (
            command_label,
            has_active_message_turn,
        )

    async def _refresh_queue_status_message(
        self,
        *,
        conversation_id: str,
        channel_id: str,
        repost: bool = False,
    ) -> None:
        queue_status = await self._dispatcher.queue_status(conversation_id)
        pending_items_raw = []
        if isinstance(queue_status, dict):
            pending_items_raw = list(queue_status.get("pending_items") or [])
        pending_items = coerce_queue_status_items(
            pending_items_raw,
            fallback_prefix="Request",
        )
        if not pending_items:
            await self._clear_queue_status_message(
                conversation_id=conversation_id,
                channel_id=channel_id,
            )
            return
        busy_label, allow_interrupt = await self._queued_notice_config_for_conversation(
            conversation_id,
            queue_status=queue_status if isinstance(queue_status, dict) else None,
        )
        payload = build_discord_queue_status_message(
            queued_items=[
                (index, item.item_id)
                for index, item in enumerate(
                    pending_items[:QUEUE_STATUS_ITEM_LIMIT],
                    start=1,
                )
            ],
            content=format_queue_status_text(
                pending_items,
                busy_label=busy_label,
            ),
            allow_interrupt=allow_interrupt,
        ).to_payload()
        existing_entry = self._queue_status_messages.get(conversation_id)
        if isinstance(existing_entry, tuple) and not repost:
            existing_channel_id, existing_message_id = existing_entry
            try:
                await self._rest.edit_channel_message(
                    channel_id=existing_channel_id,
                    message_id=existing_message_id,
                    payload=payload,
                )
            except (DiscordAPIError, OSError, TypeError, ValueError):
                pass
            else:
                self._queue_status_messages[conversation_id] = (
                    existing_channel_id,
                    existing_message_id,
                )
                return
        try:
            response = await self._send_channel_message(channel_id, payload)
        except (DiscordAPIError, OSError, TypeError, ValueError):
            await self._send_channel_message_safe(
                channel_id,
                payload,
                record_id=f"queue-status:{channel_id}:{conversation_id}",
            )
            return
        notice_message_id = response.get("id")
        if not isinstance(notice_message_id, str) or not notice_message_id:
            return
        self._queue_status_messages[conversation_id] = (
            channel_id,
            notice_message_id,
        )
        if (
            isinstance(existing_entry, tuple)
            and existing_entry[1] != notice_message_id
            and existing_entry[0]
        ):
            await self._delete_channel_message_safe(
                existing_entry[0],
                existing_entry[1],
                record_id=(
                    f"queue-status-refresh-delete:{existing_entry[0]}:{conversation_id}"
                ),
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
        await self._refresh_queue_status_message(
            conversation_id=dispatch_result.context.conversation_id,
            channel_id=channel_id,
        )
        log_event(
            self._logger,
            logging.INFO,
            "discord.turn.queue_status",
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
            cleanup = self._dependencies.reap_managed_processes(self._config.root)
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
            repo_config = self._dependencies.load_repo_config(
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
        from .service_lifecycle import (
            next_opencode_prune_interval_seconds as _next_interval_impl,
        )

        return await _next_interval_impl(self)

    async def _run_opencode_prune_loop(self) -> None:
        await _run_opencode_prune_loop_impl(self)

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
                repo_config = self._dependencies.load_repo_config(
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
                    self._dependencies.prune_filebox_root,
                    root,
                    policy=self._dependencies.resolve_filebox_retention_policy(
                        repo_config.pma
                    ),
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
        suppress_managed_thread_delivery: bool = False,
        supervision: Optional[Any] = None,
        existing_session_prompt_text: Optional[str] = None,
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
                    suppress_managed_thread_delivery=suppress_managed_thread_delivery,
                    supervision=supervision,
                    existing_session_prompt_text=existing_session_prompt_text,
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
                suppress_managed_thread_delivery=suppress_managed_thread_delivery,
                max_actions=DISCORD_TURN_PROGRESS_MAX_ACTIONS,
                min_edit_interval_seconds=DISCORD_TURN_PROGRESS_MIN_EDIT_INTERVAL_SECONDS,
                heartbeat_interval_seconds=DISCORD_TURN_PROGRESS_HEARTBEAT_INTERVAL_SECONDS,
                log_event_fn=log_event,
                chat_ux_snapshot=chat_ux_snapshot,
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

    async def _handle_bang_shell(
        self,
        *,
        channel_id: str,
        message_id: str,
        text: str,
        workspace_root: Path,
    ) -> None:
        from .car_handlers.shell_commands import handle_bang_shell

        await handle_bang_shell(
            self,
            channel_id=channel_id,
            message_id=message_id,
            text=text,
            workspace_root=workspace_root,
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
        from .pma_commands import handle_pma_command_from_normalized

        await handle_pma_command_from_normalized(
            self,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            guild_id=guild_id,
            command_path=command_path,
            options=options,
        )

    def _validate_command_sync_config(self) -> None:
        _validate_command_sync_config_impl(self)

    async def _sync_application_commands_on_startup(self) -> None:
        await _sync_application_commands_on_startup_impl(self)

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
        except Exception as exc:
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
        await _shutdown_impl(self)

    async def _close_all_app_server_supervisors(self) -> None:
        await _close_all_app_server_supervisors_impl(self)

    async def _close_all_opencode_supervisors(self) -> None:
        await _close_all_opencode_supervisors_impl(self)

    async def _watch_ticket_flow_pauses(self) -> None:
        await watch_ticket_flow_pauses(self)

    async def _scan_and_enqueue_pause_notifications(self) -> None:
        await _scan_and_enqueue_pause_notifications_impl(self)

    async def _run_chat_queue_reset_loop(self) -> None:
        await _run_chat_queue_reset_loop_impl(self)

    async def _apply_pending_chat_queue_resets(self) -> None:
        await _apply_pending_chat_queue_resets_impl(self)

    async def _watch_ticket_flow_terminals(self) -> None:
        await watch_ticket_flow_terminals(self)

    async def _send_channel_message(
        self, channel_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await _send_channel_message_impl(
            self._rest, self._logger, channel_id, payload
        )

    async def _edit_channel_message(
        self,
        channel_id: str,
        message_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._rest.edit_channel_message(
            channel_id=channel_id,
            message_id=message_id,
            payload=payload,
        )

    async def _delete_channel_message(self, channel_id: str, message_id: str) -> None:
        await _delete_channel_message_impl(self._rest, channel_id, message_id)

    async def _send_channel_message_safe(
        self,
        channel_id: str,
        payload: dict[str, Any],
        *,
        record_id: Optional[str] = None,
    ) -> bool:
        from .channel_messaging import send_channel_message_safe as _impl

        return await _impl(
            self._store,
            self._rest,
            self._logger,
            self._send_channel_message,
            channel_id,
            payload,
            record_id=record_id,
        )

    async def _handle_discord_outbox_delivery(
        self, record: OutboxRecord, delivered_message_id: Optional[str]
    ) -> None:
        await _handle_discord_outbox_delivery_impl(
            self._hub_client, self._logger, record, delivered_message_id
        )
        delivered_id = (
            delivered_message_id.strip()
            if isinstance(delivered_message_id, str)
            else ""
        )
        if (
            record.operation == "send"
            and record.record_id.startswith("managed-thread-progress:")
            and delivered_id
        ):
            mark_bound_chat_progress_delivered(
                hub_root=self._config.root,
                delivery_record_id=record.record_id,
                delivered_message_id=delivered_id,
            )
        cleanup_payload = (
            record.payload_json.get("_codex_autorunner_cleanup")
            if isinstance(record.payload_json, dict)
            else None
        )
        if not delivered_id:
            if (
                record.operation == "send"
                and isinstance(cleanup_payload, dict)
                and cleanup_payload.get("kind") == "managed_thread_live_progress"
            ):
                progress_send_record_id = (
                    await retire_bound_chat_progress_outbox_records(
                        store=self._store,
                        surface_kind="discord",
                        surface_key=str(
                            cleanup_payload.get("surface_key") or ""
                        ).strip(),
                        managed_thread_id=str(
                            cleanup_payload.get("managed_thread_id") or ""
                        ).strip(),
                        managed_turn_id=str(
                            cleanup_payload.get("managed_turn_id") or ""
                        ).strip(),
                    )
                )
                progress_message_id = (
                    bound_chat_progress_delivered_message_id(
                        hub_root=self._config.root,
                        delivery_record_id=progress_send_record_id,
                    )
                    or ""
                )
                if progress_message_id:
                    try:
                        await self._store.enqueue_outbox(
                            OutboxRecord(
                                record_id=(
                                    "managed-thread-progress-failure:"
                                    f"{progress_send_record_id}"
                                ),
                                channel_id=record.channel_id,
                                message_id=progress_message_id,
                                operation="edit",
                                payload_json={
                                    "content": format_discord_message(
                                        "Status: this turn finished, but Discord "
                                        "failed before the final reply was "
                                        "delivered. Please retry if needed."
                                    ),
                                    "components": [],
                                },
                                created_at=now_iso(),
                            )
                        )
                    except (OSError, RuntimeError):
                        log_event(
                            self._logger,
                            logging.WARNING,
                            "discord.outbox.progress_failure_cleanup_failed",
                            record_id=record.record_id,
                            progress_message_id=progress_message_id,
                        )
            return
        if not isinstance(cleanup_payload, dict):
            return
        if cleanup_payload.get("kind") != "managed_thread_live_progress":
            return
        managed_thread_id = str(cleanup_payload.get("managed_thread_id") or "").strip()
        managed_turn_id = str(cleanup_payload.get("managed_turn_id") or "").strip()
        surface_key = str(cleanup_payload.get("surface_key") or "").strip()
        if not managed_thread_id or not managed_turn_id or not surface_key:
            return
        progress_send_record_id = await retire_bound_chat_progress_outbox_records(
            store=self._store,
            surface_kind="discord",
            surface_key=surface_key,
            managed_thread_id=managed_thread_id,
            managed_turn_id=managed_turn_id,
        )
        progress_message_id = (
            bound_chat_progress_delivered_message_id(
                hub_root=self._config.root,
                delivery_record_id=progress_send_record_id,
            )
            or ""
        )
        if progress_message_id:
            await self._delete_channel_message_safe(
                surface_key,
                progress_message_id,
                record_id=(
                    "discord:managed-thread-progress-cleanup:"
                    f"{progress_send_record_id}"
                ),
            )
            return
        await self._store.mark_outbox_delivered(progress_send_record_id)

    async def _delete_channel_message_safe(
        self,
        channel_id: str,
        message_id: str,
        *,
        record_id: Optional[str] = None,
    ) -> bool:
        from .channel_messaging import delete_channel_message_safe as _impl

        return await _impl(
            self._store,
            self._delete_channel_message,
            self._logger,
            channel_id,
            message_id,
            record_id=record_id,
        )

    def _spawn_task(
        self, coro: Awaitable[None], *, await_on_shutdown: bool = False
    ) -> asyncio.Task[Any]:
        task = cast(asyncio.Task[Any], asyncio.ensure_future(coro))
        self._background_tasks.add(task)
        if await_on_shutdown:
            self._background_shutdown_wait_tasks.add(task)
        task.add_done_callback(lambda t: _on_background_task_done_impl(self, t))
        return task

    async def _reconcile_discord_progress_leases_on_startup(self) -> None:
        await _reconcile_progress_leases_on_startup_impl(self)

    async def _reconcile_background_task_failure(
        self,
        task_context: dict[str, Any],
        *,
        allow_channel_fallback: bool = False,
    ) -> int:
        return await _reconcile_background_task_failure_impl(
            self, task_context, allow_channel_fallback=allow_channel_fallback
        )

    def _on_background_task_done(self, task: asyncio.Task[Any]) -> None:
        _on_background_task_done_impl(self, task)

    async def _on_dispatch(self, event_type: str, payload: dict[str, Any]) -> None:
        if event_type == "INTERACTION_CREATE":
            await self._handle_interaction_create(payload)
            return
        if event_type == "MESSAGE_CREATE":
            self._spawn_task(
                self._record_channel_directory_seen_from_message_payload(payload)
            )
            message_event = self._chat_adapter.parse_message_event(payload)
            if message_event is not None:
                self._command_runner.submit_event(message_event)

    async def _handle_interaction_create(self, payload: dict[str, Any]) -> None:
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
        schedule_ctx: IngressContext | None = ingress_result.context
        if schedule_ctx is None:
            return
        submitted_to_runner = False
        try:
            result = await _schedule_ingressed_interaction(
                self,
                schedule_ctx,
                payload,
                submission_order=submission_order,
                dispatch_started_at=dispatch_started_at,
            )
            submitted_to_runner = result.submitted
        finally:
            if not submitted_to_runner:
                self._command_runner.skip_submission_order(submission_order)
            await self._release_interaction_ingress(schedule_ctx.interaction_id)

    async def _record_channel_directory_seen_from_message_payload(
        self, payload: dict[str, Any]
    ) -> None:
        await _record_channel_directory_seen_impl(self, payload)

    async def _resolve_channel_name(self, channel_id: str) -> Optional[str]:
        return await _resolve_channel_name_impl(self, channel_id)

    async def _resolve_guild_name(self, guild_id: str) -> Optional[str]:
        return await _resolve_guild_name_impl(self, guild_id)

    @staticmethod
    def _nested_text(payload: dict[str, Any], key: str, field: str) -> Optional[str]:
        return _cm_nested_text(payload, key, field)

    @staticmethod
    def _first_non_empty_text(*values: Any) -> Optional[str]:
        return _cm_first_non_empty_text(*values)

    @staticmethod
    def _coerce_id(value: Any) -> Optional[str]:
        return _cm_coerce_id(value)

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
        from .ticket_commands import handle_ticket_filter_component

        await handle_ticket_filter_component(
            self,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            values=values,
        )

    async def _handle_ticket_select_component(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        user_id: Optional[str],
        values: Optional[list[str]],
    ) -> None:
        await handle_ticket_select_component(
            self,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            user_id=user_id,
            values=values,
        )

    async def _handle_ticket_browser_page_component(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        user_id: Optional[str],
        custom_id: str,
    ) -> None:
        await handle_ticket_page_component(
            self,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            user_id=user_id,
            custom_id=custom_id,
        )

    async def _handle_ticket_browser_back_component(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        user_id: Optional[str],
    ) -> None:
        await handle_ticket_back_component(
            self,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            user_id=user_id,
        )

    async def _handle_ticket_browser_chunk_component(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        user_id: Optional[str],
        custom_id: str,
    ) -> None:
        await handle_ticket_chunk_component(
            self,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            user_id=user_id,
            custom_id=custom_id,
        )

    async def _handle_contextspace_select_component(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        user_id: Optional[str],
        values: Optional[list[str]],
    ) -> None:
        await handle_contextspace_select_component(
            self,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            user_id=user_id,
            values=values,
        )

    async def _handle_contextspace_browser_page_component(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        user_id: Optional[str],
        custom_id: str,
    ) -> None:
        await handle_contextspace_page_component(
            self,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            user_id=user_id,
            custom_id=custom_id,
        )

    async def _handle_contextspace_back_component(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        user_id: Optional[str],
    ) -> None:
        await handle_contextspace_back_component(
            self,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            user_id=user_id,
        )

    async def _handle_contextspace_chunk_component(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        user_id: Optional[str],
        custom_id: str,
    ) -> None:
        await handle_contextspace_chunk_component(
            self,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            user_id=user_id,
            custom_id=custom_id,
        )

    async def _open_ticket_modal(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        ticket_rel: str,
    ) -> None:
        from .ticket_commands import _open_ticket_modal

        await _open_ticket_modal(
            self,
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            ticket_rel=ticket_rel,
        )

    async def _handle_tickets(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        user_id: Optional[str],
        workspace_root: Path,
        options: dict[str, Any],
    ) -> None:
        await self._run_effectful_handler(
            handle_ticket_browser,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            user_id=user_id,
            workspace_root=workspace_root,
            options=options,
        )

    async def _handle_contextspace(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
        user_id: Optional[str],
        workspace_root: Path,
    ) -> None:
        await self._run_effectful_handler(
            handle_contextspace_browser,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            user_id=user_id,
            workspace_root=workspace_root,
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

    def _ensure_chat_operation_write_lock_state(self) -> None:
        if getattr(self, "_chat_operation_write_lock_guard", None) is None:
            self._chat_operation_write_lock_guard = asyncio.Lock()
        if getattr(self, "_chat_operation_write_locks", None) is None:
            self._chat_operation_write_locks = {}

    @contextlib.asynccontextmanager
    async def _chat_operation_write_guard(self, operation_id: str):
        """Serialize ledger read/write for one interaction across asyncio tasks.

        ``SQLiteChatOperationLedger.patch_operation`` is read-modify-write; concurrent
        ``asyncio.to_thread`` calls could otherwise apply stale snapshots after offloading.
        """
        self._ensure_chat_operation_write_lock_state()
        normalized = str(operation_id or "").strip()
        async with self._chat_operation_write_lock_guard:
            lock = self._chat_operation_write_locks.get(normalized)
            if lock is None:
                lock = asyncio.Lock()
                self._chat_operation_write_locks[normalized] = lock
        async with lock:
            yield

    async def _chat_operation_get(
        self, operation_id: str
    ) -> Optional[ChatOperationSnapshot]:
        store = self._chat_operation_store_or_none()
        if store is None:
            return None
        # Keep Discord ingress callbacks non-blocking on SQLite lock contention.
        async with self._chat_operation_write_guard(operation_id):
            return await asyncio.to_thread(store.get_operation, operation_id)

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

        interaction_id = ctx.interaction_id
        metadata = self._interaction_ledger_metadata(ctx)

        def _register_sync() -> Optional[ChatOperationSnapshot]:
            registration = store.register_operation(
                operation_id=interaction_id,
                surface_kind="discord",
                surface_operation_key=interaction_id,
                state=ChatOperationState.RECEIVED,
                conversation_id=conversation_id,
                metadata=metadata,
            )
            store.patch_operation(
                registration.snapshot.operation_id,
                ack_requested_at=now_iso(),
            )
            if registration.inserted:
                return registration.snapshot
            return store.patch_operation(
                interaction_id,
                conversation_id=(
                    conversation_id or registration.snapshot.conversation_id
                ),
                metadata_updates=metadata,
            )

        async with self._chat_operation_write_guard(interaction_id):
            return await asyncio.to_thread(_register_sync)

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

        def _patch_sync() -> Optional[ChatOperationSnapshot]:
            changes_local = dict(changes)
            try:
                return store.patch_operation(
                    interaction_id,
                    state=patch_state,
                    validate_transition=validate_transition,
                    metadata_updates=metadata_updates,
                    **changes_local,
                )
            except ValueError as exc:
                current = store.get_operation(interaction_id)
                log_event(
                    self._logger,
                    logging.ERROR,
                    "discord.chat_operation.invalid_transition_rejected",
                    interaction_id=interaction_id,
                    current_state=(
                        current.state.value if current is not None else None
                    ),
                    requested_state=(
                        state.value if isinstance(state, ChatOperationState) else None
                    ),
                    error=str(exc),
                )
                return None

        async with self._chat_operation_write_guard(interaction_id):
            return await asyncio.to_thread(_patch_sync)

    async def _patch_chat_operation_recovery(
        self,
        interaction_id: str,
        *,
        state: Optional[ChatOperationState] = None,
        metadata_updates: Optional[Mapping[str, Any]] = None,
        **changes: Any,
    ) -> Optional[ChatOperationSnapshot]:
        return await self._patch_chat_operation(
            interaction_id,
            state=state,
            validate_transition=False,
            metadata_updates=metadata_updates,
            **changes,
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
        clear_delivery_failure_marker = False
        if isinstance(cursor, dict):
            delivery_state = str(cursor.get("state") or "").strip() or None
            if delivery_state in {"pending", "failed"}:
                shared_state = ChatOperationState.DELIVERING
            elif delivery_state == "completed":
                record = await self._store.get_interaction(interaction_id)
                if record is not None and record.execution_status == "completed":
                    shared_state = ChatOperationState.COMPLETED
            if delivery_state in {"pending", "completed"}:
                clear_delivery_failure_marker = True
            record = await self._store.get_interaction(interaction_id)
            if record is not None:
                delivery_attempt_count = int(record.attempt_count or 0)
        elif cursor is None:
            record = await self._store.get_interaction(interaction_id)
            if record is not None and record.execution_status == "completed":
                shared_state = ChatOperationState.COMPLETED
            delivery_state = "delivered"
            clear_delivery_failure_marker = True
        patch_kwargs: dict[str, Any] = {
            "state": shared_state,
            "delivery_state": delivery_state,
            "delivery_cursor": cursor,
            "delivery_attempt_count": delivery_attempt_count,
        }
        if clear_delivery_failure_marker:
            patch_kwargs["terminal_outcome"] = None
            patch_kwargs["terminal_detail"] = None
        await self._patch_chat_operation(
            interaction_id,
            **patch_kwargs,
        )

    async def _mark_interaction_scheduler_state(
        self,
        ctx: IngressContext,
        *,
        scheduler_state: str,
        increment_attempt_count: bool = False,
        _for_recovery: bool = False,
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
        patch_fn = (
            self._patch_chat_operation_recovery
            if _for_recovery
            else self._patch_chat_operation
        )
        await patch_fn(
            ctx.interaction_id,
            state=self._discord_chat_operation_state_for_scheduler(scheduler_state),
            delivery_attempt_count=(
                int(record.attempt_count or 0) if record is not None else None
            ),
            terminal_outcome=terminal_outcome,
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
        recovery_event = (
            "discord.interaction.recovery.delivery_expired"
            if scheduler_state == "delivery_expired"
            else "discord.interaction.recovery.abandoned"
        )
        await self._store.mark_interaction_scheduler_state(
            record.interaction_id,
            scheduler_state=scheduler_state,
        )
        await self._patch_chat_operation_recovery(
            record.interaction_id,
            state=self._discord_chat_operation_state_for_scheduler(scheduler_state),
            terminal_outcome=(
                "abandoned"
                if scheduler_state == "abandoned"
                else ("expired" if scheduler_state == "delivery_expired" else None)
            ),
            terminal_detail=reason,
        )
        log_event(
            self._logger,
            log_level,
            recovery_event,
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
            if envelope is None:
                await self._mark_interaction_recovery_terminal(
                    record,
                    scheduler_state="abandoned",
                    reason="missing_runtime_envelope",
                    log_level=logging.ERROR,
                )
                continue
            if record.payload_json is None:
                await self._mark_interaction_recovery_terminal(
                    record,
                    scheduler_state="abandoned",
                    reason="missing_runtime_payload",
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

            if snapshot is None:
                await self._mark_interaction_recovery_terminal(
                    record,
                    scheduler_state="abandoned",
                    reason="missing_shared_ledger_snapshot",
                    log_level=logging.WARNING,
                )
                continue

            decision = plan_chat_operation_recovery(
                snapshot,
                now=now,
                max_delivery_attempts=_INTERACTION_RECOVERY_MAX_ATTEMPTS,
            )
            log_event(
                self._logger,
                logging.DEBUG,
                "discord.interaction.recovery.decision",
                interaction_id=record.interaction_id,
                action=decision.action.value,
                reason=decision.reason,
                previous_state=decision.previous_state.value,
                delivery_pending=decision.delivery_pending,
                execution_replayable=decision.execution_replayable,
                attempt_count=decision.attempt_count,
            )

            if decision.action == ChatOperationRecoveryAction.NOOP:
                continue

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

            if decision.action == ChatOperationRecoveryAction.REPLAY_DELIVERY:
                cursor = record.delivery_cursor_json or {}
                cursor_operation = (
                    str(cursor.get("operation") or "").strip()
                    if isinstance(cursor, dict)
                    else ""
                )
                if cursor_operation in {
                    "defer_ephemeral",
                    "defer_public",
                    "defer_component_update",
                }:
                    if _interaction_recovery_backoff_active(
                        updated_at=record.updated_at,
                        attempt_count=int(record.attempt_count or 0),
                        now=now,
                    ):
                        log_event(
                            self._logger,
                            logging.DEBUG,
                            "discord.interaction.recovery.execution_backoff",
                            interaction_id=record.interaction_id,
                            attempt_count=int(record.attempt_count or 0),
                        )
                        continue
                    await self._mark_interaction_scheduler_state(
                        envelope.context,
                        scheduler_state="recovery_scheduled",
                        increment_attempt_count=True,
                        _for_recovery=True,
                    )
                    log_event(
                        self._logger,
                        logging.INFO,
                        "discord.interaction.recovery.execution_replay_submitted",
                        interaction_id=record.interaction_id,
                        attempt_count=int(record.attempt_count or 0),
                        previous_state=decision.previous_state.value,
                        reason="defer_delivery_projected_to_execution_replay",
                    )
                    self._command_runner.submit_recovery(
                        envelope.context,
                        record.payload_json,
                        resource_keys=envelope.resource_keys,
                        conversation_id=envelope.conversation_id,
                        replay_mode="execution_replay",
                    )
                    continue
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
                    log_event(
                        self._logger,
                        logging.DEBUG,
                        "discord.interaction.recovery.delivery_cursor_backoff",
                        interaction_id=record.interaction_id,
                        attempt_count=int(record.attempt_count or 0),
                    )
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
                await self._patch_chat_operation_recovery(
                    record.interaction_id,
                    state=ChatOperationState.DELIVERING,
                    delivery_state="pending",
                    delivery_cursor=updated_cursor,
                    delivery_attempt_count=(
                        int(updated_record.attempt_count or 0)
                        if updated_record is not None
                        else decision.attempt_count
                    ),
                )
                log_event(
                    self._logger,
                    logging.INFO,
                    "discord.interaction.recovery.delivery_replay_submitted",
                    interaction_id=record.interaction_id,
                    attempt_count=decision.attempt_count,
                    previous_state=decision.previous_state.value,
                    reason=decision.reason,
                )
                self._command_runner.submit_recovery(
                    envelope.context,
                    record.payload_json,
                    resource_keys=envelope.resource_keys,
                    conversation_id=envelope.conversation_id,
                    replay_mode="delivery_replay",
                )
                continue

            if decision.action == ChatOperationRecoveryAction.RESUME_EXECUTION:
                if _interaction_recovery_backoff_active(
                    updated_at=record.updated_at,
                    attempt_count=int(record.attempt_count or 0),
                    now=now,
                ):
                    log_event(
                        self._logger,
                        logging.DEBUG,
                        "discord.interaction.recovery.execution_backoff",
                        interaction_id=record.interaction_id,
                        attempt_count=int(record.attempt_count or 0),
                    )
                    continue
                await self._mark_interaction_scheduler_state(
                    envelope.context,
                    scheduler_state="recovery_scheduled",
                    increment_attempt_count=True,
                    _for_recovery=True,
                )
                log_event(
                    self._logger,
                    logging.INFO,
                    "discord.interaction.recovery.execution_replay_submitted",
                    interaction_id=record.interaction_id,
                    attempt_count=int(record.attempt_count or 0),
                    previous_state=decision.previous_state.value,
                    reason=decision.reason,
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
        await self._patch_chat_operation_recovery(
            ctx.interaction_id,
            state=ChatOperationState.RUNNING,
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
        snapshot = await self._chat_operation_get(ctx.interaction_id)
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
        snapshot = await self._chat_operation_get(ctx.interaction_id)
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
        from .workspace_commands import handle_diff

        await handle_diff(
            self,
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            options=options,
        )

    async def _handle_skills(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        options: dict[str, Any],
    ) -> None:
        from .workspace_commands import handle_skills

        await handle_skills(
            self,
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            options=options,
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
        from .ticket_commands import handle_ticket_modal_submit

        await handle_ticket_modal_submit(
            self,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            custom_id=custom_id,
            values=values,
        )

    async def _handle_mcp(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
    ) -> None:
        from .workspace_commands import handle_mcp

        await handle_mcp(
            self,
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
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
        from .workspace_commands import handle_repos

        await handle_repos(
            self,
            interaction_id,
            interaction_token,
        )

    async def _handle_car_new(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        channel_id: str,
    ) -> None:
        from .car_handlers.session_commands import handle_car_new

        await self._run_effectful_handler(
            handle_car_new,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
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
        from .update_service import update_thread_blocks_restart_warning

        return update_thread_blocks_restart_warning(thread)

    def _active_update_session_count(self) -> int:
        from .update_service import active_update_session_count

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
        from .update_service import build_update_confirmation_components

        return build_update_confirmation_components(self, update_target=update_target)

    def _update_status_path(self) -> Path:
        from .update_service import update_status_path

        return update_status_path(self)

    def _format_update_status_message(self, status: Optional[dict[str, Any]]) -> str:
        from .update_service import format_update_status_message

        return format_update_status_message(self, status)

    def _dynamic_update_target_definitions(self):
        from .update_service import dynamic_update_target_definitions

        return dynamic_update_target_definitions(self)

    async def _send_update_status_notice(
        self, notify_context: dict[str, Any], text: str
    ) -> None:
        from .update_service import send_update_status_notice

        await send_update_status_notice(self, notify_context, text)

    def _mark_update_notified(self, status: dict[str, Any]) -> None:
        from .update_service import mark_notified

        mark_notified(self, status)

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
        return agent_registry.get_agent_descriptor(normalized, self)

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
        normalized = agent_registry.normalize_agent_capabilities([capability])
        if not normalized:
            return False
        return next(iter(normalized)) in descriptor.capabilities

    def _agents_supporting_capability(self, capability: str) -> list[str]:
        normalized = agent_registry.normalize_agent_capabilities([capability])
        if not normalized:
            return []
        resolved = next(iter(normalized))
        return sorted(
            descriptor.id
            for descriptor in agent_registry.get_registered_agents(self).values()
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
        from .car_handlers.agent_commands import handle_agent_profile_picker_selection

        await handle_agent_profile_picker_selection(
            self,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            selected_profile=selected_profile,
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
        from .car_handlers.agent_commands import handle_model_picker_selection

        await handle_model_picker_selection(
            self,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            user_id=user_id,
            selected_model=selected_model,
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
        from .car_handlers.agent_commands import handle_model_effort_selection

        await handle_model_effort_selection(
            self,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            user_id=user_id,
            selected_effort=selected_effort,
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

    def _list_paths_in_dir(self, folder: Path) -> list[Path]:
        return list_regular_files(folder)

    def _build_outbox_sent_destination(self, *, sent_dir: Path, path: Path) -> Path:
        destination = sent_dir / path.name
        if destination.exists():
            destination = sent_dir / f"{path.stem}-{uuid.uuid4().hex[:6]}{path.suffix}"
        return destination

    def _find_matching_sent_outbox_copy(
        self,
        *,
        sent_dir: Path,
        path: Path,
        data: bytes,
    ) -> Path | None:
        if not sent_dir.exists():
            return None
        expected_size = len(data)
        expected_digest = hashlib.sha256(data).digest()
        candidates: list[Path] = []
        exact = sent_dir / path.name
        if exact.is_file():
            candidates.append(exact)
        with contextlib.suppress(OSError):
            for candidate in sent_dir.glob(f"{path.stem}-*{path.suffix}"):
                if candidate.is_file() and candidate != exact:
                    candidates.append(candidate)
        for candidate in candidates:
            try:
                if candidate.stat().st_size != expected_size:
                    continue
                if hashlib.sha256(candidate.read_bytes()).digest() == expected_digest:
                    return candidate
            except OSError:
                continue
        return None

    def _cleanup_delivered_outbox_source(
        self,
        *,
        path: Path,
        channel_id: str,
        archived_path: Path,
    ) -> bool:
        try:
            path.unlink()
        except FileNotFoundError:
            return True
        except OSError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.files.outbox.cleanup_failed",
                channel_id=channel_id,
                path=str(path),
                archived_path=str(archived_path),
                exc=exc,
            )
            return False
        return True

    def _archive_sent_outbox_file(
        self,
        *,
        path: Path,
        data: bytes,
        sent_dir: Path,
        channel_id: str,
    ) -> bool:
        sent_dir.mkdir(parents=True, exist_ok=True)
        destination = self._build_outbox_sent_destination(sent_dir=sent_dir, path=path)
        try:
            path.replace(destination)
            return True
        except OSError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.files.outbox.move_failed",
                channel_id=channel_id,
                path=str(path),
                destination=str(destination),
                exc=exc,
            )
        try:
            destination.write_bytes(data)
        except OSError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.files.outbox.copy_failed",
                channel_id=channel_id,
                path=str(path),
                destination=str(destination),
                exc=exc,
            )
            return False
        self._cleanup_delivered_outbox_source(
            path=path,
            channel_id=channel_id,
            archived_path=destination,
        )
        return True

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
        archived_copy = self._find_matching_sent_outbox_copy(
            sent_dir=sent_dir,
            path=path,
            data=data,
        )
        if archived_copy is not None:
            self._cleanup_delivered_outbox_source(
                path=path,
                channel_id=channel_id,
                archived_path=archived_copy,
            )
            log_event(
                self._logger,
                logging.INFO,
                "discord.files.outbox.already_archived",
                channel_id=channel_id,
                path=str(path),
                archived_path=str(archived_copy),
            )
            return True
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
        archived = self._archive_sent_outbox_file(
            path=path,
            data=data,
            sent_dir=sent_dir,
            channel_id=channel_id,
        )
        if not archived:
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

    async def _handle_files_inbox(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
    ) -> None:
        from .car_handlers.system_commands import handle_files_inbox

        await handle_files_inbox(
            self,
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
        )

    async def _handle_files_outbox(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
    ) -> None:
        from .car_handlers.system_commands import handle_files_outbox

        await handle_files_outbox(
            self,
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
        )

    async def _handle_files_clear(
        self,
        interaction_id: str,
        interaction_token: str,
        *,
        workspace_root: Path,
        options: dict[str, Any],
    ) -> None:
        from .car_handlers.system_commands import handle_files_clear

        await handle_files_clear(
            self,
            interaction_id,
            interaction_token,
            workspace_root=workspace_root,
            options=options,
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
        from .pma_commands import handle_pma_command

        await handle_pma_command(
            self,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            guild_id=guild_id,
            command_path=command_path,
            options=options,
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
        dispatcher_conversation_id: Optional[str] = None,
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
            dispatcher_conversation_id=dispatcher_conversation_id,
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
        from .car_handlers.queue_interrupt_handlers import (
            send_interrupt_component_response,
        )

        await send_interrupt_component_response(
            self,
            interaction_id,
            interaction_token,
            text,
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
        from .car_handlers.queue_interrupt_handlers import handle_cancel_turn_button

        await handle_cancel_turn_button(
            self,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            user_id=user_id,
            message_id=message_id,
            custom_id=custom_id,
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
        from .car_handlers.queue_interrupt_handlers import (
            handle_cancel_queued_turn_button,
        )

        await handle_cancel_queued_turn_button(
            self,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            custom_id=custom_id,
            message_id=message_id,
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
        from .car_handlers.queue_interrupt_handlers import (
            handle_queued_turn_interrupt_send_button,
        )

        await handle_queued_turn_interrupt_send_button(
            self,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            custom_id=custom_id,
            user_id=user_id,
            message_id=message_id,
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
        from .car_handlers.queue_interrupt_handlers import handle_queue_cancel_button

        await handle_queue_cancel_button(
            self,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            custom_id=custom_id,
            guild_id=guild_id,
            message_id=message_id,
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
        from .car_handlers.queue_interrupt_handlers import (
            handle_queue_interrupt_send_button,
        )

        await handle_queue_interrupt_send_button(
            self,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            custom_id=custom_id,
            guild_id=guild_id,
            user_id=user_id,
            message_id=message_id,
        )

    async def _handle_continue_turn_button(
        self,
        interaction_id: str,
        interaction_token: str,
    ) -> None:
        from .car_handlers.queue_interrupt_handlers import handle_continue_turn_button

        await handle_continue_turn_button(
            self,
            interaction_id,
            interaction_token,
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
