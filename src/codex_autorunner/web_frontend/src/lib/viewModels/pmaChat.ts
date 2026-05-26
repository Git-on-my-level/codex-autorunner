import type {
  ChatFacetTransport,
  ChatIndexFacets,
  TicketRunGroup
} from '$lib/api/readModelContracts';
import type {
  PmaChatMessage,
  PmaMessageCapsuleRef,
  PmaChatSummary,
  PmaRunProgress,
  PmaTimelineItem,
  RepoSummary,
  SurfaceArtifact,
  WorktreeSummary,
  WorkStatus
} from './domain';
import { mapPmaMessageCapsuleRef, normalizeOptionalWorkStatus, pmaChatArchivedFromRawSignals, pmaLifecycleTokenIsActive, pmaLifecycleTokenIsArchived, pmaTimelineContractFields } from './domain';
import { isChatUnread } from './unread';

/** Status chips (All / Waiting / …) on the chat list. */
export type ChatStatusFilter = 'all' | 'active' | 'waiting' | 'unread' | 'archived';

/** Full list filter: status chips, grouped ticket runs, automation chats, or `surface:<slug>` messenger filters. */
export type ChatFilter = ChatStatusFilter | 'ticket_runs' | 'automation' | `surface:${string}`;

/** Token for the chats sidebar filter that lists only ticket-flow run groups (collapsed headers). */
export const CHAT_TICKET_RUNS_FILTER = 'ticket_runs' as const satisfies ChatFilter;

export const CHAT_EXTERNAL_TRANSPORT_FILTERS = ['discord', 'telegram', 'notification'] as const satisfies readonly ChatFacetTransport[];

/** Synthetic list selection id for pinned PMA Memory in the chats sidebar. */
export const CHAT_MEMORY_LIST_ID = '__memory__';

export const CHAT_FILTER_ORDER: ChatStatusFilter[] = ['all', 'waiting', 'active', 'unread', 'archived'];

const TOOL_PROGRESS_KINDS = new Set(['tool', 'tool_call', 'tool_result', 'function_call']);
const MAX_COMPACT_ACTIVITY_SOURCE_IDS = 200;
const MAX_MERGED_INTERMEDIATE_TEXT_CHARS = 4000;

function rawString(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null;
}

