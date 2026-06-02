import { writable, type Readable } from 'svelte/store';
import type { ChatQueuedTurn } from '$lib/api/client';
import { chatFacetRequestIsEmpty, normalizeChatFacetRequest } from '$lib/api/readModelContracts';
import type {
  ChatArtifactSummary,
  ChatFacetCounts,
  ChatFacetRequest,
  ChatDetailPatchEvent,
  ChatDetailSnapshot,
  ChatIndexCounters,
  ChatIndexGroup,
  ChatIndexSnapshot,
  ChatIndexPatchEvent,
  ChatIndexRow,
  PageWindow,
  ChatQueueSummary,
  ChatThreadProjection,
  ChatTimelineItem,
  ProjectionCursor,
  RepoTopology,
  RepoWorktreeDetailSnapshot,
  RepoWorktreePatchEvent,
  RepoWorktreeRuntimeSnapshot,
  RepoWorktreeTopologySnapshot,
  RuntimeProjection,
  TicketDetailPatchEvent,
  TicketDetailSnapshot,
  TicketProjection,
  TicketQueueSibling,
  WorktreeTopology
} from '$lib/api/readModelContracts';
import {
  type ChatRunProgress,
  type SurfaceArtifact,
  type TicketSummary
} from '$lib/viewModels/domain';
import type { ChatTranscriptCard } from '$lib/viewModels/chat';

export type EntityKind =
  | 'chat'
  | 'chatGroup'
  | 'timeline'
  | 'repo'
  | 'worktree'
  | 'ticket'
  | 'run'
  | 'artifact'
  | 'agent'
  | 'model';

export type OptimisticMutationStatus = 'pending' | 'reconciled' | 'failed' | 'reverted';

export type OptimisticMutation = {
  reconciliationId: string;
  kind: 'send' | 'retire' | 'queue' | 'read-marker' | 'repo-pin';
  entityKind: EntityKind;
  entityId: string;
  status: OptimisticMutationStatus;
  createdAt: string;
  previousValue?: unknown;
};

export type TimelineProjection = {
  chatId: string;
  itemsById: Record<string, ChatTimelineItem>;
  order: string[];
  windowLimit: number;
};

export type ChatDetailProjection = {
  thread: ChatThreadProjection | null;
  queue: ChatQueueSummary | null;
  artifactIds: string[];
};

export type RepoWorktreeRuntimeEntity = RuntimeProjection & {
  id: string;
};

export type EntityVersions = Record<EntityKind, Record<string, number>>;

export type ChatIndexWindowRequest = {
  filter?: ChatIndexSnapshot['filter'];
  query?: string | null;
  facets?: Partial<ChatFacetRequest> | null;
  surfaceKind?: string | null;
  groupBy?: 'ticket_run' | null;
  parentGroupId?: string | null;
  limit?: number;
};

/** Shared companion window for backend ticket-run groups; must match route preload. */
export const CHAT_TICKET_RUN_GROUP_WINDOW_REQUEST = {
  facets: { categories: ['ticket_run'] as const },
  groupBy: 'ticket_run' as const,
  limit: 50
} satisfies ChatIndexWindowRequest;

export type ChatIndexWindowStatus = 'idle' | 'loading' | 'ready' | 'interrupted';

export type ChatIndexWindow = {
  key: string;
  request: Required<Pick<ChatIndexWindowRequest, 'filter'>> & {
    query: string | null;
    facets: ChatFacetRequest;
    surfaceKind: string | null;
    groupBy: 'ticket_run' | null;
    parentGroupId: string | null;
    limit: number;
  };
  rowIds: string[];
  groupIds: string[];
  counters: ChatIndexCounters;
  facetCounts: ChatFacetCounts;
  cursor: ProjectionCursor | null;
  window: PageWindow | null;
  status: ChatIndexWindowStatus;
  refreshing: boolean;
  lastLoadedAt: string | null;
  error: string | null;
};

export type RepoWorktreeIndexWindow = {
  key: string;
  kind: 'all' | 'repo' | 'worktree';
  limit: number;
  repoIds: string[];
  worktreeIds: string[];
  runtimeIds: string[];
  topologyWindow: PageWindow | null;
  runtimeWindow: PageWindow | null;
  lastLoadedAt: string;
};

export type ReadModelEntityState = {
  activeChatId: string | null;
  cursors: Record<string, ProjectionCursor>;
  chatIndexCursor: ProjectionCursor | null;
  chats: Record<string, ChatIndexRow>;
  chatOrder: string[];
  chatGroups: Record<string, ChatIndexGroup>;
  chatGroupOrder: string[];
  chatCounters: ChatIndexCounters;
  chatFacetCounts: ChatFacetCounts;
  chatWindows: Record<string, ChatIndexWindow>;
  chatDetails: Record<string, ChatDetailProjection>;
  timelines: Record<string, TimelineProjection>;
  chatTranscripts: Record<string, { cardsById: Record<string, ChatTranscriptCard>; order: string[] }>;
  chatProgress: Record<string, ChatRunProgress>;
  chatQueues: Record<string, ChatQueuedTurn[]>;
  surfaceArtifacts: Record<string, SurfaceArtifact[]>;
  readMarkers: Record<string, string>;
  artifacts: Record<string, ChatArtifactSummary>;
  repos: Record<string, RepoTopology>;
  repoOrder: string[];
  worktrees: Record<string, WorktreeTopology>;
  worktreeOrder: string[];
  repoWorktreeWindows: Record<string, RepoWorktreeIndexWindow>;
  runtime: Record<string, RepoWorktreeRuntimeEntity>;
  tickets: Record<string, TicketProjection>;
  ticketSummaries: Record<string, TicketSummary>;
  ticketOrderByOwner: Record<string, string[]>;
  ticketSiblings: Record<string, TicketQueueSibling[]>;
  runs: Record<string, unknown>;
  chatRuns: Record<string, ChatRunProgress>;
  chatRunOrderByOwner: Record<string, string[]>;
  repoDetails: Record<string, RepoWorktreeDetailSnapshot>;
  worktreeDetails: Record<string, RepoWorktreeDetailSnapshot>;
  agents: Record<string, unknown>;
  models: Record<string, unknown>;
  optimistic: Record<string, OptimisticMutation>;
  versions: EntityVersions;
  repairRequired: boolean;
};

export type ChatIndexView = {
  rows: ChatIndexRow[];
  groups: ChatIndexGroup[];
  counters: ChatIndexCounters;
  facetCounts: ChatFacetCounts;
  cursor: ProjectionCursor | null;
};

export type ChatDetailView = {
  thread: ChatThreadProjection | null;
  timeline: ChatTimelineItem[];
  queue: ChatQueueSummary | null;
  artifacts: ChatArtifactSummary[];
};

export const emptyChatCounters: ChatIndexCounters = {
  total: 0,
  waiting: 0,
  running: 0,
  unread: 0,
  archived: 0
};

export const emptyChatFacetCounts: ChatFacetCounts = {
  category: {},
  turnKind: {},
  originKind: {},
  transport: {},
  scopeKind: {},
  agentKind: {}
};

export const emptyChatFacetRequest: ChatFacetRequest = {
  categories: [],
  turnKinds: [],
  originKinds: [],
  transports: [],
  scopeKinds: [],
  scopeIds: [],
  agentKinds: []
};

export const LIVE_PROGRESS_EVENT_LIMIT = 80;

export function createInitialReadModelState(): ReadModelEntityState {
  return {
    activeChatId: null,
    cursors: {},
    chatIndexCursor: null,
    chats: {},
    chatOrder: [],
    chatGroups: {},
    chatGroupOrder: [],
    chatCounters: emptyChatCounters,
    chatFacetCounts: emptyChatFacetCounts,
    chatWindows: {},
    chatDetails: {},
    timelines: {},
    chatTranscripts: {},
    chatProgress: {},
    chatQueues: {},
    surfaceArtifacts: {},
    readMarkers: {},
    artifacts: {},
    repos: {},
    repoOrder: [],
    worktrees: {},
    worktreeOrder: [],
    repoWorktreeWindows: {},
    runtime: {},
    tickets: {},
    ticketSummaries: {},
    ticketOrderByOwner: {},
    ticketSiblings: {},
    runs: {},
    chatRuns: {},
    chatRunOrderByOwner: {},
    repoDetails: {},
    worktreeDetails: {},
    agents: {},
    models: {},
    optimistic: {},
    versions: {
      chat: {},
      chatGroup: {},
      timeline: {},
      repo: {},
      worktree: {},
      ticket: {},
      run: {},
      artifact: {},
      agent: {},
      model: {}
    },
    repairRequired: false
  };
}

