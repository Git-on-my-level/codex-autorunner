import { describe, expect, it } from 'vitest';
import {
  READ_MODEL_CONTRACT_VERSION,
  type ChatDetailSnapshot,
  type ChatIndexSnapshot,
  type ProjectionCursor
} from '$lib/api/readModelContracts';
import { createReadModelSnapshotClient } from './readModelClients';

const issuedAt = '2026-05-11T12:00:00.000Z';
const emptyFacetRequest = {
  categories: [],
  turnKinds: [],
  originKinds: [],
  transports: [],
  scopeKinds: [],
  scopeIds: [],
  agentKinds: []
};
const emptyFacetCounts = {
  category: {},
  turnKind: {},
  originKind: {},
  transport: {},
  scopeKind: {},
  agentKind: {}
};

function projCursor(sequence: number, source: string): ProjectionCursor {
  return { value: `${source}:${sequence}`, sequence, source, issuedAt };
}

describe('read model snapshot client', () => {
  it('loads chat index snapshots from the read-models route', async () => {
    const calls: string[] = [];
    const indexPayload: ChatIndexSnapshot = {
      contractVersion: READ_MODEL_CONTRACT_VERSION,
      kind: 'chat.index.snapshot',
      cursor: projCursor(42, 'chat.surface.journal'),
      window: {
        limit: 25,
        nextCursor: null,
        previousCursor: null,
        totalEstimate: 1,
        totalIsExact: true
      },
      filter: 'active',
      query: null,
      facetRequest: emptyFacetRequest,
      rows: [
        {
          chatId: 'chat-1',
          surface: 'pma',
          title: 'Hermes chat',
          status: 'running',
          unreadCount: 0,
          lastActivityAt: issuedAt,
          repoId: null,
          worktreeId: null,
          ticketId: null,
          runId: null,
          agent: 'hermes',
          agentProfile: 'm4-pma',
          chatKind: null,
          model: 'gpt-5.5',
          groupId: null
        }
      ],
      groups: [],
      counters: {
        total: 1,
        waiting: 0,
        running: 1,
        unread: 0,
        archived: 0
      },
      facetCounts: emptyFacetCounts,
      repair: {
        snapshotRoute: '/hub/read-models/chats',
        cursorQueryParam: 'after',
        gapEventType: 'projection.cursor_gap',
        behavior: 'repair_snapshot_required'
      }
    };

    const client = createReadModelSnapshotClient({
      getJson: async (path: string) => {
        calls.push(path);
        return {
          ok: true,
          data: indexPayload as unknown as Record<string, unknown>
        };
      },
      readModels: {}
    } as never);

    const result = await client.chatIndex({
      filter: 'active',
      query: 'build',
      surfaceKind: 'discord',
      facets: {
        categories: ['automation'],
        transports: ['discord'],
        scopeKinds: ['worktree'],
        scopeIds: ['wt-1'],
        agentKinds: ['coding_agent']
      },
      limit: 25
    });

    expect(calls).toEqual([
      '/hub/read-models/chats?filter=active&limit=25&search=build&surface_kind=discord&category=automation&transport=discord&scope_kind=worktree&scope_id=wt-1&agent_kind=coding_agent'
    ]);
    expect(result.ok && result.data.rows[0]).toMatchObject({
      chatId: 'chat-1',
      agent: 'hermes',
      agentProfile: 'm4-pma',
      status: 'running'
    });
  });

  it('loads chat detail snapshots from the read-models route', async () => {
    const calls: string[] = [];

    const detailPayload: ChatDetailSnapshot = {
      contractVersion: READ_MODEL_CONTRACT_VERSION,
      kind: 'chat.detail.snapshot',
      cursor: projCursor(44, 'chat.surface.journal'),
      thread: {
        chatId: 'chat-1',
        surface: 'pma',
        title: 'Hermes chat',
        status: 'running',
        repoId: null,
        worktreeId: null,
        ticketId: null,
        runId: null,
        agent: 'hermes',
        agentProfile: 'm4-pma',
        chatKind: null,
        model: 'gpt-5.5',
        archived: false
      },
      timelineWindow: {
        limit: 50,
        nextCursor: null,
        previousCursor: null,
        totalEstimate: 2,
        totalIsExact: true
      },
      timeline: [
        {
          itemId: 'item-1',
          kind: 'user_message',
          role: 'user',
          createdAt: issuedAt,
          text: 'hello',
          artifactIds: [],
          clientMessageId: null,
          backendMessageId: null,
          identity: {
            timelineItemId: 'item-1',
            progressItemIds: [],
            correlationId: null
          },
          provenance: {
            sourceEventIds: ['evt-1'],
            progressEventIds: [],
            cursorEventId: null
          }
        },
        {
          itemId: 'item-2',
          kind: 'assistant_message',
          role: 'assistant',
          createdAt: '2026-05-11T12:01:00.000Z',
          text: 'working on it',
          artifactIds: [],
          clientMessageId: null,
          backendMessageId: null,
          identity: {
            timelineItemId: 'item-2',
            progressItemIds: [],
            correlationId: null
          },
          provenance: {
            sourceEventIds: ['evt-2'],
            progressEventIds: ['evt-2'],
            cursorEventId: null
          }
        }
      ],
      queue: { depth: 0, activeTurnId: null, queuedTurnIds: [] },
      artifacts: [],
      repair: {
        snapshotRoute: '/hub/read-models/chats/chat-1',
        cursorQueryParam: 'after',
        gapEventType: 'projection.cursor_gap',
        behavior: 'repair_snapshot_required'
      }
    };

    const client = createReadModelSnapshotClient({
      getJson: async (path: string) => {
        calls.push(path);
        return {
          ok: true,
          data: detailPayload as unknown as Record<string, unknown>
        };
      },
      readModels: {}
    } as never);

    const result = await client.chatDetail('chat-1');

    expect(calls).toEqual(['/hub/read-models/chats/chat-1?timeline_limit=50']);
    expect(result.ok && result.data.thread).toMatchObject({
      chatId: 'chat-1',
      agent: 'hermes',
      agentProfile: 'm4-pma',
      status: 'running'
    });
    const items = result.ok ? result.data.timeline : [];
    expect(items[0]).toMatchObject({ itemId: 'item-1', kind: 'user_message' });
    expect(items[0].identity).toEqual({
      timelineItemId: 'item-1',
      progressItemIds: [],
      correlationId: null
    });
    expect(items[0].provenance).toEqual({
      sourceEventIds: ['evt-1'],
      progressEventIds: [],
      cursorEventId: null
    });
    expect(items[1]).toMatchObject({ itemId: 'item-2', kind: 'assistant_message' });
    expect(items[1].identity?.timelineItemId).toBe('item-2');
    expect(items[1].provenance?.sourceEventIds).toEqual(['evt-2']);
  });
});
