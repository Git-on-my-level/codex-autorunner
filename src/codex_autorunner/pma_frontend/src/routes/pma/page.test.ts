import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import Page from './+page.svelte';

describe('/pma page', () => {
  it('renders the chat list and active chat shell for DOM smoke coverage', () => {
    const { body } = render(Page);

    expect(body).toContain('PMA chat workspace');
    expect(body).toContain('New chat');
    expect(body).toContain('Search chats, repos, tickets');
    expect(body).toContain('Message PMA');
    expect(body).toContain('Attach files');
    expect(body).toContain('Attach images');
    expect(body).toContain('Attach link');
    expect(body).toContain('PMA has full permission for normal coding work');
  });
});
