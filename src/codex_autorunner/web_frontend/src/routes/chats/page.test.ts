import { render } from 'svelte/server';
import { afterEach, describe, expect, it } from 'vitest';
import type { ChatIndexRow } from '$lib/api/readModelContracts';
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
