import type {
  PmaChatMessage,
  PmaChatSummary,
  PmaRunProgress,
  SurfaceArtifact,
  WorkStatus
} from './domain';

export type PmaChatFilter = 'all' | 'active' | 'waiting' | 'done';

export type PendingAttachmentKind = 'file' | 'image' | 'link';

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

export type PmaCard =
  | { kind: 'message'; id: string; message: PmaChatMessage }
  | { kind: 'ticket'; id: string; title: string; summary: string | null; ticketId: string }
  | { kind: 'progress'; id: string; progress: PmaRunProgress }
  | { kind: 'streaming'; id: string; progress: PmaRunProgress }
  | { kind: 'artifact'; id: string; artifact: SurfaceArtifact };

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
      return [chat.title, chat.repoId, chat.worktreeId, chat.ticketId, chat.agentId, chat.model]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(needle));
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

export function chooseActiveChatId(chats: PmaChatSummary[], currentId: string | null): string | null {
  if (currentId && chats.some((chat) => chat.id === currentId)) return currentId;
  return chats[0]?.id ?? null;
}

export function buildPmaCards(
  messages: PmaChatMessage[],
  progress: PmaRunProgress | null,
  chat: PmaChatSummary | null,
  artifacts: SurfaceArtifact[]
): PmaCard[] {
  const cards: PmaCard[] = messages.flatMap((message) => [
    {
      kind: 'message' as const,
      id: message.id,
      message
    },
    ...message.artifacts.map((artifact) => ({
      kind: 'artifact' as const,
      id: `message-${message.id}-${artifact.id}`,
      artifact
    }))
  ]);

  if (chat?.ticketId) {
    cards.push({
      kind: 'ticket',
      id: `ticket-${chat.ticketId}`,
      ticketId: chat.ticketId,
      title: chat.ticketId,
      summary: chat.title
    });
  }

  if (progress) {
    cards.push({
      kind: 'progress',
      id: `progress-${progress.id}`,
      progress
    });
    if (progress.status === 'running' || progress.status === 'waiting') {
      cards.push({
        kind: 'streaming',
        id: `streaming-${progress.id}`,
        progress
      });
    }
    for (const event of progress.events.slice(-3)) {
      cards.push({ kind: 'artifact', id: `event-${event.id}`, artifact: event });
    }
  }

  for (const artifact of artifacts.slice(0, 4)) {
    cards.push({ kind: 'artifact', id: `artifact-${artifact.id}`, artifact });
  }

  return cards;
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

export function removePendingAttachment(
  attachments: PendingAttachment[],
  attachmentId: string
): PendingAttachment[] {
  return attachments.filter((attachment) => attachment.id !== attachmentId);
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

export function modelSelectorState(
  loading: boolean,
  errorMessage: string | null,
  modelCount: number
): ModelSelectorState {
  if (loading) {
    return { state: 'loading', label: 'Loading models', disabled: true };
  }
  if (errorMessage) {
    return { state: 'error', label: errorMessage, disabled: true };
  }
  if (modelCount === 0) {
    return { state: 'empty', label: 'No models exposed', disabled: true };
  }
  return { state: 'loaded', label: 'Model', disabled: false };
}

function clampPercent(value: number): number {
  return Math.max(0, Math.min(100, Math.round(value)));
}
