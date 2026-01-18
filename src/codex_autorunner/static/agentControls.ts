import { api, flash } from "./utils.js";

interface Agent {
  id: string;
  name?: string;
}

interface ModelCatalogModel {
  id: string;
  display_name?: string;
  supports_reasoning: boolean;
  reasoning_options: string[];
}

interface ModelCatalog {
  default_model: string;
  models: ModelCatalogModel[];
}

interface AgentControlConfig {
  agentSelect?: HTMLSelectElement | null;
  modelSelect?: HTMLSelectElement | null;
  reasoningSelect?: HTMLSelectElement | null;
}

interface AgentControl extends AgentControlConfig {
  agentSelect?: HTMLSelectElement | null;
  modelSelect?: HTMLSelectElement | null;
  reasoningSelect?: HTMLSelectElement | null;
}

const STORAGE_KEYS = {
  selected: "car.agent.selected",
  model: (agent: string) => `car.agent.${agent}.model`,
  reasoning: (agent: string) => `car.agent.${agent}.reasoning`,
} as const;

const FALLBACK_AGENTS: Agent[] = [
  { id: "codex", name: "Codex" },
];

const controls: AgentControl[] = [];
let agentsLoaded = false;
let agentsLoadPromise: Promise<void> | null = null;
let agentList: Agent[] = [...FALLBACK_AGENTS];
let defaultAgent = "codex";
const modelCatalogs = new Map<string, ModelCatalog | null>();
const modelCatalogPromises = new Map<string, Promise<ModelCatalog | null>>();

function safeGetStorage(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch (_err) {
    return null;
  }
}

function safeSetStorage(key: string, value: unknown): void {
  try {
    if (value === null || value === undefined || value === "") {
      localStorage.removeItem(key);
    } else {
      localStorage.setItem(key, String(value));
    }
  } catch (_err) {
    // ignore storage failures
  }
}

export function getSelectedAgent(): string {
  const stored = safeGetStorage(STORAGE_KEYS.selected);
  if (stored && agentList.some((agent) => agent.id === stored)) {
    return stored;
  }
  return defaultAgent;
}

export function getSelectedModel(agent: string = getSelectedAgent()): string {
  return safeGetStorage(STORAGE_KEYS.model(agent)) || "";
}

export function getSelectedReasoning(agent: string = getSelectedAgent()): string {
  return safeGetStorage(STORAGE_KEYS.reasoning(agent)) || "";
}

function setSelectedAgent(agent: string): void {
  safeSetStorage(STORAGE_KEYS.selected, agent);
}

function setSelectedModel(agent: string, model: string): void {
  safeSetStorage(STORAGE_KEYS.model(agent), model);
}

function setSelectedReasoning(agent: string, reasoning: string): void {
  safeSetStorage(STORAGE_KEYS.reasoning(agent), reasoning);
}

function ensureFallbackAgents(): void {
  if (!agentList.length) {
    agentList = [...FALLBACK_AGENTS];
  }
  if (!agentList.some((agent) => agent.id === defaultAgent)) {
    defaultAgent = agentList[0]?.id || "codex";
  }
}

async function loadAgents(): Promise<void> {
  if (agentsLoaded) return;
  if (agentsLoadPromise) {
    await agentsLoadPromise;
    return;
  }
  agentsLoadPromise = (async () => {
    try {
      const data = await api("/api/agents", { method: "GET" });
      const agents = Array.isArray((data as { agents?: unknown[] })?.agents) ? (data as { agents: unknown[] }).agents : [];
      // Only use API response if it contains valid agents
      if (agents.length > 0 && agents.every((a) => a && typeof (a as Agent).id === "string")) {
        agentList = agents as Agent[];
        defaultAgent = (data as { default?: string })?.default || defaultAgent;
      }
    } catch (err) {
      console.warn("Failed to load agent list, using fallback", err);
    } finally {
      ensureFallbackAgents();
      agentsLoaded = true;
      agentsLoadPromise = null;
    }
  })();
  await agentsLoadPromise;
}

