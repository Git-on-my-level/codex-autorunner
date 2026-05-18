import { describe, expect, it, vi } from 'vitest';
import { WebApiClient, dataOr, normalizeApiError, partialPageIssue } from './client';

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
        threads: [{ thread_target_id: 'thread-1', display_name: 'PMA room', status: 'running' }]
      })
    ) as unknown as typeof fetch;
    const client = new WebApiClient(fetcher);

    const result = await client.pma.listChats();

    expect(fetcher).toHaveBeenCalledWith('/hub/pma/threads?status=active', expect.any(Object));
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.data[0]).toMatchObject({
        id: 'thread-1',
        title: 'PMA room',
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
            executor_kind: 'pma_turn',
            target_policy: 'hub',
            target: { repo_id: 'repo-1' },
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
        executorKind: 'pma_turn',
        target: { repo_id: 'repo-1' },
        schedule: { scheduleKind: 'daily', nextFireAt: '2026-01-01T09:00:00Z' },
        lastJob: { jobId: 'job-1', state: 'succeeded' }
      });
    }
  });

  it('maps PMA chat list status from backend execution state before lifecycle state', async () => {
    const fetcher = vi.fn(async () =>
      Response.json({
        threads: [
          {
            thread_target_id: 'thread-1',
            display_name: 'PMA room',
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
      if (url.endsWith('/archive-active')) {
        return Response.json({
          threads: [{ thread_target_id: 'thread-1', display_name: 'PMA room' }],
          archived_count: 1,
          requested_count: 1,
          error_count: 0,
          errors: []
        });
      }
      if (url.endsWith('/resume') || url.endsWith('/compact') || url.endsWith('/archive')) {
        return Response.json({ thread: { thread_target_id: 'thread-1', display_name: 'PMA room' } });
      }
      return Response.json({ status: 'ok' });
    }) as unknown as typeof fetch;
    const client = new WebApiClient(fetcher);

    await client.pma.interruptThread('thread-1');
    await client.pma.resumeThread('thread-1');
    await client.pma.compactThread('thread-1', 'summary');
    await client.pma.archiveThread('thread-1');
    const archiveAll = await client.pma.archiveActiveThreads();
    await client.pma.clearQueue('thread-1');

    expect(archiveAll.ok && archiveAll.data.archivedCount).toBe(1);
    expect(fetcher).toHaveBeenCalledWith('/hub/pma/threads/thread-1/interrupt', expect.objectContaining({ method: 'POST' }));
    expect(fetcher).toHaveBeenCalledWith('/hub/pma/threads/archive-active', expect.objectContaining({ method: 'POST' }));
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
        contract_version: 'managed_thread_timeline.v2',
        items: [
          {
            contract_version: 'managed_thread_timeline.v2',
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
        contract_version: 'managed_thread_timeline.v2',
        items: [
          {
            contract_version: 'managed_thread_timeline.v2',
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
            contract_version: 'managed_thread_timeline.v2',
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
            contract_version: 'managed_thread_timeline.v2',
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
        contract_version: 'managed_thread_timeline.v2',
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
        contract_version: 'managed_thread_transcript.v1',
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
});
