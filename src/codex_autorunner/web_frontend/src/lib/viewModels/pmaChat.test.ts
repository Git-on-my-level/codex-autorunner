import { describe, expect, it } from 'vitest';
import type { PmaChatSummary, PmaRunProgress, PmaTimelineItem, SurfaceArtifact } from './domain';
import { pmaTimelineContractFields } from './domain';
import {
  artifactCardView,
  buildManagedThreadCreatePayload,
  buildManagedThreadMessagePayload,
  agentCapabilityAllowed,
  buildPmaChatScopeOptions,
  buildChatTranscriptCards,
  buildChatActivityCards,
  buildPmaLiveActivity,
  buildPmaStatusBar,
  chooseActiveChatId,
  compactChatTranscriptCards,
  composeMessageWithAttachments,
  countTicketRunGroups,
  filterPmaChats,
  filterChatEntries,
  filterArtifactsForActiveChat,
  formatRelativeTime,
  formatCompactMessageDateTime,
  isPrimaryProgressArtifact,
  mergeChatActivityEvents,
  mapChatTranscriptSnapshot,
  mapChatSurfaceSnapshotToPmaChats,
  mapChatSurfaceEventToPmaChatSummary,
  modelReasoningOptions,
  modelSelectorState,
  pmaChatKind,
  pmaChatKindLabel,
  pmaChatHeaderScopeLine,
  chatMessengerSurface,
  pmaChatScopeLabelFromChat,
  pmaChatScopeTagView,
  chatSurfaceFilterOptions,
  chatSurfaceFilterToken,
  progressPercent,
  reconcileChatSurfaceEvent,
  reconcileChatSurfaceSnapshot,
  removePendingAttachment,
  sortChatsUnreadFirst,
  sortChatsWaitingFirst,
  summarizeFilterCounts,
  buildChatListEntries
} from './pmaChat';
import { resolvePmaChatSelectorsForActiveChat } from './modelPickers';

