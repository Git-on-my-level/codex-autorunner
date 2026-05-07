import { describe, expect, it } from 'vitest';
import type { PmaChatSummary, PmaRunProgress, PmaTimelineItem, SurfaceArtifact } from './domain';
import {
  artifactCardView,
  buildManagedThreadCreatePayload,
  buildManagedThreadMessagePayload,
  buildPmaChatScopeOptions,
  buildPmaCards,
  buildPmaLiveActivity,
  buildPmaStatusBar,
  chooseActiveChatId,
  composeMessageWithAttachments,
  filterPmaChats,
  filterArtifactsForActiveChat,
  formatRelativeTime,
  isPrimaryProgressArtifact,
  mergePmaActivityEvents,
  modelSelectorState,
  optimisticUserTimelineItemFromSend,
  pmaChatHeaderScopeLine,
  pmaChatScopeLabelFromChat,
  progressPercent,
  reconcilePmaTimeline,
  removePendingAttachment,
  sortChatsWaitingFirst,
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
    expect(pmaChatHeaderScopeLine({ ...baseChat, repoId: null, worktreeId: null })).toBe('PMA - global');
    expect(pmaChatHeaderScopeLine({ ...baseChat, repoId: 'repo-1', worktreeId: null }, () => 'My Repo')).toBe('Repo - My Repo');
    expect(
      pmaChatHeaderScopeLine({ ...baseChat, repoId: 'repo-1', worktreeId: 'wt-9' }, () => 'My Repo')
    ).toBe('Repo - My Repo - wt-9');
  });

  it('keeps the selected chat when still present and falls back otherwise', () => {
    expect(chooseActiveChatId([baseChat], 'chat-1')).toBe('chat-1');
    expect(chooseActiveChatId([baseChat], 'missing')).toBe('chat-1');
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
      'artifact',
      'ticket',
      'artifact'
    ]);
    expect(cards.at(-1)).toMatchObject({ artifact: { id: 'scoped-artifact' } });
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
      queueDepthLabel: 'queue 2'
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
        timelineItem('turn:one:intermediate:think-1', 'intermediate', { intermediate_kind: 'thinking', text: 'Inspecting repo state.' }, '002'),
        timelineItem('turn:one:tool:1:rg', 'tool_group', { tool_name: 'rg tickets', call: { summary: 'rg tickets' } }, '003'),
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
      'intermediate',
      'message'
    ]);
    expect(cards[2]).toMatchObject({
      kind: 'tool_group',
      tools: [{ title: 'rg tickets', state: 'started' }]
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
      ],
      []
    );

    expect(buildManagedThreadCreatePayload('codex', local)).toEqual({
      agent: 'codex',
      name: 'New PMA chat',
      workspace_root: '.'
    });
    expect(buildManagedThreadCreatePayload('codex', repo)).toEqual({
      agent: 'codex',
      name: 'New PMA chat',
      resource_kind: 'repo',
      resource_id: 'repo-1'
    });
    expect(buildManagedThreadCreatePayload('codex', worktree)).toEqual({
      agent: 'codex',
      name: 'New PMA chat',
      workspace_root: '/hub/repo-1-pma'
    });
    expect(buildManagedThreadCreatePayload('opencode', local, 'New PMA chat', 'zai/glm')).toEqual({
      agent: 'opencode',
      model: 'zai/glm',
      name: 'New PMA chat',
      workspace_root: '.'
    });
  });

  it('builds managed thread creation payloads for backend-owned agent workspaces', () => {
    const scopes = buildPmaChatScopeOptions([], [], [
      {
        id: 'codex-pma',
        runtime: 'codex',
        name: 'Codex PMA',
        path: '/hub/.agent-workspaces/codex-pma',
        enabled: true,
        existsOnDisk: true,
        resourceKind: 'agent_workspace',
        raw: {}
      }
    ]);

    expect(buildManagedThreadCreatePayload('codex', scopes[1])).toEqual({
      agent: 'codex',
      name: 'New PMA chat',
      resource_kind: 'agent_workspace',
      resource_id: 'codex-pma'
    });
  });

  it('labels existing chat scopes from durable backend fields', () => {
    expect(pmaChatScopeLabelFromChat({ ...baseChat, raw: { resource_kind: 'agent_workspace', resource_id: 'codex-pma' } })).toBe(
      'Agent workspace · codex-pma'
    );
    expect(pmaChatScopeLabelFromChat({ ...baseChat, repoId: 'repo-1', worktreeId: null, raw: { resource_kind: 'repo', resource_id: 'repo-1' } })).toBe(
      'Repo · repo-1'
    );
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

    expect(composeMessageWithAttachments('Review these', attachments)).toContain('Attachments:');
    expect(composeMessageWithAttachments('', attachments)).toContain('Image: screen.png');
    expect(removePendingAttachment(attachments, 'att-1')).toMatchObject([{ id: 'att-2' }]);
  });

  it('builds managed-thread create and send payloads that match backend constraints', () => {
    expect(buildManagedThreadCreatePayload('codex')).toEqual({
      agent: 'codex',
      name: 'New PMA chat',
      workspace_root: '.'
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
      busy_policy: 'queue'
    });
    expect(buildManagedThreadMessagePayload('Continue', '', false)).toEqual({
      message: 'Continue',
      attachments: undefined,
      model: undefined,
      busy_policy: undefined
    });
  });

  it('summarizes model selector loading, empty, error, and loaded states', () => {
    expect(modelSelectorState(true, null, 0)).toMatchObject({ state: 'loading', disabled: true });
    expect(modelSelectorState(false, null, 0)).toMatchObject({ state: 'empty', disabled: true });
    expect(modelSelectorState(false, 'Agent missing provider', 0)).toMatchObject({ state: 'error', disabled: true });
    expect(modelSelectorState(false, null, 2)).toMatchObject({ state: 'loaded', disabled: false });
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
      'PMA final report',
      'Error / blocker',
      'Run event'
    ]);
    expect(views.every((view) => view.detailLabel)).toBe(true);
  });
});
