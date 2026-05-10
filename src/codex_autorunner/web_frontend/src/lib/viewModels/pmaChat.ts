import type {
  PmaChatMessage,
  PmaChatSummary,
  PmaRunProgress,
  PmaTimelineItem,
  RepoSummary,
  SurfaceArtifact,
  WorktreeSummary,
  WorkStatus
} from './domain';
import { normalizeOptionalWorkStatus } from './domain';
import { surfaceRefFromThreadRaw } from './thread';

/** Status chips (All / Waiting / …) on the chat list. */
export type PmaChatStatusFilter = 'all' | 'active' | 'waiting' | 'unread';

/** Full list filter: status chips, grouped ticket runs, or `surface:<slug>` messenger filters. */
export type PmaChatFilter = PmaChatStatusFilter | 'ticket_runs' | `surface:${string}`;

/** Token for the chats sidebar filter that lists only ticket-flow run groups (collapsed headers). */
export const PMA_CHAT_TICKET_RUNS_FILTER = 'ticket_runs' as const satisfies PmaChatFilter;

/** Synthetic list selection id for pinned PMA Memory in the chats sidebar. */
export const PMA_MEMORY_LIST_ID = '__memory__';

export const PMA_CHAT_FILTER_ORDER: PmaChatStatusFilter[] = ['all', 'waiting', 'active', 'unread'];

const INTERNAL_MESSENGER_SURFACE_KINDS = new Set([
  'managed_thread',
  'web',
  'hub',
  'local',
  'api'
]);

function rawString(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null;
}

function normalizeMessengerSlug(kind: string): string {
  const slug = kind
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '');
  return slug || 'external';
}

function messengerSurfaceLabel(slug: string): string {
  const map: Record<string, string> = {
    discord: 'Discord',
    telegram: 'Telegram',
    slack: 'Slack',
    mattermost: 'Mattermost',
    msteams: 'Microsoft Teams',
    teams: 'Teams'
  };
  if (map[slug]) return map[slug];
  return slug
    .split('_')
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ');
}

function messengerBadgeClass(slug: string): string {
  const safe = slug.replace(/[^a-z0-9-]+/g, '-').replace(/^-+|-+$/g, '') || 'external';
  return `surface-${safe}`;
}

/** Badge + filter slug for chats bound to an external messenger surface (Discord, Telegram, …). */
export function pmaChatMessengerSurface(
  chat: PmaChatSummary | null
): { slug: string; label: string; badgeClass: string } | null {
  if (!chat) return null;
  const raw = chat.raw as Record<string, unknown>;
  const ref = surfaceRefFromThreadRaw(raw);
  if (ref) {
    const kindLower = ref.kind.trim().toLowerCase();
    if (!INTERNAL_MESSENGER_SURFACE_KINDS.has(kindLower)) {
      const slug = normalizeMessengerSlug(ref.kind);
      return { slug, label: messengerSurfaceLabel(slug), badgeClass: messengerBadgeClass(slug) };
    }
  }
  const kindOnly = rawString(raw.surface_kind ?? raw.channel_kind)?.toLowerCase() ?? '';
  if (kindOnly && !INTERNAL_MESSENGER_SURFACE_KINDS.has(kindOnly)) {
    const slug = normalizeMessengerSlug(kindOnly);
    return { slug, label: messengerSurfaceLabel(slug), badgeClass: messengerBadgeClass(slug) };
  }
  const title = chat.title.trim().toLowerCase();
  if (title.startsWith('discord:')) {
    return { slug: 'discord', label: 'Discord', badgeClass: 'surface-discord' };
  }
  if (title.startsWith('telegram:')) {
    return { slug: 'telegram', label: 'Telegram', badgeClass: 'surface-telegram' };
  }
  return null;
}

export function pmaChatSurfaceFilterToken(slug: string): PmaChatFilter {
  return `surface:${slug}`;
}

export function isPmaChatSurfaceFilter(filter: PmaChatFilter): filter is `surface:${string}` {
  return filter.startsWith('surface:');
}

export function pmaChatSurfaceFilterOptions(
  chats: PmaChatSummary[]
): { slug: string; label: string; count: number }[] {
  const counts = new Map<string, { label: string; count: number }>();
  for (const chat of chats) {
    const surf = pmaChatMessengerSurface(chat);
    if (!surf) continue;
    const prev = counts.get(surf.slug);
    if (prev) prev.count += 1;
    else counts.set(surf.slug, { label: surf.label, count: 1 });
  }
  return [...counts.entries()]
    .map(([slug, value]) => ({ slug, label: value.label, count: value.count }))
    .sort((left, right) => left.label.localeCompare(right.label));
}

export type PendingAttachmentKind = 'file' | 'image' | 'link';

export type DocumentFileIntentKind =
  | 'browse_source'
  | 'select_item'
  | 'attach_uploaded_file'
  | 'reference_path'
  | 'include_link'
  | 'remove_pending_attachment'
  | 'clear_pending_attachments';

export type DocumentFileIntentPayload = {
  intent: DocumentFileIntentKind;
  source?: 'tickets' | 'contextspace' | 'filebox' | 'workspace' | 'upload' | 'link';
  id?: string;
  kind?: PendingAttachmentKind;
  title?: string;
  path?: string;
  url?: string | null;
  uploadedName?: string | null;
  sizeLabel?: string | null;
  uploadState?: PendingAttachment['uploadState'];
  metadata?: Record<string, unknown>;
};

export type PendingAttachment = {
  id: string;
  kind: PendingAttachmentKind;
  title: string;
  sizeLabel: string | null;
  url: string | null;
  uploadedName: string | null;
  uploadState: 'pending' | 'uploaded' | 'error';
};

export type ModelSelectorState = {
  state: 'loading' | 'empty' | 'error' | 'loaded';
  label: string;
  disabled: boolean;
};

export type ArtifactCardView = {
  label: string;
  tone: 'neutral' | 'media' | 'success' | 'warning' | 'danger' | 'link';
  primaryAction: string | null;
  preview: 'image' | 'link' | 'text' | 'file' | 'none';
  detailLabel: string;
};

export type PmaCard =
  | { kind: 'message'; id: string; message: PmaChatMessage; turnId: string | null; orderKey: string; timestamp: string | null }
  | { kind: 'intermediate'; id: string; title: string; text: string; eventIds: string[]; detail: string | null; turnId: string | null; orderKey: string; timestamp: string | null }
  | { kind: 'tool_group'; id: string; tools: PmaToolCallCard[]; turnId: string | null; orderKey: string; timestamp: string | null }
  | { kind: 'turn_summary'; id: string; title: string; cards: PmaCard[]; turnId: string | null; orderKey: string; timestamp: string | null }
  | { kind: 'approval'; id: string; title: string; summary: string; detail: string | null; turnId: string | null; orderKey: string; timestamp: string | null }
  | { kind: 'ticket'; id: string; title: string; summary: string | null; ticketId: string }
  | { kind: 'artifact'; id: string; artifact: SurfaceArtifact };

export type PmaToolCallCard = {
  id: string;
  title: string;
  summary: string | null;
  detail: string | null;
  state: 'started' | 'completed' | 'failed' | 'unknown';
  eventIds: string[];
  source?: SurfaceArtifact;
};

type CanonicalProgressItem = {
  item_id?: string;
  kind?: string;
  state?: string;
  title?: string;
  summary?: string | null;
  event_ids?: unknown;
  group_id?: string | null;
  group_kind?: string | null;
  tool_name?: string | null;
  hidden?: boolean;
};

export type PmaLiveActivity = {
  state: WorkStatus;
  title: string;
  summary: string;
  elapsedLabel: string | null;
  steps: SurfaceArtifact[];
};

export type PmaStatusBar = {
  state: WorkStatus;
  phase: string;
  elapsedLabel: string;
  queueDepthLabel: string;
};

export type ManagedThreadCreatePayload = {
  agent?: string;
  model?: string;
  profile?: string;
  name: string;
  scope_urn: string;
};

export type PmaChatScopeOption =
  | {
      id: 'local';
      kind: 'local';
      label: string;
      detail: string;
      scopeUrn: string;
    }
  | {
      id: string;
      kind: 'repo';
      label: string;
      detail: string;
      resourceKind: 'repo';
      resourceId: string;
      scopeUrn: string;
    }
  | {
      id: string;
      kind: 'worktree';
      label: string;
      detail: string;
      workspaceRoot: string;
      resourceId: string;
      parentRepoId: string | null;
      scopeUrn: string;
    };

