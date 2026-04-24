// GENERATED FILE - do not edit directly. Source: static_src/
import { api, flash } from "./utils.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
import { createSmartRefresh } from "./smartRefresh.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
import { REPO_ID } from "./env.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
const API_PREFIX = REPO_ID ? "/api" : "/hub/pma";
const STORAGE_PREFIX = REPO_ID ? "car.agent" : "car.pma.agent";
const STORAGE_KEYS = {
    selected: `${STORAGE_PREFIX}.selected`,
    profile: (agent) => `${STORAGE_PREFIX}.${agent}.profile`,
    model: (agent) => `${STORAGE_PREFIX}.${agent}.model`,
    reasoning: (agent) => `${STORAGE_PREFIX}.${agent}.reasoning`,
};
const FALLBACK_AGENTS = [];
const controls = [];
let agentsLoaded = false;
let agentsLoadFailed = false;
let agentsLoadPromise = null;
let agentList = [...FALLBACK_AGENTS];
let defaultAgent = "";
const modelCatalogs = new Map();
const modelCatalogPromises = new Map();
const agentControlsRefresh = createSmartRefresh({
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
                .map((model) => `${model.id}:${model.display_name || ""}:${model.supports_reasoning ? "1" : "0"}:${model.reasoning_options.join(",")}`)
                .join("|")}`
            : "none";
        return `${agentsSig}::${payload.defaultAgent}::${payload.selectedProfile}::${payload.mode}::${catalogSig}`;
    },
    render: (payload) => {
        renderAgentControls(payload);
    },
});
function safeGetStorage(key) {
    try {
        return localStorage.getItem(key);
    }
    catch (_err) {
        return null;
    }
}
function safeSetStorage(key, value) {
    try {
        if (value === null || value === undefined || value === "") {
            localStorage.removeItem(key);
        }
        else {
            localStorage.setItem(key, String(value));
        }
    }
    catch (_err) {
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
export function getSelectedProfile(agent = getSelectedAgent()) {
    return safeGetStorage(STORAGE_KEYS.profile(agent)) || "";
}
export function getSelectedReasoning(agent = getSelectedAgent()) {
    return safeGetStorage(STORAGE_KEYS.reasoning(agent)) || "";
}
function setSelectedAgent(agent) {
    safeSetStorage(STORAGE_KEYS.selected, agent);
}
function setSelectedProfile(agent, profile) {
    safeSetStorage(STORAGE_KEYS.profile(agent), profile);
}
function setSelectedModel(agent, model) {
    safeSetStorage(STORAGE_KEYS.model(agent), model);
}
function setSelectedReasoning(agent, reasoning) {
    safeSetStorage(STORAGE_KEYS.reasoning(agent), reasoning);
}
function ensureFallbackAgents() {
    if (!agentList.length) {
        defaultAgent = "";
        return;
    }
    if (!agentList.some((agent) => agent.id === defaultAgent)) {
        defaultAgent = agentList[0].id;
    }
}
function getAgentEntry(agentId) {
    return agentList.find((agent) => agent.id === agentId);
}
function agentProfiles(agentId) {
    const entry = getAgentEntry(agentId);
    return Array.isArray(entry?.profiles) ? entry.profiles : [];
}
function cloneProfile(profile) {
    return {
        id: profile.id,
        display_name: profile.display_name,
    };
}
function cloneAgent(agent) {
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
export function getRegisteredAgents() {
    ensureFallbackAgents();
    return agentList.map(cloneAgent);
}
export function getRegisteredAgentProfiles(agentId) {
    return agentProfiles(agentId).map(cloneProfile);
}
async function loadAgents() {
    if (agentsLoaded)
        return;
    if (agentsLoadPromise) {
        await agentsLoadPromise;
        return;
    }
    agentsLoadPromise = (async () => {
        try {
            const data = await api(`${API_PREFIX}/agents`, { method: "GET" });
            const rawAgents = data?.agents;
            if (Array.isArray(rawAgents) &&
                rawAgents.every((a) => a && typeof a.id === "string")) {
                agentList = rawAgents;
                defaultAgent = data?.default || defaultAgent;
                agentsLoadFailed = false;
            }
            else {
                agentsLoadFailed = true;
            }
        }
        catch (err) {
            console.warn("Failed to load agent list", err);
            agentsLoadFailed = true;
        }
        finally {
            ensureFallbackAgents();
            agentsLoaded = true;
            agentsLoadPromise = null;
        }
    })();
    await agentsLoadPromise;
}
function normalizeCatalog(raw) {
    if (!raw || typeof raw !== "object") {
        return { default_model: "", models: [] };
    }
    const rawObj = raw;
    const models = Array.isArray(rawObj?.models) ? rawObj.models : [];
    const normalized = models
        .map((entry) => {
        if (!entry || typeof entry !== "object")
            return null;
        const entryObj = entry;
        const id = entryObj.id;
        if (!id || typeof id !== "string")
            return null;
        const displayName = typeof entryObj.display_name === "string" && entryObj.display_name
            ? entryObj.display_name
            : id;
        const supportsReasoning = Boolean(entryObj.supports_reasoning);
        const reasoningOptions = Array.isArray(entryObj.reasoning_options)
            ? entryObj.reasoning_options.filter((value) => typeof value === "string")
            : [];
        return {
            id,
            display_name: displayName,
            supports_reasoning: supportsReasoning,
            reasoning_options: reasoningOptions,
        };
    })
        .filter((model) => model !== null);
    const defaultModel = typeof rawObj?.default_model === "string" ? rawObj.default_model : "";
    return {
        default_model: defaultModel,
        models: normalized,
    };
}
async function loadModelCatalog(agent) {
    if (modelCatalogs.has(agent))
        return modelCatalogs.get(agent) || null;
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
function getLabelText(agentId) {
    const entry = getAgentEntry(agentId);
    return entry?.name || agentId;
}
function agentHasCapability(agentId, capability) {
    const entry = agentList.find((agent) => agent.id === agentId);
    return Array.isArray(entry?.capabilities) && entry.capabilities.includes(capability);
}
function modelControlModeForAgent(agentId) {
    if (agentHasCapability(agentId, "model_listing")) {
        return "catalog";
    }
    if (agentHasCapability(agentId, "message_turns")) {
        return "manual";
    }
    return "none";
}
function ensureAgentOptions(select) {
    if (!select)
        return;
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
        }
        else {
            option.textContent = label;
        }
        select.appendChild(option);
    });
    select.value = selected;
}
function ensureProfileOptions(select, agentId) {
    if (!select)
        return;
    const profiles = agentProfiles(agentId);
    select.classList.toggle("hidden", profiles.length === 0);
    select.innerHTML = "";
    if (!profiles.length) {
        const option = document.createElement("option");
        option.value = "";
        option.textContent = "No profiles";
        select.appendChild(option);
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
function ensureModelOptions(select, catalog, mode) {
    if (!select)
        return;
    select.innerHTML = "";
    if (mode !== "catalog" ||
        !catalog ||
        !Array.isArray(catalog.models) ||
        !catalog.models.length) {
        const option = document.createElement("option");
        option.value = "";
        option.textContent = mode === "manual" ? "Manual override" : "No models";
        select.appendChild(option);
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
function ensureReasoningOptions(select, model) {
    if (!select)
        return;
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
function ensureManualModelInput(input, { agent, mode, }) {
    if (!input)
        return;
    const isManual = mode === "manual";
    input.classList.toggle("hidden", !isManual);
    input.disabled = !isManual;
    input.value = isManual ? getSelectedModel(agent) : "";
    input.placeholder = isManual
        ? `Manual ${getLabelText(agent)} model override`
        : "Manual model override";
}
function resolveSelectedModel(agent, catalog) {
    if (!catalog?.models?.length)
        return "";
    const stored = getSelectedModel(agent);
    if (stored && catalog.models.some((entry) => entry.id === stored)) {
        return stored;
    }
    if (catalog.default_model &&
        catalog.models.some((entry) => entry.id === catalog.default_model)) {
        return catalog.default_model;
    }
    return "";
}
function resolveSelectedProfile(agent) {
    const profiles = agentProfiles(agent);
    if (!profiles.length)
        return "";
    const stored = getSelectedProfile(agent);
    if (stored && profiles.some((entry) => entry.id === stored)) {
        return stored;
    }
    const entry = getAgentEntry(agent);
    const defaultProfile = typeof entry?.default_profile === "string" ? entry.default_profile : "";
    if (defaultProfile && profiles.some((profile) => profile.id === defaultProfile)) {
        return defaultProfile;
    }
    return "";
}
function resolveSelectedReasoning(agent, model) {
    if (!model || !model.reasoning_options?.length)
        return "";
    const stored = getSelectedReasoning(agent);
    if (stored && model.reasoning_options.includes(stored)) {
        return stored;
    }
    return "";
}
async function loadAgentControlsPayload() {
    try {
        await loadAgents();
    }
    catch (err) {
        console.warn("Failed to load agents during refresh", err);
        ensureFallbackAgents();
    }
    const selectedAgent = getSelectedAgent();
    const mode = modelControlModeForAgent(selectedAgent);
    let catalog = null;
    if (mode === "catalog") {
        if (modelCatalogs.has(selectedAgent)) {
            catalog = modelCatalogs.get(selectedAgent) || null;
        }
        else {
            try {
                catalog = await loadModelCatalog(selectedAgent);
            }
            catch (err) {
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
function renderAgentControls(payload) {
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
        }
        else {
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
            const selectedReasoning = resolveSelectedReasoning(selectedAgent, modelEntry || null);
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
export async function refreshAgentControls(request = {}) {
    await agentControlsRefresh.refresh(loadAgentControlsPayload, request);
}
export async function setSelectedAgentProfile(agent, profile = "") {
    setSelectedAgent(agent);
    setSelectedProfile(agent, profile);
    await refreshAgentControls({ force: true, reason: "manual" });
}
async function handleAgentChange(nextAgent) {
    const previous = getSelectedAgent();
    setSelectedAgent(nextAgent);
    if (modelControlModeForAgent(nextAgent) === "catalog") {
        try {
            await loadModelCatalog(nextAgent);
        }
        catch (err) {
            setSelectedAgent(previous);
            flash(`Failed to load ${getLabelText(nextAgent)} models; staying on ${getLabelText(previous)}.`, "error");
        }
    }
    await refreshAgentControls({ force: true, reason: "manual" });
}
async function handleModelChange(nextModel) {
    const agent = getSelectedAgent();
    setSelectedModel(agent, nextModel);
    await refreshAgentControls({ force: true, reason: "manual" });
}
async function handleProfileChange(nextProfile) {
    const agent = getSelectedAgent();
    setSelectedProfile(agent, nextProfile);
    await refreshAgentControls({ force: true, reason: "manual" });
}
function handleManualModelInput(nextModel) {
    const agent = getSelectedAgent();
    setSelectedModel(agent, nextModel.trim());
}
async function handleReasoningChange(nextReasoning) {
    const agent = getSelectedAgent();
    setSelectedReasoning(agent, nextReasoning);
    await refreshAgentControls({ force: true, reason: "manual" });
}
/**
 * @param {AgentControlConfig} [config]
 */
export function initAgentControls(config = {}) {
    const { agentSelect, profileSelect, modelSelect, modelInput, reasoningSelect } = config;
    if (!agentSelect &&
        !profileSelect &&
        !modelSelect &&
        !modelInput &&
        !reasoningSelect) {
        return;
    }
    const control = {
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
            const target = event.target;
            void handleAgentChange(target.value);
        });
    }
    if (modelSelect) {
        modelSelect.addEventListener("change", (event) => {
            const target = event.target;
            void handleModelChange(target.value);
        });
    }
    if (modelInput) {
        modelInput.addEventListener("input", (event) => {
            const target = event.target;
            handleManualModelInput(target.value);
        });
    }
    if (profileSelect) {
        profileSelect.addEventListener("change", (event) => {
            const target = event.target;
            void handleProfileChange(target.value);
        });
    }
    if (reasoningSelect) {
        reasoningSelect.addEventListener("change", (event) => {
            const target = event.target;
            void handleReasoningChange(target.value);
        });
    }
    refreshAgentControls({ force: true, reason: "initial" }).catch((err) => {
        console.warn("Failed to refresh agent controls", err);
    });
}
export async function ensureAgentCatalog() {
    await refreshAgentControls({ force: true, reason: "manual" });
}
export const __agentControlsTest = {
    modelControlModeForAgent,
    get agentsLoadFailed() {
        return agentsLoadFailed;
    },
    reset() {
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
