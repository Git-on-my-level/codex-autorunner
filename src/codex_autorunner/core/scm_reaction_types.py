from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Mapping, Optional

ReactionKind = Literal[
    "ci_failed",
    "changes_requested",
    "approved_and_green",
    "merged",
]
ReactionOperationKind = Literal["enqueue_managed_turn", "notify_chat"]


def _normalize_optional_bool(value: Any) -> Optional[bool]:
    return value if isinstance(value, bool) else None


def _bool_from_mapping(
    mapping: Mapping[str, Any],
    key: str,
    *,
    default: bool,
) -> bool:
    value = _normalize_optional_bool(mapping.get(key))
    return default if value is None else value


@dataclass(frozen=True)
class ScmReactionConfig:
    ci_failed: bool = True
    changes_requested: bool = True
    approved_and_green: bool = True
    merged: bool = True

    @classmethod
    def from_mapping(
        cls,
        value: "ScmReactionConfig | Mapping[str, Any] | None",
    ) -> "ScmReactionConfig":
        if isinstance(value, cls):
            return value
        mapping = value if isinstance(value, Mapping) else {}
        default_enabled = _normalize_optional_bool(mapping.get("enabled"))
        default_value = True if default_enabled is None else default_enabled
        return cls(
            ci_failed=_bool_from_mapping(
                mapping,
                "ci_failed",
                default=default_value,
            ),
            changes_requested=_bool_from_mapping(
                mapping,
                "changes_requested",
                default=default_value,
            ),
            approved_and_green=_bool_from_mapping(
                mapping,
                "approved_and_green",
                default=default_value,
            ),
            merged=_bool_from_mapping(mapping, "merged", default=default_value),
        )

    def is_enabled(self, reaction_kind: ReactionKind) -> bool:
        return bool(getattr(self, reaction_kind))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReactionIntent:
    reaction_kind: ReactionKind
    operation_kind: ReactionOperationKind
    operation_key: str
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: Optional[str] = None
    binding_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def stable_reaction_operation_key(
    *,
    provider: str,
    event_id: str,
    reaction_kind: ReactionKind,
    operation_kind: ReactionOperationKind,
    repo_slug: Optional[str] = None,
    repo_id: Optional[str] = None,
    pr_number: Optional[int] = None,
    binding_id: Optional[str] = None,
    thread_target_id: Optional[str] = None,
) -> str:
    payload = {
        "binding_id": binding_id,
        "event_id": event_id,
        "operation_kind": operation_kind,
        "pr_number": pr_number,
        "provider": provider,
        "reaction_kind": reaction_kind,
        "repo_id": repo_id,
        "repo_slug": repo_slug,
        "thread_target_id": thread_target_id,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]
    return f"scm-reaction:{provider}:{reaction_kind}:{digest}"


__all__ = [
    "ReactionIntent",
    "ReactionKind",
    "ReactionOperationKind",
    "ScmReactionConfig",
    "stable_reaction_operation_key",
]
