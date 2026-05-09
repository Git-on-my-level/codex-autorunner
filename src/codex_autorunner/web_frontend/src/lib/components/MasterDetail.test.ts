import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import MasterDetailHarness from './MasterDetailHarness.test.svelte';

describe('MasterDetail', () => {
  it.each([
    ['desktop', 'detail'],
    ['tablet', 'list'],
    ['mobile', 'detail']
  ] as const)('renders stable %s layout regions for %s mode', (viewport, mode) => {
    const { body } = render(MasterDetailHarness, { props: { viewport, mode } });

    expect(body).toContain('class="master-detail');
    expect(body).toContain(`data-viewport="${viewport}"`);
    expect(body).toContain(`data-mode="${mode}"`);
    expect(body).toContain('master-detail-list');
    expect(body).toContain('master-detail-main');
    expect(body).toContain('master-detail-rail');
    expect(body).toContain('Queue list region');
    expect(body).toContain('Conversation detail region');
    expect(body).toContain('Scope rail region');
  });
});
