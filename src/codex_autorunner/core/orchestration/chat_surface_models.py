from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Mapping, Optional

from ..domain.refs import ScopeRef, SurfaceRef
from ..text_utils import _normalize_optional_text
from .models import normalize_resource_owner_fields, scope_ref_from_owner_fields

ChatSurfaceLifecycle = Literal["active", "archived", "disabled", "deleted"]

_VALID_LIFECYCLE_STATUSES: set[str] = {
    "active",
    "archived",
    "disabled",
    "deleted",
}


def normalize_chat_surface_kind(value: Any) -> str:
    normalized = _normalize_optional_text(value)
    if normalized is None:
        raise ValueError("surface_kind is required")
    normalized = normalized.lower()
    if any(ch.isspace() for ch in normalized):
        raise ValueError("surface_kind must not contain whitespace")
    return normalized


def normalize_chat_surface_key(value: Any) -> str:
    normalized = _normalize_optional_text(value)
    if normalized is None:
        raise ValueError("surface_key is required")
    return normalized


def normalize_chat_surface_lifecycle(value: Any) -> ChatSurfaceLifecycle:
    normalized = (_normalize_optional_text(value) or "active").lower()
    if normalized not in _VALID_LIFECYCLE_STATUSES:
        raise ValueError(f"unknown chat surface lifecycle status: {normalized}")
    return normalized  # type: ignore[return-value]


@dataclass(frozen=True)
class ChatSurfaceIdentity:
    """Stable orchestration identity for a chat-like surface."""

    surface_kind: str
    surface_key: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "surface_kind",
            normalize_chat_surface_kind(self.surface_kind),
        )
        object.__setattr__(
            self,
            "surface_key",
            normalize_chat_surface_key(self.surface_key),
        )

    @classmethod
    def from_parts(cls, surface_kind: Any, surface_key: Any) -> "ChatSurfaceIdentity":
        return cls(surface_kind=surface_kind, surface_key=surface_key)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ChatSurfaceIdentity":
        surface = data.get("surface")
        if isinstance(surface, ChatSurfaceIdentity):
            return surface
        if isinstance(surface, SurfaceRef):
            return cls(surface_kind=surface.kind, surface_key=surface.key)
        if isinstance(surface, Mapping):
            return cls.from_mapping(surface)

        surface_urn = _normalize_optional_text(
            data.get("surface_urn") or data.get("urn")
        )
        if surface_urn is not None:
            ref = SurfaceRef.from_urn(surface_urn)
            return cls(surface_kind=ref.kind, surface_key=ref.key)

        surface_kind = normalize_chat_surface_kind(
            data.get("surface_kind") or data.get("kind")
        )
        surface_key = normalize_chat_surface_key(
            data.get("surface_key") or data.get("key")
        )
        return cls(
            surface_kind=surface_kind,
            surface_key=surface_key,
        )

    def to_ref(self) -> SurfaceRef:
        return SurfaceRef(kind=self.surface_kind, key=self.surface_key)

    def to_urn(self) -> str:
        return self.to_ref().to_urn()

    def to_dict(self) -> dict[str, str]:
        return {
            "surface_kind": self.surface_kind,
            "surface_key": self.surface_key,
        }


