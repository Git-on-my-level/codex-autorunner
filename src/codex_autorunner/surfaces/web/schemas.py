"""
Pydantic request/response schemas for web and API routes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)

from ...core.car_context import CarContextProfile
from ...core.text_utils import _normalize_text
from ...flows.review.models import ReviewStateSnapshot, ReviewStatus
from ...integrations.chat.approval_modes import normalize_approval_mode


class Payload(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class ResponseModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


def _validate_supported_payload_keys(
    data: dict[str, Any],
    *,
    supported_keys: set[str] | frozenset[str],
    label: str,
) -> None:
    unknown_keys = sorted(
        str(key)
        for key in data.keys()
        if isinstance(key, str) and key not in supported_keys
    )
    if unknown_keys:
        raise ValueError(
            f"Unsupported {label} keys: "
            + ", ".join(unknown_keys)
            + ". Valid keys: "
            + ", ".join(sorted(supported_keys))
        )


def _normalize_filter_payload(
    *,
    raw_filter: Any,
    supported_keys: set[str] | frozenset[str],
    key_aliases: dict[str, str],
    label: str,
) -> dict[str, str]:
    if raw_filter is None:
        return {}
    if not isinstance(raw_filter, dict):
        raise ValueError(f"{label} must be an object")

    normalized_filter: dict[str, str] = {}
    unknown_filter_keys: list[str] = []
    for raw_key, raw_value in raw_filter.items():
        if not isinstance(raw_key, str):
            unknown_filter_keys.append(str(raw_key))
            continue
        normalized_key = key_aliases.get(raw_key, raw_key)
        if normalized_key not in supported_keys:
            unknown_filter_keys.append(raw_key)
            continue
        normalized_value = _normalize_text(raw_value)
        if normalized_value is None:
            raise ValueError(f"{label}.{normalized_key} must be a non-empty string")
        existing = normalized_filter.get(normalized_key)
        if existing is not None and existing != normalized_value:
            raise ValueError(
                f"Conflicting {label} values for {normalized_key}: "
                f"{existing!r} vs {normalized_value!r}"
            )
        normalized_filter[normalized_key] = normalized_value

    if unknown_filter_keys:
        raise ValueError(
            f"Unsupported {label} keys: "
            + ", ".join(sorted(unknown_filter_keys))
            + ". Valid keys: "
            + ", ".join(sorted(supported_keys))
        )
    return normalized_filter


def _merge_normalized_filter(
    data: dict[str, Any],
    normalized_filter: dict[str, str],
    *,
    label: str,
) -> dict[str, Any]:
    for key, value in normalized_filter.items():
        existing = _normalize_text(data.get(key))
        if existing is not None and existing != value:
            raise ValueError(
                f"Conflicting values for {key}: top-level={existing!r}, "
                f"{label}={value!r}"
            )
        if existing is None:
            data[key] = value
    return data


class ContextspaceWriteRequest(Payload):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    content: str = ""


class ContextspaceDocKindInfo(ResponseModel):
    kind: str
    path: str
    label: str
    description: str


class ContextspaceResponse(ResponseModel):
    active_context: str
    decisions: str
    spec: str
    kinds: List[ContextspaceDocKindInfo] = Field(default_factory=list)


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


class LocalRunArchiveSummary(ResponseModel):
    run_id: str
    archived_at: Optional[str] = None
    has_tickets: bool = False
    has_runs: bool = False


class LocalRunArchivesResponse(ResponseModel):
    archives: List[LocalRunArchiveSummary]


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
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    once: bool = False
    agent: Optional[str] = None
    model: Optional[str] = None
    reasoning: Optional[str] = None


class HubCreateRepoRequest(Payload):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

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
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    force: bool = False
    force_attestation: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("force_attestation", "forceAttestation"),
    )
    delete_dir: bool = True
    delete_worktrees: bool = False


class HubCreateAgentWorkspaceRequest(Payload):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    workspace_id: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("workspace_id", "workspaceId", "id"),
    )
    runtime: str
    enabled: bool = True
    display_name: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("display_name", "displayName", "name"),
    )


class HubUpdateAgentWorkspaceRequest(Payload):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    enabled: Optional[bool] = None
    display_name: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("display_name", "displayName", "name"),
    )


class HubRemoveAgentWorkspaceRequest(Payload):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    delete_dir: bool = Field(
        default=False, validation_alias=AliasChoices("delete_dir", "deleteDir")
    )


class HubDeleteAgentWorkspaceRequest(Payload):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    delete_dir: bool = Field(
        default=True, validation_alias=AliasChoices("delete_dir", "deleteDir")
    )


class HubCreateWorktreeRequest(Payload):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

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
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    worktree_repo_id: str = Field(
        validation_alias=AliasChoices("worktree_repo_id", "worktreeRepoId")
    )
    delete_branch: bool = False
    delete_remote: bool = False
    force: bool = False
    force_attestation: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("force_attestation", "forceAttestation"),
    )
    archive: bool = True
    force_archive: bool = Field(
        default=False, validation_alias=AliasChoices("force_archive", "forceArchive")
    )
    archive_note: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("archive_note", "archiveNote")
    )
    archive_profile: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("archive_profile", "archiveProfile")
    )


class HubArchiveWorktreeRequest(Payload):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    worktree_repo_id: str = Field(
        validation_alias=AliasChoices("worktree_repo_id", "worktreeRepoId")
    )
    archive_note: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("archive_note", "archiveNote")
    )
    archive_profile: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("archive_profile", "archiveProfile")
    )


class HubArchiveRepoStateRequest(Payload):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    repo_id: str = Field(validation_alias=AliasChoices("repo_id", "repoId"))
    archive_note: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("archive_note", "archiveNote")
    )
    archive_profile: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("archive_profile", "archiveProfile")
    )


class HubArchiveWorktreeResponse(ResponseModel):
    snapshot_id: str
    snapshot_path: str
    meta_path: str
    status: str
    file_count: int
    total_bytes: int
    flow_run_count: int
    latest_flow_run_id: Optional[str]


class HubArchiveWorktreeStateResponse(ResponseModel):
    snapshot_id: Optional[str]
    snapshot_path: Optional[str]
    meta_path: Optional[str]
    status: str
    file_count: int
    total_bytes: int
    flow_run_count: int
    latest_flow_run_id: Optional[str]
    archived_paths: list[str]
    reset_paths: list[str]
    archived_thread_ids: list[str] = []
    archived_thread_count: int = 0


class HubArchiveRepoStateResponse(HubArchiveWorktreeStateResponse):
    pass


class AppServerThreadResetRequest(Payload):
    key: str = Field(
        validation_alias=AliasChoices("key", "feature", "feature_key", "featureKey")
    )


class AppServerThreadArchiveRequest(Payload):
    thread_id: str = Field(validation_alias=AliasChoices("thread_id", "threadId", "id"))


class PmaManagedThreadCreateRequest(Payload):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    agent: Optional[str] = None
    profile: Optional[str] = None
    resource_kind: Optional[Literal["repo", "agent_workspace"]] = Field(
        default=None, validation_alias=AliasChoices("resource_kind", "resourceKind")
    )
    resource_id: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("resource_id", "resourceId")
    )
    repo_id: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("repo_id", "repoId"),
        exclude=True,
    )
    workspace_root: Optional[str] = None
    name: Optional[str] = None
    notify_on: Optional[Literal["terminal"]] = Field(
        default=None, validation_alias=AliasChoices("notify_on", "notifyOn")
    )
    terminal_followup: Optional[bool] = Field(
        default=None,
        validation_alias=AliasChoices("terminal_followup", "terminalFollowup"),
    )
    notify_lane: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("notify_lane", "notifyLane")
    )
    notify_once: bool = Field(
        default=True, validation_alias=AliasChoices("notify_once", "notifyOnce")
    )
    context_profile: Optional[CarContextProfile] = Field(
        default=None,
        validation_alias=AliasChoices("context_profile", "contextProfile"),
    )
    approval_mode: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("approval_mode", "approvalMode"),
    )
    notify_on_explicit: bool = Field(default=False, exclude=True)
    terminal_followup_explicit: bool = Field(default=False, exclude=True)
    notify_lane_explicit: bool = Field(default=False, exclude=True)
    notify_once_explicit: bool = Field(default=False, exclude=True)

    @field_validator("approval_mode")
    @classmethod
    def _normalize_approval_mode(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = normalize_approval_mode(value)
        if normalized is None:
            raise ValueError("approval_mode is invalid")
        return normalized

    @model_validator(mode="before")
    @classmethod
    def _capture_followup_intent(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        repo_id = payload.get("repo_id", payload.get("repoId"))
        if repo_id is not None:
            raise ValueError(
                "repo_id is not supported; use resource_kind='repo' with resource_id"
            )
        payload["notify_on_explicit"] = any(
            key in value for key in ("notify_on", "notifyOn")
        )
        payload["terminal_followup_explicit"] = any(
            key in value for key in ("terminal_followup", "terminalFollowup")
        )
        payload["notify_lane_explicit"] = any(
            key in value for key in ("notify_lane", "notifyLane")
        )
        payload["notify_once_explicit"] = any(
            key in value for key in ("notify_once", "notifyOnce")
        )
        return payload


class SessionSettingsRequest(Payload):
    model_config = ConfigDict(extra="forbid")

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


# Keep an explicit module-level reference so dead-code heuristics treat these
# request schemas as part of the public route contract surface.
_GITHUB_REQUEST_MODELS = (
    GithubIssueRequest,
    GithubContextRequest,
    GithubPrSyncRequest,
)


class HubPinRepoRequest(Payload):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    pinned: bool = True


class HubDestinationMountRequest(Payload):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    source: Optional[str] = None
    target: Optional[str] = None
    read_only: Optional[StrictBool] = Field(
        default=None,
        validation_alias=AliasChoices("read_only", "readOnly", "readonly"),
    )


class HubDestinationSetRequest(Payload):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    kind: str
    image: Optional[str] = None
    container_name: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("container_name", "containerName", "name"),
    )
    workdir: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("workdir", "workDir"),
    )
    profile: Optional[str] = None
    env_passthrough: Optional[List[str]] = Field(
        default=None,
        validation_alias=AliasChoices("env_passthrough", "envPassthrough"),
    )
    env: Optional[Dict[str, str]] = Field(
        default=None,
        validation_alias=AliasChoices("env", "explicit_env", "explicitEnv"),
    )
    mounts: Optional[List[HubDestinationMountRequest]] = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy_env_alias(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        raw_env = data.get("env")
        if raw_env is None or not isinstance(raw_env, list):
            return data
        if "env_passthrough" in data or "envPassthrough" in data:
            return data
        normalized = dict(data)
        normalized["env_passthrough"] = raw_env
        normalized.pop("env", None)
        return normalized

    def normalized_payload(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class HubAgentWorkspaceSummaryResponse(ResponseModel):
    id: str
    runtime: str
    path: str
    display_name: str
    enabled: bool
    exists_on_disk: bool
    effective_destination: Dict[str, Any]
    resource_kind: str


class HubAgentWorkspaceResponse(HubAgentWorkspaceSummaryResponse):
    configured_destination: Optional[Dict[str, Any]] = None
    source: Optional[str] = None
    issues: List[str] = Field(default_factory=list)


class HubAgentWorkspaceListResponse(ResponseModel):
    agent_workspaces: List[HubAgentWorkspaceSummaryResponse]


class HubAgentWorkspaceMutationResponse(ResponseModel):
    status: str
    workspace_id: str
    delete_dir: bool


class HubDispatchPayload(ResponseModel):
    mode: str
    title: Optional[str] = None
    body: str
    extra: Dict[str, Any] = Field(default_factory=dict)
    is_handoff: bool = False


class HubLatestDispatchResponse(ResponseModel):
    seq: int
    dir: str
    dispatch: Optional[HubDispatchPayload] = None
    errors: List[str] = Field(default_factory=list)
    files: List[str] = Field(default_factory=list)
    turn_summary_seq: Optional[int] = None
    turn_summary: Optional[HubDispatchPayload] = None


class HubMessageSnapshotResponse(ResponseModel):
    generated_at: str
    items: Optional[List[Dict[str, Any]]] = None
    pma_threads: Optional[List[Dict[str, Any]]] = None
    pma_files_detail: Optional[Dict[str, List[Dict[str, Any]]]] = None
    automation: Optional[Dict[str, Any]] = None
    action_queue: Optional[List[Dict[str, Any]]] = None


class HubMessagesFreshnessResponse(ResponseModel):
    schema_version: int
    generated_at: str
    stale_threshold_seconds: int
    sections: Dict[str, Any]


class HubMessagesResponse(ResponseModel):
    generated_at: str
    items: Optional[List[Dict[str, Any]]] = None
    freshness: Optional[HubMessagesFreshnessResponse] = None
    pma_threads: Optional[List[Dict[str, Any]]] = None
    pma_files_detail: Optional[Dict[str, List[Dict[str, Any]]]] = None
    automation: Optional[Dict[str, Any]] = None
    action_queue: Optional[List[Dict[str, Any]]] = None


class SessionStopRequest(Payload):
    session_id: Optional[str] = None
    repo_path: Optional[str] = None


class TemplateRepoSummary(ResponseModel):
    id: str
    url: str
    trusted: bool
    default_ref: str


class TemplateReposResponse(ResponseModel):
    enabled: bool
    repos: List[TemplateRepoSummary]


class TemplateRepoCreateRequest(Payload):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str
    url: str
    trusted: bool = False
    default_ref: str = Field(
        default="main", validation_alias=AliasChoices("default_ref", "defaultRef")
    )


class TemplateRepoUpdateRequest(Payload):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    url: Optional[str] = None
    trusted: Optional[bool] = None
    default_ref: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("default_ref", "defaultRef")
    )


class TemplateFetchRequest(Payload):
    template: str


class TemplateFetchResponse(ResponseModel):
    content: str
    repo_id: str
    path: str
    ref: str
    commit_sha: str
    blob_sha: str
    trusted: bool
    scan_decision: Optional[Dict[str, Any]] = None


class TemplateApplyRequest(Payload):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    template: str
    at: Optional[int] = None
    next_index: bool = Field(
        default=True, validation_alias=AliasChoices("next_index", "nextIndex")
    )
    suffix: Optional[str] = None
    set_agent: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("set_agent", "setAgent")
    )
    include_provenance: bool = Field(
        default=False,
        validation_alias=AliasChoices("include_provenance", "includeProvenance"),
    )


class TemplateApplyResponse(ResponseModel):
    created_path: str
    index: int
    filename: str
    metadata: Dict[str, Any]


class SystemUpdateRequest(Payload):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    target: Optional[str] = None
    force: bool = False


class SystemUpdateTargetOption(ResponseModel):
    value: str
    label: str
    description: Optional[str] = None
    includes_web: bool = False
    restart_notice: Optional[str] = None


class SystemUpdateTargetsResponse(ResponseModel):
    targets: List[SystemUpdateTargetOption]
    default_target: str


class HubJobResponse(ResponseModel):
    job_id: str
    kind: str
    status: str
    created_at: str
    started_at: Optional[str]
    finished_at: Optional[str]
    result: Optional[Dict[str, Any]]
    error: Optional[str]


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
    source_confidence: Optional[Dict[str, Any]] = None


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
    requires_confirmation: bool = False


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
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    agent: Optional[str] = None
    model: Optional[str] = None
    reasoning: Optional[str] = None
    max_wallclock_seconds: Optional[int] = Field(
        default=None,
        validation_alias=AliasChoices("max_wallclock_seconds", "maxWallclockSeconds"),
    )


class ReviewStatusResponse(ResponseModel):
    review: ReviewStateSnapshot


class ReviewControlResponse(ResponseModel):
    status: ReviewStatus
    detail: Optional[str] = None


# Ticket CRUD schemas


class TicketCreateRequest(Payload):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    agent: str = "codex"
    title: Optional[str] = None
    goal: Optional[str] = None
    body: str = ""
    profile: Optional[str] = None

    @field_validator("profile")
    @classmethod
    def _normalize_profile(cls, value: Optional[str]) -> Optional[str]:
        return _normalize_text(value)


class TicketUpdateRequest(Payload):
    content: str  # Full markdown with frontmatter


class TicketReorderRequest(Payload):
    source_index: int = Field(
        validation_alias=AliasChoices("source_index", "sourceIndex")
    )
    destination_index: int = Field(
        validation_alias=AliasChoices("destination_index", "destinationIndex")
    )
    place_after: bool = Field(
        default=False, validation_alias=AliasChoices("place_after", "placeAfter")
    )


class TicketResponse(ResponseModel):
    path: str
    index: int
    chat_key: Optional[str] = None
    frontmatter: Dict[str, Any]
    body: str


class TicketDeleteResponse(ResponseModel):
    status: str
    index: int
    path: str


class TicketReorderResponse(ResponseModel):
    status: str
    source_index: int
    destination_index: int
    place_after: bool = False
    lint_errors: list[str] = []


class TicketBulkSetAgentRequest(Payload):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    agent: str
    profile: Optional[str] = None
    range: Optional[str] = None
    profile_explicit: bool = Field(default=False, exclude=True)

    @field_validator("profile")
    @classmethod
    def _normalize_profile(cls, value: Optional[str]) -> Optional[str]:
        return _normalize_text(value)

    @model_validator(mode="before")
    @classmethod
    def _capture_profile_intent(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        payload["profile_explicit"] = "profile" in value
        return payload


class TicketBulkClearModelRequest(Payload):
    model_config = ConfigDict(extra="forbid")

    range: Optional[str] = None


class TicketBulkUpdateResponse(ResponseModel):
    status: str
    updated: int
    skipped: int
    errors: list[str] = []
    lint_errors: list[str] = []


class PmaManagedThreadMessageRequest(Payload):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    message: str
    busy_policy: Optional[Literal["queue", "interrupt", "reject"]] = Field(
        default=None, validation_alias=AliasChoices("busy_policy", "busyPolicy")
    )
    model: Optional[str] = None
    reasoning: Optional[str] = None
    defer_execution: bool = Field(
        default=False,
        validation_alias=AliasChoices("defer_execution", "deferExecution"),
    )
    notify_on: Optional[Literal["terminal"]] = Field(
        default=None, validation_alias=AliasChoices("notify_on", "notifyOn")
    )
    terminal_followup: Optional[bool] = Field(
        default=None,
        validation_alias=AliasChoices("terminal_followup", "terminalFollowup"),
    )
    notify_lane: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("notify_lane", "notifyLane")
    )
    notify_once: bool = Field(
        default=True, validation_alias=AliasChoices("notify_once", "notifyOnce")
    )
    notify_on_explicit: bool = Field(default=False, exclude=True)
    terminal_followup_explicit: bool = Field(default=False, exclude=True)
    notify_lane_explicit: bool = Field(default=False, exclude=True)
    notify_once_explicit: bool = Field(default=False, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def _capture_followup_intent(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        payload["notify_on_explicit"] = any(
            key in value for key in ("notify_on", "notifyOn")
        )
        payload["terminal_followup_explicit"] = any(
            key in value for key in ("terminal_followup", "terminalFollowup")
        )
        payload["notify_lane_explicit"] = any(
            key in value for key in ("notify_lane", "notifyLane")
        )
        payload["notify_once_explicit"] = any(
            key in value for key in ("notify_once", "notifyOnce")
        )
        return payload


class PmaManagedThreadCompactRequest(Payload):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    summary: str
    reset_backend: bool = True


class PmaManagedThreadResumeRequest(Payload):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class PmaChatRequest(Payload):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    message: Optional[str] = None
    stream: bool = False
    agent: Optional[str] = None
    profile: Optional[str] = None
    model: Optional[str] = None
    reasoning: Optional[str] = None
    client_turn_id: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("client_turn_id", "clientTurnId")
    )


class PmaStopRequest(Payload):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    lane_id: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("lane_id", "laneId")
    )


class PmaNewSessionRequest(Payload):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    agent: Optional[str] = None
    profile: Optional[str] = None
    lane_id: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("lane_id", "laneId")
    )


class PmaSessionResetRequest(Payload):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    agent: Optional[str] = None
    profile: Optional[str] = None


class PmaHistoryCompactRequest(Payload):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    summary: Optional[str] = None
    agent: Optional[str] = None
    thread_id: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("thread_id", "threadId")
    )


class PmaThreadResetRequest(Payload):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    agent: Optional[str] = None
    profile: Optional[str] = None


class PmaAutomationSubscriptionCreateRequest(Payload):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    _SUPPORTED_PAYLOAD_KEYS = frozenset(
        {
            "event_types",
            "repo_id",
            "run_id",
            "thread_id",
            "lane_id",
            "from_state",
            "to_state",
            "reason",
            "timestamp",
            "filter",
        }
    )
    _SUPPORTED_FILTER_KEYS = frozenset(
        {
            "repo_id",
            "run_id",
            "thread_id",
            "lane_id",
            "from_state",
            "to_state",
        }
    )
    _FILTER_KEY_ALIASES = {
        "repoId": "repo_id",
        "runId": "run_id",
        "threadId": "thread_id",
        "laneId": "lane_id",
        "fromState": "from_state",
        "toState": "to_state",
    }

    event_types: Optional[List[str]] = Field(
        default=None, validation_alias=AliasChoices("event_types", "eventTypes")
    )
    repo_id: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("repo_id", "repoId")
    )
    run_id: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("run_id", "runId")
    )
    thread_id: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("thread_id", "threadId")
    )
    lane_id: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("lane_id", "laneId")
    )
    from_state: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("from_state", "fromState")
    )
    to_state: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("to_state", "toState")
    )
    reason: Optional[str] = None
    timestamp: Optional[str] = None
    filter: Optional[Dict[str, Any]] = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_event_type_inputs(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        normalized_event_types: list[str] = []
        seen: set[str] = set()

        def _append(candidate: Any) -> None:
            text = _normalize_text(candidate)
            if text is None:
                return
            lowered = text.lower()
            if lowered in seen:
                return
            seen.add(lowered)
            normalized_event_types.append(lowered)

        plural_value = data.get("event_types", data.get("eventTypes"))
        if isinstance(plural_value, (list, tuple, set)):
            for item in plural_value:
                _append(item)
        else:
            _append(plural_value)

        _append(data.get("event_type"))
        _append(data.get("eventType"))

        data.pop("event_type", None)
        data.pop("eventType", None)
        data.pop("eventTypes", None)
        if normalized_event_types:
            data["event_types"] = normalized_event_types
        elif "event_types" in data:
            data["event_types"] = []
        return data

    def normalized_payload(self) -> dict[str, Any]:
        data = dict(self.model_dump(exclude_none=True))
        raw_filter = data.pop("filter", None)
        _validate_supported_payload_keys(
            data,
            supported_keys=self._SUPPORTED_PAYLOAD_KEYS,
            label="subscription",
        )
        if raw_filter is None:
            return data
        normalized_filter = _normalize_filter_payload(
            raw_filter=raw_filter,
            supported_keys=self._SUPPORTED_FILTER_KEYS,
            key_aliases=self._FILTER_KEY_ALIASES,
            label="subscription filter",
        )
        return _merge_normalized_filter(data, normalized_filter, label="filter")


class PmaAutomationTimerCreateRequest(Payload):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    _SUPPORTED_PAYLOAD_KEYS = frozenset(
        {
            "timer_type",
            "delay_seconds",
            "idle_seconds",
            "due_at",
            "subscription_id",
            "timer_id",
            "repo_id",
            "run_id",
            "thread_id",
            "lane_id",
            "from_state",
            "to_state",
            "reason",
            "timestamp",
            "idempotency_key",
            "metadata",
            "filter",
        }
    )
    _SUPPORTED_FILTER_KEYS = frozenset(
        {
            "subscription_id",
            "repo_id",
            "run_id",
            "thread_id",
            "lane_id",
            "from_state",
            "to_state",
        }
    )
    _FILTER_KEY_ALIASES = {
        "subscriptionId": "subscription_id",
        "repoId": "repo_id",
        "runId": "run_id",
        "threadId": "thread_id",
        "laneId": "lane_id",
        "fromState": "from_state",
        "toState": "to_state",
    }

    timer_type: Optional[Literal["one_shot", "watchdog"]] = Field(
        default=None, validation_alias=AliasChoices("timer_type", "timerType")
    )
    delay_seconds: Optional[int] = Field(
        default=None, validation_alias=AliasChoices("delay_seconds", "delaySeconds")
    )
    idle_seconds: Optional[int] = Field(
        default=None, validation_alias=AliasChoices("idle_seconds", "idleSeconds")
    )
    due_at: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("due_at", "dueAt")
    )
    subscription_id: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("subscription_id", "subscriptionId"),
    )
    timer_id: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("timer_id", "timerId")
    )
    repo_id: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("repo_id", "repoId")
    )
    run_id: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("run_id", "runId")
    )
    thread_id: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("thread_id", "threadId")
    )
    lane_id: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("lane_id", "laneId")
    )
    from_state: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("from_state", "fromState")
    )
    to_state: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("to_state", "toState")
    )
    reason: Optional[str] = None
    timestamp: Optional[str] = None
    idempotency_key: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("idempotency_key", "idempotencyKey"),
    )
    metadata: Optional[Dict[str, Any]] = None
    filter: Optional[Dict[str, Any]] = None

    @field_validator("due_at")
    @classmethod
    def _validate_due_at(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("due_at must be a valid ISO-8601 timestamp") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @model_validator(mode="after")
    def _validate_timer_fields(self) -> "PmaAutomationTimerCreateRequest":
        if self.delay_seconds is not None and self.delay_seconds < 0:
            raise ValueError("delay_seconds must be >= 0")
        if self.idle_seconds is not None and self.idle_seconds <= 0:
            raise ValueError("idle_seconds must be > 0")
        if self.timer_type == "watchdog" and self.idle_seconds is None:
            raise ValueError("idle_seconds is required for watchdog timers")
        return self

    def normalized_payload(self) -> dict[str, Any]:
        data = dict(self.model_dump(exclude_none=True))
        raw_filter = data.pop("filter", None)
        _validate_supported_payload_keys(
            data,
            supported_keys=self._SUPPORTED_PAYLOAD_KEYS,
            label="timer",
        )
        if "metadata" in data and not isinstance(data.get("metadata"), dict):
            raise ValueError("metadata must be an object")
        if raw_filter is None:
            return data
        normalized_filter = _normalize_filter_payload(
            raw_filter=raw_filter,
            supported_keys=self._SUPPORTED_FILTER_KEYS,
            key_aliases=self._FILTER_KEY_ALIASES,
            label="timer filter",
        )
        return _merge_normalized_filter(data, normalized_filter, label="filter")


class PmaAutomationTimerTouchRequest(Payload):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    _SUPPORTED_PAYLOAD_KEYS = frozenset(
        {"reason", "timestamp", "due_at", "delay_seconds"}
    )

    reason: Optional[str] = None
    timestamp: Optional[str] = None
    due_at: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("due_at", "dueAt")
    )
    delay_seconds: Optional[int] = Field(
        default=None, validation_alias=AliasChoices("delay_seconds", "delaySeconds")
    )

    @field_validator("due_at")
    @classmethod
    def _validate_due_at(cls, value: Optional[str]) -> Optional[str]:
        return PmaAutomationTimerCreateRequest._validate_due_at(value)

    @model_validator(mode="after")
    def _validate_touch_fields(self) -> "PmaAutomationTimerTouchRequest":
        if self.delay_seconds is not None and self.delay_seconds < 0:
            raise ValueError("delay_seconds must be >= 0")
        return self

    def normalized_payload(self) -> dict[str, Any]:
        data = dict(self.model_dump(exclude_none=True))
        _validate_supported_payload_keys(
            data,
            supported_keys=self._SUPPORTED_PAYLOAD_KEYS,
            label="timer touch",
        )
        return data


class PmaAutomationTimerCancelRequest(Payload):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    _SUPPORTED_PAYLOAD_KEYS = frozenset({"reason", "timestamp"})

    reason: Optional[str] = None
    timestamp: Optional[str] = None

    def normalized_payload(self) -> dict[str, Any]:
        data = dict(self.model_dump(exclude_none=True))
        _validate_supported_payload_keys(
            data,
            supported_keys=self._SUPPORTED_PAYLOAD_KEYS,
            label="timer cancel",
        )
        return data