function messengerSurfaceLabel(slug: string): string {
  const map: Record<string, string> = {
    discord: 'Discord',
    telegram: 'Telegram',
    slack: 'Slack',
    mattermost: 'Mattermost',
    msteams: 'Microsoft Teams',
    teams: 'Teams',
    notification: 'Notifications',
    notifications: 'Notifications'
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

export function pmaChatFacets(chat: PmaChatSummary | null): ChatIndexFacets | null {
  const rawFacets = chat?.raw.facets;
  if (!rawFacets || typeof rawFacets !== 'object' || Array.isArray(rawFacets)) return null;
  const facets = rawFacets as Partial<ChatIndexFacets>;
  if (!['regular', 'ticket_run', 'automation', 'system'].includes(String(facets.category))) return null;
  return {
    category: facets.category as ChatIndexFacets['category'],
    turnKinds: Array.isArray(facets.turnKinds) ? facets.turnKinds : [],
    originKinds: Array.isArray(facets.originKinds) ? facets.originKinds : [],
    transports: Array.isArray(facets.transports) ? facets.transports : [],
    scopeKind: facets.scopeKind ?? null,
    scopeId: facets.scopeId ?? null,
    agentKind: facets.agentKind ?? null
  };
}

export function chatTransportLabel(transport: ChatFacetTransport): string {
  return transport === 'pma' ? 'PMA' : messengerSurfaceLabel(transport);
}

export function chatCategoryLabel(category: ChatIndexFacets['category']): string {
  const map: Record<ChatIndexFacets['category'], string> = {
    regular: 'Chats',
    ticket_run: 'Ticket Runs',
    automation: 'Automation',
    system: 'System'
  };
  return map[category];
}

export function pmaChatTransportBadges(
  chat: PmaChatSummary | null
): { slug: ChatFacetTransport; label: string; badgeClass: string }[] {
  const facets = pmaChatFacets(chat);
  if (!facets) return [];
  return facets.transports
    .filter((transport): transport is Exclude<ChatFacetTransport, 'pma'> => transport !== 'pma')
    .map((transport) => ({
      slug: transport,
      label: chatTransportLabel(transport),
      badgeClass: messengerBadgeClass(transport)
    }));
}

/** True when the chat is owned by the project manager agent, not merely PMA-surface reachable. */
export function showPmaAgentBadge(chat: PmaChatSummary | null): boolean {
  if (!chat) return false;
  const facets = pmaChatFacets(chat);
  if (facets?.agentKind === 'coding_agent') return false;
  if (facets?.agentKind === 'pma') return true;
  if (pmaChatKind(chat) === 'coding_agent') return false;
  if (chat.chatKind === 'pma') return true;
  const rawKind = stringValue(chat.raw.chat_kind ?? chat.raw.thread_kind);
  return rawKind === 'pma';
}

/** Badge + filter slug for chats bound to a backend-declared transport facet. */
export function chatMessengerSurface(
  chat: PmaChatSummary | null
): { slug: string; label: string; badgeClass: string } | null {
  const transport = pmaChatFacets(chat)?.transports.find((value) => value !== 'pma') ?? null;
  return transport ? { slug: transport, label: messengerSurfaceLabel(transport), badgeClass: messengerBadgeClass(transport) } : null;
}

export function chatSurfaceFilterToken(slug: string): ChatFilter {
  return `surface:${slug}`;
}

export function isChatSurfaceFilter(filter: ChatFilter): filter is `surface:${string}` {
  return filter.startsWith('surface:');
}

export function chatSurfaceFilterOptions(
  chats: PmaChatSummary[]
): { slug: string; label: string; count: number }[] {
  const counts = new Map<string, { label: string; count: number }>();
  for (const chat of chats) {
    if (isPmaChatArchived(chat)) continue;
    const surf = chatMessengerSurface(chat);
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

export type ChatTranscriptCard =
  | { kind: 'message'; id: string; message: PmaChatMessage; turnId: string | null; orderKey: string; timestamp: string | null }
  | {
      kind: 'intermediate';
      id: string;
      title: string;
      text: string;
      eventIds: string[];
      /** Backend `progress_item.event_ids` accumulated on this live card; used to dedupe against the timeline without hiding partial catch-up. */
      progressSourceIds: string[];
      detail: string | null;
      turnId: string | null;
      orderKey: string;
      timestamp: string | null;
    }
  | { kind: 'tool_group'; id: string; tools: ChatToolCallCard[]; turnId: string | null; orderKey: string; timestamp: string | null }
  | { kind: 'turn_summary'; id: string; title: string; cards: ChatTranscriptCard[]; turnId: string | null; orderKey: string; timestamp: string | null }
  | { kind: 'approval'; id: string; title: string; summary: string; detail: string | null; turnId: string | null; orderKey: string; timestamp: string | null }
  | { kind: 'lifecycle'; id: string; title: string; text: string; detail: string | null; turnId: string | null; orderKey: string; timestamp: string | null }
  | { kind: 'ticket'; id: string; title: string; summary: string | null; ticketId: string }
  | { kind: 'artifact'; id: string; artifact: SurfaceArtifact };

export type ChatToolCallCard = {
  id: string;
  title: string;
  summary: string | null;
  detail: string | null;
  state: 'started' | 'completed' | 'failed' | 'unknown';
  eventIds: string[];
  source?: SurfaceArtifact;
};

export type ChatTranscriptSnapshot = {
  rows: ChatTranscriptCard[];
  status: PmaRunProgress | null;
  raw: Record<string, unknown>;
};

type ChatActivitySummaryCard = Extract<ChatTranscriptCard, { kind: 'intermediate' | 'tool_group' | 'approval' }>;

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
  elapsedValue: string | null;
  queueDepth: number;
  queueDepthLabel: string;
  tokenUsageLabel: string | null;
  totalTokensFull: string | null;
  totalTokensCompact: string | null;
  inputTokensFull: string | null;
  inputTokensCompact: string | null;
  outputTokensFull: string | null;
  outputTokensCompact: string | null;
  contextRemainingLabel: string | null;
  contextRemainingPercent: number | null;
};

export type ManagedThreadCreatePayload = {
  agent?: string;
  chat_kind?: PmaChatKind;
  model?: string;
  profile?: string;
  name: string;
  scope_urn: string;
};

export type PmaChatScopeSource =
  | 'default_hub'
  | 'route_explicit'
  | 'picker_explicit'
  | 'inherited_continuation';

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

type ChatSurfaceOwner = {
  repo_id?: unknown;
  resource_kind?: unknown;
  resource_id?: unknown;
  workspace_root?: unknown;
  scope_urn?: unknown;
};

type ChatSurfaceDisplay = {
  display_name?: unknown;
  title?: unknown;
  description?: unknown;
};

function rawRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function rawNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function firstRawString(...values: unknown[]): string | null {
  for (const value of values) {
    const text = rawString(value);
    if (text) return text;
  }
  return null;
}

function surfaceTitle(kind: string, key: string, display: ChatSurfaceDisplay): string {
  return firstRawString(display.display_name, display.title) ?? `${kind}:${key}`;
}

/** Convert one generic chat-surface projection row into the existing chat-list view shape. */
export function mapChatSurfaceToPmaChatSummary(surface: Record<string, unknown>): PmaChatSummary | null {
  const surfaceKind = firstRawString(surface.surface_kind);
  const surfaceKey = firstRawString(surface.surface_key);
  if (!surfaceKind || !surfaceKey) return null;
  const owner = rawRecord(surface.resource_owner) as ChatSurfaceOwner;
  const display = rawRecord(surface.display) as ChatSurfaceDisplay;
  const metadata = rawRecord(surface.metadata);
  const managedThreadId = firstRawString(surface.managed_thread_id);
  if (!managedThreadId) return null;
  const facts = Array.isArray(surface.facts) ? surface.facts : [];
  if (!facts.some((fact) => fact === 'managed_thread')) {
    return null;
  }
  const lifecycle = firstRawString(surface.lifecycle);
  const lcField = firstRawString(surface.lifecycle_status);
  const lifecycleStatus =
    pmaLifecycleTokenIsArchived(lcField) || pmaLifecycleTokenIsArchived(lifecycle) ? 'archived' : (lcField ?? 'active');
  const id = managedThreadId;
  const resourceKind = firstRawString(owner.resource_kind);
  const resourceId = firstRawString(owner.resource_id);
  const repoId = firstRawString(owner.repo_id) ?? (resourceKind === 'repo' ? resourceId : null);
  const worktreeId = resourceKind === 'worktree' ? resourceId : null;
  const bindingKind = firstRawString(metadata.binding_kind) ?? surfaceKind;
  const bindingId = firstRawString(metadata.binding_id) ?? surfaceKey;
  const statusSource =
    lifecycle ??
    metadata.runtime_status ??
    metadata.target_runtime_status ??
    metadata.latest_execution_status ??
    metadata.latest_event_status;

  return {
    id,
    title: surfaceTitle(surfaceKind, surfaceKey, display),
    lifecycleStatus,
    status: normalizeOptionalWorkStatus(statusSource) ?? 'idle',
    agentId: firstRawString(metadata.agent_id),
    agentProfile: firstRawString(metadata.agent_profile),
    model: firstRawString(metadata.model),
    repoId,
    worktreeId,
    ticketId: firstRawString(metadata.ticket_id),
    ticketDone: null,
    ticketPath: null,
    runId: firstRawString(metadata.run_id),
    unreadCount: rawNumber(metadata.unread_count ?? metadata.unreadCount ?? surface.unread_count ?? surface.unreadCount),
    flowType: firstRawString(metadata.flow_type),
    isTicketFlow: firstRawString(metadata.flow_type) === 'ticket' || firstRawString(metadata.ticket_id) !== null,
    progressPercent: rawNumber(metadata.progress_percent),
    updatedAt: firstRawString(
      metadata.last_sort_activity_at,
      metadata.last_activity_at,
      metadata.latest_turn_finished_at,
      metadata.latest_turn_started_at,
      metadata.latest_event_at,
      surface.last_sort_activity_at,
      surface.last_activity_at,
      surface.updated_at,
      surface.created_at
    ),
    raw: {
      ...surface,
      surface_kind: surfaceKind,
      surface_key: surfaceKey,
      binding_kind: bindingKind,
      binding_id: bindingId,
      managed_thread_id: managedThreadId,
      lifecycle_status: lifecycleStatus,
      repo_id: repoId,
      resource_kind: resourceKind,
      resource_id: resourceId,
      workspace_root: firstRawString(owner.workspace_root),
      scope_urn: firstRawString(owner.scope_urn),
      display_name: firstRawString(display.display_name, display.title),
      name: firstRawString(display.display_name, display.title),
      chat_kind: firstRawString(metadata.chat_kind),
      thread_kind: firstRawString(metadata.thread_kind),
      normalized_status: statusSource,
      status: statusSource,
      unread_count: rawNumber(metadata.unread_count ?? metadata.unreadCount ?? surface.unread_count ?? surface.unreadCount)
    }
  };
}

/**
 * Legacy diagnostics/test helper for `/hub/chat/events` snapshots.
 * Production chat-list rows come from the typed chat-index read model.
 */
export function mapChatSurfaceSnapshotToPmaChats(payload: Record<string, unknown>): PmaChatSummary[] {
  const surfaces = Array.isArray(payload.surfaces) ? payload.surfaces : [];
  const mapped = surfaces
    .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object' && !Array.isArray(item)))
    .map((item) => mapChatSurfaceToPmaChatSummary(item))
    .filter((chat): chat is PmaChatSummary => chat !== null);
  const byId = new Map<string, PmaChatSummary>();
  for (const chat of mapped) {
    const existing = byId.get(chat.id);
    byId.set(chat.id, existing ? mergeDuplicateChatSurfaceRows(existing, chat) : chat);
  }
  return [...byId.values()];
}

function mergeDuplicateChatSurfaceRows(left: PmaChatSummary, right: PmaChatSummary): PmaChatSummary {
  const leftKind = rawString(left.raw.surface_kind);
  const rightKind = rawString(right.raw.surface_kind);
  const primary = leftKind === 'pma' ? left : rightKind === 'pma' ? right : left;
  const secondary = primary === left ? right : left;
  const bindingKind =
    (rawString(primary.raw.binding_kind) !== 'pma' ? rawString(primary.raw.binding_kind) : null) ??
    (rawString(secondary.raw.binding_kind) !== 'pma' ? rawString(secondary.raw.binding_kind) : null);
  const bindingId =
    bindingKind === rawString(primary.raw.binding_kind) ? rawString(primary.raw.binding_id) : rawString(secondary.raw.binding_id);
  return {
    ...secondary,
    ...primary,
    raw: {
      ...secondary.raw,
      ...primary.raw,
      binding_kind: bindingKind ?? primary.raw.binding_kind,
      binding_id: bindingId ?? primary.raw.binding_id
    }
  };
}

export function mapChatSurfaceEventToPmaChatSummary(payload: Record<string, unknown>): PmaChatSummary | null {
  const surface = rawRecord(payload.surface);
  const details = rawRecord(payload.details);
  const channel = rawRecord(details.channel);
  const threadDetail = rawRecord(details.thread);
  const surfaceKind = firstRawString(surface.surface_kind);
  const surfaceKey = firstRawString(surface.surface_key);
  if (!surfaceKind || !surfaceKey) return null;
  const metadata: Record<string, unknown> = {
    latest_event_type: payload.event_type,
    latest_event_status: payload.status
  };
  const eventAgentId = firstRawString(threadDetail.agent_id);
  if (eventAgentId) metadata.agent_id = eventAgentId;
  const eventProfile = firstRawString(threadDetail.agent_profile);
  if (eventProfile) metadata.agent_profile = eventProfile;
  const eventModel = firstRawString(threadDetail.model);
  if (eventModel) metadata.model = eventModel;
  return mapChatSurfaceToPmaChatSummary({
    surface_kind: surfaceKind,
    surface_key: surfaceKey,
    facts: ['managed_thread'],
    managed_thread_id: payload.managed_thread_id,
    lifecycle: payload.lifecycle,
    lifecycle_status: payload.lifecycle_status,
    resource_owner: payload.resource_owner,
    display: {
      display_name: channel.display
    },
    created_at: payload.created_at ?? payload.occurred_at,
    updated_at: payload.occurred_at ?? payload.created_at,
    metadata
  });
}

/** Legacy diagnostics/test helper; do not use as a production chat-index writer. */
export function pmaChatBindingKey(chat: PmaChatSummary | null): string | null {
  if (!chat) return null;
  const raw = chat.raw as Record<string, unknown>;
  const kind = typeof raw.binding_kind === 'string' ? raw.binding_kind.trim() : '';
  const id = typeof raw.binding_id === 'string' ? raw.binding_id.trim() : '';
  if (kind && id) return `${kind}:${id}`;
  const surfaceKind = typeof raw.surface_kind === 'string' ? raw.surface_kind.trim() : '';
  const surfaceKey = typeof raw.surface_key === 'string' ? raw.surface_key.trim() : '';
  if (surfaceKind && surfaceKey) return `${surfaceKind}:${surfaceKey}`;
  const primarySurface = raw.primary_surface;
  if (primarySurface && typeof primarySurface === 'object' && !Array.isArray(primarySurface)) {
    const primary = primarySurface as Record<string, unknown>;
    const primaryKind = typeof primary.surface_kind === 'string' ? primary.surface_kind.trim() : '';
    const primaryKey = typeof primary.surface_key === 'string' ? primary.surface_key.trim() : '';
    if (primaryKind && primaryKey) return `${primaryKind}:${primaryKey}`;
  }
  return null;
}

/** Legacy diagnostics/test helper; production list updates use chat.index.patch. */
export function reconcileChatSurfaceEvent(
  currentChats: PmaChatSummary[],
  eventPayload: Record<string, unknown>
): PmaChatSummary[] {
  const eventChat = mapChatSurfaceEventToPmaChatSummary(eventPayload);
  if (!eventChat) return currentChats;
  const eventBinding = pmaChatBindingKey(eventChat);
  let found = false;
  const nextChats = currentChats.map((chat) => {
    if (chat.id !== eventChat.id && (!eventBinding || pmaChatBindingKey(chat) !== eventBinding)) return chat;
    found = true;
    return {
      ...chat,
      ...eventChat,
      agentId: eventChat.agentId ?? chat.agentId,
      agentProfile: eventChat.agentProfile ?? chat.agentProfile,
      model: eventChat.model ?? chat.model,
      title: isProtocolIdTitle(eventChat.title) ? chat.title : eventChat.title,
      raw: {
        ...chat.raw,
        ...eventChat.raw
      }
    };
  });
  if (!found) nextChats.push(eventChat);
  return nextChats;
}

function isProtocolIdTitle(title: string): boolean {
  return /^(discord|telegram):\S+$/i.test(title.trim());
}

/** Legacy diagnostics/test helper; production list repair uses chat.index.snapshot. */
export function reconcileChatSurfaceSnapshot(
  currentChats: PmaChatSummary[],
  nextChats: PmaChatSummary[],
  activeChatId: string | null
): { chats: PmaChatSummary[]; replacementChatId: string | null } {
  if (!activeChatId) return { chats: nextChats, replacementChatId: null };
  const priorActive = currentChats.find((chat) => chat.id === activeChatId) ?? null;
  const priorBinding = pmaChatBindingKey(priorActive);
  const nextActive = nextChats.find((chat) => chat.id === activeChatId) ?? null;
  if (nextActive && !isPmaChatArchived(nextActive)) return { chats: nextChats, replacementChatId: null };
  if (!priorBinding) return { chats: nextChats, replacementChatId: null };
  const replacement = nextChats.find(
    (chat) => chat.id !== activeChatId && pmaChatBindingKey(chat) === priorBinding && !isPmaChatArchived(chat)
  );
  return { chats: nextChats, replacementChatId: replacement?.id ?? null };
}

export function committedDraftChatPlaceholder(
  draftChat: PmaChatSummary,
  committedChatId: string,
  updatedAt = new Date().toISOString()
): PmaChatSummary {
  return {
    ...draftChat,
    id: committedChatId,
    lifecycleStatus: 'active',
    status: 'running',
    updatedAt,
    raw: {
      ...draftChat.raw,
      draft: false,
      draft_committed_placeholder: true,
      previous_draft_id: draftChat.id,
      id: committedChatId,
      managed_thread_id: committedChatId
    }
  };
}

export function isLocalChatPlaceholder(chat: PmaChatSummary): boolean {
  return chat.lifecycleStatus === 'draft' || chat.raw.draft === true || chat.raw.draft_committed_placeholder === true;
}

export function mergeLocalChatPlaceholders(
  persistedChats: PmaChatSummary[],
  placeholders: Array<PmaChatSummary | null | undefined>
): PmaChatSummary[] {
  const visiblePlaceholders = visibleLocalChatPlaceholders(persistedChats, placeholders);
  return visiblePlaceholders.length > 0 ? [...visiblePlaceholders, ...persistedChats] : persistedChats;
}

export function visibleLocalChatPlaceholders(
  persistedChats: PmaChatSummary[],
  placeholders: Array<PmaChatSummary | null | undefined>
): PmaChatSummary[] {
  return placeholders
    .filter((chat): chat is PmaChatSummary => Boolean(chat))
    .filter((chat) => !persistedChats.some((row) => row.id === chat.id));
}

export function mergeChatFacetSourceChats(
  baseChats: PmaChatSummary[],
  currentChats: PmaChatSummary[],
  placeholders: Array<PmaChatSummary | null | undefined> = []
): PmaChatSummary[] {
  const byId = new Map<string, PmaChatSummary>();
  for (const chat of baseChats) byId.set(chat.id, chat);
  for (const chat of currentChats) byId.set(chat.id, chat);
  return mergeLocalChatPlaceholders([...byId.values()], placeholders);
}

export type ManagedThreadMessagePayload = {
  message: string;
  attachments?: DocumentFileIntentPayload[];
  model?: string;
  reasoning?: string;
  profile?: string;
  client_turn_id?: string;
  busy_policy?: 'queue' | 'interrupt' | 'reject';
  defer_execution?: boolean;
  wait_for_confirmation?: boolean;
};

export type ManagedThreadStartMessagePayload = ManagedThreadCreatePayload &
  ManagedThreadMessagePayload & {
    origin: 'web';
    scope_source: PmaChatScopeSource;
  };

const activeStatuses: WorkStatus[] = ['running'];
const waitingStatuses: WorkStatus[] = ['waiting', 'blocked'];

export function isPmaChatArchived(chat: PmaChatSummary): boolean {
  if (pmaLifecycleTokenIsArchived(chat.lifecycleStatus)) return true;
  if (pmaLifecycleTokenIsActive(chat.lifecycleStatus)) return false;
  return pmaChatArchivedFromRawSignals(chat.raw);
}

export function filterPmaChats(
  chats: PmaChatSummary[],
  filter: ChatFilter,
  query: string,
  lastSeen: Record<string, string> = {}
): PmaChatSummary[] {
  const needle = query.trim().toLowerCase();
  return chats
    .filter((chat) => {
      const archived = isPmaChatArchived(chat);
      if (filter === 'archived') return archived;
      if (archived) return false;
      if (isChatSurfaceFilter(filter)) {
        const slug = filter.slice('surface:'.length);
        return chatMessengerSurface(chat)?.slug === slug;
      }
      if (filter === 'ticket_runs') return chatRunGroupKey(chat) !== null;
      if (filter === 'automation') return pmaChatIsAutomation(chat);
      if (filter === 'active') return activeStatuses.includes(chat.status);
      if (filter === 'waiting') return waitingStatuses.includes(chat.status);
      if (filter === 'unread') {
        return isUnread(chat, lastSeen);
      }
      return true;
    })
    .filter((chat) => {
      if (!needle) return true;
      const surfaceLabel = chatMessengerSurface(chat)?.label;
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

/** Unread chats first, then most recently updated. Waiting/running only break ties. */
export function sortChatsUnreadFirst(
  chats: PmaChatSummary[],
  lastSeen: Record<string, string> = {}
): PmaChatSummary[] {
  const statusRank = (status: WorkStatus) =>
    status === 'waiting' || status === 'blocked' ? 0 : status === 'running' ? 1 : 2;
  return [...chats].sort((left, right) => {
    const placeholderDiff = Number(isLocalChatPlaceholder(right)) - Number(isLocalChatPlaceholder(left));
    if (placeholderDiff !== 0) return placeholderDiff;
    const unreadDiff = Number(isUnread(right, lastSeen)) - Number(isUnread(left, lastSeen));
    if (unreadDiff !== 0) return unreadDiff;
    const leftTime = Date.parse(left.updatedAt ?? '') || 0;
    const rightTime = Date.parse(right.updatedAt ?? '') || 0;
    const timeDiff = rightTime - leftTime;
    if (timeDiff !== 0) return timeDiff;
    const rankDiff = statusRank(left.status) - statusRank(right.status);
    if (rankDiff !== 0) return rankDiff;
    return left.id.localeCompare(right.id);
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
): Record<ChatStatusFilter, number> {
  const activeChats = chats.filter((chat) => !isPmaChatArchived(chat));
  return {
    all: activeChats.length,
    active: activeChats.filter((chat) => activeStatuses.includes(chat.status)).length,
    waiting: activeChats.filter((chat) => waitingStatuses.includes(chat.status)).length,
    unread: activeChats.filter((chat) => isUnread(chat, lastSeen)).length,
    archived: chats.filter(isPmaChatArchived).length
  };
}

export function pmaChatIsAutomation(chat: PmaChatSummary | null): boolean {
  return pmaChatFacets(chat)?.category === 'automation';
}

export function adjustedUnreadFilterCount(
  serverUnread: number,
  knownChats: PmaChatSummary[],
  lastSeen: Record<string, string> = {}
): number {
  let knownServerUnread = 0;
  let knownEffectiveUnread = 0;
  for (const chat of knownChats) {
    if (isPmaChatArchived(chat) || typeof chat.unreadCount !== 'number' || chat.unreadCount <= 0) continue;
    knownServerUnread += 1;
    if (isUnread(chat, lastSeen)) knownEffectiveUnread += 1;
  }
  return Math.max(0, serverUnread - knownServerUnread + knownEffectiveUnread);
}

export function summarizeVisibleLocalPlaceholderStatusCounts(
  persistedChats: PmaChatSummary[],
  placeholders: Array<PmaChatSummary | null | undefined>
): Pick<Record<ChatStatusFilter, number>, 'active' | 'waiting'> {
  const visible = visibleLocalChatPlaceholders(persistedChats, placeholders);
  return {
    active: visible.filter((chat) => activeStatuses.includes(chat.status)).length,
    waiting: visible.filter((chat) => waitingStatuses.includes(chat.status)).length
  };
}

/**
 * A "run group" key for ticket-flow chats. Ticket-flow chats sharing the same
 * worktree (or repo, for repo-scoped flows) belong to the same operator-visible
 * run; they collapse into a single row on chat lists.
 */
export type ChatRunGroup = {
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
  aggregateSource?: 'backend' | 'legacy';
};

export type ChatListEntry =
  | { kind: 'group'; group: ChatRunGroup }
  | { kind: 'chat'; chat: PmaChatSummary };

export function chatRunGroupSummaryParts(group: ChatRunGroup): string[] {
  const parts: string[] = [];
  if (group.waitingCount > 0) parts.push(`${group.waitingCount} waiting`);
  if (group.activeCount > 0) parts.push(`${group.activeCount} active`);
  if (group.failedCount > 0) parts.push(`${group.failedCount} failed`);
  parts.push(`${group.doneCount}/${group.totalCount} done`);
  return parts;
}

export function chatRunGroupKey(chat: PmaChatSummary): string | null {
  if (pmaChatFacets(chat)?.category !== 'ticket_run') return null;
  const runSuffix = chat.runId ? `:run:${chat.runId}` : '';
  if (chat.worktreeId) return `worktree:${chat.worktreeId}${runSuffix}`;
  if (chat.repoId) return `repo:${chat.repoId}${runSuffix}`;
  return null;
}

/** Number of distinct repo/worktree ticket-flow runs (same cardinality as run-group rows). */
export function countTicketRunGroups(chats: PmaChatSummary[]): number {
  const keys = new Set<string>();
  for (const chat of chats) {
    if (isPmaChatArchived(chat)) continue;
    const key = chatRunGroupKey(chat);
    if (key) keys.add(key);
  }
  return keys.size;
}

export function countSemanticTicketRunGroups(groups: TicketRunGroup[], chats: PmaChatSummary[] = []): number {
  void chats;
  return groups.length;
}

function isUnread(chat: PmaChatSummary, lastSeen: Record<string, string>): boolean {
  if (typeof chat.unreadCount === 'number' && chat.unreadCount <= 0) return false;
  if (!isChatUnread(chat, lastSeen)) return false;
  return typeof chat.unreadCount === 'number' ? chat.unreadCount > 0 : true;
}

function rollupGroupStatus(group: ChatRunGroup): WorkStatus {
  if (group.waitingCount > 0) return 'waiting';
  if (group.activeCount > 0) return 'running';
  if (group.failedCount > 0) return 'failed';
  if (group.totalCount > 0 && group.doneCount === group.totalCount) return 'done';
  return 'idle';
}

function chatCountsDoneForRun(chat: PmaChatSummary): boolean {
  if (chat.status === 'failed' || chat.status === 'invalid') return false;
  return chat.ticketDone === true || chat.ticketStatus === 'done';
}

export function buildChatListEntries(
  chats: PmaChatSummary[],
  options: {
    lastSeen?: Record<string, string>;
    repoLabel?: (repoId: string) => string | null;
    worktreeLabel?: (worktreeId: string) => string | null;
    groupRuns?: boolean;
  } = {}
): ChatListEntry[] {
  const lastSeen = options.lastSeen ?? {};
  if (options.groupRuns === false) {
    return sortChatsUnreadFirst(chats, lastSeen).map((chat) => ({ kind: 'chat', chat }) as ChatListEntry);
  }
  const groups = new Map<string, ChatRunGroup>();
  const standalone: PmaChatSummary[] = [];

  for (const chat of chats) {
    const key = chatRunGroupKey(chat);
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
    group.chats = sortChatsUnreadFirst(group.chats, lastSeen);
    group.totalCount = group.chats.length;
    const agentSet = new Set<string>();
    for (const chat of group.chats) {
      if (chat.agentId) agentSet.add(chat.agentId);
      if (isUnread(chat, lastSeen)) group.unreadCount += 1;
      if (chat.status === 'running') group.activeCount += 1;
      else if (chat.status === 'waiting' || chat.status === 'blocked') group.waitingCount += 1;
      else if (chatCountsDoneForRun(chat)) group.doneCount += 1;
      else if (chat.status === 'failed' || chat.status === 'invalid') group.failedCount += 1;
      if (chat.updatedAt && (!group.updatedAt || chat.updatedAt > group.updatedAt)) {
        group.updatedAt = chat.updatedAt;
      }
    }
    group.agents = [...agentSet].sort();
    group.status = rollupGroupStatus(group);
  }

  type Sortable = { entry: ChatListEntry; placeholderRank: number; unreadRank: number; statusRank: number; sort: string; id: string };
  const sortables: Sortable[] = [];
  for (const group of groups.values()) {
    sortables.push({
      entry: { kind: 'group', group },
      placeholderRank: 1,
      unreadRank: group.unreadCount > 0 ? 0 : 1,
      statusRank: group.waitingCount > 0 ? 0 : group.activeCount > 0 ? 1 : 2,
      sort: group.updatedAt ?? '',
      id: group.key
    });
  }
  for (const chat of standalone) {
    sortables.push({
      entry: { kind: 'chat', chat },
      placeholderRank: isLocalChatPlaceholder(chat) ? 0 : 1,
      unreadRank: isUnread(chat, lastSeen) ? 0 : 1,
      statusRank: chat.status === 'waiting' || chat.status === 'blocked' ? 0 : chat.status === 'running' ? 1 : 2,
      sort: chat.updatedAt ?? '',
      id: chat.id
    });
  }
  sortables.sort((a, b) => {
    if (a.placeholderRank !== b.placeholderRank) return a.placeholderRank - b.placeholderRank;
    if (a.unreadRank !== b.unreadRank) return a.unreadRank - b.unreadRank;
    const timeDiff = (b.sort || '').localeCompare(a.sort || '');
    if (timeDiff !== 0) return timeDiff;
    if (a.statusRank !== b.statusRank) return a.statusRank - b.statusRank;
    return a.id.localeCompare(b.id);
  });
  return sortables.map((item) => item.entry);
}

// Current `/chats` rendering uses backend TicketRunGroup rows for semantic run groups.
export function buildSemanticChatListEntries(
  chats: PmaChatSummary[],
  groups: TicketRunGroup[],
  options: {
    lastSeen?: Record<string, string>;
    repoLabel?: (repoId: string) => string | null;
    worktreeLabel?: (worktreeId: string) => string | null;
    groupRuns?: boolean;
  } = {}
): ChatListEntry[] {
  if (options.groupRuns === false) return buildChatListEntries(chats, { ...options, groupRuns: false });
  if (groups.length === 0) {
    return sortChatsUnreadFirst(chats, options.lastSeen ?? {}).map((chat) => ({ kind: 'chat', chat }));
  }

  const lastSeen = options.lastSeen ?? {};
  const chatsByGroup = new Map<string, PmaChatSummary[]>();
  const groupedIds = new Set<string>();
  for (const chat of chats) {
    const groupId = backendGroupIdForChat(chat);
    if (!groupId) continue;
    const bucket = chatsByGroup.get(groupId) ?? [];
    bucket.push(chat);
    chatsByGroup.set(groupId, bucket);
  }

  const entries: ChatListEntry[] = [];
  const seenGroups = new Set(groups.map((group) => group.groupId));
  for (const group of groups) {
    const children = sortChatsUnreadFirst(chatsByGroup.get(group.groupId) ?? [], lastSeen);
    for (const child of children) groupedIds.add(child.id);
    const labelLookup = group.scopeKind === 'worktree' ? options.worktreeLabel : options.repoLabel;
    const agents = [...new Set(children.map((chat) => chat.agentId).filter((agent): agent is string => Boolean(agent)))].sort();
    entries.push({
      kind: 'group',
      group: {
        key: group.groupId,
        scopeKind: group.scopeKind,
        scopeId: group.scopeId,
        scopeLabel: labelLookup?.(group.scopeId) ?? group.scopeId,
        chats: children,
        totalCount: group.totalCount,
        unreadCount: group.unreadCount,
        activeCount: group.runningCount,
        waitingCount: group.waitingCount,
        doneCount: group.doneCount,
        failedCount: group.failedCount,
        agents,
        status: group.status,
        updatedAt: group.updatedAt ?? null,
        aggregateSource: 'backend'
      }
    });
  }

  for (const chat of chats) {
    if (groupedIds.has(chat.id)) continue;
    const key = backendGroupIdForChat(chat);
    if (key && seenGroups.has(key)) continue;
    entries.push({ kind: 'chat', chat });
  }
  return sortChatListEntries(entries, lastSeen);
}

function backendGroupIdForChat(chat: PmaChatSummary): string | null {
  const row = chat.raw.row;
  if (row && typeof row === 'object' && !Array.isArray(row)) {
    const value = (row as Record<string, unknown>).groupId;
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  for (const value of [chat.raw.group_id, chat.raw.groupId]) {
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  return null;
}

function sortChatListEntries(entries: ChatListEntry[], lastSeen: Record<string, string>): ChatListEntry[] {
  return [...entries].sort((left, right) => {
    const leftChat = left.kind === 'chat' ? left.chat : null;
    const rightChat = right.kind === 'chat' ? right.chat : null;
    const placeholderDiff = Number(rightChat ? isLocalChatPlaceholder(rightChat) : false) - Number(leftChat ? isLocalChatPlaceholder(leftChat) : false);
    if (placeholderDiff !== 0) return placeholderDiff;
    const leftUnread = left.kind === 'group' ? left.group.unreadCount > 0 : isUnread(left.chat, lastSeen);
    const rightUnread = right.kind === 'group' ? right.group.unreadCount > 0 : isUnread(right.chat, lastSeen);
    const unreadDiff = Number(rightUnread) - Number(leftUnread);
    if (unreadDiff !== 0) return unreadDiff;
    const leftSort = left.kind === 'group' ? left.group.updatedAt ?? '' : left.chat.updatedAt ?? '';
    const rightSort = right.kind === 'group' ? right.group.updatedAt ?? '' : right.chat.updatedAt ?? '';
    const timeDiff = rightSort.localeCompare(leftSort);
    if (timeDiff !== 0) return timeDiff;
    const rank = (entry: ChatListEntry) => {
      const status = entry.kind === 'group' ? entry.group.status : entry.chat.status;
      return status === 'waiting' || status === 'blocked' ? 0 : status === 'running' ? 1 : 2;
    };
    const statusDiff = rank(left) - rank(right);
    if (statusDiff !== 0) return statusDiff;
    const leftId = left.kind === 'group' ? left.group.key : left.chat.id;
    const rightId = right.kind === 'group' ? right.group.key : right.chat.id;
    return leftId.localeCompare(rightId);
  });
}

/** Filter entries against a single PMA chat filter while preserving group structure. */
export function filterChatEntries(
  entries: ChatListEntry[],
  filter: ChatFilter,
  search: string,
  lastSeen: Record<string, string> = {}
): ChatListEntry[] {
  const out: ChatListEntry[] = [];
  for (const entry of entries) {
    if (entry.kind === 'chat') {
      const filtered = filterPmaChats([entry.chat], filter, search, lastSeen);
      if (filtered.length) out.push(entry);
      continue;
    }
    const matchedChats = filterPmaChats(entry.group.chats, filter, search, lastSeen);
    if (entry.group.aggregateSource === 'backend' && filter === 'ticket_runs' && matchedChats.length === 0) {
      const needle = search.trim().toLowerCase();
      if (!needle || [entry.group.key, entry.group.scopeId, entry.group.scopeLabel].some((value) => value.toLowerCase().includes(needle))) {
        out.push(entry);
      }
      continue;
    }
    if (!matchedChats.length) continue;
    if (matchedChats.length === entry.group.chats.length) {
      out.push(entry);
      continue;
    }
    if (entry.group.aggregateSource === 'backend') {
      out.push({ kind: 'group', group: { ...entry.group, chats: matchedChats } });
      continue;
    }
    // Rebuild a slimmer group containing only matched chats so counts stay honest.
    const trimmed: ChatRunGroup = {
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
      else if (chatCountsDoneForRun(chat)) trimmed.doneCount += 1;
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

export function buildChatTranscriptCards(
  timeline: PmaTimelineItem[],
  chat: PmaChatSummary | null,
  artifacts: SurfaceArtifact[]
): ChatTranscriptCard[] {
  const normalizedTimeline = suppressDuplicateTimelineDeliveries(timeline);
  const messageAttachmentKeys = collectMessageAttachmentKeys(normalizedTimeline);
  const timelineCards = normalizedTimeline
    .flatMap(timelineItemToCard)
    .filter((card) => !isMessageAttachmentArtifactCard(card, messageAttachmentKeys));
  const cards: ChatTranscriptCard[] = [...timelineCards];

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

function suppressDuplicateTimelineDeliveries(timeline: PmaTimelineItem[]): PmaTimelineItem[] {
  const seen = new Set<string>();
  const out: PmaTimelineItem[] = [];
  for (const item of timeline) {
    const key = canonicalTimelineIdentityKey(item);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(item);
  }
  return out;
}

function canonicalTimelineIdentityKey(item: PmaTimelineItem): string {
  return item.identity.timelineItemId;
}

function isMessageAttachmentArtifactCard(card: ChatTranscriptCard, messageAttachmentKeys: Set<string>): boolean {
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

export function mapChatTranscriptSnapshot(
  raw: Record<string, unknown>,
  mapProgress: (raw: Record<string, unknown>) => PmaRunProgress
): ChatTranscriptSnapshot {
  return {
    rows: asRecordArray(raw.rows).map(mapChatTranscriptRow).filter((row): row is ChatTranscriptCard => row !== null),
    status: raw.status && typeof raw.status === 'object' && !Array.isArray(raw.status)
      ? mapProgress(raw.status as Record<string, unknown>)
      : null,
    raw
  };
}

export function mapChatTranscriptRows(rawRows: unknown): ChatTranscriptCard[] {
  return asRecordArray(rawRows).map(mapChatTranscriptRow).filter((row): row is ChatTranscriptCard => row !== null);
}

export function compactChatTranscriptCards(cards: ChatTranscriptCard[]): ChatTranscriptCard[] {
  return summarizeTurnActivity(mergeIntermediateDeltas(foldAdjacentToolGroups(cards)));
}

function foldAdjacentToolGroups(cards: ChatTranscriptCard[]): ChatTranscriptCard[] {
  const out: ChatTranscriptCard[] = [];
  for (const card of cards) {
    const prev = out[out.length - 1];
    if (
      card.kind === 'tool_group' &&
      prev?.kind === 'tool_group' &&
      prev.turnId === card.turnId &&
      prev.turnId !== null
    ) {
      out[out.length - 1] = {
        ...prev,
        tools: [...prev.tools, ...card.tools],
        orderKey: prev.orderKey || card.orderKey,
        timestamp: prev.timestamp ?? card.timestamp
      };
      continue;
    }
    out.push(card);
  }
  return out;
}

function mergeIntermediateDeltas(cards: ChatTranscriptCard[]): ChatTranscriptCard[] {
  const out: ChatTranscriptCard[] = [];
  for (const card of cards) {
    const prev = out[out.length - 1];
    if (
      card.kind === 'intermediate' &&
      prev?.kind === 'intermediate' &&
      shouldAppendIntermediateDelta(prev, card)
    ) {
      out[out.length - 1] = {
        ...prev,
        title: mergedIntermediateTitle(prev, card),
        text: mergeIntermediateText(prev.text, card.text, MAX_MERGED_INTERMEDIATE_TEXT_CHARS),
        eventIds: appendCappedUniqueStrings(prev.eventIds, card.eventIds, MAX_COMPACT_ACTIVITY_SOURCE_IDS),
        progressSourceIds: appendCappedUniqueStrings(prev.progressSourceIds, card.progressSourceIds, MAX_COMPACT_ACTIVITY_SOURCE_IDS),
        detail: mergedTraceDetail(
          mergedIntermediateTitle(prev, card),
          appendCappedUniqueStrings(prev.eventIds, card.eventIds, MAX_COMPACT_ACTIVITY_SOURCE_IDS)
        ),
        orderKey: prev.orderKey || card.orderKey,
        timestamp: prev.timestamp ?? card.timestamp
      };
      continue;
    }
    out.push(card);
  }
  return out;
}

function summarizeTurnActivity(cards: ChatTranscriptCard[]): ChatTranscriptCard[] {
  const out: ChatTranscriptCard[] = [];
  let pending: ChatActivitySummaryCard[] = [];

  const flush = () => {
    if (!pending.length) return;
    out.push(...compactActivityRun(pending));
    pending = [];
  };

  for (const card of cards) {
    if (isSummaryActivityCard(card)) {
      const currentTurn = activitySummaryTurnId(card);
      const pendingTurn = pending.length ? activitySummaryTurnId(pending[0]) : currentTurn;
      if (pending.length && currentTurn !== pendingTurn) flush();
      pending.push(card);
      continue;
    }
    flush();
    out.push(card);
  }
  flush();
  return out;
}

function compactActivityRun(cards: ChatActivitySummaryCard[]): ChatTranscriptCard[] {
  if (cards.length === 1 && !activityCardNeedsSummary(cards[0])) return cards;
  const turnId = activitySummaryTurnId(cards[0]);
  return [{
    kind: 'turn_summary',
    id: `turn:${turnId ?? cards[0].id}:activity:${cards[0].id}`,
    title: activitySummaryTitle(cards),
    cards,
    turnId,
    orderKey: cards[0].orderKey,
    timestamp: cards[0].timestamp
  }];
}

function formatActivityCount(value: number): string {
  if (value < 1000) return String(value);
  const thousands = value / 1000;
  return `${thousands >= 10 ? String(Math.round(thousands)) : thousands.toFixed(1).replace(/\.0$/, '')}k`;
}

function activitySummaryTitle(cards: ChatActivitySummaryCard[]): string {
  // Count merged cards — one per reasoning step / tool call — rather than raw
  // source-event ids, so a turn reads as "5 reasoning steps" instead of an
  // alarming "31689 thinking updates".
  let toolCalls = 0;
  let reasoningSteps = 0;
  let approvals = 0;
  for (const card of cards) {
    if (card.kind === 'tool_group') {
      toolCalls += card.tools.length;
    } else if (card.kind === 'approval') {
      approvals += 1;
    } else if (card.kind === 'intermediate') {
      reasoningSteps += 1;
    }
  }
  const parts: string[] = [];
  if (reasoningSteps) {
    parts.push(`${formatActivityCount(reasoningSteps)} reasoning ${reasoningSteps === 1 ? 'step' : 'steps'}`);
  }
  if (toolCalls) {
    parts.push(`${formatActivityCount(toolCalls)} tool ${toolCalls === 1 ? 'call' : 'calls'}`);
  }
  if (approvals) {
    parts.push(`${formatActivityCount(approvals)} approval ${approvals === 1 ? 'request' : 'requests'}`);
  }
  return parts.length ? parts.join(' · ') : 'Activity details';
}

function activityCardNeedsSummary(card: ChatActivitySummaryCard): boolean {
  if (card.kind === 'tool_group') return card.tools.length > 1;
  if (card.kind === 'intermediate') return uniqueStrings([...card.eventIds, ...card.progressSourceIds]).length > 1;
  return false;
}

function isSummaryActivityCard(card: ChatTranscriptCard): card is ChatActivitySummaryCard {
  if (card.kind === 'tool_group' || card.kind === 'approval') return true;
  if (card.kind !== 'intermediate') return false;
  return !isCommentaryTraceCard(card);
}

function activitySummaryTurnId(card: ChatActivitySummaryCard): string | null {
  return card.turnId;
}

function shouldAppendIntermediateDelta(
  left: Extract<ChatTranscriptCard, { kind: 'intermediate' }>,
  right: Extract<ChatTranscriptCard, { kind: 'intermediate' }>
): boolean {
  if (left.turnId !== right.turnId) return false;
  if (isCommentaryTraceCard(left) || isCommentaryTraceCard(right)) return false;
  if (isTerminalTraceCard(left) || isTerminalTraceCard(right)) return false;
  if (isThinkingTraceTitle(left.title) && isThinkingTraceTitle(right.title)) return true;
  if (isThinkingTraceTitle(left.title) && isTokenLikeIntermediate(right) && !isTokenLikeProgressIntermediate(right)) return true;
  if (isThinkingTraceTitle(right.title) && isTokenLikeIntermediate(left) && !isTokenLikeProgressIntermediate(left)) return true;
  if (isProgressTraceTitle(left.title) && isTokenLikeProgressIntermediate(right)) return true;
  if (isProgressTraceTitle(right.title) && isTokenLikeProgressIntermediate(left)) return true;
  if (traceLabelText(left.title).toLowerCase() === traceLabelText(right.title).toLowerCase()) return true;
  // Streamed progress notices arrive titled generically (`Progress`,
  // `Update`, `Notice`) — fold consecutive ones into a single card.
  if (isGenericTraceLabel(left.title) && isGenericTraceLabel(right.title)) return true;
  return isTokenLikeIntermediate(left) && isTokenLikeIntermediate(right);
}

function isTokenLikeIntermediate(card: Extract<ChatTranscriptCard, { kind: 'intermediate' }>): boolean {
  const text = card.text.trim();
  const title = card.title.trim();
  if (!text || text.length > 32 || /\n/.test(text)) return false;
  // A streamed fragment (a numbered-list token, a stray `.`) is still
  // mergeable even when it ends in punctuation; only reject genuine sentences.
  if (/[.!?]$/.test(text) && (text.length > 8 || /\s/.test(text))) return false;
  if (title && title.length <= 32 && text.toLowerCase() === title.toLowerCase()) return true;
  return text.split(/\s+/).length <= 3 && !isSpecificTraceSummary(text);
}

function mergedIntermediateTitle(
  left: Extract<ChatTranscriptCard, { kind: 'intermediate' }>,
  right: Extract<ChatTranscriptCard, { kind: 'intermediate' }>
): string {
  const leftLabel = traceLabelText(left.title);
  const rightLabel = traceLabelText(right.title);
  if (isThinkingTraceTitle(left.title) || isThinkingTraceTitle(right.title)) return 'Thinking';
  if (isProgressTraceTitle(left.title) || isProgressTraceTitle(right.title)) return 'Progress';
  if (isTokenLikeProgressIntermediate(left) && isTokenLikeProgressIntermediate(right)) return 'Progress';
  if (leftLabel && leftLabel.toLowerCase() === rightLabel.toLowerCase()) return left.title || right.title;
  // Consecutive generically-titled progress notices collapse to one stream;
  // never surface a single fragment (`1`, `.`) as the card title.
  if (isGenericTraceLabel(left.title) || isGenericTraceLabel(right.title)) return 'Progress';
  if (isTokenLikeIntermediate(left) && isTokenLikeIntermediate(right)) return 'Thinking';
  return left.title || right.title || 'Update';
}

function mergedTraceDetail(title: string, eventIds: string[]): string | null {
  return traceDetailSummary(title, uniqueStrings(eventIds));
}

function uniqueStrings(values: string[]): string[] {
  return Array.from(new Set(values.filter(Boolean)));
}

function appendCappedUniqueStrings(existing: string[], incoming: string[], max: number): string[] {
  if (max <= 0) return [];
  const out: string[] = [];
  const seen = new Set<string>();
  for (const value of existing) {
    if (!value || seen.has(value)) continue;
    seen.add(value);
    out.push(value);
    if (out.length >= max) return out;
  }
  for (const value of incoming) {
    if (!value || seen.has(value)) continue;
    seen.add(value);
    out.push(value);
    if (out.length >= max) return out;
  }
  return out;
}

function isThinkingTraceTitle(title: string): boolean {
  return traceLabelText(title).toLowerCase() === 'thinking';
}

function isProgressTraceTitle(title: string): boolean {
  return traceLabelText(title).toLowerCase() === 'progress';
}

function isTokenLikeProgressIntermediate(card: Extract<ChatTranscriptCard, { kind: 'intermediate' }>): boolean {
  if (!isTokenLikeIntermediate(card)) return false;
  const text = card.text.trim();
  const title = card.title.trim();
  return /(?:\d|%)/.test(text) || /(?:\d|%)/.test(title);
}

function mapChatTranscriptRow(raw: Record<string, unknown>): ChatTranscriptCard | null {
  const kind = stringValue(raw.kind);
  const id = stringValue(raw.id);
  if (!kind || !id) return null;
  if (kind === 'message') {
    const message = asRecord(raw.message);
    const role = stringValue(message.role) === 'user' ? 'user' : 'assistant';
    const clientTurnId = nullableString(raw.client_turn_id ?? message.client_turn_id);
    const correlationId = nullableString(raw.correlation_id ?? message.correlation_id);
    const identity = asRecord(raw.identity ?? message.identity);
    const userVisibleText = nullableString(raw.user_visible_text ?? message.user_visible_text);
    const capsuleRefs = asRecordArray(raw.capsule_refs ?? message.capsule_refs)
      .map(mapPmaMessageCapsuleRef)
      .filter((item): item is PmaMessageCapsuleRef => item !== null);
    const visibleText = nullableString(raw.visible_text ?? message.visible_text);
    const modelContextText = nullableString(raw.model_context_text ?? message.model_context_text);
    const modelContextRefs = asRecordArray(raw.model_context_refs ?? message.model_context_refs)
      .map(mapPmaMessageCapsuleRef)
      .filter((item): item is PmaMessageCapsuleRef => item !== null);
    const rawModelPrompt = nullableString(raw.raw_model_prompt ?? message.raw_model_prompt);
    return {
      kind: 'message',
      id,
      turnId: nullableString(raw.turn_id),
      orderKey: stringValue(raw.order_key),
      timestamp: nullableString(raw.timestamp),
      message: {
        id: stringValue(message.id) || id,
        chatId: stringValue(message.chat_id),
        role,
        text: role === 'user' ? visibleText ?? userVisibleText ?? stringValue(message.text) : stringValue(message.text),
        visibility: nullableString(raw.visibility ?? message.visibility),
        visibleText,
        modelContextText,
        modelContextRefs,
        rawModelPrompt,
        userVisibleText,
        capsuleRefs,
        createdAt: nullableString(message.created_at),
        status: normalizeOptionalWorkStatus(message.status),
        artifacts: asRecordArray(message.artifacts).map(mapTranscriptArtifact),
        raw: {
          ...asRecord(message.raw),
          client_turn_id: clientTurnId,
          correlation_id: correlationId,
          identity
        }
      }
    };
  }
  if (kind === 'intermediate') {
    return {
      kind: 'intermediate',
      id,
      title: stringValue(raw.title) || 'Update',
      text: stringValue(raw.text),
      eventIds: asStringArray(raw.event_ids),
      progressSourceIds: asStringArray(raw.progress_source_ids),
      detail: nullableString(raw.detail),
      turnId: nullableString(raw.turn_id),
      orderKey: stringValue(raw.order_key),
      timestamp: nullableString(raw.timestamp)
    };
  }
  if (kind === 'tool_group') {
    return {
      kind: 'tool_group',
      id,
      tools: asRecordArray(raw.tools).map(mapTranscriptToolCard),
      turnId: nullableString(raw.turn_id),
      orderKey: stringValue(raw.order_key),
      timestamp: nullableString(raw.timestamp)
    };
  }
  if (kind === 'approval') {
    return {
      kind: 'approval',
      id,
      title: stringValue(raw.title) || 'Approval requested',
      summary: stringValue(raw.summary),
      detail: nullableString(raw.detail),
      turnId: nullableString(raw.turn_id),
      orderKey: stringValue(raw.order_key),
      timestamp: nullableString(raw.timestamp)
    };
  }
  if (kind === 'lifecycle') {
    return {
      kind: 'lifecycle',
      id,
      title: stringValue(raw.title) || 'Update',
      text: stringValue(raw.text),
      detail: nullableString(raw.detail),
      turnId: nullableString(raw.turn_id),
      orderKey: stringValue(raw.order_key),
      timestamp: nullableString(raw.timestamp)
    };
  }
  if (kind === 'artifact') {
    return { kind: 'artifact', id, artifact: mapTranscriptArtifact(asRecord(raw.artifact)) };
  }
  return null;
}

function mapTranscriptToolCard(raw: Record<string, unknown>): ChatToolCallCard {
  const state = stringValue(raw.state);
  return {
    id: stringValue(raw.id) || 'tool',
    title: stringValue(raw.title) || 'Tool call',
    summary: nullableString(raw.summary),
    detail: nullableString(raw.detail),
    state: state === 'completed' || state === 'failed' || state === 'unknown' ? state : 'started',
    eventIds: asStringArray(raw.event_ids)
  };
}

function mapTranscriptArtifact(raw: Record<string, unknown>): SurfaceArtifact {
  return {
    id: stringValue(raw.id) || stringValue(raw.artifact_id) || stringValue(raw.title) || 'artifact',
    kind: stringValue(raw.kind) as SurfaceArtifact['kind'],
    title: stringValue(raw.title) || stringValue(raw.name) || 'Artifact',
    summary: nullableString(raw.summary),
    url: nullableString(raw.url),
    createdAt: nullableString(raw.created_at),
    raw
  };
}

export function mergeChatActivityEvents(
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

export function buildChatActivityCards(
  events: SurfaceArtifact[],
  options: { fallbackTurnId?: string | null } = {}
): ChatTranscriptCard[] {
  const cards: ChatTranscriptCard[] = [];
  let toolGroup: ChatToolCallCard[] = [];
  const fallbackTurnId = options.fallbackTurnId ?? null;

  const flushToolGroup = () => {
    if (!toolGroup.length) return;
    cards.push({
      kind: 'tool_group',
      id: `tools-${toolGroup[0].id}-${toolGroup.at(-1)?.id ?? toolGroup[0].id}`,
      tools: toolGroup,
      turnId: activityTurnId(toolGroup[0].source, fallbackTurnId),
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
    const mergeTarget = findMergeableIntermediate(cards, event, fallbackTurnId);
    if (mergeTarget) {
      mergeTarget.text = mergeIntermediateText(mergeTarget.text, text);
      const progressIds = progressItemEventIds(canonicalProgressItem(event));
      mergeTarget.eventIds.push(event.id, ...progressIds);
      mergeTarget.progressSourceIds.push(...progressIds);
      continue;
    }
    const progressIds = progressItemEventIds(canonicalProgressItem(event));
    cards.push({
      kind: 'intermediate',
      id: `intermediate-${event.id}`,
      title: intermediateTitle(event),
      text,
      detail: null,
      eventIds: [event.id, ...progressIds],
      progressSourceIds: [...progressIds],
      turnId: activityTurnId(event, fallbackTurnId),
      orderKey: activityOrderKey(event),
      timestamp: event.createdAt
    });
  }
  flushToolGroup();
  return cards;
}

function findMergeableIntermediate(
  cards: ChatTranscriptCard[],
  event: SurfaceArtifact,
  fallbackTurnId: string | null
): Extract<ChatTranscriptCard, { kind: 'intermediate' }> | null {
  if (isCommentaryTraceEvent(event)) {
    return null;
  }
  if (isTerminalTraceEvent(event)) {
    return null;
  }
  for (let index = cards.length - 1; index >= 0; index -= 1) {
    const card = cards[index];
    if (card.kind !== 'intermediate') {
      // Do not scan past tool / approval rows — later assistant deltas belong below.
      if (card.kind === 'tool_group' || card.kind === 'approval' || isTerminalTraceCard(card)) return null;
      continue;
    }
    if (shouldMergeIntermediate(card, event, fallbackTurnId)) return card;
    if (isTerminalTraceCard(card)) return null;
    if (isCommentaryTraceCard(card)) continue;
    // Non-mergeable trace for another title/turn: keep scanning.
    continue;
  }
  return null;
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
  const tokenUsage = extractTokenUsage(progress?.raw) ?? extractTokenUsage(chat?.raw);
  const contextRemainingPercent = contextRemainingPercentFromUsage(tokenUsage);
  const elapsedValue =
    progress?.elapsedSeconds === null || progress?.elapsedSeconds === undefined
      ? null
      : formatDuration(progress.elapsedSeconds);
  const queueDepth = progress?.queueDepth ?? 0;
  const bucket = usageBucket(tokenUsage);
  const totalTokens = tokenNumber(bucket, 'totalTokens', 'total_tokens');
  const inputTokens = tokenNumber(bucket, 'inputTokens', 'input_tokens');
  const outputTokens = tokenNumber(bucket, 'outputTokens', 'output_tokens');
  return {
    state,
    phase: progress?.phase?.replace(/_/g, ' ') || statusLabel(state),
    elapsedLabel: elapsedValue === null ? 'elapsed n/a' : `${elapsedValue} elapsed`,
    elapsedValue,
    queueDepth,
    queueDepthLabel: `queue ${queueDepth}`,
    tokenUsageLabel: formatTokenUsageLabel(tokenUsage),
    totalTokensFull: totalTokens === null ? null : formatCompactNumber(totalTokens),
    totalTokensCompact: totalTokens === null ? null : formatAbbreviatedNumber(totalTokens),
    inputTokensFull: inputTokens === null ? null : formatCompactNumber(inputTokens),
    inputTokensCompact: inputTokens === null ? null : formatAbbreviatedNumber(inputTokens),
    outputTokensFull: outputTokens === null ? null : formatCompactNumber(outputTokens),
    outputTokensCompact: outputTokens === null ? null : formatAbbreviatedNumber(outputTokens),
    contextRemainingLabel:
      contextRemainingPercent === null ? null : `ctx ${contextRemainingPercent}%`,
    contextRemainingPercent
  };
}

function formatAbbreviatedNumber(value: number): string {
  const abs = Math.abs(value);
  if (abs < 1000) return `${Math.round(value)}`;
  if (abs < 10000) return `${(value / 1000).toFixed(1)}k`;
  if (abs < 1000000) return `${Math.round(value / 1000)}k`;
  if (abs < 10000000) return `${(value / 1000000).toFixed(1)}M`;
  return `${Math.round(value / 1000000)}M`;
}

function recordValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

function numberValue(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function tokenNumber(source: Record<string, unknown> | null, ...keys: string[]): number | null {
  if (!source) return null;
  for (const key of keys) {
    const value = numberValue(source[key]);
    if (value !== null) return value;
  }
  return null;
}

function extractTokenUsage(source: unknown): Record<string, unknown> | null {
  const raw = recordValue(source);
  if (!raw) return null;
  for (const candidate of [raw.token_usage, raw.tokenUsage, recordValue(raw.turn)?.token_usage, recordValue(raw.snapshot)?.token_usage]) {
    const usage = recordValue(candidate);
    if (usage && Object.keys(usage).length > 0) return usage;
  }
  return null;
}

function usageBucket(tokenUsage: Record<string, unknown> | null): Record<string, unknown> | null {
  if (!tokenUsage) return null;
  return recordValue(tokenUsage.last) ?? recordValue(tokenUsage.total) ?? tokenUsage;
}

function contextRemainingPercentFromUsage(tokenUsage: Record<string, unknown> | null): number | null {
  const bucket = usageBucket(tokenUsage);
  const totalTokens = tokenNumber(bucket, 'totalTokens', 'total_tokens');
  const contextWindow = tokenNumber(tokenUsage, 'modelContextWindow', 'context_window', 'contextWindow') ??
    tokenNumber(bucket, 'modelContextWindow', 'context_window', 'contextWindow');
  if (totalTokens === null || contextWindow === null || contextWindow <= 0) return null;
  return Math.max(0, Math.min(100, 100 - Math.round((totalTokens / contextWindow) * 100)));
}

function formatCompactNumber(value: number): string {
  return Math.round(value).toLocaleString('en-US');
}

function formatTokenUsageLabel(tokenUsage: Record<string, unknown> | null): string | null {
  const bucket = usageBucket(tokenUsage);
  const totalTokens = tokenNumber(bucket, 'totalTokens', 'total_tokens');
  if (totalTokens === null) return null;
  const inputTokens = tokenNumber(bucket, 'inputTokens', 'input_tokens');
  const outputTokens = tokenNumber(bucket, 'outputTokens', 'output_tokens');
  const parts = [`tokens ${formatCompactNumber(totalTokens)} total`];
  if (inputTokens !== null) parts.push(`${formatCompactNumber(inputTokens)} in`);
  if (outputTokens !== null) parts.push(`${formatCompactNumber(outputTokens)} out`);
  return parts.join(' · ');
}

export function isPrimaryProgressArtifact(artifact: SurfaceArtifact): boolean {
  const item = canonicalProgressItem(artifact);
  if (!item || item.hidden === true) return false;
  const kind = stringValue(item.kind);
  return TOOL_PROGRESS_KINDS.has(kind) || ['assistant_update', 'notice', 'approval', 'turn_failed', 'turn_interrupted'].includes(kind);
}

function isToolActivityEvent(event: SurfaceArtifact): boolean {
  return TOOL_PROGRESS_KINDS.has(stringValue(canonicalProgressItem(event)?.kind));
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
  const fallback = traceLabelText(item?.title ?? item?.kind) || 'Update';
  return traceTitleFromSources(fallback, [asRecord(item), recordValue(event.raw)]);
}

function shouldMergeIntermediate(
  card: Extract<ChatTranscriptCard, { kind: 'intermediate' }>,
  event: SurfaceArtifact,
  fallbackTurnId: string | null = null
): boolean {
  return card.turnId === activityTurnId(event, fallbackTurnId) && !isCommentaryTraceCard(card);
}

function mergeIntermediateText(current: string, incoming: string, maxChars = Number.POSITIVE_INFINITY): string {
  if (!current) return clampMergedIntermediateText(incoming, maxChars);
  if (current.includes('[additional live updates omitted]')) return current;
  if (!incoming) return current;
  if (incoming === current) return current;
  if (incoming.startsWith(current)) return clampMergedIntermediateText(incoming, maxChars);
  if (current.endsWith(incoming)) return current;
  const maxOverlap = Math.min(current.length, Math.max(incoming.length - 1, 0));
  for (let overlap = maxOverlap; overlap > 0; overlap -= 1) {
    if (current.slice(-overlap) === incoming.slice(0, overlap)) {
      return clampMergedIntermediateText(`${current}${incoming.slice(overlap)}`, maxChars);
    }
  }
  if (/\s$/.test(current) || /^\s/.test(incoming)) return clampMergedIntermediateText(`${current}${incoming}`, maxChars);
  if (/^[,.;:!?)]/.test(incoming) || /[(]$/.test(current)) return clampMergedIntermediateText(`${current}${incoming}`, maxChars);
  return clampMergedIntermediateText(`${current} ${incoming}`, maxChars);
}

function clampMergedIntermediateText(text: string, maxChars: number): string {
  if (!Number.isFinite(maxChars) || maxChars <= 0 || text.length <= maxChars) return text;
  const marker = '\n\n[additional live updates omitted]';
  const bodyLimit = Math.max(0, maxChars - marker.length);
  return `${text.slice(0, bodyLimit).trimEnd()}${marker}`;
}

function thinkingTimelineDetail(item: PmaTimelineItem): string | null {
  const kind = stringValue(item.payload.intermediate_kind).trim().toLowerCase();
  if (kind !== 'thinking') return null;
  return traceDetailSummary('Thinking', timelineSourceEventIds(item));
}

function traceDetailSummary(title: string, eventIds: string[]): string | null {
  const uniqueIds = Array.from(new Set(eventIds.filter(Boolean)));
  if (!uniqueIds.length) return null;
  const kind = title.trim().toLowerCase() === 'thinking' ? 'thinking' : 'progress';
  const label = uniqueIds.length === 1 ? `1 ${kind} update` : `${uniqueIds.length} ${kind} updates`;
  return `${label} · source events ${uniqueIds.join(', ')}`;
}

function toolDisplayTitle(event: SurfaceArtifact): string {
  const item = canonicalProgressItem(event);
  return stringValue(item?.tool_name) || stringValue(item?.title) || event.summary || event.title || 'Tool call';
}

function toolState(event: SurfaceArtifact): ChatToolCallCard['state'] {
  const rawState = stringValue(canonicalProgressItem(event)?.state).toLowerCase();
  if (rawState === 'started' || rawState === 'completed' || rawState === 'failed') return rawState;
  return 'unknown';
}

function traceLabelText(value: unknown): string {
  return stringValue(value).replace(/[_.]+/g, ' ').replace(/\s+/g, ' ').trim();
}

function isGenericTraceLabel(value: string): boolean {
  return ['progress', 'update', 'notice', 'assistant update'].includes(traceLabelText(value).toLowerCase());
}

function isSpecificTraceSummary(value: string): boolean {
  const normalized = traceLabelText(value);
  return normalized.includes(' ') || normalized.includes(':') || normalized.includes('/') || normalized.length > 12;
}

function traceTitleFromSources(
  fallback: string,
  sources: Array<Record<string, unknown> | null | undefined>,
  options: { allowSummaryFallback?: boolean } = {}
): string {
  for (const source of sources) {
    const title = traceLabelText(source?.title ?? source?.display_title ?? source?.name);
    if (title && !isGenericTraceLabel(title)) return title;
  }

  if (
    sources.some(
      (source) => traceLabelText(source?.event_type ?? source?.progress_kind ?? source?.kind).toLowerCase() === 'assistant update'
    )
  ) {
    return 'Thinking';
  }

  if (options.allowSummaryFallback !== false) {
    for (const source of sources) {
      const summary = traceLabelText(source?.summary ?? source?.message ?? source?.text);
      if (summary && !isGenericTraceLabel(summary) && isSpecificTraceSummary(summary)) return summary;
    }
  }

  for (const source of sources) {
    const phase = traceLabelText(source?.phase ?? source?.assistant_phase ?? source?.tool_phase);
    if (phase && !isGenericTraceLabel(phase)) return phase;
  }

  for (const source of sources) {
    const eventType = traceLabelText(source?.event_type ?? source?.progress_kind ?? source?.kind);
    if (eventType && !isGenericTraceLabel(eventType)) return eventType;
  }

  return fallback;
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

function timelineItemToCard(item: PmaTimelineItem): ChatTranscriptCard[] {
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
      progressSourceIds: [],
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
  if (item.kind === 'lifecycle') {
    const title = stringValue(item.payload.title) || 'Chat compacted';
    const preview = stringValue(item.payload.summary_preview);
    const text = stringValue(item.payload.text) || 'Chat compacted.';
    return [{
      kind: 'lifecycle',
      id: item.id,
      title,
      text: preview ? `${text}\n\n${preview}` : text,
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

function toolCardFromTimeline(item: PmaTimelineItem): ChatToolCallCard {
  const result = asRecord(item.payload.result);
  const call = asRecord(item.payload.call);
  const rawState = stringValue(result.status ?? item.raw.status ?? item.status).toLowerCase();
  const state: ChatToolCallCard['state'] =
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
  const payload = asRecord(item.payload);
  const kind = traceLabelText(payload.intermediate_kind);
  return traceTitleFromSources(kind || 'Update', [payload, asRecord(payload.progress_item), asRecord(payload.event)], {
    allowSummaryFallback: false
  });
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

function isCommentaryTraceCard(card: ChatTranscriptCard): boolean {
  if (card.kind !== 'intermediate') return false;
  const title = card.title.trim().toLowerCase();
  return title === 'commentary';
}

function isTerminalTraceCard(card: ChatTranscriptCard): boolean {
  if (card.kind !== 'intermediate') return false;
  const title = card.title.trim().toLowerCase();
  return title === 'run failed' || title === 'turn failed' || title === 'interrupted';
}

function isCommentaryTraceEvent(event: SurfaceArtifact): boolean {
  const item = canonicalProgressItem(event);
  return traceLabelText(item?.title ?? event.title).toLowerCase() === 'commentary';
}

function isTerminalTraceEvent(event: SurfaceArtifact): boolean {
  const title = intermediateTitle(event).trim().toLowerCase();
  return title === 'run failed' || title === 'turn failed' || title === 'interrupted';
}

function timelineSourceEventIds(item: PmaTimelineItem): string[] {
  return [
    ...unknownArrayToStrings(item.provenance.sourceEventIds),
    ...unknownArrayToStrings(item.provenance.progressEventIds)
  ];
}

function progressItemEventIds(item: CanonicalProgressItem | null | undefined): string[] {
  return unknownArrayToStrings(item?.event_ids);
}

function unknownArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function unknownArrayToStrings(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => stringValue(item)).filter(Boolean);
}

function activityTurnId(event: SurfaceArtifact | undefined, fallbackTurnId: string | null = null): string | null {
  if (!event) return null;
  return stringValue(event.raw.managed_turn_id ?? event.raw.turn_id ?? event.raw.execution_id ?? event.raw.run_id) || fallbackTurnId;
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

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function asRecordArray(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value)
    ? value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object' && !Array.isArray(item))
    : [];
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => String(item)).filter(Boolean) : [];
}

function nullableString(value: unknown): string | null {
  const text = stringValue(value);
  return text || null;
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

/** Compact local datetime for message footers; pass `locale` in tests for stable output. */
export function formatCompactMessageDateTime(
  value: string | null,
  now = new Date(),
  locale: Intl.LocalesArgument = undefined
): string | null {
  if (!value?.trim()) return null;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return null;
  const timeFmt = new Intl.DateTimeFormat(locale, { hour: 'numeric', minute: '2-digit' });
  const timeStr = timeFmt.format(parsed);
  const sameCalendarDay =
    parsed.getFullYear() === now.getFullYear() &&
    parsed.getMonth() === now.getMonth() &&
    parsed.getDate() === now.getDate();
  if (sameCalendarDay) return timeStr;
  const sameYear = parsed.getFullYear() === now.getFullYear();
  if (sameYear) {
    const dateFmt = new Intl.DateTimeFormat(locale, { month: 'short', day: 'numeric' });
    return `${dateFmt.format(parsed)} · ${timeStr}`;
  }
  const dateFmt = new Intl.DateTimeFormat(locale, { month: 'short', day: 'numeric', year: 'numeric' });
  return `${dateFmt.format(parsed)} · ${timeStr}`;
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
  profile = '',
  chatKind: PmaChatKind = 'pma'
): ManagedThreadCreatePayload {
  const base: Pick<ManagedThreadCreatePayload, 'agent' | 'name' | 'model'> = {
    agent: agent || undefined,
    name
  };
  if (model) base.model = model;
  const trimmedProfile = profile.trim();
  return {
    ...base,
    chat_kind: chatKind,
    ...(trimmedProfile ? { profile: trimmedProfile } : {}),
    scope_urn: scope.scopeUrn
  };
}

export type PmaChatKind = 'pma' | 'coding_agent';

export function pmaChatKind(chat: PmaChatSummary | null): PmaChatKind {
  if (!chat) return 'pma';
  if (chat.chatKind === 'coding_agent') return 'coding_agent';
  if (chat.chatKind === 'pma') return 'pma';
  const rawKind = stringValue(chat.raw.chat_kind ?? chat.raw.thread_kind ?? chat.raw.kind).toLowerCase();
  if (rawKind === 'pma') return 'pma';
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

export function localPmaChatScopeOption(): PmaChatScopeLocalOption {
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

export type PmaChatScopeLocalOption = Extract<PmaChatScopeOption, { kind: 'local' }>;
export type PmaChatScopeRepoOption = Extract<PmaChatScopeOption, { kind: 'repo' }>;
export type PmaChatScopeWorktreeOption = Extract<PmaChatScopeOption, { kind: 'worktree' }>;

/** A repo and the worktrees that belong to it, mirroring the repos-page grouping. */
export type PmaChatScopeGroup = {
  /** Stable key — repo id, or a synthetic key for worktrees with no catalogued repo. */
  key: string;
  /** Header label shown above the group's worktrees. */
  repoLabel: string;
  /** The repo itself as a selectable scope, when one exists in the catalog. */
  repo: PmaChatScopeRepoOption | null;
  /** Worktrees belonging to this repo. */
  worktrees: PmaChatScopeWorktreeOption[];
};

export type PmaChatScopeGroupView = {
  /** Local hub option, or null when filtered out by a search query. */
  local: PmaChatScopeLocalOption | null;
  groups: PmaChatScopeGroup[];
};

const ORPHAN_SCOPE_GROUP_KEY = '__orphan__';

/**
 * Group flat scope options into repo → worktree buckets so the picker can show which repo a
 * worktree belongs to. Repo order is preserved; worktrees with no catalogued repo collapse into a
 * single trailing "Other worktrees" group.
 */
export function groupPmaChatScopeOptions(options: PmaChatScopeOption[]): PmaChatScopeGroupView {
  const local = options.find((opt): opt is PmaChatScopeLocalOption => opt.kind === 'local') ?? null;
  const groups: PmaChatScopeGroup[] = [];
  const byKey = new Map<string, PmaChatScopeGroup>();

  const ensureGroup = (key: string, label: string, repo: PmaChatScopeRepoOption | null): PmaChatScopeGroup => {
    let group = byKey.get(key);
    if (!group) {
      group = { key, repoLabel: label, repo, worktrees: [] };
      byKey.set(key, group);
      groups.push(group);
    } else if (repo && !group.repo) {
      group.repo = repo;
      group.repoLabel = label;
    }
    return group;
  };

  for (const opt of options) {
    if (opt.kind === 'repo') ensureGroup(opt.resourceId, opt.label, opt);
  }
  for (const opt of options) {
    if (opt.kind !== 'worktree') continue;
    const key = opt.parentRepoId ?? ORPHAN_SCOPE_GROUP_KEY;
    const label = opt.parentRepoId ?? 'Other worktrees';
    ensureGroup(key, label, null).worktrees.push(opt);
  }

  return { local, groups };
}

function scopeOptionMatchesQuery(option: PmaChatScopeOption, query: string): boolean {
  if (!query) return true;
  const haystack = `${option.label} ${option.detail} ${option.scopeUrn}`.toLowerCase();
  return query
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .every((term) => haystack.includes(term));
}

/**
 * Filter a grouped scope view by a free-text query. A repo header that matches keeps all of its
 * worktrees; otherwise only matching worktrees are kept — but the repo header always stays so the
 * worktree's owning repo remains visible while searching.
 */
export function filterPmaChatScopeGroups(
  view: PmaChatScopeGroupView,
  query: string
): PmaChatScopeGroupView {
  const trimmed = query.trim();
  if (!trimmed) return view;
  const needle = trimmed.toLowerCase();
  const groups: PmaChatScopeGroup[] = [];
  for (const group of view.groups) {
    const headerMatches =
      group.repoLabel.toLowerCase().includes(needle) ||
      (group.repo ? scopeOptionMatchesQuery(group.repo, trimmed) : false);
    const worktrees = headerMatches
      ? group.worktrees
      : group.worktrees.filter((worktree) => scopeOptionMatchesQuery(worktree, trimmed));
    if (headerMatches || worktrees.length > 0) {
      groups.push({ ...group, worktrees });
    }
  }
  const local = view.local && scopeOptionMatchesQuery(view.local, trimmed) ? view.local : null;
  return { local, groups };
}

/** Selectable options in keyboard-navigation order: local hub, then each repo before its worktrees. */
export function flattenPmaChatScopeGroupView(view: PmaChatScopeGroupView): PmaChatScopeOption[] {
  const flat: PmaChatScopeOption[] = [];
  if (view.local) flat.push(view.local);
  for (const group of view.groups) {
    if (group.repo) flat.push(group.repo);
    flat.push(...group.worktrees);
  }
  return flat;
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
  busyPolicy: 'queue' | 'interrupt' | 'reject' | null = isRunning ? 'queue' : null,
  clientTurnId = ''
): ManagedThreadMessagePayload {
  const trimmed = profile.trim();
  const trimmedClientTurnId = clientTurnId.trim();
  return {
    message,
    attachments: attachments.length ? attachments.map(pendingAttachmentToIntent) : undefined,
    model: model || undefined,
    reasoning: reasoning || undefined,
    ...(trimmed ? { profile: trimmed } : {}),
    client_turn_id: trimmedClientTurnId || undefined,
    busy_policy: busyPolicy ?? undefined,
    defer_execution: true,
    wait_for_confirmation: false
  };
}

export function buildManagedThreadStartMessagePayload(
  scope: PmaChatScopeOption,
  agent: string,
  profile: string,
  model: string,
  name: string,
  chatKind: PmaChatKind,
  message: string,
  attachments: Array<PendingAttachment | DocumentFileIntentPayload> = [],
  reasoning = '',
  clientTurnId = '',
  scopeSource: PmaChatScopeSource = 'default_hub'
): ManagedThreadStartMessagePayload {
  return {
    ...buildManagedThreadCreatePayload(agent, scope, name, model, profile, chatKind),
    origin: 'web',
    scope_source: scopeSource,
    ...buildManagedThreadMessagePayload(
      message,
      model,
      false,
      attachments,
      reasoning,
      profile,
      null,
      clientTurnId
    )
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
