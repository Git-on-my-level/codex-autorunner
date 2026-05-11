import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import VirtualListHarness from './VirtualListHarness.test.svelte';

describe('VirtualList', () => {
  it('server-renders a bounded initial window for large lists', () => {
    const { body } = render(VirtualListHarness, { props: { count: 5000, initialCount: 40 } });

    expect(body).toContain('Seeded rows, 5000 total');
    expect(body).toContain('Showing 1-40 of 5000 items');
    expect(body.match(/class="seeded-row"/g)).toHaveLength(40);
    expect(body).toContain('1: Row 1');
    expect(body).toContain('40: Row 40');
    expect(body).not.toContain('41: Row 41');
    expect(body).not.toContain('5000: Row 5000');
  });
});
