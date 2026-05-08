import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import MemoryView from './MemoryView.svelte';
import { buildMemoryViewModel } from '$lib/viewModels/memory';

describe('MemoryView', () => {
  it('renders hub memory with PMA docs as tabs', () => {
    const vm = buildMemoryViewModel({ kind: 'hub' }, [
      { id: 'AGENTS.md', name: 'AGENTS.md', kind: 'AGENTS.md', content: '# PMA Guidance', updatedAt: null, isPinned: true, raw: {} },
      { id: 'active_context.md', name: 'active_context.md', kind: 'active_context.md', content: '', updatedAt: null, isPinned: true, raw: {} },
      { id: 'context_log.md', name: 'context_log.md', kind: 'context_log.md', content: '## Snapshot\n\nlegacy', updatedAt: null, isPinned: true, raw: {} }
    ]);

    const { body } = render(MemoryView, { props: { state: 'ready', vm } });

    expect(body).toContain('Memory: Hub');
    expect(body).toContain('.codex-autorunner/pma/docs');
    expect(body).toContain('AGENTS.md');
    expect(body).toContain('active_context.md');
    expect(body).toContain('context_log.md');
    expect(body).toContain('<h1>PMA Guidance</h1>');
    expect(body).toContain('Copy');
  });

  it('renders useful empty states for missing docs', () => {
    const vm = buildMemoryViewModel({ kind: 'hub' }, []);
    const { body } = render(MemoryView, { props: { state: 'ready', vm } });

    expect(body).toContain('>0<');
    expect(body).toContain('>of 3<');
    expect(body).toContain('has no content');
    expect(body).toContain('PMA has not written content to this memory document yet.');
  });

  it('renders close button when onClose is provided', () => {
    const vm = buildMemoryViewModel({ kind: 'hub' }, [
      { id: 'AGENTS.md', name: 'AGENTS.md', kind: 'AGENTS.md', content: '# Test', updatedAt: null, isPinned: true, raw: {} }
    ]);
    const { body } = render(MemoryView, { props: { state: 'ready', vm, onClose: () => {} } });

    expect(body).toContain('Close memory panel');
  });

  it('does not render close button without onClose', () => {
    const vm = buildMemoryViewModel({ kind: 'hub' }, [
      { id: 'AGENTS.md', name: 'AGENTS.md', kind: 'AGENTS.md', content: '# Test', updatedAt: null, isPinned: true, raw: {} }
    ]);
    const { body } = render(MemoryView, { props: { state: 'ready', vm } });

    expect(body).not.toContain('Close memory panel');
  });

  it('renders loading state', () => {
    const { body } = render(MemoryView, { props: { state: 'loading' } });
    expect(body).toContain('Loading memory');
  });

  it('renders error state', () => {
    const { body } = render(MemoryView, { props: { state: 'error', errorMessage: 'Network failed' } });
    expect(body).toContain('Could not load memory');
    expect(body).toContain('Network failed');
  });
});
