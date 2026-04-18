export interface HubTicketFlow {
  status: string;
  done_count: number;
  total_count: number;
  current_step: number | null;
  failure?: Record<string, unknown> | null;
  failure_summary?: string | null;
}

export interface HubTicketFlowDisplay {
  status: string;
  status_label: string;
  status_icon: string;
  is_active: boolean;
  done_count: number;
  total_count: number;
  run_id: string | null;
}

export interface FreshnessPayload {
  generated_at?: string | null;
  recency_basis?: string | null;
  basis_at?: string | null;
  age_seconds?: number | null;
  stale_threshold_seconds?: number | null;
  is_stale?: boolean | null;
  status?: string | null;
}

export interface CleanupAllPreview {
  threads?: {
    archived_count?: number;
    by_repo?: Array<{ repo_id: string; count: number }>;
  };
  worktrees?: {
    archived_count?: number;
    items?: Array<{ id: string; branch?: string }>;
  };
  flow_runs?: {
    archived_count?: number;
    by_repo?: Array<{ repo_id: string; count: number }>;
  };
}

export interface HubRepo {
  id: string;
  path: string;
  display_name: string;
  enabled: boolean;
  auto_run: boolean;
  worktree_setup_commands?: string[] | null;
  kind: "base" | "worktree";
  worktree_of: string | null;
  branch: string | null;
  exists_on_disk: boolean;
  is_clean: boolean | null;
  initialized: boolean;
  init_error: string | null;
  status: string;
  lock_status: string;
  last_run_id: number | null;
  last_exit_code: number | null;
  last_run_started_at: string | null;
  last_run_finished_at: string | null;
  last_run_duration_seconds?: number | null;
  runner_pid: number | null;
  effective_destination: Record<string, unknown>;
  mounted: boolean;
  mount_error?: string | null;
  chat_bound?: boolean;
  chat_bound_thread_count?: number | null;
  pma_chat_bound_thread_count?: number | null;
  discord_chat_bound_thread_count?: number | null;
  telegram_chat_bound_thread_count?: number | null;
  non_pma_chat_bound_thread_count?: number | null;
  unbound_managed_thread_count?: number | null;
  cleanup_blocked_by_chat_binding?: boolean;
  has_car_state?: boolean;
  resource_kind: "repo";
  ticket_flow?: HubTicketFlow | null;
  ticket_flow_display?: HubTicketFlowDisplay | null;
}

export interface HubAgentWorkspace {
  id: string;
  runtime: string;
  path: string;
  display_name: string;
  enabled: boolean;
  exists_on_disk: boolean;
  effective_destination: Record<string, unknown>;
  resource_kind: "agent_workspace";
}

export interface HubAgentWorkspaceDetail extends HubAgentWorkspace {
  configured_destination: Record<string, unknown> | null;
  source: string;
  issues?: string[] | null;
}

export interface HubData {
  repos: HubRepo[];
  agent_workspaces: HubAgentWorkspace[];
  last_scan_at: string | null;
  pinned_parent_repo_ids?: string[];
}

export interface HubDestinationResponse {
  repo_id: string;
  configured_destination: Record<string, unknown> | null;
  effective_destination: Record<string, unknown>;
  source: string;
  issues?: string[];
}

export interface HubChannelEntry {
  key: string;
  display?: string | null;
  seen_at?: string | null;
  meta?: Record<string, unknown> | null;
  entry?: Record<string, unknown> | null;
  source?: string | null;
  provenance?: {
    source?: string | null;
    platform?: string | null;
    managed_thread_id?: string | null;
    agent?: string | null;
    status?: string | null;
    status_reason_code?: string | null;
    thread_kind?: string | null;
    run_id?: string | null;
    resource_kind?: string | null;
    resource_id?: string | null;
  } | null;
  repo_id?: string | null;
  resource_kind?: string | null;
  resource_id?: string | null;
  workspace_path?: string | null;
  active_thread_id?: string | null;
  channel_status?: string | null;
  status_label?: string | null;
  dirty?: boolean | null;
  diff_stats?: {
    insertions?: number | null;
    deletions?: number | null;
    files_changed?: number | null;
  } | null;
  token_usage?: {
    total_tokens?: number | null;
    input_tokens?: number | null;
    cached_input_tokens?: number | null;
    output_tokens?: number | null;
    reasoning_output_tokens?: number | null;
    turn_id?: string | null;
    timestamp?: string | null;
  } | null;
}

export interface HubChannelDirectoryResponse {
  entries: HubChannelEntry[];
}

export type HubChannelSource = "discord" | "telegram" | "pma_thread" | "unknown";

export interface HubUsageRepo {
  id: string;
  totals?: {
    total_tokens?: number;
    input_tokens?: number;
    cached_input_tokens?: number;
  };
  events?: number;
}

export interface HubUsageData {
  repos?: HubUsageRepo[];
  unmatched?: {
    events?: number;
    totals?: {
      total_tokens?: number;
    };
  };
  codex_home?: string;
  status?: string;
}

export interface SessionCachePayload<T> {
  at: number;
  value: T;
}

export interface HubJob {
  job_id: string;
  status?: string;
  error?: string;
  result?: Record<string, unknown>;
}

export interface UpdateCheckResponse {
  update_available?: boolean;
  message?: string;
}

export interface UpdateResponse {
  message?: string;
  requires_confirmation?: boolean;
}

export type HubFlowFilter =
  | "all"
  | "active"
  | "running"
  | "paused"
  | "completed"
  | "failed"
  | "idle";

export type HubSortOrder =
  | "repo_id"
  | "last_activity_desc"
  | "last_activity_asc"
  | "flow_progress_desc";

export interface HubViewPrefs {
  flowFilter: HubFlowFilter;
  sortOrder: HubSortOrder;
}

export type HubOpenPanel = "repos" | "agents";

export interface HubRepoGroup {
  base: HubRepo;
  worktrees: HubRepo[];
  filteredWorktrees: HubRepo[];
  matchesFilter: boolean;
  pinned: boolean;
  lastActivityMs: number;
  flowProgress: number;
}
