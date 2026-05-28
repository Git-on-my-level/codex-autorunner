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
import Page from './+page.svelte';

describe('/chats page', () => {
  afterEach(() => {
    readModelEntityStore.reset();
  });

  function chatDetailPageSource(): string {
    return readFileSync(
      fileURLToPath(new URL('./+page.svelte', import.meta.url)),
      'utf8'
    );
  }

  it('uses remembered new-chat preferences unless a route-selected draft is being created', () => {
    const source = chatDetailPageSource();
    const createChatBody = source.match(
      /async function createChat[\s\S]*?\n  async function sendMessage/
    )?.[0];

    expect(createChatBody).toContain('if (!options.preserveSelectedScope)');
    expect(createChatBody).toContain('applyLastNewChatPreference()');
    expect(createChatBody).toContain('if (options.preserveSelectedKind)');
    expect(createChatBody).toContain('ensureCodingAgentScope()');
    expect(createChatBody).toContain('persistCurrentNewChatPreference();');
    expect(createChatBody).not.toMatch(/detailMode = 'detail';\s*newChatKind = 'pma';/);
  });

  it('preserves explicit slash-command chat mode when starting a new draft', () => {
    const source = chatDetailPageSource();
    const slashCommandBody = source.match(
      /async function executeSlashCommand[\s\S]*?\n  function addFiles/
    )?.[0];

    expect(slashCommandBody).toBeTruthy();
    expect(slashCommandBody).toContain("await createChat({ preserveSelectedKind: true });");
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

  it('delegates chat detail transcript and composer display derivation to the application read model', () => {
    const pageSource = chatDetailPageSource();
    const architectureSource = readFileSync(
      fileURLToPath(new URL('../../lib/application/pmaChatArchitecture.ts', import.meta.url)),
      'utf8'
    );

    expect(pageSource).toContain('buildChatDetailDisplayReadModel');
    expect(architectureSource).toContain('export function buildChatDetailDisplayReadModel');
    expect(architectureSource).toContain('function shouldShowChatDetailStatusBar');
    expect(architectureSource).toContain('function shouldQueueComposerDraft');
    expect(pageSource).not.toContain('visibleChatDetailTranscriptCards(transcriptCards, queuedTurns)');
    expect(pageSource).not.toContain('compactChatTranscriptCards(activeCards)');
    expect(pageSource).not.toContain("streamState === 'connecting' || streamState === 'interrupted'");
  });

  it('uses targeted live regions instead of transcript-wide live announcements', () => {
    const pageSource = chatDetailPageSource();

    expect(pageSource).not.toContain('aria-live="off"');
    expect(pageSource).toContain('srStatusAnnouncement');
    expect(pageSource).toContain('srAlertAnnouncement');
    expect(pageSource).toContain("role={streamState === 'interrupted' ? 'alert' : 'status'}");
  });

  it('does not mark active chat updates read without an explicit read action', () => {
    const pageSource = chatDetailPageSource();

    expect(pageSource).toContain('onMarkRead: markActiveChatRead');
    expect(pageSource).toContain('markSessionChatRead(lastSeenMap, activeChatId, chats, localDraftChat)');
    expect(pageSource).not.toContain('markActiveSummaryRead');
    expect(pageSource).not.toContain('read-active:');
  });

  it('projects committed chat URLs without SvelteKit navigation', () => {
    const source = chatDetailPageSource();
    const syncCommittedBody = source.match(
      /async function syncCommittedDetailUrl[\s\S]*?\n  async function refreshActive/
    )?.[0];

    expect(syncCommittedBody).toContain('await replaceDetailUrl(detailId);');
    expect(source).toContain("history.replaceState(history.state, '', href(target));");
    expect(syncCommittedBody).not.toContain('goto(');
    expect(syncCommittedBody).not.toContain('pendingCommittedDetailUrlChatId');
  });

  it('pushes the selected chat URL and updates the cached route before activating the detail controller', () => {
    const source = chatDetailPageSource();
    const selectChatBody = source.match(
      /async function selectChat[\s\S]*?\n  function chatIdFromRowEvent/
    )?.[0];

    expect(selectChatBody).toBeTruthy();
    expect(selectChatBody).toMatch(
      /await syncCommittedDetailUrl\(chatId, \{ mode: 'push' \}\);[\s\S]*?pageController\.setRoute\(currentRouteSnapshot\(\)\);[\s\S]*?await pageController\.selectChat\(chatId, \{ syncUrl: true \}\);/
    );
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
    expect(body).toContain('+ New');
    expect(body).toContain('chat-list');
    expect(body).toContain('Status');
    expect(body).not.toContain('Done');
    expect(body).toContain('Search chats');
    expect(body).toContain('Create or select a chat');
    expect(body).toContain('Attach files');
  });

  it('does not re-apply chat search locally after requesting a backend search window', () => {
    const pageSource = chatDetailPageSource();

    expect(pageSource).toContain('query: search.trim() || null');
    expect(pageSource).toContain("filterChatEntries(chatListEntries, statusFilter === 'drafts' ? 'all' : statusFilter, '', lastSeenMap)");
  });

  it('does not let remembered picker models describe existing chats with unknown runtime models', () => {
    const pageSource = chatDetailPageSource();

    expect(pageSource).toContain("mode: 'chat-bound'");
    expect(pageSource).toContain("allowEmptyModel: mode === 'chat-bound'");
    expect(pageSource).toContain("rememberedModel: mode === 'draft' ? getLastModelForAgent(agentId) : null");
    expect(pageSource).toContain('runtimeModelIsExplicitlyUnknown');
    expect(pageSource).toContain('model unknown');
    expect(pageSource).not.toContain('activeChat?.model ?? selectedModel');
    expect(pageSource).not.toContain('resolved.model ?? selectedModel');
  });

  it('renders unknown for existing chat rows with missing projected models', () => {
    readModelEntityStore.applyChatIndexSnapshot({
      cursor: projectionCursor(),
      rows: [
        {
          ...chatIndexRow(),
          chatId: 'chat-zai-unknown',
          title: 'Existing Z.ai chat',
          agent: 'zai-coding-plan',
          model: null,
          modelSource: 'unknown',
          runtimeSource: 'unknown',
          runtime: {
            stage: 'unknown',
            source: 'unknown',
            runtimeSource: 'unknown',
            model: null,
            modelUnknown: true,
            reasoningUnknown: true,
            agentUnknown: false,
            profileUnknown: true,
            providerUnknown: true,
            backendRuntimeUnknown: true,
            modelSource: 'unknown',
            reasoningSource: 'unknown'
          }
        }
      ],
      groups: [],
      counters: { total: 1, waiting: 0, running: 1, unread: 0, archived: 0 }
    });

    const { body } = render(Page);

    expect(body).toContain('Existing Z.ai chat');
    expect(body).toContain('model unknown');
    expect(body).not.toContain('glm-5v-turbo');
  });

  it('syncs list filters through the URL and toggles status chips like facets', () => {
    const pageSource = chatDetailPageSource();

    expect(pageSource).toContain('buildChatsListHref');
    expect(pageSource).toContain('afterNavigate');
    expect(pageSource).toContain('toggleChatStatusFilter');
    expect(pageSource).toContain('shouldShowChatStatusFilterPill');
    expect(pageSource).toContain("item !== 'archived'");
    expect(pageSource).toContain('chat-filter-archive-toggle');
  });

  it('keeps composer drafts keyed by chat and treats draft filtering as a local overlay', () => {
    const pageSource = chatDetailPageSource();

    expect(pageSource).toContain('loadChatDraftRecords');
    expect(pageSource).toContain('setChatDraftText(chatDraftRecords, chatId, value, chatSummaryForId(chatId))');
    expect(pageSource).toContain("statusFilter === 'drafts' ? filteredDraftChats");
    expect(pageSource).toContain("filterPmaChats(source, 'drafts', search, lastSeenMap)");
    expect(pageSource).toContain("facets?.category !== categoryFilter");
    expect(pageSource).toContain("filter === 'drafts' ? 'all' : filter");
    expect(pageSource).toContain("filterChatEntries(chatListEntries, statusFilter === 'drafts' ? 'all' : statusFilter");
  });

  it('clears slash new/reset commands before switching to a replacement draft chat', () => {
    const pageSource = chatDetailPageSource();
    const slashBody = pageSource.match(
      /async function executeSlashCommand[\s\S]*?\n  function handleComposerKeydown/
    )?.[0];

    expect(slashBody).toBeTruthy();
    expect(slashBody).toContain("if (spec.id === 'new')");
    expect(slashBody).toContain("if (spec.id === 'newt')");
    expect(slashBody).toContain("if (spec.id === 'reset')");
    expect(slashBody).toMatch(/if \(spec\.id === 'new'\)[\s\S]*?clearSlashDraft\(\);[\s\S]*?await createChat/);
    expect(slashBody).toMatch(/if \(spec\.id === 'newt'\)[\s\S]*?clearSlashDraft\(\);[\s\S]*?await createChat/);
    expect(slashBody).toMatch(/if \(spec\.id === 'reset'\)[\s\S]*?clearSlashDraft\(\);[\s\S]*?await createChat/);
  });

  it('uses contextual facet counts from the active chat-index window', () => {
    const pageSource = chatDetailPageSource();
    expect(pageSource).toContain('selectChatFacetCountsForWindow');
    expect(pageSource).toContain('contextualFacetCounts.category');
    expect(pageSource).not.toMatch(/readModelState\.chatFacetCounts\.category/);
  });

  it('renders filter summary and hides empty status pills via shared helpers', () => {
    const pageSource = chatDetailPageSource();
    expect(pageSource).toContain('shouldShowChatStatusFilterPill');
    expect(pageSource).toContain('chat-filter-summary');
    expect(pageSource).toContain('filterSummaryChips');
  });

  it('renders backend-counted facet filters and typed row badges', () => {
    readModelEntityStore.applyChatIndexSnapshot({
      cursor: projectionCursor(),
      window: { limit: 50, totalEstimate: 3, totalIsExact: true },
      filter: 'all',
      query: null,
      rows: [
        {
          ...chatIndexRow(),
          chatId: 'automation-discord',
          title: 'Automation Discord',
          facets: {
            category: 'automation',
            turnKinds: ['automation'],
            originKinds: ['automation'],
            transports: ['discord'],
            scopeKind: 'worktree',
            scopeId: 'wt-1',
            agentKind: 'coding_agent'
          }
        }
      ],
      groups: [],
      counters: { total: 3, waiting: 0, running: 0, unread: 0, archived: 0 },
      facetCounts: {
        category: { regular: 2, automation: 1 },
        turnKind: { automation: 1 },
        originKind: { automation: 1 },
        transport: { pma: 2, discord: 4 },
        scopeKind: { hub: 2, worktree: 1 },
        agentKind: { coding_agent: 1 }
      }
    });

    const { body } = render(Page);
    const pageSource = chatDetailPageSource();

    expect(body).toContain('More filters');
    expect(body).toContain('Discord');
    expect(body).not.toContain('PMA 2');
    expect(body).toContain('Automation Discord');
    expect(pageSource).toContain("chatCategoryLabel('regular')");
    expect(pageSource).toContain('CHAT_EXTERNAL_TRANSPORT_FILTERS');
    expect(pageSource).toContain('contextualFacetCounts.transport');
  });

  it('renders the agent-kind badge (PMA or Coding agent) in the active header', () => {
    const pageSource = chatDetailPageSource();
    const subtitleBody = pageSource.match(
      /<p class="chat-header-subtitle">[\s\S]*?<\/p>/
    )?.[0];

    expect(subtitleBody).toBeTruthy();
    expect(pageSource).toContain('pmaChatBadgeViews(activeChat, { showPmaAgent: false })');
    expect(subtitleBody).toContain('{#each activeChatBadges as badge}');
  });

  it('renders cached chat rows instead of the skeleton while the index cursor is still missing', () => {
    readModelEntityStore.upsertChatIndexRows([chatIndexRow()]);

    const { body } = render(Page);

    expect(body).toContain('Chat One');
    expect(body).not.toContain('Loading chats');
  });

  it('does not render short chat ID tags in chat list rows', () => {
    readModelEntityStore.upsertChatIndexRows([
      {
        ...chatIndexRow(),
        chatId: '51e8dc9a-chat-row',
        title: 'New coding agent chat',
        repoId: 'car-workspace'
      }
    ]);

    const { body } = render(Page);
    const pageSource = chatDetailPageSource();

    expect(body).toContain('New coding agent chat');
    expect(body).toContain('car-workspace');
    expect(body).not.toContain('#51e8dc');
    expect(pageSource).not.toContain('chat.id.slice(0, 6)');
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
        primarySurface: { surfaceKind: 'pma', surfaceKey: 'thread-1', lifecycle: 'running' },
        surfaceBindings: [{ surfaceKind: 'discord', surfaceKey: 'channel-1', lifecycle: 'archived' }]
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
      { facets: { categories: ['ticket_run'] }, groupBy: 'ticket_run', limit: 50 }
    );

    const { body } = render(Page);
    const pageSource = chatDetailPageSource();

    expect(body).toContain('More filters');
    expect(body).toContain('2 active');
    expect(body).toContain('3/5 done');
    expect(body).toContain('Generic completed chat');
    expect(body).not.toContain('4/6 done');
    expect(pageSource).toContain('ticketRunGroupCount');
  });

  it('registers ticket-run aggregate refresh as a chat-index companion window', () => {
    const source = chatDetailPageSource();
    const controllerSource = readFileSync(
      fileURLToPath(new URL('../../lib/application/chatDetailPageController.ts', import.meta.url)),
      'utf8'
    );

    expect(source).toContain('ticketRunGroupRequest');
    expect(source).toContain('createChatDetailPageController');
    expect(controllerSource).toContain('this.deps.chatIndexSession.activate({');
    expect(controllerSource).toContain('companionRequests: [this.ticketRunGroupRequest]');
    expect(controllerSource).toMatch(/activate\(\{[\s\S]*refresh: false[\s\S]*\}\);[\s\S]*this\.deps\.chatIndexSession\.start\(\);/);
    expect(controllerSource).toMatch(/this\.deps\.chatIndexSession\.stop\(\);[\s\S]*activate\(\{ companionRequests: \[\], refresh: false \}\);/);
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
      { facets: { categories: ['ticket_run'] }, groupBy: 'ticket_run', limit: 50 }
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
    facets: {
      category: 'ticket_run',
      turnKinds: ['message'],
      originKinds: ['surface'],
      transports: ['pma'],
      scopeKind: 'worktree',
      scopeId: 'wt-1',
      agentKind: 'coding_agent'
    },
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
