from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from .....core.orchestration import (
    SurfaceThreadMessageRequest,
    build_surface_orchestration_ingress,
)
from .....core.pma_audit import PmaActionType
from .....core.pma_context import (
    build_hub_snapshot,
    format_pma_prompt,
    load_pma_prompt,
)
from .....core.pma_origin import PmaOriginContext, extract_pma_origin_metadata
from .....core.text_utils import _normalize_optional_text
from .....integrations.app_server.threads import pma_automation_key, pma_base_key
from .....integrations.github.context_injection import maybe_inject_github_context
from ...services.pma import get_pma_request_context
from ...services.pma.common import pma_config_from_raw
from ..agent_profile_validation import resolve_requested_agent_profile
from ..agents import _available_agents
from .chat_result_persistence import finalize_result
from .chat_runtime_execution import build_runtime_harness, execute_harness_turn
from .publish import publish_automation_result
from .runtime_state import PmaRuntimeState

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PmaExecutionOrigin:
    session_key: str
    backend_thread_id: Optional[str] = None


def _get_pma_config(request: Any) -> dict[str, Any]:
    context = get_pma_request_context(request)
    return pma_config_from_raw(context.raw_config)


def resolve_pma_session_key(
    agent_id: str,
    profile: Optional[str],
    *,
    automation_trigger: bool,
    pma_origin: Optional[PmaOriginContext] = None,
) -> str:
    if automation_trigger and pma_origin and pma_origin.thread_id:
        return pma_base_key(agent_id, profile)
    if automation_trigger:
        return pma_automation_key(agent_id, profile)
    return pma_base_key(agent_id, profile)


def resolve_pma_execution_origin(
    agent_id: str,
    profile: Optional[str],
    *,
    automation_trigger: bool,
    wake_up: Optional[dict[str, Any]],
) -> PmaExecutionOrigin:
    pma_origin = extract_pma_origin_metadata(
        wake_up.get("metadata") if isinstance(wake_up, dict) else None
    )
    should_resume_origin = (
        automation_trigger
        and pma_origin is not None
        and pma_origin.thread_id is not None
        and (pma_origin.agent is None or pma_origin.agent == agent_id)
        and (pma_origin.profile is None or pma_origin.profile == profile)
    )
    if not should_resume_origin:
        pma_origin = None
    return PmaExecutionOrigin(
        session_key=resolve_pma_session_key(
            agent_id,
            profile,
            automation_trigger=automation_trigger,
            pma_origin=pma_origin,
        ),
        backend_thread_id=pma_origin.thread_id if pma_origin else None,
    )


