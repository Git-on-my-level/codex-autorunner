from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Mapping, Optional

ReactionKind = Literal[
    "ci_failed",
    "changes_requested",
    "review_comment",
    "approved_and_green",
    "merged",
]
ReactionOperationKind = Literal[
    "enqueue_managed_turn",
    "notify_chat",
    "react_pr_review_comment",
]
ScmMessageSourceKind = Literal[
    "scm_reaction_message_builder",
    "static_payload",
]
ReactionProfile = Literal["all", "minimal_noise"]


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


def _int_from_mapping(
    mapping: Mapping[str, Any],
    key: str,
    *,
    default: int,
    minimum: int = 0,
) -> int:
    value = mapping.get(key)
    if isinstance(value, bool):
        return max(int(value), minimum)
    if isinstance(value, int):
        return max(value, minimum)
    if not isinstance(value, str):
        return default
    try:
        normalized = int(value)
    except ValueError:
        return default
    return max(normalized, minimum)


def _normalize_login(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized or None


def _login_list_from_mapping(mapping: Mapping[str, Any], *keys: str) -> tuple[str, ...]:
    seen: set[str] = set()
    logins: list[str] = []
    for key in keys:
        raw_value = mapping.get(key)
        candidates: list[Any]
        if isinstance(raw_value, str):
            candidates = [raw_value]
        elif isinstance(raw_value, (list, tuple, set)):
            candidates = list(raw_value)
        else:
            continue
        for candidate in candidates:
            normalized = _normalize_login(candidate)
            if normalized is None or normalized in seen:
                continue
            seen.add(normalized)
            logins.append(normalized)
    return tuple(logins)


@dataclass(frozen=True)
class ScmReactionConfig:
    ci_failed: bool = True
    ci_failed_batch_window_seconds: int = 60
    ci_failed_batch_max_window_seconds: int = 180
    changes_requested: bool = True
    review_comment: bool = True
    review_comment_batch_window_seconds: int = 15
    approved_and_green: bool = True
    merged: bool = True
    duplicate_escalation_threshold: int = 3
    delivery_failure_escalation_threshold: int = 3
    github_login_whitelist: tuple[str, ...] = ()
    github_login_blacklist: tuple[str, ...] = ()

    @classmethod
    def from_mapping(
        cls,
        value: "ScmReactionConfig | Mapping[str, Any] | None",
    ) -> "ScmReactionConfig":
        if isinstance(value, cls):
            return value
        mapping = value if isinstance(value, Mapping) else {}
        reactions = mapping.get("reactions")
        if isinstance(reactions, Mapping):
            mapping = reactions
        profile = cls._profile_from_mapping(mapping)
        defaults = cls._defaults_for_profile(profile)
        default_enabled = _normalize_optional_bool(mapping.get("enabled"))
        default_value = (
            defaults["enabled"] if default_enabled is None else default_enabled
        )
        return cls(
            ci_failed=_bool_from_mapping(
                mapping,
                "ci_failed",
                default=(
                    defaults["ci_failed"] if default_enabled is None else default_value
                ),
            ),
            ci_failed_batch_window_seconds=_int_from_mapping(
                mapping,
                "ci_failed_batch_window_seconds",
                default=60,
            ),
            ci_failed_batch_max_window_seconds=_int_from_mapping(
                mapping,
                "ci_failed_batch_max_window_seconds",
                default=180,
            ),
            changes_requested=_bool_from_mapping(
                mapping,
                "changes_requested",
                default=(
                    defaults["changes_requested"]
                    if default_enabled is None
                    else default_value
                ),
            ),
            review_comment=_bool_from_mapping(
                mapping,
                "review_comment",
                default=(
                    defaults["review_comment"]
                    if default_enabled is None
                    else default_value
                ),
            ),
            review_comment_batch_window_seconds=_int_from_mapping(
                mapping,
                "review_comment_batch_window_seconds",
                default=15,
            ),
            approved_and_green=_bool_from_mapping(
                mapping,
                "approved_and_green",
                default=(
                    defaults["approved_and_green"]
                    if default_enabled is None
                    else default_value
                ),
            ),
            merged=_bool_from_mapping(
                mapping,
                "merged",
                default=(
                    defaults["merged"] if default_enabled is None else default_value
                ),
            ),
            duplicate_escalation_threshold=_int_from_mapping(
                mapping,
                "duplicate_escalation_threshold",
                default=3,
            ),
            delivery_failure_escalation_threshold=_int_from_mapping(
                mapping,
                "delivery_failure_escalation_threshold",
                default=3,
            ),
            github_login_whitelist=_login_list_from_mapping(
                mapping,
                "github_login_whitelist",
                "github_login_allowlist",
                "whitelist",
                "allowlist",
            ),
            github_login_blacklist=_login_list_from_mapping(
                mapping,
                "github_login_blacklist",
                "github_login_denylist",
                "blacklist",
                "denylist",
            ),
        )

    @staticmethod
    def _profile_from_mapping(mapping: Mapping[str, Any]) -> ReactionProfile:
        value = mapping.get("profile")
        if not isinstance(value, str):
            return "all"
        normalized = value.strip().lower()
        return "minimal_noise" if normalized == "minimal_noise" else "all"

    @staticmethod
    def _defaults_for_profile(profile: ReactionProfile) -> dict[str, bool]:
        if profile == "minimal_noise":
            return {
                "enabled": True,
                "ci_failed": True,
                "changes_requested": True,
                "review_comment": True,
                "approved_and_green": False,
                "merged": False,
            }
        return {
            "enabled": True,
            "ci_failed": True,
            "changes_requested": True,
            "review_comment": True,
            "approved_and_green": True,
            "merged": True,
        }

    def is_enabled(self, reaction_kind: ReactionKind) -> bool:
        return bool(getattr(self, reaction_kind))

    def github_login_allowed(self, login: Any) -> bool:
        normalized = _normalize_login(login)
        if self.github_login_whitelist:
            if normalized is None or normalized not in self.github_login_whitelist:
                return False
        if normalized is not None and normalized in self.github_login_blacklist:
            return False
        return True

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


@dataclass(frozen=True)
class ScmMessageDescriptor:
    source_kind: ScmMessageSourceKind
    reaction_kind: ReactionKind
    operation_kind: ReactionOperationKind
    preview: str
    builder: Optional[str] = None
    payload_path: Optional[str] = None

    @classmethod
    def create(
        cls,
        *,
        reaction_kind: ReactionKind,
        operation_kind: ReactionOperationKind,
        preview: str,
        source_kind: ScmMessageSourceKind = "scm_reaction_message_builder",
        builder: Optional[str] = "build_reaction_message",
        payload_path: Optional[str] = None,
    ) -> "ScmMessageDescriptor":
        if source_kind == "scm_reaction_message_builder" and not builder:
            raise ValueError("SCM message descriptor requires builder")
        if source_kind == "static_payload" and not payload_path:
            raise ValueError("SCM static message descriptor requires payload_path")
        if not isinstance(preview, str) or not preview.strip():
            raise ValueError("SCM message descriptor requires preview")
        return cls(
            source_kind=source_kind,
            reaction_kind=reaction_kind,
            operation_kind=operation_kind,
            preview=preview,
            builder=builder,
            payload_path=payload_path,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ScmMessageDescriptor":
        source_kind = _require_literal(
            value.get("source_kind"),
            frozenset({"scm_reaction_message_builder", "static_payload"}),
            "message.source_kind",
        )
        reaction_kind = _require_literal(
            value.get("reaction_kind"),
            _REACTION_KINDS,
            "message.reaction_kind",
        )
        operation_kind = _require_literal(
            value.get("operation_kind"),
            _REACTION_OPERATION_KINDS,
            "message.operation_kind",
        )
        return cls.create(
            source_kind=source_kind,  # type: ignore[arg-type]
            reaction_kind=reaction_kind,  # type: ignore[arg-type]
            operation_kind=operation_kind,  # type: ignore[arg-type]
            preview=_require_text(value.get("preview"), "message.preview"),
            builder=_optional_text(value.get("builder")),
            payload_path=_optional_text(value.get("payload_path")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


@dataclass(frozen=True)
class ScmActionDescriptor:
    reaction_kind: ReactionKind
    operation_kind: ReactionOperationKind
    operation_key: str
    payload: dict[str, Any]
    message: ScmMessageDescriptor
    event_id: Optional[str] = None
    binding_id: Optional[str] = None

    @classmethod
    def create(
        cls,
        *,
        reaction_kind: ReactionKind,
        operation_kind: ReactionOperationKind,
        operation_key: str,
        payload: Mapping[str, Any],
        message: ScmMessageDescriptor,
        event_id: Optional[str] = None,
        binding_id: Optional[str] = None,
    ) -> "ScmActionDescriptor":
        if message.reaction_kind != reaction_kind:
            raise ValueError("SCM action message reaction_kind must match action")
        if message.operation_kind != operation_kind:
            raise ValueError("SCM action message operation_kind must match action")
        return cls(
            reaction_kind=reaction_kind,
            operation_kind=operation_kind,
            operation_key=_require_text(operation_key, "operation_key"),
            payload=dict(payload),
            message=message,
            event_id=_optional_text(event_id),
            binding_id=_optional_text(binding_id),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ScmActionDescriptor":
        reaction_kind = _require_literal(
            value.get("reaction_kind"),
            _REACTION_KINDS,
            "reaction_kind",
        )
        operation_kind = _require_literal(
            value.get("operation_kind"),
            _REACTION_OPERATION_KINDS,
            "operation_kind",
        )
        payload = value.get("payload")
        if not isinstance(payload, Mapping):
            raise ValueError("SCM action payload must be an object")
        message = value.get("message")
        if not isinstance(message, Mapping):
            raise ValueError("SCM action message descriptor is required")
        return cls.create(
            reaction_kind=reaction_kind,  # type: ignore[arg-type]
            operation_kind=operation_kind,  # type: ignore[arg-type]
            operation_key=_require_text(value.get("operation_key"), "operation_key"),
            payload=payload,
            message=ScmMessageDescriptor.from_mapping(message),
            event_id=_optional_text(value.get("event_id")),
            binding_id=_optional_text(value.get("binding_id")),
        )

    def to_intent(self) -> ReactionIntent:
        return ReactionIntent(
            reaction_kind=self.reaction_kind,
            operation_kind=self.operation_kind,
            operation_key=self.operation_key,
            payload=dict(self.payload),
            event_id=self.event_id,
            binding_id=self.binding_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "reaction_kind": self.reaction_kind,
                "operation_kind": self.operation_kind,
                "operation_key": self.operation_key,
                "payload": dict(self.payload),
                "message": self.message.to_dict(),
                "event_id": self.event_id,
                "binding_id": self.binding_id,
            }.items()
            if value is not None
        }


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


_REACTION_KINDS = frozenset(
    {
        "ci_failed",
        "changes_requested",
        "review_comment",
        "approved_and_green",
        "merged",
    }
)
_REACTION_OPERATION_KINDS = frozenset(
    {
        "enqueue_managed_turn",
        "notify_chat",
        "react_pr_review_comment",
    }
)


def _optional_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _require_text(value: Any, field_name: str) -> str:
    text = _optional_text(value)
    if text is None:
        raise ValueError(f"SCM action {field_name} is required")
    return text


def _require_literal(value: Any, allowed: frozenset[str], field_name: str) -> str:
    text = _require_text(value, field_name)
    if text not in allowed:
        raise ValueError(f"Unsupported SCM action {field_name}: {text}")
    return text


__all__ = [
    "ReactionIntent",
    "ReactionKind",
    "ReactionOperationKind",
    "ScmActionDescriptor",
    "ScmMessageDescriptor",
    "ScmReactionConfig",
    "stable_reaction_operation_key",
]
