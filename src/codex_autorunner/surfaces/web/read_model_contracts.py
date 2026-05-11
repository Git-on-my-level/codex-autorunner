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
    "ChatIndexCounters",
    "ChatIndexGroup",
    "ChatIndexPatch",
    "ChatIndexPatchEvent",
    "ChatIndexRow",
    "ChatIndexSnapshot",
    "ChatQueueSummary",
    "ChatThreadProjection",
    "ChatTimelineItem",
    "PageWindow",
    "ProjectionCursor",
    "ProjectionRevision",
    "ReadModelContract",
    "ReadModelEventEnvelope",
    "RepairPolicy",
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


class ChatIndexRow(ReadModelContract):
    chat_id: str
    surface: Literal["pma", "file_chat", "telegram", "discord", "app_server", "other"]
    title: str
    status: Literal["waiting", "running", "idle", "archived", "failed"]
    unread_count: int = Field(ge=0)
    last_activity_at: Optional[datetime] = None
    repo_id: Optional[str] = None
    worktree_id: Optional[str] = None
    ticket_id: Optional[str] = None
    run_id: Optional[str] = None
    agent: Optional[str] = None
    model: Optional[str] = None
    group_id: Optional[str] = None


class ChatIndexGroup(ReadModelContract):
    group_id: str
    kind: Literal["ticket_run", "surface", "repo", "worktree"]
    label: str
    child_count: int = Field(ge=0)
    expanded_child_window: Optional[PageWindow] = None


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
    rows: list[ChatIndexRow]
    groups: list[ChatIndexGroup] = Field(default_factory=list)
    counters: ChatIndexCounters
    repair: RepairPolicy


class ChatIndexPatch(ReadModelContract):
    rows: list[ChatIndexRow] = Field(default_factory=list)
    groups: list[ChatIndexGroup] = Field(default_factory=list)
    removed_row_ids: list[str] = Field(default_factory=list)
    removed_group_ids: list[str] = Field(default_factory=list)
    order: Optional[list[str]] = None
    counters: Optional[ChatIndexCounters] = None


class ChatIndexPatchEvent(ReadModelContract):
    envelope: ReadModelEventEnvelope
    patch: ChatIndexPatch


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
    created_at: datetime
    text: Optional[str] = None
    artifact_ids: list[str] = Field(default_factory=list)
    client_message_id: Optional[str] = None
    backend_message_id: Optional[str] = None


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
    destination_id: Optional[str] = None
    child_worktree_ids: list[str] = Field(default_factory=list)


class WorktreeTopology(ReadModelContract):
    worktree_id: str
    repo_id: str
    label: str
    path: str
    branch: Optional[str] = None
    archived: bool = False
    destination_id: Optional[str] = None


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
    scoped_tickets: list[dict[str, Any]] = Field(default_factory=list)
    scoped_runs: list[dict[str, Any]] = Field(default_factory=list)
    scoped_chats: list[dict[str, Any]] = Field(default_factory=list)
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
