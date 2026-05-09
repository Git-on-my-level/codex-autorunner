import { describe, expect, it } from 'vitest';
import { buildMemoryViewModel } from './memory';
import type { ContextspaceDocument } from './domain';

const pmaDocs: ContextspaceDocument[] = [
  { id: 'agents', name: 'AGENTS.md', kind: 'AGENTS.md', content: '# Agents', updatedAt: '2026-05-04T00:00:00Z', isPinned: true, raw: {} },
  { id: 'ctx', name: 'active_context.md', kind: 'active_context.md', content: '## Context', updatedAt: '2026-05-04T00:00:00Z', isPinned: false, raw: {} },
  { id: 'log', name: 'context_log.md', kind: 'context_log.md', content: '', updatedAt: null, isPinned: false, raw: {} }
];

describe('buildMemoryViewModel', () => {
  describe('hub scope (PMA docs)', () => {
    it('builds view model for hub scope with PMA docs', () => {
      const vm = buildMemoryViewModel({ kind: 'hub' }, pmaDocs);
      expect(vm.scope).toEqual({ kind: 'hub' });
      expect(vm.title).toBe('Memory: Hub');
      expect(vm.eyebrow).toBe('Local hub memory');
      expect(vm.workspaceHref).toBe('/chats');
      expect(vm.docs).toHaveLength(3);
      expect(vm.presentCount).toBe(2);
      expect(vm.docs[0].filename).toBe('AGENTS.md');
      expect(vm.docs[0].isMissing).toBe(false);
      expect(vm.docs[2].isMissing).toBe(true);
    });

    it('orders PMA docs by canonical order', () => {
      const reversed: ContextspaceDocument[] = [
        { id: 'log', name: 'context_log.md', kind: 'context_log.md', content: 'log', updatedAt: null, isPinned: false, raw: {} },
        { id: 'agents', name: 'AGENTS.md', kind: 'AGENTS.md', content: 'agents', updatedAt: null, isPinned: false, raw: {} },
        { id: 'ctx', name: 'active_context.md', kind: 'active_context.md', content: 'ctx', updatedAt: null, isPinned: false, raw: {} }
      ];
      const vm = buildMemoryViewModel({ kind: 'hub' }, reversed);
      expect(vm.docs.map((d) => d.filename)).toEqual(['AGENTS.md', 'active_context.md', 'context_log.md']);
    });

    it('handles empty docs for hub scope', () => {
      const vm = buildMemoryViewModel({ kind: 'hub' }, []);
      expect(vm.docs).toHaveLength(3);
      expect(vm.presentCount).toBe(0);
      expect(vm.docs.every((d) => d.isMissing)).toBe(true);
    });

    it('filters to PMA doc names for hub', () => {
      const withExtra: ContextspaceDocument[] = [
        ...pmaDocs,
        { id: 'extra', name: 'extra.md', kind: 'extra', content: 'extra', updatedAt: null, isPinned: false, raw: {} }
      ];
      const vm = buildMemoryViewModel({ kind: 'hub' }, withExtra);
      expect(vm.docs).toHaveLength(3);
    });
  });

  it('renders markdown to html', () => {
    const vm = buildMemoryViewModel(
      { kind: 'hub' },
      [{ id: 'agents', name: 'AGENTS.md', kind: 'AGENTS.md', content: '# Agents Guide', updatedAt: null, isPinned: false, raw: {} }]
    );
    expect(vm.docs[0].html).toContain('Agents Guide');
    expect(vm.docs[0].html).toContain('<');
  });
});
