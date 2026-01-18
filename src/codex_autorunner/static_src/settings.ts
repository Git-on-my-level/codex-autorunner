import { api, confirmModal, flash, resolvePath } from "./utils.js";

let modelsCache: unknown[] = [];
let currentSettings: Record<string, unknown> | null = null;

const ui = {
  settingsBtn: document.getElementById("repo-settings"),
  modelSelect: document.getElementById("autorunner-model-select") as HTMLSelectElement | null,
  effortSelect: document.getElementById("autorunner-effort-select") as HTMLSelectElement | null,
  approvalSelect: document.getElementById("autorunner-approval-select") as HTMLSelectElement | null,
  sandboxSelect: document.getElementById("autorunner-sandbox-select") as HTMLSelectElement | null,
  networkToggle: document.getElementById("autorunner-network-toggle") as HTMLInputElement | null,
  networkRow: document.getElementById("autorunner-network-row") as HTMLElement | null,
  saveBtn: document.getElementById("autorunner-settings-save") as HTMLButtonElement | null,
  reloadBtn: document.getElementById("autorunner-settings-reload") as HTMLButtonElement | null,
  warning: document.getElementById("autorunner-settings-warning") as HTMLElement | null,
  threadList: document.getElementById("thread-tools-list") as HTMLElement | null,
  threadNew: document.getElementById("thread-new-autorunner") as HTMLButtonElement | null,
  threadArchive: document.getElementById("thread-archive-autorunner") as HTMLButtonElement | null,
  threadResetAll: document.getElementById("thread-reset-all") as HTMLButtonElement | null,
  threadDownload: document.getElementById("thread-backup-download") as HTMLAnchorElement | null,
};

const DEFAULT_EFFORTS = ["low", "medium", "high"] as const;

function getModelId(model: unknown): string | null {
  if (!model || typeof model !== "object") return null;
  const modelObj = model as Record<string, unknown>;
  const keys = ["id", "model", "name", "model_id", "modelId"];
  for (const key of keys) {
    const value = modelObj[key];
    if (typeof value === "string" && value.trim()) {
      return value.trim();
    }
  }
  return null;
}

function getModelEfforts(model: unknown): string[] | null {
  if (!model || typeof model !== "object") return null;
  const modelObj = model as Record<string, unknown>;
  const keys = [
    "supported_reasoning_efforts",
    "supportedReasoningEfforts",
    "reasoning_efforts",
    "reasoningEfforts",
    "supported_efforts",
    "supportedEfforts",
    "efforts",
  ];
  for (const key of keys) {
    const value = modelObj[key];
    if (Array.isArray(value) && value.length) {
      return value.map((entry) => String(entry));
    }
  }
  return null;
}

function normalizeModels(raw: unknown): unknown[] {
  if (Array.isArray(raw)) return raw;
  if (raw && typeof raw === "object") {
    const rawObj = raw as Record<string, unknown>;
    if (Array.isArray(rawObj.models)) return rawObj.models;
    if (Array.isArray(rawObj.data)) return rawObj.data;
    if (Array.isArray(rawObj.items)) return rawObj.items;
    if (Array.isArray(rawObj.results)) return rawObj.results;
  }
  return [];
}

interface SelectOption {
  value: string;
  label: string;
}

function setOptions(
  select: HTMLSelectElement | null,
  options: SelectOption[],
  selected: string | null | undefined,
  placeholder: string,
): void {
  if (!select) return;
  select.innerHTML = "";
  const empty = document.createElement("option");
  empty.value = "";
  empty.textContent = placeholder;
  select.appendChild(empty);
  options.forEach((opt) => {
    const option = document.createElement("option");
    option.value = opt.value;
    option.textContent = opt.label;
    select.appendChild(option);
  });
  if (selected) {
    const exists = options.some((opt) => opt.value === selected);
    if (!exists) {
      const custom = document.createElement("option");
      custom.value = selected;
      custom.textContent = `${selected} (custom)`;
      select.appendChild(custom);
    }
    select.value = selected;
  } else {
    select.value = "";
  }
}

function updateNetworkVisibility(): void {
  if (!ui.networkRow || !ui.sandboxSelect) return;
  const show = ui.sandboxSelect.value === "workspaceWrite";
  ui.networkRow.classList.toggle("hidden", !show);
}

async function loadModels(): Promise<void> {
  try {
    const data = await api("/api/app-server/models");
    modelsCache = normalizeModels(data);
  } catch (err) {
    modelsCache = [];
    const error = err as Error;
    flash(error.message || "Failed to load models", "error");
  }
}

async function loadSessionSettings(): Promise<Record<string, unknown>> {
  const data = await api("/api/session/settings");
  currentSettings = data as Record<string, unknown>;
  return data as Record<string, unknown>;
}

