import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { render } from 'svelte/server';
import { afterEach, describe, expect, it } from 'vitest';
import {
  type ChatIndexRow,
  type TicketRunGroup,
  type ProjectionCursor
} from '$lib/api/readModelContracts';
import { readModelEntityStore } from '$lib/data/readModelStore';
import Page from './[[chatId]]/+page.svelte';

describe('/chats page', () => {
  afterEach(() => {
    readModelEntityStore.reset();
  });

  function chatDetailPageSource(): string {
    return readFileSync(
      fileURLToPath(new URL('./[[chatId]]/+page.svelte', import.meta.url)),
      'utf8'
    );
  }

  it('preserves route-selected agent drafts when creating a scoped local draft', () => {
    const source = chatDetailPageSource();
    const createChatBody = source.match(
      /async function createChat[\s\S]*?\n  async function sendMessage/
    )?.[0];

    expect(createChatBody).toContain('if (!options.preserveSelectedScope)');
    expect(createChatBody).toMatch(
      /if \(!options\.preserveSelectedScope\) \{[\s\S]*newChatKind = 'pma';[\s\S]*\}/
    );
    expect(createChatBody).not.toMatch(/detailMode = 'detail';\s*newChatKind = 'pma';/);
  });

  it('delegates terminal snapshot queue reconciliation to the live projection service', () => {
    const pageSource = chatDetailPageSource();
    const serviceSource = readFileSync(
      fileURLToPath(new URL('../../lib/application/chatDetailLiveProjection.ts', import.meta.url)),
      'utf8'
    );
    expect(serviceSource).toContain("if (event.kind === 'transcript_snapshot')");
    expect(serviceSource).toContain('this.refreshedTerminalTurnId = nextProgress.id');
    expect(serviceSource).toContain('this.scheduleQueueRefresh(chatId, this.queueRefreshDelayMs)');
    expect(serviceSource).toContain('async refreshQueue(chatId: string)');
    expect(pageSource).toContain('createChatDetailLiveProjection');
    expect(pageSource).not.toContain('webApi.pma.getTranscript');
    expect(pageSource).not.toContain('webApi.pma.getQueue');
    expect(pageSource).not.toContain('openChatTranscriptEventSource');
  });

  it('keeps migrated PMA transcript, stream, queue, send, and normalization calls out of the page', () => {
    const pageSource = chatDetailPageSource();
    const forbiddenTokens = [
      ['webApi.pma.getTranscript', 'transcript loading belongs in chatDetailLiveProjection'],
      ['webApi.pma.getQueue', 'queue refresh belongs in chatDetailLiveProjection'],
      ['openChatTranscriptEventSource', 'stream wiring belongs in chatDetailLiveProjection'],
      ['shouldUseChatTranscriptStream', 'stream selection belongs in chatDetailLiveProjection'],
      ['mapChatTranscriptRows', 'transcript normalization belongs in chatDetailLiveProjection'],
      ['mergePmaProgressUpdate', 'progress normalization belongs in chatDetailLiveProjection'],
      [
        'mergeTranscriptSnapshotWithPendingOptimistic',
        'snapshot repair belongs in chatDetailLiveProjection'
      ],
      ['executePmaChatCommandPlan', 'send execution belongs in chatSendController'],
      ['buildOptimisticQueuedTurn', 'optimistic queue reconciliation belongs in chatSendController'],
      [
        'buildOptimisticUserTranscriptCard',
        'optimistic transcript reconciliation belongs in chatSendController'
      ],
      ['queueContainsCommittedClientTurn', 'queue reconciliation belongs in chatSendController'],
      [
        'transcriptContainsCommittedUserRow',
        'transcript reconciliation belongs in chatSendController'
      ],
      ['webApi.pma.cancelQueuedTurn', 'queued turn mutation belongs in chatSendController'],
      ['webApi.pma.clearQueue', 'queue clearing belongs in chatSendController'],
      ['webApi.pma.startChatWithMessage', 'draft first-send execution belongs in chatSendController'],
      ['webApi.pma.createChat', 'chat creation planning belongs in chatSendController'],
      ['webApi.pma.forkThread', 'chat fork planning belongs in chatSendController'],
      ['webApi.pma.uploadInboxFile', 'attachment upload during send belongs in chatSendController']
    ] as const;

    const violations = forbiddenTokens
      .filter(([token]) => pageSource.includes(token))
      .map(([token, reason]) => `${token}: ${reason}`);

    expect(violations).toEqual([]);
  });

  it('renders filters, chat list shell, and composer affordances without global memory controls', () => {
    const { body } = render(Page);

    expect(body).toContain('Chats workspace');
    expect(body).not.toContain('memory-toggle-button');
    expect(body).toContain('+ PMA');
    expect(body).toContain('chat-list');
    expect(body).toContain('Waiting');
    expect(body).toContain('Active');
    expect(body).not.toContain('Done');
    expect(body).toContain('Search chats');
    expect(body).toContain('Create or select a chat');
    expect(body).toContain('Attach files');
  });

  it('renders cached chat rows instead of the skeleton while the index cursor is still missing', () => {
    readModelEntityStore.upsertChatIndexRows([chatIndexRow()]);

    const { body } = render(Page);

    expect(body).toContain('Chat One');
    expect(body).not.toContain('Loading chats');
  });

  it('renders active rebound rows from chat-index even when raw lifecycle fields are stale archived', () => {
    readModelEntityStore.upsertChatIndexRows([
      {
        chatId: 'discord-rebound-active',
        surface: 'discord',
        title: 'Discord Rebound Active',
        lifecycle: 'archived',
        runtimeStatus: 'running',
        archiveState: 'active',
        status: 'running',
        unreadCount: 0,
        lastActivityAt: '2026-05-11T12:00:00Z',
        primarySurface: { surface_kind: 'pma', lifecycle: 'running' },
        surfaceBindings: [{ surface_kind: 'discord', surface_key: 'channel-1', lifecycle: 'archived' }]
      },
      {
        chatId: 'discord-old-archived',
        surface: 'discord',
        title: 'Discord Old Archived',
        lifecycle: 'archived',
        archiveState: 'archived',
        status: 'archived',
        unreadCount: 0,
        lastActivityAt: '2026-05-10T12:00:00Z'
      }
    ]);

    const { body } = render(Page);

    expect(body).toContain('Discord Rebound Active');
    expect(body).toContain('Discord');
    expect(body).not.toContain('Discord Old Archived');
  });

  it('uses backend unread counters when the first chat window is smaller than the full result set', () => {
    readModelEntityStore.applyChatIndexSnapshot({
      cursor: projectionCursor(),
      window: {
        limit: 50,
        nextCursor: 'next-page',
        previousCursor: null,
        totalEstimate: 200,
        totalIsExact: false
      },
      filter: 'all',
      query: null,
      rows: [chatIndexRow()],
      groups: [],
      counters: { total: 200, waiting: 0, running: 1, unread: 7, archived: 0 }
    });

    const { body } = render(Page);

    expect(body).toContain('Unread');
    expect(body).toContain('7');
  });

  it('renders ticket-run progress from backend aggregates instead of row status inference', () => {
    const rows = [
      ticketFlowRow('done-1', 'TICKET-001', 'idle', 'ticket-run:run-1'),
      ticketFlowRow('done-2', 'TICKET-002', 'idle', 'ticket-run:run-1'),
      ticketFlowRow('done-3', 'TICKET-003', 'idle', 'ticket-run:run-1'),
      ticketFlowRow('running-1', 'TICKET-004', 'running', 'ticket-run:run-1'),
      ticketFlowRow('running-2', 'TICKET-005', 'running', 'ticket-run:run-1'),
      {
        ...chatIndexRow(),
        chatId: 'generic-complete',
        title: 'Generic completed chat',
        status: 'idle' as const,
        runtimeStatus: 'completed',
        ticketId: null,
        runId: null,
        groupId: null
      }
    ];
    readModelEntityStore.applyChatIndexSnapshot({
      cursor: projectionCursor(),
      rows,
      groups: [],
      counters: { total: rows.length, waiting: 0, running: 2, unread: 0, archived: 0 }
    });
    readModelEntityStore.applyChatIndexSnapshot(
      {
        cursor: projectionCursor(2),
        rows: rows.slice(0, 5),
        groups: [ticketRunGroup()],
        counters: { total: 5, waiting: 0, running: 2, unread: 0, archived: 0 }
      },
      { filter: 'ticket_runs', groupBy: 'ticket_run', limit: 50 }
    );

    const { body } = render(Page);

    expect(body).toContain('2 active');
    expect(body).toContain('3/5 done');
    expect(body).toContain('Generic completed chat');
    expect(body).not.toContain('4/6 done');
  });

  it('registers ticket-run aggregate refresh as a chat-index companion window', () => {
    const source = chatDetailPageSource();

    expect(source).toContain('chatIndexSession.setCompanionRequests([ticketRunGroupRequest])');
    expect(source).toMatch(/chatIndexSession\.setCompanionRequests\(\[ticketRunGroupRequest\]\);[\s\S]*chatIndexSession\.start\(\);/);
    expect(source).toMatch(/chatIndexSession\.stop\(\);[\s\S]*chatIndexSession\.setCompanionRequests\(\[\]\);/);
  });

  it('does not render legacy ticket-run grouping when current snapshots have no backend groups', () => {
    const rows = [
      ticketFlowRow('done-1', 'TICKET-001', 'idle', null, { ticketDone: true, ticketStatus: 'done' }),
      ticketFlowRow('running-1', 'TICKET-002', 'running', null, { ticketDone: false, ticketStatus: 'running' })
    ];
    readModelEntityStore.applyChatIndexSnapshot({
      cursor: projectionCursor(),
      rows,
      groups: [],
      counters: { total: rows.length, waiting: 0, running: 1, unread: 0, archived: 0 }
    });
    readModelEntityStore.applyChatIndexSnapshot(
      {
        cursor: projectionCursor(2),
        rows,
        groups: [],
        counters: { total: 2, waiting: 0, running: 1, unread: 0, archived: 0 }
      },
      { filter: 'ticket_runs', groupBy: 'ticket_run', limit: 50 }
    );

    const { body } = render(Page);

    expect(body).toContain('TICKET-001');
    expect(body).toContain('TICKET-002');
    expect(body).not.toContain('1/2 done');
  });
});