export class ReadModelEntityStore implements Readable<ReadModelEntityState> {
  private readonly store = writable<ReadModelEntityState>(createInitialReadModelState());
  private state = createInitialReadModelState();

  subscribe = this.store.subscribe;

  snapshot(): ReadModelEntityState {
    return this.state;
  }

  reset(): void {
    this.commit(createInitialReadModelState());
  }

  setActiveChatId(chatId: string | null): void {
    if (this.state.activeChatId === chatId) return;
    this.commit({ ...this.state, activeChatId: chatId });
  }

  applyChatIndexSnapshot(snapshot: {
    cursor: ProjectionCursor;
    rows: ChatIndexRow[];
    groups: ChatIndexGroup[];
    counters: ChatIndexCounters;
    filter?: ChatIndexSnapshot['filter'];
    query?: string | null;
    facetRequest?: ChatFacetRequest;
    window?: PageWindow;
    facetCounts?: ChatFacetCounts;
    }, request: ChatIndexWindowRequest = {}, options: { append?: boolean } = {}): void {
    const rows = uniqueChatIndexRows(snapshot.rows);
    const next = cloneState(this.state);
    const windowRequest = normalizeChatIndexWindowRequest({
      filter: request.filter ?? snapshot.filter,
      query: request.query ?? snapshot.query,
      facets: request.facets ?? snapshot.facetRequest,
      surfaceKind: request.surfaceKind,
      groupBy: request.groupBy,
      parentGroupId: request.parentGroupId,
      limit: request.limit ?? snapshot.window?.limit
    });
    const windowKey = canonicalChatIndexWindowKey(windowRequest);
    const previousWindow = options.append ? next.chatWindows[windowKey] : null;
    for (const row of rows) next.chats[row.chatId] = row;
    for (const group of snapshot.groups) next.chatGroups[group.groupId] = group;
    if (isDefaultChatIndexWindow(windowRequest)) next.chatCounters = snapshot.counters;
    if (isDefaultChatIndexWindow(windowRequest)) next.chatFacetCounts = cloneChatFacetCounts(snapshot.facetCounts ?? emptyChatFacetCounts);
    next.chatIndexCursor = snapshot.cursor;
    next.chatWindows[windowKey] = {
      key: windowKey,
      request: windowRequest,
      rowIds: previousWindow
        ? uniqueStrings([...previousWindow.rowIds, ...rows.map((row) => row.chatId)])
        : rows.map((row) => row.chatId),
      groupIds: previousWindow
        ? uniqueStrings([...previousWindow.groupIds, ...snapshot.groups.map((group) => group.groupId)])
        : snapshot.groups.map((group) => group.groupId),
      counters: snapshot.counters,
      facetCounts: cloneChatFacetCounts(snapshot.facetCounts ?? emptyChatFacetCounts),
      cursor: snapshot.cursor,
      window: snapshot.window ?? null,
      status: 'ready',
      refreshing: false,
      lastLoadedAt: new Date().toISOString(),
      error: null
    };
    next.repairRequired = false;
    rememberCursor(next, 'chat.index', snapshot.cursor);
    rememberCursor(next, `chat.index.window:${windowKey}`, snapshot.cursor);
    for (const detail of Object.values(next.chatDetails)) {
      if (detail.thread) seedDetailBackedChatRow(next, detail.thread);
    }
    rebuildChatIndexEntityOrder(next);
    for (const row of rows) bump(next, 'chat', row.chatId);
    for (const group of snapshot.groups) bump(next, 'chatGroup', group.groupId);
    pruneChatIndexCache(next);
    this.commit(next);
  }

  /** @deprecated Tests and transitional fixtures only. Production list rows come from chat-index snapshots/patches. */
  replaceChatIndexRows(rows: ChatIndexRow[], cursor: ProjectionCursor, counters = countersFromRows(rows)): void {
    this.applyChatIndexSnapshot({ cursor, rows, groups: [], counters });
  }

  /** @deprecated Tests and transitional fixtures only. Production list rows come from chat-index snapshots/patches. */
  upsertChatIndexRows(rows: ChatIndexRow[]): void {
    if (!rows.length) return;
    const next = cloneState(this.state);
    for (const row of rows) {
      next.chats[row.chatId] = row;
      if (!next.chatOrder.includes(row.chatId)) next.chatOrder.push(row.chatId);
      bump(next, 'chat', row.chatId);
    }
    const orderedRows = next.chatOrder.map((id) => next.chats[id]).filter(Boolean);
    next.chatCounters = countersFromRows(orderedRows);
    const windowRequest = normalizeChatIndexWindowRequest({ filter: 'all', limit: 50 });
    const windowKey = canonicalChatIndexWindowKey(windowRequest);
    next.chatWindows[windowKey] = {
      key: windowKey,
      request: windowRequest,
      rowIds: orderedRows.map((row) => row.chatId),
      groupIds: [],
      counters: next.chatCounters,
      facetCounts: next.chatFacetCounts,
      cursor: next.chatIndexCursor,
      window: null,
      status: 'ready',
      refreshing: false,
      lastLoadedAt: new Date().toISOString(),
      error: null
    };
    this.commit(next);
  }

  applyChatIndexPatchEvent(event: ChatIndexPatchEvent): 'applied' | 'ignored' | 'repair_required' {
    if (isRepairEvent(event.envelope.eventType, event.envelope.operation)) {
      this.markRepairRequired(event.envelope.cursor);
      return 'repair_required';
    }
    if (!isNewer(this.state.cursors['chat.index'], event.envelope.cursor)) return 'ignored';
    if (Object.keys(this.state.chatWindows).length === 0) {
      this.markRepairRequired(event.envelope.cursor);
      return 'repair_required';
    }
    const next = cloneState(this.state);
    const incomingRows = new Map(event.patch.rows.map((row) => [row.chatId, row]));
    const orderedRowIds = new Set(event.patch.order ?? []);
    for (const row of event.patch.rows) {
      const known = Boolean(next.chats[row.chatId]);
      const orderedIntoCachedWindow = orderedRowIds.has(row.chatId) && Object.values(next.chatWindows).some((window) => chatIndexRowMatchesWindow(row, window.request));
      if (!known && !orderedIntoCachedWindow) continue;
      next.chats[row.chatId] = row;
      if (!next.chatOrder.includes(row.chatId)) next.chatOrder.push(row.chatId);
      bump(next, 'chat', row.chatId);
    }
    for (const id of event.patch.removedRowIds) {
      delete next.chats[id];
      next.chatOrder = next.chatOrder.filter((rowId) => rowId !== id);
      bump(next, 'chat', id);
    }
    for (const group of event.patch.groups) {
      next.chatGroups[group.groupId] = group;
      if (!next.chatGroupOrder.includes(group.groupId)) next.chatGroupOrder.push(group.groupId);
      bump(next, 'chatGroup', group.groupId);
    }
    for (const id of event.patch.removedGroupIds) {
      delete next.chatGroups[id];
      next.chatGroupOrder = next.chatGroupOrder.filter((groupId) => groupId !== id);
      bump(next, 'chatGroup', id);
    }
    if (event.patch.order) next.chatOrder = event.patch.order.filter((id) => Boolean(next.chats[id] ?? incomingRows.get(id)));
    if (event.patch.counters) next.chatCounters = event.patch.counters;
    if (event.patch.facetCounts) next.chatFacetCounts = cloneChatFacetCounts(event.patch.facetCounts);
    reconcileChatIndexWindowsAfterEntityPatch(next, event);
    next.chatIndexCursor = event.envelope.cursor;
    rememberCursor(next, 'chat.index', event.envelope.cursor);
    pruneChatIndexCache(next);
    this.commit(next);
    return 'applied';
  }

