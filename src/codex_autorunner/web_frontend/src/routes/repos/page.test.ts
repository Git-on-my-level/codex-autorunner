import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import Page from './+page.svelte';

describe('/repos index page', () => {
  it('starts in a loading state before client workspace inventory resolves', () => {
    const { body } = render(Page);

    expect(body).toContain('Loading workspace state');
  });
});
