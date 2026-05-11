import { describe, expect, it } from 'vitest';
import type { PmaChatSummary, PmaRunProgress, PmaTimelineItem, SurfaceArtifact } from './domain';
import {
  artifactCardView,
  buildManagedThreadCreatePayload,
  buildManagedThreadMessagePayload,
  agentCapabilityAllowed,
  buildPmaChatScopeOptions,
  buildPmaCards,
  buildPmaActivityCards,
  buildPmaLiveActivity,
  buildPmaStatusBar,
  buildPmaTranscriptCards,
  chooseActiveChatId,
  composeMessageWithAttachments,
  countTicketRunGroups,
  filterPmaChats,
  filterPmaChatEntries,
  filterArtifactsForActiveChat,
  formatRelativeTime,
  formatCompactMessageDateTime,
  isPrimaryProgressArtifact,
  mergePmaActivityEvents,
  mergePmaTimelineAndActivityCards,
  modelReasoningOptions,
  modelSelectorState,
  optimisticUserTimelineItemFromSend,
  pmaChatKind,
  pmaChatKindLabel,
  pmaChatHeaderScopeLine,
  pmaChatMessengerSurface,
  pmaChatScopeLabelFromChat,
  pmaChatScopeTagView,
  pmaChatSurfaceFilterOptions,
  pmaChatSurfaceFilterToken,
  progressPercent,
  reconcilePmaTimeline,
  removePendingAttachment,
  sortChatsWaitingFirst,
  summarizeFilterCounts,
  buildPmaChatListEntries
} from './pmaChat';

