import { api, flash } from "./utils.js";
import { createSmartRefresh, type SmartRefreshReason } from "./smartRefresh.js";
import { REPO_ID } from "./env.js";

const API_PREFIX = REPO_ID ? "/api" : "/hub/pma";

interface Agent {
  id: string;
  name?: string;
  version?: string;
  protocol_version?: string;
  capabilities?: string[];
  default_profile?: string | null;
  profiles?: AgentProfile[];
}

interface AgentProfile {
  id: string;
  display_name?: string;
}

export interface RegisteredAgentProfile {
  id: string;
  display_name?: string;
}

export interface RegisteredAgent {
  id: string;
  name?: string;
  version?: string;
  protocol_version?: string;
  capabilities?: string[];
  default_profile?: string | null;
  profiles?: RegisteredAgentProfile[];
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
  profileSelect?: HTMLSelectElement | null;
  modelSelect?: HTMLSelectElement | null;
  modelInput?: HTMLInputElement | null;
  reasoningSelect?: HTMLSelectElement | null;
}

interface AgentControl extends AgentControlConfig {
  agentSelect?: HTMLSelectElement | null;
  profileSelect?: HTMLSelectElement | null;
  modelSelect?: HTMLSelectElement | null;
  modelInput?: HTMLInputElement | null;
  reasoningSelect?: HTMLSelectElement | null;
}

interface AgentControlsPayload {
  agents: Agent[];
  defaultAgent: string;
  selectedAgent: string;
  selectedProfile: string;
  mode: ModelControlMode;
  catalog: ModelCatalog | null;
}

type ModelControlMode = "catalog" | "manual" | "none";

const STORAGE_PREFIX = REPO_ID ? "car.agent" : "car.pma.agent";
const STORAGE_KEYS = {
  selected: `${STORAGE_PREFIX}.selected`,
  profile: (agent: string) => `${STORAGE_PREFIX}.${agent}.profile`,
  model: (agent: string) => `${STORAGE_PREFIX}.${agent}.model`,
  reasoning: (agent: string) => `${STORAGE_PREFIX}.${agent}.reasoning`,
} as const;

const FALLBACK_AGENTS: Agent[] = [];

const controls: AgentControl[] = [];
let agentsLoaded = false;
let agentsLoadFailed = false;
let agentsLoadPromise: Promise<void> | null = null;
let agentList: Agent[] = [...FALLBACK_AGENTS];
let defaultAgent = "";
const modelCatalogs = new Map<string, ModelCatalog | null>();
const modelCatalogPromises = new Map<string, Promise<ModelCatalog | null>>();

const agentControlsRefresh = createSmartRefresh<AgentControlsPayload>({
  getSignature: (payload) => {
    const agentsSig = payload.agents
      .map((agent) => {
        const profileSig = Array.isArray(agent.profiles)
          ? agent.profiles
            .map((profile) => `${profile.id}:${profile.display_name || ""}`)
            .join(",")
          : "";
        const capabilitySig = Array.isArray(agent.capabilities)
          ? agent.capabilities.join(",")
          : "";
        return `${agent.id}:${agent.name || ""}:${agent.version || ""}:${agent.protocol_version || ""}:${capabilitySig}:${agent.default_profile || ""}:${profileSig}`;
      })
      .join("|");
    const catalogSig = payload.catalog
      ? `${payload.catalog.default_model || ""}:${payload.catalog.models
        .map(
          (model) =>
            `${model.id}:${model.display_name || ""}:${model.supports_reasoning ? "1" : "0"}:${model.reasoning_options.join(",")}`
        )
        .join("|")}`
      : "none";
    return `${agentsSig}::${payload.defaultAgent}::${payload.selectedProfile}::${payload.mode}::${catalogSig}`;
  },
  render: (payload) => {
    renderAgentControls(payload);
  },
});

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

export function getSelectedProfile(agent: string = getSelectedAgent()): string {
  return safeGetStorage(STORAGE_KEYS.profile(agent)) || "";
}

