import type {
  PmaChatMessage,
  PmaChatSummary,
  PmaRunProgress,
  SurfaceArtifact,
  WorkStatus
} from './domain';

export type PmaChatFilter = 'all' | 'active' | 'waiting' | 'done';

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
  const cards: PmaCard[] = messages.map((message) => ({
    kind: 'message',
    id: message.id,
    message
  }));

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

function clampPercent(value: number): number {
  return Math.max(0, Math.min(100, Math.round(value)));
}
