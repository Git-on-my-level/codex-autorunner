import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import FilterRow, { type FilterChip } from './FilterRow.svelte';

describe('FilterRow', () => {
  it('renders direct filter buttons without native details disclosure chrome', () => {
    const items: FilterChip[] = [
      { key: 'all', label: 'All', active: true, onSelect: () => undefined },
      { key: 'active', label: 'Active', onSelect: () => undefined }
    ];

    const { body } = render(FilterRow, {
      props: { items, ariaLabel: 'Chat filters', maxRows: 0 }
    });

    expect(body).toContain('data-filter-chip');
    expect(body).toContain('All');
    expect(body).toContain('Active');
    expect(body).not.toContain('<details');
    expect(body).not.toContain('<summary');
  });
});
