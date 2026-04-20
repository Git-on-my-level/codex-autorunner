import { api, confirmModal, flash, resolvePath, openModal } from "./utils.js";
import {
  initTemplateReposSettings,
  loadTemplateRepos,
} from "./templateReposSettings.js";
import {
  handleSystemUpdate,
  loadUpdateTargetOptions,
} from "./systemUpdateUi.js";

const ui = {
  settingsBtn: document.getElementById("repo-settings") as HTMLButtonElement | null,
  threadList: document.getElementById("thread-tools-list") as HTMLElement | null,
  threadNew: document.getElementById(
    "thread-new-autorunner"
  ) as HTMLButtonElement | null,
  threadArchive: document.getElementById(
    "thread-archive-autorunner"
  ) as HTMLButtonElement | null,
  threadResetAll: document.getElementById(
    "thread-reset-all"
  ) as HTMLButtonElement | null,
  threadDownload: document.getElementById(
    "thread-backup-download"
  ) as HTMLAnchorElement | null,
  updateTarget: document.getElementById(
    "repo-update-target"
  ) as HTMLSelectElement | null,
  updateBtn: document.getElementById(
    "repo-update-btn"
  ) as HTMLButtonElement | null,
  closeBtn: document.getElementById("repo-settings-close") as HTMLButtonElement | null,
  modelSelect: document.getElementById(
    "autorunner-model-select"
  ) as HTMLSelectElement | null,
  effortSelect: document.getElementById(
    "autorunner-effort-select"
  ) as HTMLSelectElement | null,
  approvalSelect: document.getElementById(
    "autorunner-approval-select"
  ) as HTMLSelectElement | null,
  sandboxSelect: document.getElementById(
    "autorunner-sandbox-select"
  ) as HTMLSelectElement | null,
  maxRunsInput: document.getElementById(
    "autorunner-max-runs-input"
  ) as HTMLInputElement | null,
  networkToggle: document.getElementById(
    "autorunner-network-toggle"
  ) as HTMLInputElement | null,
  saveBtn: document.getElementById(
    "autorunner-settings-save"
  ) as HTMLButtonElement | null,
  reloadBtn: document.getElementById(
    "autorunner-settings-reload"
  ) as HTMLButtonElement | null,
};

interface ThreadToolData {
  autorunner?: string | number;
  file_chat?: string | number;
  file_chat_opencode?: string | number;
  corruption?: Record<string, unknown>;
  [key: string]: unknown;
}

interface SessionSettingsResponse {
  autorunner_model_override?: string | null;
  autorunner_effort_override?: string | null;
  autorunner_approval_policy?: string | null;
  autorunner_sandbox_mode?: string | null;
  autorunner_workspace_write_network?: boolean | null;
  runner_stop_after_runs?: number | null;
}

interface SessionSettingsRequest {
  autorunner_model_override: string | null;
  autorunner_effort_override: string | null;
  autorunner_approval_policy: string | null;
  autorunner_sandbox_mode: string | null;
  autorunner_workspace_write_network: boolean | null;
  runner_stop_after_runs: number | null;
}

interface AgentEntry {
  id?: string;
  capabilities?: string[];
}

interface AgentListResponse {
  agents?: AgentEntry[];
  default?: string;
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

interface SelectOption {
  value: string;
  label: string;
}

const DEFAULT_OPTION_LABEL = "Default (inherit config)";
const APPROVAL_OPTIONS: SelectOption[] = [
  { value: "", label: DEFAULT_OPTION_LABEL },
  { value: "never", label: "never" },
  { value: "unlessTrusted", label: "unlessTrusted" },
];
const SANDBOX_OPTIONS: SelectOption[] = [
  { value: "", label: DEFAULT_OPTION_LABEL },
  { value: "dangerFullAccess", label: "dangerFullAccess" },
  { value: "workspaceWrite", label: "workspaceWrite" },
];

let repoSettingsCloseModal: (() => void) | null = null;
let currentCatalog: ModelCatalog | null = null;
let currentCatalogAgent = "codex";
let settingsBusy = false;
let settingsLoaded = false;

function normalizeOptionalString(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const cleaned = value.trim();
  return cleaned || null;
}

function normalizeOptionalBoolean(value: unknown): boolean | null {
  if (typeof value !== "boolean") return null;
  return value;
}

function normalizeOptionalInteger(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isInteger(value) || value <= 0) {
    return null;
  }
  return value;
}

