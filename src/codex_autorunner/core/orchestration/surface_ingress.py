from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from .events import OrchestrationEvent
from .flows import PausedFlowTarget
from .threads import SurfaceThreadMessageRequest, ThreadControlRequest

ResolvePausedFlowTarget = Callable[
    [SurfaceThreadMessageRequest],
    Awaitable[Optional[PausedFlowTarget]],
]
SubmitFlowReply = Callable[
    [SurfaceThreadMessageRequest, PausedFlowTarget],
    Awaitable[Any],
]
SubmitThreadMessage = Callable[[SurfaceThreadMessageRequest], Awaitable[Any]]
RunThreadControl = Callable[[ThreadControlRequest], Awaitable[Any]]


@dataclass(frozen=True)
class SurfaceIngressResult:
    """Result of routing one surface request through orchestration ingress."""

    route: str
    events: tuple[OrchestrationEvent, ...] = ()
    thread_result: Any = None
    flow_result: Any = None
    flow_target: Optional[PausedFlowTarget] = None
    control_result: Any = None


@dataclass
class SurfaceOrchestrationIngress:
    """Shared ingress for surfaces that need thread-versus-flow routing."""

    event_sink: Optional[Callable[[OrchestrationEvent], None]] = None

    def _emit(
        self,
        events: list[OrchestrationEvent],
        *,
        event_type: str,
        target_kind: str,
        surface_kind: str,
        target_id: Optional[str] = None,
        status: Optional[str] = None,
        detail: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        event = OrchestrationEvent(
            event_type=event_type,  # type: ignore[arg-type]
            target_kind=target_kind,  # type: ignore[arg-type]
            surface_kind=surface_kind,
            target_id=target_id,
            status=status,
            detail=detail,
            metadata=dict(metadata or {}),
        )
        events.append(event)
        if self.event_sink is not None:
            self.event_sink(event)

    async def submit_message(
        self,
        request: SurfaceThreadMessageRequest,
        *,
        resolve_paused_flow_target: ResolvePausedFlowTarget,
        submit_flow_reply: SubmitFlowReply,
        submit_thread_message: SubmitThreadMessage,
    ) -> SurfaceIngressResult:
        events: list[OrchestrationEvent] = []
        self._emit(
            events,
            event_type="ingress.received",
            target_kind="thread",
            surface_kind=request.surface_kind,
            metadata={"pma_enabled": request.pma_enabled},
        )
        flow_target = None
        if not request.pma_enabled:
            flow_target = await resolve_paused_flow_target(request)
        if flow_target is not None:
            self._emit(
                events,
                event_type="ingress.target_resolved",
                target_kind="flow",
                surface_kind=request.surface_kind,
                target_id=flow_target.flow_target.flow_target_id,
                status=flow_target.status,
                metadata={"run_id": flow_target.run_id},
            )
            flow_result = await submit_flow_reply(request, flow_target)
            self._emit(
                events,
                event_type="ingress.flow_resumed",
                target_kind="flow",
                surface_kind=request.surface_kind,
                target_id=flow_target.flow_target.flow_target_id,
                status=flow_target.status,
                metadata={"run_id": flow_target.run_id},
            )
            return SurfaceIngressResult(
                route="flow",
                events=tuple(events),
                flow_result=flow_result,
                flow_target=flow_target,
            )

        self._emit(
            events,
            event_type="ingress.target_resolved",
            target_kind="thread",
            surface_kind=request.surface_kind,
            target_id=request.agent_id,
            metadata={"workspace_root": str(request.workspace_root)},
        )
        thread_result = await submit_thread_message(request)
        self._emit(
            events,
            event_type="ingress.thread_submitted",
            target_kind="thread",
            surface_kind=request.surface_kind,
            target_id=request.agent_id,
            metadata={"workspace_root": str(request.workspace_root)},
        )
        return SurfaceIngressResult(
            route="thread",
            events=tuple(events),
            thread_result=thread_result,
        )

    async def run_thread_control(
        self,
        request: ThreadControlRequest,
        *,
        control_runner: RunThreadControl,
    ) -> SurfaceIngressResult:
        events: list[OrchestrationEvent] = []
        self._emit(
            events,
            event_type="ingress.control_requested",
            target_kind="thread",
            surface_kind=request.surface_kind,
            target_id=request.target_id,
            status=request.action,
        )
        control_result = await control_runner(request)
        self._emit(
            events,
            event_type="ingress.control_completed",
            target_kind="thread",
            surface_kind=request.surface_kind,
            target_id=request.target_id,
            status=request.action,
        )
        return SurfaceIngressResult(
            route="thread_control",
            events=tuple(events),
            control_result=control_result,
        )


def build_surface_orchestration_ingress(
    *, event_sink: Optional[Callable[[OrchestrationEvent], None]] = None
) -> SurfaceOrchestrationIngress:
    """Build the shared ingress facade used by chat surfaces."""

    return SurfaceOrchestrationIngress(event_sink=event_sink)


def get_surface_orchestration_ingress(owner: Any) -> SurfaceOrchestrationIngress:
    """Return the lazily-initialized ingress facade for a surface/service object."""

    existing = getattr(owner, "_surface_orchestration_ingress", None)
    if isinstance(existing, SurfaceOrchestrationIngress):
        return existing
    created = build_surface_orchestration_ingress()
    owner._surface_orchestration_ingress = created
    return created
