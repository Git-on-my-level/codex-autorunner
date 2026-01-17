"""
Pydantic request/response schemas for web and API routes.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class Payload(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class RunControlRequest(Payload):
    once: bool = False


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


class HubCleanupWorktreeRequest(Payload):
    worktree_repo_id: str = Field(
        validation_alias=AliasChoices("worktree_repo_id", "worktreeRepoId")
    )
    delete_branch: bool = False
    delete_remote: bool = False


class DocContentRequest(Payload):
    content: str = ""


class SnapshotRequest(Payload):
    pass


class DocChatPayload(Payload):
    message: Optional[str] = None
    stream: bool = False
    targets: Optional[List[str]] = None
    target: Optional[str] = None
    agent: Optional[str] = None
    model: Optional[str] = None
    reasoning: Optional[str] = None
    context_doc: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("context_doc", "contextDoc", "viewing"),
    )


class IngestSpecRequest(Payload):
    force: bool = False
    spec_path: Optional[str] = None
    message: Optional[str] = None
    agent: Optional[str] = None
    model: Optional[str] = None
    reasoning: Optional[str] = None


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


class ResponseModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


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
    terminal_idle_timeout_seconds: Optional[int]
    codex_model: str


class SessionSettingsResponse(ResponseModel):
    autorunner_model_override: Optional[str]
    autorunner_effort_override: Optional[str]
    autorunner_approval_policy: Optional[str]
    autorunner_sandbox_mode: Optional[str]
    autorunner_workspace_write_network: Optional[bool]


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


class DocsResponse(ResponseModel):
    todo: str
    progress: str
    opinions: str
    spec: str
    summary: str


class IngestSpecResponse(ResponseModel):
    status: str
    todo: str
    progress: str
    opinions: str
    spec: str
    summary: str
    patch: Optional[str] = None
    agent_message: Optional[str] = None


class AppServerThreadsResponse(ResponseModel):
    doc_chat: Dict[str, Optional[str]]
    spec_ingest: Optional[str] = None
    autorunner: Optional[str] = None
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


class DocWriteResponse(ResponseModel):
    kind: str
    content: str


class SnapshotResponse(ResponseModel):
    exists: bool
    content: str
    state: Dict[str, Any]


class SnapshotCreateResponse(ResponseModel):
    content: str
    truncated: bool
    state: Dict[str, Any]


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
