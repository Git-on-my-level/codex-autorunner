export const READ_MODEL_CONTRACT_VERSION = 'web-read-models.v1' as const;

export type ReadModelContractVersion = typeof READ_MODEL_CONTRACT_VERSION;

export type ProjectionCursor = {
  value: string;
  sequence: number;
  source: string;
  issuedAt: string;
};

export type ProjectionRevision = {
  value: string;
  sourceKind: string;
  sourceId: string;
  updatedAt: string;
};

export type RepairPolicy = {
  snapshotRoute: string;
  cursorQueryParam: 'after';
  gapEventType: 'projection.cursor_gap';
  behavior: 'repair_snapshot_required';
};

export type CursorGapRepair = RepairPolicy & {
  requestedCursor: number;
  latestCursor: number;
};

export type PageWindow = {
  limit: number;
  nextCursor?: string | null;
  previousCursor?: string | null;
  totalEstimate?: number | null;
  totalIsExact: boolean;
};

export type ReadModelOperation = 'upsert' | 'patch' | 'delete' | 'reorder' | 'invalidate' | 'reset';

export type ReadModelEventEnvelope<TEvent extends string = string, TEntityKind extends string = string> = {
  contractVersion: ReadModelContractVersion;
  eventType: TEvent;
  cursor: ProjectionCursor;
  entityKind: TEntityKind;
  entityId: string;
  operation: ReadModelOperation;
  generatedAt: string;
  sourceRevision?: ProjectionRevision | null;
};

export type ChatIndexRow = {
  /**
   * Timestamp semantics: lastVisibleMessageAt is the newest user-visible
   * conversation input, lastLifecycleUpdateAt is thread/binding lifecycle
   * churn, lastInternalUpdateAt is runtime/delivery bookkeeping,
   * lastSortActivityAt is the backend-owned row-order clock, and
   * lastActivityAt is a compatibility alias for lastSortActivityAt.
   *
   * Title semantics: title/displayTitle are backend-resolved human display
   * strings, technicalTitle preserves the stable identifier, and binding
   * display names describe attached delivery surfaces.
   */
  chatId: string;
  surface: 'pma' | 'file_chat' | 'telegram' | 'discord' | 'app_server' | 'other';
  title: string;
  displayTitle?: string | null;
  technicalTitle?: string | null;
  primarySurface?: Record<string, unknown> | null;
  surfaceBindings?: Record<string, unknown>[];
  bindingDisplayName?: string | null;
  bindingDisplayNames?: string[];
  lifecycle?: string | null;
  runtimeStatus?: string | null;
  archiveState?: 'active' | 'archived' | null;
  status: 'waiting' | 'running' | 'idle' | 'archived' | 'failed';
  unreadCount: number;
  lastActivityAt?: string | null;
  lastVisibleMessageAt?: string | null;
  lastLifecycleUpdateAt?: string | null;
  lastInternalUpdateAt?: string | null;
  lastSortActivityAt?: string | null;
  sortKey?: Record<string, unknown> | null;
  resourceKind?: string | null;
  resourceId?: string | null;
  workspaceRoot?: string | null;
  repoId?: string | null;
  worktreeId?: string | null;
  ticketId?: string | null;
  runId?: string | null;
  agent?: string | null;
  agentProfile?: string | null;
  chatKind?: 'pma' | 'coding_agent' | null;
  model?: string | null;
  groupId?: string | null;
  debug?: Record<string, unknown> | null;
};

export type ChatIndexGroup = {
  groupId: string;
  kind: 'ticket_run' | 'surface' | 'repo' | 'worktree';
  label: string;
  childCount: number;
  waitingCount?: number;
  runningCount?: number;
  unreadCount?: number;
  lastActivityAt?: string | null;
  lastVisibleMessageAt?: string | null;
  lastLifecycleUpdateAt?: string | null;
  lastInternalUpdateAt?: string | null;
  lastSortActivityAt?: string | null;
  debug?: Record<string, unknown> | null;
  expandedChildWindow?: PageWindow | null;
};

export type ChatIndexCounters = {
  total: number;
  waiting: number;
  running: number;
  unread: number;
  archived: number;
};

export type ChatIndexSnapshot = {
  contractVersion: ReadModelContractVersion;
  kind: 'chat.index.snapshot';
  cursor: ProjectionCursor;
  window: PageWindow;
  filter: 'all' | 'waiting' | 'active' | 'unread' | 'archived' | 'ticket_runs' | 'external';
  query?: string | null;
  rows: ChatIndexRow[];
  groups: ChatIndexGroup[];
  counters: ChatIndexCounters;
  repair: RepairPolicy;
};

