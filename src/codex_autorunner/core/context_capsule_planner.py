from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .context_capsules import (
    ContextCapsule,
    ContextCapsuleRenderPlan,
    ledger_backend_thread_id_for_scope,
    ledger_scope_id_for_capsule,
)
from .orchestration.context_capsule_ledger import SQLiteContextCapsuleLedger
from .orchestration.turn_context import (
    ManagedThreadCapsuleRef,
    render_context_capsule_for_prompt,
)


@dataclass(frozen=True)
class PlannedContextCapsules:
    rendered_text: str
    capsule_refs: tuple[ManagedThreadCapsuleRef, ...]
    plans: tuple[ContextCapsuleRenderPlan, ...]

    @property
    def rendered(self) -> bool:
        return bool(self.rendered_text.strip())


def plan_context_capsules_for_prompt(
    capsules: Iterable[ContextCapsule | None],
    *,
    ledger: SQLiteContextCapsuleLedger,
    surface_kind: str,
    surface_key: str,
    managed_thread_id: str,
    backend_thread_id: str | None = None,
    repo_id: str | None = None,
    worktree_id: str | None = None,
    turn_id: str | None = None,
    force_refresh: bool = False,
    record_rendered: bool = False,
) -> PlannedContextCapsules:
    """Plan and render model context capsules through the durable ledger."""
    rendered: list[str] = []
    refs: list[ManagedThreadCapsuleRef] = []
    plans: list[ContextCapsuleRenderPlan] = []
    for capsule in capsules:
        if capsule is None:
            continue
        plan = ledger.plan_render(
            capsule,
            surface_kind=surface_kind,
            surface_key=surface_key,
            managed_thread_id=managed_thread_id,
            backend_thread_id=ledger_backend_thread_id_for_scope(
                capsule,
                backend_thread_id=backend_thread_id,
            ),
            scope_id=ledger_scope_id_for_capsule(
                capsule,
                repo_id=repo_id,
                worktree_id=worktree_id,
                managed_thread_id=managed_thread_id,
                backend_thread_id=backend_thread_id,
                turn_id=turn_id,
            ),
            force_refresh=force_refresh,
        )
        plans.append(plan)
        refs.append(ManagedThreadCapsuleRef.from_render_plan(plan))
        if not plan.should_render:
            continue
        text = render_context_capsule_for_prompt(capsule)
        if text.strip():
            rendered.append(text)
    planned = PlannedContextCapsules(
        rendered_text="\n\n".join(rendered),
        capsule_refs=tuple(refs),
        plans=tuple(plans),
    )
    if record_rendered:
        record_context_capsule_renders(ledger, planned.plans)
    return planned


def record_context_capsule_renders(
    ledger: SQLiteContextCapsuleLedger,
    plans: Iterable[ContextCapsuleRenderPlan],
) -> None:
    for plan in plans:
        if plan.should_render:
            ledger.record_render(plan)


__all__ = [
    "PlannedContextCapsules",
    "plan_context_capsules_for_prompt",
    "record_context_capsule_renders",
]
