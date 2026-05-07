import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import ContextspaceView from './ContextspaceView.svelte';
import { mockRepoSummary, mockWorktreeSummary } from '$lib/viewModels/mockData';
import { buildContextspaceViewModel } from '$lib/viewModels/contextspace';

describe('ContextspaceView', () => {
  it('renders present contextspace docs as markdown with actions', () => {
    const vm = buildContextspaceViewModel(
      'repo-1',
      [
        {
          id: 'active_context',
          kind: 'active_context',
          name: 'active_context.md',
          content: '# Active Context\n\n- Current run state',
          updatedAt: null,
          isPinned: true,
          raw: {}
        },
        {
          id: 'spec',
          kind: 'spec',
          name: 'spec.md',
          content: '# Spec',
          updatedAt: null,
          isPinned: true,
          raw: {}
        }
      ],
      [mockRepoSummary],
      []
    );
    const { body } = render(ContextspaceView, { props: { state: 'ready', vm } });

    expect(body).toContain('Workspace memory: codex-autorunner');
    expect(body).toContain('Repo memory is read from this repo workspace contextspace.');
    expect(body).toContain('active_context.md');
    expect(body).toContain('<h1>Active Context</h1>');
    expect(body).toContain('markdown-edit-target');
    expect(body).toContain('Copy');
    expect(body).toContain('Open repo');
    expect(body).toContain('Ask PMA to update');
  });

  it('renders useful empty states for missing docs', () => {
    const vm = buildContextspaceViewModel('repo-1', [], [mockRepoSummary], []);
    const { body } = render(ContextspaceView, { props: { state: 'ready', vm } });

    expect(body).toContain('0 of 3 standard docs have content');
    expect(body).toContain('active_context.md · missing');
    expect(body).toContain('Active context has no content');
    expect(body).toContain('Ask PMA to refresh this repo memory');
    expect(body).not.toContain('textarea');
  });

  it('renders worktree-scoped contextspace ownership labels', () => {
    const vm = buildContextspaceViewModel('worktree-1', [], [mockRepoSummary], [mockWorktreeSummary]);
    const { body } = render(ContextspaceView, { props: { state: 'ready', vm } });

    expect(body).toContain('Workspace memory: discord-5');
    expect(body).toContain('Worktree memory is read from this worktree workspace contextspace.');
    expect(body).toContain('Open worktree variant');
    expect(body).toContain('Ask PMA to refresh this worktree memory');
  });

  it('renders unknown workspace contextspace without dead workspace links', () => {
    const vm = buildContextspaceViewModel('missing-workspace', [], [mockRepoSummary], [mockWorktreeSummary]);
    const { body } = render(ContextspaceView, { props: { state: 'ready', vm } });

    expect(body).toContain('scoped contextspace was not loaded');
    expect(body).toContain('href="/repos"');
    expect(body).toContain('Open workspace index');
    expect(body).not.toContain('href="/repos/missing-workspace"');
    expect(body).not.toContain('href="/worktrees/missing-workspace"');
  });
});