  applyChatDetailSnapshot(snapshot: ChatDetailSnapshot): void {
    const next = cloneState(this.state);
    seedDetailBackedChatRow(next, snapshot.thread);
    next.chatDetails[snapshot.thread.chatId] = {
      thread: snapshot.thread,
      queue: snapshot.queue,
      artifactIds: snapshot.artifacts.map((artifact) => artifact.artifactId)
    };
    next.timelines[snapshot.thread.chatId] = {
      chatId: snapshot.thread.chatId,
      itemsById: keyed(snapshot.timeline, (item) => item.itemId),
      order: orderChatTimelineItems(snapshot.timeline).map((item) => item.itemId),
      windowLimit: snapshot.timelineWindow.limit
    };
    next.chatQueues[snapshot.thread.chatId] = snapshot.queue.queuedTurnIds.map((id, index) => ({
      managedTurnId: id,
      position: index + 1,
      state: 'queued',
      prompt: '',
      promptPreview: id,
      attachments: [],
      model: null,
      reasoning: null,
      enqueuedAt: null,
      raw: { queued_turn_id: id }
    }));
    for (const artifact of snapshot.artifacts) {
      next.artifacts[artifact.artifactId] = artifact;
      bump(next, 'artifact', artifact.artifactId);
    }
    bump(next, 'chat', snapshot.thread.chatId);
    bump(next, 'timeline', snapshot.thread.chatId);
    rememberCursor(next, `chat.detail:${snapshot.thread.chatId}`, snapshot.cursor);
    this.commit(next);
  }

  applyChatDetailPatchEvent(event: ChatDetailPatchEvent): 'applied' | 'ignored' | 'repair_required' {
    const chatId = event.envelope.entityId;
    const cursorKey = `chat.detail:${chatId}`;
    if (isRepairEvent(event.envelope.eventType, event.envelope.operation)) {
      this.markRepairRequired(event.envelope.cursor);
      return 'repair_required';
    }
    if (!isNewer(this.state.cursors[cursorKey], event.envelope.cursor)) return 'ignored';
    const next = cloneState(this.state);
    const existingTimeline = cloneTimelineProjection(
      next.timelines[chatId] ?? {
        chatId,
        itemsById: {},
        order: [],
        windowLimit: 50
      }
    );
    for (const item of [...event.patch.appendedTimeline, ...event.patch.patchedTimeline]) {
      existingTimeline.itemsById[item.itemId] = item;
      if (!existingTimeline.order.includes(item.itemId)) existingTimeline.order.push(item.itemId);
    }
    for (const id of event.patch.removedTimelineIds) {
      delete existingTimeline.itemsById[id];
      existingTimeline.order = existingTimeline.order.filter((itemId) => itemId !== id);
    }
    existingTimeline.order = orderChatTimelineItems(existingTimeline.order.map((id) => existingTimeline.itemsById[id]).filter(Boolean)).map((item) => item.itemId);
    next.timelines[chatId] = existingTimeline;
    const detail = cloneChatDetailProjection(next.chatDetails[chatId] ?? { thread: null, queue: null, artifactIds: [] });
    if (event.patch.thread) {
      detail.thread = event.patch.thread;
    }
    if (event.patch.queue) detail.queue = event.patch.queue;
    if (event.patch.artifacts.length) {
      detail.artifactIds = event.patch.artifacts.map((artifact) => artifact.artifactId);
      for (const artifact of event.patch.artifacts) {
        next.artifacts[artifact.artifactId] = artifact;
        bump(next, 'artifact', artifact.artifactId);
      }
    }
    next.chatDetails[chatId] = detail;
    bump(next, 'timeline', chatId);
    rememberCursor(next, cursorKey, event.envelope.cursor);
    this.commit(next);
    return 'applied';
  }

  replaceChatTranscript(chatId: string, cards: ChatTranscriptCard[]): void {
    const orderedCards = orderChatTranscriptCards(cards);
    const next = cloneState(this.state);
    next.chatTranscripts[chatId] = {
      cardsById: keyed(orderedCards, chatTranscriptCardEntityId),
      order: orderedCards.map(chatTranscriptCardEntityId)
    };
    bump(next, 'timeline', chatId);
    this.commit(next);
  }

  upsertChatTranscriptCards(chatId: string, cards: ChatTranscriptCard[]): void {
    if (!cards.length) return;
    const next = cloneState(this.state);
    const transcript = cloneChatTranscript(next.chatTranscripts[chatId] ?? { cardsById: {}, order: [] });
    let changed = false;
    for (const card of cards) {
      const id = chatTranscriptCardEntityId(card);
      const previous = transcript.cardsById[id];
      if (previous === card) continue;
      transcript.cardsById[id] = card;
      if (previous) {
        const currentIndex = transcript.order.indexOf(id);
        if (currentIndex >= 0) transcript.order.splice(currentIndex, 1);
      }
      insertOrderedChatTranscriptId(transcript, id);
      changed = true;
    }
    if (!changed) return;
    next.chatTranscripts[chatId] = transcript;
    bump(next, 'timeline', chatId);
    this.commit(next);
  }

  removeOptimisticChatTranscriptCards(chatId: string): void {
    const transcript = this.state.chatTranscripts[chatId];
    if (!transcript || !transcript.order.some((id) => id.startsWith('optimistic:'))) return;
    const next = cloneState(this.state);
    const target = cloneChatTranscript(next.chatTranscripts[chatId]);
    for (const id of target.order) {
      if (id.startsWith('optimistic:')) delete target.cardsById[id];
    }
    target.order = target.order.filter((id) => !id.startsWith('optimistic:'));
    next.chatTranscripts[chatId] = target;
    bump(next, 'timeline', chatId);
    this.commit(next);
  }

  removeOptimisticChatTranscriptCard(chatId: string, cardId: string): void {
    const transcript = this.state.chatTranscripts[chatId];
    if (!transcript || !cardId.startsWith('optimistic:') || !transcript.cardsById[cardId]) return;
    const next = cloneState(this.state);
    const target = cloneChatTranscript(next.chatTranscripts[chatId]);
    delete target.cardsById[cardId];
    target.order = target.order.filter((id) => id !== cardId);
    next.chatTranscripts[chatId] = target;
    bump(next, 'timeline', chatId);
    this.commit(next);
  }

  setChatProgress(chatId: string, progress: ChatRunProgress | null): void {
    const next = cloneState(this.state);
    if (progress) next.chatProgress[chatId] = withBoundedChatProgressEvents(progress);
    else delete next.chatProgress[chatId];
    bump(next, 'run', chatId);
    this.commit(next);
  }

  setChatQueue(chatId: string, queuedTurns: ChatQueuedTurn[]): void {
    const next = cloneState(this.state);
    next.chatQueues[chatId] = [...queuedTurns];
    bump(next, 'run', chatId);
    this.commit(next);
  }

  setSurfaceArtifacts(chatId: string, artifacts: SurfaceArtifact[]): void {
    const next = cloneState(this.state);
    next.surfaceArtifacts[chatId] = [...artifacts];
    for (const artifact of artifacts) bump(next, 'artifact', artifact.id);
    this.commit(next);
  }

  setReadMarkers(markers: Record<string, string>): void {
    const next = cloneState(this.state);
    next.readMarkers = { ...markers };
    for (const chatId of Object.keys(markers)) bump(next, 'chat', chatId);
    this.commit(next);
  }

  optimisticReadMarkers(markers: Record<string, string>, reconciliationId: string): void {
    const next = cloneState(this.state);
    next.optimistic[reconciliationId] = {
      reconciliationId,
      kind: 'read-marker',
      entityKind: 'chat',
      entityId: '*',
      status: 'pending',
      createdAt: new Date().toISOString(),
      previousValue: next.readMarkers
    };
    next.readMarkers = { ...markers };
    for (const chatId of Object.keys(markers)) bump(next, 'chat', chatId);
    this.commit(next);
  }

