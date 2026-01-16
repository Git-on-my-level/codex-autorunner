import { api, confirmModal, flash, resolvePath } from "./utils.js";

let modelsCache = [];
let currentSettings = null;

const ui = {
  settingsBtn: document.getElementById("repo-settings"),
  modelSelect: document.getElementById("autorunner-model-select"),
  effortSelect: document.getElementById("autorunner-effort-select"),
  approvalSelect: document.getElementById("autorunner-approval-select"),
  sandboxSelect: document.getElementById("autorunner-sandbox-select"),
  networkToggle: document.getElementById("autorunner-network-toggle"),
  networkRow: document.getElementById("autorunner-network-row"),
  saveBtn: document.getElementById("autorunner-settings-save"),
  reloadBtn: document.getElementById("autorunner-settings-reload"),
  warning: document.getElementById("autorunner-settings-warning"),
  threadList: document.getElementById("thread-tools-list"),
  threadNew: document.getElementById("thread-new-autorunner"),
  threadArchive: document.getElementById("thread-archive-autorunner"),
  threadResetAll: document.getElementById("thread-reset-all"),
  threadDownload: document.getElementById("thread-backup-download"),
};

const DEFAULT_EFFORTS = ["low", "medium", "high"];

function getModelId(model) {
  if (!model || typeof model !== "object") return null;
  const keys = ["id", "model", "name", "model_id", "modelId"];
  for (const key of keys) {
    const value = model[key];
    if (typeof value === "string" && value.trim()) {
      return value.trim();
    }
  }
  return null;
}

function getModelEfforts(model) {
  if (!model || typeof model !== "object") return null;
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
    const value = model[key];
    if (Array.isArray(value) && value.length) {
      return value.map((entry) => String(entry));
    }
  }
  return null;
}

function normalizeModels(raw) {
  if (Array.isArray(raw)) return raw;
  if (raw && Array.isArray(raw.models)) return raw.models;
  if (raw && Array.isArray(raw.data)) return raw.data;
  if (raw && Array.isArray(raw.items)) return raw.items;
  if (raw && Array.isArray(raw.results)) return raw.results;
  return [];
}

function setOptions(select, options, selected, placeholder) {
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

function updateNetworkVisibility() {
  if (!ui.networkRow || !ui.sandboxSelect) return;
  const show = ui.sandboxSelect.value === "workspaceWrite";
  ui.networkRow.classList.toggle("hidden", !show);
}

async function loadModels() {
  try {
    const data = await api("/api/app-server/models");
    modelsCache = normalizeModels(data);
  } catch (err) {
    modelsCache = [];
    flash(err.message || "Failed to load models", "error");
  }
}

async function loadSessionSettings() {
  const data = await api("/api/session/settings");
  currentSettings = data;
  return data;
}

function renderSettings(settings) {
  if (!settings) return;
  const modelOptions = modelsCache
    .map((model) => {
      const id = getModelId(model);
      return id ? { value: id, label: id } : null;
    })
    .filter(Boolean);
  setOptions(
    ui.modelSelect,
    modelOptions,
    settings.autorunner_model_override,
    "Default model"
  );

  const selectedModelId = ui.modelSelect?.value || settings.autorunner_model_override;
  const selectedModel =
    modelsCache.find((model) => getModelId(model) === selectedModelId) || null;
  const efforts = getModelEfforts(selectedModel) || DEFAULT_EFFORTS;
  const effortOptions = efforts.map((effort) => ({
    value: effort,
    label: effort,
  }));
  setOptions(
    ui.effortSelect,
    effortOptions,
    settings.autorunner_effort_override,
    "Default effort"
  );

  setOptions(
    ui.approvalSelect,
    [
      { value: "never", label: "Never" },
      { value: "unlessTrusted", label: "Unless trusted" },
    ],
    settings.autorunner_approval_policy,
    "Default approval"
  );
  setOptions(
    ui.sandboxSelect,
    [
      { value: "dangerFullAccess", label: "Full access" },
      { value: "workspaceWrite", label: "Workspace write" },
    ],
    settings.autorunner_sandbox_mode,
    "Default sandbox"
  );
  if (ui.networkToggle) {
    ui.networkToggle.checked = Boolean(
      settings.autorunner_workspace_write_network
    );
  }
  updateNetworkVisibility();
}

async function saveSettings() {
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
    currentSettings = data;
    flash("Autorunner settings saved", "success");
  } catch (err) {
    flash(err.message || "Failed to save settings", "error");
  } finally {
    ui.saveBtn.disabled = false;
    ui.saveBtn.classList.remove("loading");
  }
}

function renderThreadTools(data) {
  if (!ui.threadList) return;
  ui.threadList.innerHTML = "";
  if (!data) {
    ui.threadList.textContent = "Unable to load thread info.";
    return;
  }
  const entries = [];
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
        value: data.doc_chat[key] || "—",
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

async function loadThreadTools() {
  try {
    const data = await api("/api/app-server/threads");
    renderThreadTools(data);
    return data;
  } catch (err) {
    renderThreadTools(null);
    flash(err.message || "Failed to load threads", "error");
    return null;
  }
}

async function refreshSettings() {
  await loadModels();
  const settings = await loadSessionSettings();
  renderSettings(settings);
  await loadThreadTools();
}

export function initRepoSettingsPanel() {
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
        flash(err.message || "Failed to reset autorunner thread", "error");
      }
    });
  }
  if (ui.threadArchive) {
    ui.threadArchive.addEventListener("click", async () => {
      const data = await api("/api/app-server/threads");
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
        flash(err.message || "Failed to archive thread", "error");
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
        flash(err.message || "Failed to reset conversations", "error");
      }
    });
  }
  if (ui.threadDownload) {
    ui.threadDownload.addEventListener("click", () => {
      window.location.href = resolvePath("/api/app-server/threads/backup");
    });
  }
}
