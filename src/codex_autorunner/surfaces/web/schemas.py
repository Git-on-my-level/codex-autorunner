"""
Pydantic request/response schemas for web and API routes.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class Payload(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class ResponseModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class WorkspaceWriteRequest(Payload):
    content: str = ""


class WorkspaceResponse(ResponseModel):
    active_context: str
    decisions: str
    spec: str


class WorkspaceFileItem(ResponseModel):
    name: str
    path: str
    is_pinned: bool = False
    modified_at: Optional[str] = None


class WorkspaceFileListResponse(ResponseModel):
    files: List[WorkspaceFileItem]


class WorkspaceNode(ResponseModel):
    name: str
    path: str
    type: Literal["file", "folder"]
    is_pinned: bool = False
    modified_at: Optional[str] = None
    size: Optional[int] = None
    children: Optional[List["WorkspaceNode"]] = None


WorkspaceNode.model_rebuild()


class WorkspaceTreeResponse(ResponseModel):
    tree: List[WorkspaceNode]


class WorkspaceUploadedItem(ResponseModel):
    filename: str
    path: str
    size: int


class WorkspaceUploadResponse(ResponseModel):
    status: str
    uploaded: List[WorkspaceUploadedItem]


class ArchiveSnapshotSummary(ResponseModel):
    snapshot_id: str
    worktree_repo_id: str
    created_at: Optional[str] = None
    status: Optional[str] = None
    branch: Optional[str] = None
    head_sha: Optional[str] = None
    note: Optional[str] = None
    summary: Optional[Dict[str, Any]] = None


class ArchiveSnapshotsResponse(ResponseModel):
    snapshots: List[ArchiveSnapshotSummary]


class ArchiveSnapshotDetailResponse(ResponseModel):
    snapshot: ArchiveSnapshotSummary
    meta: Optional[Dict[str, Any]] = None


class ArchiveTreeNode(ResponseModel):
    path: str
    name: str
    type: Literal["file", "folder"]
    size_bytes: Optional[int] = None
    mtime: Optional[float] = None


class ArchiveTreeResponse(ResponseModel):
    path: str
    nodes: List[ArchiveTreeNode]


class SpecIngestTicketsResponse(ResponseModel):
    status: str
    created: int
    first_ticket_path: Optional[str] = None


class RunControlRequest(Payload):
    once: bool = False
    agent: Optional[str] = None
    model: Optional[str] = None
    reasoning: Optional[str] = None


class HubCreateRepoRequest(Payload):
    git_url: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("git_url", "gitUrl")
    )
    repo_id: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("repo_id", "id")
    )
    path: Optional[str] = None
    git_init: bool = True
    force: bool = False


class HubRemoveRepoRequest(Payload):
    force: bool = False
    delete_dir: bool = True
    delete_worktrees: bool = False


class HubCreateWorktreeRequest(Payload):
    base_repo_id: str = Field(
        validation_alias=AliasChoices("base_repo_id", "baseRepoId")
    )
    branch: str
    force: bool = False
    start_point: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices(
            "start_point", "startPoint", "base_ref", "baseRef"
        ),
    )


class HubCleanupWorktreeRequest(Payload):
    worktree_repo_id: str = Field(
        validation_alias=AliasChoices("worktree_repo_id", "worktreeRepoId")
    )
    delete_branch: bool = False
    delete_remote: bool = False
    archive: bool = True
    force_archive: bool = Field(
        default=False, validation_alias=AliasChoices("force_archive", "forceArchive")
    )
    archive_note: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("archive_note", "archiveNote")
    )


class AppServerThreadResetRequest(Payload):
    key: str = Field(
        validation_alias=AliasChoices("key", "feature", "feature_key", "featureKey")
    )


class AppServerThreadArchiveRequest(Payload):
    thread_id: str = Field(validation_alias=AliasChoices("thread_id", "threadId", "id"))


class SessionSettingsRequest(Payload):
    autorunner_model_override: Optional[str] = None
    autorunner_effort_override: Optional[str] = None
    autorunner_approval_policy: Optional[str] = None
    autorunner_sandbox_mode: Optional[str] = None
    autorunner_workspace_write_network: Optional[bool] = None
    runner_stop_after_runs: Optional[int] = None


class GithubIssueRequest(Payload):
    issue: str


class GithubContextRequest(Payload):
    url: str


class GithubPrSyncRequest(Payload):
    draft: bool = True
    title: Optional[str] = None
    body: Optional[str] = None
    mode: Optional[str] = None


class SessionStopRequest(Payload):
    session_id: Optional[str] = None
    repo_path: Optional[str] = None


class SystemUpdateRequest(Payload):
    target: Optional[str] = None


class HubJobResponse(ResponseModel):
    job_id: str
    kind: str
    status: str
    created_at: str
    started_at: Optional[str]
    finished_at: Optional[str]
    result: Optional[Dict[str, Any]]
    error: Optional[str]


class StateResponse(ResponseModel):
    last_run_id: Optional[int]
    status: str
    last_exit_code: Optional[int]
    last_run_started_at: Optional[str]
    last_run_finished_at: Optional[str]
    outstanding_count: int
    done_count: int
    running: bool
    runner_pid: Optional[int]
    lock_present: bool
    lock_pid: Optional[int]
    lock_freeable: bool
    lock_freeable_reason: Optional[str]
    terminal_idle_timeout_seconds: Optional[int]
    codex_model: str


class SessionSettingsResponse(ResponseModel):
    autorunner_model_override: Optional[str]
    autorunner_effort_override: Optional[str]
    autorunner_approval_policy: Optional[str]
    autorunner_sandbox_mode: Optional[str]
    autorunner_workspace_write_network: Optional[bool]
    runner_stop_after_runs: Optional[int]


class VersionResponse(ResponseModel):
    asset_version: Optional[str]


class RunControlResponse(ResponseModel):
    running: bool
    once: bool


class RunStatusResponse(ResponseModel):
    running: bool


class RunResetResponse(ResponseModel):
    status: str
    message: str


class SessionItemResponse(ResponseModel):
    session_id: str
    repo_path: Optional[str]
    abs_repo_path: Optional[str] = None
    created_at: Optional[str]
    last_seen_at: Optional[str]
    status: Optional[str]
    alive: bool


class SessionsResponse(ResponseModel):
    sessions: List[SessionItemResponse]
    repo_to_session: Dict[str, str]
    abs_repo_to_session: Optional[Dict[str, str]] = None


class SessionStopResponse(ResponseModel):
    status: str
    session_id: str


class AppServerThreadsResponse(ResponseModel):
    file_chat: Optional[str] = None
    file_chat_opencode: Optional[str] = None
    autorunner: Optional[str] = None
    autorunner_opencode: Optional[str] = None
    corruption: Optional[Dict[str, Any]] = None


class AppServerThreadResetResponse(ResponseModel):
    status: str
    key: str
    cleared: bool


class AppServerThreadArchiveResponse(ResponseModel):
    status: str
    thread_id: str
    archived: bool


class AppServerThreadResetAllResponse(ResponseModel):
    status: str
    cleared: bool


class TokenTotalsResponse(ResponseModel):
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int


class RepoUsageResponse(ResponseModel):
    mode: str
    repo: str
    codex_home: str
    since: Optional[str]
    until: Optional[str]
    status: str
    events: int
    totals: TokenTotalsResponse
    latest_rate_limits: Optional[Dict[str, Any]]


class UsageSeriesEntryResponse(ResponseModel):
    key: str
    model: Optional[str]
    token_type: Optional[str]
    total: int
    values: List[int]


class UsageSeriesResponse(ResponseModel):
    mode: str
    repo: str
    codex_home: str
    since: Optional[str]
    until: Optional[str]
    status: str
    bucket: str
    segment: str
    buckets: List[str]
    series: List[UsageSeriesEntryResponse]


class SystemHealthResponse(ResponseModel):
    status: str
    mode: str
    base_path: str
    asset_version: Optional[str] = None


class SystemUpdateResponse(ResponseModel):
    status: str
    message: str
    target: str


class SystemUpdateStatusResponse(ResponseModel):
    status: str
    message: str


class SystemUpdateCheckResponse(ResponseModel):
    status: str
    update_available: bool
    message: str
    local_commit: Optional[str] = None
    remote_commit: Optional[str] = None


class ReviewStartRequest(Payload):
    agent: Optional[str] = None
    model: Optional[str] = None
    reasoning: Optional[str] = None
    max_wallclock_seconds: Optional[int] = Field(
        default=None,
        validation_alias=AliasChoices("max_wallclock_seconds", "maxWallclockSeconds"),
    )


class ReviewStatusResponse(ResponseModel):
    review: Dict[str, Any]


class ReviewControlResponse(ResponseModel):
    status: str
    detail: Optional[str] = None


# Ticket CRUD schemas


class TicketCreateRequest(Payload):
    agent: str = "codex"
    title: Optional[str] = None
    goal: Optional[str] = None
    body: str = ""


class TicketUpdateRequest(Payload):
    content: str  # Full markdown with frontmatter


class TicketResponse(ResponseModel):
    path: str
    index: int
    frontmatter: Dict[str, Any]
    body: str


class TicketDeleteResponse(ResponseModel):
    status: str
    index: int
    path: str
