// GENERATED FILE - do not edit directly. Source: static_src/
import { api, confirmModal, flash, resolvePath, openModal } from "./utils.js";
import { initTemplateReposSettings, loadTemplateRepos, } from "./templateReposSettings.js";
import { handleSystemUpdate, loadUpdateTargetOptions, } from "./systemUpdateUi.js";
const ui = {
    settingsBtn: document.getElementById("repo-settings"),
    threadList: document.getElementById("thread-tools-list"),
    threadNew: document.getElementById("thread-new-autorunner"),
    threadArchive: document.getElementById("thread-archive-autorunner"),
    threadResetAll: document.getElementById("thread-reset-all"),
    threadDownload: document.getElementById("thread-backup-download"),
    updateTarget: document.getElementById("repo-update-target"),
    updateBtn: document.getElementById("repo-update-btn"),
    closeBtn: document.getElementById("repo-settings-close"),
    modelSelect: document.getElementById("autorunner-model-select"),
    effortSelect: document.getElementById("autorunner-effort-select"),
    approvalSelect: document.getElementById("autorunner-approval-select"),
    sandboxSelect: document.getElementById("autorunner-sandbox-select"),
    maxRunsInput: document.getElementById("autorunner-max-runs-input"),
    networkToggle: document.getElementById("autorunner-network-toggle"),
    saveBtn: document.getElementById("autorunner-settings-save"),
    reloadBtn: document.getElementById("autorunner-settings-reload"),
};
const DEFAULT_OPTION_LABEL = "Default (inherit config)";
const APPROVAL_OPTIONS = [
    { value: "", label: DEFAULT_OPTION_LABEL },
    { value: "never", label: "never" },
    { value: "unlessTrusted", label: "unlessTrusted" },
];
const SANDBOX_OPTIONS = [
    { value: "", label: DEFAULT_OPTION_LABEL },
    { value: "dangerFullAccess", label: "dangerFullAccess" },
    { value: "workspaceWrite", label: "workspaceWrite" },
];
let repoSettingsCloseModal = null;
let currentCatalog = null;
let currentCatalogAgent = "codex";
let settingsBusy = false;
let settingsLoaded = false;
function normalizeOptionalString(value) {
    if (typeof value !== "string")
        return null;
    const cleaned = value.trim();
    return cleaned || null;
}
function normalizeOptionalBoolean(value) {
    if (typeof value !== "boolean")
        return null;
    return value;
}
function normalizeOptionalInteger(value) {
    if (typeof value !== "number" || !Number.isInteger(value) || value <= 0) {
        return null;
    }
    return value;
}
function normalizeCatalog(raw) {
    if (!raw || typeof raw !== "object") {
        return { default_model: "", models: [] };
    }
    const rawObj = raw;
    const models = Array.isArray(rawObj.models) ? rawObj.models : [];
    const normalized = models
        .map((entry) => {
        if (!entry || typeof entry !== "object")
            return null;
        const entryObj = entry;
        const id = normalizeOptionalString(entryObj.id);
        if (!id)
            return null;
        const reasoningOptions = Array.isArray(entryObj.reasoning_options)
            ? entryObj.reasoning_options.filter((option) => typeof option === "string" && option.trim().length > 0)
            : [];
        return {
            id,
            display_name: normalizeOptionalString(entryObj.display_name) || id,
            supports_reasoning: Boolean(entryObj.supports_reasoning),
            reasoning_options: reasoningOptions,
        };
    })
        .filter((model) => model !== null);
    return {
        default_model: normalizeOptionalString(rawObj.default_model) || "",
        models: normalized,
    };
}
function renderThreadTools(data) {
    if (!ui.threadList)
        return;
    ui.threadList.innerHTML = "";
    if (!data) {
        ui.threadList.textContent = "Unable to load thread info.";
        return;
    }
    const entries = [];
    if (data.autorunner !== undefined) {
        entries.push({ label: "Autorunner", value: data.autorunner || "—" });
    }
    if (data.file_chat !== undefined) {
        entries.push({ label: "File chat", value: data.file_chat || "—" });
    }
    if (data.file_chat_opencode !== undefined) {
        entries.push({
            label: "File chat (opencode)",
            value: data.file_chat_opencode || "—",
        });
    }
    Object.keys(data).forEach((key) => {
        if (["autorunner", "file_chat", "file_chat_opencode", "corruption"].includes(key)) {
            return;
        }
        const value = data[key];
        if (typeof value === "string" || typeof value === "number") {
            entries.push({ label: key, value: value || "—" });
        }
    });
    if (!entries.length) {
        ui.threadList.textContent = "No threads recorded.";
        return;
    }
    entries.forEach((entry) => {
        const row = document.createElement("div");
        row.className = "thread-tool-row";
        row.innerHTML = `
      <span class="thread-tool-label">${entry.label}</span>
      <span class="thread-tool-value">${entry.value}</span>
    `;
        ui.threadList?.appendChild(row);
    });
    if (ui.threadArchive) {
        ui.threadArchive.disabled = !data.autorunner;
    }
}
async function loadThreadTools() {
    try {
        const data = (await api("/api/app-server/threads"));
        renderThreadTools(data);
        return data;
    }
    catch (err) {
        renderThreadTools(null);
        flash(err.message || "Failed to load threads", "error");
        return null;
    }
}
function setSelectOptions(select, options, selectedValue, unknownLabel, preserveUnknown = true) {
    if (!select)
        return;
    const normalizedSelected = normalizeOptionalString(selectedValue) || "";
    const rendered = [...options];
    if (preserveUnknown &&
        normalizedSelected &&
        !rendered.some((option) => option.value === normalizedSelected)) {
        rendered.push({
            value: normalizedSelected,
            label: `${normalizedSelected} (${unknownLabel})`,
        });
    }
    select.replaceChildren();
    rendered.forEach((entry) => {
        const option = document.createElement("option");
        option.value = entry.value;
        option.textContent = entry.label;
        select.appendChild(option);
    });
    select.dataset.optionAvailable =
        rendered.length <= 1 && rendered[0]?.value === "" ? "0" : "1";
    select.value = rendered.some((entry) => entry.value === normalizedSelected)
        ? normalizedSelected
        : rendered[0]?.value || "";
}
function modelLabel(model) {
    return model.display_name && model.display_name !== model.id
        ? `${model.display_name} (${model.id})`
        : model.id;
}
function currentEffectiveModelId() {
    const selectedModel = normalizeOptionalString(ui.modelSelect?.value || null);
    if (selectedModel)
        return selectedModel;
    if (currentCatalog?.default_model &&
        currentCatalog.models.some((model) => model.id === currentCatalog.default_model)) {
        return currentCatalog.default_model;
    }
    return currentCatalog?.models[0]?.id || null;
}
function currentEffectiveModel() {
    const modelId = currentEffectiveModelId();
    if (!modelId || !currentCatalog)
        return null;
    return currentCatalog.models.find((model) => model.id === modelId) || null;
}
function updateReasoningOptions(selectedValue, preserveUnknown = true) {
    const model = currentEffectiveModel();
    const options = [{ value: "", label: DEFAULT_OPTION_LABEL }];
    if (model?.supports_reasoning) {
        model.reasoning_options.forEach((optionValue) => {
            options.push({ value: optionValue, label: optionValue });
        });
    }
    setSelectOptions(ui.effortSelect, options, selectedValue, "current override", preserveUnknown);
}
function renderAutorunnerSettings(data) {
    const modelOptions = [{ value: "", label: DEFAULT_OPTION_LABEL }];
    currentCatalog?.models.forEach((model) => {
        modelOptions.push({ value: model.id, label: modelLabel(model) });
    });
    setSelectOptions(ui.modelSelect, modelOptions, normalizeOptionalString(data.autorunner_model_override), "current override");
    updateReasoningOptions(normalizeOptionalString(data.autorunner_effort_override), true);
    setSelectOptions(ui.approvalSelect, APPROVAL_OPTIONS, normalizeOptionalString(data.autorunner_approval_policy), "current override");
    setSelectOptions(ui.sandboxSelect, SANDBOX_OPTIONS, normalizeOptionalString(data.autorunner_sandbox_mode), "current override");
    if (ui.maxRunsInput) {
        const maxRuns = normalizeOptionalInteger(data.runner_stop_after_runs);
        ui.maxRunsInput.value = maxRuns ? String(maxRuns) : "";
    }
    if (ui.networkToggle) {
        const networkSetting = normalizeOptionalBoolean(data.autorunner_workspace_write_network);
        ui.networkToggle.checked = networkSetting === true;
        ui.networkToggle.indeterminate = networkSetting === null;
    }
    updateAutorunnerFormInteractivity();
}
async function resolveCatalogAgent() {
    try {
        const data = (await api("/api/agents", {
            method: "GET",
        }));
        const agents = Array.isArray(data.agents) ? data.agents : [];
        const supportsListing = (agent) => Array.isArray(agent?.capabilities) &&
            agent.capabilities.includes("model_listing");
        const defaultAgentId = normalizeOptionalString(data.default) || "codex";
        const defaultAgent = agents.find((agent) => agent.id === defaultAgentId);
        if (supportsListing(defaultAgent)) {
            return defaultAgentId;
        }
        const codexAgent = agents.find((agent) => agent.id === "codex");
        if (supportsListing(codexAgent)) {
            return "codex";
        }
        const firstListed = agents.find((agent) => supportsListing(agent));
        return normalizeOptionalString(firstListed?.id) || defaultAgentId;
    }
    catch (_err) {
        return "codex";
    }
}
async function loadCatalog(agentId) {
    try {
        const data = await api(`/api/agents/${encodeURIComponent(agentId)}/models`, {
            method: "GET",
        });
        return normalizeCatalog(data);
    }
    catch (_err) {
        return null;
    }
}
function setAutorunnerBusy(busy) {
    settingsBusy = busy;
    updateAutorunnerFormInteractivity();
}
function updateAutorunnerFormInteractivity() {
    const formDisabled = settingsBusy || !settingsLoaded;
    const applySelectState = (select) => {
        if (!select)
            return;
        select.disabled =
            formDisabled || select.dataset.optionAvailable === "0";
    };
    applySelectState(ui.modelSelect);
    applySelectState(ui.effortSelect);
    applySelectState(ui.approvalSelect);
    applySelectState(ui.sandboxSelect);
    if (ui.maxRunsInput)
        ui.maxRunsInput.disabled = formDisabled;
    if (ui.networkToggle)
        ui.networkToggle.disabled = formDisabled;
    if (ui.saveBtn)
        ui.saveBtn.disabled = settingsBusy || !settingsLoaded;
    if (ui.reloadBtn)
        ui.reloadBtn.disabled = settingsBusy;
}
async function loadAutorunnerSettings() {
    settingsLoaded = false;
    setAutorunnerBusy(true);
    try {
        const [settingsPayload, agentId] = await Promise.all([
            api("/api/session/settings", { method: "GET" }),
            resolveCatalogAgent(),
        ]);
        currentCatalogAgent = agentId;
        currentCatalog = await loadCatalog(agentId);
        settingsLoaded = true;
        renderAutorunnerSettings(settingsPayload);
    }
    catch (err) {
        currentCatalog = null;
        settingsLoaded = false;
        renderAutorunnerSettings({});
        flash(err.message || "Failed to load autorunner settings", "error");
    }
    finally {
        setAutorunnerBusy(false);
    }
}
function collectAutorunnerSettingsPayload() {
    const maxRunsRaw = ui.maxRunsInput?.value.trim() || "";
    const parsedMaxRuns = maxRunsRaw ? Number.parseInt(maxRunsRaw, 10) : NaN;
    return {
        autorunner_model_override: normalizeOptionalString(ui.modelSelect?.value || null),
        autorunner_effort_override: normalizeOptionalString(ui.effortSelect?.value || null),
        autorunner_approval_policy: normalizeOptionalString(ui.approvalSelect?.value || null),
        autorunner_sandbox_mode: normalizeOptionalString(ui.sandboxSelect?.value || null),
        autorunner_workspace_write_network: ui.networkToggle && !ui.networkToggle.indeterminate
            ? ui.networkToggle.checked
            : null,
        runner_stop_after_runs: Number.isInteger(parsedMaxRuns) && parsedMaxRuns > 0 ? parsedMaxRuns : null,
    };
}
async function saveAutorunnerSettings() {
    if (settingsBusy || !settingsLoaded)
        return;
    setAutorunnerBusy(true);
    try {
        const payload = collectAutorunnerSettingsPayload();
        await api("/api/session/settings", {
            method: "POST",
            body: payload,
        });
        flash("Autorunner settings saved", "success");
        await refreshSettings();
    }
    catch (err) {
        flash(err.message || "Failed to save autorunner settings", "error");
    }
    finally {
        setAutorunnerBusy(false);
    }
}
async function refreshSettings() {
    await Promise.all([
        loadThreadTools(),
        loadTemplateRepos(),
        loadAutorunnerSettings(),
    ]);
}
export function initRepoSettingsPanel() {
    window.__CAR_SETTINGS = { loadThreadTools, refreshSettings };
    initRepoSettingsModal();
    initTemplateReposSettings();
    if (ui.threadNew) {
        ui.threadNew.addEventListener("click", async () => {
            try {
                await api("/api/app-server/threads/reset", {
                    method: "POST",
                    body: { key: "autorunner" },
                });
                flash("Started a new autorunner thread", "success");
                await loadThreadTools();
            }
            catch (err) {
                flash(err.message || "Failed to reset autorunner thread", "error");
            }
        });
    }
    if (ui.threadArchive) {
        ui.threadArchive.addEventListener("click", async () => {
            const data = await loadThreadTools();
            const threadId = data?.autorunner;
            if (!threadId) {
                flash("No autorunner thread to archive.", "error");
                return;
            }
            const confirmed = await confirmModal("Archive autorunner thread? This starts a new conversation.");
            if (!confirmed)
                return;
            try {
                await api("/api/app-server/threads/archive", {
                    method: "POST",
                    body: { thread_id: threadId },
                });
                await api("/api/app-server/threads/reset", {
                    method: "POST",
                    body: { key: "autorunner" },
                });
                flash("Autorunner thread archived", "success");
                await loadThreadTools();
            }
            catch (err) {
                flash(err.message || "Failed to archive thread", "error");
            }
        });
    }
    if (ui.threadResetAll) {
        ui.threadResetAll.addEventListener("click", async () => {
            const confirmed = await confirmModal("Reset all conversations? This clears all saved app-server threads.", { confirmText: "Reset all", danger: true });
            if (!confirmed)
                return;
            try {
                await api("/api/app-server/threads/reset-all", { method: "POST" });
                flash("Conversations reset", "success");
                await loadThreadTools();
            }
            catch (err) {
                flash(err.message || "Failed to reset conversations", "error");
            }
        });
    }
    if (ui.threadDownload) {
        ui.threadDownload.addEventListener("click", () => {
            window.location.href = resolvePath("/api/app-server/threads/backup");
        });
    }
    if (ui.modelSelect) {
        ui.modelSelect.addEventListener("change", () => {
            updateReasoningOptions(normalizeOptionalString(ui.effortSelect?.value || null), false);
        });
    }
    if (ui.networkToggle) {
        const clearIndeterminate = () => {
            ui.networkToggle.indeterminate = false;
        };
        ui.networkToggle.addEventListener("change", clearIndeterminate);
        ui.networkToggle.addEventListener("click", clearIndeterminate);
    }
    if (ui.saveBtn) {
        ui.saveBtn.addEventListener("click", () => {
            void saveAutorunnerSettings();
        });
    }
    if (ui.reloadBtn) {
        ui.reloadBtn.addEventListener("click", () => {
            void refreshSettings();
        });
    }
    try {
        localStorage.removeItem("logs:tail");
    }
    catch (_err) {
        // ignore
    }
}
function hideRepoSettingsModal() {
    if (repoSettingsCloseModal) {
        const close = repoSettingsCloseModal;
        repoSettingsCloseModal = null;
        close();
    }
}
export function openRepoSettings(triggerEl) {
    const modal = document.getElementById("repo-settings-modal");
    if (!modal)
        return;
    hideRepoSettingsModal();
    repoSettingsCloseModal = openModal(modal, {
        initialFocus: ui.closeBtn || ui.updateBtn || modal,
        returnFocusTo: triggerEl || null,
        onRequestClose: hideRepoSettingsModal,
    });
    void refreshSettings();
    void loadUpdateTargetOptions(ui.updateTarget ? ui.updateTarget.id : null);
}
function initRepoSettingsModal() {
    void loadUpdateTargetOptions(ui.updateTarget ? ui.updateTarget.id : null);
    if (ui.settingsBtn) {
        ui.settingsBtn.addEventListener("click", () => {
            openRepoSettings(ui.settingsBtn);
        });
    }
    if (ui.closeBtn) {
        ui.closeBtn.addEventListener("click", () => {
            hideRepoSettingsModal();
        });
    }
    if (ui.updateBtn) {
        ui.updateBtn.addEventListener("click", () => handleSystemUpdate("repo-update-btn", ui.updateTarget ? ui.updateTarget.id : null));
    }
}
export const __settingsTest = {
    collectAutorunnerSettingsPayload,
    loadAutorunnerSettings,
    refreshSettings,
    reset() {
        currentCatalog = null;
        currentCatalogAgent = "codex";
        settingsBusy = false;
        settingsLoaded = false;
        hideRepoSettingsModal();
    },
    getCurrentCatalogAgent() {
        return currentCatalogAgent;
    },
};