/** Whole positive decimal integer string only (avoids parseInt silently truncating "1.5", "1e3", etc.). */
function parsePositiveIntegerRuns(raw: string): number | null | undefined {
  const trimmed = raw.trim();
  if (!trimmed) return null;
  if (!/^\d+$/.test(trimmed)) return undefined;
  const n = Number.parseInt(trimmed, 10);
  if (!Number.isFinite(n) || n <= 0) return undefined;
  return n;
}

function normalizeCatalog(raw: unknown): ModelCatalog {
  if (!raw || typeof raw !== "object") {
    return { default_model: "", models: [] };
  }
  const rawObj = raw as Record<string, unknown>;
  const models = Array.isArray(rawObj.models) ? rawObj.models : [];
  const normalized = models
    .map((entry): ModelCatalogModel | null => {
      if (!entry || typeof entry !== "object") return null;
      const entryObj = entry as Record<string, unknown>;
      const id = normalizeOptionalString(entryObj.id);
      if (!id) return null;
      const reasoningOptions = Array.isArray(entryObj.reasoning_options)
        ? entryObj.reasoning_options.filter(
            (option): option is string =>
              typeof option === "string" && option.trim().length > 0
          )
        : [];
      return {
        id,
        display_name: normalizeOptionalString(entryObj.display_name) || id,
        supports_reasoning: Boolean(entryObj.supports_reasoning),
        reasoning_options: reasoningOptions,
      };
    })
    .filter((model): model is ModelCatalogModel => model !== null);
  return {
    default_model: normalizeOptionalString(rawObj.default_model) || "",
    models: normalized,
  };
}

