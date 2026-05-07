import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import Page from './+page.svelte';

describe('/chats page', () => {
  it('renders pinned memory, filters, scoped chat list shell, and composer affordances', () => {
    const { body } = render(Page);

    expect(body).toContain('Chats workspace');
    expect(body).toContain('PMA Memory');
    expect(body).toContain('Pinned');
    expect(body).toContain('Chat scope');
    expect(body).toContain('Local hub');
    expect(body).toContain('+ New chat');
    expect(body).toContain('Waiting');
    expect(body).toContain('Active');
    expect(body).toContain('Done');
    expect(body).toContain('Search chats');
    expect(body).toContain('Create or select a chat');
    expect(body).toContain('Attach files');
    expect(body).toContain('Agent-native approvals apply during turns');
  });
});
