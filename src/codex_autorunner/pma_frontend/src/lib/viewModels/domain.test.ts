import { describe, expect, it } from 'vitest';
import {
  mapContextspaceDocument,
  mapDashboardSummary,
  mapPmaChatSummary,
  mapPmaRunProgress,
  mapRepoSummary,
  mapSurfaceArtifact,
  mapTicketDetail,
  mapWorktreeSummary
} from './domain';

describe('view model mappers', () => {
  it('maps managed thread payloads into chat summaries', () => {
    const vm = mapPmaChatSummary({
      thread_target_id: 'thread-1',
      display_name: 'Repo fix',
      agent_id: 'codex',
      lifecycle_status: 'active',
      repo_id: 'repo-1',
      latest_execution: { model: 'gpt-5.2', started_at: '2026-05-04T00:00:00Z' }
    });

    expect(vm).toMatchObject({
      id: 'thread-1',
      title: 'Repo fix',
      status: 'running',
      agentId: 'codex',
      model: 'gpt-5.2',
      repoId: 'repo-1',
      updatedAt: '2026-05-04T00:00:00Z'
    });
  });

  it('maps PMA tail/status payloads into run progress', () => {
    const vm = mapPmaRunProgress({
      managed_thread_id: 'thread-1',
      managed_turn_id: 'turn-1',
      turn_status: 'running',
      phase: 'editing',
      queue_depth: 2,
      last_event_id: 7,
      events: [{ event_id: 7, event_type: 'tool_completed', summary: 'Tests passed' }]
    });

    expect(vm.id).toBe('turn-1');
    expect(vm.status).toBe('running');
    expect(vm.queueDepth).toBe(2);
    expect(vm.events[0]).toMatchObject({
      id: '7',
      kind: 'progress',
      title: 'Tests passed'
    });
  });

  it('maps file and dispatch attachments into artifacts', () => {
    expect(mapSurfaceArtifact({ name: 'screenshot.png', url: '/file' })).toMatchObject({
      kind: 'screenshot',
      title: 'screenshot.png',
      url: '/file'
    });
    expect(mapSurfaceArtifact({ event_type: 'command_completed', summary: 'pnpm test' }).kind).toBe('command_summary');
    expect(mapSurfaceArtifact({ name: 'Preview URL', url: 'http://localhost:4173' }).kind).toBe('preview_url');
    expect(mapSurfaceArtifact({ name: 'pull request', url: 'https://github.com/org/repo/pull/1' }).kind).toBe('link');
  });

  it('maps ticket dispatch history attachments into ticket details', () => {
    const vm = mapTicketDetail({
      ticket_id: 'TICKET-001',
      title: 'Fix bug',
      status: 'paused',
      history: [{ attachments: [{ name: 'report.md', rel_path: 'report.md' }] }]
    });

    expect(vm.status).toBe('waiting');
    expect(vm.artifacts).toHaveLength(1);
    expect(vm.artifacts[0].kind).toBe('final_report');
  });

  it('maps contextspace documents', () => {
    const vm = mapContextspaceDocument({
      kind: 'spec',
      name: 'spec.md',
      content: '# Spec',
      is_pinned: true
    });

    expect(vm).toMatchObject({
      id: 'spec',
      name: 'spec.md',
      content: '# Spec',
      isPinned: true
    });
  });

  it('maps dashboard summary payloads from hub messages sections', () => {
    const vm = mapDashboardSummary({
      items: [{ status: 'paused' }, { status: 'failed' }],
      pma_threads: [{ lifecycle_status: 'active' }],
      pma_files_detail: { inbox: [{ name: 'result-report.md', summary: 'Final report' }] },
      repo_count: 3,
      worktree_count: 5
    });

    expect(vm).toMatchObject({
      activeRuns: 1,
      waitingForUser: 1,
      failedOrBlocked: 1,
      openTickets: 2,
      repos: 3,
      worktrees: 5
    });
    expect(vm.recentArtifacts[0]).toMatchObject({
      kind: 'final_report',
      title: 'result-report.md'
    });
  });

  it('maps repo summaries from enriched hub repo payloads', () => {
    const vm = mapRepoSummary({
      id: 'base',
      name: 'Base repo',
      path: '/work/base',
      current_branch: 'main',
      ticket_flow_display: {
        status: 'running',
        is_active: true,
        done_count: 2,
        total_count: 5
      },
      run_state: { last_event_at: '2026-05-04T01:00:00Z' }
    });

    expect(vm).toMatchObject({
      id: 'base',
      name: 'Base repo',
      status: 'running',
      defaultBranch: 'main',
      activeRuns: 1,
      openTickets: 3,
      lastActivityAt: '2026-05-04T01:00:00Z'
    });
  });

  it('maps worktree summaries from enriched hub repo payloads', () => {
    const vm = mapWorktreeSummary({
      id: 'base--feature',
      kind: 'worktree',
      worktree_of: 'base',
      path: '/work/base--feature',
      branch: 'feature/current-run',
      ticket_flow_display: {
        status: 'paused',
        is_active: true,
        done_count: 1,
        total_count: 2
      }
    });

    expect(vm).toMatchObject({
      id: 'base--feature',
      repoId: 'base',
      branch: 'feature/current-run',
      status: 'waiting',
      activeRuns: 1,
      openTickets: 1
    });
  });
});
