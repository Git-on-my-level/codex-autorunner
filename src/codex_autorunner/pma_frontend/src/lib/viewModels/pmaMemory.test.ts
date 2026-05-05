import { describe, expect, it } from 'vitest';
import { buildPmaMemoryViewModel } from './pmaMemory';

describe('PMA memory view model', () => {
  it('orders PMA docs by their prompt relevance', () => {
    const vm = buildPmaMemoryViewModel([
      { id: 'context_log.md', name: 'context_log.md', kind: 'context_log.md', content: 'old notes', updatedAt: null, isPinned: true, raw: {} },
      { id: 'AGENTS.md', name: 'AGENTS.md', kind: 'AGENTS.md', content: '# Guidance', updatedAt: null, isPinned: true, raw: {} },
      { id: 'active_context.md', name: 'active_context.md', kind: 'active_context.md', content: '', updatedAt: null, isPinned: true, raw: {} }
    ]);

    expect(vm.title).toBe('PMA memory');
    expect(vm.docs.map((doc) => doc.filename)).toEqual(['AGENTS.md', 'active_context.md', 'context_log.md']);
    expect(vm.docs[0]).toMatchObject({ label: 'Durable guidance', isMissing: false });
    expect(vm.docs[1]).toMatchObject({ label: 'Active context', isMissing: true });
    expect(vm.presentCount).toBe(2);
  });
});