export type ChatIndexPatch = {
  rows: ChatIndexRow[];
  groups: ChatIndexGroup[];
  removedRowIds: string[];
  removedGroupIds: string[];
  order?: string[] | null;
  counters?: ChatIndexCounters | null;
};

export type ChatIndexPatchEvent = {
  envelope: ReadModelEventEnvelope<'chat.index.patch' | 'projection.cursor_gap', 'chat'>;
  patch: ChatIndexPatch;
  repair?: CursorGapRepair | null;
};

export type ChatTimelineIdentity = {
  timelineItemId: string;
  progressItemIds: string[];
  correlationId?: string | null;
};

export type ChatTimelineProvenance = {
  sourceEventIds: unknown[];
  progressEventIds: unknown[];
  cursorEventId?: string | null;
};

export type ChatTimelineItem = {
  itemId: string;
  kind: 'user_message' | 'assistant_message' | 'tool_event' | 'progress' | 'artifact' | 'system';
  role?: 'user' | 'assistant' | 'tool' | 'system' | null;
  managedTurnId?: string | null;
  orderKey?: string | null;
  section?: 'user_message' | 'activity' | 'assistant_message' | 'terminal_metadata' | 'thread_metadata' | null;
  sectionOrder?: number | null;
  createdAt: string;
  text?: string | null;
  artifactIds: string[];
  clientMessageId?: string | null;
  backendMessageId?: string | null;
  identity?: ChatTimelineIdentity | null;
  provenance?: ChatTimelineProvenance | null;
};

export type ChatQueueSummary = {
  depth: number;
  activeTurnId?: string | null;
  queuedTurnIds: string[];
};

export type ChatArtifactSummary = {
  artifactId: string;
  name: string;
  kind: string;
  href?: string | null;
  updatedAt?: string | null;
};

export type ChatThreadProjection = {
  chatId: string;
  surface: string;
  title: string;
  status: 'waiting' | 'running' | 'idle' | 'archived' | 'failed';
  repoId?: string | null;
  worktreeId?: string | null;
  ticketId?: string | null;
  runId?: string | null;
  agent?: string | null;
  agentProfile?: string | null;
  chatKind?: 'pma' | 'coding_agent' | null;
  model?: string | null;
  archived: boolean;
};

export type ChatDetailSnapshot = {
  contractVersion: ReadModelContractVersion;
  kind: 'chat.detail.snapshot';
  cursor: ProjectionCursor;
  thread: ChatThreadProjection;
  timelineWindow: PageWindow;
  timeline: ChatTimelineItem[];
  queue: ChatQueueSummary;
  artifacts: ChatArtifactSummary[];
  repair: RepairPolicy;
};

export type ChatDetailPatch = {
  thread?: ChatThreadProjection | null;
  appendedTimeline: ChatTimelineItem[];
  patchedTimeline: ChatTimelineItem[];
  removedTimelineIds: string[];
  queue?: ChatQueueSummary | null;
  artifacts: ChatArtifactSummary[];
};

export type ChatDetailPatchEvent = {
  envelope: ReadModelEventEnvelope<'chat.detail.patch', 'chat'>;
  patch: ChatDetailPatch;
};

export type RepoTopology = {
  repoId: string;
  label: string;
  path: string;
  archived: boolean;
  isPinned?: boolean;
  destinationId?: string | null;
  childWorktreeIds: string[];
  worktreeSetupCommands?: string[] | null;
  chatBound?: boolean;
  chatBindingCount?: number;
  chatBindingSources?: Record<string, number>;
  chatBindingDisplayNames?: string[];
};

export type WorktreeTopology = {
  worktreeId: string;
  repoId: string;
  label: string;
  path: string;
  branch?: string | null;
  archived: boolean;
  destinationId?: string | null;
  chatBound?: boolean;
  chatBindingCount?: number;
  chatBindingSources?: Record<string, number>;
  chatBindingDisplayNames?: string[];
};

export type RepoWorktreeTopologySnapshot = {
  contractVersion: ReadModelContractVersion;
  kind: 'repo_worktree.topology.snapshot';
  cursor: ProjectionCursor;
  window: PageWindow;
  repos: RepoTopology[];
  worktrees: WorktreeTopology[];
  repair: RepairPolicy;
};

export type RuntimeProjection = {
  entityKind: 'repo' | 'worktree';
  entityId: string;
  gitDirty?: boolean | null;
  gitAhead?: number | null;
  gitBehind?: number | null;
  activeRunId?: string | null;
  activeRunStatus?: string | null;
  waitingTicketCount: number;
  runningTicketCount: number;
  chatCount: number;
  cleanupBlockers: string[];
  updatedAt?: string | null;
};

export type RepoWorktreeRuntimeSnapshot = {
  contractVersion: ReadModelContractVersion;
  kind: 'repo_worktree.runtime.snapshot';
  cursor: ProjectionCursor;
  window: PageWindow;
  runtime: RuntimeProjection[];
  repair: RepairPolicy;
};

