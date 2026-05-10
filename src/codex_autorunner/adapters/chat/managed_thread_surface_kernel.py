"""Shared managed-thread surface kernel for chat adapters.

Discord and Telegram should keep transport-specific ingress, formatting,
typing/progress UX, and API delivery in their own modules. The shared kernel in
this file owns the reusable boundary between surface code and the generic
managed-thread coordinator: coordinator construction, durable final-delivery
hook creation, and bound queue-progress hook wiring.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, Optional

from ...core.orchestration import SQLiteManagedThreadDeliveryEngine
from .bound_live_progress import build_bound_chat_queue_execution_controller
from .managed_thread_delivery_support import (
    ManagedThreadDeliveryCleanupContext,
    ManagedThreadDeliverySendResult,
    deliver_managed_thread_terminal_record,
)
from .managed_thread_turns import (
    ManagedThreadDurableDeliveryHooks,
    ManagedThreadErrorMessages,
    ManagedThreadExecutionHooks,
    ManagedThreadSurfaceInfo,
    ManagedThreadTurnCoordinator,
    build_managed_thread_delivery_intent,
)

ManagedThreadRecordSend = Callable[
    [Any, ManagedThreadDeliveryCleanupContext],
    Awaitable[Optional[ManagedThreadDeliverySendResult]],
]
ManagedThreadRecordCleanup = Callable[
    [Any, ManagedThreadDeliveryCleanupContext],
    Awaitable[None],
]


def build_managed_thread_surface_coordinator(
    *,
    orchestration_service: Any,
    state_root: Path,
    hub_client: Any,
    raw_config: Optional[dict[str, Any]],
    surface: ManagedThreadSurfaceInfo,
    public_execution_error: str,
    timeout_error: str,
    interrupted_error: str,
    timeout_seconds: float,
    stall_timeout_seconds: Optional[float],
    idle_timeout_only: bool,
    logger: Any,
    turn_preview: str,
    preview_builder: Callable[[str], str],
) -> ManagedThreadTurnCoordinator:
    return ManagedThreadTurnCoordinator(
        orchestration_service=orchestration_service,
        state_root=state_root,
        hub_client=hub_client,
        raw_config=raw_config,
        surface=surface,
        errors=ManagedThreadErrorMessages(
            public_execution_error=public_execution_error,
            timeout_error=timeout_error,
            interrupted_error=interrupted_error,
            timeout_seconds=timeout_seconds,
            stall_timeout_seconds=stall_timeout_seconds,
            idle_timeout_only=idle_timeout_only,
        ),
        logger=logger,
        turn_preview=turn_preview,
        preview_builder=preview_builder,
    )


def build_managed_thread_surface_queue_execution_hooks(
    *,
    hub_root: Path,
    raw_config: Mapping[str, Any],
    managed_thread_id: str,
    surface_targets: tuple[tuple[str, str], ...],
    base_hooks: Optional[ManagedThreadExecutionHooks] = None,
    on_progress_session_started: Optional[Callable[[Any], object]] = None,
) -> ManagedThreadExecutionHooks:
    return build_bound_chat_queue_execution_controller(
        hub_root=hub_root,
        raw_config=raw_config,
        managed_thread_id=managed_thread_id,
        surface_targets=surface_targets,
        base_hooks=base_hooks or ManagedThreadExecutionHooks(),
        on_progress_session_started=on_progress_session_started,
    ).hooks


def build_managed_thread_terminal_delivery_hooks(
    *,
    state_root: Path,
    surface: ManagedThreadSurfaceInfo,
    adapter_key: str,
    transport_target: dict[str, Any],
    metadata: Optional[dict[str, Any]] = None,
    send_success: ManagedThreadRecordSend,
    send_failure: ManagedThreadRecordSend,
    cleanup: ManagedThreadRecordCleanup,
    cleanup_statuses: frozenset[str] = frozenset({"ok", "error"}),
    suppress_statuses: frozenset[str] = frozenset({"interrupted"}),
) -> ManagedThreadDurableDeliveryHooks:
    engine = SQLiteManagedThreadDeliveryEngine(Path(state_root))
    frozen_transport_target = dict(transport_target)
    frozen_metadata = dict(metadata) if isinstance(metadata, dict) else None

    class _ManagedThreadSurfaceDeliveryAdapter:
        @property
        def adapter_key(self) -> str:
            return adapter_key

        async def deliver_managed_thread_record(
            self, record: Any, *, claim: Any
        ) -> Any:
            _ = claim

            async def _send_success(
                context: ManagedThreadDeliveryCleanupContext,
            ) -> ManagedThreadDeliverySendResult:
                result = await send_success(record, context)
                return result or ManagedThreadDeliverySendResult()

            async def _send_failure(
                context: ManagedThreadDeliveryCleanupContext,
            ) -> ManagedThreadDeliverySendResult:
                result = await send_failure(record, context)
                return result or ManagedThreadDeliverySendResult()

            async def _cleanup(context: ManagedThreadDeliveryCleanupContext) -> None:
                await cleanup(record, context)

            return await deliver_managed_thread_terminal_record(
                record,
                send_success=_send_success,
                send_failure=_send_failure,
                cleanup=_cleanup,
                cleanup_statuses=cleanup_statuses,
            )

    return ManagedThreadDurableDeliveryHooks(
        engine=engine,
        adapter=_ManagedThreadSurfaceDeliveryAdapter(),
        build_delivery_intent=lambda finalized: (
            None
            if finalized.status in suppress_statuses
            else build_managed_thread_delivery_intent(
                finalized,
                surface=surface,
                transport_target=dict(frozen_transport_target),
                metadata=(
                    dict(frozen_metadata) if frozen_metadata is not None else None
                ),
            )
        ),
    )


__all__ = [
    "ManagedThreadRecordCleanup",
    "ManagedThreadRecordSend",
    "build_managed_thread_surface_coordinator",
    "build_managed_thread_surface_queue_execution_hooks",
    "build_managed_thread_terminal_delivery_hooks",
]
