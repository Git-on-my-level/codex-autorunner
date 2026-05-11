import { writable, type Readable } from 'svelte/store';
import type { PmaQueuedTurn } from '$lib/api/client';
import type {
  ChatArtifactSummary,
  ChatDetailPatchEvent,
  ChatDetailSnapshot,
  ChatIndexCounters,
  ChatIndexGroup,
  ChatIndexPatchEvent,
  ChatIndexRow,
  ChatQueueSummary,
  ChatThreadProjection,
  ChatTimelineItem,
  ProjectionCursor,
  RepoTopology,
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
import type { PmaRunProgress, PmaTimelineItem, SurfaceArtifact, TicketSummary } from '$lib/viewModels/domain';

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
  kind: 'send' | 'archive' | 'queue' | 'read-marker';
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

export type ReadModelEntityState = {
  cursors: Record<string, ProjectionCursor>;
  chatIndexCursor: ProjectionCursor | null;
  chats: Record<string, ChatIndexRow>;
  chatOrder: string[];
  chatGroups: Record<string, ChatIndexGroup>;
  chatGroupOrder: string[];
  chatCounters: ChatIndexCounters;
  chatDetails: Record<string, ChatDetailProjection>;
  timelines: Record<string, TimelineProjection>;
  pmaTimelines: Record<string, { itemsById: Record<string, PmaTimelineItem>; order: string[] }>;
  pmaProgress: Record<string, PmaRunProgress>;
  pmaQueues: Record<string, PmaQueuedTurn[]>;
  pmaArtifacts: Record<string, SurfaceArtifact[]>;
  readMarkers: Record<string, string>;
  artifacts: Record<string, ChatArtifactSummary>;
  repos: Record<string, RepoTopology>;
  repoOrder: string[];
  worktrees: Record<string, WorktreeTopology>;
  worktreeOrder: string[];
  runtime: Record<string, RepoWorktreeRuntimeEntity>;
  tickets: Record<string, TicketProjection>;
  ticketSummaries: Record<string, TicketSummary>;
  ticketOrderByOwner: Record<string, string[]>;
  ticketSiblings: Record<string, TicketQueueSibling[]>;
  runs: Record<string, unknown>;
  pmaRuns: Record<string, PmaRunProgress>;
  pmaRunOrderByOwner: Record<string, string[]>;
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

export function createInitialReadModelState(): ReadModelEntityState {
  return {
    cursors: {},
    chatIndexCursor: null,
    chats: {},
    chatOrder: [],
    chatGroups: {},
    chatGroupOrder: [],
    chatCounters: emptyChatCounters,
    chatDetails: {},
    timelines: {},
    pmaTimelines: {},
    pmaProgress: {},
    pmaQueues: {},
    pmaArtifacts: {},
    readMarkers: {},
    artifacts: {},
    repos: {},
    repoOrder: [],
    worktrees: {},
    worktreeOrder: [],
    runtime: {},
    tickets: {},
    ticketSummaries: {},
    ticketOrderByOwner: {},
    ticketSiblings: {},
    runs: {},
    pmaRuns: {},
    pmaRunOrderByOwner: {},
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

  applyChatIndexSnapshot(snapshot: {
    cursor: ProjectionCursor;
    rows: ChatIndexRow[];
    groups: ChatIndexGroup[];
    counters: ChatIndexCounters;
  }): void {
    const next = cloneState(this.state);
    for (const id of Object.keys(next.chats)) bump(next, 'chat', id);
    for (const id of Object.keys(next.chatGroups)) bump(next, 'chatGroup', id);
    next.chats = keyed(snapshot.rows, (row) => row.chatId);
    next.chatOrder = snapshot.rows.map((row) => row.chatId);
    next.chatGroups = keyed(snapshot.groups, (group) => group.groupId);
    next.chatGroupOrder = snapshot.groups.map((group) => group.groupId);
    next.chatCounters = snapshot.counters;
    next.chatIndexCursor = snapshot.cursor;
    rememberCursor(next, 'chat.index', snapshot.cursor);
    for (const row of snapshot.rows) bump(next, 'chat', row.chatId);
    for (const group of snapshot.groups) bump(next, 'chatGroup', group.groupId);
    this.commit(next);
  }

  replaceChatIndexRows(rows: ChatIndexRow[], cursor: ProjectionCursor, counters = countersFromRows(rows)): void {
    this.applyChatIndexSnapshot({ cursor, rows, groups: [], counters });
  }

  upsertChatIndexRows(rows: ChatIndexRow[]): void {
    if (!rows.length) return;
    const next = cloneState(this.state);
    for (const row of rows) {
      next.chats[row.chatId] = row;
      if (!next.chatOrder.includes(row.chatId)) next.chatOrder.push(row.chatId);
      bump(next, 'chat', row.chatId);
    }
    next.chatCounters = countersFromRows(next.chatOrder.map((id) => next.chats[id]).filter(Boolean));
    this.commit(next);
  }

  applyChatIndexPatchEvent(event: ChatIndexPatchEvent): 'applied' | 'ignored' | 'repair_required' {
    if (isRepairEvent(event.envelope.eventType, event.envelope.operation)) {
      this.markRepairRequired(event.envelope.cursor);
      return 'repair_required';
    }
    if (!isNewer(this.state.cursors['chat.index'], event.envelope.cursor)) return 'ignored';
    const next = cloneState(this.state);
    for (const row of event.patch.rows) {
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
    if (event.patch.order) {
      next.chatOrder = event.patch.order.filter((id) => Boolean(next.chats[id]));
    }
    if (event.patch.counters) next.chatCounters = event.patch.counters;
    next.chatIndexCursor = event.envelope.cursor;
    rememberCursor(next, 'chat.index', event.envelope.cursor);
    this.commit(next);
    return 'applied';
  }

  applyChatDetailSnapshot(snapshot: ChatDetailSnapshot): void {
    const next = cloneState(this.state);
    upsertChatThread(next, snapshot.thread);
    next.chatDetails[snapshot.thread.chatId] = {
      thread: snapshot.thread,
      queue: snapshot.queue,
      artifactIds: snapshot.artifacts.map((artifact) => artifact.artifactId)
    };
    next.timelines[snapshot.thread.chatId] = {
      chatId: snapshot.thread.chatId,
      itemsById: keyed(snapshot.timeline, (item) => item.itemId),
      order: snapshot.timeline.map((item) => item.itemId),
      windowLimit: snapshot.timelineWindow.limit
    };
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
    const existingTimeline = next.timelines[chatId] ?? {
      chatId,
      itemsById: {},
      order: [],
      windowLimit: 50
    };
    for (const item of [...event.patch.appendedTimeline, ...event.patch.patchedTimeline]) {
      existingTimeline.itemsById[item.itemId] = item;
      if (!existingTimeline.order.includes(item.itemId)) existingTimeline.order.push(item.itemId);
    }
    for (const id of event.patch.removedTimelineIds) {
      delete existingTimeline.itemsById[id];
      existingTimeline.order = existingTimeline.order.filter((itemId) => itemId !== id);
    }
    next.timelines[chatId] = existingTimeline;
    const detail = next.chatDetails[chatId] ?? { thread: null, queue: null, artifactIds: [] };
    if (event.patch.thread) {
      detail.thread = event.patch.thread;
      upsertChatThread(next, event.patch.thread);
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
    bump(next, 'chat', chatId);
    bump(next, 'timeline', chatId);
    rememberCursor(next, cursorKey, event.envelope.cursor);
    this.commit(next);
    return 'applied';
  }

  replacePmaTimeline(chatId: string, items: PmaTimelineItem[]): void {
    const next = cloneState(this.state);
    next.pmaTimelines[chatId] = {
      itemsById: keyed(items, (item) => item.id),
      order: items.map((item) => item.id)
    };
    bump(next, 'timeline', chatId);
    this.commit(next);
  }

  upsertPmaTimelineItems(chatId: string, items: PmaTimelineItem[]): void {
    if (!items.length) return;
    const next = cloneState(this.state);
    const timeline = next.pmaTimelines[chatId] ?? { itemsById: {}, order: [] };
    for (const item of items) {
      timeline.itemsById[item.id] = item;
      if (!timeline.order.includes(item.id)) timeline.order.push(item.id);
    }
    next.pmaTimelines[chatId] = timeline;
    bump(next, 'timeline', chatId);
    this.commit(next);
  }

  removeOptimisticPmaTimelineItems(chatId: string): void {
    const timeline = this.state.pmaTimelines[chatId];
    if (!timeline || !timeline.order.some((id) => id.startsWith('optimistic:'))) return;
    const next = cloneState(this.state);
    const target = next.pmaTimelines[chatId];
    for (const id of target.order) {
      if (id.startsWith('optimistic:')) delete target.itemsById[id];
    }
    target.order = target.order.filter((id) => !id.startsWith('optimistic:'));
    bump(next, 'timeline', chatId);
    this.commit(next);
  }

  setPmaProgress(chatId: string, progress: PmaRunProgress | null): void {
    const next = cloneState(this.state);
    if (progress) next.pmaProgress[chatId] = progress;
    else delete next.pmaProgress[chatId];
    bump(next, 'run', chatId);
    this.commit(next);
  }

  setPmaQueue(chatId: string, queuedTurns: PmaQueuedTurn[]): void {
    const next = cloneState(this.state);
    next.pmaQueues[chatId] = [...queuedTurns];
    bump(next, 'run', chatId);
    this.commit(next);
  }

  setPmaArtifacts(chatId: string, artifacts: SurfaceArtifact[]): void {
    const next = cloneState(this.state);
    next.pmaArtifacts[chatId] = [...artifacts];
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
    for (const repo of snapshot.repos) bump(next, 'repo', repo.repoId);
    for (const worktree of snapshot.worktrees) bump(next, 'worktree', worktree.worktreeId);
    rememberCursor(next, 'repo_worktree.topology', snapshot.cursor);
    this.commit(next);
  }

  applyRepoWorktreeRuntimeSnapshot(snapshot: RepoWorktreeRuntimeSnapshot): void {
    const next = cloneState(this.state);
    for (const runtime of snapshot.runtime) {
      const id = `${runtime.entityKind}:${runtime.entityId}`;
      next.runtime[id] = { ...runtime, id };
      bump(next, runtime.entityKind, runtime.entityId);
    }
    rememberCursor(next, 'repo_worktree.runtime', snapshot.cursor);
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
    for (const chat of snapshot.linkedChats) {
      next.chats[chat.chatId] = chat;
      if (!next.chatOrder.includes(chat.chatId)) next.chatOrder.push(chat.chatId);
      bump(next, 'chat', chat.chatId);
    }
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

  replaceScopedRuns(ownerKey: string, runs: PmaRunProgress[]): void {
    const next = cloneState(this.state);
    next.pmaRunOrderByOwner[ownerKey] = runs.map((run) => run.id);
    for (const run of runs) {
      next.pmaRuns[run.id] = run;
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
    for (const chat of event.patch.linkedChats) {
      next.chats[chat.chatId] = chat;
      if (!next.chatOrder.includes(chat.chatId)) next.chatOrder.push(chat.chatId);
      bump(next, 'chat', chat.chatId);
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
    const timeline = next.timelines[chatId] ?? { chatId, itemsById: {}, order: [], windowLimit: 50 };
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
    const timeline = next.timelines[chatId];
    const optimisticItemId = (mutation?.previousValue as { itemId?: string } | undefined)?.itemId;
    if (timeline && optimisticItemId) {
      delete timeline.itemsById[optimisticItemId];
      timeline.itemsById[backendItem.itemId] = backendItem;
      timeline.order = timeline.order.map((id) => (id === optimisticItemId ? backendItem.itemId : id));
      if (!timeline.order.includes(backendItem.itemId)) timeline.order.push(backendItem.itemId);
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
      const timeline = next.timelines[chatId];
      if (timeline && itemId) {
        delete timeline.itemsById[itemId];
        timeline.order = timeline.order.filter((id) => id !== itemId);
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

  optimisticArchiveChat(chatId: string, reconciliationId: string): void {
    const previous = this.state.chats[chatId];
    if (!previous) return;
    const next = cloneState(this.state);
    next.chats[chatId] = { ...previous, status: 'archived' };
    next.optimistic[reconciliationId] = {
      reconciliationId,
      kind: 'archive',
      entityKind: 'chat',
      entityId: chatId,
      status: 'pending',
      createdAt: new Date().toISOString(),
      previousValue: previous
    };
    bump(next, 'chat', chatId);
    this.commit(next);
  }

  revertOptimisticMutation(reconciliationId: string): void {
    const mutation = this.state.optimistic[reconciliationId];
    if (!mutation) return;
    const next = cloneState(this.state);
    if (mutation.kind === 'archive' && mutation.entityKind === 'chat' && mutation.previousValue) {
      next.chats[mutation.entityId] = mutation.previousValue as ChatIndexRow;
      bump(next, 'chat', mutation.entityId);
    }
    if (mutation.kind === 'send' && mutation.entityKind === 'timeline') {
      const itemId = (mutation.previousValue as { itemId?: string } | undefined)?.itemId;
      const timeline = next.timelines[mutation.entityId];
      if (timeline && itemId) {
        delete timeline.itemsById[itemId];
        timeline.order = timeline.order.filter((id) => id !== itemId);
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
    cursor: state.chatIndexCursor
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
    chatDetails: cloneRecord(state.chatDetails),
    timelines: cloneRecord(state.timelines),
    pmaTimelines: cloneRecord(state.pmaTimelines),
    pmaProgress: { ...state.pmaProgress },
    pmaQueues: cloneRecord(state.pmaQueues),
    pmaArtifacts: cloneRecord(state.pmaArtifacts),
    readMarkers: { ...state.readMarkers },
    artifacts: { ...state.artifacts },
    repos: { ...state.repos },
    repoOrder: [...state.repoOrder],
    worktrees: { ...state.worktrees },
    worktreeOrder: [...state.worktreeOrder],
    runtime: { ...state.runtime },
    tickets: { ...state.tickets },
    ticketSummaries: { ...state.ticketSummaries },
    ticketOrderByOwner: cloneRecord(state.ticketOrderByOwner),
    ticketSiblings: cloneRecord(state.ticketSiblings),
    runs: { ...state.runs },
    pmaRuns: { ...state.pmaRuns },
    pmaRunOrderByOwner: cloneRecord(state.pmaRunOrderByOwner),
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

function cloneRecord<T>(record: Record<string, T>): Record<string, T> {
  return Object.fromEntries(Object.entries(record).map(([key, value]) => [key, structuredClone(value)]));
}

function keyed<T>(items: T[], key: (item: T) => string): Record<string, T> {
  const record: Record<string, T> = {};
  for (const item of items) record[key(item)] = item;
  return record;
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
  return eventType === 'projection.cursor_gap' || operation === 'reset';
}

function upsertChatThread(state: ReadModelEntityState, thread: ChatThreadProjection): void {
  state.chats[thread.chatId] = {
    chatId: thread.chatId,
    surface: thread.surface === 'pma' ? 'pma' : 'other',
    title: thread.title,
    status: thread.archived ? 'archived' : thread.status,
    unreadCount: state.chats[thread.chatId]?.unreadCount ?? 0,
    repoId: thread.repoId,
    worktreeId: thread.worktreeId,
    ticketId: thread.ticketId,
    runId: thread.runId,
    agent: thread.agent,
    model: thread.model,
    lastActivityAt: state.chats[thread.chatId]?.lastActivityAt ?? null,
    groupId: state.chats[thread.chatId]?.groupId ?? null
  };
  if (!state.chatOrder.includes(thread.chatId)) state.chatOrder.push(thread.chatId);
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