export type RepoWorktreePatch = {
  topologyRepos: RepoTopology[];
  topologyWorktrees: WorktreeTopology[];
  runtime: RuntimeProjection[];
  removedRepoIds: string[];
  removedWorktreeIds: string[];
  order?: string[] | null;
};

export type RepoWorktreePatchEvent = {
  envelope: ReadModelEventEnvelope<'repo.topology.patch' | 'repo.runtime.patch' | 'worktree.topology.patch' | 'worktree.runtime.patch', 'repo' | 'worktree'>;
  patch: RepoWorktreePatch;
};

export type RepoWorktreeDetailSnapshot = {
  contractVersion: ReadModelContractVersion;
  kind: 'repo_worktree.detail.snapshot';
  cursor: ProjectionCursor;
  ownerKind: 'repo' | 'worktree';
  ownerId: string;
  identity: Record<string, unknown>;
  parentLinks: Record<string, unknown>;
  topology: Record<string, unknown>;
  runtime: Record<string, unknown>;
  ticketQueue: Record<string, unknown>[];
  runQueue: Record<string, unknown>[];
  chatQueue: Record<string, unknown>[];
  contextspaceSummary: Record<string, unknown>[];
  currentArtifacts: Record<string, unknown>[];
  ticketWindow: PageWindow;
  runWindow: PageWindow;
  chatWindow: PageWindow;
  artifactWindow: PageWindow;
  repair: RepairPolicy;
};

export type TicketProjection = {
  ticketId: string;
  routeId: string;
  title: string;
  status: 'queued' | 'waiting' | 'running' | 'blocked' | 'done' | 'failed' | 'invalid';
  ownerKind: 'repo' | 'worktree';
  ownerId: string;
  agent?: string | null;
  model?: string | null;
  done: boolean;
  updatedAt?: string | null;
};

export type TicketQueueSibling = {
  ticketId: string;
  routeId: string;
  title: string;
  status: string;
  previousTicketId?: string | null;
  nextTicketId?: string | null;
};

export type RunProjection = {
  runId: string;
  status: string;
  startedAt?: string | null;
  finishedAt?: string | null;
  workerActivity?: string | null;
};

export type TicketDetailSnapshot = {
  contractVersion: ReadModelContractVersion;
  kind: 'ticket.detail.snapshot';
  cursor: ProjectionCursor;
  ticket: TicketProjection;
  siblings: TicketQueueSibling[];
  linkedRun?: RunProjection | null;
  linkedChats: ChatIndexRow[];
  artifacts: ChatArtifactSummary[];
  dispatchWindow: PageWindow;
  dispatches: Record<string, unknown>[];
  repair: RepairPolicy;
  ticketDetail: Record<string, unknown>;
  ticketQueue: Record<string, unknown>[];
  runQueue: Record<string, unknown>[];
  chatQueue: Record<string, unknown>[];
};

export type TicketDetailPatch = {
  ticket?: TicketProjection | null;
  siblings: TicketQueueSibling[];
  linkedRun?: RunProjection | null;
  linkedChats: ChatIndexRow[];
  artifacts: ChatArtifactSummary[];
  dispatches: Record<string, unknown>[];
};

export type TicketDetailPatchEvent = {
  envelope: ReadModelEventEnvelope<'ticket.detail.patch', 'ticket'>;
  patch: TicketDetailPatch;
};

export type ReadModelSnapshot =
  | ChatIndexSnapshot
  | ChatDetailSnapshot
  | RepoWorktreeTopologySnapshot
  | RepoWorktreeRuntimeSnapshot
  | RepoWorktreeDetailSnapshot
  | TicketDetailSnapshot;

export type ReadModelPatchEvent =
  | ChatIndexPatchEvent
  | ChatDetailPatchEvent
  | RepoWorktreePatchEvent
  | TicketDetailPatchEvent;

export function mapReadModelContract<T extends ReadModelSnapshot | ReadModelPatchEvent>(payload: unknown): T {
  const record = asRecord(payload);
  const version = record.contractVersion ?? asRecord(record.envelope).contractVersion;
  if (version !== READ_MODEL_CONTRACT_VERSION) {
    throw new Error(`Unsupported read model contract version: ${String(version)}`);
  }
  return JSON.parse(JSON.stringify(record)) as T;
}

export function readModelRepairRequired(event: ReadModelPatchEvent): boolean {
  const eventType: string = event.envelope.eventType;
  return eventType === 'projection.cursor_gap' || event.envelope.operation === 'reset';
}

function asRecord(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('Expected read model contract object.');
  }
  return value as Record<string, unknown>;
}
