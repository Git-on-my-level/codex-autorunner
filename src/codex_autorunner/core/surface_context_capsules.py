from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .artifact_instructions import (
    ArtifactDeliveryContext,
    render_agent_artifact_instructions,
)
from .context_capsules import (
    ContextCapsule,
    ContextCapsuleExpiry,
    ContextCapsuleScope,
    ContextCapsuleVisibility,
    stable_json_digest,
)
from .injected_context import INJECTED_CONTEXT_END, INJECTED_CONTEXT_START
from .orchestration.turn_context import render_context_capsule_for_prompt


def build_model_only_text_capsule(
    *,
    capsule_id: str,
    text: str,
    reason: str,
    source: Mapping[str, Any] | None = None,
    version: int = 1,
    scope: ContextCapsuleScope = ContextCapsuleScope.TURN,
    expiry: ContextCapsuleExpiry = ContextCapsuleExpiry.TURN_SCOPED,
) -> ContextCapsule | None:
    normalized = _capsule_text(text)
    if not normalized:
        return None
    payload: dict[str, Any] = {"text": normalized}
    if source:
        payload["source"] = _jsonable_source(source)
    return ContextCapsule(
        capsule_id=capsule_id,
        version=version,
        scope=scope,
        visibility=ContextCapsuleVisibility.MODEL_ONLY,
        source_digest=stable_json_digest(payload),
        expiry=expiry,
        reason=reason,
        payload=payload,
    )


def build_artifact_delivery_capsule(
    context: ArtifactDeliveryContext,
    *,
    capsule_id: str = "artifact_delivery.current_turn",
    reason: str = "artifact_delivery_hint",
) -> ContextCapsule:
    return _required_capsule(
        build_model_only_text_capsule(
            capsule_id=capsule_id,
            text=render_agent_artifact_instructions(context),
            reason=reason,
            source={
                "surface": context.surface,
                "conversation_key": context.conversation_key,
                "workspace_scope": context.workspace_scope,
                "scope_label": context.scope_label,
                "user_upload_inbox": context.user_upload_inbox,
                "extra_agent_lines": context.extra_agent_lines,
            },
        )
    )


def build_attachment_manifest_capsule(
    *,
    surface: str,
    text: str,
    reason: str,
    source: Mapping[str, Any] | None = None,
) -> ContextCapsule:
    return _required_capsule(
        build_model_only_text_capsule(
            capsule_id=f"attachments.{surface}.current_turn",
            text=text,
            reason=reason,
            source=source,
        )
    )


def build_whisper_disclaimer_capsule(
    *,
    surface: str,
    disclaimer: str,
    provider: str,
) -> ContextCapsule | None:
    if provider != "openai_whisper":
        return None
    return build_model_only_text_capsule(
        capsule_id=f"transcription.{surface}.whisper_disclaimer",
        text=disclaimer,
        reason=f"audio_transcription_provider:{provider}",
        source={"surface": surface, "provider": provider, "disclaimer": disclaimer},
    )


def build_github_context_capsule(
    *,
    hint_text: str,
    url: str,
    path: str | None,
    kind: str | None = None,
    reason: str = "github_link_context",
) -> ContextCapsule:
    return _required_capsule(
        build_model_only_text_capsule(
            capsule_id="github.context_file",
            text=hint_text,
            reason=reason,
            source={"url": url, "path": path, "kind": kind},
        )
    )


def build_prompt_writing_capsule(text: str) -> ContextCapsule:
    return _required_capsule(
        build_model_only_text_capsule(
            capsule_id="prompt.formatting",
            text=text,
            reason="prompt_keyword_detected",
            scope=ContextCapsuleScope.THREAD,
            expiry=ContextCapsuleExpiry.WHEN_SOURCE_CHANGES,
        )
    )


def render_capsules_for_prompt(capsules: Iterable[ContextCapsule | None]) -> str:
    rendered = [
        render_context_capsule_for_prompt(capsule)
        for capsule in capsules
        if capsule is not None
    ]
    return "\n\n".join(text for text in rendered if text.strip())


def append_capsules_to_prompt(
    prompt_text: str, capsules: Iterable[ContextCapsule | None]
) -> tuple[str, bool]:
    injection = render_capsules_for_prompt(capsules)
    if not injection:
        return prompt_text, False
    if prompt_text.strip():
        separator = "\n" if prompt_text.endswith("\n") else "\n\n"
        return f"{prompt_text}{separator}{injection}", True
    return injection, True


def _capsule_text(text: str) -> str:
    return (
        str(text or "")
        .replace(INJECTED_CONTEXT_START, "")
        .replace(INJECTED_CONTEXT_END, "")
        .strip()
    )


def _jsonable_source(source: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _jsonable_value(value) for key, value in source.items()}


def _jsonable_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [_jsonable_value(item) for item in value]
    if isinstance(value, list):
        return [_jsonable_value(item) for item in value]
    if isinstance(value, Mapping):
        return _jsonable_source(value)
    return value


def _required_capsule(capsule: ContextCapsule | None) -> ContextCapsule:
    if capsule is None:
        raise ValueError("capsule text is required")
    return capsule


__all__ = [
    "append_capsules_to_prompt",
    "build_artifact_delivery_capsule",
    "build_attachment_manifest_capsule",
    "build_github_context_capsule",
    "build_model_only_text_capsule",
    "build_prompt_writing_capsule",
    "build_whisper_disclaimer_capsule",
    "render_capsules_for_prompt",
]
