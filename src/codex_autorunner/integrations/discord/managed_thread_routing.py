from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, cast

from ...core.config import load_hub_config
from ...core.config_contract import ConfigError
from ...core.hub_control_plane import (
    RemoteSurfaceBindingStore,
    RemoteThreadExecutionStore,
)
from ...core.orchestration import (
    SQLiteManagedThreadDeliveryEngine,
    build_harness_backed_orchestration_service,
)
from ...core.orchestration.bindings import OrchestrationBindingStore
from ...integrations.chat.agents import resolve_chat_runtime_agent
from ..chat.managed_thread_turns import (
    ManagedThreadCoordinatorHooks,
    ManagedThreadDurableDeliveryHooks,
    ManagedThreadErrorMessages,
    ManagedThreadFinalizationResult,
    ManagedThreadSurfaceInfo,
    ManagedThreadTargetRequest,
    ManagedThreadTurnCoordinator,
    build_managed_thread_delivery_intent,
    render_managed_thread_response_text,
)
from ..chat.managed_thread_turns import (
    build_managed_thread_input_items as _shared_build_managed_thread_input_items,
)
from ..chat.managed_thread_turns import (
    resolve_managed_thread_target as _shared_resolve_managed_thread_target,
)
from .managed_thread_delivery import deliver_discord_managed_thread_record
from .rendering import (
    DISCORD_MAX_MESSAGE_LENGTH,
    chunk_discord_message,
    format_discord_message,
    truncate_for_discord,
)

_logger = logging.getLogger(__name__)

_DEFAULT_DISCORD_PMA_TIMEOUT_SECONDS = 7200


def _build_managed_thread_input_items(
    runtime_prompt: str,
    input_items: Optional[list[dict[str, Any]]],
) -> Optional[list[dict[str, Any]]]:
    return _shared_build_managed_thread_input_items(
        runtime_prompt,
        input_items,
    )


def _coerce_launch_command(value: Any) -> Optional[list[str]]:
    if isinstance(value, (list, tuple)):
        command = [str(part) for part in value if str(part).strip()]
        return command or None
    return None


def _runtime_launch_command_from_harness(harness: Any) -> Optional[list[str]]:
    supervisor = getattr(harness, "_supervisor", None)
    if supervisor is None:
        return None
    launch_command = getattr(supervisor, "launch_command", None)
    if callable(launch_command):
        try:
            return _coerce_launch_command(launch_command())
        except (RuntimeError, ValueError, TypeError, AttributeError):
            return None
    return _coerce_launch_command(launch_command)


async def _evict_cached_runtime_supervisors(
    service: Any,
    *,
    agent_id: str,
    profile: Optional[str],
    workspace_root: Path,
) -> int:
    from .message_turns import resolve_agent_runtime

    cache = getattr(service, "_agent_runtime_supervisors", None)
    if not isinstance(cache, dict) or not cache:
        return 0
    try:
        resolution = resolve_agent_runtime(agent_id, profile, context=service)
    except (KeyError, ValueError, TypeError, RuntimeError):
        return 0
    runtime_agent_id = str(getattr(resolution, "runtime_agent_id", "") or "").strip()
    runtime_profile = (
        str(getattr(resolution, "runtime_profile", "") or "").strip().lower()
    )
    if not runtime_agent_id:
        return 0

    matching_keys = [
        key
        for key in list(cache.keys())
        if isinstance(key, tuple)
        and len(key) == 3
        and str(key[1] or "").strip() == runtime_agent_id
        and str(key[2] or "").strip().lower() == runtime_profile
    ]
    if not matching_keys:
        return 0

    supervisors = [cache.pop(key, None) for key in matching_keys]
    evicted = 0
    for supervisor in supervisors:
        if supervisor is None:
            continue
        evicted += 1
        if getattr(service, "hermes_supervisor", None) is supervisor:
            with contextlib.suppress(AttributeError):
                service.hermes_supervisor = None
        close_workspace = getattr(supervisor, "close_workspace", None)
        close_all = getattr(supervisor, "close_all", None)
        try:
            if callable(close_workspace):
                await close_workspace(workspace_root)
            elif callable(close_all):
                await close_all()
        except (RuntimeError, ConnectionError, OSError, ValueError, TypeError):
            _logger.debug(
                "Runtime supervisor eviction cleanup failed for agent=%s profile=%s",
                runtime_agent_id,
                runtime_profile or None,
                exc_info=True,
            )
    return evicted