function renderThreadTools(data: ThreadToolData | null): void {
  if (!ui.threadList) return;
  ui.threadList.innerHTML = "";
  if (!data) {
    ui.threadList.textContent = "Unable to load thread info.";
    return;
  }
  const entries: { label: string; value: string | number }[] = [];
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
    if (
      ["autorunner", "file_chat", "file_chat_opencode", "corruption"].includes(
        key
      )
    ) {
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

async function loadThreadTools(): Promise<ThreadToolData | null> {
  try {
    const data = (await api("/api/app-server/threads")) as ThreadToolData;
    renderThreadTools(data);
    return data;
  } catch (err) {
    renderThreadTools(null);
    flash((err as Error).message || "Failed to load threads", "error");
    return null;
  }
}

function setSelectOptions(
  select: HTMLSelectElement | null,
  options: SelectOption[],
  selectedValue: string | null,
  unknownLabel: string,
  preserveUnknown: boolean = true
): void {
  if (!select) return;
  const normalizedSelected = normalizeOptionalString(selectedValue) || "";
  const rendered = [...options];
  if (
    preserveUnknown &&
    normalizedSelected &&
    !rendered.some((option) => option.value === normalizedSelected)
  ) {
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

function modelLabel(model: ModelCatalogModel): string {
  return model.display_name && model.display_name !== model.id
    ? `${model.display_name} (${model.id})`
    : model.id;
}

function currentEffectiveModelId(): string | null {
  const selectedModel = normalizeOptionalString(ui.modelSelect?.value || null);
  if (selectedModel) return selectedModel;
  if (
    currentCatalog?.default_model &&
    currentCatalog.models.some((model) => model.id === currentCatalog.default_model)
  ) {
    return currentCatalog.default_model;
  }
  return currentCatalog?.models[0]?.id || null;
}

function currentEffectiveModel(): ModelCatalogModel | null {
  const modelId = currentEffectiveModelId();
  if (!modelId || !currentCatalog) return null;
  return currentCatalog.models.find((model) => model.id === modelId) || null;
}

function updateReasoningOptions(
  selectedValue: string | null,
  preserveUnknown: boolean = true
): void {
  const model = currentEffectiveModel();
  const options: SelectOption[] = [{ value: "", label: DEFAULT_OPTION_LABEL }];
  if (model?.supports_reasoning) {
    model.reasoning_options.forEach((optionValue) => {
      options.push({ value: optionValue, label: optionValue });
    });
  }
  setSelectOptions(
    ui.effortSelect,
    options,
    selectedValue,
    "current override",
    preserveUnknown
  );
}

function renderAutorunnerSettings(data: SessionSettingsResponse): void {
  const modelOptions: SelectOption[] = [{ value: "", label: DEFAULT_OPTION_LABEL }];
  currentCatalog?.models.forEach((model) => {
    modelOptions.push({ value: model.id, label: modelLabel(model) });
  });
  setSelectOptions(
    ui.modelSelect,
    modelOptions,
    normalizeOptionalString(data.autorunner_model_override),
    "current override"
  );
  updateReasoningOptions(
    normalizeOptionalString(data.autorunner_effort_override),
    true
  );
  setSelectOptions(
    ui.approvalSelect,
    APPROVAL_OPTIONS,
    normalizeOptionalString(data.autorunner_approval_policy),
    "current override"
  );
  setSelectOptions(
    ui.sandboxSelect,
    SANDBOX_OPTIONS,
    normalizeOptionalString(data.autorunner_sandbox_mode),
    "current override"
  );
  if (ui.maxRunsInput) {
    const maxRuns = normalizeOptionalInteger(data.runner_stop_after_runs);
    ui.maxRunsInput.value = maxRuns ? String(maxRuns) : "";
  }
  if (ui.networkToggle) {
    const networkSetting = normalizeOptionalBoolean(
      data.autorunner_workspace_write_network
    );
    ui.networkToggle.checked = networkSetting === true;
    ui.networkToggle.indeterminate = networkSetting === null;
  }
  updateAutorunnerFormInteractivity();
}

async function resolveCatalogAgent(): Promise<string> {
  try {
    const data = (await api("/api/agents", {
      method: "GET",
    })) as AgentListResponse;
    const agents = Array.isArray(data.agents) ? data.agents : [];
    const supportsListing = (agent: AgentEntry | undefined): boolean =>
      Array.isArray(agent?.capabilities) &&
      agent.capabilities.includes("model_listing");
    const defaultAgentId =
      normalizeOptionalString(data.default) || "codex";
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
  } catch (_err) {
    return "codex";
  }
}

async function loadCatalog(agentId: string): Promise<ModelCatalog | null> {
  try {
    const data = await api(`/api/agents/${encodeURIComponent(agentId)}/models`, {
      method: "GET",
    });
    return normalizeCatalog(data);
  } catch (_err) {
    return null;
  }
}

function setAutorunnerBusy(busy: boolean): void {
  settingsBusy = busy;
  updateAutorunnerFormInteractivity();
}

function updateAutorunnerFormInteractivity(): void {
  const formDisabled = settingsBusy || !settingsLoaded;
  const applySelectState = (select: HTMLSelectElement | null): void => {
    if (!select) return;
    select.disabled =
      formDisabled || select.dataset.optionAvailable === "0";
  };

  applySelectState(ui.modelSelect);
  applySelectState(ui.effortSelect);
  applySelectState(ui.approvalSelect);
  applySelectState(ui.sandboxSelect);
  if (ui.maxRunsInput) ui.maxRunsInput.disabled = formDisabled;
  if (ui.networkToggle) ui.networkToggle.disabled = formDisabled;
  if (ui.saveBtn) ui.saveBtn.disabled = settingsBusy || !settingsLoaded;
  if (ui.reloadBtn) ui.reloadBtn.disabled = settingsBusy;
}

async function loadAutorunnerSettings(): Promise<void> {
  settingsLoaded = false;
  setAutorunnerBusy(true);
  try {
    const [settingsPayload, agentId] = await Promise.all([
      api("/api/session/settings", { method: "GET" }) as Promise<SessionSettingsResponse>,
      resolveCatalogAgent(),
    ]);
    currentCatalogAgent = agentId;
    currentCatalog = await loadCatalog(agentId);
    settingsLoaded = true;
    renderAutorunnerSettings(settingsPayload);
  } catch (err) {
    currentCatalog = null;
    settingsLoaded = false;
    renderAutorunnerSettings({});
    flash(
      (err as Error).message || "Failed to load autorunner settings",
      "error"
    );
  } finally {
    setAutorunnerBusy(false);
  }
}

function collectAutorunnerSettingsPayload(): SessionSettingsRequest {
  const maxRunsRaw = ui.maxRunsInput?.value ?? "";
  const runs = parsePositiveIntegerRuns(maxRunsRaw);
  return {
    autorunner_model_override: normalizeOptionalString(ui.modelSelect?.value || null),
    autorunner_effort_override: normalizeOptionalString(
      ui.effortSelect?.value || null
    ),
    autorunner_approval_policy: normalizeOptionalString(
      ui.approvalSelect?.value || null
    ),
    autorunner_sandbox_mode: normalizeOptionalString(
      ui.sandboxSelect?.value || null
    ),
    autorunner_workspace_write_network:
      ui.networkToggle && !ui.networkToggle.indeterminate
        ? ui.networkToggle.checked
        : null,
    runner_stop_after_runs: runs === undefined ? null : runs,
  };
}

async function saveAutorunnerSettings(): Promise<void> {
  if (settingsBusy || !settingsLoaded) return;
  const maxRunsRaw = ui.maxRunsInput?.value ?? "";
  if (parsePositiveIntegerRuns(maxRunsRaw) === undefined) {
    flash(
      "Stop after runs must be a positive whole number, or leave blank for no limit",
      "error"
    );
    return;
  }
  setAutorunnerBusy(true);
  try {
    const payload = collectAutorunnerSettingsPayload();
    await api("/api/session/settings", {
      method: "POST",
      body: payload,
    });
    flash("Autorunner settings saved", "success");
    await refreshSettings();
  } catch (err) {
    flash(
      (err as Error).message || "Failed to save autorunner settings",
      "error"
    );
  } finally {
    setAutorunnerBusy(false);
  }
}

async function refreshSettings(): Promise<void> {
  await Promise.all([
    loadThreadTools(),
    loadTemplateRepos(),
    loadAutorunnerSettings(),
  ]);
}

export function initRepoSettingsPanel(): void {
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
      } catch (err) {
        flash(
          (err as Error).message || "Failed to reset autorunner thread",
          "error"
        );
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
      const confirmed = await confirmModal(
        "Archive autorunner thread? This starts a new conversation."
      );
      if (!confirmed) return;
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
      } catch (err) {
        flash((err as Error).message || "Failed to archive thread", "error");
      }
    });
  }
  if (ui.threadResetAll) {
    ui.threadResetAll.addEventListener("click", async () => {
      const confirmed = await confirmModal(
        "Reset all conversations? This clears all saved app-server threads.",
        { confirmText: "Reset all", danger: true }
      );
      if (!confirmed) return;
      try {
        await api("/api/app-server/threads/reset-all", { method: "POST" });
        flash("Conversations reset", "success");
        await loadThreadTools();
      } catch (err) {
        flash(
          (err as Error).message || "Failed to reset conversations",
          "error"
        );
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
      updateReasoningOptions(
        normalizeOptionalString(ui.effortSelect?.value || null),
        false
      );
    });
  }
  if (ui.networkToggle) {
    const clearIndeterminate = () => {
      ui.networkToggle!.indeterminate = false;
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
  } catch (_err) {
    // ignore
  }
}

function hideRepoSettingsModal(): void {
  if (repoSettingsCloseModal) {
    const close = repoSettingsCloseModal;
    repoSettingsCloseModal = null;
    close();
  }
}

export function openRepoSettings(triggerEl?: HTMLElement | null): void {
  const modal = document.getElementById("repo-settings-modal");
  if (!modal) return;

  hideRepoSettingsModal();
  repoSettingsCloseModal = openModal(modal, {
    initialFocus: ui.closeBtn || ui.updateBtn || modal,
    returnFocusTo: triggerEl || null,
    onRequestClose: hideRepoSettingsModal,
  });
  void refreshSettings();
  void loadUpdateTargetOptions(ui.updateTarget ? ui.updateTarget.id : null);
}

function initRepoSettingsModal(): void {
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
    ui.updateBtn.addEventListener("click", () =>
      handleSystemUpdate(
        "repo-update-btn",
        ui.updateTarget ? ui.updateTarget.id : null
      )
    );
  }
}

export const __settingsTest = {
  collectAutorunnerSettingsPayload,
  parsePositiveIntegerRuns,
  loadAutorunnerSettings,
  refreshSettings,
  reset(): void {
    currentCatalog = null;
    currentCatalogAgent = "codex";
    settingsBusy = false;
    settingsLoaded = false;
    hideRepoSettingsModal();
  },
  getCurrentCatalogAgent(): string {
    return currentCatalogAgent;
  },
};