@dataclass(frozen=True)
class ChatSurfaceResourceOwner:
    """Resource scope that owns or contextualizes a chat surface."""

    repo_id: Optional[str] = None
    resource_kind: Optional[str] = None
    resource_id: Optional[str] = None
    workspace_root: Optional[str] = None
    scope_urn: Optional[str] = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ChatSurfaceResourceOwner":
        scope = data.get("scope")
        if isinstance(scope, ScopeRef):
            repo_id, resource_kind, resource_id, workspace_root = (
                _owner_fields_from_scope(scope)
            )
            return cls(
                repo_id=repo_id,
                resource_kind=resource_kind,
                resource_id=resource_id,
                workspace_root=workspace_root,
                scope_urn=scope.to_urn(),
            )
        if isinstance(scope, Mapping):
            return cls.from_scope_ref(ScopeRef.from_mapping(scope))

        scope_urn = _normalize_optional_text(data.get("scope_urn"))
        repo_id, resource_kind, resource_id, workspace_root = (None, None, None, None)
        if scope_urn is not None:
            scope_ref = scope_ref_from_owner_fields(scope_urn=scope_urn)
            repo_id, resource_kind, resource_id, workspace_root = (
                _owner_fields_from_scope(scope_ref)
            )
        else:
            resource_kind, resource_id, repo_id = normalize_resource_owner_fields(
                resource_kind=data.get("resource_kind"),
                resource_id=data.get("resource_id"),
                repo_id=data.get("repo_id"),
            )
            workspace_root = _normalize_optional_text(data.get("workspace_root"))
            if (
                resource_kind is not None
                or repo_id is not None
                or workspace_root is not None
            ):
                scope_urn = scope_ref_from_owner_fields(
                    resource_kind=resource_kind,
                    resource_id=resource_id,
                    repo_id=repo_id,
                    workspace_root=workspace_root,
                ).to_urn()

        return cls(
            repo_id=repo_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
            workspace_root=workspace_root,
            scope_urn=scope_urn,
        )

    @classmethod
    def from_scope_ref(cls, scope: ScopeRef) -> "ChatSurfaceResourceOwner":
        repo_id, resource_kind, resource_id, workspace_root = _owner_fields_from_scope(
            scope
        )
        return cls(
            repo_id=repo_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
            workspace_root=workspace_root,
            scope_urn=scope.to_urn(),
        )

    def to_dict(self) -> dict[str, Optional[str]]:
        return asdict(self)


@dataclass(frozen=True)
class ChatSurfaceExternalConversationId:
    """Protocol-specific conversation id attached to a canonical chat surface."""

    provider: str
    conversation_id: str
    conversation_kind: Optional[str] = None

    def __post_init__(self) -> None:
        provider = normalize_chat_surface_kind(self.provider)
        conversation_id = normalize_chat_surface_key(self.conversation_id)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "conversation_id", conversation_id)
        object.__setattr__(
            self,
            "conversation_kind",
            _normalize_optional_text(self.conversation_kind),
        )

    @classmethod
    def from_mapping(
        cls, data: Mapping[str, Any]
    ) -> "ChatSurfaceExternalConversationId":
        provider = normalize_chat_surface_kind(
            data.get("provider") or data.get("surface_kind")
        )
        conversation_id = normalize_chat_surface_key(
            data.get("conversation_id")
            or data.get("external_conversation_id")
            or data.get("id")
        )
        return cls(
            provider=provider,
            conversation_id=conversation_id,
            conversation_kind=data.get("conversation_kind") or data.get("kind"),
        )

    def to_dict(self) -> dict[str, Optional[str]]:
        return asdict(self)


