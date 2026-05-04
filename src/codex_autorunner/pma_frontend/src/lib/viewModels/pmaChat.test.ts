import { describe, expect, it } from 'vitest';
import type { PmaChatMessage, PmaChatSummary, PmaRunProgress, SurfaceArtifact } from './domain';
import {
  buildPmaCards,
  chooseActiveChatId,
  filterPmaChats,
  formatRelativeTime,
  progressPercent,
  summarizeFilterCounts
} from './pmaChat';

const baseChat: PmaChatSummary = {
  id: 'chat-1',
  title: 'Repo repair',
  status: 'running',
  agentId: 'codex',
  model: 'gpt-5.2',
  repoId: 'repo-1',
  worktreeId: 'repo-1--pma',
  ticketId: 'TICKET-120',
  progressPercent: null,
  updatedAt: '2026-05-04T00:00:00Z',
  raw: {}
};

const baseMessage: PmaChatMessage = {
  id: 'msg-1',
  chatId: 'chat-1',
  role: 'assistant',
  text: 'Created a PMA ticket and started the run.',
  createdAt: '2026-05-04T00:00:10Z',
  status: 'running',
  artifacts: [],
  raw: {}
};

const baseArtifact: SurfaceArtifact = {
  id: 'artifact-1',
  kind: 'test_result',
  title: 'Frontend checks',
  summary: 'Typecheck passed.',
  url: null,
  createdAt: '2026-05-04T00:00:30Z',
  raw: {}
};

const baseProgress: PmaRunProgress = {
  id: 'run-1',
  chatId: 'chat-1',
  status: 'running',
  phase: 'testing',
  guidance: 'Running frontend checks.',
  queueDepth: 1,
  elapsedSeconds: 95,
  idleSeconds: 2,
  lastEventId: 7,
  lastEventAt: '2026-05-04T00:00:30Z',
  events: [baseArtifact],
  raw: {}
};

describe('PMA chat view helpers', () => {
  it('filters chat list by status and scoped search text', () => {
    const chats: PmaChatSummary[] = [
      baseChat,
      { ...baseChat, id: 'chat-2', title: 'Waiting approval', status: 'waiting', repoId: 'billing' },
      { ...baseChat, id: 'chat-3', title: 'Finished work', status: 'done', ticketId: 'TICKET-099' }
    ];

    expect(filterPmaChats(chats, 'active', '')).toHaveLength(1);
    expect(filterPmaChats(chats, 'waiting', 'billing')).toMatchObject([{ id: 'chat-2' }]);
    expect(filterPmaChats(chats, 'done', 'ticket-099')).toMatchObject([{ id: 'chat-3' }]);
    expect(summarizeFilterCounts(chats)).toEqual({ all: 3, active: 1, waiting: 1, done: 1 });
  });

  it('keeps the selected chat when still present and falls back otherwise', () => {
    expect(chooseActiveChatId([baseChat], 'chat-1')).toBe('chat-1');
    expect(chooseActiveChatId([baseChat], 'missing')).toBe('chat-1');
    expect(chooseActiveChatId([], 'missing')).toBeNull();
  });

  it('builds active chat cards for messages, tickets, compact progress, streaming, and artifacts', () => {
    const cards = buildPmaCards([baseMessage], baseProgress, baseChat, [baseArtifact]);

    expect(cards.map((card) => card.kind)).toEqual([
      'message',
      'ticket',
      'progress',
      'streaming',
      'artifact',
      'artifact'
    ]);
  });

  it('derives compact progress and relative timestamps', () => {
    expect(progressPercent(baseChat, baseProgress)).toBe(64);
    expect(progressPercent({ ...baseChat, progressPercent: 41 }, baseProgress)).toBe(41);
    expect(formatRelativeTime('2026-05-04T00:00:00Z', new Date('2026-05-04T00:03:00Z'))).toBe('3m ago');
  });
});
