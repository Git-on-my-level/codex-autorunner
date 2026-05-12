import { describe, expect, it } from 'vitest';
import { createReadModelSnapshotClient } from './readModelClients';

describe('read model snapshot client', () => {
  it('loads chat index snapshots from the live chat projection route', async () => {
    const calls: string[] = [];
    const client = createReadModelSnapshotClient({
      getJson: async (path: string) => {
        calls.push(path);
        return {
          ok: true,
          data: {
            cursor: 42,
            window: { limit: 25, offset: 0, returned: 1, total_count: 1, has_more: false },
            rows: [
              {
                managed_thread_id: 'chat-1',
                title: 'Hermes chat',
                lifecycle_status: 'active',
                runtime_status: 'running',
                agent: 'hermes',
                agent_profile: 'm4-pma',
                model: 'gpt-5.5'
              }
            ]
          }
        };
      },
      readModels: {}
    } as never);

    const result = await client.chatIndex({ filter: 'active', limit: 25 });

    expect(calls).toEqual(['/hub/chat/index?view=active&limit=25']);
    expect(result.ok && result.data.rows[0]).toMatchObject({
      chatId: 'chat-1',
      agent: 'hermes',
      agentProfile: 'm4-pma',
      status: 'running'
    });
  });

  it('loads chat detail snapshots from the live chat detail route', async () => {
    const calls: string[] = [];
    const client = createReadModelSnapshotClient({
      getJson: async (path: string) => {
        calls.push(path);
        return {
          ok: true,
          data: {
            cursor: 44,
            thread: {
              managed_thread_id: 'chat-1',
              title: 'Hermes chat',
              lifecycle_status: 'active',
              runtime_status: 'running',
              agent: 'hermes',
              agent_profile: 'm4-pma',
              model: 'gpt-5.5'
            },
            timeline: {
              item_count: 2,
              window: { limit: 50, returned: 2, has_older: false },
              items: [
                {
                  item_id: 'item-1',
                  kind: 'user_message',
                  role: 'user',
                  timestamp: '2026-05-11T12:00:00Z',
                  text: 'hello',
                  identity: {
                    timeline_item_id: 'item-1',
                    progress_item_ids: [],
                    correlation_id: null
                  },
                  provenance: {
                    source_event_ids: ['evt-1'],
                    progress_event_ids: [],
                    cursor_event_id: null
                  }
                },
                {
                  item_id: 'item-2',
                  kind: 'assistant_message',
                  role: 'assistant',
                  timestamp: '2026-05-11T12:01:00Z',
                  text: 'working on it'
                }
              ]
            },
            queue_summary: { depth: 0, items: [] }
          }
        };
      },
      readModels: {}
    } as never);

    const result = await client.chatDetail('chat-1');

    expect(calls).toEqual(['/hub/chat/threads/chat-1/detail?timeline_limit=50']);
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
    expect(items[1].identity).toBeUndefined();
    expect(items[1].provenance).toBeUndefined();
  });
});