export type ManagedThreadMessagePayload = {
  message: string;
  attachments?: DocumentFileIntentPayload[];
  model?: string;
  reasoning?: string;
  profile?: string;
  busy_policy?: 'queue' | 'interrupt';
  defer_execution?: boolean;
  wait_for_confirmation?: boolean;
};

const activeStatuses: WorkStatus[] = ['running'];
const waitingStatuses: WorkStatus[] = ['waiting', 'blocked'];

export function filterPmaChats(
  chats: PmaChatSummary[],
  filter: PmaChatFilter,
  query: string,
  lastSeen: Record<string, string> = {}
): PmaChatSummary[] {
  const needle = query.trim().toLowerCase();
  return chats
    .filter((chat) => {
      if (isPmaChatSurfaceFilter(filter)) {
        const slug = filter.slice('surface:'.length);
        return pmaChatMessengerSurface(chat)?.slug === slug;
      }
      if (filter === 'ticket_runs') return pmaChatRunGroupKey(chat) !== null;
      if (filter === 'active') return activeStatuses.includes(chat.status);
      if (filter === 'waiting') return waitingStatuses.includes(chat.status);
      if (filter === 'unread') {
        if (!chat.updatedAt) return false;
        const seen = lastSeen[chat.id];
        return !seen || chat.updatedAt > seen;
      }
      return true;
    })
    .filter((chat) => {
      if (!needle) return true;
      const surfaceLabel = pmaChatMessengerSurface(chat)?.label;
      return [
        chat.title,
        chat.repoId,
        chat.worktreeId,
        chat.ticketId,
        chat.agentId,
        chat.model,
        surfaceLabel,
        chat.raw.resource_kind,
        chat.raw.resource_id
      ]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(needle));
    });
}

/** Waiting/blocked chats first (operator inbox), then most recently updated. */
export function sortChatsWaitingFirst(chats: PmaChatSummary[]): PmaChatSummary[] {
  const waitingRank = (status: WorkStatus) =>
    status === 'waiting' || status === 'blocked' ? 0 : 1;
  return [...chats].sort((left, right) => {
    const rankDiff = waitingRank(left.status) - waitingRank(right.status);
    if (rankDiff !== 0) return rankDiff;
    const leftTime = Date.parse(left.updatedAt ?? '') || 0;
    const rightTime = Date.parse(right.updatedAt ?? '') || 0;
    return rightTime - leftTime;
  });
}

export function summarizeFilterCounts(
  chats: PmaChatSummary[],
  lastSeen: Record<string, string> = {}
): Record<PmaChatStatusFilter, number> {
  return {
    all: chats.length,
    active: chats.filter((chat) => activeStatuses.includes(chat.status)).length,
    waiting: chats.filter((chat) => waitingStatuses.includes(chat.status)).length,
    unread: chats.filter(
      (chat) => chat.updatedAt && (!lastSeen[chat.id] || chat.updatedAt > lastSeen[chat.id])
    ).length
  };
}

/**
 * A "run group" key for ticket-flow chats. Ticket-flow chats sharing the same
 * worktree (or repo, for repo-scoped flows) belong to the same operator-visible
 * run; they collapse into a single row on chat lists.
 */
export type PmaChatRunGroup = {
  key: string;
  scopeKind: 'worktree' | 'repo';
  scopeId: string;
  scopeLabel: string;
  chats: PmaChatSummary[];
  totalCount: number;
  unreadCount: number;
  activeCount: number;
  waitingCount: number;
  doneCount: number;
  failedCount: number;
  agents: string[];
  status: WorkStatus;
  updatedAt: string | null;
};

export type PmaChatListEntry =
  | { kind: 'group'; group: PmaChatRunGroup }
  | { kind: 'chat'; chat: PmaChatSummary };

export function pmaChatRunGroupKey(chat: PmaChatSummary): string | null {
  if (!chat.isTicketFlow && !chat.ticketId) return null;
  if (chat.worktreeId) return `worktree:${chat.worktreeId}`;
  if (chat.repoId) return `repo:${chat.repoId}`;
  return null;
}

/** Number of distinct repo/worktree ticket-flow runs (same cardinality as run-group rows). */
export function countTicketRunGroups(chats: PmaChatSummary[]): number {
  const keys = new Set<string>();
  for (const chat of chats) {
    const key = pmaChatRunGroupKey(chat);
    if (key) keys.add(key);
  }
  return keys.size;
}

function isUnread(chat: PmaChatSummary, lastSeen: Record<string, string>): boolean {
  if (!chat.updatedAt) return false;
  const seen = lastSeen[chat.id];
  return !seen || chat.updatedAt > seen;
}

function rollupGroupStatus(group: PmaChatRunGroup): WorkStatus {
  if (group.waitingCount > 0) return 'waiting';
  if (group.activeCount > 0) return 'running';
  if (group.failedCount > 0) return 'failed';
  if (group.totalCount > 0 && group.doneCount === group.totalCount) return 'done';
  return 'idle';
}

export function buildPmaChatListEntries(
  chats: PmaChatSummary[],
  options: {
    lastSeen?: Record<string, string>;
    repoLabel?: (repoId: string) => string | null;
    worktreeLabel?: (worktreeId: string) => string | null;
    groupRuns?: boolean;
  } = {}
): PmaChatListEntry[] {
  const lastSeen = options.lastSeen ?? {};
  if (options.groupRuns === false) {
    return sortChatsWaitingFirst(chats).map((chat) => ({ kind: 'chat', chat }) as PmaChatListEntry);
  }
  const groups = new Map<string, PmaChatRunGroup>();
  const standalone: PmaChatSummary[] = [];

  for (const chat of chats) {
    const key = pmaChatRunGroupKey(chat);
    if (!key) {
      standalone.push(chat);
      continue;
    }
    let group = groups.get(key);
    if (!group) {
      const scopeKind: 'worktree' | 'repo' = chat.worktreeId ? 'worktree' : 'repo';
      const scopeId = (scopeKind === 'worktree' ? chat.worktreeId : chat.repoId) ?? '';
      const labelLookup = scopeKind === 'worktree' ? options.worktreeLabel : options.repoLabel;
      group = {
        key,
        scopeKind,
        scopeId,
        scopeLabel: labelLookup?.(scopeId) ?? scopeId,
        chats: [],
        totalCount: 0,
        unreadCount: 0,
        activeCount: 0,
        waitingCount: 0,
        doneCount: 0,
        failedCount: 0,
        agents: [],
        status: 'idle',
        updatedAt: null
      };
      groups.set(key, group);
    }
    group.chats.push(chat);
  }

  for (const group of groups.values()) {
    group.chats = sortChatsWaitingFirst(group.chats);
    group.totalCount = group.chats.length;
    const agentSet = new Set<string>();
    for (const chat of group.chats) {
      if (chat.agentId) agentSet.add(chat.agentId);
      if (isUnread(chat, lastSeen)) group.unreadCount += 1;
      if (chat.status === 'running') group.activeCount += 1;
      else if (chat.status === 'waiting' || chat.status === 'blocked') group.waitingCount += 1;
      else if (chat.status === 'done') group.doneCount += 1;
      else if (chat.status === 'failed' || chat.status === 'invalid') group.failedCount += 1;
      if (chat.updatedAt && (!group.updatedAt || chat.updatedAt > group.updatedAt)) {
        group.updatedAt = chat.updatedAt;
      }
    }
    group.agents = [...agentSet].sort();
    group.status = rollupGroupStatus(group);
  }

  type Sortable = { entry: PmaChatListEntry; waitingRank: number; sort: string };
  const sortables: Sortable[] = [];
  for (const group of groups.values()) {
    sortables.push({
      entry: { kind: 'group', group },
      waitingRank: group.waitingCount > 0 ? 0 : group.activeCount > 0 ? 1 : 2,
      sort: group.updatedAt ?? ''
    });
  }
  for (const chat of standalone) {
    sortables.push({
      entry: { kind: 'chat', chat },
      waitingRank: chat.status === 'waiting' || chat.status === 'blocked' ? 0 : chat.status === 'running' ? 1 : 2,
      sort: chat.updatedAt ?? ''
    });
  }
  sortables.sort((a, b) => {
    if (a.waitingRank !== b.waitingRank) return a.waitingRank - b.waitingRank;
    return (b.sort || '').localeCompare(a.sort || '');
  });
  return sortables.map((item) => item.entry);
}

