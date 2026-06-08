from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ..artifact_instructions import (
    ArtifactDeliveryCommands,
    ArtifactDeliveryContext,
    render_agent_artifact_instructions,
)
from ..car_context import (
    build_car_context_bundle,
    build_car_context_capsule,
    default_managed_thread_context_profile,
    normalize_car_context_profile,
)
from ..context_capsule_planner import plan_context_capsules_for_prompt
from ..context_capsules import ContextCapsuleRenderPlan
from ..filebox import inbox_dir
from ..managed_thread_kinds import infer_managed_thread_chat_kind
from ..orchestration.context_capsule_ledger import SQLiteContextCapsuleLedger
from ..orchestration.sqlite import open_orchestration_sqlite
from ..text_utils import _normalize_optional_text
from .attachments import (
    build_managed_thread_attachment_execution_context,
    normalize_managed_thread_attachments,
)
from .policies import BusyPolicy, normalize_busy_policy, validate_max_text_chars
from .prompts import (
    ManagedThreadPromptRequest,
    compose_managed_thread_execution_prompt_with_capsules,
)


@dataclass(frozen=True)
class ManagedThreadMessageInput:
    message: Any
    busy_policy: Any
    notify_on: Optional[str]
    notify_lane: Optional[str]
    notify_once: bool
    notify_required: bool
    defer_execution: bool
    model: Optional[str]
    reasoning: Optional[str]
    agent_profile: Optional[str]
    attachments: Any
    defaults: dict[str, Any]
    thread: dict[str, Any]
    managed_thread_id: str
    hub_root: Path
    runtime_cwd: Path | None
    live_backend_thread_id: str
    approval_policy: Optional[str]
    sandbox_policy: Optional[Any]


@dataclass(frozen=True)
class ManagedThreadMessageOptions:
    busy_policy: BusyPolicy
    message: str
    notify_on: Optional[str]
    notify_lane: Optional[str]
    notify_once: bool
    notify_required: bool
    defer_execution: bool
    model: Optional[str]
    reasoning: Optional[str]
    agent_profile: Optional[str]
    context_profile: Any
    context_bundle: Any
    approval_policy: Optional[str]
    sandbox_policy: Optional[Any]
    live_backend_thread_id: str
    execution_prompt: str
    capsule_refs: tuple[dict[str, Any], ...]
    capsule_render_plans: tuple[ContextCapsuleRenderPlan, ...]
    execution_input_items: Optional[list[dict[str, Any]]]
    delivery_payload: dict[str, Any]


WEB_ARTIFACT_SURFACE = "web"


def web_artifact_conversation_key(managed_thread_id: str) -> str:
    """Conversation key used for web (managed-thread) artifact deliveries."""

    return f"managed_thread:{managed_thread_id}"


def _build_web_artifact_instructions(input: ManagedThreadMessageInput) -> str:
    workspace_root = input.runtime_cwd or _normalize_optional_text(
        input.thread.get("workspace_root")
    )
    workspace_scope = f"repo:{workspace_root}" if workspace_root else None
    conversation_key = web_artifact_conversation_key(input.managed_thread_id)
    # Pin --root to the thread's workspace root so the delivery journal the agent
    # writes to is exactly the one the web post-turn drain reads. Without this the
    # CLI resolves the journal via find_repo_root(cwd), which can diverge from the
    # thread workspace_root (e.g. a hub-root thread whose agent runs in a nested
    # repo), leaving deliveries stuck pending.
    root_flag = f" --root {shlex.quote(str(workspace_root))}" if workspace_root else ""
    explicit_send = (
        f"car artifacts send <file>{root_flag} --to explicit "
        f"--surface {WEB_ARTIFACT_SURFACE} --conversation {conversation_key}"
    )
    list_deliveries = f"car artifacts list{root_flag}"
    upload_inbox: Path | None = None
    if workspace_root is not None:
        upload_inbox = inbox_dir(Path(workspace_root))
    return render_agent_artifact_instructions(
        ArtifactDeliveryContext(
            surface=WEB_ARTIFACT_SURFACE,
            conversation_key=conversation_key,
            workspace_scope=workspace_scope,
            scope_label="this web chat thread",
            user_upload_inbox=upload_inbox,
            extra_agent_lines=(
                "Delivered files appear as downloadable attachments in this web chat.",
            ),
        ),
        commands=ArtifactDeliveryCommands(
            send_current=explicit_send,
            list_deliveries=list_deliveries,
        ),
    )


