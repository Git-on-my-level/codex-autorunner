"""Versioned web UI read-model contracts.

These models define the target screen-shaped payloads for the responsive web UI
projection layer. Route handlers are expected to serialize them with
``dump_read_model_contract`` so JSON field names match the TypeScript contracts.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field

READ_MODEL_CONTRACT_VERSION: Literal["web-read-models.v1"] = "web-read-models.v1"

__all__ = [
    "READ_MODEL_CONTRACT_VERSION",
    "ChatArtifactSummary",
    "ChatDetailPatch",
    "ChatDetailPatchEvent",
    "ChatDetailSnapshot",
    "ChatFacetAgentKind",
    "ChatFacetCategory",
    "ChatFacetCounts",
    "ChatFacetOriginKind",
    "ChatFacetRequest",
    "ChatFacetScopeKind",
    "ChatFacetTransport",
    "ChatFacetTurnKind",
    "ChatIndexFacets",
    "ChatIndexCounters",
    "ChatIndexGroup",
    "ChatIndexGroupEntry",
    "ChatIndexPatch",
    "ChatIndexPatchEvent",
    "ChatIndexRow",
    "ChatIndexSnapshot",
    "ChatQueueSummary",
    "ChatThreadProjection",
    "ChatTimelineIdentity",
    "ChatTimelineItem",
    "ChatTimelineProvenance",
    "PageWindow",
    "ProjectionCursor",
    "ProjectionRevision",
    "ReadModelContract",
    "ReadModelEventEnvelope",
    "RepairPolicy",
    "CursorGapRepair",
    "RepoTopology",
    "RepoWorktreeDetailSnapshot",
    "RepoWorktreePatch",
    "RepoWorktreePatchEvent",
    "RepoWorktreeRuntimeSnapshot",
    "RepoWorktreeTopologySnapshot",
    "RunProjection",
    "RuntimeProjection",
    "TicketDetailPatch",
    "TicketDetailPatchEvent",
    "TicketDetailSnapshot",
    "TicketRunGroup",
    "TicketProjection",
    "TicketQueueSibling",
    "WorktreeTopology",
    "dump_read_model_contract",
    "load_read_model_contract",
    "read_model_now",
]


def _to_camel(value: str) -> str:
    parts = value.split("_")
    return parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:])


class ReadModelContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        alias_generator=_to_camel,
    )


class ProjectionCursor(ReadModelContract):
    value: str
    sequence: int = Field(ge=0)
    source: str
    issued_at: datetime


class ProjectionRevision(ReadModelContract):
    value: str
    source_kind: str
    source_id: str
    updated_at: datetime


class RepairPolicy(ReadModelContract):
    snapshot_route: str
    cursor_query_param: str = "after"
    gap_event_type: Literal["projection.cursor_gap"] = "projection.cursor_gap"
    behavior: Literal["repair_snapshot_required"] = "repair_snapshot_required"


class CursorGapRepair(RepairPolicy):
    requested_cursor: int = Field(ge=0)
    latest_cursor: int = Field(ge=0)


class PageWindow(ReadModelContract):
    limit: int = Field(ge=1, le=500)
    next_cursor: Optional[str] = None
    previous_cursor: Optional[str] = None
    total_estimate: Optional[int] = Field(default=None, ge=0)
    total_is_exact: bool = False


class ReadModelEventEnvelope(ReadModelContract):
    contract_version: Literal["web-read-models.v1"] = READ_MODEL_CONTRACT_VERSION
    event_type: str
    cursor: ProjectionCursor
    entity_kind: str
    entity_id: str
    operation: Literal["upsert", "patch", "delete", "reorder", "invalidate", "reset"]
    generated_at: datetime
    source_revision: Optional[ProjectionRevision] = None


ChatFacetCategory = Literal["regular", "ticket_run", "automation", "system"]
ChatFacetTurnKind = Literal[
    "message",
    "review",
    "automation",
    "publish",
    "recovery",
    "lifecycle",
]
ChatFacetOriginKind = Literal["surface", "automation", "publish", "recovery", "system"]
ChatFacetTransport = Literal["pma", "discord", "telegram", "notification"]
ChatFacetScopeKind = Literal["hub", "repo", "worktree", "filesystem"]
ChatFacetAgentKind = Literal["pma", "coding_agent"]


class ChatIndexFacets(ReadModelContract):
    """Backend-owned orthogonal facets for chat-index classification.

    ``category`` is the small primary bucket shown in chat-list UIs. Review,
    publish, recovery, and lifecycle execution purposes are represented in
    ``turn_kinds``/``origin_kinds`` instead of becoming primary categories.
    Delivery transports are canonical user-visible chat transports; runtime
    entrypoints such as web, cli, file_chat, and app_server are deliberately not
    transport facet values.
    """

    category: ChatFacetCategory = "regular"
    turn_kinds: list[ChatFacetTurnKind] = Field(default_factory=list)
    origin_kinds: list[ChatFacetOriginKind] = Field(default_factory=list)
    transports: list[ChatFacetTransport] = Field(default_factory=list)
    scope_kind: Optional[ChatFacetScopeKind] = None
    scope_id: Optional[str] = None
    agent_kind: Optional[ChatFacetAgentKind] = None


class ChatFacetCounts(ReadModelContract):
    """Backend projection counts for the full matching chat-index scope.

    Counts are computed before pagination for the current backend query/filter
    scope. ``transport`` is the user-facing external-channel count set and
    intentionally excludes the PMA home/control surface even though rows may
    still carry ``pma`` in ``facets.transports`` for badges and compatibility.
    """

    category: dict[ChatFacetCategory, int] = Field(default_factory=dict)
    turn_kind: dict[ChatFacetTurnKind, int] = Field(default_factory=dict)
    origin_kind: dict[ChatFacetOriginKind, int] = Field(default_factory=dict)
    transport: dict[ChatFacetTransport, int] = Field(default_factory=dict)
    scope_kind: dict[ChatFacetScopeKind, int] = Field(default_factory=dict)
    agent_kind: dict[ChatFacetAgentKind, int] = Field(default_factory=dict)


class ChatFacetRequest(ReadModelContract):
    """Compound facet window request, applied conjunctively by the backend.

    Values within one field are ORed, fields are ANDed, and counts are computed
    from the backend projection state for the same base query rather than from
    the currently loaded page.
    """

    categories: list[ChatFacetCategory] = Field(default_factory=list)
    turn_kinds: list[ChatFacetTurnKind] = Field(default_factory=list)
    origin_kinds: list[ChatFacetOriginKind] = Field(default_factory=list)
    transports: list[ChatFacetTransport] = Field(default_factory=list)
    scope_kinds: list[ChatFacetScopeKind] = Field(default_factory=list)
    scope_ids: list[str] = Field(default_factory=list)
    agent_kinds: list[ChatFacetAgentKind] = Field(default_factory=list)


class ChatIndexRow(ReadModelContract):
    """Chat row contract shared by snapshots and patch payloads.

    Timestamp semantics:
    - ``last_visible_message_at`` is the newest user-visible conversation input.
    - ``last_lifecycle_update_at`` is durable thread/binding lifecycle churn.
    - ``last_internal_update_at`` is runtime/execution/delivery bookkeeping.
    - ``last_sort_activity_at`` is the backend-owned recency clock for row order.
    - ``last_activity_at`` is a compatibility alias for ``last_sort_activity_at``.

    Title semantics:
    - ``title``/``display_title`` are backend-resolved human display strings.
    - ``technical_title`` preserves the stable thread/surface identifier.
    - binding display fields describe attached delivery surfaces and do not own
      managed-thread identity.
    """

    chat_id: str
    surface: Literal["pma", "file_chat", "telegram", "discord", "app_server", "other"]
    title: str
    display_title: Optional[str] = None
    technical_title: Optional[str] = None
    primary_surface: Optional[dict[str, Any]] = None
    surface_bindings: list[dict[str, Any]] = Field(default_factory=list)
    binding_display_name: Optional[str] = None
    binding_display_names: list[str] = Field(default_factory=list)
    lifecycle: Optional[str] = None
    runtime_status: Optional[str] = None
    effective_status: Literal["waiting", "running", "idle", "archived", "failed"]
    archive_state: Optional[Literal["active", "archived"]] = None
    status: Literal["waiting", "running", "idle", "archived", "failed"]
    unread_count: int = Field(ge=0)
    last_activity_at: Optional[datetime] = None
    last_visible_message_at: Optional[datetime] = None
    last_lifecycle_update_at: Optional[datetime] = None
    last_internal_update_at: Optional[datetime] = None
    last_sort_activity_at: Optional[datetime] = None
    sort_key: Optional[dict[str, Any]] = None
    resource_kind: Optional[str] = None
    resource_id: Optional[str] = None
    workspace_root: Optional[str] = None
    repo_id: Optional[str] = None
    worktree_id: Optional[str] = None
    ticket_id: Optional[str] = None
    run_id: Optional[str] = None
    agent: Optional[str] = None
    agent_profile: Optional[str] = None
    chat_kind: Optional[Literal["pma", "coding_agent"]] = None
    facets: Optional[ChatIndexFacets] = None
    model: Optional[str] = None
    group_id: Optional[str] = None
    flow_type: Optional[Literal["ticket_flow"]] = None
    ticket_path: Optional[str] = None
    ticket_done: Optional[bool] = None
    ticket_status: Optional[
        Literal["done", "running", "waiting", "failed", "unknown"]
    ] = None
    debug: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "Non-authoritative diagnostic hints explaining title and activity "
            "clock resolution for support/debug UIs."
        ),
    )


class ChatIndexGroup(ReadModelContract):
    group_id: str
    kind: Literal["surface", "repo", "worktree"]
    label: str
    child_count: int = Field(ge=0)
    waiting_count: int = Field(default=0, ge=0)
    running_count: int = Field(default=0, ge=0)
    unread_count: int = Field(default=0, ge=0)
    last_activity_at: Optional[datetime] = None
    last_visible_message_at: Optional[datetime] = None
    last_lifecycle_update_at: Optional[datetime] = None
    last_internal_update_at: Optional[datetime] = None
    last_sort_activity_at: Optional[datetime] = None
    debug: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "Non-authoritative diagnostic hints explaining group activity clock "
            "resolution for support/debug UIs."
        ),
    )
    expanded_child_window: Optional[PageWindow] = None


class TicketRunGroup(ReadModelContract):
    kind: Literal["ticket_run_group"] = "ticket_run_group"
    group_id: str
    run_id: str
    scope_kind: Literal["repo", "worktree"]
    scope_id: str
    label: str
    status: Literal["running", "waiting", "failed", "done", "idle"]
    total_count: int = Field(ge=0)
    done_count: int = Field(ge=0)
    running_count: int = Field(ge=0)
    waiting_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    unread_count: int = Field(ge=0)
    last_activity_at: Optional[datetime] = None
    last_visible_message_at: Optional[datetime] = None
    last_lifecycle_update_at: Optional[datetime] = None
    last_internal_update_at: Optional[datetime] = None
    last_sort_activity_at: Optional[datetime] = None
    debug: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "Non-authoritative diagnostic hints explaining group activity clock "
            "resolution for support/debug UIs."
        ),
    )
    updated_at: Optional[datetime] = None
    expanded_child_window: Optional[PageWindow] = None


ChatIndexGroupEntry = ChatIndexGroup | TicketRunGroup


class ChatIndexCounters(ReadModelContract):
    total: int = Field(ge=0)
    waiting: int = Field(ge=0)
    running: int = Field(ge=0)
    unread: int = Field(ge=0)
    archived: int = Field(ge=0)


class ChatIndexSnapshot(ReadModelContract):
    contract_version: Literal["web-read-models.v1"] = READ_MODEL_CONTRACT_VERSION
    kind: Literal["chat.index.snapshot"] = "chat.index.snapshot"
    cursor: ProjectionCursor
    window: PageWindow
    filter: Literal[
        "all", "waiting", "active", "unread", "archived", "ticket_runs", "external"
    ]
    query: Optional[str] = None
    facet_request: ChatFacetRequest = Field(default_factory=ChatFacetRequest)
    rows: list[ChatIndexRow]
    groups: list[ChatIndexGroupEntry] = Field(default_factory=list)
    counters: ChatIndexCounters
    facet_counts: ChatFacetCounts = Field(default_factory=ChatFacetCounts)
    repair: RepairPolicy


class ChatIndexPatch(ReadModelContract):
    rows: list[ChatIndexRow] = Field(default_factory=list)
    groups: list[ChatIndexGroupEntry] = Field(default_factory=list)
    removed_row_ids: list[str] = Field(default_factory=list)
    removed_group_ids: list[str] = Field(default_factory=list)
    order: Optional[list[str]] = None
    counters: Optional[ChatIndexCounters] = None
    facet_counts: Optional[ChatFacetCounts] = None


class ChatIndexPatchEvent(ReadModelContract):
    envelope: ReadModelEventEnvelope
    patch: ChatIndexPatch
    repair: Optional[CursorGapRepair] = None


class ChatTimelineIdentity(ReadModelContract):
    timeline_item_id: str
    progress_item_ids: list[str] = Field(default_factory=list)
    correlation_id: Optional[str] = None


class ChatTimelineProvenance(ReadModelContract):
    source_event_ids: list[Any] = Field(default_factory=list)
    progress_event_ids: list[Any] = Field(default_factory=list)
    cursor_event_id: Optional[str] = None


class ChatTimelineItem(ReadModelContract):
    item_id: str
    kind: Literal[
        "user_message",
        "assistant_message",
        "tool_event",
        "progress",
        "artifact",
        "system",
    ]
    role: Optional[Literal["user", "assistant", "tool", "system"]] = None
    managed_turn_id: Optional[str] = None
    order_key: str
    section: Literal[
        "user_message",
        "activity",
        "assistant_message",
        "terminal_metadata",
        "thread_metadata",
    ]
    section_order: int = Field(ge=0)
    created_at: datetime
    text: Optional[str] = None
    artifact_ids: list[str] = Field(default_factory=list)
    client_message_id: Optional[str] = None
    backend_message_id: Optional[str] = None
    identity: ChatTimelineIdentity
    provenance: ChatTimelineProvenance


class ChatQueueSummary(ReadModelContract):
    depth: int = Field(ge=0)
    active_turn_id: Optional[str] = None
    queued_turn_ids: list[str] = Field(default_factory=list)


class ChatArtifactSummary(ReadModelContract):
    artifact_id: str
    name: str
    kind: str
    href: Optional[str] = None
    updated_at: Optional[datetime] = None


class ChatThreadProjection(ReadModelContract):
    chat_id: str
    surface: str
    title: str
    status: Literal["waiting", "running", "idle", "archived", "failed"]
    repo_id: Optional[str] = None
    worktree_id: Optional[str] = None
    ticket_id: Optional[str] = None
    run_id: Optional[str] = None
    agent: Optional[str] = None
    agent_profile: Optional[str] = None
    chat_kind: Optional[Literal["pma", "coding_agent"]] = None
    model: Optional[str] = None
    archived: bool = False


class ChatDetailSnapshot(ReadModelContract):
    contract_version: Literal["web-read-models.v1"] = READ_MODEL_CONTRACT_VERSION
    kind: Literal["chat.detail.snapshot"] = "chat.detail.snapshot"
    cursor: ProjectionCursor
    thread: ChatThreadProjection
    timeline_window: PageWindow
    timeline: list[ChatTimelineItem]
    queue: ChatQueueSummary
    artifacts: list[ChatArtifactSummary] = Field(default_factory=list)
    repair: RepairPolicy


class ChatDetailPatch(ReadModelContract):
    thread: Optional[ChatThreadProjection] = None
    appended_timeline: list[ChatTimelineItem] = Field(default_factory=list)
    patched_timeline: list[ChatTimelineItem] = Field(default_factory=list)
    removed_timeline_ids: list[str] = Field(default_factory=list)
    queue: Optional[ChatQueueSummary] = None
    artifacts: list[ChatArtifactSummary] = Field(default_factory=list)


class ChatDetailPatchEvent(ReadModelContract):
    envelope: ReadModelEventEnvelope
    patch: ChatDetailPatch


class RepoTopology(ReadModelContract):
    repo_id: str
    label: str
    path: str
    archived: bool = False
    is_pinned: bool = False
    destination_id: Optional[str] = None
    child_worktree_ids: list[str] = Field(default_factory=list)
    worktree_setup_commands: Optional[list[str]] = None
    chat_bound: bool = False
    chat_binding_count: int = Field(default=0, ge=0)
    chat_binding_sources: dict[str, int] = Field(default_factory=dict)
    chat_binding_display_names: list[str] = Field(default_factory=list)


class WorktreeTopology(ReadModelContract):
    worktree_id: str
    repo_id: str
    label: str
    path: str
    branch: Optional[str] = None
    archived: bool = False
    destination_id: Optional[str] = None
    chat_bound: bool = False
    chat_binding_count: int = Field(default=0, ge=0)
    chat_binding_sources: dict[str, int] = Field(default_factory=dict)
    chat_binding_display_names: list[str] = Field(default_factory=list)


class RepoWorktreeTopologySnapshot(ReadModelContract):
    contract_version: Literal["web-read-models.v1"] = READ_MODEL_CONTRACT_VERSION
    kind: Literal["repo_worktree.topology.snapshot"] = "repo_worktree.topology.snapshot"
    cursor: ProjectionCursor
    window: PageWindow
    repos: list[RepoTopology]
    worktrees: list[WorktreeTopology]
    repair: RepairPolicy


class RuntimeProjection(ReadModelContract):
    entity_kind: Literal["repo", "worktree"]
    entity_id: str
    git_dirty: Optional[bool] = None
    git_ahead: Optional[int] = Field(default=None, ge=0)
    git_behind: Optional[int] = Field(default=None, ge=0)
    active_run_id: Optional[str] = None
    active_run_status: Optional[str] = None
    waiting_ticket_count: int = Field(default=0, ge=0)
    running_ticket_count: int = Field(default=0, ge=0)
    chat_count: int = Field(default=0, ge=0)
    cleanup_blockers: list[str] = Field(default_factory=list)
    updated_at: Optional[datetime] = None


class RepoWorktreeRuntimeSnapshot(ReadModelContract):
    contract_version: Literal["web-read-models.v1"] = READ_MODEL_CONTRACT_VERSION
    kind: Literal["repo_worktree.runtime.snapshot"] = "repo_worktree.runtime.snapshot"
    cursor: ProjectionCursor
    window: PageWindow
    runtime: list[RuntimeProjection]
    repair: RepairPolicy


class RepoWorktreePatch(ReadModelContract):
    topology_repos: list[RepoTopology] = Field(default_factory=list)
    topology_worktrees: list[WorktreeTopology] = Field(default_factory=list)
    runtime: list[RuntimeProjection] = Field(default_factory=list)
    removed_repo_ids: list[str] = Field(default_factory=list)
    removed_worktree_ids: list[str] = Field(default_factory=list)
    order: Optional[list[str]] = None


class RepoWorktreePatchEvent(ReadModelContract):
    envelope: ReadModelEventEnvelope
    patch: RepoWorktreePatch


class RepoWorktreeDetailSnapshot(ReadModelContract):
    contract_version: Literal["web-read-models.v1"] = READ_MODEL_CONTRACT_VERSION
    kind: Literal["repo_worktree.detail.snapshot"] = "repo_worktree.detail.snapshot"
    cursor: ProjectionCursor
    owner_kind: Literal["repo", "worktree"]
    owner_id: str
    identity: dict[str, Any]
    parent_links: dict[str, Any] = Field(default_factory=dict)
    topology: dict[str, Any] = Field(default_factory=dict)
    runtime: dict[str, Any] = Field(default_factory=dict)
    ticket_queue: list[dict[str, Any]] = Field(default_factory=list)
    run_queue: list[dict[str, Any]] = Field(default_factory=list)
    chat_queue: list[dict[str, Any]] = Field(default_factory=list)
    contextspace_summary: list[dict[str, Any]] = Field(default_factory=list)
    current_artifacts: list[dict[str, Any]] = Field(default_factory=list)
    ticket_window: PageWindow
    run_window: PageWindow
    chat_window: PageWindow
    artifact_window: PageWindow
    repair: RepairPolicy


class TicketProjection(ReadModelContract):
    ticket_id: str
    route_id: str
    title: str
    status: Literal[
        "queued", "waiting", "running", "blocked", "done", "failed", "invalid"
    ]
    owner_kind: Literal["repo", "worktree"]
    owner_id: str
    agent: Optional[str] = None
    model: Optional[str] = None
    done: bool = False
    updated_at: Optional[datetime] = None


class TicketQueueSibling(ReadModelContract):
    ticket_id: str
    route_id: str
    title: str
    status: str
    previous_ticket_id: Optional[str] = None
    next_ticket_id: Optional[str] = None


class RunProjection(ReadModelContract):
    run_id: str
    status: str
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    worker_activity: Optional[str] = None


class TicketDetailSnapshot(ReadModelContract):
    contract_version: Literal["web-read-models.v1"] = READ_MODEL_CONTRACT_VERSION
    kind: Literal["ticket.detail.snapshot"] = "ticket.detail.snapshot"
    cursor: ProjectionCursor
    ticket: TicketProjection
    siblings: list[TicketQueueSibling] = Field(default_factory=list)
    linked_run: Optional[RunProjection] = None
    linked_chats: list[ChatIndexRow] = Field(default_factory=list)
    artifacts: list[ChatArtifactSummary] = Field(default_factory=list)
    dispatch_window: PageWindow
    dispatches: list[dict[str, Any]] = Field(default_factory=list)
    repair: RepairPolicy
    ticket_detail: dict[str, Any] = Field(default_factory=dict)
    ticket_queue: list[dict[str, Any]] = Field(default_factory=list)
    run_queue: list[dict[str, Any]] = Field(default_factory=list)
    chat_queue: list[dict[str, Any]] = Field(default_factory=list)


class TicketDetailPatch(ReadModelContract):
    ticket: Optional[TicketProjection] = None
    siblings: list[TicketQueueSibling] = Field(default_factory=list)
    linked_run: Optional[RunProjection] = None
    linked_chats: list[ChatIndexRow] = Field(default_factory=list)
    artifacts: list[ChatArtifactSummary] = Field(default_factory=list)
    dispatches: list[dict[str, Any]] = Field(default_factory=list)


class TicketDetailPatchEvent(ReadModelContract):
    envelope: ReadModelEventEnvelope
    patch: TicketDetailPatch


ReadModel = TypeVar("ReadModel", bound=ReadModelContract)


def read_model_now() -> datetime:
    return datetime.now(timezone.utc)


def dump_read_model_contract(model: ReadModelContract) -> dict[str, Any]:
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)


def load_read_model_contract(
    model_type: type[ReadModel], payload: dict[str, Any]
) -> ReadModel:
    return model_type.model_validate(payload)
