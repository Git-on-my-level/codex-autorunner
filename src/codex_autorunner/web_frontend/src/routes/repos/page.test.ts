import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import Page from './+page.svelte';

describe('/repos index page', () => {
  it('starts in a loading state before client workspace inventory resolves', () => {
    const { body } = render(Page, { props: { data: { status: 'cold', tags: [] } } });

    expect(body).toContain('skeleton-page');
    expect(body).toContain('aria-busy="true"');
  });
});
