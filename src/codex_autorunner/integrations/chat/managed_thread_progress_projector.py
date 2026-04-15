from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal, Optional

from ...core.ports.run_event import (
    RUN_EVENT_DELTA_TYPE_USER_MESSAGE,
    ApprovalRequested,
    OutputDelta,
    RunEvent,
    RunNotice,
    Started,
    ToolCall,
)
from .managed_thread_progress import (
    ProgressRuntimeState,
    ProgressTrackerEventOutcome,
    apply_run_event_to_progress_tracker,
)
from .progress_primitives import TurnProgressTracker, render_progress_text

ManagedThreadSemanticPhase = Literal[
    "queued",
    "working",
    "approval",
    "progress",
    "terminal",
]

_LIVE_MANAGED_THREAD_PHASES = frozenset({"queued", "working", "approval", "progress"})


@dataclass(frozen=True)
class AnchorLifecycleSnapshot:
    anchor_ref: Optional[str]
    stage: ManagedThreadSemanticPhase
    owned: bool
    reused: bool
    superseded_anchor_ref: Optional[str]
    cleanup_required: bool


@dataclass(frozen=True)
class ManagedThreadProgressProjection:
    changed: bool
    force: bool = False
    render_mode: str = "live"
    remove_components: bool = False
    terminal: bool = False
    semantic_phase: ManagedThreadSemanticPhase = "working"
    phase_changed: bool = False
    tracker_label: str = "working"
    anchor: AnchorLifecycleSnapshot = field(
        default_factory=lambda: AnchorLifecycleSnapshot(
            anchor_ref=None,
            stage="working",
            owned=False,
            reused=False,
            superseded_anchor_ref=None,
            cleanup_required=False,
        )
    )


class ManagedThreadAnchorPolicy:
    """Track the single live anchor for a managed-thread run.

    Rules:
    - One live anchor is reused through `queued -> working -> approval/progress`.
    - Binding a different anchor supersedes the previous one and marks cleanup.
    - Terminalization does not create a new anchor by itself; it only marks the
      live anchor stage so the surface can decide whether to keep or retire it.
    """

    def __init__(self) -> None:
        self._anchor_ref: Optional[str] = None
        self._stage: ManagedThreadSemanticPhase = "working"
        self._owned = False
        self._reused = False
        self._superseded_anchor_ref: Optional[str] = None
        self._cleanup_required = False

    @property
    def anchor_ref(self) -> Optional[str]:
        return self._anchor_ref

    @property
    def stage(self) -> ManagedThreadSemanticPhase:
        return self._stage

    def bind(
        self,
        anchor_ref: Optional[str],
        *,
        stage: ManagedThreadSemanticPhase,
        owned: bool,
        reused: bool = False,
    ) -> AnchorLifecycleSnapshot:
        normalized = _normalized_optional_text(anchor_ref)
        if normalized is None:
            return self.snapshot()
        if self._anchor_ref and self._anchor_ref != normalized:
            self._superseded_anchor_ref = self._anchor_ref
            self._cleanup_required = self._cleanup_required or self._owned
        self._anchor_ref = normalized
        self._stage = stage
        self._owned = owned
        self._reused = reused
        return self.snapshot()

    def transition(self, stage: ManagedThreadSemanticPhase) -> AnchorLifecycleSnapshot:
        self._stage = stage
        return self.snapshot()

    def mark_cleanup_completed(self) -> AnchorLifecycleSnapshot:
        self._superseded_anchor_ref = None
        self._cleanup_required = False
        return self.snapshot()

    def snapshot(self) -> AnchorLifecycleSnapshot:
        return AnchorLifecycleSnapshot(
            anchor_ref=self._anchor_ref,
            stage=self._stage,
            owned=self._owned,
            reused=self._reused,
            superseded_anchor_ref=self._superseded_anchor_ref,
            cleanup_required=self._cleanup_required,
        )