export function getSelectedReasoning(agent: string = getSelectedAgent()): string {
  return safeGetStorage(STORAGE_KEYS.reasoning(agent)) || "";
}

function setSelectedAgent(agent: string): void {
  safeSetStorage(STORAGE_KEYS.selected, agent);
}

function setSelectedProfile(agent: string, profile: string): void {
  safeSetStorage(STORAGE_KEYS.profile(agent), profile);
}

function setSelectedModel(agent: string, model: string): void {
  safeSetStorage(STORAGE_KEYS.model(agent), model);
}

function setSelectedReasoning(agent: string, reasoning: string): void {
  safeSetStorage(STORAGE_KEYS.reasoning(agent), reasoning);
}

function ensureFallbackAgents(): void {
  if (!agentList.length) {
    defaultAgent = "";
    return;
  }
  if (!agentList.some((agent) => agent.id === defaultAgent)) {
    defaultAgent = agentList[0].id;
  }
}

function getAgentEntry(agentId: string): Agent | undefined {
  return agentList.find((agent) => agent.id === agentId);
}

function agentProfiles(agentId: string): AgentProfile[] {
  const entry = getAgentEntry(agentId);
  return Array.isArray(entry?.profiles) ? entry.profiles : [];
}

function cloneProfile(profile: AgentProfile): RegisteredAgentProfile {
  return {
    id: profile.id,
    display_name: profile.display_name,
  };
}

function cloneAgent(agent: Agent): RegisteredAgent {
  return {
    id: agent.id,
    name: agent.name,
    version: agent.version,
    protocol_version: agent.protocol_version,
    capabilities: Array.isArray(agent.capabilities)
      ? [...agent.capabilities]
      : undefined,
    default_profile: agent.default_profile ?? null,
    profiles: Array.isArray(agent.profiles)
      ? agent.profiles.map(cloneProfile)
      : [],
  };
}

export function getRegisteredAgents(): RegisteredAgent[] {
  ensureFallbackAgents();
  return agentList.map(cloneAgent);
}

export function getRegisteredAgentProfiles(
  agentId: string
): RegisteredAgentProfile[] {
  return agentProfiles(agentId).map(cloneProfile);
}

