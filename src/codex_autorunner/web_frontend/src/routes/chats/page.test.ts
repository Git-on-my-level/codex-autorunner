import { render } from 'svelte/server';
import { afterEach, describe, expect, it } from 'vitest';
import {
  type ChatIndexRow,
  type ProjectionCursor
} from '$lib/api/readModelContracts';
import { readModelEntityStore } from '$lib/data/readModelStore';
import Page from './[[chatId]]/+page.svelte';

describe('/chats page', () => {
  afterEach(() => {
    readModelEntityStore.reset();
  });

  it('renders filters, chat list shell, and composer affordances without global memory controls', () => {
    const { body } = render(Page);

    expect(body).toContain('Chats workspace');
    expect(body).not.toContain('memory-toggle-button');
    expect(body).toContain('+ Chat');
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

function projectionCursor(): ProjectionCursor {
  return {
    value: 'test:1',
    sequence: 1,
    source: 'test',
    issuedAt: '2026-05-11T12:00:00Z'
  };
}