const baseChat: PmaChatSummary = {
  id: 'chat-1',
  title: 'Repo repair',
  lifecycleStatus: 'active',
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
    ...pmaTimelineContractFields(id),
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
  startedAt: '2026-05-04T00:00:00Z',
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

function baseArtifactCardTrace(id: string, text: string, eventIds: string[]) {
  return {
    kind: 'intermediate' as const,
    id,
    title: text,
    text,
    eventIds,
    progressSourceIds: [],
    detail: null,
    turnId: 'one',
    orderKey: id,
    timestamp: '2026-05-04T00:00:01Z'
  };
}

describe('PMA chat view helpers', () => {
  it('collapses ticket-flow chats sharing a worktree into one run group, even without ticket ids', () => {
    const chats: PmaChatSummary[] = [
      { ...baseChat, id: 'tf-1', ticketId: null, isTicketFlow: true, worktreeId: 'wt-A', repoId: 'repo-1' },
      { ...baseChat, id: 'tf-2', ticketId: null, isTicketFlow: true, worktreeId: 'wt-A', repoId: 'repo-1' },
      { ...baseChat, id: 'tf-3', ticketId: null, isTicketFlow: true, worktreeId: 'wt-A', repoId: 'repo-1' }
    ];
    const entries = buildChatListEntries(chats, { groupRuns: true });
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
    const entries = buildChatListEntries(chats, { groupRuns: true });
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
    const entries = buildChatListEntries(chats, { groupRuns: true });
    const filtered = filterChatEntries(entries, 'ticket_runs', '', {});
    expect(filtered).toHaveLength(2);
    expect(filtered.every((e) => e.kind === 'group')).toBe(true);
  });

  it('keeps separate ticket-flow runs under the same scope when run ids differ', () => {
    const chats: PmaChatSummary[] = [
      { ...baseChat, id: 'run-a-ticket-1', runId: 'run-a', isTicketFlow: true, worktreeId: 'wt-1', repoId: 'repo-1' },
      { ...baseChat, id: 'run-a-ticket-2', runId: 'run-a', isTicketFlow: true, worktreeId: 'wt-1', repoId: 'repo-1' },
      { ...baseChat, id: 'run-b-ticket-1', runId: 'run-b', isTicketFlow: true, worktreeId: 'wt-1', repoId: 'repo-1' }
    ];

    const entries = buildChatListEntries(chats, { groupRuns: true });

    expect(entries).toHaveLength(2);
    expect(entries.map((entry) => entry.kind === 'group' ? entry.group.key : '').sort()).toEqual([
      'worktree:wt-1:run:run-a',
      'worktree:wt-1:run:run-b'
    ]);
  });

  it('keeps separate ticket-flow runs under the same worktree in separate groups when run ids are present', () => {
    const chats: PmaChatSummary[] = [
      { ...baseChat, id: 'run-1-a', runId: 'run-1', ticketId: 'TICKET-001', worktreeId: 'wt-1' },
      { ...baseChat, id: 'run-1-b', runId: 'run-1', ticketId: 'TICKET-002', worktreeId: 'wt-1' },
      { ...baseChat, id: 'run-2-a', runId: 'run-2', ticketId: 'TICKET-003', worktreeId: 'wt-1' }
    ];

    const entries = buildChatListEntries(chats, { groupRuns: true });

    expect(entries).toHaveLength(2);
    expect(entries.filter((entry) => entry.kind === 'group').map((entry) => entry.group.key).sort()).toEqual([
      'worktree:wt-1:run:run-1',
      'worktree:wt-1:run:run-2'
    ]);
  });

  it('merges overlapping live assistant progress without duplicating snapshots', () => {
    const cards = buildChatActivityCards([
      {
        ...baseArtifact,
        id: 'progress-1',
        kind: 'progress',
        summary: 'Let me load the PMA skill',
        raw: {
          managed_turn_id: 'turn-1',
          progress_item: {
            item_id: 'progress:assistant_update:0001',
            kind: 'assistant_update',
            title: 'Thinking',
            summary: 'Let me load the PMA skill',
            event_ids: [1]
          }
        }
      },
      {
        ...baseArtifact,
        id: 'progress-2',
        kind: 'progress',
        summary: 'PMA skill first, then gather data',
        raw: {
          managed_turn_id: 'turn-1',
          progress_item: {
            item_id: 'progress:assistant_update:0002',
            kind: 'assistant_update',
            title: 'Thinking',
            summary: 'PMA skill first, then gather data',
            event_ids: [2]
          }
        }
      }
    ]);

    expect(cards).toHaveLength(1);
    expect(cards[0].kind).toBe('intermediate');
    if (cards[0].kind === 'intermediate') {
      expect(cards[0].text).toBe('Let me load the PMA skill first, then gather data');
    }
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
    expect(summarizeFilterCounts(chats, lastSeen)).toEqual({ all: 3, active: 1, waiting: 1, unread: 2, archived: 0 });
  });

  it('keeps archived chats out of the working filters and exposes them through archived', () => {
    const chats: PmaChatSummary[] = [
      baseChat,
      { ...baseChat, id: 'chat-2', title: 'Old support thread', lifecycleStatus: 'archived', status: 'done' },
      { ...baseChat, id: 'chat-3', title: 'Waiting approval', status: 'waiting' }
    ];

    expect(filterPmaChats(chats, 'all', '').map((chat) => chat.id)).toEqual(['chat-1', 'chat-3']);
    expect(filterPmaChats(chats, 'archived', 'support').map((chat) => chat.id)).toEqual(['chat-2']);
    expect(filterPmaChats(chats, 'unread', '').map((chat) => chat.id).sort()).toEqual(['chat-1', 'chat-3']);
    expect(summarizeFilterCounts(chats)).toEqual({ all: 2, active: 1, waiting: 1, unread: 2, archived: 1 });
  });

  it('drops archived ticket-flow chats from grouped run rows when not on the archived filter', () => {
    const active = {
      ...baseChat,
      id: 'tf-active',
      isTicketFlow: true,
      worktreeId: 'wt-collab',
      repoId: 'repo-1'
    };
    const archivedViaRaw: PmaChatSummary = {
      ...baseChat,
      id: 'tf-archived',
      isTicketFlow: true,
      worktreeId: 'wt-collab',
      repoId: 'repo-1',
      lifecycleStatus: null,
      status: 'done',
      raw: { lifecycle: 'archived' }
    };
    const chats: PmaChatSummary[] = [active, archivedViaRaw];
    const entries = buildChatListEntries(chats, { groupRuns: true });
    expect(entries).toHaveLength(1);
    const filtered = filterChatEntries(entries, 'all', '', {});
    expect(filtered).toHaveLength(1);
    if (filtered[0].kind !== 'group') throw new Error('expected group row');
    expect(filtered[0].group.chats.map((c) => c.id)).toEqual(['tf-active']);
    expect(filtered[0].group.totalCount).toBe(1);
  });

  it('detects messenger surface from API fields, not protocol-id titles', () => {
    expect(
      chatMessengerSurface({
        ...baseChat,
        title: 'discord:123',
        raw: {}
      })
    ).toBeNull();

    expect(
      chatMessengerSurface({
        ...baseChat,
        title: 'General',
        raw: { surface_kind: 'discord', surface_key: 'ch-1' }
      })
    ).toEqual({ slug: 'discord', label: 'Discord', badgeClass: 'surface-discord' });

    expect(
      chatMessengerSurface({
        ...baseChat,
        title: 'side thread',
        raw: { managed_thread_id: 't1', surface_urn: 'managed_thread:t1' }
      })
    ).toBeNull();
  });

  it('maps generic chat surface snapshots into chat-list rows', () => {
    const chats = mapChatSurfaceSnapshotToPmaChats({
      surfaces: [
        {
          surface_kind: 'discord',
          surface_key: 'channel-1',
          managed_thread_id: 'thread-1',
          facts: ['managed_thread'],
          lifecycle: 'running',
          lifecycle_status: 'active',
          resource_owner: { repo_id: 'repo-1', resource_kind: 'repo', resource_id: 'repo-1' },
          display: { display_name: 'Discord Ops' },
          updated_at: '2026-05-04T01:00:00Z',
          metadata: { agent_id: 'codex', agent_profile: 'm4-pma', latest_execution_status: 'running' }
        },
        {
          surface_kind: 'telegram',
          surface_key: '-100:42',
          lifecycle: 'discovered',
          display: { display_name: 'Telegram Topic' }
        }
      ]
    });

    expect(chats).toMatchObject([
      {
        id: 'thread-1',
        title: 'Discord Ops',
        status: 'running',
        agentProfile: 'm4-pma',
        repoId: 'repo-1',
        raw: { surface_kind: 'discord', surface_key: 'channel-1', binding_kind: 'discord', binding_id: 'channel-1' }
      }
    ]);
  });

  it('does not map unbound or stale surface inventory into selectable chats', () => {
    const chats = mapChatSurfaceSnapshotToPmaChats({
      surfaces: [
        {
          surface_kind: 'notification',
          surface_key: 'notification:abc',
          lifecycle: 'discovered',
          display: { display_name: 'Notification abc' }
        },
        {
          surface_kind: 'discord',
          surface_key: 'channel-1',
          managed_thread_id: 'missing-thread',
          facts: ['binding'],
          display: { display_name: 'Stale bound thread' }
        },
        {
          surface_kind: 'discord',
          surface_key: 'channel-2',
          managed_thread_id: 'missing-facts-thread',
          display: { display_name: 'Missing facts thread' }
        },
        {
          surface_kind: 'discord',
          surface_key: 'channel-3',
          managed_thread_id: 'non-array-facts-thread',
          facts: 'managed_thread',
          display: { display_name: 'Non-array facts thread' }
        },
        {
          surface_kind: 'pma',
          surface_key: 'live-thread',
          managed_thread_id: 'live-thread',
          facts: ['managed_thread'],
          display: { display_name: 'Live thread' }
        }
      ]
    });

    expect(chats.map((chat) => chat.id)).toEqual(['live-thread']);
  });

  it('prefers projection lifecycle over stale latest execution when thread is archived', () => {
    const chats = mapChatSurfaceSnapshotToPmaChats({
      surfaces: [
        {
          surface_kind: 'pma',
          surface_key: 'thread-archived',
          managed_thread_id: 'thread-archived',
          facts: ['managed_thread'],
          lifecycle: 'archived',
          lifecycle_status: 'archived',
          display: { display_name: 'Archived chat' },
          metadata: {
            runtime_status: 'archived',
            latest_execution_status: 'running'
          }
        }
      ]
    });

    expect(chats).toMatchObject([
      {
        id: 'thread-archived',
        status: 'idle',
        raw: {
          normalized_status: 'archived',
          status: 'archived'
        }
      }
    ]);
  });

  it('prefers queued projection lifecycle over terminal runtime for sidebar status', () => {
    const chats = mapChatSurfaceSnapshotToPmaChats({
      surfaces: [
        {
          surface_kind: 'pma',
          surface_key: 'thread-queued',
          managed_thread_id: 'thread-queued',
          facts: ['managed_thread'],
          lifecycle: 'queued',
          lifecycle_status: 'active',
          display: { display_name: 'Follow-up queued' },
          metadata: {
            runtime_status: 'completed',
            latest_execution_status: 'queued',
            queue_depth: 1
          }
        }
      ]
    });

    expect(chats).toMatchObject([
      {
        id: 'thread-queued',
        status: 'waiting',
        raw: {
          normalized_status: 'queued',
          status: 'queued'
        }
      }
    ]);
  });

  it('maps chat surface unread activity from visible event metadata before row updates', () => {
    const chats = mapChatSurfaceSnapshotToPmaChats({
      surfaces: [
        {
          surface_kind: 'discord',
          surface_key: 'channel-1',
          managed_thread_id: 'thread-1',
          facts: ['managed_thread'],
          lifecycle: 'completed',
          display: { display_name: 'Discord Ops' },
          updated_at: '2026-05-11T05:48:46Z',
          metadata: {
            last_activity_at: '2026-05-08T12:00:00Z'
          }
        }
      ]
    });

    expect(chats[0].updatedAt).toBe('2026-05-08T12:00:00Z');
  });

  it('reconciles generic chat snapshots with active-thread replacement by surface binding', () => {
    const current: PmaChatSummary[] = [
      { ...baseChat, id: 'old-thread', lifecycleStatus: 'active', raw: { binding_kind: 'discord', binding_id: 'channel-1' } }
    ];
    const next = [
      { ...baseChat, id: 'old-thread', lifecycleStatus: 'archived', raw: { binding_kind: 'discord', binding_id: 'channel-1' } },
      { ...baseChat, id: 'new-thread', lifecycleStatus: 'active', raw: { binding_kind: 'discord', binding_id: 'channel-1' } }
    ];

    expect(reconcileChatSurfaceSnapshot(current, next, 'old-thread')).toEqual({
      chats: next,
      replacementChatId: 'new-thread'
    });
  });

  it('applies generic chat events through the same chat-list reconciliation path', () => {
    const current: PmaChatSummary[] = [
      {
        ...baseChat,
        id: 'thread-1',
        title: 'Discord Ops',
        status: 'idle',
        raw: { binding_kind: 'discord', binding_id: 'channel-1', surface_kind: 'discord', surface_key: 'channel-1' }
      }
    ];
    const next = reconcileChatSurfaceEvent(current, {
      event_type: 'queue.state_changed',
      surface: { surface_kind: 'discord', surface_key: 'channel-1' },
      managed_thread_id: 'thread-1',
      lifecycle: 'queued',
      lifecycle_status: 'active',
      status: 'queued',
      occurred_at: '2026-05-04T01:00:00Z'
    });

    expect(next).toMatchObject([
      {
        id: 'thread-1',
        title: 'Discord Ops',
        status: 'waiting',
        updatedAt: '2026-05-04T01:00:00Z'
      }
    ]);
  });

  it('maps chat surface events as managed-thread projections', () => {
    const chat = mapChatSurfaceEventToPmaChatSummary({
      event_type: 'queue.state_changed',
      surface: { surface_kind: 'discord', surface_key: 'channel-1' },
      managed_thread_id: 'thread-1',
      lifecycle: 'queued',
      lifecycle_status: 'active',
      status: 'queued',
      occurred_at: '2026-05-04T01:00:00Z',
      details: { channel: { display: 'Discord Ops' } }
    });

    expect(chat).toMatchObject({
      id: 'thread-1',
      title: 'Discord Ops',
      status: 'waiting',
      raw: {
        facts: ['managed_thread'],
        surface_kind: 'discord',
        surface_key: 'channel-1'
      }
    });
  });

  it('accepts human channel titles that contain colons during event reconciliation', () => {
    const current: PmaChatSummary[] = [
      {
        ...baseChat,
        id: 'thread-1',
        title: 'discord:1495134681929355404',
        raw: { binding_kind: 'discord', binding_id: '1495134681929355404', surface_kind: 'discord', surface_key: '1495134681929355404' }
      }
    ];

    const next = reconcileChatSurfaceEvent(current, {
      event_type: 'channel_directory.discovered',
      surface: { surface_kind: 'discord', surface_key: '1495134681929355404' },
      managed_thread_id: 'thread-1',
      lifecycle: 'discovered',
      lifecycle_status: 'active',
      status: 'discovered',
      occurred_at: '2026-05-04T01:00:00Z',
      details: { channel: { display: 'guild:149 / #codex' } }
    });

    expect(next[0]?.title).toBe('guild:149 / #codex');
  });

  it('does not replace a chat title with a protocol id fallback from events', () => {
    const current: PmaChatSummary[] = [
      {
        ...baseChat,
        id: 'thread-1',
        title: 'Agent Nexus / #codex',
        raw: { binding_kind: 'discord', binding_id: '1495134681929355404', surface_kind: 'discord', surface_key: '1495134681929355404' }
      }
    ];

    const next = reconcileChatSurfaceEvent(current, {
      event_type: 'surface.bound',
      surface: { surface_kind: 'discord', surface_key: '1495134681929355404' },
      managed_thread_id: 'thread-1',
      lifecycle: 'bound',
      lifecycle_status: 'active',
      status: 'bound',
      occurred_at: '2026-05-04T01:00:00Z',
      details: { channel: { display: 'discord:1495134681929355404' } }
    });

    expect(next[0]?.title).toBe('Agent Nexus / #codex');
  });

  it('maps chat surface event thread details into metadata.agent_id', () => {
    const chat = mapChatSurfaceEventToPmaChatSummary({
      event_type: 'lifecycle.status_changed',
      surface: { surface_kind: 'pma', surface_key: 'thread-99' },
      managed_thread_id: 'thread-99',
      lifecycle: 'idle',
      lifecycle_status: 'active',
      status: 'created',
      occurred_at: '2026-05-04T01:00:00Z',
      details: {
        thread: {
          managed_thread_id: 'thread-99',
          agent_id: 'codex',
          agent_profile: 'm4-pma',
          model: 'gpt-5'
        }
      }
    });

    expect(chat?.agentId).toBe('codex');
    expect(chat?.agentProfile).toBe('m4-pma');
    expect(chat?.model).toBe('gpt-5');
  });

  it('does not clear chat agent identity when reconciling status-only surface events', () => {
    const current: PmaChatSummary[] = [
      {
        ...baseChat,
        id: 'thread-1',
        title: 'My chat',
        agentId: 'codex',
        agentProfile: 'pma',
        model: null,
        raw: {}
      }
    ];
    const next = reconcileChatSurfaceEvent(current, {
      event_type: 'queue.state_changed',
      surface: { surface_kind: 'pma', surface_key: 'thread-1' },
      managed_thread_id: 'thread-1',
      lifecycle: 'queued',
      lifecycle_status: 'active',
      status: 'queued',
      occurred_at: '2026-05-04T01:00:00Z'
    });
    expect(next[0]?.agentId).toBe('codex');
    expect(next[0]?.agentProfile).toBe('pma');
  });

  it('resolvePmaChatSelectorsForActiveChat restores defaults when chat has no agent', () => {
    const withAgent = resolvePmaChatSelectorsForActiveChat(
      { ...baseChat, id: 'a', agentId: 'opencode', agentProfile: 'x', model: 'm-1', raw: {} },
      [{ id: 'codex' }, { id: 'opencode' }],
      'codex',
      'profile-a'
    );
    expect(withAgent).toEqual({
      mode: 'chat-bound',
      agentId: 'opencode',
      agentProfile: 'x',
      reasoning: '',
      model: 'm-1'
    });

    const noAgent = resolvePmaChatSelectorsForActiveChat(
      { ...baseChat, id: 'b', agentId: null, agentProfile: null, model: null, raw: {} },
      [{ id: 'codex' }, { id: 'opencode' }],
      'codex',
      ''
    );
    expect(noAgent).toEqual({
      mode: 'defaults',
      agentId: 'codex',
      agentProfile: '',
      reasoning: ''
    });

    const hermesDefault = resolvePmaChatSelectorsForActiveChat(
      { ...baseChat, id: 'c', agentId: null, agentProfile: null, model: null, raw: {} },
      [{ id: 'hermes' }],
      'hermes',
      'm4-pma'
    );
    expect(hermesDefault).toEqual({
      mode: 'defaults',
      agentId: 'hermes',
      agentProfile: 'm4-pma',
      reasoning: ''
    });
  });

  it('filters chats by messenger surface slug', () => {
    const discordChat = { ...baseChat, id: 'd1', title: 'Engineering', raw: { surface_kind: 'discord' } };
    const hubChat = { ...baseChat, id: 'h1', title: 'Chat · repo', raw: {} };
    const list = [discordChat, hubChat];
    expect(filterPmaChats(list, chatSurfaceFilterToken('discord'), '')).toEqual([discordChat]);
    expect(chatSurfaceFilterOptions(list)).toEqual([{ slug: 'discord', label: 'Discord', count: 1 }]);
  });

  it('gives notification chats their own surface filter instead of generic other when identifiable', () => {
    const notificationChat = { ...baseChat, id: 'n1', title: 'Notification run_finished', raw: { surface_kind: 'other' } };
    const list = [notificationChat, { ...baseChat, id: 'h1', title: 'Chat', raw: {} }];

    expect(chatMessengerSurface(notificationChat)).toEqual({
      slug: 'notifications',
      label: 'Notifications',
      badgeClass: 'surface-notifications'
    });
    expect(chatSurfaceFilterOptions(list)).toEqual([{ slug: 'notifications', label: 'Notifications', count: 1 }]);
    expect(filterPmaChats(list, chatSurfaceFilterToken('notifications'), '')).toEqual([notificationChat]);
  });

  it('sorts waiting chats ahead of others then by recent updates', () => {
    const chats: PmaChatSummary[] = [
      { ...baseChat, id: 'a', status: 'running', updatedAt: '2026-05-04T03:00:00Z' },
      { ...baseChat, id: 'b', status: 'waiting', updatedAt: '2026-05-04T01:00:00Z' },
      { ...baseChat, id: 'c', status: 'waiting', updatedAt: '2026-05-04T02:00:00Z' }
    ];
    expect(sortChatsWaitingFirst(chats).map((chat) => chat.id)).toEqual(['c', 'b', 'a']);
  });

  it('sorts chats unread first, then by recent updates', () => {
    const chats: PmaChatSummary[] = [
      { ...baseChat, id: 'read-new', status: 'running', updatedAt: '2026-05-04T04:00:00Z' },
      { ...baseChat, id: 'unread-old', status: 'idle', updatedAt: '2026-05-04T01:00:00Z' },
      { ...baseChat, id: 'unread-new', status: 'idle', updatedAt: '2026-05-04T03:00:00Z' },
      { ...baseChat, id: 'read-old', status: 'waiting', updatedAt: '2026-05-04T02:00:00Z' }
    ];
    const lastSeen = {
      'read-new': '2026-05-04T04:00:00Z',
      'read-old': '2026-05-04T02:00:00Z'
    };

    expect(sortChatsUnreadFirst(chats, lastSeen).map((chat) => chat.id)).toEqual([
      'unread-new',
      'unread-old',
      'read-new',
      'read-old'
    ]);
    expect(buildChatListEntries(chats, { groupRuns: false, lastSeen }).map((entry) => entry.kind === 'chat' ? entry.chat.id : '')).toEqual([
      'unread-new',
      'unread-old',
      'read-new',
      'read-old'
    ]);
  });

  it('ignores backend unread counts for filters, sort, and run-group unread totals', () => {
    const chats: PmaChatSummary[] = [
      { ...baseChat, id: 'backend-read', unreadCount: 0, updatedAt: '2026-05-04T05:00:00Z' },
      { ...baseChat, id: 'backend-unread-low', unreadCount: 1, updatedAt: '2026-05-04T01:00:00Z' },
      { ...baseChat, id: 'backend-unread-high', unreadCount: 3, updatedAt: '2026-05-04T02:00:00Z' }
    ];
    const lastSeen = {
      'backend-read': '2026-05-04T05:00:00Z'
    };

    expect(filterPmaChats(chats, 'unread', '', lastSeen)).toMatchObject([
      { id: 'backend-unread-low' },
      { id: 'backend-unread-high' }
    ]);
    expect(sortChatsUnreadFirst(chats, lastSeen).map((chat) => chat.id)).toEqual([
      'backend-unread-high',
      'backend-unread-low',
      'backend-read'
    ]);

    const entries = buildChatListEntries(chats, { groupRuns: true, lastSeen });
    expect(entries).toHaveLength(1);
    expect(entries[0].kind).toBe('group');
    if (entries[0].kind === 'group') {
      expect(entries[0].group.unreadCount).toBe(2);
    }
  });

  it('uses timestamp read markers instead of backend unread counts', () => {
    const chats: PmaChatSummary[] = [
      { ...baseChat, id: 'backend-unread', unreadCount: 2, updatedAt: '2026-05-04T01:00:00Z' },
      { ...baseChat, id: 'read-newer', unreadCount: 0, updatedAt: '2026-05-04T03:00:00Z' }
    ];

    expect(
      filterPmaChats(chats, 'unread', '', {
        'backend-unread': '2026-05-04T01:00:00Z',
        'read-newer': '2026-05-04T03:00:00Z'
      }).map((chat) => chat.id)
    ).toEqual([]);
    expect(sortChatsUnreadFirst(chats, { 'read-newer': '2026-05-04T03:00:00Z' }).map((chat) => chat.id)).toEqual([
      'backend-unread',
      'read-newer'
    ]);
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
    const cards = buildChatTranscriptCards(
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

  it('maps backend-owned PMA transcript snapshots into renderable cards and status', () => {
    const snapshot = mapChatTranscriptSnapshot(
      {
        rows: [
          {
            kind: 'message',
            id: 'turn:1:user',
            turn_id: '1',
            order_key: '001',
            timestamp: '2026-05-04T00:00:01Z',
            message: {
              id: 'turn:1:user',
              chat_id: 'chat-1',
              role: 'user',
              text: 'hello transcript',
              created_at: '2026-05-04T00:00:01Z',
              artifacts: []
            }
          },
          {
            kind: 'intermediate',
            id: 'turn:1:intermediate:1',
            title: 'Thinking',
            text: 'checking repo',
            order_key: '002'
          }
        ],
        status: {
          managed_thread_id: 'chat-1',
          managed_turn_id: 'run-1',
          turn_status: 'running',
          phase: 'testing',
          events: []
        }
      },
      (raw) => ({ ...baseProgress, id: String(raw.managed_turn_id), phase: String(raw.phase) })
    );

    expect(snapshot.rows.map((row) => row.kind)).toEqual(['message', 'intermediate']);
    expect(snapshot.rows[0]).toMatchObject({
      kind: 'message',
      message: { role: 'user', text: 'hello transcript' }
    });
    expect(snapshot.status).toMatchObject({ id: 'run-1', phase: 'testing' });
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

  it('prefers specific progress labels over generic progress/update fallbacks', () => {
    const activityCards = buildChatActivityCards([
      {
        ...baseArtifact,
        id: 'progress-1',
        kind: 'progress',
        title: 'Progress',
        summary: 'Starting pytest',
        raw: {
          managed_turn_id: 'turn-1',
          event_type: 'progress',
          phase: 'testing',
          progress_item: {
            kind: 'notice',
            title: 'Progress',
            summary: 'Starting pytest',
            event_ids: [21]
          }
        }
      }
    ]);

    expect(activityCards).toHaveLength(1);
    expect(activityCards[0]).toMatchObject({
      kind: 'intermediate',
      title: 'Starting pytest',
      text: 'Starting pytest'
    });

    const transcriptCards = buildChatTranscriptCards(
      [
        timelineItem(
          'turn:one:intermediate:21',
          'intermediate',
          {
            intermediate_kind: 'progress',
            title: 'Starting pytest',
            text: 'Starting pytest',
            event_type: 'progress',
            event: {
              event_type: 'progress',
              title: 'Starting pytest',
              summary: 'Starting pytest',
              phase: 'testing',
              progress_kind: 'notice'
            },
            progress_item: {
              kind: 'notice',
              title: 'Starting pytest',
              summary: 'Starting pytest',
              event_ids: [21]
            }
          },
          '00000021'
        )
      ],
      null,
      []
    );

    expect(transcriptCards).toHaveLength(1);
    expect(transcriptCards[0]).toMatchObject({
      kind: 'intermediate',
      title: 'Starting pytest',
      text: 'Starting pytest'
    });
  });

  it('falls back to phase metadata when the progress text is generic', () => {
    const activityCards = buildChatActivityCards([
      {
        ...baseArtifact,
        id: 'progress-2',
        kind: 'progress',
        title: 'Progress',
        summary: 'Progress',
        raw: {
          managed_turn_id: 'turn-1',
          event_type: 'progress',
          phase: 'testing',
          progress_item: {
            kind: 'notice',
            title: 'Progress',
            summary: 'Progress',
            event_ids: [22]
          }
        }
      }
    ]);

    expect(activityCards).toHaveLength(1);
    expect(activityCards[0]).toMatchObject({
      kind: 'intermediate',
      title: 'testing',
      text: 'Progress'
    });
  });

  it('builds a thin status bar from backend status fields', () => {
    expect(buildPmaStatusBar({ ...baseProgress, elapsedSeconds: 125, queueDepth: 2 }, baseChat)).toEqual({
      state: 'running',
      phase: 'testing',
      elapsedLabel: '2m 5s elapsed',
      elapsedValue: '2m 5s',
      queueDepth: 2,
      queueDepthLabel: 'queue 2',
      tokenUsageLabel: null,
      totalTokensFull: null,
      totalTokensCompact: null,
      inputTokensFull: null,
      inputTokensCompact: null,
      outputTokensFull: null,
      outputTokensCompact: null,
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
    const cards = buildChatTranscriptCards(
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
    const cards = buildChatTranscriptCards(
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
    const cards = buildChatTranscriptCards(
      [
        timelineItem('turn:one:user', 'user_message', { text: 'Create tickets' }, '001'),
        {
          ...timelineItem('turn:one:intermediate:think-1', 'intermediate', { intermediate_kind: 'thinking', text: 'Inspecting repo state.', event: { kind: 'thinking', message: 'Inspecting repo state.' } }, '002'),
          ...pmaTimelineContractFields('turn:one:intermediate:think-1', { sourceEventIds: ['turn:one:intermediate:think-1'] })
        },
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

  it('treats Hermes tool_call progress items as tool cards', () => {
    const cards = buildChatActivityCards([
      {
        ...baseArtifact,
        id: 'hermes-tool-1',
        kind: 'progress',
        createdAt: '2026-05-04T00:00:11Z',
        raw: {
          progress_item: {
            kind: 'tool_call',
            state: 'completed',
            tool_name: 'shell',
            summary: 'git status',
            event_ids: [21]
          }
        }
      }
    ]);

    expect(cards).toMatchObject([
      {
        kind: 'tool_group',
        tools: [{ title: 'shell', summary: 'git status', state: 'completed' }]
      }
    ]);
  });

  it('does not merge live progress notices across tool activity', () => {
    const cards = buildChatActivityCards(
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

  it('does not merge live progress notices across tool activity', () => {
    const cards = buildChatActivityCards(
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

  it('drops decode-failure lifecycle noise from canonical and live activity cards', () => {
    expect(
      buildChatTranscriptCards(
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
      buildChatActivityCards([
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
    const cards = buildChatTranscriptCards(
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
      buildChatActivityCards([
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
    const cards = buildChatTranscriptCards(
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

  it('preserves grouped tool provenance across all contributing events', () => {
    const cards = buildChatTranscriptCards(
      [
        timelineItem('turn:one:user', 'user_message', { text: 'Refactor' }, '001'),
        {
          ...timelineItem('turn:one:tool:group:1', 'tool_group', {
            tool_name: 'multi-tool-group',
            progress_items: [
              { event_ids: ['evt-51'] },
              { event_ids: ['evt-52'] },
              { event_ids: ['evt-53'] }
            ],
            call: { summary: 'Refactor pipeline' },
            result: { status: 'completed', summary: '3 tools completed' }
          }, '002'),
          ...pmaTimelineContractFields('turn:one:tool:group:1', {
            sourceEventIds: ['evt-51', 'evt-52', 'evt-53'],
            progressEventIds: ['evt-51', 'evt-52', 'evt-53'],
            progressItemIds: ['prog-51', 'prog-52', 'prog-53']
          })
        }
      ],
      null,
      []
    );

    expect(cards).toHaveLength(2);
    expect(cards[1]).toMatchObject({
      kind: 'tool_group',
      id: 'turn:one:tool:group:1',
      tools: [{ title: 'multi-tool-group', state: 'completed', summary: '3 tools completed' }]
    });
    const toolCard = cards[1];
    if (toolCard.kind !== 'tool_group') throw new Error('expected tool_group');
    expect(toolCard.tools[0].eventIds).toContain('evt-51');
    expect(toolCard.tools[0].eventIds).toContain('evt-52');
    expect(toolCard.tools[0].eventIds).toContain('evt-53');
  });

  it('compacts dense transcript activity into expandable turn summaries', () => {
    const cards = compactChatTranscriptCards([
      {
        kind: 'message',
        id: 'turn:one:user',
        turnId: 'one',
        orderKey: '001',
        timestamp: '2026-05-04T00:00:00Z',
        message: {
          id: 'turn:one:user',
          chatId: 'chat-1',
          role: 'user',
          text: 'Investigate',
          createdAt: '2026-05-04T00:00:00Z',
          status: null,
          artifacts: [],
          raw: {}
        }
      },
      {
        ...baseArtifactCardTrace('need', 'Need', ['197']),
        orderKey: '002'
      },
      {
        ...baseArtifactCardTrace('to', 'to', ['198']),
        orderKey: '003'
      },
      {
        kind: 'tool_group',
        id: 'tool-1',
        turnId: 'one',
        orderKey: '004',
        timestamp: '2026-05-04T00:00:03Z',
        tools: [
          {
            id: 'tool-1',
            title: 'rg',
            summary: 'Search files',
            detail: null,
            state: 'completed',
            eventIds: ['199']
          }
        ]
      },
      {
        kind: 'tool_group',
        id: 'tool-2',
        turnId: 'one',
        orderKey: '005',
        timestamp: '2026-05-04T00:00:04Z',
        tools: [
          {
            id: 'tool-2',
            title: 'pytest',
            summary: 'Run tests',
            detail: null,
            state: 'completed',
            eventIds: ['200']
          }
        ]
      }
    ]);

    expect(cards).toHaveLength(2);
    expect(cards[1]).toMatchObject({
      kind: 'turn_summary',
      title: '2 tool calls, 2 thinking updates'
    });
    const summary = cards[1];
    if (summary.kind !== 'turn_summary') throw new Error('expected turn summary');
    expect(summary.cards).toHaveLength(2);
    expect(summary.cards[0]).toMatchObject({
      kind: 'intermediate',
      title: 'Thinking',
      text: 'Need to',
      eventIds: ['197', '198']
    });
    expect(summary.cards[1]).toMatchObject({
      kind: 'tool_group',
      tools: [
        { title: 'rg', eventIds: ['199'] },
        { title: 'pytest', eventIds: ['200'] }
      ]
    });
  });

  it('keeps numeric token-like progress updates out of thinking summaries', () => {
    const cards = compactChatTranscriptCards([
      {
        ...baseArtifactCardTrace('progress-10', '10%', ['301']),
        turnId: 'progress-turn',
        orderKey: '001'
      },
      {
        ...baseArtifactCardTrace('progress-20', '20%', ['302']),
        turnId: 'progress-turn',
        orderKey: '002'
      }
    ]);

    expect(cards).toHaveLength(1);
    expect(cards[0]).toMatchObject({
      kind: 'turn_summary',
      title: '2 progress updates'
    });
    const summary = cards[0];
    if (summary.kind !== 'turn_summary') throw new Error('expected turn summary');
    expect(summary.cards[0]).toMatchObject({
      kind: 'intermediate',
      title: 'Progress',
      text: '10% 20%',
      eventIds: ['301', '302']
    });
  });

  it('preserves explicit non-thinking labels when merging token-like updates', () => {
    const cards = compactChatTranscriptCards([
      {
        ...baseArtifactCardTrace('status-queued', 'queued', ['311']),
        title: 'Status',
        turnId: 'status-turn',
        orderKey: '001'
      },
      {
        ...baseArtifactCardTrace('status-running', 'running', ['312']),
        title: 'Status',
        turnId: 'status-turn',
        orderKey: '002'
      }
    ]);

    expect(cards).toHaveLength(1);
    expect(cards[0]).toMatchObject({
      kind: 'turn_summary',
      title: '2 progress updates'
    });
    const summary = cards[0];
    if (summary.kind !== 'turn_summary') throw new Error('expected turn summary');
    expect(summary.cards[0]).toMatchObject({
      kind: 'intermediate',
      title: 'Status',
      text: 'queued running',
      eventIds: ['311', '312']
    });
  });

  it('keeps compact transcript ids stable as streaming activity grows', () => {
    const first = compactChatTranscriptCards([
      baseArtifactCardTrace('thinking-1', 'Need', ['401']),
      baseArtifactCardTrace('thinking-2', 'to', ['402'])
    ]);
    const second = compactChatTranscriptCards([
      baseArtifactCardTrace('thinking-1', 'Need', ['401']),
      baseArtifactCardTrace('thinking-2', 'to', ['402']),
      baseArtifactCardTrace('thinking-3', 'check', ['403'])
    ]);

    expect(first[0]).toMatchObject({ kind: 'turn_summary', id: 'turn:one:activity:thinking-1' });
    expect(second[0]).toMatchObject({ kind: 'turn_summary', id: 'turn:one:activity:thinking-1' });
    const firstSummary = first[0];
    const secondSummary = second[0];
    if (firstSummary.kind !== 'turn_summary' || secondSummary.kind !== 'turn_summary') {
      throw new Error('expected turn summaries');
    }
    expect(firstSummary.cards[0]).toMatchObject({ id: 'thinking-1', text: 'Need to' });
    expect(secondSummary.cards[0]).toMatchObject({ id: 'thinking-1', text: 'Need to check' });
  });

  it('merges streamed activity events without dropping older transcript activity', () => {
    const merged = mergeChatActivityEvents(
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
      chat_kind: 'pma',
      name: 'New chat',
      scope_urn: 'hub'
    });
    expect(buildManagedThreadCreatePayload('codex', repo)).toEqual({
      agent: 'codex',
      chat_kind: 'pma',
      name: 'New chat',
      scope_urn: 'repo:repo-1'
    });
    expect(buildManagedThreadCreatePayload('codex', worktree)).toEqual({
      agent: 'codex',
      chat_kind: 'pma',
      name: 'New chat',
      scope_urn: 'worktree:repo-1/worktree-1'
    });
    expect(buildManagedThreadCreatePayload('opencode', local, 'New chat', 'zai/glm')).toEqual({
      agent: 'opencode',
      chat_kind: 'pma',
      model: 'zai/glm',
      name: 'New chat',
      scope_urn: 'hub'
    });
    expect(buildManagedThreadCreatePayload('hermes', local, 'New chat', '', 'planning')).toEqual({
      agent: 'hermes',
      chat_kind: 'pma',
      name: 'New chat',
      profile: 'planning',
      scope_urn: 'hub'
    });
    expect(buildManagedThreadCreatePayload('codex', repo, 'New coding agent chat', '', '', 'coding_agent')).toEqual({
      agent: 'codex',
      chat_kind: 'coding_agent',
      name: 'New coding agent chat',
      scope_urn: 'repo:repo-1'
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
      chat_kind: 'pma',
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
      client_turn_id: undefined,
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
      client_turn_id: undefined,
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
      client_turn_id: undefined,
      busy_policy: undefined,
      defer_execution: true,
      wait_for_confirmation: false
    });
    expect(buildManagedThreadMessagePayload('Continue', '', false, [], '', '', null, 'client-1')).toMatchObject({
      client_turn_id: 'client-1'
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
    expect(pmaChatKind({ ...baseChat, chatKind: 'coding_agent', raw: { name: 'New chat' } })).toBe('coding_agent');
    expect(pmaChatKind({ ...baseChat, raw: { name: 'New coding agent chat' } })).toBe('coding_agent');
    expect(pmaChatKind({ ...baseChat, raw: { chat_kind: 'pma', name: 'New coding agent chat' } })).toBe('pma');
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
