import { describe, expect, it } from 'vitest';
import type { PmaChatMessage, PmaChatSummary, PmaRunProgress, SurfaceArtifact } from './domain';
import {
  approvalActionUrl,
  artifactCardView,
  buildManagedThreadCreatePayload,
  buildManagedThreadMessagePayload,
  buildPmaCards,
  buildPmaLiveActivity,
  chooseActiveChatId,
  composeMessageWithAttachments,
  filterSensitiveCarApprovals,
  filterPmaChats,
  formatRelativeTime,
  isPrimaryProgressArtifact,
  modelSelectorState,
  progressPercent,
  removePendingAttachment,
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
  events: [{ ...baseArtifact, kind: 'progress', raw: { event_type: 'tool_completed' } }],
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

  it('prefers a requested linked chat when present', () => {
    const chats: PmaChatSummary[] = [
      baseChat,
      { ...baseChat, id: 'chat-2', title: 'Linked conversation', status: 'waiting' }
    ];

    expect(chooseActiveChatId(chats, 'chat-1', 'chat-2')).toBe('chat-2');
    expect(chooseActiveChatId(chats, 'chat-1', 'missing')).toBe('chat-1');
  });

  it('builds active chat cards for durable transcript content and artifacts', () => {
    const cards = buildPmaCards(
      [{ ...baseMessage, artifacts: [{ ...baseArtifact, id: 'message-attachment' }] }],
      baseProgress,
      baseChat,
      [baseArtifact]
    );

    expect(cards.map((card) => card.kind)).toEqual([
      'message',
      'artifact',
      'ticket',
      'artifact'
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
          raw: { event_type: 'token_usage' }
        },
        {
          ...baseArtifact,
          id: 'tool-started',
          kind: 'progress',
          title: 'Running tests',
          summary: 'pnpm test',
          raw: { event_type: 'tool_started' }
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

  it('skips empty message cards and suppresses debug-only lifecycle events from the transcript', () => {
    const cards = buildPmaCards(
      [{ ...baseMessage, id: 'empty-message', text: '' }],
      {
        ...baseProgress,
        events: [
          {
            ...baseArtifact,
            id: 'token-usage',
            kind: 'progress',
            title: 'Token usage updated',
            raw: { event_type: 'token_usage' }
          },
          {
            ...baseArtifact,
            id: 'turn-completed',
            kind: 'progress',
            title: 'Turn completed',
            raw: { event_type: 'turn_completed' }
          },
          {
            ...baseArtifact,
            id: 'assistant-update',
            kind: 'progress',
            title: 'Thinking',
            raw: { event_type: 'assistant_update' }
          }
        ]
      },
      null,
      []
    );

    expect(cards.some((card) => card.kind === 'message')).toBe(false);
    expect(cards.filter((card) => card.kind === 'artifact')).toHaveLength(0);
  });

  it('keeps low-level PMA events out of primary transcript cards while preserving final responses', () => {
    const cards = buildPmaCards(
      [
        {
          ...baseMessage,
          id: 'final-response',
          status: 'done',
          text: 'Done. The PMA smoke fixtures are now covered.'
        }
      ],
      {
        ...baseProgress,
        status: 'done',
        events: [
          {
            ...baseArtifact,
            id: 'raw-token-delta',
            kind: 'progress',
            title: 'Raw token delta',
            raw: { event_type: 'response.output_text.delta' }
          },
          {
            ...baseArtifact,
            id: 'low-level-lifecycle',
            kind: 'progress',
            title: 'Run loop tick',
            raw: { event_type: 'thread.run.step.delta' }
          },
          {
            ...baseArtifact,
            id: 'thinking-summary',
            kind: 'progress',
            title: 'Thinking summary',
            summary: 'Inspecting repo/worktree ownership.',
            raw: { event_type: 'assistant_update' }
          }
        ]
      },
      null,
      []
    );

    expect(cards.filter((card) => card.kind === 'message')).toHaveLength(1);
    expect(cards.find((card) => card.kind === 'message')).toMatchObject({
      message: { text: 'Done. The PMA smoke fixtures are now covered.' }
    });
    expect(cards.filter((card) => card.kind === 'artifact')).toHaveLength(0);
  });

  it('keeps raw progress classifications deterministic', () => {
    expect(
      isPrimaryProgressArtifact({
        ...baseArtifact,
        kind: 'progress',
        title: 'Token usage updated',
        raw: { event_type: 'token_usage' }
      })
    ).toBe(false);
    expect(
      isPrimaryProgressArtifact({
        ...baseArtifact,
        kind: 'progress',
        title: 'status-check completed',
        raw: { event_type: 'tool_completed' }
      })
    ).toBe(true);
  });

  it('derives compact progress and relative timestamps', () => {
    expect(progressPercent(baseChat, baseProgress)).toBe(64);
    expect(progressPercent({ ...baseChat, progressPercent: 41 }, baseProgress)).toBe(41);
    expect(formatRelativeTime('2026-05-04T00:00:00Z', new Date('2026-05-04T00:03:00Z'))).toBe('3m ago');
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
    expect(buildManagedThreadMessagePayload('Continue', 'gpt-5.2', true)).toEqual({
      message: 'Continue',
      model: 'gpt-5.2',
      busy_policy: 'queue'
    });
    expect(buildManagedThreadMessagePayload('Continue', '', false)).toEqual({
      message: 'Continue',
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

  it('filters sensitive CAR approvals without treating normal run progress as approval UI', () => {
    const approvals = filterSensitiveCarApprovals([
      {
        id: 'normal-command',
        title: 'Command completed',
        description: 'pnpm test passed',
        risk: 'low',
        action: 'command',
        createdAt: null,
        raw: {}
      },
      {
        id: 'delete-worktree',
        title: 'Delete worktree',
        description: 'Remove repo worktree from disk',
        risk: 'high',
        action: 'delete_worktree',
        createdAt: null,
        raw: { approve_url: '/approve', decline_url: '/decline' }
      }
    ]);

    expect(approvals).toMatchObject([{ id: 'delete-worktree' }]);
    expect(approvalActionUrl(approvals[0], 'approve')).toBe('/approve');
    expect(approvalActionUrl(approvals[0], 'decline')).toBe('/decline');
  });
});