  applyRepoWorktreeTopologySnapshot(snapshot: RepoWorktreeTopologySnapshot): void {
    const next = cloneState(this.state);
    next.repos = keyed(snapshot.repos, (repo) => repo.repoId);
    next.repoOrder = snapshot.repos.map((repo) => repo.repoId);
    next.worktrees = keyed(snapshot.worktrees, (worktree) => worktree.worktreeId);
    next.worktreeOrder = snapshot.worktrees.map((worktree) => worktree.worktreeId);
    const windowKey = repoWorktreeWindowKey('all', snapshot.window?.limit ?? snapshot.repos.length + snapshot.worktrees.length);
    const previousWindow = next.repoWorktreeWindows[windowKey] ?? emptyRepoWorktreeIndexWindow(windowKey, snapshot.window?.limit ?? 0);
    next.repoWorktreeWindows[windowKey] = {
      ...previousWindow,
      repoIds: next.repoOrder,
      worktreeIds: next.worktreeOrder,
      topologyWindow: snapshot.window ?? null,
      lastLoadedAt: new Date().toISOString()
    };
    for (const repo of snapshot.repos) bump(next, 'repo', repo.repoId);
    for (const worktree of snapshot.worktrees) bump(next, 'worktree', worktree.worktreeId);
    rememberCursor(next, 'repo_worktree.topology', snapshot.cursor);
    this.commit(next);
  }

  applyRepoWorktreeRuntimeSnapshot(snapshot: RepoWorktreeRuntimeSnapshot): void {
    const next = cloneState(this.state);
    const runtimeIds: string[] = [];
    for (const runtime of snapshot.runtime) {
      const id = `${runtime.entityKind}:${runtime.entityId}`;
      next.runtime[id] = { ...runtime, id };
      runtimeIds.push(id);
      bump(next, runtime.entityKind, runtime.entityId);
    }
    const windowKey = repoWorktreeWindowKey('all', snapshot.window?.limit ?? snapshot.runtime.length);
    const previousWindow = next.repoWorktreeWindows[windowKey] ?? emptyRepoWorktreeIndexWindow(windowKey, snapshot.window?.limit ?? 0);
    next.repoWorktreeWindows[windowKey] = {
      ...previousWindow,
      runtimeIds,
      runtimeWindow: snapshot.window ?? null,
      lastLoadedAt: new Date().toISOString()
    };
    rememberCursor(next, 'repo_worktree.runtime', snapshot.cursor);
    this.commit(next);
  }

  applyRepoDetailSnapshot(snapshot: RepoWorktreeDetailSnapshot): void {
    const next = cloneState(this.state);
    next.repoDetails[snapshot.ownerId] = snapshot;
    bump(next, 'repo', snapshot.ownerId);
    rememberCursor(next, `repo.detail:${snapshot.ownerId}`, snapshot.cursor);
    this.commit(next);
  }

  applyWorktreeDetailSnapshot(snapshot: RepoWorktreeDetailSnapshot): void {
    const next = cloneState(this.state);
    next.worktreeDetails[snapshot.ownerId] = snapshot;
    bump(next, 'worktree', snapshot.ownerId);
    rememberCursor(next, `worktree.detail:${snapshot.ownerId}`, snapshot.cursor);
    this.commit(next);
  }

  applyRepoWorktreePatchEvent(event: RepoWorktreePatchEvent): 'applied' | 'ignored' | 'repair_required' {
    const cursorKey = event.envelope.eventType.includes('runtime') ? 'repo_worktree.runtime' : 'repo_worktree.topology';
    if (isRepairEvent(event.envelope.eventType, event.envelope.operation)) {
      this.markRepairRequired(event.envelope.cursor);
      return 'repair_required';
    }
    if (!isNewer(this.state.cursors[cursorKey], event.envelope.cursor)) return 'ignored';
    const next = cloneState(this.state);
    for (const repo of event.patch.topologyRepos) {
      next.repos[repo.repoId] = repo;
      if (!next.repoOrder.includes(repo.repoId)) next.repoOrder.push(repo.repoId);
      bump(next, 'repo', repo.repoId);
    }
    for (const worktree of event.patch.topologyWorktrees) {
      next.worktrees[worktree.worktreeId] = worktree;
      if (!next.worktreeOrder.includes(worktree.worktreeId)) next.worktreeOrder.push(worktree.worktreeId);
      bump(next, 'worktree', worktree.worktreeId);
    }
    for (const runtime of event.patch.runtime) {
      const id = `${runtime.entityKind}:${runtime.entityId}`;
      next.runtime[id] = { ...runtime, id };
      bump(next, runtime.entityKind, runtime.entityId);
    }
    for (const repoId of event.patch.removedRepoIds) {
      delete next.repos[repoId];
      next.repoOrder = next.repoOrder.filter((id) => id !== repoId);
      bump(next, 'repo', repoId);
    }
    for (const worktreeId of event.patch.removedWorktreeIds) {
      delete next.worktrees[worktreeId];
      next.worktreeOrder = next.worktreeOrder.filter((id) => id !== worktreeId);
      bump(next, 'worktree', worktreeId);
    }
    rememberCursor(next, cursorKey, event.envelope.cursor);
    this.commit(next);
    return 'applied';
  }

  applyTicketDetailSnapshot(snapshot: TicketDetailSnapshot): void {
    const next = cloneState(this.state);
    next.tickets[snapshot.ticket.ticketId] = snapshot.ticket;
    next.ticketSiblings[snapshot.ticket.ticketId] = snapshot.siblings;
    if (snapshot.linkedRun) next.runs[snapshot.linkedRun.runId] = snapshot.linkedRun;
    for (const artifact of snapshot.artifacts) {
      next.artifacts[artifact.artifactId] = artifact;
      bump(next, 'artifact', artifact.artifactId);
    }
    bump(next, 'ticket', snapshot.ticket.ticketId);
    rememberCursor(next, `ticket.detail:${snapshot.ticket.ticketId}`, snapshot.cursor);
    this.commit(next);
  }

  replaceScopedTicketSummaries(ownerKey: string, tickets: TicketSummary[]): void {
    const next = cloneState(this.state);
    next.ticketOrderByOwner[ownerKey] = tickets.map((ticket) => ticket.id);
    for (const ticket of tickets) {
      next.ticketSummaries[ticket.id] = ticket;
      bump(next, 'ticket', ticket.id);
    }
    this.commit(next);
  }

  replaceScopedRuns(ownerKey: string, runs: ChatRunProgress[]): void {
    const next = cloneState(this.state);
    next.chatRunOrderByOwner[ownerKey] = runs.map((run) => run.id);
    for (const run of runs) {
      next.chatRuns[run.id] = run;
      bump(next, 'run', run.id);
    }
    this.commit(next);
  }

  applyTicketDetailPatchEvent(event: TicketDetailPatchEvent): 'applied' | 'ignored' | 'repair_required' {
    const cursorKey = `ticket.detail:${event.envelope.entityId}`;
    if (isRepairEvent(event.envelope.eventType, event.envelope.operation)) {
      this.markRepairRequired(event.envelope.cursor);
      return 'repair_required';
    }
    if (!isNewer(this.state.cursors[cursorKey], event.envelope.cursor)) return 'ignored';
    const next = cloneState(this.state);
    if (event.patch.ticket) {
      next.tickets[event.patch.ticket.ticketId] = event.patch.ticket;
      bump(next, 'ticket', event.patch.ticket.ticketId);
    }
    next.ticketSiblings[event.envelope.entityId] = event.patch.siblings;
    if (event.patch.linkedRun) {
      next.runs[event.patch.linkedRun.runId] = event.patch.linkedRun;
      bump(next, 'run', event.patch.linkedRun.runId);
    }
    for (const artifact of event.patch.artifacts) {
      next.artifacts[artifact.artifactId] = artifact;
      bump(next, 'artifact', artifact.artifactId);
    }
    rememberCursor(next, cursorKey, event.envelope.cursor);
    this.commit(next);
    return 'applied';
  }

