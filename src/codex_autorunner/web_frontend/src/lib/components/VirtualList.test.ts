import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import VirtualListHarness from './VirtualListHarness.test.svelte';

describe('VirtualList', () => {
  function virtualListSource(): string {
    return readFileSync(
      fileURLToPath(new URL('./VirtualList.svelte', import.meta.url)),
      'utf8'
    );
  }

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

  it('server-renders every item for non-scrollable nested lists', () => {
    const { body } = render(VirtualListHarness, { props: { count: 80, initialCount: 12, scrollable: false } });

    expect(body).toContain('class="virtual-list non-scrollable');
    expect(body.match(/class="seeded-row"/g)).toHaveLength(80);
    expect(body).toContain('80: Row 80');
  });

  it('preserves bottom on row and viewport resize only when the caller opts in and the list was already at bottom', () => {
    const source = virtualListSource();

    expect(source).toContain('preserveBottomOnResize = false');
    expect(source).toContain('let lastAtBottom = true;');
    expect(source).toContain('const wasAtBottom = lastAtBottom || isNearBottom();');
    expect(source).toContain('function handleViewportResize()');
    expect(source).toContain('const observer = new ResizeObserver(handleViewportResize);');
    expect(source).toContain('void preserveBottomIfNeeded(wasAtBottom);');
    expect(source).toContain('if (!preserveBottomOnResize || !wasAtBottom || !viewport || !scrollable) return;');
  });
});