/** Filter entries against a single PMA chat filter while preserving group structure. */
export function filterPmaChatEntries(
  entries: PmaChatListEntry[],
  filter: PmaChatFilter,
  search: string,
  lastSeen: Record<string, string> = {}
): PmaChatListEntry[] {
  const out: PmaChatListEntry[] = [];
  for (const entry of entries) {
    if (entry.kind === 'chat') {
      const filtered = filterPmaChats([entry.chat], filter, search, lastSeen);
      if (filtered.length) out.push(entry);
      continue;
    }
    const matchedChats = filterPmaChats(entry.group.chats, filter, search, lastSeen);
    if (!matchedChats.length) continue;
    if (matchedChats.length === entry.group.chats.length) {
      out.push(entry);
      continue;
    }
    // Rebuild a slimmer group containing only matched chats so counts stay honest.
    const trimmed: PmaChatRunGroup = {
      ...entry.group,
      chats: matchedChats,
      totalCount: matchedChats.length,
      unreadCount: 0,
      activeCount: 0,
      waitingCount: 0,
      doneCount: 0,
      failedCount: 0,
      agents: [],
      updatedAt: null
    };
    const agentSet = new Set<string>();
    for (const chat of matchedChats) {
      if (chat.agentId) agentSet.add(chat.agentId);
      if (isUnread(chat, lastSeen)) trimmed.unreadCount += 1;
      if (chat.status === 'running') trimmed.activeCount += 1;
      else if (chat.status === 'waiting' || chat.status === 'blocked') trimmed.waitingCount += 1;
      else if (chat.status === 'done') trimmed.doneCount += 1;
      else if (chat.status === 'failed' || chat.status === 'invalid') trimmed.failedCount += 1;
      if (chat.updatedAt && (!trimmed.updatedAt || chat.updatedAt > trimmed.updatedAt)) {
        trimmed.updatedAt = chat.updatedAt;
      }
    }
    trimmed.agents = [...agentSet].sort();
    trimmed.status = rollupGroupStatus(trimmed);
    out.push({ kind: 'group', group: trimmed });
  }
  return out;
}

export function chooseActiveChatId(
  chats: PmaChatSummary[],
  currentId: string | null,
  requestedId: string | null = null
): string | null {
  if (requestedId && chats.some((chat) => chat.id === requestedId)) return requestedId;
  if (currentId && chats.some((chat) => chat.id === currentId)) return currentId;
  return null;
}

export function buildPmaCards(
  timeline: PmaTimelineItem[],
  chat: PmaChatSummary | null,
  artifacts: SurfaceArtifact[]
): PmaCard[] {
  const messageAttachmentKeys = collectMessageAttachmentKeys(timeline);
  const timelineCards = timeline
    .flatMap(timelineItemToCard)
    .filter((card) => !isMessageAttachmentArtifactCard(card, messageAttachmentKeys));
  const cards: PmaCard[] = [...timelineCards];

  if (chat?.ticketId) {
    cards.push({
      kind: 'ticket',
      id: `ticket-${chat.ticketId}`,
      ticketId: chat.ticketId,
      title: chat.ticketId,
      summary: chat.title
    });
  }

  const remainingArtifacts = filterArtifactsForActiveChat(artifacts, chat, null).filter(
    (artifact) => !artifactKeysFor(artifact).some((key) => messageAttachmentKeys.has(key))
  );
  for (const artifact of remainingArtifacts.slice(0, 4)) {
    cards.push({ kind: 'artifact', id: `artifact-${artifact.id}`, artifact });
  }

  return cards;
}

function isMessageAttachmentArtifactCard(card: PmaCard, messageAttachmentKeys: Set<string>): boolean {
  if (card.kind !== 'artifact') return false;
  return artifactKeysFor(card.artifact).some((key) => messageAttachmentKeys.has(key));
}

function collectMessageAttachmentKeys(timeline: PmaTimelineItem[]): Set<string> {
  const keys = new Set<string>();
  for (const item of timeline) {
    if (item.kind !== 'user_message' && item.kind !== 'assistant_message') continue;
    for (const raw of asRecordArray(item.payload.attachments)) {
      for (const key of artifactKeysFor(mapTimelineArtifact(raw))) keys.add(key);
    }
  }
  return keys;
}

function artifactKeysFor(artifact: SurfaceArtifact): string[] {
  const keys = new Set<string>();
  const add = (value: string | null | undefined) => {
    if (!value) return;
    const normalized = value.toLowerCase().trim();
    if (!normalized) return;
    keys.add(normalized);
    const slash = normalized.lastIndexOf('/');
    if (slash >= 0 && slash < normalized.length - 1) keys.add(normalized.slice(slash + 1));
  };
  add(artifact.url);
  add(artifact.title);
  add(artifact.id);
  const raw = artifact.raw;
  if (raw && typeof raw === 'object') {
    add(typeof raw.name === 'string' ? raw.name : null);
    add(typeof raw.rel_path === 'string' ? raw.rel_path : null);
    add(typeof raw.uploadedName === 'string' ? raw.uploadedName : null);
  }
  return [...keys];
}

export function buildPmaTranscriptCards(
  timeline: PmaTimelineItem[],
  chat: PmaChatSummary | null,
  artifacts: SurfaceArtifact[],
  progress: PmaRunProgress | null
): PmaCard[] {
  const timelineCards = coalesceThinkingTraceCards(buildPmaCards(timeline, chat, artifacts));
  const activityCards = shouldSupplementWithLiveActivity(timelineCards, progress)
    ? buildPmaActivityCards(progress?.events ?? [])
    : [];
  return summarizeCompletedTurnActivity(
    mergePmaTimelineAndActivityCards(
      timelineCards,
      activityCards
    ),
    progress
  );
}

export function reconcilePmaTimeline(
  existing: PmaTimelineItem[],
  incoming: PmaTimelineItem[],
  limit = 500
): PmaTimelineItem[] {
  if (!incoming.length) return existing;
  const canonicalUserMessages = incoming.filter((item) => item.kind === 'user_message' && !item.id.startsWith('optimistic:'));
  const byId = new Map(
    existing
      .filter((item) => !isSupersededOptimisticUserMessage(item, canonicalUserMessages))
      .map((item) => [item.id, item])
  );
  for (const item of incoming) {
    byId.set(item.id, { ...byId.get(item.id), ...item, payload: { ...byId.get(item.id)?.payload, ...item.payload } });
  }
  return [...byId.values()]
    .sort(compareTimelineItems)
    .slice(-limit);
}

function isSupersededOptimisticUserMessage(item: PmaTimelineItem, canonicalUserMessages: PmaTimelineItem[]): boolean {
  if (!item.id.startsWith('optimistic:') || item.kind !== 'user_message') return false;
  const optimisticText = stringValue(item.payload.text).trim();
  return canonicalUserMessages.some((canonical) => {
    if (canonical.chatId !== item.chatId) return false;
    const canonicalText = stringValue(canonical.payload.text).trim();
    return Boolean(optimisticText && canonicalText && optimisticText === canonicalText);
  });
}

export function optimisticUserTimelineItemFromSend(
  raw: Record<string, unknown>,
  fallbackText: string,
  fallbackChatId: string
): PmaTimelineItem | null {
  const turnId = stringValue(raw.managed_turn_id);
  const text = stringValue(raw.delivered_message) || stringValue(raw.prompt) || fallbackText;
  if (!turnId || !text.trim()) return null;
  const chatId = stringValue(raw.managed_thread_id) || fallbackChatId;
  const timestamp = new Date().toISOString();
  return {
    id: `turn:${turnId}:user`,
    kind: 'user_message',
    orderKey: `optimistic|${timestamp}|turn:${turnId}:user`,
    timestamp,
    chatId,
    turnId,
    status: normalizeOptionalWorkStatus(raw.execution_state ?? raw.status),
    payload: {
      text,
      text_preview: text.slice(0, 240),
      attachments: Array.isArray(raw.attachments) ? raw.attachments : []
    },
    raw: { optimistic: true, ...raw }
  };
}