class ManagedThreadProgressProjector:
    """Shared semantic projector for managed-thread progress UX."""

    def __init__(
        self,
        tracker: TurnProgressTracker,
        *,
        min_render_interval_seconds: float,
        heartbeat_interval_seconds: float,
        initial_phase: ManagedThreadSemanticPhase = "working",
        runtime_state: Optional[ProgressRuntimeState] = None,
    ) -> None:
        self.tracker = tracker
        self.runtime_state = runtime_state or ProgressRuntimeState()
        self.min_render_interval_seconds = max(float(min_render_interval_seconds), 0.0)
        self.heartbeat_interval_seconds = max(float(heartbeat_interval_seconds), 0.0)
        self.semantic_phase: ManagedThreadSemanticPhase = initial_phase
        self.phase_history: list[ManagedThreadSemanticPhase] = []
        self.anchor = ManagedThreadAnchorPolicy()
        self.anchor.transition(initial_phase)
        self.last_rendered: Optional[str] = None
        self.last_render_at = 0.0
        self.finalized = False

    def phase_sequence(self) -> tuple[ManagedThreadSemanticPhase, ...]:
        return tuple(self.phase_history)

    def bind_anchor(
        self,
        anchor_ref: Optional[str],
        *,
        owned: bool,
        reused: bool = False,
    ) -> AnchorLifecycleSnapshot:
        return self.anchor.bind(
            anchor_ref,
            stage=self.semantic_phase,
            owned=owned,
            reused=reused,
        )

    def mark_queued(self, *, force: bool = True) -> ManagedThreadProgressProjection:
        phase_changed = self._set_phase("queued", backfill_previous=False)
        return self._projection(
            changed=phase_changed,
            force=force,
            phase_changed=phase_changed,
        )

    def mark_working(self, *, force: bool = True) -> ManagedThreadProgressProjection:
        phase_changed = self._set_phase("working", backfill_previous=False)
        return self._projection(
            changed=phase_changed,
            force=force,
            phase_changed=phase_changed,
        )

    def note_context_usage(self, percent: Optional[int]) -> None:
        self.tracker.set_context_usage_percent(percent)

    def apply_run_event(
        self,
        run_event: RunEvent,
    ) -> ManagedThreadProgressProjection:
        if isinstance(run_event, Started):
            phase_changed = self._set_phase("working", backfill_previous=False)
            return self._projection(
                changed=phase_changed,
                force=phase_changed,
                phase_changed=phase_changed,
            )

        outcome = apply_run_event_to_progress_tracker(
            self.tracker,
            run_event,
            runtime_state=self.runtime_state,
        )
        target_phase = _semantic_phase_for_run_event(run_event, outcome=outcome)
        phase_changed = False
        if target_phase is not None:
            phase_changed = self._set_phase(target_phase, backfill_previous=True)
        if outcome.terminal:
            self.finalized = True
        return self._projection(
            changed=outcome.changed or phase_changed,
            force=outcome.force or phase_changed,
            render_mode=outcome.render_mode,
            remove_components=outcome.remove_components,
            terminal=outcome.terminal,
            phase_changed=phase_changed,
        )

    def render(
        self,
        *,
        max_length: int,
        now: Optional[float] = None,
        render_mode: str = "live",
    ) -> str:
        render_now = time.monotonic() if now is None else now
        return render_progress_text(
            self.tracker,
            max_length=max_length,
            now=render_now,
            render_mode=render_mode,
        )

    def should_emit_render(
        self,
        rendered: str,
        *,
        now: float,
        force: bool = False,
        min_interval_seconds: Optional[float] = None,
    ) -> bool:
        if force:
            return True
        effective_min_interval = (
            self.min_render_interval_seconds
            if min_interval_seconds is None
            else max(float(min_interval_seconds), 0.0)
        )
        if (now - self.last_render_at) < effective_min_interval:
            return False
        if rendered == self.last_rendered:
            return False
        return True

    def note_rendered(self, rendered: str, *, now: float) -> None:
        self.last_rendered = rendered
        self.last_render_at = now

    def note_render_attempt(self, *, now: float) -> None:
        self.last_render_at = now

    def should_emit_heartbeat(self, *, now: float) -> bool:
        if self.finalized:
            return False
        if self.heartbeat_interval_seconds <= 0:
            return False
        return (now - self.last_render_at) >= self.heartbeat_interval_seconds

    def _projection(
        self,
        *,
        changed: bool,
        force: bool = False,
        render_mode: str = "live",
        remove_components: bool = False,
        terminal: bool = False,
        phase_changed: bool = False,
    ) -> ManagedThreadProgressProjection:
        return ManagedThreadProgressProjection(
            changed=changed,
            force=force,
            render_mode=render_mode,
            remove_components=remove_components,
            terminal=terminal,
            semantic_phase=self.semantic_phase,
            phase_changed=phase_changed,
            tracker_label=self.tracker.label,
            anchor=self.anchor.snapshot(),
        )

    def _set_phase(
        self,
        phase: ManagedThreadSemanticPhase,
        *,
        backfill_previous: bool,
    ) -> bool:
        previous_phase = self.semantic_phase
        if (
            backfill_previous
            and not self.phase_history
            and previous_phase in _LIVE_MANAGED_THREAD_PHASES
            and previous_phase != phase
        ):
            self.phase_history.append(previous_phase)
        phase_changed = phase != previous_phase
        self.semantic_phase = phase
        self.anchor.transition(phase)
        next_label = _tracker_label_for_phase(phase, current_label=self.tracker.label)
        if next_label is not None:
            self.tracker.set_label(next_label)
        if not self.phase_history or self.phase_history[-1] != phase:
            self.phase_history.append(phase)
        return phase_changed


def _semantic_phase_for_run_event(
    run_event: RunEvent,
    *,
    outcome: ProgressTrackerEventOutcome,
) -> Optional[ManagedThreadSemanticPhase]:
    if outcome.terminal:
        return "terminal"
    if isinstance(run_event, ApprovalRequested):
        return "approval"
    if isinstance(run_event, ToolCall):
        return "progress"
    if isinstance(run_event, OutputDelta):
        if run_event.delta_type == RUN_EVENT_DELTA_TYPE_USER_MESSAGE:
            return None
        if not run_event.content.strip():
            return None
        return "progress"
    if isinstance(run_event, RunNotice):
        if run_event.kind in {"thinking", "reasoning", "progress"}:
            return "progress"
    return None


def _tracker_label_for_phase(
    phase: ManagedThreadSemanticPhase,
    *,
    current_label: str,
) -> Optional[str]:
    if phase == "queued":
        return "queued"
    if phase == "working":
        return "working"
    if phase == "approval":
        return "review"
    if phase == "progress":
        return "working"
    if phase == "terminal":
        return current_label or None
    return None


def _normalized_optional_text(value: Optional[str]) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


__all__ = [
    "AnchorLifecycleSnapshot",
    "ManagedThreadAnchorPolicy",
    "ManagedThreadProgressProjection",
    "ManagedThreadProgressProjector",
    "ManagedThreadSemanticPhase",
]