function renderSettings(settings: Record<string, unknown> | null): void {
  if (!settings) return;
  const modelOptions = modelsCache
    .map((model) => {
      const id = getModelId(model);
      return id ? { value: id, label: id } : null;
    })
    .filter((opt): opt is SelectOption => opt !== null);
  setOptions(
    ui.modelSelect,
    modelOptions,
    settings.autorunner_model_override as string | null | undefined,
    "Default model"
  );

  const selectedModelId = ui.modelSelect?.value || (settings.autorunner_model_override as string | null | undefined);
  const selectedModel =
    modelsCache.find((model) => getModelId(model) === selectedModelId) || null;
  const efforts = getModelEfforts(selectedModel) || [...DEFAULT_EFFORTS];
  const effortOptions = efforts.map((effort) => ({
    value: effort,
    label: effort,
  }));
  setOptions(
    ui.effortSelect,
    effortOptions,
    settings.autorunner_effort_override as string | null | undefined,
    "Default effort"
  );

  setOptions(
    ui.approvalSelect,
    [
      { value: "never", label: "Never" },
      { value: "unlessTrusted", label: "Unless trusted" },
    ],
    settings.autorunner_approval_policy as string | null | undefined,
    "Default approval"
  );
  setOptions(
    ui.sandboxSelect,
    [
      { value: "dangerFullAccess", label: "Full access" },
      { value: "workspaceWrite", label: "Workspace write" },
    ],
    settings.autorunner_sandbox_mode as string | null | undefined,
    "Default sandbox"
  );
  if (ui.networkToggle) {
    ui.networkToggle.checked = Boolean(
      settings.autorunner_workspace_write_network
    );
  }
  updateNetworkVisibility();
}

async function saveSettings(): Promise<void> {
  if (!ui.saveBtn) return;
  ui.saveBtn.disabled = true;
  ui.saveBtn.classList.add("loading");
  try {
    const payload = {
      autorunner_model_override: ui.modelSelect?.value || null,
      autorunner_effort_override: ui.effortSelect?.value || null,
      autorunner_approval_policy: ui.approvalSelect?.value || null,
      autorunner_sandbox_mode: ui.sandboxSelect?.value || null,
      autorunner_workspace_write_network: Boolean(
        ui.networkToggle?.checked
      ),
    };
    const data = await api("/api/session/settings", {
      method: "POST",
      body: payload,
    });
    currentSettings = data as Record<string, unknown>;
    flash("Autorunner settings saved", "success");
  } catch (err) {
    const error = err as Error;
    flash(error.message || "Failed to save settings", "error");
  } finally {
    ui.saveBtn.disabled = false;
    ui.saveBtn.classList.remove("loading");
  }
}

interface ThreadToolData {
  autorunner?: string | number;
  spec_ingest?: string | number;
  doc_chat?: Record<string, string | number>;
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
  if (data.spec_ingest !== undefined) {
    entries.push({ label: "Spec ingest", value: data.spec_ingest || "—" });
  }
  if (data.doc_chat && typeof data.doc_chat === "object") {
    Object.keys(data.doc_chat).forEach((key) => {
      entries.push({
        label: `Doc chat (${key})`,
        value: data.doc_chat![key] || "—",
      });
    });
  }
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
    ui.threadList.appendChild(row);
  });
  if (ui.threadArchive) {
    ui.threadArchive.disabled = !data.autorunner;
  }
}

async function loadThreadTools(): Promise<ThreadToolData | null> {
  try {
    const data = await api("/api/app-server/threads");
    renderThreadTools(data as ThreadToolData);
    return data as ThreadToolData;
  } catch (err) {
    renderThreadTools(null);
    const error = err as Error;
    flash(error.message || "Failed to load threads", "error");
    return null;
  }
}

async function refreshSettings(): Promise<void> {
  await loadModels();
  const settings = await loadSessionSettings();
  renderSettings(settings);
  await loadThreadTools();
}

export function initRepoSettingsPanel(): void {
  if (ui.settingsBtn) {
    ui.settingsBtn.addEventListener("click", () => {
      refreshSettings();
    });
  }
  if (ui.modelSelect) {
    ui.modelSelect.addEventListener("change", () => {
      if (!currentSettings) return;
      const currentEffort = ui.effortSelect?.value || null;
      const updated = {
        ...currentSettings,
        autorunner_model_override: ui.modelSelect.value || null,
        autorunner_effort_override:
          currentEffort || currentSettings.autorunner_effort_override,
      };
      renderSettings(updated);
    });
  }
  if (ui.sandboxSelect) {
    ui.sandboxSelect.addEventListener("change", updateNetworkVisibility);
  }
  if (ui.saveBtn) {
    ui.saveBtn.addEventListener("click", () => saveSettings());
  }
  if (ui.reloadBtn) {
    ui.reloadBtn.addEventListener("click", () => refreshSettings());
  }
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
        const error = err as Error;
        flash(error.message || "Failed to reset autorunner thread", "error");
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
        const error = err as Error;
        flash(error.message || "Failed to archive thread", "error");
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
        const error = err as Error;
        flash(error.message || "Failed to reset conversations", "error");
      }
    });
  }
  if (ui.threadDownload) {
    ui.threadDownload.addEventListener("click", () => {
      window.location.href = resolvePath("/api/app-server/threads/backup");
    });
  }
}