export function mergePmaActivityEvents(
  existing: SurfaceArtifact[],
  incoming: SurfaceArtifact[],
  limit = 160
): SurfaceArtifact[] {
  if (!incoming.length) return existing;
  const byId = new Map(existing.map((event) => [event.id, event]));
  const ordered = [...existing];
  for (const event of incoming) {
    const current = byId.get(event.id);
    if (current) {
      const index = ordered.findIndex((item) => item.id === event.id);
      if (index >= 0) ordered[index] = { ...current, ...event, raw: { ...current.raw, ...event.raw } };
      byId.set(event.id, event);
    } else {
      ordered.push(event);
      byId.set(event.id, event);
    }
  }
  return ordered.slice(-limit);
}

export function buildPmaActivityCards(events: SurfaceArtifact[]): PmaCard[] {
  const cards: PmaCard[] = [];
  let toolGroup: PmaToolCallCard[] = [];

  const flushToolGroup = () => {
    if (!toolGroup.length) return;
    cards.push({
      kind: 'tool_group',
      id: `tools-${toolGroup[0].id}-${toolGroup.at(-1)?.id ?? toolGroup[0].id}`,
      tools: toolGroup,
      turnId: activityTurnId(toolGroup[0].source),
      orderKey: activityOrderKey(toolGroup[0].source),
      timestamp: toolGroup[0].source?.createdAt ?? null
    });
    toolGroup = [];
  };

  for (const event of events) {
    if (!isPrimaryProgressArtifact(event)) continue;
    if (isToolActivityEvent(event)) {
      toolGroup.push({
        id: event.id,
        title: toolDisplayTitle(event),
        summary: stringValue(canonicalProgressItem(event)?.summary) || event.summary,
        detail: null,
        state: toolState(event),
        eventIds: [event.id, ...progressItemEventIds(canonicalProgressItem(event))],
        source: event
      });
      continue;
    }

    if (isHiddenLifecycleActivity(event)) continue;
    const text = assistantActivityText(event);
    if (!text) continue;
    flushToolGroup();
    const previous = cards.at(-1);
    if (previous?.kind === 'intermediate' && shouldMergeIntermediate(previous, event)) {
      previous.text = mergeIntermediateText(previous.text, text);
      previous.eventIds.push(event.id);
      continue;
    }
    cards.push({
      kind: 'intermediate',
      id: `intermediate-${event.id}`,
      title: intermediateTitle(event),
      text,
      detail: null,
      eventIds: [event.id],
      turnId: activityTurnId(event),
      orderKey: activityOrderKey(event),
      timestamp: event.createdAt
    });
  }
  flushToolGroup();
  return cards;
}

/** Leading digits of PMA timeline `order_key` (`{sequence:08d}|…`), or an 8-digit test shorthand. */
function parseLeadingTimelineSequence(orderKey: string): number | null {
  const trimmed = orderKey.trim();
  const prefixed = /^(\d{8})\|/.exec(trimmed);
  if (prefixed) {
    const value = Number.parseInt(prefixed[1], 10);
    return Number.isFinite(value) ? value : null;
  }
  if (/^\d{8}$/.test(trimmed)) {
    const value = Number.parseInt(trimmed, 10);
    return Number.isFinite(value) ? value : null;
  }
  return null;
}

function turnUserTimelineSequence(cards: PmaCard[]): Map<string, number> {
  const map = new Map<string, number>();
  for (const card of cards) {
    if (card.kind !== 'message' || card.message.role !== 'user') continue;
    const turnId = card.turnId;
    if (!turnId) continue;
    const seq = parseLeadingTimelineSequence(card.orderKey);
    if (seq !== null) map.set(turnId, seq);
  }
  return map;
}

function buildOptimisticUserOrderKeyByTurn(cards: PmaCard[]): Map<string, string> {
  const map = new Map<string, string>();
  for (const card of cards) {
    if (card.kind !== 'message' || card.message.role !== 'user') continue;
    const turnId = card.turnId;
    if (!turnId || !card.orderKey.startsWith('optimistic|')) continue;
    map.set(turnId, card.orderKey);
  }
  return map;
}

/** Highest backend sequence for durable rows that are not the turn's final assistant reply. */
function maxTimelineTraceSequenceByTurn(cards: PmaCard[]): Map<string, number> {
  const map = new Map<string, number>();
  for (const card of cards) {
    if (!('turnId' in card) || !card.turnId) continue;
    if (card.kind === 'turn_summary') continue;
    if (card.kind === 'message' && card.message.role === 'assistant') continue;
    if (!('orderKey' in card) || !card.orderKey) continue;
    const seq = parseLeadingTimelineSequence(card.orderKey);
    if (seq === null) continue;
    const prior = map.get(card.turnId);
    map.set(card.turnId, prior === undefined ? seq : Math.max(prior, seq));
  }
  return map;
}

function transcriptMergeSortKey(
  card: PmaCard,
  isLiveExtra: boolean,
  ctx: {
    turnUserSeq: Map<string, number>;
    maxTraceSeqByTurn: Map<string, number>;
    optimisticUserOrderKeyByTurn: Map<string, string>;
    timelineOrdinal: number;
    liveOrdinal: number;
  }
): string {
  const orderKey = 'orderKey' in card ? card.orderKey : '';

  if (!isLiveExtra) {
    if (orderKey) return orderKey;
    return `99999999|meta|${String(ctx.timelineOrdinal).padStart(8, '0')}|${card.id}`;
  }

  const turnId = 'turnId' in card ? card.turnId : null;
  const liveInner = parseLeadingTimelineSequence(orderKey) ?? ctx.liveOrdinal;

  if (turnId) {
    const optimisticUserKey = ctx.optimisticUserOrderKeyByTurn.get(turnId);
    if (optimisticUserKey) {
      return `${optimisticUserKey}|live|${String(liveInner).padStart(8, '0')}|${String(ctx.liveOrdinal).padStart(8, '0')}|${card.id}`;
    }
    const traceMax = ctx.maxTraceSeqByTurn.get(turnId);
    const userSeq = ctx.turnUserSeq.get(turnId);
    const anchor =
      traceMax !== undefined ? traceMax : userSeq !== undefined ? userSeq : 0;
    const anchorStr = String(anchor).padStart(8, '0');
    return `${anchorStr}|live|${String(liveInner).padStart(8, '0')}|${String(ctx.liveOrdinal).padStart(8, '0')}|${card.id}`;
  }

  const ts = cardTimestamp(card);
  if (ts) {
    return `${ts}|live|${String(liveInner).padStart(8, '0')}|${String(ctx.liveOrdinal).padStart(8, '0')}|${card.id}`;
  }
  return `99999998|live|${String(liveInner).padStart(8, '0')}|${String(ctx.liveOrdinal).padStart(8, '0')}|${card.id}`;
}

export function mergePmaTimelineAndActivityCards(
  timelineCards: PmaCard[],
  activityCards: PmaCard[]
): PmaCard[] {
  if (!activityCards.length) return timelineCards;
  const timelineIds = new Set(timelineCards.map((card) => card.id));
  const timelineEventIds = new Set<string>();
  for (const card of timelineCards) {
    for (const id of cardEventIds(card)) timelineEventIds.add(id);
  }
  const extra = activityCards.filter((card) => {
    if (timelineIds.has(card.id)) return false;
    return !cardEventIds(card).some((id) => timelineEventIds.has(id));
  });
  if (!extra.length) return timelineCards;

  const turnUserSeq = turnUserTimelineSequence(timelineCards);
  const maxTraceSeqByTurn = maxTimelineTraceSequenceByTurn(timelineCards);
  const optimisticUserOrderKeyByTurn = buildOptimisticUserOrderKeyByTurn(timelineCards);
  const timelineOrdinal = new Map<PmaCard, number>(
    timelineCards.map((card, index) => [card, index])
  );
  const extraOrdinal = new Map<PmaCard, number>(extra.map((card, index) => [card, index]));
  const extraSet = new Set(extra);

  const merged = [...timelineCards, ...extra];
  return merged.sort((left, right) => {
    const leftKey = transcriptMergeSortKey(left, extraSet.has(left), {
      turnUserSeq,
      maxTraceSeqByTurn,
      optimisticUserOrderKeyByTurn,
      timelineOrdinal: timelineOrdinal.get(left) ?? 0,
      liveOrdinal: extraOrdinal.get(left) ?? 0
    });
    const rightKey = transcriptMergeSortKey(right, extraSet.has(right), {
      turnUserSeq,
      maxTraceSeqByTurn,
      optimisticUserOrderKeyByTurn,
      timelineOrdinal: timelineOrdinal.get(right) ?? 0,
      liveOrdinal: extraOrdinal.get(right) ?? 0
    });
    return leftKey.localeCompare(rightKey);
  });
}

