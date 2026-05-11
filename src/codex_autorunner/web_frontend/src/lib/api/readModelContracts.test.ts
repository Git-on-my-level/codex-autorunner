import { describe, expect, it } from 'vitest';
import {
  READ_MODEL_CONTRACT_VERSION,
  mapReadModelContract,
  readModelRepairRequired,
  type ChatDetailPatchEvent,
  type ChatDetailSnapshot,
  type ChatIndexPatchEvent,
  type ChatIndexSnapshot,
  type PageWindow,
  type ProjectionCursor,
  type RepairPolicy,
  type RepoWorktreePatchEvent,
  type RepoWorktreeRuntimeSnapshot,
  type RepoWorktreeTopologySnapshot,
  type TicketDetailPatchEvent,
  type TicketDetailSnapshot
} from './readModelContracts';

const now = '2026-05-11T12:00:00Z';

function cursor(sequence = 1): ProjectionCursor {
  return {
    value: `projection:ui:${sequence}`,
    sequence,
    source: 'ui_projection_journal',
    issuedAt: now
  };
}

function window(): PageWindow {
  return {
    limit: 50,
    nextCursor: 'projection:ui:next',
    totalEstimate: 1,
    totalIsExact: false
  };
}

function repair(snapshotRoute: string): RepairPolicy {
  return {
    snapshotRoute,
    cursorQueryParam: 'after',
    gapEventType: 'projection.cursor_gap',
    behavior: 'repair_snapshot_required'
  };
}

const chatRow = {
  chatId: 'chat-1',
  surface: 'pma' as const,
  title: 'Ticket chat',
  status: 'running' as const,
  unreadCount: 2,
  lastActivityAt: now,
  repoId: 'repo-1',
  worktreeId: 'wt-1',
  ticketId: 'TICKET-001',
  runId: 'run-1',
  agent: 'codex',
  model: 'gpt-5.3-codex',
  groupId: 'ticket-run:run-1'
};

function envelope(eventType: string, entityKind: string, entityId: string, operation = 'patch') {
  return {
    contractVersion: READ_MODEL_CONTRACT_VERSION,
    eventType,
    cursor: cursor(2),
    entityKind,
    entityId,
    operation,
    generatedAt: now
  };
}

