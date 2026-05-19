from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping, Optional

from ..context_capsules import ContextCapsule, ContextCapsuleRenderPlan
from ..injected_context import wrap_injected_context


@dataclass(frozen=True)
class ManagedThreadCapsuleRef:
    capsule_id: str
    capsule_version: str
    visibility: str
    scope: str
    source_digest: str
    payload_digest: Optional[str] = None
    render_decision: Optional[str] = None
    reason: Optional[str] = None

    @classmethod
    def from_capsule(
        cls,
        capsule: ContextCapsule,
        *,
        payload_digest: Optional[str] = None,
        render_decision: Optional[str] = None,
    ) -> "ManagedThreadCapsuleRef":
        return cls(
            capsule_id=capsule.capsule_id,
            capsule_version=capsule.capsule_version,
            visibility=capsule.visibility.value,
            scope=capsule.scope.value,
            source_digest=capsule.source_digest,
            payload_digest=payload_digest,
            render_decision=render_decision,
            reason=capsule.reason,
        )

    @classmethod
    def from_render_plan(
        cls, plan: ContextCapsuleRenderPlan
    ) -> "ManagedThreadCapsuleRef":
        return cls.from_capsule(
            plan.capsule,
            payload_digest=plan.payload_digest,
            render_decision=plan.decision.value,
        )

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass(frozen=True)
class ManagedThreadTurnAssembly:
    raw_model_prompt: str
    user_visible_text: str
    title_seed: str
    capsule_refs: tuple[ManagedThreadCapsuleRef, ...] = ()

    def metadata_patch(self) -> dict[str, Any]:
        return {
            "raw_model_prompt": self.raw_model_prompt,
            "user_visible_text": self.user_visible_text,
            "title_seed": self.title_seed,
            "capsule_refs": [ref.to_dict() for ref in self.capsule_refs],
        }


def render_context_capsule_for_prompt(capsule: ContextCapsule) -> str:
    """Render a structured capsule for runtimes without native context channels."""
    payload_text = capsule.payload.get("text")
    text = str(payload_text).strip() if payload_text is not None else ""
    if not text:
        return ""
    return wrap_injected_context(text)


def capsule_refs_from_values(
    values: Iterable[Any],
) -> tuple[ManagedThreadCapsuleRef, ...]:
    refs: list[ManagedThreadCapsuleRef] = []
    for value in values:
        if isinstance(value, ManagedThreadCapsuleRef):
            refs.append(value)
        elif isinstance(value, ContextCapsuleRenderPlan):
            refs.append(ManagedThreadCapsuleRef.from_render_plan(value))
        elif isinstance(value, ContextCapsule):
            refs.append(ManagedThreadCapsuleRef.from_capsule(value))
        elif isinstance(value, Mapping):
            capsule_id = _optional_text(value.get("capsule_id"))
            capsule_version = _optional_text(
                value.get("capsule_version") or value.get("version")
            )
            visibility = _optional_text(value.get("visibility"))
            scope = _optional_text(value.get("scope"))
            source_digest = _optional_text(value.get("source_digest"))
            if not (
                capsule_id
                and capsule_version
                and visibility
                and scope
                and source_digest
            ):
                continue
            refs.append(
                ManagedThreadCapsuleRef(
                    capsule_id=capsule_id,
                    capsule_version=capsule_version,
                    visibility=visibility,
                    scope=scope,
                    source_digest=source_digest,
                    payload_digest=_optional_text(value.get("payload_digest")),
                    render_decision=_optional_text(value.get("render_decision")),
                    reason=_optional_text(value.get("reason")),
                )
            )
    return tuple(refs)


def assemble_managed_thread_turn(
    *,
    runtime_prompt: str,
    user_visible_text: str,
    title_seed: Optional[str] = None,
    capsule_refs: Iterable[Any] = (),
) -> ManagedThreadTurnAssembly:
    visible = str(user_visible_text or "")
    resolved_title_seed = str(title_seed if title_seed is not None else visible)
    return ManagedThreadTurnAssembly(
        raw_model_prompt=str(runtime_prompt or ""),
        user_visible_text=visible,
        title_seed=resolved_title_seed,
        capsule_refs=capsule_refs_from_values(capsule_refs),
    )


def turn_assembly_from_request_metadata(
    *,
    message_text: str,
    metadata: Mapping[str, Any],
) -> ManagedThreadTurnAssembly:
    runtime_prompt = _present_text(metadata.get("raw_model_prompt"))
    if runtime_prompt is None:
        runtime_prompt = _present_text(metadata.get("runtime_prompt"))
    if runtime_prompt is None:
        runtime_prompt = message_text
    user_visible_text = _present_text(metadata.get("user_visible_text")) or message_text
    title_seed = _present_text(metadata.get("title_seed")) or user_visible_text
    raw_capsule_refs = metadata.get("capsule_refs")
    capsule_refs = raw_capsule_refs if isinstance(raw_capsule_refs, list) else ()
    return assemble_managed_thread_turn(
        runtime_prompt=runtime_prompt,
        user_visible_text=user_visible_text,
        title_seed=title_seed,
        capsule_refs=capsule_refs,
    )


def _optional_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _present_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    return value if value.strip() else None


__all__ = [
    "ManagedThreadCapsuleRef",
    "ManagedThreadTurnAssembly",
    "assemble_managed_thread_turn",
    "capsule_refs_from_values",
    "render_context_capsule_for_prompt",
    "turn_assembly_from_request_metadata",
]