async def execute_queue_item(
    runtime: PmaRuntimeState,
    item: Any,
    request: Any,
    *,
    turn_timeout_seconds: Optional[float] = None,
) -> dict[str, Any]:
    context = get_pma_request_context(request)
    hub_root = context.hub_root
    payload = item.payload

    client_turn_id = payload.get("client_turn_id")
    message = payload.get("message", "")
    agent = payload.get("agent")
    profile = _normalize_optional_text(payload.get("profile"))
    model = _normalize_optional_text(payload.get("model"))
    reasoning = _normalize_optional_text(payload.get("reasoning"))
    lifecycle_event = payload.get("lifecycle_event")
    if not isinstance(lifecycle_event, dict):
        lifecycle_event = None
    wake_up = payload.get("wake_up")
    if not isinstance(wake_up, dict):
        wake_up = None
    automation_trigger = lifecycle_event is not None or wake_up is not None
    requested_origin = extract_pma_origin_metadata(
        wake_up.get("metadata") if isinstance(wake_up, dict) else None
    )

    store = runtime.get_state_store(hub_root)
    defaults = _get_pma_config(request)
    started = False

    async def _finalize_queue_result_payload(
        result_payload: dict[str, Any],
        *,
        persist: bool = True,
    ) -> dict[str, Any]:
        payload_result = dict(result_payload or {})
        timeline_events = (
            payload_result.get("timeline_events")
            if isinstance(payload_result.get("timeline_events"), list)
            else None
        )
        payload_result.pop("timeline_events", None)
        if automation_trigger:
            try:
                payload_result.update(
                    await publish_automation_result(
                        request=request,
                        result=payload_result,
                        client_turn_id=(
                            _normalize_optional_text(client_turn_id) or None
                        ),
                        lifecycle_event=lifecycle_event,
                        wake_up=wake_up,
                    )
                )
            except (
                RuntimeError,
                OSError,
                ConnectionError,
            ) as exc:
                logger.exception(
                    "Failed publishing PMA automation result: client_turn_id=%s",
                    client_turn_id,
                )
                payload_result["delivery_status"] = "failed"
                payload_result["delivery_outcome"] = {
                    "published": 0,
                    "duplicates": 0,
                    "failed": 1,
                    "targets": 0,
                    "repo_id": None,
                    "correlation_id": (
                        _normalize_optional_text(client_turn_id)
                        or f"pma-{uuid.uuid4().hex[:12]}"
                    ),
                    "errors": [str(exc)],
                }
        if persist and started:
            await finalize_result(
                runtime,
                payload_result,
                request=request,
                store=store,
                prompt_message=message,
                lifecycle_event=lifecycle_event,
                profile=profile,
                model=model,
                reasoning=reasoning,
                timeline_events=timeline_events,
            )
        return payload_result

    def _resolve_default_agent(available_ids: set[str], available_default: str) -> str:
        configured_default = defaults.get("default_agent")
        from .....agents.registry import validate_agent_id

        try:
            candidate = validate_agent_id(
                configured_default or "", context.agent_context
            )
        except ValueError:
            candidate = None
        if candidate and candidate in available_ids:
            return candidate
        return available_default

    from .....agents.registry import validate_agent_id

    agents, available_default = _available_agents(request)
    available_ids = {
        agent_id
        for entry in agents
        if isinstance(entry, dict)
        for agent_id in [entry.get("id")]
        if isinstance(agent_id, str)
    }

    try:
        requested_agent = agent
        if requested_agent is None and requested_origin is not None:
            requested_agent = requested_origin.agent
        agent_id = validate_agent_id(requested_agent or "", context.agent_context)
    except ValueError:
        agent_id = _resolve_default_agent(available_ids, available_default)

    profile = resolve_requested_agent_profile(
        request,
        agent_id,
        (
            profile
            if profile is not None
            else requested_origin.profile if requested_origin is not None else None
        ),
        default_profile=_normalize_optional_text(defaults.get("profile")),
    )
    execution_origin = resolve_pma_execution_origin(
        agent_id,
        profile,
        automation_trigger=automation_trigger,
        wake_up=wake_up,
    )

    safety_checker = runtime.get_safety_checker(hub_root, context)
    safety_check = safety_checker.check_chat_start(agent_id, message, client_turn_id)
    if not safety_check.allowed:
        detail = safety_check.reason or "PMA action blocked by safety check"
        if safety_check.details:
            detail = f"{detail}: {safety_check.details}"
        return await _finalize_queue_result_payload(
            {"status": "error", "detail": detail},
            persist=False,
        )

    started = await runtime.begin_turn(
        client_turn_id, store=store, lane_id=getattr(item, "lane_id", None)
    )
    if not started:
        detail = "Another PMA turn is already active; queue item was not started"
        logger.warning("PMA queue item rejected: %s", detail)
        return await _finalize_queue_result_payload(
            {
                "status": "error",
                "detail": detail,
                "client_turn_id": client_turn_id or "",
            },
            persist=False,
        )

    if not model and defaults.get("model"):
        model = defaults["model"]
    if not reasoning and defaults.get("reasoning"):
        reasoning = defaults["reasoning"]

    try:
        prompt_base = load_pma_prompt(hub_root)
        supervisor = context.hub_supervisor
        snapshot_builder = getattr(
            context.ports, "build_hub_snapshot", build_hub_snapshot
        )
        github_context_injector = (
            getattr(
                context.ports,
                "maybe_inject_github_context",
                None,
            )
            or maybe_inject_github_context
        )

        snapshot = await snapshot_builder(supervisor, hub_root=hub_root)
        prompt_state_key = execution_origin.session_key

        async def _rebuild_prompt(force_full_base_prompt: bool) -> str:
            built = format_pma_prompt(
                prompt_base,
                snapshot,
                message,
                hub_root=hub_root,
                prompt_state_key=prompt_state_key,
                force_full_base_prompt=force_full_base_prompt,
            )
            injected, _ = await github_context_injector(
                prompt_text=built,
                link_source_text=message,
                workspace_root=hub_root,
                logger=logger,
                event_prefix="web.pma.github_context",
                allow_cross_repo=True,
            )
            return injected

        prompt = await _rebuild_prompt(False)
    except (
        OSError,
        ValueError,
        RuntimeError,
    ) as exc:
        error_result = {
            "status": "error",
            "detail": str(exc),
            "client_turn_id": client_turn_id or "",
        }
        return await _finalize_queue_result_payload(error_result)

    interrupt_event = await runtime.get_interrupt_event()
    if interrupt_event.is_set():
        result = {"status": "interrupted", "detail": "PMA chat interrupted"}
        return await _finalize_queue_result_payload(result)

    async def _meta(thread_id: str, turn_id: str) -> None:
        await runtime.update_current(
            store=store,
            client_turn_id=client_turn_id or "",
            status="running",
            agent=agent_id,
            profile=profile,
            thread_id=thread_id,
            turn_id=turn_id,
        )

        safety_checker.record_action(
            action_type=PmaActionType.CHAT_STARTED,
            agent=agent_id,
            thread_id=thread_id,
            turn_id=turn_id,
            client_turn_id=client_turn_id,
            details={"message": message[:200]},
        )

        from .....core.logging_utils import log_event

        log_event(
            logger,
            logging.INFO,
            "pma.turn.started",
            agent=agent_id,
            client_turn_id=client_turn_id or None,
            thread_id=thread_id,
            turn_id=turn_id,
        )

    registry = context.app_server_threads

    from . import chat_runtime as chat_runtime_routes

    ingress_builder = getattr(
        chat_runtime_routes,
        "build_surface_orchestration_ingress",
        build_surface_orchestration_ingress,
    )
    ingress = ingress_builder(
        event_sink=lambda orchestration_event: logger.info(
            "web.pma.%s surface=%s target_kind=%s target_id=%s status=%s meta=%s",
            orchestration_event.event_type,
            orchestration_event.surface_kind,
            orchestration_event.target_kind,
            orchestration_event.target_id,
            orchestration_event.status,
            orchestration_event.metadata,
        )
    )

    async def _resolve_no_flow(
        _request: SurfaceThreadMessageRequest,
    ) -> None:
        """PMA web does not participate in paused-flow resolution.

        Returning ``None`` signals the ingress layer that no flow target
        exists to resume, which is the expected behavior for the web
        surface.
        """
        return None

    async def _submit_flow_reply(
        _request: SurfaceThreadMessageRequest, _flow_target: Any
    ) -> dict[str, Any]:
        raise RuntimeError("PMA web ingress does not route ticket_flow replies")

    async def _submit_thread_message(
        _request: SurfaceThreadMessageRequest,
    ) -> dict[str, Any]:
        try:
            harness = build_runtime_harness(request, agent_id, profile)
        except Exception as exc:
            from fastapi import HTTPException

            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            return {"status": "error", "detail": str(detail)}
        if not callable(getattr(harness, "supports", None)):
            return {"status": "error", "detail": "Runtime harness unavailable"}
        if not harness.supports("durable_threads") or not harness.supports(
            "message_turns"
        ):
            return {
                "status": "error",
                "detail": f"Agent '{agent_id}' does not support PMA message turns",
            }
        return await execute_harness_turn(
            harness,
            hub_root,
            prompt,
            interrupt_event,
            model=model,
            reasoning=reasoning,
            thread_registry=registry,
            thread_key=execution_origin.session_key,
            backend_thread_id=execution_origin.backend_thread_id,
            on_meta=_meta,
            timeout_seconds=turn_timeout_seconds,
            rebuild_prompt=_rebuild_prompt,
        )

    try:
        ingress_result = await ingress.submit_message(
            SurfaceThreadMessageRequest(
                surface_kind="web",
                workspace_root=hub_root,
                prompt_text=message,
                agent_id=agent_id,
                pma_enabled=True,
                metadata={
                    "client_turn_id": client_turn_id or "",
                    "profile": profile or "",
                },
            ),
            resolve_paused_flow_target=_resolve_no_flow,
            submit_flow_reply=_submit_flow_reply,
            submit_thread_message=_submit_thread_message,
        )
        result = dict(ingress_result.thread_result or {})
    except Exception as exc:
        error_result = {
            "status": "error",
            "detail": str(exc),
            "client_turn_id": client_turn_id or "",
        }
        await _finalize_queue_result_payload(error_result)
        raise

    result = dict(result or {})
    result["client_turn_id"] = client_turn_id or ""
    return await _finalize_queue_result_payload(result)