function chatIndexRow(): ChatIndexRow {
  return {
    chatId: 'chat-1',
    surface: 'pma',
    title: 'Chat One',
    status: 'running',
    unreadCount: 0,
    lastActivityAt: '2026-05-11T12:00:00Z',
    repoId: null,
    worktreeId: null,
    ticketId: null,
    runId: null,
    agent: 'codex',
    chatKind: 'pma',
    model: 'gpt-5.5',
    groupId: null
  };
}

function projectionCursor(sequence = 1): ProjectionCursor {
  return {
    value: `test:${sequence}`,
    sequence,
    source: 'test',
    issuedAt: '2026-05-11T12:00:00Z'
  };
}

function ticketFlowRow(
  chatId: string,
  ticketId: string,
  status: ChatIndexRow['status'],
  groupId: string | null,
  overrides: Partial<ChatIndexRow> = {}
): ChatIndexRow {
  return {
    ...chatIndexRow(),
    chatId,
    title: ticketId,
    status,
    runtimeStatus: status === 'idle' ? 'completed' : status,
    ticketId,
    runId: 'run-1',
    worktreeId: 'wt-1',
    flowType: 'ticket_flow',
    groupId,
    ticketDone: status === 'idle' ? null : false,
    ticketStatus: status === 'running' ? 'running' : status === 'waiting' ? 'waiting' : 'unknown',
    ...overrides
  };
}

function ticketRunGroup(): TicketRunGroup {
  return {
    kind: 'ticket_run_group',
    groupId: 'ticket-run:run-1',
    runId: 'run-1',
    scopeKind: 'worktree',
    scopeId: 'wt-1',
    label: 'Ticket run run-1',
    status: 'running',
    totalCount: 5,
    doneCount: 3,
    runningCount: 2,
    waitingCount: 0,
    failedCount: 0,
    unreadCount: 0,
    updatedAt: '2026-05-11T12:00:00Z'
  };
}