describe('read model contracts', () => {
  it('maps chat index snapshot and patch payloads', () => {
    const snapshot = mapReadModelContract<ChatIndexSnapshot>({
      contractVersion: READ_MODEL_CONTRACT_VERSION,
      kind: 'chat.index.snapshot',
      cursor: cursor(),
      window: window(),
      filter: 'active',
      rows: [chatRow],
      groups: [],
      counters: { total: 1, waiting: 0, running: 1, unread: 2, archived: 0 },
      repair: repair('/hub/read-models/chats')
    });
    const event = mapReadModelContract<ChatIndexPatchEvent>({
      envelope: envelope('chat.index.patch', 'chat', 'chat-1'),
      patch: {
        rows: [chatRow],
        groups: [],
        removedRowIds: [],
        removedGroupIds: [],
        counters: snapshot.counters
      }
    });

    expect(snapshot.rows[0].chatId).toBe('chat-1');
    expect(event.patch.rows[0].unreadCount).toBe(2);
  });

  it('maps chat detail snapshot and append events', () => {
    const timelineItem = {
      itemId: 'timeline-1',
      kind: 'assistant_message' as const,
      role: 'assistant' as const,
      createdAt: now,
      text: 'Working on it.',
      artifactIds: [],
      backendMessageId: 'turn-1'
    };
    const thread = {
      chatId: 'chat-1',
      surface: 'pma',
      title: 'Ticket chat',
      status: 'running' as const,
      repoId: 'repo-1',
      worktreeId: 'wt-1',
      ticketId: 'TICKET-001',
      runId: 'run-1',
      archived: false
    };
    const snapshot = mapReadModelContract<ChatDetailSnapshot>({
      contractVersion: READ_MODEL_CONTRACT_VERSION,
      kind: 'chat.detail.snapshot',
      cursor: cursor(),
      thread,
      timelineWindow: window(),
      timeline: [timelineItem],
      queue: { depth: 1, activeTurnId: 'turn-1', queuedTurnIds: [] },
      artifacts: [],
      repair: repair('/hub/read-models/chats/chat-1')
    });
    const event = mapReadModelContract<ChatDetailPatchEvent>({
      envelope: envelope('chat.detail.patch', 'chat', 'chat-1', 'upsert'),
      patch: {
        appendedTimeline: [timelineItem],
        patchedTimeline: [],
        removedTimelineIds: [],
        queue: snapshot.queue,
        artifacts: []
      }
    });

    expect(snapshot.timelineWindow.limit).toBe(50);
    expect(event.patch.appendedTimeline[0].backendMessageId).toBe('turn-1');
  });

  it('keeps repo/worktree topology and runtime as separate contracts', () => {
    const topology = mapReadModelContract<RepoWorktreeTopologySnapshot>({
      contractVersion: READ_MODEL_CONTRACT_VERSION,
      kind: 'repo_worktree.topology.snapshot',
      cursor: cursor(),
      window: window(),
      repos: [{ repoId: 'repo-1', label: 'Repo', path: '/work/repo', archived: false, childWorktreeIds: ['wt-1'] }],
      worktrees: [{ worktreeId: 'wt-1', repoId: 'repo-1', label: 'Feature', path: '/work/repo-wt', branch: 'feature/read-models', archived: false }],
      repair: repair('/hub/read-models/repo-worktree/topology')
    });
    const runtime = mapReadModelContract<RepoWorktreeRuntimeSnapshot>({
      contractVersion: READ_MODEL_CONTRACT_VERSION,
      kind: 'repo_worktree.runtime.snapshot',
      cursor: cursor(),
      window: window(),
      runtime: [{ entityKind: 'worktree', entityId: 'wt-1', gitDirty: true, activeRunId: 'run-1', activeRunStatus: 'running', waitingTicketCount: 1, runningTicketCount: 0, chatCount: 1, cleanupBlockers: [], updatedAt: now }],
      repair: repair('/hub/read-models/repo-worktree/runtime')
    });
    const event = mapReadModelContract<RepoWorktreePatchEvent>({
      envelope: envelope('worktree.runtime.patch', 'worktree', 'wt-1'),
      patch: {
        topologyRepos: [],
        topologyWorktrees: [],
        runtime: runtime.runtime,
        removedRepoIds: [],
        removedWorktreeIds: []
      }
    });

    expect(topology.repos[0].childWorktreeIds).toEqual(['wt-1']);
    expect(event.patch.runtime[0].activeRunStatus).toBe('running');
  });

  it('maps ticket detail snapshots and patch events with scoped links', () => {
    const ticket = {
      ticketId: 'tkt_1',
      routeId: 'TICKET-001',
      title: 'Define contracts',
      status: 'running' as const,
      ownerKind: 'worktree' as const,
      ownerId: 'wt-1',
      agent: 'codex',
      done: false
    };
    const sibling = {
      ticketId: 'tkt_2',
      routeId: 'TICKET-002',
      title: 'Implement projection',
      status: 'queued',
      previousTicketId: 'tkt_1'
    };
    const snapshot = mapReadModelContract<TicketDetailSnapshot>({
      contractVersion: READ_MODEL_CONTRACT_VERSION,
      kind: 'ticket.detail.snapshot',
      cursor: cursor(),
      ticket,
      siblings: [sibling],
      linkedChats: [chatRow],
      artifacts: [],
      dispatchWindow: window(),
      dispatches: [{ seq: 1, mode: 'notify' }],
      repair: repair('/hub/read-models/tickets/tkt_1')
    });
    const event = mapReadModelContract<TicketDetailPatchEvent>({
      envelope: envelope('ticket.detail.patch', 'ticket', 'tkt_1'),
      patch: {
        ticket,
        siblings: [sibling],
        linkedChats: [chatRow],
        artifacts: [],
        dispatches: []
      }
    });

    expect(snapshot.ticket.ownerKind).toBe('worktree');
    expect(event.patch.linkedChats[0].ticketId).toBe('TICKET-001');
  });

  it('flags reset events as repair-required', () => {
    const event = mapReadModelContract<ChatIndexPatchEvent>({
      envelope: envelope('chat.index.patch', 'chat', 'all', 'reset'),
      patch: { rows: [], groups: [], removedRowIds: [], removedGroupIds: [] }
    });

    expect(readModelRepairRequired(event)).toBe(true);
  });
});
