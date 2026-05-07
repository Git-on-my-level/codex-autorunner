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

    expect(vm.title).toBe('Workspace memory: codex-autorunner');
    expect(vm.eyebrow).toBe('Repo-scoped contextspace');
    expect(vm.description).toContain('Repo memory');
    expect(vm.openWorkspaceHref).toBe('/repos/repo-1');
    expect(vm.askPmaHref).toContain('/chats?draft=');
    expect(vm.docs.map((doc) => doc.filename)).toEqual(['active_context.md', 'spec.md', 'decisions.md']);
    expect(vm.docs[0]).toMatchObject({ id: 'active_context', isMissing: true });
    expect(vm.presentCount).toBe(2);
  });

  it('uses worktree links when the workspace id is a worktree', () => {
    const vm = buildContextspaceViewModel('worktree-1', [], [mockRepoSummary], [mockWorktreeSummary]);

    expect(vm.workspaceKind).toBe('worktree');
    expect(vm.description).toContain('Worktree memory');
    expect(vm.openWorkspaceHref).toBe('/worktrees/worktree-1');
    expect(vm.openWorkspaceLabel).toBe('Open worktree variant');
  });

  it('treats local contextspace as unsupported in the PMA hub', () => {
    const vm = buildContextspaceViewModel('local', [], [mockRepoSummary], [mockWorktreeSummary]);

    expect(vm.workspaceKind).toBe('unknown');
    expect(vm.title).toBe('Workspace memory: local');
    expect(vm.eyebrow).toBe('Unknown workspace contextspace');
    expect(vm.description).toContain('scoped contextspace was not loaded');
    expect(vm.openWorkspaceHref).toBe('/repos');
    expect(vm.openWorkspaceLabel).toBe('Open workspace index');
  });

  it('marks unknown workspace ids without pretending they are unscoped contextspace', () => {
    const vm = buildContextspaceViewModel('missing-workspace', [], [mockRepoSummary], [mockWorktreeSummary]);

    expect(vm.workspaceKind).toBe('unknown');
    expect(vm.isUnknown).toBe(true);
    expect(vm.eyebrow).toBe('Unknown workspace contextspace');
    expect(vm.description).toContain('scoped contextspace was not loaded');
    expect(vm.openWorkspaceHref).toBe('/repos');
    expect(vm.openWorkspaceLabel).toBe('Open workspace index');
    expect(vm.presentCount).toBe(0);
  });

  it('renders readable safe markdown html', () => {
    const html = renderMarkdownToHtml('# Title\n\n- **Decision**\n\n`code` <script>');

    expect(html).toContain('<h1>Title</h1>');
    expect(html).toContain('<li><strong>Decision</strong></li>');
    expect(html).toContain('<code>code</code>');
    expect(html).toContain('&lt;script&gt;');
  });

  it('renders safe markdown links without enabling script URLs', () => {
    const html = renderMarkdownToHtml('[Ticket](/tmp/TICKET-001.md) [bad](javascript:alert(1))');

    expect(html).toContain('<a href="/tmp/TICKET-001.md">Ticket</a>');
    expect(html).not.toContain('javascript:alert');
    expect(html).toContain('bad');
  });
});