export function filterArtifactsForActiveChat(
  artifacts: SurfaceArtifact[],
  chat: PmaChatSummary | null,
  progress: PmaRunProgress | null
): SurfaceArtifact[] {
  if (!chat) return [];
  const durableIds = new Set(
    [
      chat.id,
      chat.ticketId,
      chat.repoId,
      chat.worktreeId,
      progress?.chatId,
      progress?.id,
      stringValue(chat.raw.thread_target_id),
      stringValue(chat.raw.managed_thread_id),
      stringValue(chat.raw.thread_id),
      stringValue(chat.raw.resource_id),
      stringValue(chat.raw.last_execution_id),
      stringValue(chat.raw.last_run_id)
    ].filter(Boolean)
  );
  if (durableIds.size === 0) return [];
  const durableKeys = [
    'managed_thread_id',
    'thread_target_id',
    'thread_id',
    'chat_id',
    'managed_turn_id',
    'turn_id',
    'execution_id',
    'run_id',
    'ticket_id',
    'repo_id',
    'worktree_id',
    'worktree_repo_id',
    'resource_id',
    'filebox_origin_id'
  ];
  return artifacts.filter((artifact) =>
    durableKeys.some((key) => durableIds.has(stringValue(artifact.raw[key])))
  );
}

export function buildPmaLiveActivity(progress: PmaRunProgress | null): PmaLiveActivity | null {
  if (!progress) return null;
  const steps = progress.events.filter(isPrimaryProgressArtifact).slice(-4);
  const phase = progress.phase?.replace(/_/g, ' ') ?? null;
  const status = progress.status;
  const title =
    status === 'running'
      ? phase
        ? `Working · ${phase}`
        : 'Working'
      : status === 'waiting'
        ? phase
          ? `Waiting · ${phase}`
          : 'Waiting'
        : status === 'failed'
          ? 'Run failed'
          : status === 'blocked'
            ? 'Blocked'
          : status === 'done'
            ? 'Run complete'
            : 'Idle';
  const summary =
    progress.guidance ??
    (steps.length
      ? steps.at(-1)?.summary ?? steps.at(-1)?.title ?? 'Updating the workspace.'
      : status === 'running'
        ? 'Streaming activity.'
        : `Last update ${formatRelativeTime(progress.lastEventAt)}.`);
  const elapsedLabel = formatElapsedProgress(progress.elapsedSeconds, progress.idleSeconds);
  return { state: status, title, summary, elapsedLabel, steps };
}

export function buildPmaStatusBar(progress: PmaRunProgress | null, chat: PmaChatSummary | null): PmaStatusBar | null {
  if (!progress && !chat) return null;
  const state = progress?.status ?? chat?.status ?? 'idle';
  return {
    state,
    phase: progress?.phase?.replace(/_/g, ' ') || statusLabel(state),
    elapsedLabel: progress?.elapsedSeconds === null || progress?.elapsedSeconds === undefined
      ? 'elapsed n/a'
      : `${formatDuration(progress.elapsedSeconds)} elapsed`,
    queueDepthLabel: `queue ${progress?.queueDepth ?? 0}`
  };
}

export function isPrimaryProgressArtifact(artifact: SurfaceArtifact): boolean {
  const item = canonicalProgressItem(artifact);
  if (!item || item.hidden === true) return false;
  return ['assistant_update', 'tool', 'notice', 'approval', 'turn_failed', 'turn_interrupted'].includes(
    stringValue(item.kind)
  );
}

function isToolActivityEvent(event: SurfaceArtifact): boolean {
  return canonicalProgressItem(event)?.kind === 'tool';
}

function canonicalProgressItem(event: SurfaceArtifact): CanonicalProgressItem | null {
  const item = asRecord(event.raw.progress_item);
  if (!Object.keys(item).length) return null;
  return item as CanonicalProgressItem;
}

function assistantActivityText(event: SurfaceArtifact): string {
  const item = canonicalProgressItem(event);
  const kind = stringValue(item?.kind);
  if (!['assistant_update', 'notice', 'approval', 'turn_failed', 'turn_interrupted'].includes(kind)) {
    return '';
  }
  const rawSummary = stringValue(item?.summary) || event.summary || '';
  const summary = rawSummary.trim();
  if (summary && summary.toLowerCase() !== 'thinking') return rawSummary;
  const title = (stringValue(item?.title) || event.title).trim();
  if (title && title.toLowerCase() !== 'thinking' && title.toLowerCase() !== 'assistant update') return title;
  return summary || title;
}

function intermediateTitle(event: SurfaceArtifact): string {
  const item = canonicalProgressItem(event);
  const kind = stringValue(item?.kind);
  if (kind === 'turn_failed') return 'Run failed';
  if (kind === 'turn_interrupted') return 'Interrupted';
  if (kind === 'assistant_update') return 'Thinking';
  const title = (stringValue(item?.title) || event.title).trim();
  if (title && title.toLowerCase() !== assistantActivityText(event).toLowerCase()) return title;
  return 'Update';
}

function shouldMergeIntermediate(card: Extract<PmaCard, { kind: 'intermediate' }>, event: SurfaceArtifact): boolean {
  return card.title === 'Thinking' && canonicalProgressItem(event)?.kind === 'assistant_update';
}

function mergeIntermediateText(current: string, incoming: string): string {
  if (!current) return incoming;
  if (!incoming) return current;
  if (incoming === current) return current;
  if (incoming.startsWith(current)) return incoming;
  if (current.endsWith(incoming)) return current;
  return `${current}${incoming}`;
}

function shouldSupplementWithLiveActivity(cards: PmaCard[], progress: PmaRunProgress | null): boolean {
  if (!progress || !progress.events.length) return false;
  if (!progress.terminal) return true;
  const latestMessage = cards.filter((card) => card.kind === 'message').at(-1);
  if (latestMessage?.kind === 'message' && latestMessage.message.role === 'assistant') return false;
  return !cards.some(
    (card) =>
      card.kind === 'message' &&
      card.message.role === 'assistant' &&
      card.turnId === progress.id
  );
}

function coalesceThinkingTraceCards(cards: PmaCard[]): PmaCard[] {
  const output: PmaCard[] = [];
  const thinkingByTurn = new Map<string, Extract<PmaCard, { kind: 'intermediate' }>>();

  for (const card of cards) {
    if (!isThinkingTraceCard(card) || !card.turnId) {
      output.push(card);
      continue;
    }

    const existing = thinkingByTurn.get(card.turnId);
    if (!existing) {
      thinkingByTurn.set(card.turnId, card);
      output.push(card);
      continue;
    }

    existing.text = mergeIntermediateText(existing.text, card.text);
    existing.eventIds.push(...card.eventIds.filter((id) => !existing.eventIds.includes(id)));
    existing.detail = thinkingDetailSummary(existing.eventIds);
  }

  return output;
}

function isThinkingTraceCard(card: PmaCard): card is Extract<PmaCard, { kind: 'intermediate' }> {
  return card.kind === 'intermediate' && card.title.trim().toLowerCase() === 'thinking';
}

function thinkingTimelineDetail(item: PmaTimelineItem): string | null {
  const kind = stringValue(item.payload.intermediate_kind).trim().toLowerCase();
  if (kind !== 'thinking') return null;
  return thinkingDetailSummary(timelineSourceEventIds(item));
}

function thinkingDetailSummary(eventIds: string[]): string | null {
  const uniqueIds = Array.from(new Set(eventIds.filter(Boolean)));
  if (!uniqueIds.length) return null;
  const label = uniqueIds.length === 1 ? '1 thinking update' : `${uniqueIds.length} thinking updates`;
  return `${label} · source events ${uniqueIds.join(', ')}`;
}

