import type {
  AgentWorkspaceSummary,
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

export type PmaChatFilter = 'all' | 'active' | 'waiting' | 'done';

/** Synthetic list selection id for pinned PMA Memory in the chats sidebar. */
export const PMA_MEMORY_LIST_ID = '__memory__';

export const PMA_CHAT_FILTER_ORDER: PmaChatFilter[] = ['all', 'waiting', 'active', 'done'];

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
  | { kind: 'message'; id: string; message: PmaChatMessage }
  | { kind: 'intermediate'; id: string; title: string; text: string; eventIds: string[] }
  | { kind: 'tool_group'; id: string; tools: PmaToolCallCard[] }
  | { kind: 'ticket'; id: string; title: string; summary: string | null; ticketId: string }
  | { kind: 'artifact'; id: string; artifact: SurfaceArtifact };

export type PmaToolCallCard = {
  id: string;
  title: string;
  summary: string | null;
  state: 'started' | 'completed' | 'failed' | 'unknown';
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
  name: string;
  workspace_root?: string;
  resource_kind?: 'repo' | 'agent_workspace';
  resource_id?: string;
};

export type PmaChatScopeOption =
  | {
      id: 'local';
      kind: 'local';
      label: string;
      detail: string;
      workspaceRoot: string;
    }
  | {
      id: string;
      kind: 'repo';
      label: string;
      detail: string;
      resourceKind: 'repo';
      resourceId: string;
    }
  | {
      id: string;
      kind: 'worktree';
      label: string;
      detail: string;
      workspaceRoot: string;
      resourceId: string;
    }
  | {
      id: string;
      kind: 'agent_workspace';
      label: string;
      detail: string;
      resourceKind: 'agent_workspace';
      resourceId: string;
      agentId: string | null;
    };

export type ManagedThreadMessagePayload = {
  message: string;
  attachments?: DocumentFileIntentPayload[];
  model?: string;
  busy_policy?: 'queue';
};

const activeStatuses: WorkStatus[] = ['running'];
const waitingStatuses: WorkStatus[] = ['waiting', 'blocked'];
const doneStatuses: WorkStatus[] = ['done', 'failed', 'idle'];

export function filterPmaChats(
  chats: PmaChatSummary[],
  filter: PmaChatFilter,
  query: string
): PmaChatSummary[] {
  const needle = query.trim().toLowerCase();
  return chats
    .filter((chat) => {
      if (filter === 'active') return activeStatuses.includes(chat.status);
      if (filter === 'waiting') return waitingStatuses.includes(chat.status);
      if (filter === 'done') return doneStatuses.includes(chat.status);
      return true;
    })
    .filter((chat) => {
      if (!needle) return true;
      return [
        chat.title,
        chat.repoId,
        chat.worktreeId,
        chat.ticketId,
        chat.agentId,
        chat.model,
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

export function summarizeFilterCounts(chats: PmaChatSummary[]): Record<PmaChatFilter, number> {
  return {
    all: chats.length,
    active: chats.filter((chat) => activeStatuses.includes(chat.status)).length,
    waiting: chats.filter((chat) => waitingStatuses.includes(chat.status)).length,
    done: chats.filter((chat) => doneStatuses.includes(chat.status)).length
  };
}

export function chooseActiveChatId(
  chats: PmaChatSummary[],
  currentId: string | null,
  requestedId: string | null = null
): string | null {
  if (requestedId && chats.some((chat) => chat.id === requestedId)) return requestedId;
  if (currentId && chats.some((chat) => chat.id === currentId)) return currentId;
  return chats[0]?.id ?? null;
}

export function buildPmaCards(
  timeline: PmaTimelineItem[],
  chat: PmaChatSummary | null,
  artifacts: SurfaceArtifact[]
): PmaCard[] {
  const cards: PmaCard[] = timeline.flatMap(timelineItemToCard);

  if (chat?.ticketId) {
    cards.push({
      kind: 'ticket',
      id: `ticket-${chat.ticketId}`,
      ticketId: chat.ticketId,
      title: chat.ticketId,
      summary: chat.title
    });
  }

  for (const artifact of filterArtifactsForActiveChat(artifacts, chat, null).slice(0, 4)) {
    cards.push({ kind: 'artifact', id: `artifact-${artifact.id}`, artifact });
  }

  return cards;
}

export function reconcilePmaTimeline(
  existing: PmaTimelineItem[],
  incoming: PmaTimelineItem[],
  limit = 500
): PmaTimelineItem[] {
  if (!incoming.length) return existing;
  const byId = new Map(existing.map((item) => [item.id, item]));
  for (const item of incoming) {
    byId.set(item.id, { ...byId.get(item.id), ...item, payload: { ...byId.get(item.id)?.payload, ...item.payload } });
  }
  return [...byId.values()]
    .sort(compareTimelineItems)
    .slice(-limit);
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
      tools: toolGroup
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
        state: toolState(event)
      });
      continue;
    }

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
      eventIds: [event.id]
    });
  }
  flushToolGroup();
  return cards;
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
      ? steps.at(-1)?.summary ?? steps.at(-1)?.title ?? 'PMA is updating the workspace.'
      : status === 'running'
        ? 'PMA is streaming activity.'
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
  return 'PMA update';
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

