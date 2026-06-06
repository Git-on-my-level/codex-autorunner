import { describe, expect, it, vi } from 'vitest';
import { WebApiClient, dataOr, normalizeApiError, partialPageIssue } from './client';

function automationFixture(id: string, overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id,
    name: id,
    enabled: true,
    system_owned: false,
    kind: 'security_scan_pr',
    executor_kind: 'managed_thread_turn',
    target_policy: 'hub',
    target: { repo_id: 'repo-1' },
    product_api_version: 1,
    editable: {
      can_enable: true,
      can_rename: true,
      can_edit_schedule: true,
      can_edit_message: true,
      can_edit_ticket_body: false,
      can_run_now: true,
      can_edit_raw: false
    },
    managed: { managed: false, system_owned: false },
    schedule_editor: { kind: 'daily', editable: true, fields: { timezone: 'UTC', hour: 9, minute: 0 }, summary: 'Daily 09:00 UTC' },
    trigger_summary: { kind: 'schedule', label: 'schedule.fire', event_types: [] },
    message_source: 'executor.message',
    message_preview: 'Run automation.',
    action_preview: { kind: 'managed_thread_turn' },
    target_summary: { repo_id: 'repo-1', label: 'hub / repo-1' },
    executor_summary: { kind: 'managed_thread_turn', label: 'managed thread turn' },
    policy_summary: { approval_mode: 'never_require_approval' },
    raw_links: { control_plane_rule: `/hub/api/control-plane/automations/rules/${id}` },
    diagnostics: [],
    ...overrides
  };
}