  optimisticSend(chatId: string, item: ChatTimelineItem, reconciliationId: string): void {
    const next = cloneState(this.state);
    const timeline = cloneTimelineProjection(next.timelines[chatId] ?? { chatId, itemsById: {}, order: [], windowLimit: 50 });
    timeline.itemsById[item.itemId] = item;
    if (!timeline.order.includes(item.itemId)) timeline.order.push(item.itemId);
    next.timelines[chatId] = timeline;
    next.optimistic[reconciliationId] = {
      reconciliationId,
      kind: 'send',
      entityKind: 'timeline',
      entityId: chatId,
      status: 'pending',
      createdAt: new Date().toISOString(),
      previousValue: { itemId: item.itemId }
    };
    bump(next, 'timeline', chatId);
    this.commit(next);
  }

  reconcileOptimisticTimelineItem(chatId: string, reconciliationId: string, backendItem: ChatTimelineItem): void {
    const next = cloneState(this.state);
    const mutation = next.optimistic[reconciliationId];
    const timeline = next.timelines[chatId] ? cloneTimelineProjection(next.timelines[chatId]) : undefined;
    const optimisticItemId = (mutation?.previousValue as { itemId?: string } | undefined)?.itemId;
    if (timeline && optimisticItemId) {
      delete timeline.itemsById[optimisticItemId];
      timeline.itemsById[backendItem.itemId] = backendItem;
      timeline.order = timeline.order.map((id) => (id === optimisticItemId ? backendItem.itemId : id));
      if (!timeline.order.includes(backendItem.itemId)) timeline.order.push(backendItem.itemId);
      next.timelines[chatId] = timeline;
    }
    if (mutation) next.optimistic[reconciliationId] = { ...mutation, status: 'reconciled' };
    bump(next, 'timeline', chatId);
    this.commit(next);
  }

  failOptimisticMutation(reconciliationId: string): void {
    const mutation = this.state.optimistic[reconciliationId];
    if (!mutation) return;
    if (mutation.kind === 'send' && mutation.entityKind === 'timeline') {
      const chatId = mutation.entityId;
      const itemId = (mutation.previousValue as { itemId?: string } | undefined)?.itemId;
      const next = cloneState(this.state);
      const timeline = next.timelines[chatId] ? cloneTimelineProjection(next.timelines[chatId]) : undefined;
      if (timeline && itemId) {
        delete timeline.itemsById[itemId];
        timeline.order = timeline.order.filter((id) => id !== itemId);
        next.timelines[chatId] = timeline;
      }
      next.optimistic[reconciliationId] = { ...mutation, status: 'failed' };
      bump(next, 'timeline', chatId);
      this.commit(next);
      return;
    }
    const next = cloneState(this.state);
    next.optimistic[reconciliationId] = { ...mutation, status: 'failed' };
    this.commit(next);
  }

  optimisticRetireChat(chatId: string, reconciliationId: string): void {
    const previous = this.state.chats[chatId];
    if (!previous) return;
    const next = cloneState(this.state);
    next.chats[chatId] = { ...previous, status: 'archived' };
    next.optimistic[reconciliationId] = {
      reconciliationId,
      kind: 'retire',
      entityKind: 'chat',
      entityId: chatId,
      status: 'pending',
      createdAt: new Date().toISOString(),
      previousValue: previous
    };
    bump(next, 'chat', chatId);
    this.commit(next);
  }

  optimisticRepoPin(repoId: string, pinned: boolean, reconciliationId: string): void {
    const previous = this.state.repos[repoId];
    if (!previous) return;
    const next = cloneState(this.state);
    next.repos[repoId] = { ...previous, isPinned: pinned };
    next.optimistic[reconciliationId] = {
      reconciliationId,
      kind: 'repo-pin',
      entityKind: 'repo',
      entityId: repoId,
      status: 'pending',
      createdAt: new Date().toISOString(),
      previousValue: previous
    };
    bump(next, 'repo', repoId);
    this.commit(next);
  }

  reconcileOptimisticMutation(reconciliationId: string): void {
    const mutation = this.state.optimistic[reconciliationId];
    if (!mutation) return;
    const next = cloneState(this.state);
    next.optimistic[reconciliationId] = { ...mutation, status: 'reconciled' };
    this.commit(next);
  }

  revertOptimisticMutation(reconciliationId: string): void {
    const mutation = this.state.optimistic[reconciliationId];
    if (!mutation) return;
    const next = cloneState(this.state);
    if (mutation.kind === 'retire' && mutation.entityKind === 'chat' && mutation.previousValue) {
      next.chats[mutation.entityId] = mutation.previousValue as ChatIndexRow;
      bump(next, 'chat', mutation.entityId);
    }
    if (mutation.kind === 'repo-pin' && mutation.entityKind === 'repo' && mutation.previousValue) {
      next.repos[mutation.entityId] = mutation.previousValue as RepoTopology;
      bump(next, 'repo', mutation.entityId);
    }
    if (mutation.kind === 'send' && mutation.entityKind === 'timeline') {
      const itemId = (mutation.previousValue as { itemId?: string } | undefined)?.itemId;
      const timeline = next.timelines[mutation.entityId] ? cloneTimelineProjection(next.timelines[mutation.entityId]) : undefined;
      if (timeline && itemId) {
        delete timeline.itemsById[itemId];
        timeline.order = timeline.order.filter((id) => id !== itemId);
        next.timelines[mutation.entityId] = timeline;
        bump(next, 'timeline', mutation.entityId);
      }
    }
    if (mutation.kind === 'read-marker' && mutation.previousValue) {
      next.readMarkers = mutation.previousValue as Record<string, string>;
      for (const chatId of Object.keys(next.readMarkers)) bump(next, 'chat', chatId);
    }
    next.optimistic[reconciliationId] = { ...mutation, status: 'reverted' };
    this.commit(next);
  }

  private markRepairRequired(cursor: ProjectionCursor): void {
    const next = cloneState(this.state);
    next.repairRequired = true;
    rememberCursor(next, 'repair.required', cursor);
    this.commit(next);
  }

  private commit(next: ReadModelEntityState): void {
    this.state = next;
    this.store.set(next);
  }
}

export const readModelEntityStore = new ReadModelEntityStore();

export function selectChatIndexView(state: ReadModelEntityState): ChatIndexView {
  return {
    rows: state.chatOrder.map((id) => state.chats[id]).filter(Boolean),
    groups: state.chatGroupOrder.map((id) => state.chatGroups[id]).filter(Boolean),
    counters: state.chatCounters,
    facetCounts: state.chatFacetCounts,
    cursor: state.chatIndexCursor
  };
}

export function selectChatIndexWindowView(
  state: ReadModelEntityState,
  request: ChatIndexWindowRequest = {}
): ChatIndexView & { window: ChatIndexWindow | null } {
  const key = canonicalChatIndexWindowKey(request);
  const window = state.chatWindows[key] ?? null;
  if (!window) return { rows: [], groups: [], counters: emptyChatCounters, facetCounts: emptyChatFacetCounts, cursor: null, window: null };
  return {
    rows: window.rowIds.map((id) => state.chats[id]).filter(Boolean),
    groups: window.groupIds.map((id) => state.chatGroups[id]).filter(Boolean),
    counters: window.counters,
    facetCounts: window.facetCounts,
    cursor: window.cursor,
    window
  };
}

export function selectChatDetailView(state: ReadModelEntityState, chatId: string | null): ChatDetailView {
  if (!chatId) return { thread: null, timeline: [], queue: null, artifacts: [] };
  const detail = state.chatDetails[chatId] ?? { thread: null, queue: null, artifactIds: [] };
  const timeline = state.timelines[chatId];
  return {
    thread: detail.thread,
    timeline: timeline ? timeline.order.map((id) => timeline.itemsById[id]).filter(Boolean) : [],
    queue: detail.queue,
    artifacts: detail.artifactIds.map((id) => state.artifacts[id]).filter(Boolean)
  };
}

export function selectorFingerprint(state: ReadModelEntityState, kind: EntityKind, ids: string[]): string {
  return ids.map((id) => `${id}:${state.versions[kind][id] ?? 0}`).join('|');
}