function toolDisplayTitle(event: SurfaceArtifact): string {
  const item = canonicalProgressItem(event);
  return stringValue(item?.tool_name) || stringValue(item?.title) || event.summary || event.title || 'Tool call';
}

function toolState(event: SurfaceArtifact): PmaToolCallCard['state'] {
  const rawState = stringValue(canonicalProgressItem(event)?.state).toLowerCase();
  if (rawState === 'started' || rawState === 'completed' || rawState === 'failed') return rawState;
  return 'unknown';
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
      },
      ...attachments.map((artifact) => ({ kind: 'artifact' as const, id: `${item.id}:artifact:${artifact.id}`, artifact }))
    ];
  }
  if (item.kind === 'intermediate') {
    const text = stringValue(item.payload.text);
    if (!text.trim()) return [];
    return [{ kind: 'intermediate', id: item.id, title: intermediateTimelineTitle(item), text, eventIds: [item.id] }];
  }
  if (item.kind === 'tool_group') {
    return [{ kind: 'tool_group', id: item.id, tools: [toolCardFromTimeline(item)] }];
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
  return { id: item.id, title, summary, state };
}

function intermediateTimelineTitle(item: PmaTimelineItem): string {
  const kind = stringValue(item.payload.intermediate_kind).replace(/_/g, ' ');
  return kind || 'PMA update';
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

export function pendingAttachmentToIntent(attachment: PendingAttachment): DocumentFileIntentPayload {
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
  attachments: PendingAttachment[]
): string {
  const message = draft.trim();
  const lines = attachments.map((attachment) => {
    const label = attachment.kind === 'image' ? 'Image' : attachment.kind === 'link' ? 'Link' : 'File';
    const target = attachment.url || attachment.uploadedName || attachment.title;
    return `- ${label}: ${attachment.title}${target && target !== attachment.title ? ` (${target})` : ''}`;
  });
  if (!lines.length) return message;
  return [message, 'Attachments:', ...lines].filter(Boolean).join('\n');
}

export function buildManagedThreadCreatePayload(
  agent: string,
  scope: PmaChatScopeOption = localPmaChatScopeOption(),
  name = 'New PMA chat',
  model = ''
): ManagedThreadCreatePayload {
  const base: Pick<ManagedThreadCreatePayload, 'agent' | 'name' | 'model'> = {
    agent: agent || undefined,
    name
  };
  if (model) base.model = model;
  if (scope.kind === 'repo' || scope.kind === 'agent_workspace') {
    return {
      ...base,
      resource_kind: scope.resourceKind,
      resource_id: scope.resourceId
    };
  }
  return {
    ...base,
    workspace_root: scope.workspaceRoot
  };
}

export function localPmaChatScopeOption(): PmaChatScopeOption {
  return {
    id: 'local',
    kind: 'local',
    label: 'Local hub',
    detail: 'Current workspace',
    workspaceRoot: '.'
  };
}

export function buildPmaChatScopeOptions(
  repos: RepoSummary[],
  worktrees: WorktreeSummary[],
  agentWorkspaces: AgentWorkspaceSummary[]
): PmaChatScopeOption[] {
  return [
    localPmaChatScopeOption(),
    ...repos.map((repo) => ({
      id: `repo:${repo.id}`,
      kind: 'repo' as const,
      label: repo.name || repo.id,
      detail: `Repo · ${repo.id}`,
      resourceKind: 'repo' as const,
      resourceId: repo.id
    })),
    ...worktrees
      .filter((worktree) => Boolean(worktree.path))
      .map((worktree) => ({
        id: `worktree:${worktree.id}`,
        kind: 'worktree' as const,
        label: worktree.name || worktree.id,
        detail: `Worktree · ${worktree.repoId ?? worktree.id}`,
        workspaceRoot: worktree.path || '.',
        resourceId: worktree.id
      })),
    ...agentWorkspaces.map((workspace) => ({
      id: `agent_workspace:${workspace.id}`,
      kind: 'agent_workspace' as const,
      label: workspace.name || workspace.id,
      detail: `Agent workspace · ${workspace.runtime || workspace.id}`,
      resourceKind: 'agent_workspace' as const,
      resourceId: workspace.id,
      agentId: workspace.runtime || null
    }))
  ];
}

export function pmaChatScopeLabel(scope: PmaChatScopeOption | null): string {
  if (!scope) return 'Workspace scope';
  if (scope.kind === 'local') return 'Local hub · current workspace';
  if (scope.kind === 'repo') return `Repo · ${scope.resourceId}`;
  if (scope.kind === 'agent_workspace') return `Agent workspace · ${scope.resourceId}`;
  return `Worktree · ${scope.resourceId}`;
}

export function pmaChatScopeLabelFromChat(chat: PmaChatSummary | null): string {
  if (!chat) return 'Choose a scope before creating a chat';
  const resourceKind = stringValue(chat.raw.resource_kind).toLowerCase();
  const resourceId = stringValue(chat.raw.resource_id);
  if (resourceKind === 'agent_workspace' && resourceId) return `Agent workspace · ${resourceId}`;
  if (chat.worktreeId) return `Worktree · ${chat.worktreeId}`;
  if (chat.repoId) return `Repo · ${chat.repoId}`;
  const workspaceRoot = stringValue(chat.raw.workspace_root);
  if (workspaceRoot && workspaceRoot !== '.') return `Workspace · ${workspaceRoot}`;
  return 'Local hub · current workspace';
}

/** One-line scope for the active chat header (`PMA - global` vs repo naming). */
export function pmaChatHeaderScopeLine(
  chat: PmaChatSummary | null,
  repoLabel?: (repoId: string) => string | null
): string {
  if (!chat) return '';
  const resourceKind = stringValue(chat.raw.resource_kind).toLowerCase();
  const resourceId = stringValue(chat.raw.resource_id);
  if (resourceKind === 'agent_workspace' && resourceId) return `Agent workspace - ${resourceId}`;
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
  return 'PMA - global';
}

export function buildManagedThreadMessagePayload(
  message: string,
  model: string,
  isRunning: boolean,
  attachments: PendingAttachment[] = []
): ManagedThreadMessagePayload {
  return {
    message,
    attachments: attachments.length ? attachments.map(pendingAttachmentToIntent) : undefined,
    model: model || undefined,
    busy_policy: isRunning ? 'queue' : undefined
  };
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
        label: 'PMA final report',
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