function normalizeCatalog(raw: unknown): ModelCatalog {
  if (!raw || typeof raw !== "object") {
    return { default_model: "", models: [] };
  }
  const rawObj = raw as Record<string, unknown>;
  const models = Array.isArray(rawObj?.models) ? rawObj.models : [];
  const normalized = models
    .map((entry): ModelCatalogModel | null => {
      if (!entry || typeof entry !== "object") return null;
      const entryObj = entry as Record<string, unknown>;
      const id = entryObj.id;
      if (!id || typeof id !== "string") return null;
      const displayName =
        typeof entryObj.display_name === "string" && entryObj.display_name
          ? entryObj.display_name
          : id;
      const supportsReasoning = Boolean(entryObj.supports_reasoning);
      const reasoningOptions = Array.isArray(entryObj.reasoning_options)
        ? (entryObj.reasoning_options as unknown[]).filter((value) => typeof value === "string")
        : [];
      return {
        id,
        display_name: displayName,
        supports_reasoning: supportsReasoning,
        reasoning_options: reasoningOptions,
      };
    })
    .filter((model): model is ModelCatalogModel => model !== null);
  const defaultModel =
    typeof rawObj?.default_model === "string" ? rawObj.default_model : "";
  return {
    default_model: defaultModel,
    models: normalized,
  };
}

async function loadModelCatalog(agent: string): Promise<ModelCatalog | null> {
  if (modelCatalogs.has(agent)) return modelCatalogs.get(agent) || null;
  if (modelCatalogPromises.has(agent)) {
    return await modelCatalogPromises.get(agent) || null;
  }
  const promise = api(`/api/agents/${encodeURIComponent(agent)}/models`, {
    method: "GET",
  })
    .then((data) => {
      const catalog = normalizeCatalog(data);
      modelCatalogs.set(agent, catalog);
      return catalog;
    })
    .catch((err) => {
      modelCatalogs.set(agent, null);
      throw err;
    })
    .finally(() => {
      modelCatalogPromises.delete(agent);
    });
  modelCatalogPromises.set(agent, promise);
  return await promise;
}

function getLabelText(agentId: string): string {
  const entry = agentList.find((agent) => agent.id === agentId);
  return entry?.name || agentId;
}

function ensureAgentOptions(select: HTMLSelectElement | null | undefined): void {
  if (!select) return;
  const selected = getSelectedAgent();
  select.innerHTML = "";
  agentList.forEach((agent) => {
    const option = document.createElement("option");
    option.value = agent.id;
    option.textContent = agent.name || agent.id;
    select.appendChild(option);
  });
  select.value = selected;
}

function ensureModelOptions(select: HTMLSelectElement | null | undefined, catalog: ModelCatalog | null): void {
  if (!select) return;
  select.innerHTML = "";
  if (!catalog || !Array.isArray(catalog.models) || !catalog.models.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "No models";
    select.appendChild(option);
    select.disabled = true;
    return;
  }
  select.disabled = false;
  catalog.models.forEach((model) => {
    const option = document.createElement("option");
    option.value = model.id;
    option.textContent =
      model.display_name && model.display_name !== model.id
        ? `${model.display_name} (${model.id})`
        : model.id;
    select.appendChild(option);
  });
}

function ensureReasoningOptions(select: HTMLSelectElement | null | undefined, model: ModelCatalogModel | null): void {
  if (!select) return;
  select.innerHTML = "";
  if (!model || !model.supports_reasoning || !model.reasoning_options?.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "None";
    select.appendChild(option);
    select.disabled = true;
    return;
  }
  select.disabled = false;
  model.reasoning_options.forEach((optionValue) => {
    const option = document.createElement("option");
    option.value = optionValue;
    option.textContent = optionValue;
    select.appendChild(option);
  });
}

function resolveSelectedModel(agent: string, catalog: ModelCatalog | null): string {
  if (!catalog?.models?.length) return "";
  const stored = getSelectedModel(agent);
  if (stored && catalog.models.some((entry) => entry.id === stored)) {
    return stored;
  }
  if (
    catalog.default_model &&
    catalog.models.some((entry) => entry.id === catalog.default_model)
  ) {
    return catalog.default_model;
  }
  return catalog.models[0].id;
}