@dataclass(frozen=True)
class ChatSurfaceDisplayMetadata:
    display_name: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    avatar_url: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ChatSurfaceDisplayMetadata":
        display = data.get("display")
        if isinstance(display, ChatSurfaceDisplayMetadata):
            return display
        display_map = display if isinstance(display, Mapping) else data
        metadata = display_map.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        return cls(
            display_name=_normalize_optional_text(
                display_map.get("display_name") or display_map.get("name")
            ),
            title=_normalize_optional_text(display_map.get("title")),
            description=_normalize_optional_text(display_map.get("description")),
            avatar_url=_normalize_optional_text(display_map.get("avatar_url")),
            metadata=dict(metadata),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ChatSurface:
    """Canonical chat surface snapshot owned by orchestration."""

    identity: ChatSurfaceIdentity
    lifecycle_status: ChatSurfaceLifecycle = "active"
    owner: ChatSurfaceResourceOwner = field(default_factory=ChatSurfaceResourceOwner)
    external_conversation_ids: tuple[ChatSurfaceExternalConversationId, ...] = ()
    managed_thread_id: Optional[str] = None
    display: ChatSurfaceDisplayMetadata = field(
        default_factory=ChatSurfaceDisplayMetadata
    )
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    archived_at: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "lifecycle_status",
            normalize_chat_surface_lifecycle(self.lifecycle_status),
        )
        object.__setattr__(
            self,
            "managed_thread_id",
            _normalize_optional_text(self.managed_thread_id),
        )

    @property
    def surface_kind(self) -> str:
        return self.identity.surface_kind

    @property
    def surface_key(self) -> str:
        return self.identity.surface_key

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ChatSurface":
        identity = ChatSurfaceIdentity.from_mapping(data)
        external_ids = _external_ids_from_mapping(data, identity)
        metadata = data.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        return cls(
            identity=identity,
            lifecycle_status=data.get("lifecycle_status")
            or data.get("lifecycle")
            or data.get("status")
            or "active",
            owner=ChatSurfaceResourceOwner.from_mapping(data),
            external_conversation_ids=external_ids,
            managed_thread_id=(
                data.get("managed_thread_id")
                or data.get("thread_target_id")
                or data.get("target_id")
            ),
            display=ChatSurfaceDisplayMetadata.from_mapping(data),
            created_at=_normalize_optional_text(data.get("created_at")),
            updated_at=_normalize_optional_text(data.get("updated_at")),
            archived_at=_normalize_optional_text(data.get("archived_at")),
            metadata=dict(metadata),
        )

    def to_dict(self) -> dict[str, Any]:
        identity = self.identity.to_dict()
        owner = self.owner.to_dict()
        display = self.display.to_dict()
        return {
            **identity,
            "surface_urn": self.identity.to_urn(),
            "lifecycle_status": self.lifecycle_status,
            "repo_id": owner["repo_id"],
            "resource_kind": owner["resource_kind"],
            "resource_id": owner["resource_id"],
            "workspace_root": owner["workspace_root"],
            "scope_urn": owner["scope_urn"],
            "external_conversation_ids": [
                item.to_dict() for item in self.external_conversation_ids
            ],
            "managed_thread_id": self.managed_thread_id,
            "display": display,
            "display_name": display["display_name"],
            "title": display["title"],
            "description": display["description"],
            "avatar_url": display["avatar_url"],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "archived_at": self.archived_at,
            "metadata": dict(self.metadata),
        }


def normalize_chat_surface_identity(
    *,
    surface_kind: Any,
    surface_key: Any,
) -> ChatSurfaceIdentity:
    return ChatSurfaceIdentity.from_parts(surface_kind, surface_key)


def chat_surface_identity_dict(
    *, surface_kind: Any, surface_key: Any
) -> dict[str, str]:
    return normalize_chat_surface_identity(
        surface_kind=surface_kind,
        surface_key=surface_key,
    ).to_dict()


def _external_ids_from_mapping(
    data: Mapping[str, Any],
    identity: ChatSurfaceIdentity,
) -> tuple[ChatSurfaceExternalConversationId, ...]:
    raw = data.get("external_conversation_ids") or data.get("external_ids")
    items: list[ChatSurfaceExternalConversationId] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, ChatSurfaceExternalConversationId):
                items.append(item)
            elif isinstance(item, Mapping):
                items.append(ChatSurfaceExternalConversationId.from_mapping(item))
    external_id = _normalize_optional_text(data.get("external_conversation_id"))
    if external_id is not None:
        items.append(
            ChatSurfaceExternalConversationId(
                provider=identity.surface_kind,
                conversation_id=external_id,
                conversation_kind=data.get("external_conversation_kind"),
            )
        )
    return tuple(items)


def _owner_fields_from_scope(
    scope: ScopeRef,
) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    if scope.kind == "repo":
        return scope.id, "repo", scope.id, None
    if scope.kind == "filesystem":
        return None, None, None, scope.path
    if scope.kind == "hub":
        return None, None, None, None
    return None, scope.kind, scope.id, None


__all__ = [
    "ChatSurface",
    "ChatSurfaceDisplayMetadata",
    "ChatSurfaceExternalConversationId",
    "ChatSurfaceIdentity",
    "ChatSurfaceLifecycle",
    "ChatSurfaceResourceOwner",
    "chat_surface_identity_dict",
    "normalize_chat_surface_identity",
    "normalize_chat_surface_key",
    "normalize_chat_surface_kind",
    "normalize_chat_surface_lifecycle",
]
