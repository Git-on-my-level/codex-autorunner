import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import type { SensitiveApprovalRequest, SurfaceArtifact } from '$lib/viewModels/domain';
import SensitiveApprovalCard from './SensitiveApprovalCard.svelte';
import SurfaceArtifactCard from './SurfaceArtifactCard.svelte';

const baseArtifact: SurfaceArtifact = {
  id: 'artifact-1',
  kind: 'screenshot',
  title: 'Run screenshot',
  summary: 'Browser preview captured.',
  url: '/hub/pma/files/outbox/screen.png',
  createdAt: '2026-05-04T00:00:00Z',
  raw: { name: 'screen.png' }
};

describe('surfaced artifact cards', () => {
  it('renders image previews and detail drilldowns', () => {
    const { body } = render(SurfaceArtifactCard, { props: { artifact: baseArtifact } });

    expect(body).toContain('Screenshot');
    expect(body).toContain('Open screenshot');
    expect(body).toContain('<details>');
    expect(body).toContain('Screenshot details');
    expect(body).toContain('artifact-image-preview');
  });

  it('renders compact non-image artifact states', () => {
    const kinds: SurfaceArtifact['kind'][] = [
      'preview_url',
      'test_result',
      'command_summary',
      'diff_summary',
      'link',
      'final_report',
      'error',
      'file'
    ];

    for (const kind of kinds) {
      const { body } = render(SurfaceArtifactCard, {
        props: { artifact: { ...baseArtifact, kind, title: kind, summary: `${kind} summary` } }
      });
      expect(body).toContain(`${kind} summary`);
      expect(body).toContain('<details>');
    }
  });
});

describe('sensitive approval prompt', () => {
  it('identifies target action, scope, policy, and decisions', () => {
    const approval: SensitiveApprovalRequest = {
      id: 'approval-1',
      title: 'Delete worktree',
      description: 'Remove a managed worktree from disk.',
      risk: 'high',
      action: 'delete_worktree',
      createdAt: null,
      raw: { target_scope: 'hub.worktree.cleanup:repo--branch' }
    };

    const { body } = render(SensitiveApprovalCard, { props: { approval } });

    expect(body).toContain('Sensitive CAR approval');
    expect(body).toContain('delete_worktree');
    expect(body).toContain('hub.worktree.cleanup:repo--branch');
    expect(body).toContain('normal coding work');
    expect(body).toContain('Decline');
    expect(body).toContain('Approve');
  });
});
