import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import Page from './+page.svelte';

describe('/chats page', () => {
  it('renders memory toggle, filters, chat list shell, and composer affordances', () => {
    const { body } = render(Page);

    expect(body).toContain('Chats workspace');
    expect(body).toContain('Memory');
    expect(body).toContain('memory-toggle-button');
    expect(body).toContain('+ PMA chat');
    expect(body).toContain('chat-list');
    expect(body).toContain('Waiting');
    expect(body).toContain('Active');
    expect(body).toContain('Done');
    expect(body).toContain('Search chats');
    expect(body).toContain('Create or select a chat');
    expect(body).toContain('Attach files');
    expect(body).toContain('Agent-native approvals apply during turns');
  });
});