def build_discord_thread_orchestration_service(service: Any) -> Any:
    from .message_turns import (
        get_registered_agents,
        log_event,
        resolve_agent_runtime,
        wrap_requested_agent_context,
    )

    cached = getattr(service, "_discord_thread_orchestration_service", None)
    if cached is None:
        cached = getattr(service, "_discord_managed_thread_orchestration_service", None)
    if cached is not None:
        return cached

    descriptors = get_registered_agents(service)

    def _make_harness(agent_id: str, profile: Optional[str] = None) -> Any:
        resolution = resolve_agent_runtime(agent_id, profile, context=service)
        descriptor = descriptors.get(resolution.runtime_agent_id)
        if descriptor is None:
            raise KeyError(f"Unknown agent definition '{resolution.runtime_agent_id}'")
        harness = descriptor.make_harness(
            wrap_requested_agent_context(
                service,
                agent_id=resolution.runtime_agent_id,
                profile=resolution.runtime_profile,
            )
        )
        runtime_kind = str(
            getattr(descriptor, "runtime_kind", resolution.runtime_agent_id) or ""
        ).strip()
        if runtime_kind == "hermes" or resolution.logical_agent_id == "hermes":
            log_event(
                service._logger,
                logging.INFO,
                "discord.hermes.runtime_resolution",
                requested_agent_id=agent_id,
                requested_profile=profile,
                logical_agent_id=resolution.logical_agent_id,
                logical_profile=resolution.logical_profile,
                resolution_kind=resolution.resolution_kind,
                runtime_agent_id=resolution.runtime_agent_id,
                runtime_profile=resolution.runtime_profile,
                launch_command=_runtime_launch_command_from_harness(harness),
            )
        return harness

    hub_client = getattr(service, "_hub_client", None)
    handshake_compat = getattr(service, "_hub_handshake_compatibility", None)
    handshake_ok = hub_client is not None and getattr(
        handshake_compat, "compatible", False
    )
    if not handshake_ok:
        log_event(
            service._logger,
            logging.WARNING,
            "discord.orchestration.hub_client_unavailable",
            message="Hub control-plane client not available; orchestration disabled",
        )
        return None
    thread_store = RemoteThreadExecutionStore(cast(Any, hub_client))
    binding_store: OrchestrationBindingStore = RemoteSurfaceBindingStore(  # type: ignore[assignment]
        cast(Any, hub_client)
    )
    created = build_harness_backed_orchestration_service(
        descriptors=cast(Any, descriptors),
        harness_factory=_make_harness,
        thread_store=thread_store,
        binding_store=binding_store,
    )
    service._discord_thread_orchestration_service = created
    service._discord_managed_thread_orchestration_service = created
    return created


