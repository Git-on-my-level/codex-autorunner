from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class ContextCapsuleScope(str, Enum):
    REPO = "repo"
    WORKTREE = "worktree"
    THREAD = "thread"
    BACKEND_SESSION = "backend_session"
    TURN = "turn"


class ContextCapsuleVisibility(str, Enum):
    MODEL_ONLY = "model_only"
    TRANSCRIPT_COLLAPSIBLE = "transcript_collapsible"
    USER_VISIBLE = "user_visible"


class ContextCapsuleExpiry(str, Enum):
    ONCE_PER_THREAD = "once_per_thread"
    ONCE_PER_BACKEND_SESSION = "once_per_backend_session"
    WHEN_SOURCE_CHANGES = "when_source_changes"
    EVERY_TURN = "every_turn"
    TURN_SCOPED = "turn_scoped"


class ContextCapsuleRenderDecision(str, Enum):
    SKIP_DUPLICATE = "skip_duplicate"
    NEW = "new"
    CHANGED = "changed"
    EXPIRED = "expired"
    FORCE_REFRESHED = "force_refreshed"


@dataclass(frozen=True)
class ContextCapsule:
    capsule_id: str
    version: int
    scope: ContextCapsuleScope
    visibility: ContextCapsuleVisibility
    source_digest: str
    expiry: ContextCapsuleExpiry
    reason: str
    payload: Mapping[str, Any]

    @property
    def capsule_version(self) -> str:
        return str(self.version)

    @property
    def can_seed_durable_user_fields(self) -> bool:
        return self.scope is not ContextCapsuleScope.TURN and self.expiry not in {
            ContextCapsuleExpiry.TURN_SCOPED,
            ContextCapsuleExpiry.EVERY_TURN,
        }

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "capsule_id": self.capsule_id,
            "version": self.version,
            "scope": self.scope.value,
            "visibility": self.visibility.value,
            "source_digest": self.source_digest,
            "expiry": self.expiry.value,
            "reason": self.reason,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True)
class ContextCapsuleLedgerKey:
    surface_kind: str
    surface_key: str
    managed_thread_id: str
    backend_thread_id: str
    scope_kind: str
    scope_id: str
    capsule_id: str
    capsule_version: str

    @classmethod
    def from_capsule(
        cls,
        capsule: ContextCapsule,
        *,
        surface_kind: str,
        surface_key: str,
        managed_thread_id: str,
        backend_thread_id: str | None,
        scope_id: str,
    ) -> "ContextCapsuleLedgerKey":
        return cls(
            surface_kind=_required_text(surface_kind, "surface_kind"),
            surface_key=_required_text(surface_key, "surface_key"),
            managed_thread_id=_required_text(managed_thread_id, "managed_thread_id"),
            backend_thread_id=(backend_thread_id or "").strip(),
            scope_kind=capsule.scope.value,
            scope_id=_required_text(scope_id, "scope_id"),
            capsule_id=_required_text(capsule.capsule_id, "capsule_id"),
            capsule_version=_required_text(capsule.capsule_version, "capsule_version"),
        )

    def identity_tuple(self) -> tuple[str, ...]:
        return (
            self.surface_kind,
            self.surface_key,
            self.managed_thread_id,
            self.backend_thread_id,
            self.scope_kind,
            self.scope_id,
            self.capsule_id,
            self.capsule_version,
        )

    def stable_id(self) -> str:
        return stable_json_digest(self.identity_tuple())


@dataclass(frozen=True)
class ContextCapsuleLedgerObservation:
    key: ContextCapsuleLedgerKey
    payload_digest: str
    source_digest: str
    expiry: ContextCapsuleExpiry
    visibility: ContextCapsuleVisibility
    reason: str

    @property
    def can_seed_durable_user_fields(self) -> bool:
        return self.expiry not in {
            ContextCapsuleExpiry.TURN_SCOPED,
            ContextCapsuleExpiry.EVERY_TURN,
        }


