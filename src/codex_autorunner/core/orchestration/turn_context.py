from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Literal, Mapping, Optional

from ..context_capsules import ContextCapsule, ContextCapsuleRenderPlan
from ..injected_context import render_legacy_injected_context_transport
from .models import BusyThreadPolicy, MessageRequest, MessageRequestKind


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


@dataclass(frozen=True)
class ChatTurnDeliveryTarget:
    surface_kind: str
    surface_key: str

    def to_dict(self) -> dict[str, str]:
        return {"surface_kind": self.surface_kind, "surface_key": self.surface_key}


@dataclass(frozen=True)
class ChatTurnSource:
    surface_kind: str
    surface_key: str
    message_id: Optional[str] = None
    thread_id: Optional[str] = None
    update_id: Optional[str] = None

    def metadata_patch(self) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "source_surface_kind": self.surface_kind,
            "source_surface_key": self.surface_key,
        }
        if self.message_id:
            metadata["source_message_id"] = self.message_id
        if self.thread_id:
            metadata["source_thread_id"] = self.thread_id
        if self.update_id:
            metadata["source_update_id"] = self.update_id
        return metadata


@dataclass(frozen=True)
class ChatTurnEnvelope:
    """Typed surface-to-runtime turn submission contract.

    The visible text is the user-facing transcript/title seed. The runtime
    prompt is the model-facing prompt and may include injected context.
    """

    source: ChatTurnSource
    user_visible_text: str
    runtime_prompt: str
    title_seed: str
    busy_policy: BusyThreadPolicy = "queue"
    kind: MessageRequestKind = "message"
    input_items: Optional[list[dict[str, Any]]] = None
    model_context_refs: tuple[ManagedThreadCapsuleRef, ...] = ()
    delivery_targets: tuple[ChatTurnDeliveryTarget, ...] = ()
    existing_session_runtime_prompt: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        visible = str(self.user_visible_text or "")
        runtime_prompt = str(self.runtime_prompt or "")
        title_seed = str(self.title_seed or visible)
        if not visible.strip():
            raise ValueError("ChatTurnEnvelope requires user_visible_text")
        if not runtime_prompt.strip():
            raise ValueError("ChatTurnEnvelope requires runtime_prompt")
        if not title_seed.strip():
            raise ValueError("ChatTurnEnvelope requires title_seed")
        object.__setattr__(self, "user_visible_text", visible)
        object.__setattr__(self, "runtime_prompt", runtime_prompt)
        object.__setattr__(self, "title_seed", title_seed)
        object.__setattr__(
            self,
            "model_context_refs",
            capsule_refs_from_values(self.model_context_refs),
        )
        if self.input_items is not None:
            object.__setattr__(
                self,
                "input_items",
                [dict(item) for item in self.input_items if isinstance(item, dict)]
                or None,
            )

    def metadata_patch(self) -> dict[str, Any]:
        metadata = dict(self.metadata)
        metadata.update(
            {
                "runtime_prompt": self.runtime_prompt,
                "raw_model_prompt": self.runtime_prompt,
                "user_visible_text": self.user_visible_text,
                "title_seed": self.title_seed,
                "source": self.source.metadata_patch(),
            }
        )
        metadata.update(self.source.metadata_patch())
        if self.model_context_refs:
            metadata["capsule_refs"] = [
                ref.to_dict() for ref in self.model_context_refs
            ]
        if self.delivery_targets:
            metadata["delivery_targets"] = [
                target.to_dict() for target in self.delivery_targets
            ]
        if (
            isinstance(self.existing_session_runtime_prompt, str)
            and self.existing_session_runtime_prompt.strip()
        ):
            metadata["existing_session_runtime_prompt"] = (
                self.existing_session_runtime_prompt
            )
        return metadata

    def to_message_request(
        self,
        *,
        target_id: str,
        target_kind: Literal["thread", "flow"] = "thread",
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        approval_mode: Optional[str] = None,
        input_items: Optional[list[dict[str, Any]]] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> MessageRequest:
        request_metadata = self.metadata_patch()
        if metadata:
            request_metadata.update(dict(metadata))
        return MessageRequest(
            target_id=target_id,
            target_kind=target_kind,
            message_text=self.user_visible_text,
            kind=self.kind,
            busy_policy=self.busy_policy,
            model=model,
            reasoning=reasoning,
            approval_mode=approval_mode,
            input_items=input_items if input_items is not None else self.input_items,
            metadata=request_metadata,
        )


def render_context_capsule_for_prompt(capsule: ContextCapsule) -> str:
    """Render a structured capsule for runtimes without native context channels."""
    payload_text = capsule.payload.get("text")
    text = str(payload_text).strip() if payload_text is not None else ""
    if not text:
        return ""
    return render_legacy_injected_context_transport(text)


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
    "ChatTurnDeliveryTarget",
    "ChatTurnEnvelope",
    "ChatTurnSource",
    "assemble_managed_thread_turn",
    "capsule_refs_from_values",
    "render_context_capsule_for_prompt",
    "turn_assembly_from_request_metadata",
]