function cloneState(state: ReadModelEntityState): ReadModelEntityState {
  return {
    ...state,
    cursors: { ...state.cursors },
    chats: { ...state.chats },
    chatOrder: [...state.chatOrder],
    chatGroups: { ...state.chatGroups },
    chatGroupOrder: [...state.chatGroupOrder],
    chatCounters: { ...state.chatCounters },
    chatFacetCounts: cloneChatFacetCounts(state.chatFacetCounts),
    chatWindows: Object.fromEntries(Object.entries(state.chatWindows).map(([key, window]) => [key, cloneChatIndexWindow(window)])),
    chatDetails: { ...state.chatDetails },
    timelines: { ...state.timelines },
    chatTranscripts: { ...state.chatTranscripts },
    chatProgress: { ...state.chatProgress },
    chatQueues: { ...state.chatQueues },
    surfaceArtifacts: { ...state.surfaceArtifacts },
    readMarkers: { ...state.readMarkers },
    artifacts: { ...state.artifacts },
    repos: { ...state.repos },
    repoOrder: [...state.repoOrder],
    worktrees: { ...state.worktrees },
    worktreeOrder: [...state.worktreeOrder],
    repoWorktreeWindows: Object.fromEntries(Object.entries(state.repoWorktreeWindows).map(([key, window]) => [key, cloneRepoWorktreeIndexWindow(window)])),
    runtime: { ...state.runtime },
    tickets: { ...state.tickets },
    ticketSummaries: { ...state.ticketSummaries },
    ticketOrderByOwner: { ...state.ticketOrderByOwner },
    ticketSiblings: { ...state.ticketSiblings },
    runs: { ...state.runs },
    chatRuns: { ...state.chatRuns },
    chatRunOrderByOwner: { ...state.chatRunOrderByOwner },
    repoDetails: { ...state.repoDetails },
    worktreeDetails: { ...state.worktreeDetails },
    agents: { ...state.agents },
    models: { ...state.models },
    optimistic: { ...state.optimistic },
    versions: {
      chat: { ...state.versions.chat },
      chatGroup: { ...state.versions.chatGroup },
      timeline: { ...state.versions.timeline },
      repo: { ...state.versions.repo },
      worktree: { ...state.versions.worktree },
      ticket: { ...state.versions.ticket },
      run: { ...state.versions.run },
      artifact: { ...state.versions.artifact },
      agent: { ...state.versions.agent },
      model: { ...state.versions.model }
    }
  };
}

function cloneChatDetailProjection(detail: ChatDetailProjection): ChatDetailProjection {
  return {
    thread: detail.thread,
    queue: detail.queue,
    artifactIds: [...detail.artifactIds]
  };
}

export function repoWorktreeWindowKey(kind: 'all' | 'repo' | 'worktree' = 'all', limit = 200): string {
  return `${kind}:limit=${limit}`;
}

function emptyRepoWorktreeIndexWindow(key: string, limit: number): RepoWorktreeIndexWindow {
  return {
    key,
    kind: 'all',
    limit,
    repoIds: [],
    worktreeIds: [],
    runtimeIds: [],
    topologyWindow: null,
    runtimeWindow: null,
    lastLoadedAt: new Date().toISOString()
  };
}

function cloneRepoWorktreeIndexWindow(window: RepoWorktreeIndexWindow): RepoWorktreeIndexWindow {
  return {
    ...window,
    repoIds: [...window.repoIds],
    worktreeIds: [...window.worktreeIds],
    runtimeIds: [...window.runtimeIds],
    topologyWindow: window.topologyWindow ? { ...window.topologyWindow } : null,
    runtimeWindow: window.runtimeWindow ? { ...window.runtimeWindow } : null
  };
}

function cloneTimelineProjection(timeline: TimelineProjection): TimelineProjection {
  return {
    chatId: timeline.chatId,
    itemsById: { ...timeline.itemsById },
    order: [...timeline.order],
    windowLimit: timeline.windowLimit
  };
}

function genericTimelineSortKey(item: ChatTimelineItem): string {
  return item.orderKey || item.createdAt || item.itemId;
}

function timelineTurnId(item: ChatTimelineItem): string | null {
  const id = item.managedTurnId;
  if (id == null) return null;
  const trimmed = String(id).trim();
  return trimmed ? trimmed : null;
}

function timelineItemPhase(item: ChatTimelineItem): number {
  if (typeof item.sectionOrder === 'number') return item.sectionOrder;
  if (item.section === 'user_message' || item.kind === 'user_message' || item.role === 'user') return 10;
  if (item.section === 'assistant_message' || item.kind === 'assistant_message' || item.role === 'assistant') return 30;
  if (item.section === 'terminal_metadata') return 40;
  if (item.section === 'thread_metadata') return 50;
  return 20;
}

function timelineItemIsUserAnchor(item: ChatTimelineItem): boolean {
  return item.section === 'user_message' || item.kind === 'user_message' || item.role === 'user';
}

function buildTimelineTurnAnchors(items: ChatTimelineItem[]): Map<string, string> {
  const turnFallbacks: Record<string, string> = {};
  const turnAnchors: Record<string, string> = {};
  for (const item of items) {
    const turnId = timelineTurnId(item);
    if (!turnId) continue;
    const g = genericTimelineSortKey(item);
    const prevFb = turnFallbacks[turnId];
    if (prevFb === undefined || g < prevFb) turnFallbacks[turnId] = g;
    if (timelineItemIsUserAnchor(item)) {
      const prevAnchor = turnAnchors[turnId];
      if (prevAnchor === undefined || g < prevAnchor) turnAnchors[turnId] = g;
    }
  }
  for (const turnId of Object.keys(turnFallbacks)) {
    if (turnAnchors[turnId] === undefined) turnAnchors[turnId] = turnFallbacks[turnId]!;
  }
  return new Map(Object.entries(turnAnchors));
}

type TimelineSortTuple = readonly [string, number, string, string];

function timelineRowSortKey(item: ChatTimelineItem, anchors: Map<string, string>): TimelineSortTuple {
  const genericKey = genericTimelineSortKey(item);
  const turnId = timelineTurnId(item);
  const phase = timelineItemPhase(item);
  if (!turnId) return [genericKey, phase, genericKey, item.itemId];
  const anchor = anchors.get(turnId) ?? genericKey;
  return [anchor, phase, genericKey, item.itemId];
}

function compareTimelineSortTuples(a: TimelineSortTuple, b: TimelineSortTuple): number {
  if (a[0] !== b[0]) return a[0] < b[0] ? -1 : 1;
  if (a[1] !== b[1]) return a[1] - b[1];
  if (a[2] !== b[2]) return a[2] < b[2] ? -1 : 1;
  if (a[3] !== b[3]) return a[3] < b[3] ? -1 : 1;
  return 0;
}

function orderChatTimelineItems(items: ChatTimelineItem[]): ChatTimelineItem[] {
  const anchors = buildTimelineTurnAnchors(items);
  return [...items].sort((a, b) =>
    compareTimelineSortTuples(timelineRowSortKey(a, anchors), timelineRowSortKey(b, anchors))
  );
}

function cloneChatTranscript(transcript: { cardsById: Record<string, ChatTranscriptCard>; order: string[] }): { cardsById: Record<string, ChatTranscriptCard>; order: string[] } {
  return {
    cardsById: { ...transcript.cardsById },
    order: [...transcript.order]
  };
}

function cloneChatIndexWindow(window: ChatIndexWindow): ChatIndexWindow {
  return {
    ...window,
    request: { ...window.request },
    rowIds: [...window.rowIds],
    groupIds: [...window.groupIds],
    counters: { ...window.counters },
    facetCounts: cloneChatFacetCounts(window.facetCounts),
    window: window.window ? { ...window.window } : null
  };
}

