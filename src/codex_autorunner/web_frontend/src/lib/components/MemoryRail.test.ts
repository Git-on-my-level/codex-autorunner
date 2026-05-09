import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import MemoryRail from './MemoryRail.svelte';

describe('MemoryRail', () => {
  it('renders nothing when closed', () => {
    const { body } = render(MemoryRail, {
      props: { open: false, scope: { kind: 'hub' }, onClose: () => {} }
    });
    expect(body).not.toContain('memory-rail-panel');
    expect(body).not.toContain('memory-rail-backdrop');
  });

  it('renders rail panel when open', () => {
    const { body } = render(MemoryRail, {
      props: { open: true, scope: { kind: 'hub' }, onClose: () => {} }
    });
    expect(body).toContain('memory-rail-panel');
    expect(body).toContain('Loading memory');
  });

  it('uses complementary role for accessibility', () => {
    const { body } = render(MemoryRail, {
      props: { open: true, scope: { kind: 'hub' }, onClose: () => {} }
    });
    expect(body).toContain('role="complementary"');
    expect(body).toContain('Memory panel');
  });
});