const baseChat: PmaChatSummary = {
  id: 'chat-1',
  title: 'Repo repair',
  status: 'running',
  agentId: 'codex',
  agentProfile: null,
  model: 'gpt-5.2',
  repoId: 'repo-1',
  worktreeId: 'repo-1--pma',
  ticketId: 'TICKET-120',
  isTicketFlow: true,
  progressPercent: null,
  updatedAt: '2026-05-04T00:00:00Z',
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

function timelineItem(
  id: string,
  kind: PmaTimelineItem['kind'],
  payload: Record<string, unknown>,
  order = id
): PmaTimelineItem {
  return {
    id,
    kind,
    orderKey: order,
    timestamp: '2026-05-04T00:00:10Z',
    chatId: 'chat-1',
    turnId: id.split(':')[1] ?? null,
    status: 'running',
    payload,
    raw: { item_id: id, kind, payload }
  };
}

const baseProgress: PmaRunProgress = {
  id: 'run-1',
  chatId: 'chat-1',
  status: 'running',
  workStatus: 'running',
  operatorStatus: 'running',
  terminal: false,
  streamShouldClose: false,
  streamCloseReason: null,
  phase: 'testing',
  guidance: 'Running frontend checks.',
  queueDepth: 1,
  elapsedSeconds: 95,
  idleSeconds: 2,
  lastEventId: 7,
  lastEventAt: '2026-05-04T00:00:30Z',
  progressPercent: null,
  events: [
    {
      ...baseArtifact,
      kind: 'progress',
      raw: { progress_item: { kind: 'tool', state: 'completed', title: 'Frontend checks' } }
    }
  ],
  raw: {}
};

describe('PMA chat view helpers', () => {
  it('collapses ticket-flow chats sharing a worktree into one run group, even without ticket ids', () => {
    const chats: PmaChatSummary[] = [
      { ...baseChat, id: 'tf-1', ticketId: null, isTicketFlow: true, worktreeId: 'wt-A', repoId: 'repo-1' },
      { ...baseChat, id: 'tf-2', ticketId: null, isTicketFlow: true, worktreeId: 'wt-A', repoId: 'repo-1' },
      { ...baseChat, id: 'tf-3', ticketId: null, isTicketFlow: true, worktreeId: 'wt-A', repoId: 'repo-1' }
    ];
    const entries = buildPmaChatListEntries(chats, { groupRuns: true });
    expect(entries).toHaveLength(1);
    expect(entries[0].kind).toBe('group');
    if (entries[0].kind === 'group') {
      expect(entries[0].group.totalCount).toBe(3);
    }
  });

  it('counts archived ticket-flow chats with done ticket files as run progress done', () => {
    const chats: PmaChatSummary[] = [
      { ...baseChat, id: 'tf-1', status: 'done', ticketId: 'TICKET-001', ticketDone: true },
      { ...baseChat, id: 'tf-2', status: 'idle', ticketId: 'TICKET-002', ticketDone: true },
      { ...baseChat, id: 'tf-3', status: 'running', ticketId: 'TICKET-003', ticketDone: false }
    ];
    const entries = buildPmaChatListEntries(chats, { groupRuns: true });
    expect(entries).toHaveLength(1);
    expect(entries[0].kind).toBe('group');
    if (entries[0].kind === 'group') {
      expect(entries[0].group.totalCount).toBe(3);
      expect(entries[0].group.doneCount).toBe(2);
      expect(entries[0].group.activeCount).toBe(1);
    }
  });

  it('counts distinct ticket run groups and filters ticket_runs to grouped flows only', () => {
    const standalone = {
      ...baseChat,
      id: 'solo',
      title: 'hub chat',
      ticketId: null,
      isTicketFlow: false,
      repoId: null,
      worktreeId: null
    };
    const runA = { ...baseChat, id: 'r-a', isTicketFlow: true, worktreeId: 'wt-1', repoId: 'repo-1' };
    const runB = { ...baseChat, id: 'r-b', isTicketFlow: true, worktreeId: 'wt-1', repoId: 'repo-1' };
    const runOther = { ...baseChat, id: 'r-c', isTicketFlow: true, worktreeId: 'wt-2', repoId: 'repo-1' };
    const chats = [standalone, runA, runB, runOther];
    expect(countTicketRunGroups(chats)).toBe(2);
    expect(filterPmaChats(chats, 'ticket_runs', '', {}).map((c) => c.id).sort()).toEqual(['r-a', 'r-b', 'r-c']);
    const entries = buildPmaChatListEntries(chats, { groupRuns: true });
    const filtered = filterPmaChatEntries(entries, 'ticket_runs', '', {});
    expect(filtered).toHaveLength(2);
    expect(filtered.every((e) => e.kind === 'group')).toBe(true);
  });

  it('filters chat list by status and scoped search text', () => {
    const chats: PmaChatSummary[] = [
      baseChat,
      { ...baseChat, id: 'chat-2', title: 'Waiting approval', status: 'waiting', repoId: 'billing' },
      { ...baseChat, id: 'chat-3', title: 'Finished work', status: 'done', ticketId: 'TICKET-099' }
    ];

    expect(filterPmaChats(chats, 'active', '')).toHaveLength(1);
    expect(filterPmaChats(chats, 'waiting', 'billing')).toMatchObject([{ id: 'chat-2' }]);
    const lastSeen = { 'chat-1': '2026-05-04T00:00:00Z' };
    expect(filterPmaChats(chats, 'unread', '', lastSeen).map((c) => c.id).sort()).toEqual([
      'chat-2',
      'chat-3'
    ]);
    expect(summarizeFilterCounts(chats, lastSeen)).toEqual({ all: 3, active: 1, waiting: 1, unread: 2 });
  });

  it('detects messenger surface from API fields and title prefix', () => {
    expect(
      pmaChatMessengerSurface({
        ...baseChat,
        title: 'discord:123',
        raw: {}
      })
    ).toEqual({ slug: 'discord', label: 'Discord', badgeClass: 'surface-discord' });

    expect(
      pmaChatMessengerSurface({
        ...baseChat,
        title: 'General',
        raw: { surface_kind: 'discord', surface_key: 'ch-1' }
      })
    ).toEqual({ slug: 'discord', label: 'Discord', badgeClass: 'surface-discord' });

    expect(
      pmaChatMessengerSurface({
        ...baseChat,
        title: 'side thread',
        raw: { managed_thread_id: 't1', surface_urn: 'managed_thread:t1' }
      })
    ).toBeNull();
  });

  it('filters chats by messenger surface slug', () => {
    const discordChat = { ...baseChat, id: 'd1', title: 'discord:999', raw: {} };
    const hubChat = { ...baseChat, id: 'h1', title: 'Chat · repo', raw: {} };
    const list = [discordChat, hubChat];
    expect(filterPmaChats(list, pmaChatSurfaceFilterToken('discord'), '')).toEqual([discordChat]);
    expect(pmaChatSurfaceFilterOptions(list)).toEqual([{ slug: 'discord', label: 'Discord', count: 1 }]);
  });

  it('sorts waiting chats ahead of others then by recent updates', () => {
    const chats: PmaChatSummary[] = [
      { ...baseChat, id: 'a', status: 'running', updatedAt: '2026-05-04T03:00:00Z' },
      { ...baseChat, id: 'b', status: 'waiting', updatedAt: '2026-05-04T01:00:00Z' },
      { ...baseChat, id: 'c', status: 'waiting', updatedAt: '2026-05-04T02:00:00Z' }
    ];
    expect(sortChatsWaitingFirst(chats).map((chat) => chat.id)).toEqual(['c', 'b', 'a']);
  });

  it('formats header scope lines for PMA global, repo, and worktree chats', () => {
    expect(pmaChatHeaderScopeLine(null)).toBe('');
    expect(pmaChatHeaderScopeLine({ ...baseChat, repoId: null, worktreeId: null })).toBe('Hub workspace');
    expect(pmaChatHeaderScopeLine({ ...baseChat, repoId: 'repo-1', worktreeId: null }, () => 'My Repo')).toBe('Repo - My Repo');
    expect(
      pmaChatHeaderScopeLine({ ...baseChat, repoId: 'repo-1', worktreeId: 'wt-9' }, () => 'My Repo')
    ).toBe('Repo - My Repo - wt-9');
  });

  it('builds scope tag chips with optional friendly repo/worktree labels', () => {
    expect(
      pmaChatScopeTagView({ ...baseChat, repoId: 'repo-1', worktreeId: null }, { repoLabel: () => 'My Repo' })
    ).toEqual({ kindKey: 'repo', kindLabel: 'Repo', detail: 'My Repo' });
    expect(
      pmaChatScopeTagView(
        { ...baseChat, repoId: 'repo-1', worktreeId: 'wt-9' },
        { worktreeLabel: () => 'WT nine' }
      )
    ).toEqual({ kindKey: 'worktree', kindLabel: 'Worktree', detail: 'WT nine' });
    expect(pmaChatScopeTagView({ ...baseChat, repoId: null, worktreeId: null, raw: { workspace_root: '/tmp/hub' } })).toEqual({
      kindKey: 'hub',
      kindLabel: 'Hub',
      detail: 'hub',
      detailFull: '/tmp/hub'
    });
    expect(pmaChatScopeTagView({ ...baseChat, repoId: null, worktreeId: null, raw: {} })).toEqual({
      kindKey: 'local',
      kindLabel: 'Local',
      detail: 'Hub workspace'
    });
  });

  it('prefers URL/request id, then a valid current id, otherwise none', () => {
    expect(chooseActiveChatId([baseChat], 'chat-1')).toBe('chat-1');
    expect(chooseActiveChatId([baseChat], 'missing')).toBeNull();
    expect(chooseActiveChatId([], 'missing')).toBeNull();
  });

  it('prefers a requested linked chat when present', () => {
    const chats: PmaChatSummary[] = [
      baseChat,
      { ...baseChat, id: 'chat-2', title: 'Linked conversation', status: 'waiting' }
    ];

    expect(chooseActiveChatId(chats, 'chat-1', 'chat-2')).toBe('chat-2');
    expect(chooseActiveChatId(chats, 'chat-1', 'missing')).toBe('chat-1');
  });

  it('builds active chat cards for durable transcript content and scoped artifacts', () => {
    const cards = buildPmaCards(
      [
        timelineItem('turn:one:assistant', 'assistant_message', {
          text: 'Created a PMA ticket and started the run.',
          attachments: [{ id: 'message-attachment', title: 'Attachment' }]
        })
      ],
      baseChat,
      [
        { ...baseArtifact, id: 'scoped-artifact', raw: { managed_thread_id: 'chat-1' } },
        { ...baseArtifact, id: 'global-artifact', raw: {} }
      ]
    );

    expect(cards.map((card) => card.kind)).toEqual([
      'message',
      'ticket',
      'artifact'
    ]);
    expect(cards.at(-1)).toMatchObject({ artifact: { id: 'scoped-artifact' } });
    const messageCard = cards[0];
    if (messageCard.kind !== 'message') throw new Error('expected message card');
    expect(messageCard.message.artifacts).toHaveLength(1);
    expect(messageCard.message.artifacts[0]).toMatchObject({ id: 'message-attachment' });
  });

  it('filters active-chat artifacts by durable associations', () => {
    const scoped = { ...baseArtifact, id: 'turn-file', raw: { managed_thread_id: 'chat-1' } };
    const repoScoped = { ...baseArtifact, id: 'repo-file', raw: { repo_id: 'repo-1' } };
    const unrelated = { ...baseArtifact, id: 'unrelated-file', raw: { managed_thread_id: 'chat-2' } };

    expect(filterArtifactsForActiveChat([scoped, repoScoped, unrelated], baseChat, baseProgress).map((item) => item.id)).toEqual([
      'turn-file',
      'repo-file'
    ]);
  });

  it('summarizes live progress separately from transcript cards', () => {
    const live = buildPmaLiveActivity({
      ...baseProgress,
      elapsedSeconds: 125,
      idleSeconds: 0,
      events: [
        {
          ...baseArtifact,
          id: 'token-usage',
          kind: 'progress',
          title: 'Token usage updated',
          raw: { progress_item: { kind: 'hidden', hidden: true, title: 'Token usage updated' } }
        },
        {
          ...baseArtifact,
          id: 'tool-started',
          kind: 'progress',
          title: 'Running tests',
          summary: 'pnpm test',
          raw: { progress_item: { kind: 'tool', state: 'started', title: 'Running tests', summary: 'pnpm test' } }
        }
      ]
    });

    expect(live).toMatchObject({
      state: 'running',
      title: 'Working · testing',
      summary: 'Running frontend checks.',
      elapsedLabel: '2m 5s elapsed'
    });
    expect(live?.steps.map((step) => step.id)).toEqual(['tool-started']);
  });

  it('builds a thin status bar from backend status fields', () => {
    expect(buildPmaStatusBar({ ...baseProgress, elapsedSeconds: 125, queueDepth: 2 }, baseChat)).toEqual({
      state: 'running',
      phase: 'testing',
      elapsedLabel: '2m 5s elapsed',
      queueDepthLabel: 'queue 2',
      tokenUsageLabel: null,
      contextRemainingLabel: null,
      contextRemainingPercent: null
    });
  });

  it('adds token usage and context remaining metadata to the status bar', () => {
    expect(
      buildPmaStatusBar(
        {
          ...baseProgress,
          raw: {
            token_usage: {
              last: { totalTokens: 123390, inputTokens: 122709, outputTokens: 681 },
              modelContextWindow: 256000
            }
          }
        },
        baseChat
      )
    ).toMatchObject({
      tokenUsageLabel: 'tokens 123,390 total · 122,709 in · 681 out',
      contextRemainingLabel: 'ctx 52%',
      contextRemainingPercent: 52
    });
  });

  it('skips empty message cards and suppresses debug-only lifecycle events from the transcript', () => {
    const cards = buildPmaCards(
      [
        timelineItem('turn:empty:user', 'user_message', { text: '' }),
        timelineItem('turn:empty:status:running', 'status', { status: 'running' })
      ],
      null,
      []
    );

    expect(cards.some((card) => card.kind === 'message')).toBe(false);
    expect(cards.filter((card) => card.kind === 'artifact')).toHaveLength(0);
  });

  it('keeps low-level PMA events out of primary transcript cards while preserving final responses', () => {
    const cards = buildPmaCards(
      [
        timelineItem('turn:final:assistant', 'assistant_message', {
          text: 'Done. The PMA smoke fixtures are now covered.'
        }),
        timelineItem('turn:final:status:ok', 'status', { status: 'ok' })
      ],
      null,
      []
    );

    expect(cards.filter((card) => card.kind === 'message')).toHaveLength(1);
    expect(cards.find((card) => card.kind === 'message')).toMatchObject({
      message: { text: 'Done. The PMA smoke fixtures are now covered.' }
    });
    expect(cards.filter((card) => card.kind === 'artifact')).toHaveLength(0);
  });

  it('persists intermediate output and groups tool calls between user and final assistant messages', () => {
    const cards = buildPmaCards(
      [
        timelineItem('turn:one:user', 'user_message', { text: 'Create tickets' }, '001'),
        timelineItem('turn:one:intermediate:think-1', 'intermediate', { intermediate_kind: 'thinking', text: 'Inspecting repo state.', event: { kind: 'thinking', message: 'Inspecting repo state.' } }, '002'),
        timelineItem('turn:one:tool:1:rg', 'tool_group', { tool_name: 'rg tickets', call: { summary: 'rg tickets' }, result: { status: 'completed', summary: '2 matches' } }, '003'),
        timelineItem('turn:one:approval:write-1', 'approval', { description: 'Allow write' }, '0035'),
        timelineItem('turn:one:intermediate:think-2', 'intermediate', { intermediate_kind: 'thinking', text: 'Drafting ticket files.' }, '004'),
        timelineItem('turn:one:assistant', 'assistant_message', { text: 'Done.\n\n- [TICKET-001.md](/tmp/TICKET-001.md)' }, '005')
      ],
      null,
      []
    );

    expect(cards.map((card) => card.kind)).toEqual([
      'message',
      'intermediate',
      'tool_group',
      'approval',
      'intermediate',
      'message'
    ]);
    expect(cards[2]).toMatchObject({
      kind: 'tool_group',
      tools: [{ title: 'rg tickets', state: 'completed', summary: '2 matches' }]
    });
    expect(cards[3]).toMatchObject({
      kind: 'approval',
      summary: 'Allow write'
    });
    expect(cards.find((card) => card.kind === 'intermediate')).toMatchObject({
      detail: '1 thinking update · source events turn:one:intermediate:think-1'
    });
  });

  it('collapses completed turn activity into one worked summary before the assistant reply', () => {
    const cards = buildPmaTranscriptCards(
      [
        timelineItem('turn:one:user', 'user_message', { text: 'Create tickets' }, '001'),
        timelineItem('turn:one:intermediate:think-1', 'intermediate', { intermediate_kind: 'thinking', text: 'Inspecting repo state.' }, '002'),
        timelineItem('turn:one:tool:1:rg', 'tool_group', { tool_name: 'rg tickets', call: { summary: 'rg tickets' }, result: { status: 'completed' } }, '003'),
        timelineItem('turn:one:assistant', 'assistant_message', { text: 'Done.' }, '004')
      ],
      null,
      [],
      { ...baseProgress, id: 'one', terminal: true, status: 'done', elapsedSeconds: 14, events: [] }
    );

    expect(cards.map((card) => card.kind)).toEqual(['message', 'turn_summary', 'message']);
    expect(cards[1]).toMatchObject({
      kind: 'turn_summary',
      title: 'Worked for 14s',
      cards: [{ kind: 'intermediate' }, { kind: 'tool_group' }]
    });
  });

  it('accumulates persisted OpenCode thinking deltas into one completed-turn summary row', () => {
    const cards = buildPmaTranscriptCards(
      [
        timelineItem('turn:one:user', 'user_message', { text: 'Run a smoke test' }, '001'),
        timelineItem('turn:one:intermediate:think-1', 'intermediate', {
          intermediate_kind: 'thinking',
          text: 'Got it'
        }, '002'),
        timelineItem('turn:one:intermediate:think-2', 'intermediate', {
          intermediate_kind: 'thinking',
          text: ' - I will create a test file'
        }, '003'),
        timelineItem('turn:one:intermediate:think-3', 'intermediate', {
          intermediate_kind: 'thinking',
          text: ', edit it, search within it, and then delete it.'
        }, '004'),
        timelineItem('turn:one:assistant', 'assistant_message', { text: 'Done.' }, '005')
      ],
      null,
      [],
      { ...baseProgress, id: 'one', terminal: true, status: 'done', elapsedSeconds: 17, events: [] }
    );

    expect(cards.map((card) => card.kind)).toEqual(['message', 'turn_summary', 'message']);
    expect(cards[1]).toMatchObject({
      kind: 'turn_summary',
      cards: [
        {
          kind: 'intermediate',
          title: 'thinking',
          text: 'Got it - I will create a test file, edit it, search within it, and then delete it.',
          detail: '3 thinking updates · source events turn:one:intermediate:think-1, turn:one:intermediate:think-2, turn:one:intermediate:think-3'
        }
      ]
    });
    expect(JSON.stringify(cards[1])).not.toContain('"message":"Got it"');
  });

  it('does not re-add terminal live progress outside the worked summary after final output lands', () => {
    const cards = buildPmaTranscriptCards(
      [
        timelineItem('turn:one:user', 'user_message', { text: 'Run tools' }, '001'),
        timelineItem('turn:one:intermediate:think-1', 'intermediate', { intermediate_kind: 'thinking', text: 'Reading files' }, '002'),
        timelineItem('turn:one:tool:1:rg', 'tool_group', { tool_name: 'rg', result: { status: 'completed' } }, '003'),
        timelineItem('turn:one:assistant', 'assistant_message', { text: 'Done.' }, '004')
      ],
      null,
      [],
      {
        ...baseProgress,
        id: 'one',
        terminal: true,
        status: 'done',
        elapsedSeconds: 17,
        events: [
          {
            ...baseArtifact,
            id: 'live-thinking-1',
            kind: 'progress',
            createdAt: '2026-05-04T00:00:11Z',
            summary: 'Reading files',
            raw: {
              execution_id: 'runtime-one',
              progress_item: { kind: 'assistant_update', state: 'running', title: 'Thinking', summary: 'Reading files' }
            }
          },
          {
            ...baseArtifact,
            id: 'live-tool-1',
            kind: 'progress',
            createdAt: '2026-05-04T00:00:12Z',
            summary: 'rg',
            raw: {
              execution_id: 'runtime-one',
              progress_item: { kind: 'tool', state: 'completed', title: 'rg' }
            }
          }
        ]
      }
    );

    expect(cards.map((card) => card.kind)).toEqual(['message', 'turn_summary', 'message']);
    expect(cards[1]).toMatchObject({
      kind: 'turn_summary',
      cards: [{ kind: 'intermediate' }, { kind: 'tool_group' }]
    });
  });

  it('accumulates current running raw activity in a turn summary until the assistant message lands', () => {
    const cards = buildPmaTranscriptCards(
      [
        timelineItem('turn:one:user', 'user_message', { text: 'Create tickets' }, '001'),
        timelineItem('turn:one:intermediate:think-1', 'intermediate', { intermediate_kind: 'thinking', text: 'Inspecting repo state.' }, '002')
      ],
      null,
      [],
      { ...baseProgress, id: 'one', terminal: false, status: 'running', events: [] }
    );

    expect(cards.map((card) => card.kind)).toEqual(['message', 'turn_summary']);
    expect(cards[1]).toMatchObject({
      kind: 'turn_summary',
      title: 'Worked for 1m 35s',
      cards: [{ kind: 'intermediate', text: 'Inspecting repo state.' }]
    });
  });

  it('inherits the active turn for raw tail progress and folds merged deltas under the work summary', () => {
    const cards = buildPmaTranscriptCards(
      [
        timelineItem('turn:one:user', 'user_message', { text: 'Summarize this thread' }, '00000001')
      ],
      null,
      [],
      {
        ...baseProgress,
        id: 'one',
        terminal: false,
        status: 'running',
        elapsedSeconds: 21,
        events: [
          {
            ...baseArtifact,
            id: 'raw-progress-1',
            kind: 'progress',
            createdAt: '2026-05-04T00:00:11Z',
            summary: 'The',
            raw: {
              progress_item: { kind: 'notice', title: 'Progress', summary: 'The', event_ids: [11] }
            }
          },
          {
            ...baseArtifact,
            id: 'raw-progress-2',
            kind: 'progress',
            createdAt: '2026-05-04T00:00:11.500Z',
            summary: 'user',
            raw: {
              progress_item: { kind: 'notice', title: 'Progress', summary: 'user', event_ids: [12] }
            }
          },
          {
            ...baseArtifact,
            id: 'commentary-1',
            kind: 'progress',
            createdAt: '2026-05-04T00:00:12Z',
            summary: 'I am checking the latest context.',
            raw: {
              progress_item: { kind: 'notice', title: 'Commentary', summary: 'I am checking the latest context.', event_ids: [13] }
            }
          },
          {
            ...baseArtifact,
            id: 'raw-progress-3',
            kind: 'progress',
            createdAt: '2026-05-04T00:00:13Z',
            summary: 'wants',
            raw: {
              progress_item: { kind: 'notice', title: 'Progress', summary: 'wants', event_ids: [14] }
            }
          }
        ]
      }
    );

    expect(cards.map((card) => card.kind)).toEqual(['message', 'turn_summary', 'intermediate']);
    expect(cards[1]).toMatchObject({
      kind: 'turn_summary',
      title: 'Worked for 21s',
      cards: [
        { kind: 'intermediate', title: 'Progress', text: 'The user wants' }
      ]
    });
    expect(cards[2]).toMatchObject({
      kind: 'intermediate',
      title: 'Commentary',
      text: 'I am checking the latest context.'
    });
  });

  it('folds failed and interrupted turn notices under a summary instead of chat bubbles', () => {
    const cards = buildPmaTranscriptCards(
      [
        timelineItem('turn:failed:user', 'user_message', { text: 'Run the check' }, '00000001'),
        timelineItem(
          'turn:failed:intermediate:1',
          'intermediate',
          { intermediate_kind: 'turn_failed', text: 'Turn failed.' },
          '00000002'
        ),
        timelineItem(
          'turn:failed:intermediate:2',
          'intermediate',
          { intermediate_kind: 'thinking', text: 'Collected failure context.' },
          '00000003'
        )
      ],
      null,
      [],
      { ...baseProgress, id: 'other-turn', terminal: true, status: 'done', events: [] }
    );

    expect(cards.map((card) => card.kind)).toEqual(['message', 'turn_summary']);
    expect(cards[1]).toMatchObject({
      kind: 'turn_summary',
      cards: [
        { kind: 'intermediate', title: 'turn failed', text: 'Turn failed.' },
        { kind: 'intermediate', title: 'thinking', text: 'Collected failure context.' }
      ]
    });
  });

  it('dedupes live activity against canonical source event ids and preserves chronological order', () => {
    const canonical = buildPmaCards(
      [
        timelineItem('turn:one:user', 'user_message', { text: 'Run tools' }, '00000001'),
        timelineItem(
          'turn:one:tool:7:rg',
          'tool_group',
          {
            tool_name: 'rg',
            progress_items: [{ event_ids: [7] }],
            call: { summary: 'rg TODO' },
            result: { status: 'completed' }
          },
          '00000007'
        ),
        {
          ...timelineItem('turn:one:assistant', 'assistant_message', { text: 'Done.' }, '00000009'),
          timestamp: '2026-05-04T00:00:13Z'
        }
      ],
      null,
      []
    );
    const live = buildPmaActivityCards([
      {
        ...baseArtifact,
        id: '7',
        kind: 'progress',
        createdAt: '2026-05-04T00:00:11Z',
        raw: {
          managed_turn_id: 'one',
          progress_item: { kind: 'tool', state: 'completed', title: 'rg', event_ids: [7] }
        }
      },
      {
        ...baseArtifact,
        id: '8',
        kind: 'progress',
        createdAt: '2026-05-04T00:00:12Z',
        summary: 'Still thinking',
        raw: {
          managed_turn_id: 'one',
          progress_item: { kind: 'assistant_update', state: 'running', title: 'Thinking', summary: 'Still thinking', event_ids: [8] }
        }
      }
    ]);

    expect(mergePmaTimelineAndActivityCards(canonical, live).map((card) => card.id)).toEqual([
      'turn:one:user',
      'turn:one:tool:7:rg',
      'intermediate-8',
      'turn:one:assistant'
    ]);
  });

  it('does not merge live progress notices across tool activity', () => {
    const cards = buildPmaActivityCards(
      [
        {
          ...baseArtifact,
          id: 'prog-1',
          kind: 'progress',
          createdAt: '2026-05-08T12:00:01Z',
          raw: {
            progress_item: {
              kind: 'notice',
              title: 'Progress',
              summary: 'Starting',
              event_ids: [1]
            }
          }
        },
        {
          ...baseArtifact,
          id: 'tool-2',
          kind: 'progress',
          createdAt: '2026-05-08T12:00:02Z',
          raw: {
            progress_item: {
              kind: 'tool',
              state: 'completed',
              title: 'rg',
              summary: 'rg TODO',
              event_ids: [2]
            }
          }
        },
        {
          ...baseArtifact,
          id: 'prog-3',
          kind: 'progress',
          createdAt: '2026-05-08T12:00:03Z',
          raw: {
            progress_item: {
              kind: 'notice',
              title: 'Progress',
              summary: 'Continuing',
              event_ids: [3]
            }
          }
        }
      ],
      { fallbackTurnId: 'one' }
    );

    expect(cards).toMatchObject([
      { kind: 'intermediate', title: 'Progress', text: 'Starting', turnId: 'one' },
      { kind: 'tool_group', tools: [{ title: 'rg' }], turnId: 'one' },
      { kind: 'intermediate', title: 'Progress', text: 'Continuing', turnId: 'one' }
    ]);
  });

  it('anchors live progress after the user row when SSE timestamps precede the persisted prompt', () => {
    const canonical = buildPmaCards(
      [
        {
          ...timelineItem('turn:one:user', 'user_message', { text: 'Do work' }, '00000042'),
          timestamp: '2026-05-08T12:00:05Z',
          orderKey: '00000042|2026-05-08T12:00:05Z|turn:one:user'
        }
      ],
      null,
      []
    );
    const live = buildPmaActivityCards([
      {
        ...baseArtifact,
        id: 'prog-1',
        kind: 'progress',
        createdAt: '2026-05-08T12:00:01Z',
        raw: {
          managed_turn_id: 'one',
          progress_item: {
            kind: 'notice',
            title: 'Progress',
            summary: 'Starting',
            event_ids: [1]
          }
        }
      },
      {
        ...baseArtifact,
        id: 'prog-2',
        kind: 'progress',
        createdAt: '2026-05-08T12:00:02Z',
        raw: {
          managed_turn_id: 'one',
          progress_item: {
            kind: 'notice',
            title: 'Progress',
            summary: 'Continuing',
            event_ids: [2]
          }
        }
      }
    ]);

    const merged = mergePmaTimelineAndActivityCards(canonical, live);
    expect(merged.map((card) => card.id)).toEqual([
      'turn:one:user',
      'intermediate-prog-1'
    ]);
    expect(merged[1]).toMatchObject({ kind: 'intermediate', text: 'Starting Continuing' });
  });

  it('anchors live progress after optimistic user bubbles', () => {
    const optimistic = optimisticUserTimelineItemFromSend(
      { managed_turn_id: 'one', delivered_message: 'Hi', managed_thread_id: 'chat-1' },
      'Hi',
      'chat-1'
    );
    expect(optimistic).not.toBeNull();
    const canonical = buildPmaCards([optimistic!], null, []);
    const live = buildPmaActivityCards([
      {
        ...baseArtifact,
        id: 'prog-opt',
        kind: 'progress',
        createdAt: '2026-05-08T11:59:00Z',
        raw: {
          managed_turn_id: 'one',
          progress_item: {
            kind: 'notice',
            title: 'Progress',
            summary: 'Early SSE',
            event_ids: [1]
          }
        }
      }
    ]);

    expect(mergePmaTimelineAndActivityCards(canonical, live).map((card) => card.id)).toEqual([
      'turn:one:user',
      'intermediate-prog-opt'
    ]);
  });

  it('drops decode-failure lifecycle noise from canonical and live activity cards', () => {
    expect(
      buildPmaCards(
        [
          timelineItem('turn:one:intermediate:1', 'intermediate', {
            intermediate_kind: 'decode_failure',
            text: 'No decoder for method: turn/diff/updated'
          })
        ],
        null,
        []
      )
    ).toEqual([]);
    expect(
      buildPmaActivityCards([
        {
          ...baseArtifact,
          id: 'decode-1',
          kind: 'progress',
          title: 'Decode Failure',
          summary: 'No decoder for method: turn/diff/updated',
          raw: { progress_item: { kind: 'decode_failure', title: 'Decode Failure', summary: 'No decoder for method: turn/diff/updated' } }
        }
      ])
    ).toEqual([]);
  });

  it('drops persisted assistant answer deltas, log lines, and internal journal notices from visible trace cards', () => {
    const cards = buildPmaCards(
      [
        timelineItem('turn:one:intermediate:journal', 'intermediate', {
          intermediate_kind: 'chat_execution_journal',
          text: 'Managed-thread execution accepted',
          event: { kind: 'chat_execution_journal', message: 'Managed-thread execution accepted' }
        }),
        timelineItem('turn:one:intermediate:compaction', 'intermediate', {
          intermediate_kind: 'compaction_summary',
          text: 'Compacted hot timeline rows.',
          event: { kind: 'compaction_summary', message: 'Compacted hot timeline rows.' }
        }),
        timelineItem('turn:one:intermediate:stream', 'intermediate', {
          intermediate_kind: 'assistant_stream',
          event_type: 'output_delta',
          text: 'Final answer chunk'
        }),
        timelineItem('turn:one:intermediate:final-echo', 'intermediate', {
          intermediate_kind: 'assistant_message',
          event_type: 'output_delta',
          text: 'Final answer'
        }),
        timelineItem('turn:one:intermediate:log-line', 'intermediate', {
          intermediate_kind: 'log_line',
          event_type: 'output_delta',
          text: 'raw command stdout that belongs in the tool trace'
        }),
        timelineItem('turn:one:intermediate:thinking', 'intermediate', {
          intermediate_kind: 'thinking',
          text: 'Reading files'
        })
      ],
      null,
      []
    );

    expect(cards).toHaveLength(1);
    expect(cards[0]).toMatchObject({ kind: 'intermediate', text: 'Reading files' });
    expect(
      buildPmaActivityCards([
        {
          ...baseArtifact,
          id: 'journal-1',
          kind: 'progress',
          title: 'Chat Execution Journal',
          summary: 'Managed-thread execution accepted',
          raw: { progress_item: { kind: 'notice', title: 'Chat Execution Journal', summary: 'Managed-thread execution accepted' } }
        },
        {
          ...baseArtifact,
          id: 'compaction-1',
          kind: 'progress',
          title: 'Compaction Summary',
          summary: 'Compacted hot timeline rows.',
          raw: { progress_item: { kind: 'notice', title: 'Compaction Summary', summary: 'Compacted hot timeline rows.' } }
        }
      ])
    ).toEqual([]);
  });

  it('renders compaction lifecycle timeline items as visible dividers', () => {
    const cards = buildPmaCards(
      [
        timelineItem('action:1:compact', 'lifecycle', {
          lifecycle_kind: 'chat_compacted',
          title: 'Chat compacted',
          text: 'Chat compacted. The next message starts a fresh backend session with the compacted context.',
          summary_preview: 'Keep the current goal and constraints.'
        })
      ],
      null,
      []
    );

    expect(cards).toHaveLength(1);
    expect(cards[0]).toMatchObject({
      kind: 'lifecycle',
      title: 'Chat compacted',
      text: expect.stringContaining('Keep the current goal and constraints.')
    });
  });

  it('reconciles optimistic sends with backend timeline IDs in order', () => {
    const optimistic = optimisticUserTimelineItemFromSend(
      {
        managed_thread_id: 'chat-1',
        managed_turn_id: 'turn-2',
        delivered_message: 'queued second',
        execution_state: 'queued'
      },
      'fallback',
      'chat-1'
    );
    expect(optimistic).not.toBeNull();

    const merged = reconcilePmaTimeline(
      [
        timelineItem('turn:turn-1:user', 'user_message', { text: 'first' }, '001'),
        optimistic!
      ],
      [
        timelineItem('turn:turn-2:user', 'user_message', { text: 'queued second' }, '002')
      ]
    );

    expect(merged.map((item) => item.id)).toEqual(['turn:turn-1:user', 'turn:turn-2:user']);
  });

  it('removes random optimistic user placeholders when the canonical user message arrives', () => {
    const merged = reconcilePmaTimeline(
      [
        timelineItem('optimistic:user:123', 'user_message', { text: 'queued second' }, 'optimistic'),
        timelineItem('turn:turn-1:user', 'user_message', { text: 'first' }, '001')
      ],
      [
        timelineItem('turn:turn-2:user', 'user_message', { text: 'queued second' }, '002')
      ]
    );

    expect(merged.map((item) => item.id)).toEqual(['turn:turn-1:user', 'turn:turn-2:user']);
  });

  it('normalizes optimistic send status through the canonical optional work-status mapper', () => {
    expect(
      optimisticUserTimelineItemFromSend(
        {
          managed_thread_id: 'chat-1',
          managed_turn_id: 'turn-active',
          delivered_message: 'active work',
          execution_state: 'active'
        },
        'fallback',
        'chat-1'
      )?.status
    ).toBe('running');
    expect(
      optimisticUserTimelineItemFromSend(
        {
          managed_thread_id: 'chat-1',
          managed_turn_id: 'turn-unknown',
          delivered_message: 'unknown work',
          execution_state: 'unknown-status'
        },
        'fallback',
        'chat-1'
      )?.status
    ).toBe('idle');
    expect(
      optimisticUserTimelineItemFromSend(
        {
          managed_thread_id: 'chat-1',
          managed_turn_id: 'turn-empty',
          delivered_message: 'empty work',
          execution_state: ''
        },
        'fallback',
        'chat-1'
      )?.status
    ).toBeNull();
  });

  it('merges streamed activity events without dropping older transcript activity', () => {
    const merged = mergePmaActivityEvents(
      [
        {
          ...baseArtifact,
          id: 'event-1',
          kind: 'progress',
          summary: 'First update.',
          raw: { progress_item: { kind: 'assistant_update', state: 'running', title: 'Thinking', summary: 'First update.' } }
        }
      ],
      [
        {
          ...baseArtifact,
          id: 'event-2',
          kind: 'progress',
          summary: 'Second update.',
          raw: { progress_item: { kind: 'assistant_update', state: 'running', title: 'Thinking', summary: 'Second update.' } }
        }
      ]
    );

    expect(merged.map((event) => event.id)).toEqual(['event-1', 'event-2']);
  });

  it('uses backend-owned progress item visibility for primary progress', () => {
    expect(
      isPrimaryProgressArtifact({
        ...baseArtifact,
        kind: 'progress',
        title: 'Token usage updated',
        raw: { progress_item: { kind: 'hidden', hidden: true, title: 'Token usage updated' } }
      })
    ).toBe(false);
    expect(
      isPrimaryProgressArtifact({
        ...baseArtifact,
        kind: 'progress',
        title: 'status-check completed',
        raw: { progress_item: { kind: 'tool', state: 'completed', title: 'status-check' } }
      })
    ).toBe(true);
  });

  it('derives compact progress and relative timestamps', () => {
    expect(progressPercent(baseChat, baseProgress)).toBe(64);
    expect(progressPercent({ ...baseChat, progressPercent: 41 }, baseProgress)).toBe(41);
    expect(formatRelativeTime('2026-05-04T00:00:00Z', new Date('2026-05-04T00:03:00Z'))).toBe('3m ago');
  });

  it('formats compact message datetimes for footers', () => {
    expect(formatCompactMessageDateTime(null, new Date(2026, 4, 10), 'en-US')).toBeNull();
    expect(formatCompactMessageDateTime('', new Date(2026, 4, 10), 'en-US')).toBeNull();
    expect(formatCompactMessageDateTime('not-a-date', new Date(2026, 4, 10), 'en-US')).toBeNull();
    const may10 = new Date(2026, 4, 10, 18, 30, 0);
    const may10Noon = new Date(2026, 4, 10, 12, 0, 0);
    expect(formatCompactMessageDateTime(may10.toISOString(), may10Noon, 'en-US')).toMatch(/6:30/);
    const apr1 = new Date(2026, 3, 1, 9, 0, 0);
    const out = formatCompactMessageDateTime(apr1.toISOString(), may10Noon, 'en-US');
    expect(out).toContain('·');
    expect(out).toMatch(/Apr/);
    const jan2025 = new Date(2025, 0, 3, 8, 0, 0);
    expect(formatCompactMessageDateTime(jan2025.toISOString(), may10Noon, 'en-US')).toMatch(/2025/);
  });

  it('builds managed thread creation payloads for local, repo, and worktree scopes', () => {
    const [local, repo, worktree] = buildPmaChatScopeOptions(
      [
        {
          id: 'repo-1',
          name: 'Repo One',
          path: '/hub/repo-1',
          status: 'idle',
          defaultBranch: 'main',
          worktreeCount: 1,
          activeRuns: 0,
          openTickets: 0,
          lastActivityAt: null,
          raw: {}
        }
      ],
      [
        {
          id: 'worktree-1',
          repoId: 'repo-1',
          name: 'Feature worktree',
          path: '/hub/repo-1-pma',
          branch: 'pma/feature',
          status: 'idle',
          activeRuns: 0,
          openTickets: 0,
          lastActivityAt: null,
          raw: {}
        }
      ]
    );

    expect(buildManagedThreadCreatePayload('codex', local)).toEqual({
      agent: 'codex',
      name: 'New chat',
      scope_urn: 'hub'
    });
    expect(buildManagedThreadCreatePayload('codex', repo)).toEqual({
      agent: 'codex',
      name: 'New chat',
      scope_urn: 'repo:repo-1'
    });
    expect(buildManagedThreadCreatePayload('codex', worktree)).toEqual({
      agent: 'codex',
      name: 'New chat',
      scope_urn: 'worktree:repo-1/worktree-1'
    });
    expect(buildManagedThreadCreatePayload('opencode', local, 'New chat', 'zai/glm')).toEqual({
      agent: 'opencode',
      model: 'zai/glm',
      name: 'New chat',
      scope_urn: 'hub'
    });
    expect(buildManagedThreadCreatePayload('hermes', local, 'New chat', '', 'planning')).toEqual({
      agent: 'hermes',
      name: 'New chat',
      profile: 'planning',
      scope_urn: 'hub'
    });
  });

  it('labels existing chat scopes from durable backend fields', () => {
    expect(
      pmaChatScopeLabelFromChat({
        ...baseChat,
        repoId: 'repo-1',
        worktreeId: null,
        raw: { resource_kind: 'repo', resource_id: 'repo-1' }
      })
    ).toBe('Repo · repo-1');
  });

  it('labels hub-scoped chats using workspace_root as Hub (not repo/worktree)', () => {
    expect(
      pmaChatScopeLabelFromChat({
        ...baseChat,
        repoId: null,
        worktreeId: null,
        raw: { workspace_root: '/Users/me/proj' }
      })
    ).toBe('Hub · /Users/me/proj');
  });

  it('renders pending attachment message text and removes staged attachments', () => {
    const attachments = [
      {
        id: 'att-1',
        kind: 'image' as const,
        title: 'screen.png',
        sizeLabel: '8 KB',
        url: '/hub/pma/files/inbox/screen.png',
        uploadedName: 'screen.png',
        uploadState: 'uploaded' as const
      },
      {
        id: 'att-2',
        kind: 'link' as const,
        title: 'https://example.test/preview',
        sizeLabel: null,
        url: 'https://example.test/preview',
        uploadedName: null,
        uploadState: 'uploaded' as const
      }
    ];

    expect(composeMessageWithAttachments('Review these', attachments)).toBe('Review these');
    expect(composeMessageWithAttachments('  draft  ', attachments)).toBe('draft');
    expect(composeMessageWithAttachments('', attachments)).toBe('');
    expect(removePendingAttachment(attachments, 'att-1')).toMatchObject([{ id: 'att-2' }]);
  });

  it('builds managed-thread create and send payloads that match backend constraints', () => {
    expect(buildManagedThreadCreatePayload('codex')).toEqual({
      agent: 'codex',
      name: 'New chat',
      scope_urn: 'hub'
    });
    const attachments = [
      {
        id: 'att-1',
        kind: 'file' as const,
        title: 'report.md',
        sizeLabel: '1 KB',
        url: '/hub/pma/files/inbox/report.md',
        uploadedName: 'report.md',
        uploadState: 'uploaded' as const
      }
    ];
    expect(buildManagedThreadMessagePayload('Continue', 'gpt-5.2', true, attachments)).toEqual({
      message: 'Continue',
      attachments: [
        {
          intent: 'attach_uploaded_file',
          source: 'upload',
          id: 'att-1',
          kind: 'file',
          title: 'report.md',
          sizeLabel: '1 KB',
          url: '/hub/pma/files/inbox/report.md',
          uploadedName: 'report.md',
          uploadState: 'uploaded'
        }
      ],
      model: 'gpt-5.2',
      reasoning: undefined,
      busy_policy: 'queue',
      defer_execution: true,
      wait_for_confirmation: false
    });
    expect(buildManagedThreadMessagePayload('Continue', 'gpt-5.2', false, [], '', 'planning')).toEqual({
      message: 'Continue',
      attachments: undefined,
      model: 'gpt-5.2',
      reasoning: undefined,
      profile: 'planning',
      busy_policy: undefined,
      defer_execution: true,
      wait_for_confirmation: false
    });
    expect(buildManagedThreadMessagePayload('Continue', 'gpt-5.2', false, [], 'high')).toMatchObject({
      message: 'Continue',
      model: 'gpt-5.2',
      reasoning: 'high'
    });
    expect(buildManagedThreadMessagePayload('Continue', '', false)).toEqual({
      message: 'Continue',
      attachments: undefined,
      model: undefined,
      reasoning: undefined,
      busy_policy: undefined,
      defer_execution: true,
      wait_for_confirmation: false
    });
    expect(buildManagedThreadMessagePayload('Replace current work', '', true, [], '', '', 'interrupt')).toMatchObject({
      busy_policy: 'interrupt',
      wait_for_confirmation: false
    });
    expect(buildManagedThreadMessagePayload('Summarize only if idle', '', false, [], '', '', 'reject')).toMatchObject({
      busy_policy: 'reject',
      wait_for_confirmation: false
    });
    expect(
      buildManagedThreadMessagePayload(
        'Queued attachment',
        '',
        true,
        [
          {
            intent: 'include_link',
            source: 'link',
            id: 'queued-link',
            kind: 'link',
            title: 'https://example.test',
            url: 'https://example.test'
          }
        ],
        '',
        '',
        'interrupt'
      ).attachments
    ).toEqual([
      {
        intent: 'include_link',
        source: 'link',
        id: 'queued-link',
        kind: 'link',
        title: 'https://example.test',
        url: 'https://example.test'
      }
    ]);
  });

  it('summarizes model selector loading, empty, error, and loaded states', () => {
    expect(modelSelectorState(true, null, 0)).toMatchObject({ state: 'loading', disabled: true });
    expect(modelSelectorState(false, null, 0)).toMatchObject({ state: 'empty', disabled: true });
    expect(modelSelectorState(false, 'Agent missing provider', 0)).toMatchObject({ state: 'error', disabled: true });
    expect(modelSelectorState(false, null, 2)).toMatchObject({ state: 'loaded', disabled: false });
  });

  it('derives chat kind and reasoning affordances from shared thread/model metadata', () => {
    expect(pmaChatKind(baseChat)).toBe('pma');
    expect(pmaChatKind({ ...baseChat, raw: { name: 'New coding agent chat' } })).toBe('coding_agent');
    expect(pmaChatKind({ ...baseChat, raw: { chat_kind: 'direct_agent' } })).toBe('coding_agent');
    expect(pmaChatKindLabel('coding_agent')).toBe('Coding agent');
    expect(pmaChatKindLabel('pma')).toBe('Chat');
    expect(agentCapabilityAllowed({ capability_projection: { actions: { list_models: { allowed: true } } } }, 'list_models')).toBe(true);
    expect(agentCapabilityAllowed({ capability_projection: { actions: { list_models: { allowed: false } } } }, 'list_models')).toBe(false);
    expect(modelReasoningOptions({ reasoning_options: ['low', 'high', 'high'] })).toEqual(['low', 'high']);
    expect(modelReasoningOptions({ supports_reasoning: false, reasoning_options: ['none', 'high'] })).toEqual([]);
    expect(modelReasoningOptions({ supports_reasoning: true })).toEqual([]);
  });

  it('defines high-signal artifact card views for all surfaced variants', () => {
    const variants: SurfaceArtifact['kind'][] = [
      'screenshot',
      'image',
      'file',
      'preview_url',
      'test_result',
      'command_summary',
      'diff_summary',
      'link',
      'final_report',
      'error',
      'progress'
    ];

    const views = variants.map((kind) => artifactCardView({ ...baseArtifact, kind, url: '/artifact' }));

    expect(views.map((view) => view.label)).toEqual([
      'Screenshot',
      'Image',
      'File',
      'Preview URL',
      'Test result',
      'Command summary',
      'Diff summary',
      'PR / link',
      'Final report',
      'Error / blocker',
      'Run event'
    ]);
    expect(views.every((view) => view.detailLabel)).toBe(true);
  });
});
