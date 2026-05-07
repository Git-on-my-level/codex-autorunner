import { describe, expect, it } from 'vitest';
import { buildMemoryViewModel } from './memory';
import type { ContextspaceDocument } from './domain';

const docs: ContextspaceDocument[] = [
  { id: 'agents', name: 'AGENTS.md', kind: 'AGENTS.md', content: '# Agents', updatedAt: '2026-05-04T00:00:00Z', isPinned: true, raw: {} },
  { id: 'ctx', name: 'active_context.md', kind: 'active_context.md', content: '## Context', updatedAt: '2026-05-04T00:00:00Z', isPinned: false, raw: {} },
  { id: 'log', name: 'context_log.md', kind: 'context_log.md', content: '', updatedAt: null, isPinned: false, raw: {} }
];

describe('buildMemoryViewModel', () => {
  it('builds view model for repo scope', () => {
    const vm = buildMemoryViewModel({ kind: 'repo', id: 'my-repo' }, docs);
    expect(vm.scope).toEqual({ kind: 'repo', id: 'my-repo' });
    expect(vm.title).toBe('Memory: my-repo');
    expect(vm.eyebrow).toBe('Repo memory');
    expect(vm.workspaceHref).toBe('/repos/my-repo');
    expect(vm.memoryHref).toBe('/contextspace/my-repo');
    expect(vm.docs).toHaveLength(3);
    expect(vm.presentCount).toBe(2);
    expect(vm.docs[0].filename).toBe('AGENTS.md');
    expect(vm.docs[0].isMissing).toBe(false);
    expect(vm.docs[2].isMissing).toBe(true);
  });

  it('builds view model for worktree scope', () => {
    const vm = buildMemoryViewModel(
      { kind: 'worktree', id: 'wt-1', parentRepoId: 'base' },
      docs
    );
    expect(vm.scope).toEqual({ kind: 'worktree', id: 'wt-1', parentRepoId: 'base' });
    expect(vm.title).toBe('Memory: wt-1');
    expect(vm.eyebrow).toBe('Worktree memory');
    expect(vm.workspaceHref).toBe('/worktrees/wt-1');
  });

  it('builds view model for hub scope', () => {
    const vm = buildMemoryViewModel({ kind: 'hub' }, docs);
    expect(vm.scope).toEqual({ kind: 'hub' });
    expect(vm.workspaceHref).toBe('/chats');
    expect(vm.memoryHref).toBeNull();
  });

  it('orders docs by canonical order', () => {
    const reversed: ContextspaceDocument[] = [
      { id: 'log', name: 'context_log.md', kind: 'context_log.md', content: 'log', updatedAt: null, isPinned: false, raw: {} },
      { id: 'agents', name: 'AGENTS.md', kind: 'AGENTS.md', content: 'agents', updatedAt: null, isPinned: false, raw: {} },
      { id: 'ctx', name: 'active_context.md', kind: 'active_context.md', content: 'ctx', updatedAt: null, isPinned: false, raw: {} }
    ];
    const vm = buildMemoryViewModel({ kind: 'repo', id: 'r1' }, reversed);
    expect(vm.docs.map((d) => d.filename)).toEqual(['AGENTS.md', 'active_context.md', 'context_log.md']);
  });

  it('handles empty docs', () => {
    const vm = buildMemoryViewModel({ kind: 'repo', id: 'r1' }, []);
    expect(vm.docs).toHaveLength(0);
    expect(vm.presentCount).toBe(0);
  });

  it('filters to known doc names', () => {
    const withExtra: ContextspaceDocument[] = [
      ...docs,
      { id: 'extra', name: 'extra.md', kind: 'extra', content: 'extra', updatedAt: null, isPinned: false, raw: {} }
    ];
    const vm = buildMemoryViewModel({ kind: 'repo', id: 'r1' }, withExtra);
    expect(vm.docs).toHaveLength(3);
  });

  it('renders markdown to html', () => {
    const vm = buildMemoryViewModel(
      { kind: 'repo', id: 'r1' },
      [{ id: 'agents', name: 'AGENTS.md', kind: 'AGENTS.md', content: '# Agents Guide', updatedAt: null, isPinned: false, raw: {} }]
    );
    expect(vm.docs[0].html).toContain('Agents Guide');
    expect(vm.docs[0].html).toContain('<');
  });
});
