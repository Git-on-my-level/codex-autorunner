import { api, flash } from "./utils.js";

const STORAGE_KEYS = {
  selected: "car.agent.selected",
  model: (agent) => `car.agent.${agent}.model`,
  reasoning: (agent) => `car.agent.${agent}.reasoning`,
};

const controls = [];
let agentsLoaded = false;
let agentsLoadPromise = null;
let agentList = [];
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

async function loadAgents() {
  if (agentsLoaded) return;
  if (agentsLoadPromise) {
    await agentsLoadPromise;
    return;
  }
  agentsLoadPromise = api("/api/agents", { method: "GET" })
    .then((data) => {
      agentList = Array.isArray(data?.agents) ? data.agents : [];
      defaultAgent = data?.default || defaultAgent;
      if (!agentList.some((agent) => agent.id === defaultAgent)) {
        defaultAgent = agentList[0]?.id || "codex";
      }
      agentsLoaded = true;
    })
    .catch((err) => {
      console.warn("Failed to load agent list", err);
      agentsLoaded = true;
      agentList = agentList.length
        ? agentList
        : [
            { id: "codex", name: "Codex" },
            { id: "opencode", name: "OpenCode" },
          ];
    })
    .finally(() => {
      agentsLoadPromise = null;
    });
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
  await loadAgents();
  const selectedAgent = getSelectedAgent();
  let catalog = modelCatalogs.get(selectedAgent);
  if (!catalog) {
    try {
      catalog = await loadModelCatalog(selectedAgent);
    } catch (err) {
      catalog = null;
    }
  }
  controls.forEach((control) => {
    ensureAgentOptions(control.agentSelect);
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
  refreshControls();
}

export async function ensureAgentCatalog() {
  await refreshControls();
}
