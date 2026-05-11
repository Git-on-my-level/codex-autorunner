import { describe, expect, it, vi } from 'vitest';
import { PmaApiClient, dataOr, normalizeApiError, partialPageIssue } from './client';

describe('API client error handling', () => {
  it('normalizes HTTP JSON errors into displayable errors', async () => {
    const fetcher = vi.fn(async () =>
      new Response(JSON.stringify({ detail: 'Missing repo' }), {
        status: 404,
        statusText: 'Not Found',
        headers: { 'content-type': 'application/json' }
      })
    ) as unknown as typeof fetch;
    const client = new PmaApiClient(fetcher);

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
    const client = new PmaApiClient(fetcher);

    const result = await client.getJson('/hub/pma/threads');

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.message).toBe('Server returned an HTML error page for request 404.');
    }
  });

  it('truncates long text error responses before display', async () => {
    const fetcher = vi.fn(async () => new Response('x'.repeat(260), { status: 500 })) as unknown as typeof fetch;
    const client = new PmaApiClient(fetcher);

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
    const client = new PmaApiClient(fetcher);

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
    const client = new PmaApiClient(fetcher);

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
      if (url.endsWith('/resume') || url.endsWith('/compact') || url.endsWith('/archive')) {
        return Response.json({ thread: { thread_target_id: 'thread-1', display_name: 'PMA room' } });
      }
      return Response.json({ status: 'ok' });
    }) as unknown as typeof fetch;
    const client = new PmaApiClient(fetcher);

    await client.pma.interruptThread('thread-1');
    await client.pma.resumeThread('thread-1');
    await client.pma.compactThread('thread-1', 'summary');
    await client.pma.archiveThread('thread-1');
    await client.pma.clearQueue('thread-1');

    expect(fetcher).toHaveBeenCalledWith('/hub/pma/threads/thread-1/interrupt', expect.objectContaining({ method: 'POST' }));
    expect(fetcher).toHaveBeenCalledWith(
      '/hub/pma/threads/thread-1/compact',
      expect.objectContaining({ body: JSON.stringify({ summary: 'summary', reset_backend: true }) })
    );
    expect(fetcher).toHaveBeenCalledWith('/hub/pma/threads/thread-1/queue/clear', expect.objectContaining({ method: 'POST' }));
  });

  it('prefixes API requests with the runtime hub base path when configured', async () => {
    const fetcher = vi.fn(async () =>
      Response.json({
        threads: []
      })
    ) as unknown as typeof fetch;
    const client = new PmaApiClient(fetcher, '/car');

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
    const client = new PmaApiClient(fetcher);

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
    const client = new PmaApiClient(fetcher);

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
    const client = new PmaApiClient(fetcher);

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
    const client = new PmaApiClient(fetcher);

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
    const client = new PmaApiClient(fetcher);

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
    const client = new PmaApiClient(fetcher, '/car');

    const result = await client.getJson('/car/hub/messages');

    expect(fetcher).toHaveBeenCalledWith('/car/hub/messages', expect.any(Object));
    expect(result.ok).toBe(true);
  });

  it('maps PMA canonical timeline payloads with stable item IDs', async () => {
    const fetcher = vi.fn(async () =>
      Response.json({
        contract_version: 'managed_thread_timeline.v1',
        items: [
          {
            item_id: 'turn:turn-1:user',
            kind: 'user_message',
            order_key: '001',
            managed_thread_id: 'thread-1',
            managed_turn_id: 'turn-1',
            status: 'queued',
            payload: { text: 'first queued message' }
          }
        ]
      })
    ) as unknown as typeof fetch;
    const client = new PmaApiClient(fetcher);

    const result = await client.pma.getTimeline('thread-1');

    expect(fetcher).toHaveBeenCalledWith('/hub/pma/threads/thread-1/timeline', expect.any(Object));
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

  it('uploads PMA inbox files with multipart form data', async () => {
    const fetcher = vi.fn(async () => Response.json({ status: 'ok', saved: ['screen.png'] })) as unknown as typeof fetch;
    const client = new PmaApiClient(fetcher);

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
    const client = new PmaApiClient(fetcher);

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
    const client = new PmaApiClient(fetcher);

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
    const client = new PmaApiClient(fetcher);

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
    const client = new PmaApiClient(fetcher);

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