function toolDisplayTitle(event: SurfaceArtifact): string {
  const item = canonicalProgressItem(event);
  return stringValue(item?.tool_name) || stringValue(item?.title) || event.summary || event.title || 'Tool call';
}

function toolState(event: SurfaceArtifact): PmaToolCallCard['state'] {
  const rawState = stringValue(canonicalProgressItem(event)?.state).toLowerCase();
  if (rawState === 'started' || rawState === 'completed' || rawState === 'failed') return rawState;
  return 'unknown';
}

function isDecodeFailureActivity(event: SurfaceArtifact): boolean {
  const item = canonicalProgressItem(event);
  const kind = stringValue(item?.kind).toLowerCase();
  const title = (stringValue(item?.title) || event.title).trim().toLowerCase();
  return kind === 'decode_failure' || title === 'decode failure';
}

function isHiddenLifecycleActivity(event: SurfaceArtifact): boolean {
  if (isDecodeFailureActivity(event)) return true;
  const item = canonicalProgressItem(event);
  const title = (stringValue(item?.title) || event.title).trim().toLowerCase();
  return title === 'chat execution journal' || title === 'compaction summary';
}

function timelineItemToCard(item: PmaTimelineItem): PmaCard[] {
  if (item.kind === 'user_message' || item.kind === 'assistant_message') {
    const text = stringValue(item.payload.text);
    if (!text.trim()) return [];
    const attachments = asRecordArray(item.payload.attachments).map(mapTimelineArtifact);
    return [
      {
        kind: 'message',
        id: item.id,
        turnId: item.turnId,
        orderKey: item.orderKey,
        timestamp: item.timestamp,
        message: {
          id: item.id,
          chatId: item.chatId,
          role: item.kind === 'user_message' ? 'user' : 'assistant',
          text,
          createdAt: item.timestamp,
          status: item.status,
          artifacts: attachments,
          raw: item.raw
        }
      }
    ];
  }
  if (item.kind === 'intermediate') {
    if (isHiddenLifecycleTimelineItem(item)) return [];
    const text = stringValue(item.payload.text);
    if (!text.trim()) return [];
    return [{
      kind: 'intermediate',
      id: item.id,
      title: intermediateTimelineTitle(item),
      text,
      detail: thinkingTimelineDetail(item) ?? timelineDetail(item),
      eventIds: timelineSourceEventIds(item),
      turnId: item.turnId,
      orderKey: item.orderKey,
      timestamp: item.timestamp
    }];
  }
  if (item.kind === 'tool_group') {
    return [{
      kind: 'tool_group',
      id: item.id,
      tools: [toolCardFromTimeline(item)],
      turnId: item.turnId,
      orderKey: item.orderKey,
      timestamp: item.timestamp
    }];
  }
  if (item.kind === 'approval') {
    const title = stringValue(item.payload.description) || stringValue(item.payload.summary) || 'Approval requested';
    return [{
      kind: 'approval',
      id: item.id,
      title: 'Approval requested',
      summary: title,
      detail: timelineDetail(item),
      turnId: item.turnId,
      orderKey: item.orderKey,
      timestamp: item.timestamp
    }];
  }
  if (item.kind === 'artifact') {
    return [{ kind: 'artifact', id: item.id, artifact: mapTimelineArtifact(item.payload) }];
  }
  return [];
}

function toolCardFromTimeline(item: PmaTimelineItem): PmaToolCallCard {
  const result = asRecord(item.payload.result);
  const call = asRecord(item.payload.call);
  const rawState = stringValue(result.status ?? item.raw.status ?? item.status).toLowerCase();
  const state: PmaToolCallCard['state'] =
    rawState.includes('fail') || rawState === 'error'
      ? 'failed'
      : result && Object.keys(result).length > 0
        ? 'completed'
        : 'started';
  const title = stringValue(item.payload.tool_name) || stringValue(call.tool_name) || 'Tool call';
  const summary = stringValue(result.summary) || stringValue(call.summary) || null;
  return { id: item.id, title, summary, detail: timelineDetail(item), state, eventIds: timelineSourceEventIds(item) };
}

function intermediateTimelineTitle(item: PmaTimelineItem): string {
  const kind = stringValue(item.payload.intermediate_kind).replace(/_/g, ' ');
  return kind || 'Update';
}

function isDecodeFailureTimelineItem(item: PmaTimelineItem): boolean {
  const kind = stringValue(item.payload.intermediate_kind).toLowerCase();
  const title = intermediateTimelineTitle(item).trim().toLowerCase();
  return kind === 'decode_failure' || title === 'decode failure';
}

function isHiddenLifecycleTimelineItem(item: PmaTimelineItem): boolean {
  if (isDecodeFailureTimelineItem(item)) return true;
  const intermediateKind = stringValue(item.payload.intermediate_kind).toLowerCase();
  const eventType = stringValue(item.payload.event_type).toLowerCase();
  if (
    eventType === 'output_delta' &&
    ['assistant_stream', 'assistant_message', 'log_line'].includes(intermediateKind)
  ) {
    return true;
  }
  const event = asRecord(item.payload.event);
  return ['chat_execution_journal', 'compaction_summary'].includes(
    stringValue(event.kind).toLowerCase()
  );
}

function timelineDetail(item: PmaTimelineItem): string | null {
  const detailSource =
    item.payload.live_tail_event ??
    item.payload.event ??
    item.payload.result ??
    item.payload.call ??
    null;
  if (!detailSource || typeof detailSource !== 'object') return null;
  try {
    return JSON.stringify(detailSource, null, 2);
  } catch {
    return null;
  }
}

function mapTimelineArtifact(raw: Record<string, unknown>): SurfaceArtifact {
  return {
    id: stringValue(raw.id ?? raw.artifact_id ?? raw.name ?? raw.url) || 'artifact',
    kind: 'file',
    title: stringValue(raw.title ?? raw.name ?? raw.url) || 'Artifact',
    summary: stringValue(raw.summary ?? raw.description) || null,
    url: stringValue(raw.url ?? raw.href) || null,
    createdAt: stringValue(raw.created_at ?? raw.modified_at) || null,
    raw
  };
}

function summarizeCompletedTurnActivity(cards: PmaCard[], progress: PmaRunProgress | null): PmaCard[] {
  const byTurn = new Map<string, PmaCard[]>();
  for (const card of cards) {
    const turnId = cardTurnId(card);
    if (!turnId) continue;
    const group = byTurn.get(turnId) ?? [];
    group.push(card);
    byTurn.set(turnId, group);
  }
  const summaryByTurn = new Map<string, PmaCard>();
  for (const [turnId, group] of byTurn) {
    const trace = group.filter(isTraceCard);
    if (!trace.length) continue;
    const hasAssistantMessage = group.some((card) => card.kind === 'message' && card.message.role === 'assistant');
    if (!hasAssistantMessage) continue;
    const isCurrentRunning = progress?.id === turnId && !progress.terminal && progress.status === 'running';
    if (isCurrentRunning) continue;
    const firstTrace = trace[0];
    summaryByTurn.set(turnId, {
      kind: 'turn_summary',
      id: `turn:${turnId}:summary`,
      title: `Worked for ${turnElapsedLabel(turnId, group, progress)}`,
      cards: trace,
      turnId,
      orderKey: firstTrace.orderKey,
      timestamp: firstTrace.timestamp
    });
  }
  const output: PmaCard[] = [];
  const inserted = new Set<string>();
  for (const card of cards) {
    const turnId = cardTurnId(card);
    if (turnId && summaryByTurn.has(turnId) && isTraceCard(card)) {
      if (!inserted.has(turnId)) {
        output.push(summaryByTurn.get(turnId)!);
        inserted.add(turnId);
      }
      continue;
    }
    output.push(card);
  }
  return output;
}

function turnElapsedLabel(turnId: string, cards: PmaCard[], progress: PmaRunProgress | null): string {
  if (progress?.id === turnId && progress.elapsedSeconds !== null) return formatDuration(progress.elapsedSeconds);
  const times = cards
    .map((card) => Date.parse(cardTimestamp(card) ?? ''))
    .filter((value) => Number.isFinite(value));
  if (times.length >= 2) return formatDuration(Math.max(0, Math.round((Math.max(...times) - Math.min(...times)) / 1000)));
  return 'a moment';
}

