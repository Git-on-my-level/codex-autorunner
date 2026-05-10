import { describe, expect, it } from 'vitest';
import {
  mapContextspaceDocument,
  mapDashboardSummary,
  mapPmaChatSummary,
  mapPmaRunProgress,
  mapRepoSummary,
  mapSurfaceArtifact,
  mapTicketDetail,
  mapTicketSummary,
  mapWorktreeSummary,
  normalizeOptionalWorkStatus,
  normalizeWorkStatus
} from './domain';

describe('view model mappers', () => {
  it('normalizes backend work-status aliases through one canonical mapper', () => {
    const cases: Array<[unknown, ReturnType<typeof normalizeWorkStatus>]> = [
      ['running', 'running'],
      ['active', 'running'],
      ['in_progress', 'running'],
      ['queued', 'waiting'],
      ['pending', 'waiting'],
      ['ok', 'done'],
      ['completed', 'done'],
      ['error', 'failed'],
      ['invalid', 'invalid'],
      ['interrupted', 'done'],
      ['stalled', 'blocked'],
      ['unexpected-status', 'idle'],
      ['', 'idle'],
      [null, 'idle']
    ];

    for (const [raw, expected] of cases) {
      expect(normalizeWorkStatus(raw)).toBe(expected);
    }
  });

  it('keeps nullable status fields explicit without owning a second alias table', () => {
    expect(normalizeOptionalWorkStatus(undefined)).toBeNull();
    expect(normalizeOptionalWorkStatus(null)).toBeNull();
    expect(normalizeOptionalWorkStatus('')).toBeNull();
    expect(normalizeOptionalWorkStatus('active')).toBe('running');
    expect(normalizeOptionalWorkStatus('unknown-status')).toBe('idle');
  });

  it('maps managed thread payloads into chat summaries', () => {
    const vm = mapPmaChatSummary({
      thread_target_id: 'thread-1',
      display_name: 'Repo fix',
      agent_id: 'codex',
      lifecycle_status: 'active',
      normalized_status: 'idle',
      repo_id: 'repo-1',
      latest_execution: { model: 'gpt-5.2', started_at: '2026-05-04T00:00:00Z' }
    });

    expect(vm).toMatchObject({
      id: 'thread-1',
      title: 'Repo fix',
      status: 'idle',
      agentId: 'codex',
      agentProfile: null,
      model: 'gpt-5.2',
      repoId: 'repo-1',
      updatedAt: '2026-05-04T00:00:00Z'
    });
  });

  it('uses chat channel display names instead of surface id fallback titles', () => {
    const vm = mapPmaChatSummary({
      thread_target_id: 'thread-1',
      display_name: 'discord:1488827014600331415',
      chat_bound: true,
      binding_kind: 'discord',
      binding_id: '1488827014600331415',
      chat_display_name: 'CAR Workspace / #hermes',
      normalized_status: 'idle'
    });

    expect(vm.title).toBe('CAR Workspace / #hermes');
  });

  it('keeps explicit user-managed thread titles even when channel display exists', () => {
    const vm = mapPmaChatSummary({
      thread_target_id: 'thread-1',
      display_name: 'Chat-bound thread',
      chat_bound: true,
      binding_kind: 'telegram',
      binding_id: '-1001:root',
      chat_display_name: 'Ops / Deploys',
      normalized_status: 'idle'
    });

    expect(vm.title).toBe('Chat-bound thread');
  });

  it('maps managed thread agent_profile into chat summaries', () => {
    const vm = mapPmaChatSummary({
      managed_thread_id: 'thread-h',
      name: 'Hermes chat',
      agent: 'hermes',
      agent_profile: 'planning',
      normalized_status: 'idle'
    });
    expect(vm.agentProfile).toBe('planning');
  });

  it('falls back to status_changed_at when updated_at is absent from list payloads', () => {
    const vm = mapPmaChatSummary({
      managed_thread_id: 't-stamp',
      name: 'Hello',
      agent: 'codex',
      normalized_status: 'done',
      status_changed_at: '2026-05-08T12:00:00Z'
    });
    expect(vm.updatedAt).toBe('2026-05-08T12:00:00Z');
  });

  it('maps invalid ticket frontmatter to needs-repair status instead of run failure', () => {
    const vm = mapTicketSummary({
      id: 'tkt-invalid',
      path: '.codex-autorunner/tickets/TICKET-001.md',
      errors: ['frontmatter.agent is required'],
      frontmatter: { title: 'Needs repair', done: false }
    });

    expect(vm.status).toBe('invalid');
  });

  it('derives readable ticket-flow chat summaries from managed thread payload fields', () => {
    const vm = mapPmaChatSummary({
      managed_thread_id: 'thread-ticket-flow',
      name: 'ticket-flow:codex',
      agent: 'codex',
      status: 'running',
      repo_id: 'codex-autorunner',
      resource_kind: 'worktree',
      resource_id: 'codex-autorunner--discord-5',
      workspace_root: '/Users/dazheng/car-workspace/codex-autorunner--discord-5',
      status_reason: 'managed_turn_running',
      last_message_preview:
        '<CAR_TICKET_FLOW_PROMPT><CAR_CURRENT_TICKET_FILE>PATH: .codex-autorunner/tickets/TICKET-330-pma-chat-managed-thread-readability.md</CAR_CURRENT_TICKET_FILE></CAR_TICKET_FLOW_PROMPT>',
      updated_at: '2026-05-04T00:00:00Z'
    });

    expect(vm).toMatchObject({
      id: 'thread-ticket-flow',
      title: 'Ticket flow · TICKET-330-pma-chat-managed-thread-readability · worktree codex-autorunner--discord-5',
      ticketId: 'TICKET-330-pma-chat-managed-thread-readability',
      repoId: 'codex-autorunner',
      worktreeId: 'codex-autorunner--discord-5',
      status: 'running'
    });
    expect(vm.title).not.toBe('ticket-flow:codex');
    expect(vm.isTicketFlow).toBe(true);
  });

  it('flags ticket-flow chats from raw name even when no ticket id surfaces', () => {
    const vm = mapPmaChatSummary({
      managed_thread_id: 'thread-no-ticket',
      name: 'ticket-flow:codex',
      agent: 'codex',
      status: 'running',
      resource_kind: 'worktree',
      resource_id: 'organization-autorunner--discord-2'
    });
    expect(vm.ticketId).toBeNull();
    expect(vm.isTicketFlow).toBe(true);
  });

  it('does not flag plain chats as ticket-flow', () => {
    const vm = mapPmaChatSummary({
      managed_thread_id: 'thread-plain',
      name: 'New chat',
      agent: 'codex',
      status: 'idle',
      repo_id: 'codex-autorunner'
    });
    expect(vm.isTicketFlow).toBe(false);
  });

  it('maps PMA tail/status payloads into run progress', () => {
    const vm = mapPmaRunProgress({
      managed_thread_id: 'thread-1',
      managed_turn_id: 'turn-1',
      turn_status: 'running',
      work_status: 'running',
      operator_status: 'running',
      terminal: false,
      stream_should_close: false,
      stream_close_reason: null,
      phase: 'editing',
      queue_depth: 2,
      last_event_id: 7,
      events: [{ event_id: 7, event_type: 'tool_completed', summary: 'Tests passed' }]
    });

    expect(vm.id).toBe('turn-1');
    expect(vm.status).toBe('running');
    expect(vm.streamShouldClose).toBe(false);
    expect(vm.terminal).toBe(false);
    expect(vm.workStatus).toBe('running');
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
    expect(mapSurfaceArtifact({ kind: 'link', title: 'Fixture preview', url: 'https://example.test' }).kind).toBe('link');
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

  it('maps ticket list payloads into rich queue summaries', () => {
    const vm = mapTicketSummary({
      path: '.codex-autorunner/tickets/TICKET-170-ticket-list-and-detail-pages.md',
      index: 170,
      chat_key: 'ticket:tkt_pages',
      frontmatter: {
        ticket_id: 'tkt_pages',
        title: 'Implement ticket pages',
        agent: 'codex',
        done: false
      },
      mtime: 1777852920,
      diff_stats: { insertions: 12, deletions: 3, files_changed: 2 },
      duration_seconds: 45
    });

    expect(vm).toMatchObject({
      id: 'tkt_pages',
      number: 170,
      title: 'Implement ticket pages',
      status: 'idle',
      agentId: 'codex',
      chatKey: 'ticket:tkt_pages',
      durationSeconds: 45,
      diffStats: { insertions: 12, deletions: 3, filesChanged: 2 }
    });
    expect(vm.updatedAt).toContain('2026');
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
      managed_threads: [{ lifecycle_status: 'active', normalized_status: 'idle' }],
      pma_files_detail: { inbox: [{ name: 'result-report.md', summary: 'Final report' }] },
      repo_count: 3,
      worktree_count: 5
    });

    expect(vm).toMatchObject({
      activeRuns: 0,
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

  it('treats pending repo ticket-flow state as active instead of waiting for user', () => {
    const vm = mapRepoSummary({
      id: 'base',
      name: 'Base repo',
      ticket_flow_display: {
        status: 'pending',
        is_active: true,
        done_count: 1,
        total_count: 3
      }
    });

    expect(vm.status).toBe('running');
    expect(vm.activeRuns).toBe(1);
    expect(vm.openTickets).toBe(2);
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
