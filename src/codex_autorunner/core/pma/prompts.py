from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..car_context import build_car_context_capsule
from ..managed_thread_kinds import (
    MANAGED_THREAD_CHAT_KIND_PMA,
    ManagedThreadChatKind,
    normalize_managed_thread_chat_kind,
)
from ..orchestration.turn_context import (
    ManagedThreadCapsuleRef,
    render_context_capsule_for_prompt,
)
from ..pma_context import format_pma_discoverability_preamble


@dataclass(frozen=True)
class ManagedThreadPromptRequest:
    agent: Any
    hub_root: Path
    runtime_cwd: Path | None
    stored_backend_id: str | None
    compact_seed: str | None
    message: str
    context_bundle: Any
    chat_kind: ManagedThreadChatKind = MANAGED_THREAD_CHAT_KIND_PMA


@dataclass(frozen=True)
class ManagedThreadPromptAssembly:
    prompt: str
    capsule_refs: tuple[ManagedThreadCapsuleRef, ...]


def compose_compacted_prompt(compact_seed: str, message: str) -> str:
    return (
        "Context summary (from compaction):\n"
        f"{compact_seed}\n\n"
        "User message:\n"
        f"{message}"
    )


def compose_managed_thread_execution_prompt_with_capsules(
    request: ManagedThreadPromptRequest,
) -> ManagedThreadPromptAssembly:
    execution_message = request.message
    if not request.stored_backend_id and request.compact_seed:
        execution_message = compose_compacted_prompt(
            request.compact_seed,
            request.message,
        )

    chat_kind = normalize_managed_thread_chat_kind(request.chat_kind)
    if chat_kind == MANAGED_THREAD_CHAT_KIND_PMA:
        preamble = format_pma_discoverability_preamble(
            hub_root=request.hub_root,
            runtime_cwd=request.runtime_cwd,
        )
    else:
        runtime_text = (
            f"Runtime cwd: `{request.runtime_cwd.expanduser().resolve()}`.\n"
            if request.runtime_cwd is not None
            else ""
        )
        preamble = (
            f"Hub root: `{request.hub_root.expanduser().resolve()}`.\n{runtime_text}\n"
        )
    user_message = f"<user_message>\n{execution_message}\n</user_message>\n"
    capsule = build_car_context_capsule(request.context_bundle)
    car_context = render_context_capsule_for_prompt(capsule) if capsule else ""
    capsule_refs = (
        (ManagedThreadCapsuleRef.from_capsule(capsule),) if capsule is not None else ()
    )
    if not car_context:
        return ManagedThreadPromptAssembly(
            prompt=f"{preamble}{user_message}",
            capsule_refs=capsule_refs,
        )
    return ManagedThreadPromptAssembly(
        prompt=f"{preamble}{car_context}\n\n{user_message}",
        capsule_refs=capsule_refs,
    )


def compose_managed_thread_execution_prompt(
    request: ManagedThreadPromptRequest,
) -> str:
    return compose_managed_thread_execution_prompt_with_capsules(request).prompt


__all__ = [
    "ManagedThreadPromptAssembly",
    "ManagedThreadPromptRequest",
    "compose_compacted_prompt",
    "compose_managed_thread_execution_prompt",
    "compose_managed_thread_execution_prompt_with_capsules",
]
