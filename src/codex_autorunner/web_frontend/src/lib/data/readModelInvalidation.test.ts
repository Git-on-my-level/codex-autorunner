import { describe, expect, it, vi } from 'vitest';
import type { ApiResult, JsonRecord } from '$lib/api/client';
import {
  invalidateReadModelTags,
  invalidateReadModelTagsOnSuccess,
  mutateAndInvalidate
} from './readModelInvalidation';

describe('read model invalidation', () => {
  it('invalidates each unique entity tag after a successful mutation', async () => {
    const invalidator = vi.fn().mockResolvedValue(undefined);
    const result: ApiResult<JsonRecord> = { ok: true, data: { status: 'ok' } };

    await invalidateReadModelTagsOnSuccess(result, ['entity:chat:chat-1', 'entity:chat:index', 'entity:chat:chat-1'], invalidator);

    expect(invalidator).toHaveBeenCalledTimes(2);
    expect(invalidator).toHaveBeenCalledWith('entity:chat:chat-1');
    expect(invalidator).toHaveBeenCalledWith('entity:chat:index');
  });

  it('does not invalidate tags after a failed mutation', async () => {
    const invalidator = vi.fn().mockResolvedValue(undefined);
    const result: ApiResult<JsonRecord> = {
      ok: false,
      error: { kind: 'http', status: 500, code: 'failed', message: 'Mutation failed.' }
    };

    await invalidateReadModelTagsOnSuccess(result, ['entity:repo:repo-1'], invalidator);

    expect(invalidator).not.toHaveBeenCalled();
  });

  it('returns the original failed result from mutateAndInvalidate without invalidating unrelated data', async () => {
    const invalidator = vi.fn().mockResolvedValue(undefined);
    const failure: ApiResult<JsonRecord> = {
      ok: false,
      error: { kind: 'network', status: null, code: 'network_error', message: 'Offline.' }
    };

    const result = await mutateAndInvalidate(['entity:worktree:wt-1'], async () => failure, invalidator);

    expect(result).toBe(failure);
    expect(invalidator).not.toHaveBeenCalled();
  });

  it('can invalidate directly for successful non-ApiResult flows', async () => {
    const invalidator = vi.fn().mockResolvedValue(undefined);

    await invalidateReadModelTags(['entity:ticket:index', 'entity:ticket:ticket-1'], invalidator);

    expect(invalidator.mock.calls.map(([tag]) => tag)).toEqual(['entity:ticket:index', 'entity:ticket:ticket-1']);
  });
});