function rebuildChatIndexEntityOrder(state: ReadModelEntityState): void {
  const chatIds: string[] = [];
  const groupIds: string[] = [];
  const pushUnique = (target: string[], id: string): void => {
    if (!target.includes(id)) target.push(id);
  };
  for (const window of Object.values(state.chatWindows)) {
    for (const chatId of window.rowIds) {
      if (state.chats[chatId]) pushUnique(chatIds, chatId);
    }
    for (const groupId of window.groupIds) {
      if (state.chatGroups[groupId]) pushUnique(groupIds, groupId);
    }
  }
  for (const chatId of state.chatOrder) {
    if (state.chats[chatId]) pushUnique(chatIds, chatId);
  }
  for (const groupId of state.chatGroupOrder) {
    if (state.chatGroups[groupId]) pushUnique(groupIds, groupId);
  }
  state.chatOrder = chatIds;
  state.chatGroupOrder = groupIds;
}

function reconcileChatIndexWindowsAfterEntityPatch(next: ReadModelEntityState, event: ChatIndexPatchEvent): void {
  const incomingRows = new Map(event.patch.rows.map((row) => [row.chatId, row]));
  for (const [key, window] of Object.entries(next.chatWindows)) {
    const defaultWindow = isDefaultChatIndexWindow(window.request);
    const existingIds = new Set(window.rowIds);
    window.rowIds = window.rowIds.filter((rowId) => {
      const row = next.chats[rowId];
      return Boolean(row && chatIndexRowMatchesWindow(row, window.request));
    });
    window.groupIds = window.groupIds.filter((groupId) => Boolean(next.chatGroups[groupId]));
    if (defaultWindow) {
      if (event.patch.order) {
        const loadedLimit = Math.max(window.request.limit, window.rowIds.length);
        const orderedIds = new Set(event.patch.order);
        window.rowIds = event.patch.order
          .filter((rowId) => Boolean(next.chats[rowId] ?? incomingRows.get(rowId)))
          .concat(window.rowIds.filter((rowId) => !orderedIds.has(rowId)))
          .slice(0, loadedLimit);
      }
      if (event.patch.counters) window.counters = event.patch.counters;
      if (event.patch.facetCounts) window.facetCounts = cloneChatFacetCounts(event.patch.facetCounts);
      window.cursor = event.envelope.cursor;
      const affected = event.patch.removedRowIds.some((rowId) => existingIds.has(rowId)) || event.patch.rows.some((row) => existingIds.has(row.chatId) || (event.patch.order ? event.patch.order.includes(row.chatId) : chatIndexRowMatchesWindow(row, window.request)));
      const needsBackfill = affected && !event.patch.order && (window.window?.totalEstimate ?? window.rowIds.length) > window.rowIds.length;
      window.status = needsBackfill ? 'interrupted' : 'ready';
      window.refreshing = needsBackfill;
      window.lastLoadedAt = new Date().toISOString();
      window.error = null;
      rememberCursor(next, `chat.index.window:${key}`, event.envelope.cursor);
      continue;
    }
    const affected = event.patch.removedRowIds.some((rowId) => existingIds.has(rowId)) || event.patch.rows.some((row) => existingIds.has(row.chatId) || chatIndexRowMatchesWindow(row, window.request));
    if (affected) {
      window.status = 'interrupted';
      window.refreshing = true;
      window.error = null;
    }
  }
}

function pruneChatIndexCache(state: ReadModelEntityState): void {
  const retainedChatIds = retainedChatIndexRowIds(state);
  for (const chatId of Object.keys(state.chats)) {
    if (retainedChatIds.has(chatId)) continue;
    delete state.chats[chatId];
    delete state.versions.chat[chatId];
  }
  state.chatOrder = state.chatOrder.filter((chatId) => retainedChatIds.has(chatId) && Boolean(state.chats[chatId]));

  const retainedGroupIds = new Set<string>();
  for (const window of Object.values(state.chatWindows)) {
    for (const groupId of window.groupIds) retainedGroupIds.add(groupId);
  }
  for (const groupId of Object.keys(state.chatGroups)) {
    if (retainedGroupIds.has(groupId)) continue;
    delete state.chatGroups[groupId];
    delete state.versions.chatGroup[groupId];
  }
  state.chatGroupOrder = state.chatGroupOrder.filter((groupId) => retainedGroupIds.has(groupId) && Boolean(state.chatGroups[groupId]));
}

function retainedChatIndexRowIds(state: ReadModelEntityState): Set<string> {
  const retained = new Set<string>();
  for (const window of Object.values(state.chatWindows)) {
    for (const chatId of window.rowIds) retained.add(chatId);
  }
  for (const [chatId, detail] of Object.entries(state.chatDetails)) {
    if (detail.thread) retained.add(chatId);
  }
  for (const chatId of Object.keys(state.chatTranscripts)) retained.add(chatId);
  for (const chatId of Object.keys(state.chatProgress)) retained.add(chatId);
  for (const chatId of Object.keys(state.chatQueues)) retained.add(chatId);
  for (const chatId of Object.keys(state.surfaceArtifacts)) {
    if (chatId !== '__global__') retained.add(chatId);
  }
  for (const mutation of Object.values(state.optimistic)) {
    if (mutation.entityKind === 'chat' && mutation.entityId !== '*') retained.add(mutation.entityId);
  }
  return retained;
}

function chatIndexRowMatchesWindow(row: ChatIndexRow, request: ChatIndexWindow['request']): boolean {
  if (request.parentGroupId && row.groupId !== request.parentGroupId) return false;
  if (request.surfaceKind && row.surface !== request.surfaceKind) return false;
  if (!chatIndexRowMatchesFacetRequest(row, request.facets)) return false;
  if (!chatIndexRowMatchesFilter(row, request.filter)) return false;
  if (!request.query) return true;
  const query = request.query.toLocaleLowerCase();
  return [row.title, row.repoId, row.worktreeId, row.ticketId, row.runId, row.agent, row.agentProfile, row.model]
    .some((value) => (value ?? '').toLocaleLowerCase().includes(query));
}

function chatIndexRowMatchesFilter(row: ChatIndexRow, filter: ChatIndexSnapshot['filter']): boolean {
  if (filter === 'waiting') return row.status === 'waiting';
  if (filter === 'active') return row.status === 'running';
  if (filter === 'unread') return row.unreadCount > 0;
  if (filter === 'archived') return row.status === 'archived';
  if (filter === 'ticket_runs') return row.facets?.category === 'ticket_run';
  if (filter === 'external') return row.surface !== 'pma';
  return true;
}

function chatIndexRowMatchesFacetRequest(row: ChatIndexRow, request: ChatFacetRequest): boolean {
  if (chatFacetRequestIsEmpty(request)) return true;
  const facets = row.facets;
  if (!facets) return false;
  return (
    matchesAny(request.categories, facets.category) &&
    matchesAny(request.turnKinds, facets.turnKinds) &&
    matchesAny(request.originKinds, facets.originKinds) &&
    matchesAny(request.transports, facets.transports) &&
    matchesAny(request.scopeKinds, facets.scopeKind ?? null) &&
    matchesAny(request.scopeIds, facets.scopeId ?? null) &&
    matchesAny(request.agentKinds, facets.agentKind ?? null)
  );
}

function matchesAny<T extends string>(requested: T[], actual: T | T[] | string | null | undefined): boolean {
  if (!requested.length) return true;
  if (Array.isArray(actual)) return actual.some((value) => requested.includes(value as T));
  return typeof actual === 'string' && requested.includes(actual as T);
}

export function canonicalChatIndexWindowKey(request: ChatIndexWindowRequest = {}): string {
  const normalized = normalizeChatIndexWindowRequest(request);
  return JSON.stringify({
    filter: normalized.filter,
    query: normalized.query,
    facets: normalized.facets,
    surfaceKind: normalized.surfaceKind,
    groupBy: normalized.groupBy,
    parentGroupId: normalized.parentGroupId,
    limit: normalized.limit
  });
}

function normalizeChatIndexWindowRequest(request: ChatIndexWindowRequest = {}): Required<Pick<ChatIndexWindowRequest, 'filter'>> & {
  query: string | null;
  facets: ChatFacetRequest;
  surfaceKind: string | null;
  groupBy: 'ticket_run' | null;
  parentGroupId: string | null;
  limit: number;
} {
  const query = request.query?.trim() || null;
  const surfaceKind = request.surfaceKind?.trim() || null;
  const parentGroupId = request.parentGroupId?.trim() || null;
  return {
    filter: request.filter ?? 'all',
    query,
    facets: normalizeChatFacetRequest(request.facets),
    surfaceKind,
    groupBy: request.groupBy === 'ticket_run' ? 'ticket_run' : null,
    parentGroupId,
    limit: request.limit ?? 50
  };
}