def resolve_discord_thread_target(
    service: Any,
    *,
    channel_id: str,
    managed_thread_surface_key: Optional[str] = None,
    workspace_root: Path,
    agent: str,
    agent_profile: Optional[str] = None,
    repo_id: Optional[str],
    resource_kind: Optional[str],
    resource_id: Optional[str],
    mode: str,
    pma_enabled: bool,
) -> Any:
    orchestration_service = build_discord_thread_orchestration_service(service)
    if orchestration_service is None:
        raise RuntimeError(
            "Discord orchestration service unavailable: hub control-plane client not connected"
        )
    surface_key = managed_thread_surface_key or channel_id
    existing_binding = None
    existing_thread = None
    get_binding = getattr(orchestration_service, "get_binding", None)
    get_thread_target = getattr(orchestration_service, "get_thread_target", None)
    if callable(get_binding):
        with contextlib.suppress(
            RuntimeError, ValueError, TypeError, KeyError, AttributeError
        ):
            existing_binding = get_binding(
                surface_kind="discord",
                surface_key=surface_key,
            )
    normalized_mode = str(mode or "").strip().lower()
    existing_thread_target_id = (
        str(getattr(existing_binding, "thread_target_id", "") or "").strip()
        if str(getattr(existing_binding, "mode", "") or "").strip().lower()
        == normalized_mode
        else ""
    )
    if callable(get_thread_target) and existing_thread_target_id:
        with contextlib.suppress(
            RuntimeError, ValueError, TypeError, KeyError, AttributeError
        ):
            existing_thread = get_thread_target(existing_thread_target_id)
    runtime_agent = resolve_chat_runtime_agent(
        agent,
        agent_profile,
        default=getattr(service, "DEFAULT_AGENT", "codex"),
        context=service,
    )
    owner_kind, owner_id, normalized_repo_id = service._resource_owner_for_workspace(
        workspace_root,
        repo_id=repo_id,
        resource_kind=resource_kind,
        resource_id=resource_id,
    )
    canonical_workspace = str(workspace_root.resolve())
    reusable_agent_ids = tuple(dict.fromkeys((agent, runtime_agent)))
    existing_thread_reusable = (
        existing_thread is not None
        and str(getattr(existing_thread, "agent_id", "") or "").strip()
        in reusable_agent_ids
        and (getattr(existing_thread, "agent_profile", None) or None)
        == (agent_profile or None)
        and str(getattr(existing_thread, "workspace_root", "") or "").strip()
        == canonical_workspace
    )
    current_backend_thread_id = (
        str(getattr(existing_thread, "backend_thread_id", "") or "").strip() or None
        if existing_thread_reusable
        else None
    )
    current_runtime_instance_id = (
        str(getattr(existing_thread, "backend_runtime_instance_id", "") or "").strip()
        or None
        if existing_thread_reusable
        else None
    )
    return _shared_resolve_managed_thread_target(
        orchestration_service,
        request=ManagedThreadTargetRequest(
            surface_kind="discord",
            surface_key=surface_key,
            mode=mode,
            agent=agent,
            agent_profile=agent_profile,
            workspace_root=workspace_root,
            display_name=f"discord:{surface_key}",
            repo_id=normalized_repo_id,
            resource_kind=owner_kind,
            resource_id=owner_id,
            binding_metadata={"channel_id": channel_id, "pma_enabled": pma_enabled},
            reusable_agent_ids=(runtime_agent,),
            backend_thread_id=current_backend_thread_id,
            backend_runtime_instance_id=current_runtime_instance_id,
            existing_binding=existing_binding,
            existing_thread=existing_thread,
        ),
    )


@dataclass(frozen=True)
class _DiscordManagedThreadStatus:
    thread_target_id: Optional[str]
    busy: bool


def _build_discord_managed_thread_coordinator(
    *,
    service: Any,
    orchestration_service: Any,
    channel_id: str,
    public_execution_error: str,
    timeout_error: str,
    interrupted_error: str,
    pma_enabled: bool,
) -> ManagedThreadTurnCoordinator:
    timeout_seconds = (
        _load_discord_pma_turn_timeout_seconds(service)
        if pma_enabled
        else float(_DEFAULT_DISCORD_PMA_TIMEOUT_SECONDS)
    )
    return ManagedThreadTurnCoordinator(
        orchestration_service=orchestration_service,
        state_root=service._config.root,
        hub_client=getattr(service, "_hub_client", None),
        raw_config=(
            service._config.raw
            if isinstance(getattr(service._config, "raw", None), dict)
            else None
        ),
        surface=ManagedThreadSurfaceInfo(
            log_label="Discord",
            surface_kind="discord",
            surface_key=channel_id,
        ),
        errors=ManagedThreadErrorMessages(
            public_execution_error=public_execution_error,
            timeout_error=timeout_error,
            interrupted_error=interrupted_error,
            timeout_seconds=timeout_seconds,
        ),
        logger=getattr(service, "_logger", _logger),
        turn_preview="",
        preview_builder=lambda message_text: truncate_for_discord(
            message_text,
            max_len=120,
        ),
    )


