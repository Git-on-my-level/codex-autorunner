import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import PmaMemoryView from './PmaMemoryView.svelte';
import { buildPmaMemoryViewModel } from '$lib/viewModels/pmaMemory';

describe('PmaMemoryView', () => {
  it('renders PMA docs as memory without workspace fallback actions', () => {
    const vm = buildPmaMemoryViewModel([
      { id: 'AGENTS.md', name: 'AGENTS.md', kind: 'AGENTS.md', content: '# PMA Guidance', updatedAt: null, isPinned: true, raw: {} },
      { id: 'active_context.md', name: 'active_context.md', kind: 'active_context.md', content: '', updatedAt: null, isPinned: true, raw: {} },
      { id: 'random.md', name: 'random.md', kind: 'random.md', content: '# Extra', updatedAt: null, isPinned: true, raw: {} }
    ]);

    const { body } = render(PmaMemoryView, { props: { state: 'ready', vm } });

    expect(body).toContain('PMA memory');
    expect(body).toContain('.codex-autorunner/pma/docs');
    expect(body).toContain('AGENTS.md');
    expect(body).toContain('<h1>PMA Guidance</h1>');
    expect(body).toContain('markdown-edit-target');
    expect(body).toContain('Copy');
    expect(body).not.toContain('random.md');
    expect(body).not.toContain('Open workspace index');
    expect(body).not.toContain('Ask PMA to update');
  });
});
