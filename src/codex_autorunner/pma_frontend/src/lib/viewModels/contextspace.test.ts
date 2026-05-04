import { describe, expect, it } from 'vitest';
import { mockRepoSummary, mockWorktreeSummary } from './mockData';
import { buildContextspaceViewModel, renderMarkdownToHtml } from './contextspace';

describe('contextspace view models', () => {
  it('builds ordered standard docs with workspace actions', () => {
    const vm = buildContextspaceViewModel(
      'repo-1',
      [
        { id: 'spec', kind: 'spec', name: 'spec.md', content: '# Spec', updatedAt: null, isPinned: true, raw: {} },
        {
          id: 'decisions',
          kind: 'decisions',
          name: 'decisions.md',
          content: '- Keep PMA first',
          updatedAt: null,
          isPinned: true,
          raw: {}
        }
      ],
      [mockRepoSummary],
      [mockWorktreeSummary]
    );

    expect(vm.title).toBe('codex-autorunner contextspace');
    expect(vm.openWorkspaceHref).toBe('/repos/repo-1');
    expect(vm.askPmaHref).toContain('/pma?draft=');
    expect(vm.docs.map((doc) => doc.filename)).toEqual(['active_context.md', 'spec.md', 'decisions.md']);
    expect(vm.docs[0]).toMatchObject({ id: 'active_context', isMissing: true });
    expect(vm.presentCount).toBe(2);
  });

  it('uses worktree links when the workspace id is a worktree', () => {
    const vm = buildContextspaceViewModel('worktree-1', [], [mockRepoSummary], [mockWorktreeSummary]);

    expect(vm.workspaceKind).toBe('worktree');
    expect(vm.openWorkspaceHref).toBe('/worktrees/worktree-1');
    expect(vm.openWorkspaceLabel).toBe('Open worktree');
  });

  it('links unknown or local contextspace back to the workspace index', () => {
    const vm = buildContextspaceViewModel('local', [], [mockRepoSummary], [mockWorktreeSummary]);

    expect(vm.workspaceKind).toBe('workspace');
    expect(vm.openWorkspaceHref).toBe('/repos');
    expect(vm.openWorkspaceLabel).toBe('Open workspaces');
  });

  it('renders readable safe markdown html', () => {
    const html = renderMarkdownToHtml('# Title\n\n- **Decision**\n\n`code` <script>');

    expect(html).toContain('<h1>Title</h1>');
    expect(html).toContain('<li><strong>Decision</strong></li>');
    expect(html).toContain('<code>code</code>');
    expect(html).toContain('&lt;script&gt;');
  });
});
