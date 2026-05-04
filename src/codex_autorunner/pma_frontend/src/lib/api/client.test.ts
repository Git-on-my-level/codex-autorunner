import { describe, expect, it, vi } from 'vitest';
import { PmaApiClient, normalizeApiError } from './client';

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

    expect(fetcher).toHaveBeenCalledWith('/hub/pma/threads', expect.any(Object));
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.data[0]).toMatchObject({
        id: 'thread-1',
        title: 'PMA room',
        status: 'running'
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
});
