from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping, Optional

from .scope_urn import (
    format_scope_urn,
    parse_scope_urn,
)


class ScopeRefError(ValueError):
    """Raised when a ScopeRef cannot be constructed."""


@dataclass(frozen=True)
class ScopeRef:
    """Canonical identity for a scoping boundary in CAR."""

    kind: str
    id: Optional[str] = None
    parent_repo_id: Optional[str] = None

    def __post_init__(self) -> None:
        if self.kind == "hub":
            if self.id is not None:
                raise ScopeRefError("hub scope must not have an id")
            if self.parent_repo_id is not None:
                raise ScopeRefError("hub scope must not have a parent_repo_id")
        elif self.kind == "repo":
            if not self.id:
                raise ScopeRefError("repo scope requires an id")
            if self.parent_repo_id is not None:
                raise ScopeRefError("repo scope must not have a parent_repo_id")
        elif self.kind == "worktree":
            if not self.id:
                raise ScopeRefError("worktree scope requires an id")
            if not self.parent_repo_id:
                raise ScopeRefError("worktree scope requires a parent_repo_id")
        elif self.kind == "agent_workspace":
            if not self.id:
                raise ScopeRefError("agent_workspace scope requires an id")
            if self.parent_repo_id is not None:
                raise ScopeRefError(
                    "agent_workspace scope must not have a parent_repo_id"
                )
        else:
            raise ScopeRefError(f"Unknown scope kind: {self.kind}")

    def to_urn(self) -> str:
        return format_scope_urn(
            kind=self.kind, id=self.id, parent_repo_id=self.parent_repo_id
        )

    @classmethod
    def from_urn(cls, urn: str) -> "ScopeRef":
        parts = parse_scope_urn(urn)
        kind = parts["kind"]
        assert isinstance(kind, str)
        return cls(
            kind=kind,
            id=parts["id"],
            parent_repo_id=parts["parent_repo_id"],
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ScopeRef":
        urn = data.get("urn")
        if isinstance(urn, str):
            return cls.from_urn(urn)
        kind = data.get("kind")
        if not isinstance(kind, str):
            repo_id = data.get("repo_id")
            if isinstance(repo_id, str):
                return cls(kind="repo", id=repo_id)
            raise ScopeRefError("ScopeRef requires kind or repo_id")
        return cls(
            kind=kind,
            id=data.get("id"),
            parent_repo_id=data.get("parent_repo_id"),
        )


@dataclass(frozen=True)
class SurfaceRef:
    """Canonical identity for a surface channel."""

    kind: str
    key: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "SurfaceRef":
        kind = data.get("surface_kind") or data.get("kind")
        key = data.get("surface_key") or data.get("key")
        if not isinstance(kind, str):
            raise ValueError("SurfaceRef requires a kind")
        if not isinstance(key, str):
            raise ValueError("SurfaceRef requires a key")
        return cls(kind=kind, key=key)


@dataclass(frozen=True)
class ParticipantRef:
    """Canonical identity for a conversation participant."""

    kind: str
    id: str
    display_name: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ParticipantRef":
        kind = data.get("kind")
        pid = data.get("id")
        if not isinstance(kind, str):
            raise ValueError("ParticipantRef requires a kind")
        if not isinstance(pid, str):
            raise ValueError("ParticipantRef requires an id")
        return cls(
            kind=kind,
            id=pid,
            display_name=data.get("display_name"),
        )


@dataclass(frozen=True)
class AgentRef:
    """Canonical identity for an agent."""

    agent_id: str
    profile: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AgentRef":
        agent_id = data.get("agent_id") or data.get("agent")
        if not isinstance(agent_id, str):
            raise ValueError("AgentRef requires an agent_id")
        return cls(
            agent_id=agent_id,
            profile=data.get("profile") or data.get("agent_profile"),
        )


@dataclass(frozen=True)
class MemoryRef:
    """Canonical identity for a scoped memory store."""

    scope: ScopeRef
    key: str

    def to_dict(self) -> dict[str, Any]:
        return {"scope": self.scope.to_dict(), "key": self.key}

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "MemoryRef":
        scope_data = data.get("scope")
        if isinstance(scope_data, Mapping):
            scope = ScopeRef.from_mapping(scope_data)
        elif isinstance(scope_data, ScopeRef):
            scope = scope_data
        else:
            raise ValueError("MemoryRef requires a scope")
        key = data.get("key")
        if not isinstance(key, str):
            raise ValueError("MemoryRef requires a key")
        return cls(scope=scope, key=key)


@dataclass(frozen=True)
class TicketRef:
    """Canonical identity for a ticket within a scope."""

    scope: ScopeRef
    ticket_id: str

    def to_dict(self) -> dict[str, Any]:
        return {"scope": self.scope.to_dict(), "ticket_id": self.ticket_id}

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "TicketRef":
        scope_data = data.get("scope")
        if isinstance(scope_data, Mapping):
            scope = ScopeRef.from_mapping(scope_data)
        elif isinstance(scope_data, ScopeRef):
            scope = scope_data
        else:
            raise ValueError("TicketRef requires a scope")
        ticket_id = data.get("ticket_id")
        if not isinstance(ticket_id, str):
            raise ValueError("TicketRef requires a ticket_id")
        return cls(scope=scope, ticket_id=ticket_id)


__all__ = [
    "AgentRef",
    "MemoryRef",
    "ParticipantRef",
    "ScopeRef",
    "ScopeRefError",
    "SurfaceRef",
    "TicketRef",
]
