import { describe, expect, it, vi } from 'vitest';
import type { ApiError, ApiResult, JsonRecord } from '$lib/api/client';
import { createAgentModelCatalogStore, type AgentModelCatalogApi } from './agentModelCatalogStore';

describe('agent model catalog store', () => {
  it('loads agents, model catalogs for capable agents, and preserves partial model failures', async () => {
    const api = createApi({
      agents: [
        listCapableAgent('codex'),
        { id: 'pma', capability_projection: { actions: { list_models: { allowed: false } } } },
        listCapableAgent('hermes')
      ],
      modelsByAgent: {
        codex: ok([{ id: 'gpt-5.5' }]),
        hermes: fail('Hermes catalog unavailable.')
      }
    });
    const store = createAgentModelCatalogStore(api);

    await store.ensureLoaded();

    expect(api.listAgents).toHaveBeenCalledTimes(1);
    expect(api.listAgentModels).toHaveBeenCalledTimes(2);
    expect(api.listAgentModels).toHaveBeenCalledWith('codex');
    expect(api.listAgentModels).toHaveBeenCalledWith('hermes');
    expect(store.snapshot().agents.map((agent) => agent.id)).toEqual(['codex', 'pma', 'hermes']);
    expect(store.snapshot().modelCatalogs.codex).toEqual([{ id: 'gpt-5.5' }]);
    expect(store.snapshot().modelCatalogs.hermes).toBeNull();
    expect(store.snapshot().modelStates.hermes).toMatchObject({
      status: 'error',
      error: expect.objectContaining({ message: 'Hermes catalog unavailable.' })
    });
  });

  it('reuses cached agents and refreshes on demand', async () => {
    const api = createApi({ agents: [listCapableAgent('codex')] });
    const store = createAgentModelCatalogStore(api);

    await store.ensureLoaded();
    await store.ensureLoaded();
    expect(api.listAgents).toHaveBeenCalledTimes(1);

    await store.refresh();
    expect(api.listAgents).toHaveBeenCalledTimes(2);
  });

  it('ignores stale agent and model responses after a newer refresh starts', async () => {
    const firstAgents = deferred<ApiResult<{ agents: JsonRecord[]; default: string; defaults: JsonRecord }>>();
    const api = createApi({ agents: [listCapableAgent('fresh')] });
    api.listAgents
      .mockReturnValueOnce(firstAgents.promise)
      .mockResolvedValueOnce(ok({ agents: [listCapableAgent('fresh')], default: 'fresh', defaults: {} }));
    api.listAgentModels.mockResolvedValue(ok([{ id: 'fresh-model' }]));
    const store = createAgentModelCatalogStore(api);

    const staleLoad = store.ensureLoaded();
    const freshLoad = store.refresh();
    await freshLoad;
    firstAgents.resolve(ok({ agents: [listCapableAgent('stale')], default: 'stale', defaults: {} }));
    await staleLoad;

    expect(store.snapshot().agents.map((agent) => agent.id)).toEqual(['fresh']);
    expect(store.snapshot().modelCatalogs.fresh).toEqual([{ id: 'fresh-model' }]);
    expect(store.snapshot().modelCatalogs.stale).toBeUndefined();
  });
});

function createApi(options: {
  agents: JsonRecord[];
  modelsByAgent?: Record<string, ApiResult<JsonRecord[]>>;
}): AgentModelCatalogApi & {
  listAgents: ReturnType<typeof vi.fn>;
  listAgentModels: ReturnType<typeof vi.fn>;
} {
  return {
    listAgents: vi.fn().mockResolvedValue(ok({ agents: options.agents, default: 'codex', defaults: {} })),
    listAgentModels: vi.fn((agentId: string) =>
      Promise.resolve(options.modelsByAgent?.[agentId] ?? ok([{ id: `${agentId}-model` }]))
    )
  };
}

function listCapableAgent(id: string): JsonRecord {
  return { id, capability_projection: { actions: { list_models: { allowed: true } } } };
}

function ok<T>(data: T): ApiResult<T> {
  return { ok: true, data };
}

function fail(message: string): ApiResult<never> {
  return {
    ok: false,
    error: {
      kind: 'http',
      status: 503,
      code: 'catalog_error',
      message
    } satisfies ApiError
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}
