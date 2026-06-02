from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ..context_awareness import (
    PlannedPromptInjection,
    plan_worktree_pr_hint_injection,
    record_planned_prompt_injection,
)
from ..managed_thread_identity import pma_automation_key, pma_base_key
from ..pma_context import format_pma_prompt
from ..pma_origin import PmaOriginContext, extract_pma_origin_metadata


@dataclass(frozen=True)
class PmaExecutionOrigin:
    session_key: str
    backend_thread_id: Optional[str] = None


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


GitHubContextInjector = Callable[..., Awaitable[tuple[str, Any]]]


@dataclass(frozen=True)
class PmaQueuePromptInputs:
    prompt_base: str
    snapshot: Any
    message: str
    hub_root: Path
    prompt_state_key: str
    github_context_injector: GitHubContextInjector | None = None
    logger: Any = None


def build_queue_execution_prompt(
    inputs: PmaQueuePromptInputs,
    *,
    force_full_base_prompt: bool = False,
) -> str | Awaitable[str]:
    built = format_pma_prompt(
        inputs.prompt_base,
        inputs.snapshot,
        inputs.message,
        hub_root=inputs.hub_root,
        prompt_state_key=inputs.prompt_state_key,
        force_full_base_prompt=force_full_base_prompt,
        user_input_texts=[inputs.message],
    )
    planned_pma_injections: tuple[PlannedPromptInjection, ...] = ()
    # Queue prompts do not use the two-variant PMA builder, so collect equivalent
    # stable hint plans after prompt rendering and record them from this lifecycle.
    planned_worktree = plan_worktree_pr_hint_injection(
        inputs.message,
        hub_root=inputs.hub_root,
        surface_kind="pma",
        surface_key=inputs.prompt_state_key,
        managed_thread_id=inputs.prompt_state_key,
        user_input_texts=[inputs.message],
    )
    if planned_worktree.injected:
        planned_pma_injections = (planned_worktree,)
    if inputs.github_context_injector is None:
        for planned in planned_pma_injections:
            record_planned_prompt_injection(
                inputs.hub_root,
                planned.prompt_text,
                planned.render_plans,
            )
        return built
    return _inject_github_context(
        inputs,
        built,
        planned_pma_injections=planned_pma_injections,
    )


async def _inject_github_context(
    inputs: PmaQueuePromptInputs,
    built: str,
    *,
    planned_pma_injections: tuple[PlannedPromptInjection, ...] = (),
) -> str:
    assert inputs.github_context_injector is not None
    planned_injections: list[PlannedPromptInjection] = []
    injected, _ = await inputs.github_context_injector(
        prompt_text=built,
        link_source_text=inputs.message,
        workspace_root=inputs.hub_root,
        logger=inputs.logger,
        event_prefix="web.pma.github_context",
        allow_cross_repo=True,
        hub_root=inputs.hub_root,
        surface_kind="pma",
        surface_key=inputs.prompt_state_key,
        managed_thread_id=inputs.prompt_state_key,
        planned_injections=planned_injections,
    )
    for planned in planned_injections:
        record_planned_prompt_injection(
            inputs.hub_root,
            planned.prompt_text,
            planned.render_plans,
        )
    for planned in planned_pma_injections:
        record_planned_prompt_injection(
            inputs.hub_root,
            planned.prompt_text,
            planned.render_plans,
        )
    return injected


__all__ = [
    "PmaExecutionOrigin",
    "PmaQueuePromptInputs",
    "build_queue_execution_prompt",
    "resolve_pma_execution_origin",
    "resolve_pma_session_key",
]