def resolve_managed_thread_message_options(
    input: ManagedThreadMessageInput,
) -> ManagedThreadMessageOptions:
    busy_policy = normalize_busy_policy(input.busy_policy)
    message = str(input.message or "")
    attachments = normalize_managed_thread_attachments(input.attachments)
    if not message.strip() and not attachments:
        raise ValueError("message is required")
    max_text_chars = int(input.defaults.get("max_text_chars", 0) or 0)
    validate_max_text_chars(message, max_text_chars)

    attachment_context = build_managed_thread_attachment_execution_context(
        attachments,
        hub_root=input.hub_root,
    )
    execution_message = message
    if attachment_context is not None:
        execution_message = (
            f"{message}\n\n{attachment_context.prompt_text}"
            if message.strip()
            else attachment_context.prompt_text
        )
    artifact_instructions = _build_web_artifact_instructions(input)
    execution_message = (
        f"{execution_message}\n\n{artifact_instructions}"
        if execution_message.strip()
        else artifact_instructions
    )
    model = _normalize_optional_text(input.model) or input.defaults.get("model")
    reasoning = _normalize_optional_text(input.reasoning) or input.defaults.get(
        "reasoning"
    )
    compact_seed = _normalize_optional_text(input.thread.get("compact_seed"))
    metadata = input.thread.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    agent_profile = _normalize_optional_text(input.agent_profile)
    chat_kind = infer_managed_thread_chat_kind(
        metadata=metadata,
        display_name=input.thread.get("display_name") or input.thread.get("name"),
    )
    context_profile = normalize_car_context_profile(
        input.thread.get("context_profile") or metadata.get("context_profile"),
        default=default_managed_thread_context_profile(),
    )
    context_bundle = build_car_context_bundle(
        context_profile,
        prompt_text=message,
    )
    context_capsule = build_car_context_capsule(context_bundle)
    with open_orchestration_sqlite(input.hub_root) as conn:
        planned_context = plan_context_capsules_for_prompt(
            (context_capsule,),
            ledger=SQLiteContextCapsuleLedger(conn),
            surface_kind="web",
            surface_key=input.managed_thread_id,
            managed_thread_id=input.managed_thread_id,
            backend_thread_id=input.live_backend_thread_id,
            repo_id=_normalize_optional_text(input.thread.get("repo_id")),
            worktree_id=_normalize_optional_text(input.thread.get("workspace_root")),
        )
    prompt_assembly = compose_managed_thread_execution_prompt_with_capsules(
        ManagedThreadPromptRequest(
            agent=input.thread.get("agent"),
            hub_root=input.hub_root,
            runtime_cwd=input.runtime_cwd,
            stored_backend_id=input.live_backend_thread_id,
            compact_seed=compact_seed,
            message=execution_message,
            context_bundle=context_bundle,
            rendered_context=planned_context.rendered_text,
            capsule_refs=planned_context.capsule_refs,
            chat_kind=chat_kind,
        )
    )
    execution_prompt = prompt_assembly.prompt

    delivery_payload: dict[str, Any] = {"delivered_message": message}
    if attachments:
        delivery_payload["attachments"] = attachments

    return ManagedThreadMessageOptions(
        busy_policy=busy_policy,
        message=message,
        notify_on=input.notify_on,
        notify_lane=input.notify_lane,
        notify_once=input.notify_once,
        notify_required=input.notify_required,
        defer_execution=bool(input.defer_execution),
        model=model,
        reasoning=reasoning,
        agent_profile=agent_profile,
        context_profile=context_profile,
        context_bundle=context_bundle,
        approval_policy=input.approval_policy,
        sandbox_policy=input.sandbox_policy,
        live_backend_thread_id=input.live_backend_thread_id,
        execution_prompt=execution_prompt,
        capsule_refs=tuple(ref.to_dict() for ref in prompt_assembly.capsule_refs),
        capsule_render_plans=planned_context.plans,
        execution_input_items=(
            attachment_context.input_items if attachment_context is not None else None
        ),
        delivery_payload=delivery_payload,
    )


__all__ = [
    "ManagedThreadMessageInput",
    "ManagedThreadMessageOptions",
    "WEB_ARTIFACT_SURFACE",
    "resolve_managed_thread_message_options",
    "web_artifact_conversation_key",
]
