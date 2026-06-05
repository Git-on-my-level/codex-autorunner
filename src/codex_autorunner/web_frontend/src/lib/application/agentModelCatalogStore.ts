import { writable, type Readable } from 'svelte/store';
import { webApi, type ApiError, type ApiResult, type JsonRecord, type WebApiClient } from '$lib/api/client';
import { agentCanListModels, agentId } from '$lib/viewModels/modelPickers';

export type AgentModelCatalogApi = Pick<WebApiClient['pma'], 'listAgents' | 'listAgentModels'>;

export type AgentModelCatalogStatus = 'idle' | 'loading' | 'ready' | 'error';

export type AgentModelCatalogAgentState = {
  status: AgentModelCatalogStatus;
  error: ApiError | null;
};

export type AgentModelCatalogState = {
  status: AgentModelCatalogStatus;
  agents: JsonRecord[];
  agentStatuses: JsonRecord[];
  defaultAgent: string;
  defaults: JsonRecord;
  setupPrompt: string;
  error: ApiError | null;
  modelCatalogs: Record<string, JsonRecord[] | null>;
  modelStates: Record<string, AgentModelCatalogAgentState>;
};

export class AgentModelCatalogStore implements Readable<AgentModelCatalogState> {
  private readonly store = writable<AgentModelCatalogState>(initialAgentModelCatalogState());
  private state = initialAgentModelCatalogState();
  private requestSeq = 0;
  private loadPromise: Promise<AgentModelCatalogState> | null = null;
  private hasLoadedAgents = false;

  constructor(private readonly api: AgentModelCatalogApi) {}

  subscribe = this.store.subscribe;

  snapshot(): AgentModelCatalogState {
    return this.state;
  }

  ensureLoaded(): Promise<AgentModelCatalogState> {
    return this.load({ force: false });
  }

  refresh(): Promise<AgentModelCatalogState> {
    return this.load({ force: true });
  }

  async load(options: { force?: boolean } = {}): Promise<AgentModelCatalogState> {
    if (!options.force && this.hasLoadedAgents) return this.state;
    if (!options.force && this.loadPromise) return this.loadPromise;
    const requestSeq = ++this.requestSeq;
    this.commit({ ...this.state, status: 'loading', error: null });
    const promise = this.loadForRequest(requestSeq);
    this.loadPromise = promise;
    try {
      return await promise;
    } finally {
      if (this.loadPromise === promise) this.loadPromise = null;
    }
  }

  reset(): void {
    this.requestSeq += 1;
    this.loadPromise = null;
    this.hasLoadedAgents = false;
    this.commit(initialAgentModelCatalogState());
  }

  private async loadForRequest(requestSeq: number): Promise<AgentModelCatalogState> {
    const agentsResult = await this.api.listAgents();
    if (!this.isCurrent(requestSeq)) return this.state;
    if (!agentsResult.ok) {
      this.commit({ ...this.state, status: 'error', error: agentsResult.error });
      return this.state;
    }

    this.hasLoadedAgents = true;
    const agents = agentsResult.data.agents;
    const modelCapableAgents = agents.filter((agent) => agentCanListModels(agent));
    const modelCatalogs = pruneModelCatalogs(this.state.modelCatalogs, modelCapableAgents);
    const modelStates = buildInitialModelStates(modelCapableAgents);
    this.commit({
      status: 'ready',
      agents,
      agentStatuses: agentsResult.data.agentStatuses,
      defaultAgent: agentsResult.data.default,
      defaults: agentsResult.data.defaults,
      setupPrompt: agentsResult.data.setupPrompt,
      error: null,
      modelCatalogs,
      modelStates
    });

    await Promise.all(
      modelCapableAgents.map(async (agent) => {
        const id = agentId(agent);
        const result = await this.api.listAgentModels(id);
        if (!this.isCurrent(requestSeq)) return;
        this.commitModelCatalog(id, result);
      })
    );
    return this.state;
  }

  private commitModelCatalog(id: string, result: ApiResult<JsonRecord[]>): void {
    this.commit({
      ...this.state,
      modelCatalogs: {
        ...this.state.modelCatalogs,
        [id]: result.ok ? result.data : null
      },
      modelStates: {
        ...this.state.modelStates,
        [id]: {
          status: result.ok ? 'ready' : 'error',
          error: result.ok ? null : result.error
        }
      }
    });
  }

  private isCurrent(requestSeq: number): boolean {
    return requestSeq === this.requestSeq;
  }

  private commit(state: AgentModelCatalogState): void {
    this.state = state;
    this.store.set(state);
  }
}

export function createAgentModelCatalogStore(api: AgentModelCatalogApi): AgentModelCatalogStore {
  return new AgentModelCatalogStore(api);
}

export const agentModelCatalogStore = createAgentModelCatalogStore(webApi.pma);

function initialAgentModelCatalogState(): AgentModelCatalogState {
  return {
    status: 'idle',
    agents: [],
    agentStatuses: [],
    defaultAgent: '',
    defaults: {},
    setupPrompt: '',
    error: null,
    modelCatalogs: {},
    modelStates: {}
  };
}

function buildInitialModelStates(agents: JsonRecord[]): Record<string, AgentModelCatalogAgentState> {
  return Object.fromEntries(
    agents.map((agent) => {
      const id = agentId(agent);
      return [id, { status: 'loading', error: null }];
    })
  );
}

function pruneModelCatalogs(modelCatalogs: Record<string, JsonRecord[] | null>, agents: JsonRecord[]): Record<string, JsonRecord[] | null> {
  const ids = new Set(agents.map((agent) => agentId(agent)));
  return Object.fromEntries(Object.entries(modelCatalogs).filter(([id]) => ids.has(id)));
}