function isTraceCard(card: PmaCard): card is Extract<PmaCard, { kind: 'intermediate' | 'tool_group' | 'approval' }> {
  return card.kind === 'intermediate' || card.kind === 'tool_group' || card.kind === 'approval';
}

function cardTurnId(card: PmaCard): string | null {
  return 'turnId' in card ? card.turnId : null;
}

function cardTimestamp(card: PmaCard): string | null {
  return 'timestamp' in card ? card.timestamp : null;
}

function cardEventIds(card: PmaCard): string[] {
  if (card.kind === 'intermediate') return card.eventIds;
  if (card.kind === 'tool_group') return card.tools.flatMap((tool) => [tool.id, ...tool.eventIds]);
  if (card.kind === 'approval') return [card.id];
  return [];
}

function timelineSourceEventIds(item: PmaTimelineItem): string[] {
  return [
    item.id,
    ...unknownArrayToStrings(item.payload.source_event_ids),
    ...progressItemEventIds(asRecord(item.payload.progress_item) as CanonicalProgressItem),
    ...asRecordArray(item.payload.progress_items).flatMap((progressItem) => progressItemEventIds(progressItem as CanonicalProgressItem))
  ];
}

function progressItemEventIds(item: CanonicalProgressItem | null | undefined): string[] {
  return unknownArrayToStrings(item?.event_ids);
}

function unknownArrayToStrings(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => stringValue(item)).filter(Boolean);
}

function activityTurnId(event: SurfaceArtifact | undefined): string | null {
  if (!event) return null;
  return stringValue(event.raw.managed_turn_id ?? event.raw.turn_id ?? event.raw.execution_id ?? event.raw.run_id) || null;
}

function activityOrderKey(event: SurfaceArtifact | undefined): string {
  if (!event) return '';
  const item = canonicalProgressItem(event);
  const eventIds = progressItemEventIds(item);
  const sequence = Number.parseInt(eventIds.at(-1) ?? event.id, 10);
  if (Number.isFinite(sequence)) {
    return `${String(sequence).padStart(8, '0')}|${event.createdAt ?? ''}|${event.id}`;
  }
  return stringValue(event.raw.order_key) || `${event.createdAt ?? ''}|${event.id}|${stringValue(item?.item_id)}`;
}

function compareTimelineItems(left: PmaTimelineItem, right: PmaTimelineItem): number {
  return timelineSortKey(left).localeCompare(timelineSortKey(right));
}

function timelineSortKey(item: PmaTimelineItem): string {
  return item.orderKey || `${item.timestamp ?? ''}|${item.id}`;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function asRecordArray(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value)
    ? value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object' && !Array.isArray(item))
    : [];
}

