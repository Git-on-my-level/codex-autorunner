from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ..car_context import (
    build_car_context_bundle,
    default_managed_thread_context_profile,
    normalize_car_context_profile,
)
from ..text_utils import _normalize_optional_text
from .attachments import (
    build_managed_thread_attachment_execution_context,
    normalize_managed_thread_attachments,
)
from .policies import BusyPolicy, normalize_busy_policy, validate_max_text_chars
from .prompts import (
    ManagedThreadPromptRequest,
    compose_managed_thread_execution_prompt,
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
    execution_input_items: Optional[list[dict[str, Any]]]
    delivery_payload: dict[str, Any]


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
    model = _normalize_optional_text(input.model) or input.defaults.get("model")
    reasoning = _normalize_optional_text(input.reasoning) or input.defaults.get(
        "reasoning"
    )
    compact_seed = _normalize_optional_text(input.thread.get("compact_seed"))
    metadata = input.thread.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    agent_profile = _normalize_optional_text(input.agent_profile)
    context_profile = normalize_car_context_profile(
        input.thread.get("context_profile") or metadata.get("context_profile"),
        default=default_managed_thread_context_profile(),
    )
    context_bundle = build_car_context_bundle(
        context_profile,
        prompt_text=message,
    )
    execution_prompt = compose_managed_thread_execution_prompt(
        ManagedThreadPromptRequest(
            agent=input.thread.get("agent"),
            hub_root=input.hub_root,
            runtime_cwd=input.runtime_cwd,
            stored_backend_id=input.live_backend_thread_id,
            compact_seed=compact_seed,
            message=execution_message,
            context_bundle=context_bundle,
        )
    )

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
        execution_input_items=(
            attachment_context.input_items if attachment_context is not None else None
        ),
        delivery_payload=delivery_payload,
    )


__all__ = [
    "ManagedThreadMessageInput",
    "ManagedThreadMessageOptions",
    "resolve_managed_thread_message_options",
]
