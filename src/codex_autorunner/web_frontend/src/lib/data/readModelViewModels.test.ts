import { describe, expect, it } from 'vitest';
import type { ChatIndexRow, RepoWorktreeRuntimeSnapshot, RepoWorktreeTopologySnapshot } from '$lib/api/readModelContracts';
import { ReadModelEntityStore } from './readModelStore';
import {
  chatIndexRowToPmaChatSummary,
  legacyChatIndexRecordToChatIndexRow,
  pmaChatSummaryToChatIndexRow,
  selectPmaChats,
  selectRepoSummaries,
  selectTicketRunGroups,
  selectWorktreeSummaries
} from './readModelViewModels';
import { chatSurfaceFilterToken, filterPmaChats } from '$lib/viewModels/pmaChat';
import { buildRepoWorktreeIndexViewModel } from '$lib/viewModels/repoWorktree';

const now = '2026-05-11T12:00:00Z';
const cursor = { value: 'c:1', sequence: 1, source: 'test', issuedAt: now };

describe('read model view-model selectors', () => {
  it('treats archived chat rows as archived for sidebar filtering', () => {
    const row: ChatIndexRow = {
      chatId: 'tf-archived',
      surface: 'pma',
      title: 'ticket-flow:codex',
      status: 'archived',
      unreadCount: 0,
      lastActivityAt: now
    };
    expect(row.status).toBe('archived');
    const summary = chatIndexRowToPmaChatSummary(row);
    expect(summary.lifecycleStatus).toBe('archived');
  });

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
      agentProfile: 'm4-pma',
      chatKind: 'coding_agent',
      model: 'gpt-5.5',
      groupId: 'ticket:TICKET-005'
    };

    const summary = chatIndexRowToPmaChatSummary(row);
    expect(summary.id).toBe('chat-1');
    expect(summary.status).toBe('waiting');
    expect(summary.chatKind).toBe('coding_agent');
    expect(summary.agentProfile).toBe('m4-pma');
    expect(summary.isTicketFlow).toBe(true);
    expect(summary.raw.surface_kind).toBe('discord');
    expect(summary.raw.agent_profile).toBe('m4-pma');
    expect(pmaChatSummaryToChatIndexRow(summary).chatKind).toBe('coding_agent');
    expect(pmaChatSummaryToChatIndexRow(summary).agentProfile).toBe('m4-pma');
    expect(pmaChatSummaryToChatIndexRow(summary).chatId).toBe('chat-1');
    expect(pmaChatSummaryToChatIndexRow(summary).unreadCount).toBe(2);
  });

  it('keeps active rebound chat-index rows visible despite stale raw surface archive fields', () => {
    const row: ChatIndexRow = {
      chatId: 'discord-rebound-active',
      surface: 'discord',
      title: 'Discord Ops',
      lifecycle: 'archived',
      runtimeStatus: 'running',
      archiveState: 'active',
      status: 'running',
      unreadCount: 0,
      lastActivityAt: now,
      primarySurface: {
        surface_kind: 'pma',
        lifecycle: 'running'
      },
      surfaceBindings: [
        {
          surface_kind: 'discord',
          surface_key: 'channel-1',
          lifecycle: 'archived'
        }
      ]
    };

    const summary = chatIndexRowToPmaChatSummary(row);

    expect(summary.lifecycleStatus).toBe('active');
    expect(pmaChatSummaryToChatIndexRow(summary).status).toBe('running');
    expect(filterPmaChats([summary], 'all', '').map((chat) => chat.id)).toEqual(['discord-rebound-active']);
    expect(filterPmaChats([summary], chatSurfaceFilterToken('discord'), '').map((chat) => chat.id)).toEqual([
      'discord-rebound-active'
    ]);
    expect(filterPmaChats([summary], 'archived', '').map((chat) => chat.id)).toEqual([]);
  });

  it('uses backend effective status before raw lifecycle detail', () => {
    const row = legacyChatIndexRecordToChatIndexRow({
      row_id: 'row-effective',
      title: 'Effective row',
      lifecycle: 'running',
      runtime_status: 'running',
      queue_depth: 5,
      effective_status: 'idle'
    });

    expect(row.status).toBe('idle');
    expect(row.effectiveStatus).toBe('idle');
    expect(chatIndexRowToPmaChatSummary(row).status).toBe('idle');
  });

  it('preserves unread counts through PMA chat row conversion', () => {
    const row: ChatIndexRow = {
      chatId: 'chat-1',
      surface: 'pma',
      title: 'Chat',
      status: 'idle',
      unreadCount: 3,
      lastActivityAt: now
    };

    const summary = chatIndexRowToPmaChatSummary(row);

    expect(pmaChatSummaryToChatIndexRow(summary).unreadCount).toBe(3);
  });

  it('flags generic ticket-flow rows as ticket flows for grouping', () => {
    const summary = chatIndexRowToPmaChatSummary({
      chatId: 'chat-ticket-flow',
      surface: 'pma',
      title: 'ticket-flow:hermes@m4-pma',
      status: 'idle',
      unreadCount: 0,
      lastActivityAt: now,
      repoId: 'repo-1',
      worktreeId: 'repo-1--ticket-flow',
      groupId: 'ticket:chat-ticket-flow'
    });

    expect(summary.isTicketFlow).toBe(true);
    expect(summary.repoId).toBe('repo-1');
    expect(summary.worktreeId).toBe('repo-1--ticket-flow');
  });

  it('derives worktree ids from legacy resource owner fields', () => {
    const summary = chatIndexRowToPmaChatSummary(
      legacyChatIndexRecordToChatIndexRow({
        row_id: 'row-1',
        surface: 'discord',
        title: 'Discord channel',
        lifecycle: 'bound',
        runtime_status: 'idle',
        repo_id: 'repo-1',
        resource_kind: 'worktree',
        resource_id: 'repo-1--discord-1'
      })
    );

    expect(summary.repoId).toBe('repo-1');
    expect(summary.worktreeId).toBe('repo-1--discord-1');
  });

  it('uses explicit visible-message clocks for chat row recency before lifecycle updates', () => {
    const row = legacyChatIndexRecordToChatIndexRow({
      row_id: 'thread:chat-clock',
      managed_thread_id: 'chat-clock',
      surface: 'pma',
      title: 'Visible message',
      lifecycle: 'bound',
      runtime_status: 'idle',
      last_visible_message_at: '2026-05-11T00:01:00Z',
      last_sort_activity_at: '2026-05-11T00:01:00Z',
      last_lifecycle_update_at: '2026-05-11T00:05:00Z',
      last_internal_update_at: '2026-05-11T00:05:00Z',
      debug: {
        activity: {
          selected: '2026-05-11T00:01:00Z',
          selected_source: 'last_visible_message_at'
        }
      },
      updated_at: '2026-05-11T00:05:00Z',
      created_at: '2026-05-11T00:00:00Z'
    });

    expect(row.lastActivityAt).toBe('2026-05-11T00:01:00Z');
    expect(row.lastVisibleMessageAt).toBe('2026-05-11T00:01:00Z');
    expect(row.lastLifecycleUpdateAt).toBe('2026-05-11T00:05:00Z');
    const summary = chatIndexRowToPmaChatSummary(row);
    expect(summary.updatedAt).toBe('2026-05-11T00:01:00Z');
    expect(summary.raw.debug).toEqual(row.debug);
    expect(pmaChatSummaryToChatIndexRow(summary).debug).toEqual(row.debug);
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
      repos: [
        {
          repoId: 'repo-1',
          label: 'Repo',
          path: '/repo',
          archived: false,
          isPinned: true,
          childWorktreeIds: ['wt-1'],
          worktreeSetupCommands: ['make setup']
        }
      ],
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
    expect(selectRepoSummaries(store.snapshot())[0].raw.worktree_setup_commands).toEqual(['make setup']);
    expect(selectRepoSummaries(store.snapshot())[0].raw.is_pinned).toBe(true);
    expect(selectWorktreeSummaries(store.snapshot())[0].repoId).toBe('repo-1');
  });

  it('projects optimistic repo pins immediately and rolls them back by reconciliation id', () => {
    const store = new ReadModelEntityStore();
    store.applyRepoWorktreeTopologySnapshot({
      contractVersion: 'web-read-models.v1',
      kind: 'repo_worktree.topology.snapshot',
      cursor,
      window: { limit: 50, totalIsExact: true },
      repos: [
        { repoId: 'repo-a', label: 'Repo A', path: '/repo-a', archived: false, childWorktreeIds: [] },
        { repoId: 'repo-b', label: 'Repo B', path: '/repo-b', archived: false, childWorktreeIds: [] }
      ],
      worktrees: [],
      repair: {
        snapshotRoute: '/hub/read-models/repo-worktree/topology',
        cursorQueryParam: 'after',
        gapEventType: 'projection.cursor_gap',
        behavior: 'repair_snapshot_required'
      }
    } satisfies RepoWorktreeTopologySnapshot);

    store.optimisticRepoPin('repo-b', true, 'pin-repo-b');
    const pinnedIndex = buildRepoWorktreeIndexViewModel({
      repos: selectRepoSummaries(store.snapshot()),
      worktrees: selectWorktreeSummaries(store.snapshot()),
      runs: [],
      chats: [],
      tickets: [],
      artifacts: [],
      ticketsListLoaded: false
    });

    expect(pinnedIndex.rows.map((row) => row.id)).toEqual(['repo-b', 'repo-a']);
    expect(pinnedIndex.rows[0].isPinned).toBe(true);

    store.revertOptimisticMutation('pin-repo-b');
    const reverted = selectRepoSummaries(store.snapshot()).find((repo) => repo.id === 'repo-b');
    expect(reverted?.raw.is_pinned).toBe(false);
  });

  it('preserves backend ticket-flow fields and ticket-run groups', () => {
    const store = new ReadModelEntityStore();
    const request = { filter: 'ticket_runs' as const, groupBy: 'ticket_run' as const, limit: 50 };
    store.applyChatIndexSnapshot({
      cursor,
      rows: [
        {
          chatId: 'ticket-chat-1',
          surface: 'pma',
          title: 'Ticket chat',
          status: 'idle',
          runtimeStatus: 'completed',
          unreadCount: 0,
          lastActivityAt: now,
          repoId: 'repo-1',
          worktreeId: 'wt-1',
          ticketId: 'TICKET-001',
          ticketPath: '.codex-autorunner/tickets/TICKET-001.md',
          ticketDone: true,
          ticketStatus: 'done',
          runId: 'run-1',
          groupId: 'run:run-1',
          flowType: 'ticket_flow'
        }
      ],
      groups: [
        {
          kind: 'ticket_run_group',
          groupId: 'run:run-1',
          runId: 'run-1',
          scopeKind: 'worktree',
          scopeId: 'wt-1',
          label: 'run:run-1',
          status: 'running',
          totalCount: 5,
          doneCount: 3,
          runningCount: 2,
          waitingCount: 0,
          failedCount: 0,
          unreadCount: 0,
          updatedAt: now
        }
      ],
      counters: { total: 5, waiting: 0, running: 2, unread: 0, archived: 0 },
      filter: 'ticket_runs',
      window: { limit: 50, totalIsExact: true }
    }, request);

    const summary = selectPmaChats(store.snapshot(), request)[0];
    expect(summary.ticketDone).toBe(true);
    expect(summary.ticketStatus).toBe('done');
    expect(selectTicketRunGroups(store.snapshot(), request)[0].doneCount).toBe(3);
  });
});