function resolveSelectedReasoning(agent: string, model: ModelCatalogModel | null): string {
  if (!model || !model.reasoning_options?.length) return "";
  const stored = getSelectedReasoning(agent);
  if (stored && model.reasoning_options.includes(stored)) {
    return stored;
  }
  return model.reasoning_options[0] || "";
}

async function refreshControls(): Promise<void> {
  try {
    await loadAgents();
  } catch (err) {
    console.warn("Failed to load agents during refresh", err);
    ensureFallbackAgents();
  }

  const selectedAgent = getSelectedAgent();

  // Always update agent options first (uses in-memory agentList)
  controls.forEach((control) => {
    ensureAgentOptions(control.agentSelect);
  });

  // Then try to load model catalog
  let catalog = modelCatalogs.get(selectedAgent);
  if (!catalog) {
    try {
      catalog = await loadModelCatalog(selectedAgent);
    } catch (err) {
      console.warn(`Failed to load model catalog for ${selectedAgent}`, err);
      catalog = null;
    }
  }

  // Update model and reasoning options
  controls.forEach((control) => {
    ensureModelOptions(control.modelSelect, catalog);
    if (catalog) {
      const selectedModelId = resolveSelectedModel(selectedAgent, catalog);
      setSelectedModel(selectedAgent, selectedModelId);
      if (control.modelSelect) {
        control.modelSelect.value = selectedModelId;
      }
      const modelEntry = catalog.models.find((entry) => entry.id === selectedModelId);
      ensureReasoningOptions(control.reasoningSelect, modelEntry || null);
      const selectedReasoning = resolveSelectedReasoning(selectedAgent, modelEntry || null);
      setSelectedReasoning(selectedAgent, selectedReasoning);
      if (control.reasoningSelect) {
        control.reasoningSelect.value = selectedReasoning;
      }
    } else {
      ensureReasoningOptions(control.reasoningSelect, null);
    }
  });
}

async function handleAgentChange(nextAgent: string): Promise<void> {
  const previous = getSelectedAgent();
  setSelectedAgent(nextAgent);
  try {
    await loadModelCatalog(nextAgent);
  } catch (err) {
    setSelectedAgent(previous);
    flash(
      `Failed to load ${getLabelText(nextAgent)} models; staying on ${getLabelText(previous)}.`,
      "error"
    );
  }
  await refreshControls();
}

async function handleModelChange(nextModel: string): Promise<void> {
  const agent = getSelectedAgent();
  setSelectedModel(agent, nextModel);
  await refreshControls();
}

async function handleReasoningChange(nextReasoning: string): Promise<void> {
  const agent = getSelectedAgent();
  setSelectedReasoning(agent, nextReasoning);
  await refreshControls();
}

/**
 * @param {AgentControlConfig} [config]
 */
export function initAgentControls(config: AgentControlConfig = {}): void {
  const { agentSelect, modelSelect, reasoningSelect } = config;
  if (!agentSelect && !modelSelect && !reasoningSelect) {
    return;
  }
  const control: AgentControl = { agentSelect, modelSelect, reasoningSelect };
  controls.push(control);

  // Immediately populate agent options from in-memory list (synchronous)
  ensureAgentOptions(agentSelect);
  ensureModelOptions(modelSelect, null);
  ensureReasoningOptions(reasoningSelect, null);

  if (agentSelect) {
    agentSelect.addEventListener("change", (event) => {
      const target = event.target as HTMLSelectElement;
      handleAgentChange(target.value);
    });
  }
  if (modelSelect) {
    modelSelect.addEventListener("change", (event) => {
      const target = event.target as HTMLSelectElement;
      handleModelChange(target.value);
    });
  }
  if (reasoningSelect) {
    reasoningSelect.addEventListener("change", (event) => {
      const target = event.target as HTMLSelectElement;
      handleReasoningChange(target.value);
    });
  }

  // Async refresh to load from API (will update if API returns different data)
  refreshControls().catch((err) => {
    console.warn("Failed to refresh agent controls", err);
  });
}

export async function ensureAgentCatalog(): Promise<void> {
  await refreshControls();
}