export function formatRelativeTime(value: string | null, now = new Date()): string {
  if (!value) return 'No activity yet';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  const seconds = Math.max(0, Math.round((now.getTime() - parsed.getTime()) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  return `${days}d ago`;
}

export function progressPercent(chat: PmaChatSummary, progress: PmaRunProgress | null = null): number {
  if (typeof chat.progressPercent === 'number') return clampPercent(chat.progressPercent);
  if (progress?.status === 'done') return 100;
  if (progress?.status === 'failed') return 100;
  if (progress?.status === 'running') return 64;
  if (progress?.status === 'waiting') return 28;
  if (chat.status === 'running') return 58;
  if (chat.status === 'waiting' || chat.status === 'blocked') return 24;
  if (chat.status === 'done' || chat.status === 'failed') return 100;
  return 0;
}

export function statusLabel(status: WorkStatus): string {
  if (status === 'invalid') return 'needs repair';
  return status.replace('_', ' ');
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB'];
  let value = bytes / 1024;
  let unit = units[0];
  for (let index = 1; value >= 1024 && index < units.length; index += 1) {
    value /= 1024;
    unit = units[index];
  }
  return `${value >= 10 ? value.toFixed(0) : value.toFixed(1)} ${unit}`;
}

function formatElapsedProgress(elapsedSeconds: number | null, idleSeconds: number | null): string | null {
  const parts: string[] = [];
  if (elapsedSeconds !== null) parts.push(`${formatDuration(elapsedSeconds)} elapsed`);
  if (idleSeconds !== null && idleSeconds > 0) parts.push(`${formatDuration(idleSeconds)} idle`);
  return parts.length ? parts.join(' · ') : null;
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  if (minutes < 60) return remainder ? `${minutes}m ${remainder}s` : `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const minuteRemainder = minutes % 60;
  return minuteRemainder ? `${hours}h ${minuteRemainder}m` : `${hours}h`;
}

export function removePendingAttachment(
  attachments: PendingAttachment[],
  attachmentId: string
): PendingAttachment[] {
  return attachments.filter((attachment) => attachment.id !== attachmentId);
}

export function pendingAttachmentToIntent(attachment: PendingAttachment | DocumentFileIntentPayload): DocumentFileIntentPayload {
  if ('intent' in attachment) return attachment as DocumentFileIntentPayload;
  const intent = attachment.kind === 'link' ? 'include_link' : 'attach_uploaded_file';
  return {
    intent,
    source: attachment.kind === 'link' ? 'link' : 'upload',
    id: attachment.id,
    kind: attachment.kind,
    title: attachment.title,
    sizeLabel: attachment.sizeLabel,
    url: attachment.url,
    uploadedName: attachment.uploadedName,
    uploadState: attachment.uploadState
  };
}

export function composeMessageWithAttachments(
  draft: string,
  _attachments: PendingAttachment[]
): string {
  return draft.trim();
}

export function buildManagedThreadCreatePayload(
  agent: string,
  scope: PmaChatScopeOption = localPmaChatScopeOption(),
  name = 'New chat',
  model = '',
  profile = ''
): ManagedThreadCreatePayload {
  const base: Pick<ManagedThreadCreatePayload, 'agent' | 'name' | 'model'> = {
    agent: agent || undefined,
    name
  };
  if (model) base.model = model;
  const trimmedProfile = profile.trim();
  return {
    ...base,
    ...(trimmedProfile ? { profile: trimmedProfile } : {}),
    scope_urn: scope.scopeUrn
  };
}

export type PmaChatKind = 'pma' | 'coding_agent';

export function pmaChatKind(chat: PmaChatSummary | null): PmaChatKind {
  if (!chat) return 'pma';
  const rawKind = stringValue(chat.raw.chat_kind ?? chat.raw.thread_kind ?? chat.raw.kind).toLowerCase();
  if (['coding_agent', 'coding-agent', 'agent', 'direct_agent', 'direct-agent'].includes(rawKind)) return 'coding_agent';
  const explicitName = stringValue(chat.raw.display_name ?? chat.raw.name ?? chat.raw.title ?? chat.title).toLowerCase();
  return explicitName.includes('coding agent') ? 'coding_agent' : 'pma';
}

export function pmaChatKindLabel(kind: PmaChatKind): string {
  return kind === 'coding_agent' ? 'Coding agent' : 'Chat';
}

export function agentCapabilityAllowed(
  record: Record<string, unknown> | null,
  action: string
): boolean {
  const projection = record?.capability_projection;
  if (!projection || typeof projection !== 'object') return false;
  const actions = (projection as Record<string, unknown>).actions;
  if (!actions || typeof actions !== 'object') return false;
  const result = (actions as Record<string, unknown>)[action];
  return Boolean(result && typeof result === 'object' && (result as Record<string, unknown>).allowed === true);
}

export function localPmaChatScopeOption(): PmaChatScopeOption {
  return {
    id: 'local',
    kind: 'local',
    label: 'Local hub',
    detail: 'Current workspace',
    scopeUrn: 'hub'
  };
}

export function buildPmaChatScopeOptions(
  repos: RepoSummary[],
  worktrees: WorktreeSummary[]
): PmaChatScopeOption[] {
  return [
    localPmaChatScopeOption(),
    ...repos.map((repo) => ({
      id: `repo:${repo.id}`,
      kind: 'repo' as const,
      label: repo.name || repo.id,
      detail: `Repo · ${repo.id}`,
      resourceKind: 'repo' as const,
      resourceId: repo.id,
      scopeUrn: `repo:${repo.id}`
    })),
    ...worktrees
      .filter((worktree) => Boolean(worktree.path))
      .map((worktree) => ({
        id: `worktree:${worktree.id}`,
        kind: 'worktree' as const,
        label: worktree.name || worktree.id,
        detail: `Worktree · ${worktree.repoId ?? worktree.id}`,
        workspaceRoot: worktree.path || '.',
        resourceId: worktree.id,
        parentRepoId: worktree.repoId,
        scopeUrn: worktree.repoId
          ? `worktree:${worktree.repoId}/${worktree.id}`
          : `filesystem:${encodeURIComponent(worktree.path || '.')}`
      }))
  ];
}

export function pmaChatScopeLabel(scope: PmaChatScopeOption | null): string {
  if (!scope) return 'Workspace scope';
  if (scope.kind === 'local') return 'Local hub · current workspace';
  if (scope.kind === 'repo') return `Repo · ${scope.resourceId}`;
  return `Worktree · ${scope.resourceId}`;
}

export function pmaChatScopeLabelFromChat(chat: PmaChatSummary | null): string {
  if (!chat) return 'Choose a scope before creating a chat';
  if (chat.worktreeId) return `Worktree · ${chat.worktreeId}`;
  if (chat.repoId) return `Repo · ${chat.repoId}`;
  const workspaceRoot = stringValue(chat.raw.workspace_root);
  if (workspaceRoot && workspaceRoot !== '.') return `Hub · ${workspaceRoot}`;
  return 'Local hub · current workspace';
}

export type PmaChatScopeUiKind = 'repo' | 'worktree' | 'hub' | 'local';

export type PmaChatScopeTagView = {
  kindKey: PmaChatScopeUiKind;
  kindLabel: string;
  detail: string;
  /** Full path / id for tooltip when `detail` is shortened (hub workspace basename). */
  detailFull?: string;
};

function workspacePathBasename(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts.at(-1) ?? path;
}

/** Scope line split into a kind chip plus detail for chat list cards. */
export function pmaChatScopeTagView(
  chat: PmaChatSummary,
  opts?: {
    repoLabel?: (repoId: string) => string | null;
    worktreeLabel?: (worktreeId: string) => string | null;
  }
): PmaChatScopeTagView {
  const repoLabel = opts?.repoLabel;
  const worktreeLabel = opts?.worktreeLabel;
  if (chat.worktreeId) {
    return {
      kindKey: 'worktree',
      kindLabel: 'Worktree',
      detail: worktreeLabel?.(chat.worktreeId) ?? chat.worktreeId
    };
  }
  if (chat.repoId) {
    return {
      kindKey: 'repo',
      kindLabel: 'Repo',
      detail: repoLabel?.(chat.repoId) ?? chat.repoId
    };
  }
  const workspaceRoot = stringValue(chat.raw.workspace_root);
  if (workspaceRoot && workspaceRoot !== '.') {
    const base = workspacePathBasename(workspaceRoot);
    return {
      kindKey: 'hub',
      kindLabel: 'Hub',
      detail: base,
      detailFull: workspaceRoot
    };
  }
  return { kindKey: 'local', kindLabel: 'Local', detail: 'Hub workspace' };
}

/** One-line scope for the active chat header (hub workspace vs repo naming). */
export function pmaChatHeaderScopeLine(
  chat: PmaChatSummary | null,
  repoLabel?: (repoId: string) => string | null
): string {
  if (!chat) return '';
  if (chat.worktreeId) {
    const repoId = chat.repoId ?? '';
    const repoName = repoId ? repoLabel?.(repoId) ?? repoId : '';
    const branch = repoName ? `${repoName} - ${chat.worktreeId}` : chat.worktreeId;
    return `Repo - ${branch}`;
  }
  if (chat.repoId) {
    const repoName = repoLabel?.(chat.repoId) ?? chat.repoId;
    return `Repo - ${repoName}`;
  }
  return 'Hub workspace';
}

export function buildManagedThreadMessagePayload(
  message: string,
  model: string,
  isRunning: boolean,
  attachments: Array<PendingAttachment | DocumentFileIntentPayload> = [],
  reasoning = '',
  profile = '',
  busyPolicy: 'queue' | 'interrupt' | null = isRunning ? 'queue' : null
): ManagedThreadMessagePayload {
  const trimmed = profile.trim();
  return {
    message,
    attachments: attachments.length ? attachments.map(pendingAttachmentToIntent) : undefined,
    model: model || undefined,
    reasoning: reasoning || undefined,
    ...(trimmed ? { profile: trimmed } : {}),
    busy_policy: busyPolicy ?? undefined,
    defer_execution: true,
    wait_for_confirmation: false
  };
}

export function modelReasoningOptions(model: Record<string, unknown> | null): string[] {
  if (!model) return [];
  if (model.supports_reasoning === false || model.supportsReasoning === false) return [];
  const rawOptions = model.reasoning_options ?? model.reasoningOptions ?? model.supportedReasoningEfforts;
  const options = Array.isArray(rawOptions)
    ? rawOptions.filter((option): option is string => typeof option === 'string' && option.trim().length > 0).map((option) => option.trim())
    : [];
  if (options.length > 0) return Array.from(new Set(options));
  return [];
}

export function modelSelectorState(
  loading: boolean,
  errorMessage: string | null,
  modelCount: number
): ModelSelectorState {
  if (loading) {
    return { state: 'loading', label: 'loading', disabled: true };
  }
  if (errorMessage) {
    return { state: 'error', label: errorMessage, disabled: true };
  }
  if (modelCount === 0) {
    return { state: 'empty', label: 'no models', disabled: true };
  }
  return { state: 'loaded', label: 'model', disabled: false };
}

export function artifactCardView(artifact: SurfaceArtifact): ArtifactCardView {
  switch (artifact.kind) {
    case 'screenshot':
      return {
        label: 'Screenshot',
        tone: 'media',
        primaryAction: artifact.url ? 'Open screenshot' : null,
        preview: artifact.url ? 'image' : 'text',
        detailLabel: 'Screenshot details'
      };
    case 'image':
      return {
        label: 'Image',
        tone: 'media',
        primaryAction: artifact.url ? 'Open image' : null,
        preview: artifact.url ? 'image' : 'text',
        detailLabel: 'Image details'
      };
    case 'file':
      return {
        label: 'File',
        tone: 'neutral',
        primaryAction: artifact.url ? 'Open file' : null,
        preview: 'file',
        detailLabel: 'File details'
      };
    case 'preview_url':
      return {
        label: 'Preview URL',
        tone: 'link',
        primaryAction: artifact.url ? 'Open preview' : null,
        preview: 'link',
        detailLabel: 'Preview details'
      };
    case 'test_result':
      return {
        label: 'Test result',
        tone: artifact.summary?.toLowerCase().includes('fail') ? 'danger' : 'success',
        primaryAction: artifact.url ? 'Open test output' : null,
        preview: 'text',
        detailLabel: 'Test details'
      };
    case 'command_summary':
      return {
        label: 'Command summary',
        tone: 'neutral',
        primaryAction: artifact.url ? 'Open command output' : null,
        preview: 'text',
        detailLabel: 'Command details'
      };
    case 'diff_summary':
      return {
        label: 'Diff summary',
        tone: 'warning',
        primaryAction: artifact.url ? 'Open diff' : null,
        preview: 'text',
        detailLabel: 'Diff details'
      };
    case 'link':
      return {
        label: 'PR / link',
        tone: 'link',
        primaryAction: artifact.url ? 'Open link' : null,
        preview: 'link',
        detailLabel: 'Link details'
      };
    case 'final_report':
      return {
        label: 'Final report',
        tone: 'success',
        primaryAction: artifact.url ? 'Open report' : null,
        preview: 'text',
        detailLabel: 'Report details'
      };
    case 'error':
      return {
        label: 'Error / blocker',
        tone: 'danger',
        primaryAction: artifact.url ? 'Open details' : null,
        preview: 'text',
        detailLabel: 'Blocker details'
      };
    case 'progress':
      return {
        label: 'Run event',
        tone: 'neutral',
        primaryAction: artifact.url ? 'Open event' : null,
        preview: 'text',
        detailLabel: 'Event details'
      };
  }
}

function clampPercent(value: number): number {
  return Math.max(0, Math.min(100, Math.round(value)));
}

function stringValue(value: unknown): string {
  if (typeof value === 'string' && value.trim()) return value;
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  return '';
}