@dataclass(frozen=True)
class ContextCapsuleRenderPlan:
    capsule: ContextCapsule
    key: ContextCapsuleLedgerKey
    payload_digest: str
    decision: ContextCapsuleRenderDecision

    @property
    def should_render(self) -> bool:
        return self.decision is not ContextCapsuleRenderDecision.SKIP_DUPLICATE

    @property
    def can_seed_durable_user_fields(self) -> bool:
        return self.capsule.can_seed_durable_user_fields


def ledger_backend_thread_id_for_scope(
    capsule: ContextCapsule,
    *,
    backend_thread_id: str | None,
) -> str:
    """Return the backend id component that belongs in a capsule ledger key."""
    if capsule.scope is ContextCapsuleScope.BACKEND_SESSION:
        return (backend_thread_id or "").strip()
    return ""


def ledger_scope_id_for_capsule(
    capsule: ContextCapsule,
    *,
    repo_id: str | None = None,
    worktree_id: str | None = None,
    managed_thread_id: str | None = None,
    backend_thread_id: str | None = None,
    turn_id: str | None = None,
) -> str:
    """Resolve the scope id for a capsule from the standard runtime ids."""
    if capsule.scope is ContextCapsuleScope.REPO:
        return _required_text(repo_id or managed_thread_id, "scope_id")
    if capsule.scope is ContextCapsuleScope.WORKTREE:
        return _required_text(worktree_id or managed_thread_id, "scope_id")
    if capsule.scope is ContextCapsuleScope.THREAD:
        return _required_text(managed_thread_id or backend_thread_id, "scope_id")
    if capsule.scope is ContextCapsuleScope.BACKEND_SESSION:
        return _required_text(backend_thread_id or managed_thread_id, "scope_id")
    if capsule.scope is ContextCapsuleScope.TURN:
        return _required_text(turn_id or managed_thread_id, "scope_id")
    return _required_text(managed_thread_id or backend_thread_id, "scope_id")


def stable_json_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def capsule_payload_digest(capsule: ContextCapsule) -> str:
    return stable_json_digest(capsule.canonical_payload())


def plan_capsule_render(
    capsule: ContextCapsule,
    key: ContextCapsuleLedgerKey,
    *,
    previous: ContextCapsuleLedgerObservation | None,
    force_refresh: bool = False,
) -> ContextCapsuleRenderPlan:
    payload_digest = capsule_payload_digest(capsule)
    if force_refresh:
        decision = ContextCapsuleRenderDecision.FORCE_REFRESHED
    elif previous is None:
        decision = ContextCapsuleRenderDecision.NEW
    elif capsule.expiry in {
        ContextCapsuleExpiry.EVERY_TURN,
        ContextCapsuleExpiry.TURN_SCOPED,
    }:
        decision = ContextCapsuleRenderDecision.EXPIRED
    elif previous.payload_digest != payload_digest:
        decision = ContextCapsuleRenderDecision.CHANGED
    elif previous.source_digest != capsule.source_digest:
        decision = ContextCapsuleRenderDecision.CHANGED
    else:
        decision = ContextCapsuleRenderDecision.SKIP_DUPLICATE
    return ContextCapsuleRenderPlan(
        capsule=capsule,
        key=key,
        payload_digest=payload_digest,
        decision=decision,
    )


def _required_text(value: object, field_name: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


__all__ = [
    "ContextCapsule",
    "ContextCapsuleExpiry",
    "ContextCapsuleLedgerKey",
    "ContextCapsuleLedgerObservation",
    "ContextCapsuleRenderDecision",
    "ContextCapsuleRenderPlan",
    "ContextCapsuleScope",
    "ContextCapsuleVisibility",
    "capsule_payload_digest",
    "ledger_backend_thread_id_for_scope",
    "ledger_scope_id_for_capsule",
    "plan_capsule_render",
    "stable_json_digest",
]