async function loadAgents(): Promise<void> {
  if (agentsLoaded) return;
  if (agentsLoadPromise) {
    await agentsLoadPromise;
    return;
  }
  agentsLoadPromise = (async () => {
    try {
      const data = await api(`${API_PREFIX}/agents`, { method: "GET" });
      const rawAgents = (data as { agents?: unknown })?.agents;
      if (
        Array.isArray(rawAgents) &&
        rawAgents.every((a) => a && typeof (a as Agent).id === "string")
      ) {
        agentList = rawAgents as Agent[];
        defaultAgent = (data as { default?: string })?.default || defaultAgent;
        agentsLoadFailed = false;
      } else {
        agentsLoadFailed = true;
      }
    } catch (err) {
      console.warn("Failed to load agent list", err);
      agentsLoadFailed = true;
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
        ? (entryObj.reasoning_options as unknown[]).filter(
          (value) => typeof value === "string"
        )
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
  const promise = api(`${API_PREFIX}/agents/${encodeURIComponent(agent)}/models`, {
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
  const entry = getAgentEntry(agentId);
  return entry?.name || agentId;
}

function agentHasCapability(agentId: string, capability: string): boolean {
  const entry = agentList.find((agent) => agent.id === agentId);
  return Array.isArray(entry?.capabilities) && entry.capabilities.includes(capability);
}

function modelControlModeForAgent(agentId: string): ModelControlMode {
  if (agentHasCapability(agentId, "model_listing")) {
    return "catalog";
  }
  if (agentHasCapability(agentId, "message_turns")) {
    return "manual";
  }
  return "none";
}

function ensureAgentOptions(select: HTMLSelectElement | null | undefined): void {
  if (!select) return;
  const selected = getSelectedAgent();
  select.innerHTML = "";
  if (!agentList.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = agentsLoadFailed
      ? "Failed to load agents \u2014 refresh to retry"
      : "No agents available";
    select.appendChild(option);
    select.disabled = true;
    return;
  }
  select.disabled = false;
  agentList.forEach((agent) => {
    const option = document.createElement("option");
    option.value = agent.id;
    const label = agent.name || agent.id;
    const version = agent.version || agent.protocol_version;
    if (version) {
      option.textContent = `${label} (${version})`;
    } else {
      option.textContent = label;
    }
    select.appendChild(option);
  });
  select.value = selected;
}

function ensureProfileOptions(
  select: HTMLSelectElement | null | undefined,
  agentId: string
): void {
  if (!select) return;
  const profiles = agentProfiles(agentId);
  const hide = profiles.length === 0;
  select.classList.toggle("hidden", hide);
  select.innerHTML = "";
  if (hide) {
    select.disabled = true;
    return;
  }
  select.disabled = false;
  const profilePlaceholder = document.createElement("option");
  profilePlaceholder.value = "";
  profilePlaceholder.textContent = "Select a profile\u2026";
  profilePlaceholder.disabled = true;
  select.appendChild(profilePlaceholder);
  profiles.forEach((profile) => {
    const option = document.createElement("option");
    option.value = profile.id;
    option.textContent =
      profile.display_name && profile.display_name !== profile.id
        ? `${profile.display_name} (${profile.id})`
        : profile.id;
    select.appendChild(option);
  });
}

function ensureModelOptions(
  select: HTMLSelectElement | null | undefined,
  catalog: ModelCatalog | null,
  mode: ModelControlMode
): void {
  if (!select) return;
  const showCatalog =
    mode === "catalog" &&
    Boolean(catalog) &&
    Array.isArray(catalog?.models) &&
    (catalog?.models?.length ?? 0) > 0;
  select.classList.toggle("hidden", !showCatalog);
  select.innerHTML = "";
  if (!showCatalog) {
    select.disabled = true;
    return;
  }
  select.disabled = false;
  const modelPlaceholder = document.createElement("option");
  modelPlaceholder.value = "";
  modelPlaceholder.textContent = "Select a model\u2026";
  modelPlaceholder.disabled = true;
  select.appendChild(modelPlaceholder);
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

function ensureReasoningOptions(
  select: HTMLSelectElement | null | undefined,
  model: ModelCatalogModel | null
): void {
  if (!select) return;
  const show = Boolean(
    model?.supports_reasoning && model.reasoning_options?.length
  );
  select.classList.toggle("hidden", !show);
  select.innerHTML = "";
  if (!show) {
    select.disabled = true;
    return;
  }
  select.disabled = false;
  const reasoningPlaceholder = document.createElement("option");
  reasoningPlaceholder.value = "";
  reasoningPlaceholder.textContent = "Select reasoning\u2026";
  reasoningPlaceholder.disabled = true;
  select.appendChild(reasoningPlaceholder);
  model.reasoning_options.forEach((optionValue) => {
    const option = document.createElement("option");
    option.value = optionValue;
    option.textContent = optionValue;
    select.appendChild(option);
  });
}

function ensureManualModelInput(
  input: HTMLInputElement | null | undefined,
  {
    agent,
    mode,
  }: {
    agent: string;
    mode: ModelControlMode;
  }
): void {
  if (!input) return;
  const isManual = mode === "manual";
  input.classList.toggle("hidden", !isManual);
  input.disabled = !isManual;
  input.value = isManual ? getSelectedModel(agent) : "";
  input.placeholder = isManual
    ? `Manual ${getLabelText(agent)} model override`
    : "Manual model override";
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
  return catalog.models[0]?.id || "";
}

function resolveSelectedProfile(agent: string): string {
  const profiles = agentProfiles(agent);
  if (!profiles.length) return "";
  const stored = getSelectedProfile(agent);
  if (stored && profiles.some((entry) => entry.id === stored)) {
    return stored;
  }
  const entry = getAgentEntry(agent);
  const defaultProfile =
    typeof entry?.default_profile === "string" ? entry.default_profile : "";
  if (defaultProfile && profiles.some((profile) => profile.id === defaultProfile)) {
    return defaultProfile;
  }
  return profiles[0]?.id || "";
}

function resolveSelectedReasoning(
  agent: string,
  model: ModelCatalogModel | null
): string {
  if (!model?.supports_reasoning || !model.reasoning_options?.length) return "";
  const stored = getSelectedReasoning(agent);
  if (stored && model.reasoning_options.includes(stored)) {
    return stored;
  }
  return model.reasoning_options[0] || "";
}

async function loadAgentControlsPayload(): Promise<AgentControlsPayload> {
  try {
    await loadAgents();
  } catch (err) {
    console.warn("Failed to load agents during refresh", err);
    ensureFallbackAgents();
  }

  const selectedAgent = getSelectedAgent();
  const mode = modelControlModeForAgent(selectedAgent);

  let catalog: ModelCatalog | null = null;
  if (mode === "catalog") {
    if (modelCatalogs.has(selectedAgent)) {
      catalog = modelCatalogs.get(selectedAgent) || null;
    } else {
      try {
        catalog = await loadModelCatalog(selectedAgent);
      } catch (err) {
        console.warn(`Failed to load model catalog for ${selectedAgent}`, err);
        catalog = null;
      }
    }
  }

  return {
    agents: [...agentList],
    defaultAgent,
    selectedAgent,
    selectedProfile: resolveSelectedProfile(selectedAgent),
    mode,
    catalog,
  };
}

function renderAgentControls(payload: AgentControlsPayload): void {
  const selectedAgent = payload.selectedAgent;
  const selectedProfile = payload.selectedProfile;

  controls.forEach((control) => {
    ensureAgentOptions(control.agentSelect);
    ensureProfileOptions(control.profileSelect, selectedAgent);
    if (control.profileSelect) {
      control.profileSelect.value = selectedProfile;
    }
  });

  const { catalog, mode } = payload;

  controls.forEach((control) => {
    ensureModelOptions(control.modelSelect, catalog, mode);
    ensureManualModelInput(control.modelInput, { agent: selectedAgent, mode });
    if (selectedProfile) {
      setSelectedProfile(selectedAgent, selectedProfile);
    } else {
      setSelectedProfile(selectedAgent, "");
    }
    if (mode === "catalog" && catalog) {
      const selectedModelId = resolveSelectedModel(selectedAgent, catalog);
      setSelectedModel(selectedAgent, selectedModelId);
      if (control.modelSelect) {
        control.modelSelect.value = selectedModelId;
      }
      const modelEntry = catalog.models.find((entry) => entry.id === selectedModelId);
      ensureReasoningOptions(control.reasoningSelect, modelEntry || null);
      const selectedReasoning = resolveSelectedReasoning(
        selectedAgent,
        modelEntry || null
      );
      setSelectedReasoning(selectedAgent, selectedReasoning);
      if (control.reasoningSelect) {
        control.reasoningSelect.value = selectedReasoning;
      }
      return;
    }
    if (mode !== "catalog") {
      setSelectedReasoning(selectedAgent, "");
    }
    ensureReasoningOptions(control.reasoningSelect, null);
  });
}

export async function refreshAgentControls(
  request: { force?: boolean; reason?: SmartRefreshReason } = {}
): Promise<void> {
  await agentControlsRefresh.refresh(loadAgentControlsPayload, request);
}

export async function setSelectedAgentProfile(
  agent: string,
  profile: string = ""
): Promise<void> {
  setSelectedAgent(agent);
  setSelectedProfile(agent, profile);
  await refreshAgentControls({ force: true, reason: "manual" });
}

async function handleAgentChange(nextAgent: string): Promise<void> {
  const previous = getSelectedAgent();
  setSelectedAgent(nextAgent);
  if (modelControlModeForAgent(nextAgent) === "catalog") {
    try {
      await loadModelCatalog(nextAgent);
    } catch (err) {
      setSelectedAgent(previous);
      flash(
        `Failed to load ${getLabelText(nextAgent)} models; staying on ${getLabelText(previous)}.`,
        "error"
      );
    }
  }
  await refreshAgentControls({ force: true, reason: "manual" });
}

async function handleModelChange(nextModel: string): Promise<void> {
  const agent = getSelectedAgent();
  setSelectedModel(agent, nextModel);
  await refreshAgentControls({ force: true, reason: "manual" });
}

async function handleProfileChange(nextProfile: string): Promise<void> {
  const agent = getSelectedAgent();
  setSelectedProfile(agent, nextProfile);
  await refreshAgentControls({ force: true, reason: "manual" });
}

function handleManualModelInput(nextModel: string): void {
  const agent = getSelectedAgent();
  setSelectedModel(agent, nextModel.trim());
}

async function handleReasoningChange(nextReasoning: string): Promise<void> {
  const agent = getSelectedAgent();
  setSelectedReasoning(agent, nextReasoning);
  await refreshAgentControls({ force: true, reason: "manual" });
}

/**
 * @param {AgentControlConfig} [config]
 */
export function initAgentControls(config: AgentControlConfig = {}): void {
  const { agentSelect, profileSelect, modelSelect, modelInput, reasoningSelect } =
    config;
  if (
    !agentSelect &&
    !profileSelect &&
    !modelSelect &&
    !modelInput &&
    !reasoningSelect
  ) {
    return;
  }
  const control: AgentControl = {
    agentSelect,
    profileSelect,
    modelSelect,
    modelInput,
    reasoningSelect,
  };
  controls.push(control);

  ensureAgentOptions(agentSelect);
  ensureProfileOptions(profileSelect, getSelectedAgent());
  ensureModelOptions(modelSelect, null, "none");
  ensureManualModelInput(modelInput, { agent: getSelectedAgent(), mode: "none" });
  ensureReasoningOptions(reasoningSelect, null);

  if (agentSelect) {
    agentSelect.addEventListener("change", (event) => {
      const target = event.target as HTMLSelectElement;
      void handleAgentChange(target.value);
    });
  }
  if (modelSelect) {
    modelSelect.addEventListener("change", (event) => {
      const target = event.target as HTMLSelectElement;
      void handleModelChange(target.value);
    });
  }
  if (modelInput) {
    modelInput.addEventListener("input", (event) => {
      const target = event.target as HTMLInputElement;
      handleManualModelInput(target.value);
    });
  }
  if (profileSelect) {
    profileSelect.addEventListener("change", (event) => {
      const target = event.target as HTMLSelectElement;
      void handleProfileChange(target.value);
    });
  }
  if (reasoningSelect) {
    reasoningSelect.addEventListener("change", (event) => {
      const target = event.target as HTMLSelectElement;
      void handleReasoningChange(target.value);
    });
  }

  refreshAgentControls({ force: true, reason: "initial" }).catch((err) => {
    console.warn("Failed to refresh agent controls", err);
  });
}

export async function ensureAgentCatalog(): Promise<void> {
  await refreshAgentControls({ force: true, reason: "manual" });
}

export const __agentControlsTest = {
  modelControlModeForAgent,
  get agentsLoadFailed(): boolean {
    return agentsLoadFailed;
  },
  reset(): void {
    agentControlsRefresh.reset();
    controls.length = 0;
    agentsLoaded = false;
    agentsLoadFailed = false;
    agentsLoadPromise = null;
    agentList = [...FALLBACK_AGENTS];
    defaultAgent = "";
    modelCatalogs.clear();
    modelCatalogPromises.clear();
  },
};