describe('API client error handling', () => {
  it('normalizes HTTP JSON errors into displayable errors', async () => {
    const fetcher = vi.fn(async () =>
      new Response(JSON.stringify({ detail: 'Missing repo' }), {
        status: 404,
        statusText: 'Not Found',
        headers: { 'content-type': 'application/json' }
      })
    ) as unknown as typeof fetch;
    const client = new WebApiClient(fetcher);

    const result = await client.getJson('/hub/repos/missing');

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error).toMatchObject({
        kind: 'http',
        status: 404,
        code: 'http_404',
        message: 'Missing repo'
      });
    }
  });

  it('replaces HTML error documents with a readable summary', async () => {
    const fetcher = vi.fn(async () =>
      new Response('<!doctype html><html><body><script>dev payload</script></body></html>', {
        status: 404,
        statusText: 'Not Found',
        headers: { 'content-type': 'text/html' }
      })
    ) as unknown as typeof fetch;
    const client = new WebApiClient(fetcher);

    const result = await client.getJson('/hub/pma/threads');

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.message).toBe('Server returned an HTML error page for request 404.');
    }
  });

  it('truncates long text error responses before display', async () => {
    const fetcher = vi.fn(async () => new Response('x'.repeat(260), { status: 500 })) as unknown as typeof fetch;
    const client = new WebApiClient(fetcher);

    const result = await client.getJson('/hub/pma/threads');

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.message).toHaveLength(220);
      expect(result.error.message.endsWith('...')).toBe(true);
    }
  });

  it('normalizes network failures', async () => {
    const error = normalizeApiError(new Error('socket closed'));

    expect(error).toMatchObject({
      kind: 'network',
      status: null,
      code: 'network_error',
      message: 'socket closed'
    });
  });

  it('maps domain client responses through view model mappers', async () => {
    const fetcher = vi.fn(async () =>
      Response.json({
        threads: [{ thread_target_id: 'thread-1', display_name: 'chat', status: 'running' }]
      })
    ) as unknown as typeof fetch;
    const client = new WebApiClient(fetcher);

    const result = await client.pma.listChats();

    expect(fetcher).toHaveBeenCalledWith('/hub/pma/threads?status=active', expect.any(Object));
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.data[0]).toMatchObject({
        id: 'thread-1',
        title: 'chat',
        status: 'running'
      });
    }
  });

  it('maps automation overview responses', async () => {
    const fetcher = vi.fn(async () =>
      Response.json({
        automations: [
          {
            id: 'rule-1',
            name: 'Daily scan',
            enabled: true,
            kind: 'security_scan_pr',
            executor_kind: 'managed_thread_turn',
            target_policy: 'hub',
            target: { repo_id: 'repo-1' },
            product_api_version: 1,
            editable: { can_edit_schedule: true },
            schedule_editor: { kind: 'daily', fields: { hour: 9, minute: 0 } },
            trigger_summary: { kind: 'schedule', label: 'schedule.fire' },
            message_source: 'executor.message',
            message_preview: 'Run a security scan',
            action_preview: { kind: 'managed_thread_turn' },
            target_summary: { repo_id: 'repo-1', label: 'hub / repo-1' },
            executor_summary: { kind: 'managed_thread_turn' },
            policy_summary: { approval_mode: 'never_require_approval' },
            raw_links: { control_plane_rule: '/hub/api/control-plane/automations/rules/rule-1' },
            schedule: {
              schedule_id: 'schedule-1',
              schedule_kind: 'daily',
              timezone: 'UTC',
              next_fire_at: '2026-01-01T09:00:00Z',
              schedule: { hour: 9, minute: 0 },
              state: 'active'
            },
            last_job: { job_id: 'job-1', state: 'succeeded' },
            job_count: 1
          }
        ],
        summary: { total: 1, active: 1, paused: 0, failed_jobs: 0 }
      })
    ) as unknown as typeof fetch;
    const client = new WebApiClient(fetcher);

    const result = await client.hub.listAutomations();

    expect(fetcher).toHaveBeenCalledWith('/hub/automations', expect.any(Object));
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.data.automations[0]).toMatchObject({
        id: 'rule-1',
        kind: 'security_scan_pr',
        executorKind: 'managed_thread_turn',
        target: { repo_id: 'repo-1' },
        product: {
          productApiVersion: 1,
          messageSource: 'executor.message',
          messagePreview: 'Run a security scan',
          scheduleEditor: { kind: 'daily', fields: { hour: 9, minute: 0 } }
        },
        schedule: { scheduleKind: 'daily', nextFireAt: '2026-01-01T09:00:00Z' },
        lastJob: { jobId: 'job-1', state: 'succeeded' }
      });
    }
  });

  it('maps preview services read models and lifecycle requests', async () => {
    const fetcher = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url) === '/hub/read-models/services?scope=repo%3Acar') {
        return Response.json({
          services: [
            {
              service_id: 'svc_managed123',
              name: 'Frontend',
              kind: 'managed_command',
              service_class: 'preview',
              trust_level: 'generated',
              ownership: 'car_managed',
              network_policy: 'loopback_only',
              status: 'healthy',
              created_by: 'pma',
              created_at: '2026-06-05T00:00:00Z',
              updated_at: '2026-06-05T00:01:00Z',
              scope_links: [{ kind: 'repo', id: 'car' }],
              scope: 'repo:car',
              car_url: '/preview/services/svc_managed123/',
              proxy_enabled: true,
              direct_url: 'http://127.0.0.1:39001/',
              host: '127.0.0.1',
              port: 39001,
              owner_pid: 123,
              logs: { path: '.codex-autorunner/services/logs/svc_managed123.log' },
              capabilities: { can_stop: true, can_kill: true, can_view_logs: true },
              desired_state: { kind: 'managed_command' },
              observed_state: { status: 'healthy' }
            }
          ],
          counts: { total: 1, running: 1, attention: 0, managed: 1, static: 0, loopback: 0, preview: 1, application: 0, infrastructure: 0 }
        });
      }
      if (String(url) === '/hub/services/svc_managed123/kill') {
        expect(init?.method).toBe('POST');
        expect(JSON.parse(String(init?.body))).toEqual({
          force: true,
          force_attestation: 'terminate preview'
        });
        return Response.json({
          read_model: {
            service_id: 'svc_managed123',
            name: 'Frontend',
            kind: 'managed_command',
            status: 'stopped',
            car_url: '/preview/services/svc_managed123/'
          }
        });
      }
      return new Response('missing', { status: 404 });
    }) as unknown as typeof fetch;
    const client = new WebApiClient(fetcher);

    const listed = await client.hub.getServicesReadModel('repo:car');
    expect(listed.ok).toBe(true);
    if (listed.ok) {
      expect(listed.data.counts.running).toBe(1);
      expect(listed.data.services[0]).toMatchObject({
        serviceId: 'svc_managed123',
        kind: 'managed_command',
        status: 'healthy',
        serviceClass: 'preview',
        trustLevel: 'generated',
        ownership: 'car_managed',
        scope: 'repo:car',
        carUrl: '/preview/services/svc_managed123/',
        port: 39001,
        ownerPid: 123,
        capabilities: { can_stop: true, can_kill: true, can_view_logs: true },
        desiredState: { kind: 'managed_command' },
        observedState: { status: 'healthy' }
      });
    }

    const killed = await client.hub.serviceAction('svc_managed123', 'kill', {
      force: true,
      forceAttestation: 'terminate preview'
    });
    expect(killed.ok).toBe(true);
    if (killed.ok) expect(killed.data.status).toBe('stopped');
  });

  it('maps typed automation projections for schedule, message, managed, and diagnostic UI states', async () => {
    const fetcher = vi.fn(async () =>
      Response.json({
        automations: [
          automationFixture('pma-timer', {
            system_owned: true,
            kind: 'pma_prompt',
            executor_kind: 'managed_thread_turn',
            editable: { can_edit_schedule: false, can_edit_message: false, can_edit_ticket_body: false, can_run_now: true, can_enable: true },
            managed: { managed: true, system_owned: true, reason: 'System-managed automation: pma_timer' },
            schedule_editor: { kind: 'one_shot', fields: { due_at: '2026-01-02T00:00:00Z' }, summary: 'Once at 2026-01-02T00:00:00Z' },
            message_source: 'executor.message',
            message_preview: 'Automation wake-up received.'
          }),
          automationFixture('github-scm', {
            system_owned: true,
            kind: 'publish_operation',
            executor_kind: 'publish_operation',
            editable: { can_edit_schedule: false, can_edit_message: false, can_edit_ticket_body: false, can_run_now: true, can_enable: true },
            managed: { managed: true, system_owned: true, reason: 'System-managed automation: github_scm_reaction' },
            schedule_editor: { kind: 'event_driven', fields: {}, summary: 'Event driven' },
            message_source: 'executor.actions.message.template',
            message_preview: 'SCM review requires follow-up for PR {{ event.payload.pr_number }}.',
            action_preview: { kind: 'scm_reaction', source: 'automation_event.payload.actions' }
          }),
          automationFixture('daily-user', {
            kind: 'security_scan_pr',
            executor_kind: 'managed_thread_turn',
            editable: { can_edit_schedule: true, can_edit_message: true, can_edit_ticket_body: false, can_run_now: true, can_enable: true },
            managed: { managed: false, system_owned: false },
            schedule_editor: { kind: 'daily', editable: true, fields: { timezone: 'UTC', hour: 9, minute: 0 }, summary: 'Daily 09:00 UTC' },
            message_source: 'executor.message',
            message_preview: 'Run a security scan.'
          }),
          automationFixture('weekly-ticket-flow', {
            kind: 'weekly_ticket_flow',
            executor_kind: 'ticket_flow',
            editable: { can_edit_schedule: true, can_edit_message: false, can_edit_ticket_body: true, can_run_now: true, can_enable: true },
            managed: { managed: false, system_owned: false },
            schedule_editor: { kind: 'weekly', editable: true, fields: { timezone: 'UTC', weekday: 0, hour: 10, minute: 0 }, summary: 'Mon 10:00 UTC' },
            message_source: 'executor.ticket_pack.tickets[0].content',
            message_preview: 'Run the configured weekly maintenance ticket.'
          })
        ],
        summary: { total: 4, active: 3, paused: 1, failed_jobs: 0 }
      })
    ) as unknown as typeof fetch;
    const client = new WebApiClient(fetcher);

    const result = await client.hub.listAutomations();

    expect(result.ok).toBe(true);
    if (result.ok) {
      const byId = Object.fromEntries(result.data.automations.map((automation) => [automation.id, automation]));
      expect(byId['pma-timer'].product).toMatchObject({
        managed: { managed: true },
        scheduleEditor: { kind: 'one_shot', fields: { due_at: '2026-01-02T00:00:00Z' } }
      });
      expect(byId['github-scm'].product).toMatchObject({
        scheduleEditor: { kind: 'event_driven' },
        messageSource: 'executor.actions.message.template',
        messagePreview: expect.stringContaining('SCM review requires follow-up'),
        actionPreview: { kind: 'scm_reaction' }
      });
      expect(byId['daily-user'].product).toMatchObject({
        editable: { canEditSchedule: true, canEditMessage: true },
        scheduleEditor: { kind: 'daily', fields: { hour: 9, minute: 0 } }
      });
      expect(byId['weekly-ticket-flow'].product).toMatchObject({
        editable: { canEditSchedule: true, canEditTicketBody: true },
        scheduleEditor: { kind: 'weekly', fields: { weekday: 0, hour: 10 } }
      });
    }
  });

  it('gets and updates automation detail responses', async () => {
    const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/hub/automations/rule-1') && init?.method === 'PATCH') {
        return Response.json({
          automation: {
            id: 'rule-1',
            name: 'Updated scan',
            enabled: false,
            kind: 'security_scan_pr',
            executor_kind: 'managed_thread_turn',
            target_policy: 'hub',
            target: { repo_id: 'repo-1' },
            executor: { message: 'Updated prompt' }
          }
        });
      }
      return Response.json({
        automation: {
          id: 'rule-1',
          name: 'Daily scan',
          enabled: true,
          kind: 'security_scan_pr',
          executor_kind: 'managed_thread_turn',
          target_policy: 'hub',
          target: { repo_id: 'repo-1' }
        }
      });
    }) as unknown as typeof fetch;
    const client = new WebApiClient(fetcher);

    const detail = await client.hub.getAutomation('rule-1');
    const updated = await client.hub.updateAutomation('rule-1', { prompt: 'Updated prompt' });

    expect(fetcher).toHaveBeenCalledWith('/hub/automations/rule-1', expect.any(Object));
    expect(fetcher).toHaveBeenCalledWith(
      '/hub/automations/rule-1',
      expect.objectContaining({ method: 'PATCH', body: JSON.stringify({ prompt: 'Updated prompt' }) })
    );
    expect(detail.ok).toBe(true);
    expect(updated.ok).toBe(true);
    if (updated.ok) {
      expect(updated.data).toMatchObject({ id: 'rule-1', name: 'Updated scan', raw: { executor: { message: 'Updated prompt' } } });
    }
  });

  it('maps chat list status from backend execution state before lifecycle state', async () => {
    const fetcher = vi.fn(async () =>
      Response.json({
        threads: [
          {
            thread_target_id: 'thread-1',
            display_name: 'chat',
            status: 'completed',
            normalized_status: 'completed',
            execution_status: 'running'
          }
        ]
      })
    ) as unknown as typeof fetch;
    const client = new WebApiClient(fetcher);

    const result = await client.pma.listChats();

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.data[0]).toMatchObject({
        id: 'thread-1',
        status: 'running'
      });
    }
  });

  it('calls managed-thread slash command action endpoints', async () => {
    const fetcher = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/retire-active')) {
        return Response.json({
          threads: [{ thread_target_id: 'thread-1', display_name: 'chat' }],
          retired_count: 1,
          requested_count: 1,
          error_count: 0,
          errors: []
        });
      }
      if (url.endsWith('/resume') || url.endsWith('/compact') || url.endsWith('/retire')) {
        return Response.json({ thread: { thread_target_id: 'thread-1', display_name: 'chat' } });
      }
      return Response.json({ status: 'ok' });
    }) as unknown as typeof fetch;
    const client = new WebApiClient(fetcher);

    await client.pma.interruptThread('thread-1');
    await client.pma.resumeThread('thread-1');
    await client.pma.compactThread('thread-1', 'summary');
    await client.pma.retireThread('thread-1');
    const retireAll = await client.pma.retireActiveThreads();
    await client.pma.clearQueue('thread-1');

    expect(retireAll.ok && retireAll.data.retiredCount).toBe(1);
    expect(fetcher).toHaveBeenCalledWith('/hub/pma/threads/thread-1/interrupt', expect.objectContaining({ method: 'POST' }));
    expect(fetcher).toHaveBeenCalledWith('/hub/pma/threads/retire-active', expect.objectContaining({ method: 'POST' }));
    expect(fetcher).toHaveBeenCalledWith(
      '/hub/pma/threads/thread-1/compact',
      expect.objectContaining({ body: JSON.stringify({ summary: 'summary', reset_backend: true }) })
    );
    expect(fetcher).toHaveBeenCalledWith('/hub/pma/threads/thread-1/queue/clear', expect.objectContaining({ method: 'POST' }));
  });

  it('calls hub state and repo pin endpoints', async () => {
    const fetcher = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === '/hub/state') return Response.json({ title: 'Dispatch Desk' });
      return Response.json({ status: 'ok' });
    }) as unknown as typeof fetch;
    const client = new WebApiClient(fetcher);

    const state = await client.hub.getState();
    await client.hub.updateState({ title: 'Dispatch Desk' });
    await client.hub.setRepoPinned('demo', true);

    expect(state.ok && state.data.title).toBe('Dispatch Desk');
    expect(fetcher).toHaveBeenCalledWith('/hub/state', expect.objectContaining({ method: 'PUT', body: JSON.stringify({ title: 'Dispatch Desk' }) }));
    expect(fetcher).toHaveBeenCalledWith('/hub/repos/demo/pin', expect.objectContaining({ method: 'POST', body: JSON.stringify({ pinned: true }) }));
  });

  it('calls repo and worktree sync endpoints separately', async () => {
    const fetcher = vi.fn(async () => Response.json({ status: 'ok' })) as unknown as typeof fetch;
    const client = new WebApiClient(fetcher);

    await client.hub.syncRepoMain('demo');
    await client.hub.syncWorktree('demo--feature');

    expect(fetcher).toHaveBeenCalledWith('/hub/repos/demo/sync-main', expect.objectContaining({ method: 'POST' }));
    expect(fetcher).toHaveBeenCalledWith('/hub/worktrees/demo--feature/sync', expect.objectContaining({ method: 'POST' }));
  });

  it('prefixes API requests with the runtime hub base path when configured', async () => {
    const fetcher = vi.fn(async () =>
      Response.json({
        threads: []
      })
    ) as unknown as typeof fetch;
    const client = new WebApiClient(fetcher, '/car');

    const result = await client.pma.listChats();

    expect(fetcher).toHaveBeenCalledWith('/car/hub/pma/threads?status=active', expect.any(Object));
    expect(result.ok).toBe(true);
  });

  it('falls back to the mounted repo ticket API when the hub ticket projection is unavailable', async () => {
    const fetcher = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === '/hub/tickets?worktree=wt-1') {
        return new Response(JSON.stringify({ detail: 'Not Found' }), {
          status: 404,
          statusText: 'Not Found',
          headers: { 'content-type': 'application/json' }
        });
      }
      if (url === '/repos/wt-1/api/flows/ticket_flow/tickets') {
        return Response.json({
          tickets: [
            {
              id: 'ticket-1',
              index: 1,
              path: '.codex-autorunner/tickets/TICKET-001.md',
              frontmatter: { title: 'Fallback ticket' },
              status: 'idle',
              errors: []
            }
          ]
        });
      }
      return new Response('unexpected request', { status: 500 });
    }) as unknown as typeof fetch;
    const client = new WebApiClient(fetcher);

    const result = await client.ticketFlow.listTickets({ worktree: 'wt-1' });

    expect(fetcher).toHaveBeenNthCalledWith(1, '/hub/tickets?worktree=wt-1', expect.any(Object));
    expect(fetcher).toHaveBeenNthCalledWith(2, '/repos/wt-1/api/flows/ticket_flow/tickets', expect.any(Object));
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.data[0]).toMatchObject({
        title: 'Fallback ticket',
        workspaceKind: 'worktree',
        workspaceId: 'wt-1',
        worktreeId: 'wt-1'
      });
    }
  });

  it('formats scoped hub ticket query parameters with URLSearchParams encoding', async () => {
    const fetcher = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === '/hub/tickets?repo=repo+with+spaces%2Fand%25symbols') {
        return Response.json({ tickets: [] });
      }
      if (url === '/hub/tickets?worktree=wt+with+spaces%2Fand%25symbols') {
        return Response.json({ tickets: [] });
      }
      return new Response('unexpected request', { status: 500 });
    }) as unknown as typeof fetch;
    const client = new WebApiClient(fetcher);

    await client.ticketFlow.listTickets({ repo: 'repo with spaces/and%symbols' });
    await client.ticketFlow.listTickets({ worktree: 'wt with spaces/and%symbols' });

    expect(fetcher).toHaveBeenNthCalledWith(1, '/hub/tickets?repo=repo+with+spaces%2Fand%25symbols', expect.any(Object));
    expect(fetcher).toHaveBeenNthCalledWith(2, '/hub/tickets?worktree=wt+with+spaces%2Fand%25symbols', expect.any(Object));
  });

  it('aggregates mounted repo and worktree ticket APIs when the global hub ticket projection is unavailable', async () => {
    const fetcher = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === '/hub/tickets') {
        return new Response(JSON.stringify({ detail: 'Not Found' }), {
          status: 404,
          statusText: 'Not Found',
          headers: { 'content-type': 'application/json' }
        });
      }
      if (url === '/hub/read-models/repo-worktree/topology?kind=all&limit=200') {
        return Response.json({
          contractVersion: 'web-read-models.v1',
          kind: 'repo_worktree.topology.snapshot',
          cursor: { value: 'topology:1', sequence: 1, source: 'topology', issuedAt: '2026-05-11T00:00:00Z' },
          window: { limit: 200, totalEstimate: 2, totalIsExact: true },
          repos: [{ repoId: 'repo-1', label: 'Repo 1', path: '/repo-1', archived: false, childWorktreeIds: ['wt-1'] }],
          worktrees: [{ worktreeId: 'wt-1', repoId: 'repo-1', label: 'Worktree 1', path: '/wt-1', archived: false }],
          repair: {
            snapshotRoute: '/hub/read-models/repo-worktree/topology',
            cursorQueryParam: 'after',
            gapEventType: 'projection.cursor_gap',
            behavior: 'repair_snapshot_required'
          }
        });
      }
      if (url === '/repos/repo-1/api/flows/ticket_flow/tickets') {
        return Response.json({ tickets: [{ index: 1, frontmatter: { title: 'Repo ticket' }, status: 'idle', errors: [] }] });
      }
      if (url === '/repos/wt-1/api/flows/ticket_flow/tickets') {
        return Response.json({ tickets: [{ index: 2, frontmatter: { title: 'Worktree ticket' }, status: 'idle', errors: [] }] });
      }
      return new Response('unexpected request', { status: 500 });
    }) as unknown as typeof fetch;
    const client = new WebApiClient(fetcher);

    const result = await client.ticketFlow.listTickets();

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.data.map((ticket) => [ticket.title, ticket.workspaceKind, ticket.workspaceId])).toEqual([
        ['Repo ticket', 'repo', 'repo-1'],
        ['Worktree ticket', 'worktree', 'wt-1']
      ]);
    }
  });

  it('fetches ticket-flow runs through mounted repo and worktree routes when scoped', async () => {
    const fetcher = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === '/repos/repo-1/api/flows/runs?flow_type=ticket_flow') {
        return Response.json([{ run_id: 'repo-run', status: 'running' }]);
      }
      if (url === '/repos/wt-1/api/flows/runs?flow_type=ticket_flow') {
        return Response.json([{ run_id: 'worktree-run', status: 'waiting' }]);
      }
      return new Response('unexpected request', { status: 500 });
    }) as unknown as typeof fetch;
    const client = new WebApiClient(fetcher);

    const repoRuns = await client.ticketFlow.listRuns({ repo: 'repo-1' });
    const worktreeRuns = await client.ticketFlow.listRuns({ worktree: 'wt-1' });

    expect(fetcher).toHaveBeenNthCalledWith(1, '/repos/repo-1/api/flows/runs?flow_type=ticket_flow', expect.any(Object));
    expect(fetcher).toHaveBeenNthCalledWith(2, '/repos/wt-1/api/flows/runs?flow_type=ticket_flow', expect.any(Object));
    expect(repoRuns.ok && repoRuns.data[0].id).toBe('repo-run');
    expect(worktreeRuns.ok && worktreeRuns.data[0].id).toBe('worktree-run');
  });

  it('creates and reorders scoped tickets through mounted workspace APIs', async () => {
    const fetcher = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === '/repos/repo-1/api/flows/ticket_flow/tickets') {
        return Response.json({ index: 3, frontmatter: { title: 'Created' }, body: '## Goal\nShip it.' });
      }
      if (url === '/repos/repo-1/api/flows/ticket_flow/tickets/reorder') {
        return Response.json({ status: 'ok' });
      }
      return new Response('unexpected request', { status: 500 });
    }) as unknown as typeof fetch;
    const client = new WebApiClient(fetcher);

    const created = await client.ticketFlow.createTicket({ title: 'Created', body: '## Goal\nShip it.' }, { repo: 'repo-1' });
    const reordered = await client.ticketFlow.reorderTicket(3, 1, false, { repo: 'repo-1' });

    expect(fetcher).toHaveBeenNthCalledWith(
      1,
      '/repos/repo-1/api/flows/ticket_flow/tickets',
      expect.objectContaining({ method: 'POST' })
    );
    expect(fetcher).toHaveBeenNthCalledWith(
      2,
      '/repos/repo-1/api/flows/ticket_flow/tickets/reorder',
      expect.objectContaining({ method: 'POST' })
    );
    expect(created.ok && created.data.title).toBe('Created');
    expect(reordered.ok).toBe(true);
  });

  it('does not double-prefix already based request paths', async () => {
    const fetcher = vi.fn(async () => Response.json({ ok: true })) as unknown as typeof fetch;
    const client = new WebApiClient(fetcher, '/car');

    const result = await client.getJson('/car/hub/messages');

    expect(fetcher).toHaveBeenCalledWith('/car/hub/messages', expect.any(Object));
    expect(result.ok).toBe(true);
  });

  it('maps PMA canonical diagnostic timeline payloads with stable item IDs', async () => {
    const fetcher = vi.fn(async () =>
      Response.json({
        contract_version: 'managed_thread_timeline.v3',
        items: [
          {
            contract_version: 'managed_thread_timeline.v3',
            item_id: 'turn:turn-1:user',
            kind: 'user_message',
            order_key: '001',
            managed_thread_id: 'thread-1',
            managed_turn_id: 'turn-1',
            status: 'queued',
            identity: {
              timeline_item_id: 'turn:turn-1:user',
              progress_item_ids: [],
              correlation_id: null
            },
            provenance: {
              source_event_ids: [],
              progress_event_ids: [],
              cursor_event_id: null
            },
            payload: { text: 'first queued message' }
          }
        ]
      })
    ) as unknown as typeof fetch;
    const client = new WebApiClient(fetcher);

    const result = await client.pma.diagnostics.getTimeline('thread-1');

    expect(fetcher).toHaveBeenCalledWith('/hub/pma/threads/thread-1/timeline?limit=50', expect.any(Object));
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.data[0]).toMatchObject({
        id: 'turn:turn-1:user',
        kind: 'user_message',
        orderKey: '001',
        payload: { text: 'first queued message' }
      });
    }
  });

  it('maps v2 diagnostic timeline items with canonical identity and provenance', async () => {
    const fetcher = vi.fn(async () =>
      Response.json({
        contract_version: 'managed_thread_timeline.v3',
        items: [
          {
            contract_version: 'managed_thread_timeline.v3',
            item_id: 'turn:one:user',
            kind: 'user_message',
            order_key: '001',
            timestamp: '2026-05-13T00:00:00Z',
            managed_thread_id: 'thread-1',
            managed_turn_id: 'one',
            status: 'completed',
            identity: {
              timeline_item_id: 'turn:one:user',
              progress_item_ids: [],
              correlation_id: 'client-1'
            },
            provenance: {
              source_event_ids: ['evt-1', 'evt-2'],
              progress_event_ids: [],
              cursor_event_id: 'cursor-42'
            },
            payload: { text: 'Fix the deploy script' }
          },
          {
            contract_version: 'managed_thread_timeline.v3',
            item_id: 'turn:one:tool:1:rg',
            kind: 'tool_group',
            order_key: '002',
            timestamp: '2026-05-13T00:01:00Z',
            managed_thread_id: 'thread-1',
            managed_turn_id: 'one',
            status: 'completed',
            identity: {
              timeline_item_id: 'turn:one:tool:1:rg',
              progress_item_ids: ['prog-1'],
              correlation_id: null
            },
            provenance: {
              source_event_ids: ['evt-3', 'evt-4'],
              progress_event_ids: ['evt-3', 'evt-4'],
              cursor_event_id: 'cursor-43'
            },
            payload: {
              tool_name: 'rg',
              result: { status: 'completed', summary: 'Found 3 matches' }
            }
          },
          {
            contract_version: 'managed_thread_timeline.v3',
            item_id: 'turn:one:assistant',
            kind: 'assistant_message',
            order_key: '003',
            timestamp: '2026-05-13T00:02:00Z',
            managed_thread_id: 'thread-1',
            managed_turn_id: 'one',
            status: 'completed',
            identity: {
              timeline_item_id: 'turn:one:assistant',
              progress_item_ids: [],
              correlation_id: null
            },
            provenance: {
              source_event_ids: ['evt-5'],
              progress_event_ids: [],
              cursor_event_id: 'cursor-44'
            },
            payload: { text: 'Deploy script fixed.' }
          }
        ]
      })
    ) as unknown as typeof fetch;
    const client = new WebApiClient(fetcher);

    const result = await client.pma.diagnostics.getTimeline('thread-1');

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.data).toHaveLength(3);
      expect(result.data[0]).toMatchObject({
        id: 'turn:one:user',
        kind: 'user_message',
        orderKey: '001'
      });
      expect(result.data[1]).toMatchObject({
        id: 'turn:one:tool:1:rg',
        kind: 'tool_group',
        orderKey: '002'
      });
      expect(result.data[2]).toMatchObject({
        id: 'turn:one:assistant',
        kind: 'assistant_message',
        orderKey: '003'
      });
    }
  });

  it('requests PMA diagnostic timelines with an explicit bounded limit', async () => {
    const fetcher = vi.fn(async () =>
      Response.json({
        contract_version: 'managed_thread_timeline.v3',
        items: []
      })
    ) as unknown as typeof fetch;
    const client = new WebApiClient(fetcher);

    await client.pma.diagnostics.getTimeline('thread-1', { limit: 25 });

    expect(fetcher).toHaveBeenCalledWith('/hub/pma/threads/thread-1/timeline?limit=25', expect.any(Object));
  });

  it('requests PMA transcript projections with backend-owned rows', async () => {
    const fetcher = vi.fn(async () =>
      Response.json({
        contract_version: 'managed_thread_transcript.v2',
        rows: [
          {
            kind: 'message',
            id: 'turn:turn-1:user',
            turn_id: 'turn-1',
            order_key: '001',
            timestamp: '2026-05-15T01:00:00Z',
            message: {
              id: 'turn:turn-1:user',
              chat_id: 'thread-1',
              role: 'user',
              text: 'hello transcript',
              created_at: '2026-05-15T01:00:00Z',
              status: null,
              artifacts: [],
              raw: {}
            }
          }
        ],
        status: null
      })
    ) as unknown as typeof fetch;
    const client = new WebApiClient(fetcher);

    const result = await client.pma.getTranscript('thread-1', { limit: 25 });

    expect(fetcher).toHaveBeenCalledWith('/hub/pma/threads/thread-1/transcript?limit=25', expect.any(Object));
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.data.rows[0]).toMatchObject({
        kind: 'message',
        id: 'turn:turn-1:user',
        message: { role: 'user', text: 'hello transcript' }
      });
    }
  });

  it('requests PMA tail projections through diagnostics-only client methods', async () => {
    const fetcher = vi.fn(async () =>
      Response.json({
        managed_turn_id: 'turn-1',
        managed_thread_id: 'thread-1',
        status: 'running'
      })
    ) as unknown as typeof fetch;
    const client = new WebApiClient(fetcher);

    const result = await client.pma.diagnostics.getTail('thread-1');

    expect(fetcher).toHaveBeenCalledWith('/hub/pma/threads/thread-1/tail', expect.any(Object));
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.data).toMatchObject({
        id: 'turn-1',
        chatId: 'thread-1',
        status: 'running'
      });
    }
  });

  it('uploads PMA inbox files with multipart form data', async () => {
    const fetcher = vi.fn(async () => Response.json({ status: 'ok', saved: ['screen.png'] })) as unknown as typeof fetch;
    const client = new WebApiClient(fetcher);

    const result = await client.pma.uploadInboxFile(new File(['png'], 'screen.png', { type: 'image/png' }));

    expect(fetcher).toHaveBeenCalledWith(
      '/hub/pma/files/inbox',
      expect.objectContaining({
        method: 'POST',
        body: expect.any(FormData)
      })
    );
    expect(result).toEqual({ ok: true, data: ['screen.png'] });
  });

  it('deletes PMA filebox files and boxes', async () => {
    const fetcher = vi.fn(async () => Response.json({ status: 'ok' })) as unknown as typeof fetch;
    const client = new WebApiClient(fetcher);

    const fileResult = await client.pma.deleteFile('inbox', 'screen shot.png');
    const boxResult = await client.pma.deleteFileBox('outbox');

    expect(fetcher).toHaveBeenNthCalledWith(
      1,
      '/hub/pma/files/inbox/screen%20shot.png',
      expect.objectContaining({ method: 'DELETE' })
    );
    expect(fetcher).toHaveBeenNthCalledWith(
      2,
      '/hub/pma/files/outbox',
      expect.objectContaining({ method: 'DELETE' })
    );
    expect(fileResult.ok).toBe(true);
    expect(boxResult.ok).toBe(true);
  });

  it('manages repo-scoped filebox files through generic helpers', async () => {
    const fetcher = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === '/hub/filebox/repo-1') {
        return Response.json({
          inbox: [{ name: 'screen.png', box: 'inbox', url: '/hub/filebox/repo-1/inbox/screen.png' }],
          outbox: []
        });
      }
      return Response.json({ status: 'ok' });
    }) as unknown as typeof fetch;
    const client = new WebApiClient(fetcher);

    const listed = await client.filebox.listFiles({ kind: 'repo', repoId: 'repo-1' });
    const deleted = await client.filebox.deleteFile({ kind: 'repo', repoId: 'repo-1' }, 'inbox', 'screen.png');
    const cleared = await client.filebox.deleteBox({ kind: 'repo', repoId: 'repo-1' }, 'outbox');

    expect(fetcher).toHaveBeenNthCalledWith(1, '/hub/filebox/repo-1', expect.any(Object));
    expect(fetcher).toHaveBeenNthCalledWith(
      2,
      '/hub/filebox/repo-1/inbox/screen.png',
      expect.objectContaining({ method: 'DELETE' })
    );
    expect(fetcher).toHaveBeenNthCalledWith(
      3,
      '/hub/filebox/repo-1/outbox',
      expect.objectContaining({ method: 'DELETE' })
    );
    expect(listed.ok && listed.data[0]).toMatchObject({ title: 'screen.png', raw: { box: 'inbox' } });
    expect(deleted.ok).toBe(true);
    expect(cleared.ok).toBe(true);
  });

  it('lists repo artifact deliveries through the hub filebox route', async () => {
    const fetcher = vi.fn(async () =>
      Response.json({
        deliveries: [
          {
            delivery_id: 'delivery:abc',
            artifact_id: 'sha256:abc',
            state: 'pending',
            artifact: { filename: 'report.md' },
            download_url: '/hub/filebox/repo-1/artifacts/deliveries/delivery%3Aabc/download'
          }
        ]
      })
    ) as unknown as typeof fetch;
    const client = new WebApiClient(fetcher);

    const result = await client.pma.listArtifactDeliveries('repo-1');

    expect(fetcher).toHaveBeenCalledWith('/hub/filebox/repo-1/artifacts/deliveries', expect.any(Object));
    expect(result).toEqual({
      ok: true,
      data: [
        expect.objectContaining({
          deliveryId: 'delivery:abc',
          filename: 'report.md',
          downloadUrl: '/hub/filebox/repo-1/artifacts/deliveries/delivery%3Aabc/download'
        })
      ]
    });
  });

  it('maps workspace contextspace responses through pinned standard docs', async () => {
    const fetcher = vi.fn(async () =>
      Response.json({
        active_context: '# Active',
        spec: '',
        decisions: '- Decision'
      })
    ) as unknown as typeof fetch;
    const client = new WebApiClient(fetcher);

    const result = await client.contextspace.listDocuments('repo-1');

    expect(fetcher).toHaveBeenCalledWith('/repos/repo-1/api/contextspace', expect.any(Object));
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.data.map((doc) => doc.name)).toEqual(['active_context.md', 'spec.md', 'decisions.md']);
      expect(result.data[0]).toMatchObject({
        id: 'active_context',
        content: '# Active',
        isPinned: true
      });
    }
  });

  it('hydrates PMA docs with their document content', async () => {
    const fetcher = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === '/hub/pma/docs') {
        return Response.json({
          docs: [
            { name: 'AGENTS.md', exists: true, mtime: '2026-05-04T00:00:00Z' },
            { name: 'active_context.md', exists: true },
            { name: 'scratch.md', exists: true }
          ]
        });
      }
      if (url === '/hub/pma/docs/AGENTS.md') {
        return Response.json({ name: 'AGENTS.md', content: '# Guidance' });
      }
      return Response.json({ name: 'active_context.md', content: 'Current PMA work' });
    }) as unknown as typeof fetch;
    const client = new WebApiClient(fetcher);

    const result = await client.pma.listDocsWithContent();

    expect(fetcher).toHaveBeenCalledWith('/hub/pma/docs', expect.any(Object));
    expect(fetcher).toHaveBeenCalledWith('/hub/pma/docs/AGENTS.md', expect.any(Object));
    expect(fetcher).toHaveBeenCalledWith('/hub/pma/docs/active_context.md', expect.any(Object));
    expect(fetcher).not.toHaveBeenCalledWith('/hub/pma/docs/scratch.md', expect.any(Object));
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.data.map((doc) => [doc.name, doc.content])).toEqual([
        ['AGENTS.md', '# Guidance'],
        ['active_context.md', 'Current PMA work']
      ]);
      expect(result.data[0].updatedAt).toBe('2026-05-04T00:00:00Z');
    }
  });

  it('updates PMA docs through the hub docs endpoint', async () => {
    const fetcher = vi.fn(async () => Response.json({ name: 'AGENTS.md', status: 'ok' })) as unknown as typeof fetch;
    const client = new WebApiClient(fetcher);

    const result = await client.pma.updateDoc('AGENTS.md', '# Updated guidance');

    expect(fetcher).toHaveBeenCalledWith(
      '/hub/pma/docs/AGENTS.md',
      expect.objectContaining({
        method: 'PUT',
        body: JSON.stringify({ content: '# Updated guidance' })
      })
    );
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.data).toMatchObject({
        id: 'AGENTS.md',
        name: 'AGENTS.md',
        content: '# Updated guidance'
      });
    }
  });

  it('keeps partial page loads renderable when a secondary API result fails', () => {
    const failedRuns = {
      ok: false,
      error: normalizeApiError(new Error('runs endpoint offline'))
    } as const;
    const fallbackRuns: unknown[] = [];

    expect(dataOr(failedRuns, fallbackRuns)).toBe(fallbackRuns);
    expect(partialPageIssue('active_runs', 'Active runs unavailable', failedRuns.error)).toEqual({
      id: 'active_runs',
      title: 'Active runs unavailable',
      message: 'runs endpoint offline',
      retryLabel: 'Retry'
    });
  });

  it('maps updateDocument responses like listDocuments (filename + pinned)', async () => {
    const fetcher = vi.fn(async () =>
      Response.json({
        active_context: '# Updated',
        spec: '',
        decisions: '- Decision'
      })
    ) as unknown as typeof fetch;
    const client = new WebApiClient(fetcher);

    const result = await client.contextspace.updateDocument('repo-1', 'active_context', '# Updated');

    expect(fetcher).toHaveBeenCalledWith(
      '/repos/repo-1/api/contextspace/active_context',
      expect.objectContaining({
        method: 'PUT',
        body: JSON.stringify({ content: '# Updated' })
      })
    );
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.data.map((doc) => doc.name)).toEqual(['active_context.md', 'decisions.md', 'spec.md']);
      expect(result.data[0]).toMatchObject({
        id: 'active_context',
        content: '# Updated',
        isPinned: true
      });
    }
  });

  it('maps system update targets, status, and start responses', async () => {
    const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === '/system/update/targets') {
        return Response.json({
          targets: [
            {
              value: 'all',
              label: 'all',
              description: 'Web + Telegram + Discord',
              includes_web: true,
              restart_notice: 'The web UI, Telegram, and Discord will restart.'
            }
          ],
          default_target: 'all'
        });
      }
      if (path === '/system/update/status') {
        return Response.json({
          status: 'running',
          message: 'Updating',
          phase: 'pull',
          update_target: 'all'
        });
      }
      if (path === '/system/update' && init?.method === 'POST') {
        return Response.json({
          status: 'warning',
          message: 'Active terminal sessions will be interrupted.',
          target: 'all',
          requires_confirmation: true
        });
      }
      return Response.json({ detail: 'unexpected route' }, { status: 404 });
    }) as unknown as typeof fetch;
    const client = new WebApiClient(fetcher);

    await expect(client.system.getUpdateTargets()).resolves.toMatchObject({
      ok: true,
      data: {
        defaultTarget: 'all',
        targets: [
          {
            value: 'all',
            includesWeb: true,
            restartNotice: 'The web UI, Telegram, and Discord will restart.'
          }
        ]
      }
    });
    await expect(client.system.getUpdateStatus()).resolves.toMatchObject({
      ok: true,
      data: {
        status: 'running',
        phase: 'pull',
        updateTarget: 'all'
      }
    });
    await expect(client.system.startUpdate({ target: 'all' })).resolves.toMatchObject({
      ok: true,
      data: {
        status: 'warning',
        requiresConfirmation: true,
        target: 'all'
      }
    });
    expect(fetcher).toHaveBeenCalledWith(
      '/system/update',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ target: 'all', force: false })
      })
    );
  });
});
