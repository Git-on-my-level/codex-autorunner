import { describe, expect, it } from 'vitest';
import { buildPmaMemoryViewModel } from './pmaMemory';

describe('PMA memory view model', () => {
  it('keeps PMA memory focused on hub working-set docs', () => {
    const vm = buildPmaMemoryViewModel([
      { id: 'context_log.md', name: 'context_log.md', kind: 'context_log.md', content: 'old notes', updatedAt: null, isPinned: true, raw: {} },
      { id: 'ABOUT_CAR.md', name: 'ABOUT_CAR.md', kind: 'ABOUT_CAR.md', content: '# Ops', updatedAt: null, isPinned: true, raw: {} },
      { id: 'prompt.md', name: 'prompt.md', kind: 'prompt.md', content: '# Prompt', updatedAt: null, isPinned: true, raw: {} },
      { id: 'AGENTS.md', name: 'AGENTS.md', kind: 'AGENTS.md', content: '# Guidance', updatedAt: null, isPinned: true, raw: {} },
      { id: 'active_context.md', name: 'active_context.md', kind: 'active_context.md', content: '', updatedAt: null, isPinned: true, raw: {} }
    ]);

    expect(vm.title).toBe('PMA memory');
    expect(vm.docs.map((doc) => doc.filename)).toEqual(['AGENTS.md', 'active_context.md', 'context_log.md']);
    expect(vm.docs[0]).toMatchObject({ filename: 'AGENTS.md', isMissing: false });
    expect(vm.docs[1]).toMatchObject({ filename: 'active_context.md', isMissing: true });
    expect(vm.docs[2]).toMatchObject({ filename: 'context_log.md', isMissing: false });
    expect(vm.presentCount).toBe(2);
  });
});
