import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import ContextspaceView from './ContextspaceView.svelte';
import { mockRepoSummary } from '$lib/viewModels/mockData';
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

    expect(body).toContain('codex-autorunner contextspace');
    expect(body).toContain('active_context.md');
    expect(body).toContain('<h1>Active Context</h1>');
    expect(body).toContain('Copy');
    expect(body).toContain('Open repo');
    expect(body).toContain('Ask PMA to update');
  });

  it('renders useful empty states for missing docs', () => {
    const vm = buildContextspaceViewModel('repo-1', [], [mockRepoSummary], []);
    const { body } = render(ContextspaceView, { props: { state: 'ready', vm } });

    expect(body).toContain('0 of 3 standard docs have content');
    expect(body).toContain('active_context.md · missing');
    expect(body).toContain('Active context is empty.');
    expect(body).toContain('Ask PMA to update contextspace');
    expect(body).not.toContain('textarea');
  });
});
