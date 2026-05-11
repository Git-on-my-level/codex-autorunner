import { describe, expect, it } from 'vitest';
import type { ChatIndexRow, RepoWorktreeRuntimeSnapshot, RepoWorktreeTopologySnapshot } from '$lib/api/readModelContracts';
import { ReadModelEntityStore } from './readModelStore';
import {
  chatIndexRowToPmaChatSummary,
  pmaChatSummaryToChatIndexRow,
  selectPmaChats,
  selectRepoSummaries,
  selectWorktreeSummaries
} from './readModelViewModels';

const now = '2026-05-11T12:00:00Z';
const cursor = { value: 'c:1', sequence: 1, source: 'test', issuedAt: now };

describe('read model view-model selectors', () => {
  it('maps chat rows to existing PMA chat summaries', () => {
    const row: ChatIndexRow = {
      chatId: 'chat-1',
      surface: 'discord',
      title: 'Discord thread',
      status: 'waiting',
      unreadCount: 2,
      lastActivityAt: now,
      repoId: 'repo-1',
      worktreeId: null,
      ticketId: 'TICKET-005',
      runId: 'run-1',
      agent: 'codex',
      model: 'gpt-5.5',
      groupId: 'ticket:TICKET-005'
    };

    const summary = chatIndexRowToPmaChatSummary(row);
    expect(summary.id).toBe('chat-1');
    expect(summary.status).toBe('waiting');
    expect(summary.isTicketFlow).toBe(true);
    expect(summary.raw.surface_kind).toBe('discord');
    expect(pmaChatSummaryToChatIndexRow(summary).chatId).toBe('chat-1');
  });

  it('selects chat and repo/worktree summaries from normalized state', () => {
    const store = new ReadModelEntityStore();
    store.applyChatIndexSnapshot({
      cursor,
      rows: [
        {
          chatId: 'chat-1',
          surface: 'pma',
          title: 'Chat',
          status: 'running',
          unreadCount: 0,
          lastActivityAt: now
        }
      ],
      groups: [],
      counters: { total: 1, waiting: 0, running: 1, unread: 0, archived: 0 }
    });
    store.applyRepoWorktreeTopologySnapshot({
      contractVersion: 'web-read-models.v1',
      kind: 'repo_worktree.topology.snapshot',
      cursor,
      window: { limit: 50, totalIsExact: true },
      repos: [{ repoId: 'repo-1', label: 'Repo', path: '/repo', archived: false, childWorktreeIds: ['wt-1'] }],
      worktrees: [{ worktreeId: 'wt-1', repoId: 'repo-1', label: 'Feature', path: '/repo-wt', branch: 'feature', archived: false }],
      repair: {
        snapshotRoute: '/hub/read-models/repo-worktree/topology',
        cursorQueryParam: 'after',
        gapEventType: 'projection.cursor_gap',
        behavior: 'repair_snapshot_required'
      }
    } satisfies RepoWorktreeTopologySnapshot);
    store.applyRepoWorktreeRuntimeSnapshot({
      contractVersion: 'web-read-models.v1',
      kind: 'repo_worktree.runtime.snapshot',
      cursor: { ...cursor, sequence: 2 },
      window: { limit: 50, totalIsExact: true },
      runtime: [
        {
          entityKind: 'repo',
          entityId: 'repo-1',
          activeRunId: 'run-1',
          activeRunStatus: 'running',
          waitingTicketCount: 1,
          runningTicketCount: 0,
          chatCount: 1,
          cleanupBlockers: []
        }
      ],
      repair: {
        snapshotRoute: '/hub/read-models/repo-worktree/runtime',
        cursorQueryParam: 'after',
        gapEventType: 'projection.cursor_gap',
        behavior: 'repair_snapshot_required'
      }
    } satisfies RepoWorktreeRuntimeSnapshot);

    expect(selectPmaChats(store.snapshot())[0].status).toBe('running');
    expect(selectRepoSummaries(store.snapshot())[0].activeRuns).toBe(1);
    expect(selectWorktreeSummaries(store.snapshot())[0].repoId).toBe('repo-1');
  });
});