def _load_discord_pma_turn_timeout_seconds(service: Any) -> float:
    from . import message_turns as _mt

    overridden_timeout = getattr(
        _mt,
        "DISCORD_PMA_TIMEOUT_SECONDS",
        _DEFAULT_DISCORD_PMA_TIMEOUT_SECONDS,
    )
    if overridden_timeout != _DEFAULT_DISCORD_PMA_TIMEOUT_SECONDS:
        return float(overridden_timeout)
    try:
        hub_config = load_hub_config(Path(service._config.root))
    except (ConfigError, OSError, RuntimeError, TypeError, ValueError):
        return float(_DEFAULT_DISCORD_PMA_TIMEOUT_SECONDS)
    configured_timeout = getattr(
        getattr(hub_config, "pma", None),
        "turn_timeout_seconds",
        None,
    )
    if configured_timeout is None:
        return float(_DEFAULT_DISCORD_PMA_TIMEOUT_SECONDS)
    return float(configured_timeout)


def _build_discord_runner_hooks(
    service: Any,
    *,
    channel_id: str,
    managed_thread_id: str,
    public_execution_error: str,
) -> ManagedThreadCoordinatorHooks:
    async def _run_with_discord_typing_indicator(work: Any) -> None:
        run_with_typing = getattr(service, "_run_with_typing_indicator", None)
        if callable(run_with_typing):
            await run_with_typing(channel_id=channel_id, work=work)
            return
        await work()

    async def _on_execution_started(
        started_execution: Any,
    ) -> None:
        service._register_discord_turn_approval_context(
            started_execution=started_execution,
            channel_id=channel_id,
        )

    def _on_execution_finished(started_execution: Any) -> None:
        service._clear_discord_turn_approval_context(
            started_execution=started_execution
        )

    state_root = Path(getattr(getattr(service, "_config", None), "root", Path.cwd()))
    engine = SQLiteManagedThreadDeliveryEngine(state_root)

    class _DiscordManagedThreadDeliveryAdapter:
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
                channel_id_fallback=channel_id,
                base_record_label="discord-queued",
                error_record_label="discord-queued-error",
                default_execution_error=public_execution_error,
            )

    durable_delivery = ManagedThreadDurableDeliveryHooks(
        engine=engine,
        adapter=_DiscordManagedThreadDeliveryAdapter(),
        build_delivery_intent=lambda finalized: (
            None
            if finalized.status == "interrupted"
            else build_managed_thread_delivery_intent(
                finalized,
                surface=ManagedThreadSurfaceInfo(
                    log_label="Discord",
                    surface_kind="discord",
                    surface_key=channel_id,
                ),
                transport_target={"channel_id": channel_id},
                metadata={"managed_thread_id": managed_thread_id},
            )
        ),
    )

    async def _deliver_result(finalized: ManagedThreadFinalizationResult) -> None:
        if finalized.status == "ok":
            assistant_text = render_managed_thread_response_text(finalized)
            formatted = (
                format_discord_message(assistant_text)
                if assistant_text
                else "(No response text returned.)"
            )
            chunks = chunk_discord_message(
                formatted,
                max_len=DISCORD_MAX_MESSAGE_LENGTH,
                with_numbering=False,
            )
            if not chunks:
                chunks = [formatted]
            base_record_id = (
                f"discord-queued:{managed_thread_id}:{finalized.managed_turn_id}"
            )
            for chunk_index, chunk in enumerate(chunks, start=1):
                record_id = (
                    f"{base_record_id}:chunk:{chunk_index}"
                    if len(chunks) > 1
                    else base_record_id
                )
                await service._send_channel_message_safe(
                    channel_id,
                    {"content": chunk},
                    record_id=record_id,
                )
            return
        if finalized.status == "interrupted":
            return
        await service._send_channel_message_safe(
            channel_id,
            {"content": (f"Turn failed: {finalized.error or public_execution_error}")},
            record_id=(
                f"discord-queued-error:{managed_thread_id}:{finalized.managed_turn_id}"
            ),
        )

    return ManagedThreadCoordinatorHooks(
        on_execution_started=_on_execution_started,
        on_execution_finished=_on_execution_finished,
        durable_delivery=durable_delivery,
        deliver_result=_deliver_result,
        run_with_indicator=_run_with_discord_typing_indicator,
    )


def _build_discord_queue_worker_hooks(
    service: Any,
    *,
    channel_id: str,
    managed_thread_id: str,
    public_execution_error: str,
) -> Any:
    return _build_discord_runner_hooks(
        service,
        channel_id=channel_id,
        managed_thread_id=managed_thread_id,
        public_execution_error=public_execution_error,
    ).queue_worker_hooks()
