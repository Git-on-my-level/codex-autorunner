import { api, flash } from "./utils.js";

const STORAGE_KEYS = {
  selected: "car.agent.selected",
  model: (agent) => `car.agent.${agent}.model`,
  reasoning: (agent) => `car.agent.${agent}.reasoning`,
};

const FALLBACK_AGENTS = [
  { id: "codex", name: "Codex" },
  { id: "opencode", name: "OpenCode" },
];

const controls = [];
let agentsLoaded = false;
let agentsLoadPromise = null;
let agentList = [...FALLBACK_AGENTS]; // Initialize with fallback
let defaultAgent = "codex";
const modelCatalogs = new Map();
const modelCatalogPromises = new Map();

function safeGetStorage(key) {
  try {
    return localStorage.getItem(key);
  } catch (_err) {
    return null;
  }
}

function safeSetStorage(key, value) {
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

export function getSelectedAgent() {
  const stored = safeGetStorage(STORAGE_KEYS.selected);
  if (stored && agentList.some((agent) => agent.id === stored)) {
    return stored;
  }
  return defaultAgent;
}

export function getSelectedModel(agent = getSelectedAgent()) {
  return safeGetStorage(STORAGE_KEYS.model(agent)) || "";
}

export function getSelectedReasoning(agent = getSelectedAgent()) {
  return safeGetStorage(STORAGE_KEYS.reasoning(agent)) || "";
}

function setSelectedAgent(agent) {
  safeSetStorage(STORAGE_KEYS.selected, agent);
}

function setSelectedModel(agent, model) {
  safeSetStorage(STORAGE_KEYS.model(agent), model);
}

function setSelectedReasoning(agent, reasoning) {
  safeSetStorage(STORAGE_KEYS.reasoning(agent), reasoning);
}

function ensureFallbackAgents() {
  if (!agentList.length) {
    agentList = [...FALLBACK_AGENTS];
  }
  if (!agentList.some((agent) => agent.id === defaultAgent)) {
    defaultAgent = agentList[0]?.id || "codex";
  }
}

async function loadAgents() {
  if (agentsLoaded) return;
  if (agentsLoadPromise) {
    await agentsLoadPromise;
    return;
  }
  agentsLoadPromise = (async () => {
    try {
      const data = await api("/api/agents", { method: "GET" });
      const agents = Array.isArray(data?.agents) ? data.agents : [];
      // Only use API response if it contains valid agents
      if (agents.length > 0 && agents.every((a) => a && typeof a.id === "string")) {
        agentList = agents;
        defaultAgent = data?.default || defaultAgent;
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

function normalizeCatalog(raw) {
  const models = Array.isArray(raw?.models) ? raw.models : [];
  const normalized = models
    .map((entry) => {
      if (!entry || typeof entry !== "object") return null;
      const id = entry.id;
      if (!id || typeof id !== "string") return null;
      const displayName =
        typeof entry.display_name === "string" && entry.display_name
          ? entry.display_name
          : id;
      const supportsReasoning = Boolean(entry.supports_reasoning);
      const reasoningOptions = Array.isArray(entry.reasoning_options)
        ? entry.reasoning_options.filter((value) => typeof value === "string")
        : [];
      return {
        id,
        display_name: displayName,
        supports_reasoning: supportsReasoning,
        reasoning_options: reasoningOptions,
      };
    })
    .filter(Boolean);
  const defaultModel =
    typeof raw?.default_model === "string" ? raw.default_model : "";
  return {
    default_model: defaultModel,
    models: normalized,
  };
}

async function loadModelCatalog(agent) {
  if (modelCatalogs.has(agent)) return modelCatalogs.get(agent);
  if (modelCatalogPromises.has(agent)) {
    return await modelCatalogPromises.get(agent);
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

function getLabelText(agentId) {
  const entry = agentList.find((agent) => agent.id === agentId);
  return entry?.name || agentId;
}

function ensureAgentOptions(select) {
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

function ensureModelOptions(select, catalog) {
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

function ensureReasoningOptions(select, model) {
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

function resolveSelectedModel(agent, catalog) {
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

function resolveSelectedReasoning(agent, model) {
  if (!model || !model.reasoning_options?.length) return "";
  const stored = getSelectedReasoning(agent);
  if (stored && model.reasoning_options.includes(stored)) {
    return stored;
  }
  return model.reasoning_options[0] || "";
}

async function refreshControls() {
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
      ensureReasoningOptions(control.reasoningSelect, modelEntry);
      const selectedReasoning = resolveSelectedReasoning(selectedAgent, modelEntry);
      setSelectedReasoning(selectedAgent, selectedReasoning);
      if (control.reasoningSelect) {
        control.reasoningSelect.value = selectedReasoning;
      }
    } else {
      ensureReasoningOptions(control.reasoningSelect, null);
    }
  });
}

async function handleAgentChange(nextAgent) {
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

async function handleModelChange(nextModel) {
  const agent = getSelectedAgent();
  setSelectedModel(agent, nextModel);
  await refreshControls();
}

async function handleReasoningChange(nextReasoning) {
  const agent = getSelectedAgent();
  setSelectedReasoning(agent, nextReasoning);
  await refreshControls();
}

/**
 * @typedef {Object} AgentControlConfig
 * @property {HTMLSelectElement|null} [agentSelect]
 * @property {HTMLSelectElement|null} [modelSelect]
 * @property {HTMLSelectElement|null} [reasoningSelect]
 */

/**
 * @param {AgentControlConfig} [config]
 */
export function initAgentControls(config = {}) {
  const { agentSelect, modelSelect, reasoningSelect } = config;
  if (!agentSelect && !modelSelect && !reasoningSelect) {
    return;
  }
  const control = { agentSelect, modelSelect, reasoningSelect };
  controls.push(control);

  // Immediately populate agent options from in-memory list (synchronous)
  ensureAgentOptions(agentSelect);
  ensureModelOptions(modelSelect, null);
  ensureReasoningOptions(reasoningSelect, null);

  if (agentSelect) {
    agentSelect.addEventListener("change", (event) => {
      const target = /** @type {HTMLSelectElement} */ (event.target);
      handleAgentChange(target.value);
    });
  }
  if (modelSelect) {
    modelSelect.addEventListener("change", (event) => {
      const target = /** @type {HTMLSelectElement} */ (event.target);
      handleModelChange(target.value);
    });
  }
  if (reasoningSelect) {
    reasoningSelect.addEventListener("change", (event) => {
      const target = /** @type {HTMLSelectElement} */ (event.target);
      handleReasoningChange(target.value);
    });
  }

  // Async refresh to load from API (will update if API returns different data)
  refreshControls().catch((err) => {
    console.warn("Failed to refresh agent controls", err);
  });
}

export async function ensureAgentCatalog() {
  await refreshControls();
}