function isDefaultChatIndexWindow(request: ReturnType<typeof normalizeChatIndexWindowRequest>): boolean {
  return request.filter === 'all' && request.query === null && chatFacetRequestIsEmpty(request.facets) && request.surfaceKind === null && request.groupBy === null && request.parentGroupId === null;
}

function cloneChatFacetCounts(counts: ChatFacetCounts): ChatFacetCounts {
  return {
    category: { ...counts.category },
    turnKind: { ...counts.turnKind },
    originKind: { ...counts.originKind },
    transport: { ...counts.transport },
    scopeKind: { ...counts.scopeKind },
    agentKind: { ...counts.agentKind }
  };
}

function keyed<T>(items: T[], key: (item: T) => string): Record<string, T> {
  const record: Record<string, T> = {};
  for (const item of items) record[key(item)] = item;
  return record;
}

function uniqueChatIndexRows(rows: ChatIndexRow[]): ChatIndexRow[] {
  const order: string[] = [];
  const byChatId = new Map<string, ChatIndexRow>();
  for (const row of rows) {
    if (!byChatId.has(row.chatId)) order.push(row.chatId);
    byChatId.set(row.chatId, row);
  }
  return order.map((chatId) => byChatId.get(chatId)).filter((row): row is ChatIndexRow => Boolean(row));
}

function uniqueStrings(values: string[]): string[] {
  const seen = new Set<string>();
  const unique: string[] = [];
  for (const value of values) {
    if (seen.has(value)) continue;
    seen.add(value);
    unique.push(value);
  }
  return unique;
}

function seedDetailBackedChatRow(state: ReadModelEntityState, thread: ChatThreadProjection): void {
  if (state.chats[thread.chatId]) {
    if (!state.chatOrder.includes(thread.chatId)) state.chatOrder.push(thread.chatId);
    return;
  }
  state.chats[thread.chatId] = {
    chatId: thread.chatId,
    surface: chatIndexSurface(thread.surface),
    title: thread.title,
    status: thread.archived ? 'archived' : thread.status,
    unreadCount: 0,
    lastActivityAt: null,
    repoId: thread.repoId ?? null,
    worktreeId: thread.worktreeId ?? null,
    ticketId: thread.ticketId ?? null,
    runId: thread.runId ?? null,
    agent: thread.agent ?? null,
    agentProfile: thread.agentProfile ?? null,
    chatKind: thread.chatKind ?? null,
    model: thread.model ?? null,
    runtime: thread.runtime ?? null,
    runtimeSource: thread.runtimeSource ?? null,
    modelSource: thread.modelSource ?? null,
    reasoning: thread.reasoning ?? null,
    reasoningSource: thread.reasoningSource ?? null,
    groupId: null
  };
  state.chatOrder.push(thread.chatId);
}

function chatIndexSurface(surface: string): ChatIndexRow['surface'] {
  if (['pma', 'file_chat', 'telegram', 'discord', 'app_server', 'other'].includes(surface)) {
    return surface as ChatIndexRow['surface'];
  }
  return 'other';
}

function chatTranscriptCardEntityId(card: ChatTranscriptCard): string {
  return card.id;
}

function orderChatTranscriptCards(cards: ChatTranscriptCard[]): ChatTranscriptCard[] {
  return [...cards].sort(compareChatTranscriptCards);
}

function insertOrderedChatTranscriptId(
  transcript: { cardsById: Record<string, ChatTranscriptCard>; order: string[] },
  id: string
): void {
  const card = transcript.cardsById[id];
  if (!card) return;
  let low = 0;
  let high = transcript.order.length;
  while (low < high) {
    const mid = Math.floor((low + high) / 2);
    const midCard = transcript.cardsById[transcript.order[mid]];
    if (!midCard || compareChatTranscriptCards(midCard, card) <= 0) low = mid + 1;
    else high = mid;
  }
  transcript.order.splice(low, 0, id);
}

function compareChatTranscriptCards(left: ChatTranscriptCard, right: ChatTranscriptCard): number {
  const leftTimestamp = transcriptCardTimestamp(left) ?? '';
  const rightTimestamp = transcriptCardTimestamp(right) ?? '';
  if (leftTimestamp && rightTimestamp && leftTimestamp !== rightTimestamp) return leftTimestamp.localeCompare(rightTimestamp);
  const sameTurn = transcriptCardTurnId(left) && transcriptCardTurnId(left) === transcriptCardTurnId(right);
  if (sameTurn) {
    const byRole = transcriptCardRoleRank(left).localeCompare(transcriptCardRoleRank(right));
    if (byRole !== 0) return byRole;
  }
  const byKey = transcriptCardOrderKey(left).localeCompare(transcriptCardOrderKey(right));
  if (byKey !== 0) return byKey;
  const byRole = transcriptCardRoleRank(left).localeCompare(transcriptCardRoleRank(right));
  if (byRole !== 0) return byRole;
  return chatTranscriptCardEntityId(left).localeCompare(chatTranscriptCardEntityId(right));
}

function transcriptCardOrderKey(card: ChatTranscriptCard): string {
  if ('orderKey' in card && typeof card.orderKey === 'string') return card.orderKey.trim();
  return '';
}

function transcriptCardRoleRank(card: ChatTranscriptCard): string {
  if (card.kind === 'message') return card.message.role === 'user' ? '0' : '2';
  return '1';
}

function transcriptCardTurnId(card: ChatTranscriptCard): string | null {
  return 'turnId' in card ? card.turnId : null;
}

function transcriptCardTimestamp(card: ChatTranscriptCard): string | null {
  if ('timestamp' in card && card.timestamp) return card.timestamp;
  if (card.kind === 'message') return card.message.createdAt;
  return null;
}

function withBoundedChatProgressEvents(progress: ChatRunProgress): ChatRunProgress {
  const events = progress.events.filter((event) => !isDebugOnlyProgressArtifact(event)).slice(-LIVE_PROGRESS_EVENT_LIMIT);
  return events.length === progress.events.length ? progress : { ...progress, events };
}

function isDebugOnlyProgressArtifact(event: SurfaceArtifact): boolean {
  const progressItem = asRecord(event.raw.progress_item);
  if (progressItem.hidden === true || event.raw.hidden === true) return true;
  const kind = stringValue(progressItem.kind ?? event.kind).toLowerCase();
  if (kind === 'hidden' || kind === 'decode_failure') return true;
  const eventType = stringValue(event.raw.event_type ?? progressItem.event_type).toLowerCase();
  const intermediateKind = stringValue(progressItem.intermediate_kind ?? event.raw.intermediate_kind).toLowerCase();
  return eventType === 'output_delta' && ['assistant_stream', 'assistant_message', 'log_line'].includes(intermediateKind);
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value : value === null || value === undefined ? '' : String(value);
}

function bump(state: ReadModelEntityState, kind: EntityKind, id: string): void {
  state.versions[kind][id] = (state.versions[kind][id] ?? 0) + 1;
}

function rememberCursor(state: ReadModelEntityState, key: string, cursor: ProjectionCursor): void {
  state.cursors[key] = cursor;
}

function isNewer(previous: ProjectionCursor | undefined | null, next: ProjectionCursor): boolean {
  return !previous || next.sequence > previous.sequence || next.value !== previous.value;
}

function isRepairEvent(eventType: string, operation: string): boolean {
  return eventType === 'projection.cursor_gap' || operation === 'reset' || operation === 'invalidate';
}

function countersFromRows(rows: ChatIndexRow[]): ChatIndexCounters {
  return {
    total: rows.length,
    waiting: rows.filter((row) => row.status === 'waiting').length,
    running: rows.filter((row) => row.status === 'running').length,
    unread: rows.reduce((total, row) => total + Math.max(0, row.unreadCount || 0), 0),
    archived: rows.filter((row) => row.status === 'archived').length
  };
}
