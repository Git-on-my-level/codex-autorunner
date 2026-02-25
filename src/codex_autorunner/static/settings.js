// GENERATED FILE - do not edit directly. Source: static_src/
import { api, confirmModal, flash, resolvePath, openModal } from "./utils.js";
import { initTemplateReposSettings, loadTemplateRepos } from "./templateReposSettings.js";
const ui = {
    settingsBtn: document.getElementById("repo-settings"),
    threadList: document.getElementById("thread-tools-list"),
    threadNew: document.getElementById("thread-new-autorunner"),
    threadArchive: document.getElementById("thread-archive-autorunner"),
    threadResetAll: document.getElementById("thread-reset-all"),
    threadDownload: document.getElementById("thread-backup-download"),
};
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
    // Render any additional string/number keys to avoid hiding future entries.
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
    }
    catch (err) {
        renderThreadTools(null);
        const error = err;
        flash(error.message || "Failed to load threads", "error");
        return null;
    }
}
async function refreshSettings() {
    await loadThreadTools();
    await loadTemplateRepos();
}
export function initRepoSettingsPanel() {
    window.__CAR_SETTINGS = { loadThreadTools, refreshSettings };
    // Initialize the modal interaction
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
                const error = err;
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
                const error = err;
                flash(error.message || "Failed to archive thread", "error");
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
                const error = err;
                flash(error.message || "Failed to reset conversations", "error");
            }
        });
    }
    if (ui.threadDownload) {
        ui.threadDownload.addEventListener("click", () => {
            window.location.href = resolvePath("/api/app-server/threads/backup");
        });
    }
    // Clear cached logs since log loading is no longer available
    try {
        localStorage.removeItem("logs:tail");
    }
    catch (_err) {
        // ignore
    }
}
const UPDATE_TARGET_LABELS = {
    both: "Web + Chat Apps",
    web: "web only",
    chat: "Chat Apps (Telegram + Discord)",
    telegram: "Telegram only",
    discord: "Discord only",
};
function normalizeUpdateTarget(value) {
    if (!value)
        return "both";
    if (value === "both" ||
        value === "web" ||
        value === "chat" ||
        value === "telegram" ||
        value === "discord") {
        return value;
    }
    return "both";
}
function getUpdateTarget(selectId) {
    const select = selectId ? document.getElementById(selectId) : null;
    return normalizeUpdateTarget(select ? select.value : "both");
}
function describeUpdateTarget(target) {
    return UPDATE_TARGET_LABELS[target] || UPDATE_TARGET_LABELS.both;
}
function includesWebUpdateTarget(target) {
    return target === "both" || target === "web";
}
function updateRestartNotice(target) {
    if (target === "chat")
        return "Telegram and Discord bots will restart.";
    if (target === "telegram")
        return "The Telegram bot will restart.";
    if (target === "discord")
        return "The Discord bot will restart.";
    return "The service will restart.";
}
async function loadUpdateTargetOptions(selectId) {
    const select = selectId ? document.getElementById(selectId) : null;
    if (!select)
        return;
    let payload = null;
    try {
        payload = await api("/system/update/targets", { method: "GET" });
    }
    catch (_err) {
        return;
    }
    const rawOptions = Array.isArray(payload?.targets) ? payload.targets : [];
    const options = [];
    const seen = new Set();
    rawOptions.forEach((entry) => {
        const rawValue = typeof entry?.value === "string" ? entry.value : "";
        if (!["both", "web", "chat", "telegram", "discord"].includes(rawValue))
            return;
        if (!rawValue)
            return;
        const value = normalizeUpdateTarget(rawValue);
        if (seen.has(value))
            return;
        seen.add(value);
        const label = typeof entry?.label === "string" && entry.label.trim()
            ? entry.label.trim()
            : describeUpdateTarget(value);
        options.push({ value, label });
    });
    if (!options.length)
        return;
    const previous = normalizeUpdateTarget(select.value || "both");
    const hasPrevious = options.some((item) => item.value === previous);
    const defaultTarget = normalizeUpdateTarget(payload?.default_target || "both");
    const fallback = options.some((item) => item.value === defaultTarget)
        ? defaultTarget
        : options[0].value;
    select.replaceChildren();
    options.forEach((item) => {
        const option = document.createElement("option");
        option.value = item.value;
        option.textContent = item.label;
        select.appendChild(option);
    });
    select.value = hasPrevious ? previous : fallback;
}
async function handleSystemUpdate(btnId, targetSelectId) {
    const btn = document.getElementById(btnId);
    if (!btn)
        return;
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Checking...";
    const updateTarget = getUpdateTarget(targetSelectId);
    const targetLabel = describeUpdateTarget(updateTarget);
    let check;
    try {
        check = await api("/system/update/check");
    }
    catch (err) {
        check = { update_available: true, message: err.message || "Unable to check for updates." };
    }
    if (!check?.update_available) {
        flash(check?.message || "No update available.", "info");
        btn.disabled = false;
        btn.textContent = originalText;
        return;
    }
    const restartNotice = updateRestartNotice(updateTarget);
    const confirmed = await confirmModal(`${check?.message || "Update available."} Update Codex Autorunner (${targetLabel})? ${restartNotice}`);
    if (!confirmed) {
        btn.disabled = false;
        btn.textContent = originalText;
        return;
    }
    btn.textContent = "Updating...";
    try {
        const res = await api("/system/update", {
            method: "POST",
            body: { target: updateTarget },
        });
        flash(res.message || `Update started (${targetLabel}).`, "success");
        if (!includesWebUpdateTarget(updateTarget)) {
            btn.disabled = false;
            btn.textContent = originalText;
            return;
        }
        document.body.style.pointerEvents = "none";
        setTimeout(() => {
            const url = new URL(window.location.href);
            url.searchParams.set("v", String(Date.now()));
            window.location.replace(url.toString());
        }, 8000);
    }
    catch (err) {
        flash(err.message || "Update failed", "error");
        btn.disabled = false;
        btn.textContent = originalText;
    }
}
let repoSettingsCloseModal = null;
function hideRepoSettingsModal() {
    if (repoSettingsCloseModal) {
        const close = repoSettingsCloseModal;
        repoSettingsCloseModal = null;
        close();
    }
}
export function openRepoSettings(triggerEl) {
    const modal = document.getElementById("repo-settings-modal");
    const closeBtn = document.getElementById("repo-settings-close");
    const updateBtn = document.getElementById("repo-update-btn");
    const updateTarget = document.getElementById("repo-update-target");
    if (!modal)
        return;
    hideRepoSettingsModal();
    repoSettingsCloseModal = openModal(modal, {
        initialFocus: closeBtn || updateBtn || modal,
        returnFocusTo: triggerEl || null,
        onRequestClose: hideRepoSettingsModal,
    });
    // Trigger settings refresh when modal opens
    const { refreshSettings } = window.__CAR_SETTINGS || {};
    if (typeof refreshSettings === "function") {
        refreshSettings();
    }
    void loadUpdateTargetOptions(updateTarget ? updateTarget.id : null);
}
function initRepoSettingsModal() {
    const settingsBtn = document.getElementById("repo-settings");
    const closeBtn = document.getElementById("repo-settings-close");
    const updateBtn = document.getElementById("repo-update-btn");
    const updateTarget = document.getElementById("repo-update-target");
    void loadUpdateTargetOptions(updateTarget ? updateTarget.id : null);
    // If the gear button exists in HTML, wire it up (backwards compatibility)
    if (settingsBtn) {
        settingsBtn.addEventListener("click", () => {
            openRepoSettings(settingsBtn);
        });
    }
    if (closeBtn) {
        closeBtn.addEventListener("click", () => {
            hideRepoSettingsModal();
        });
    }
    if (updateBtn) {
        updateBtn.addEventListener("click", () => handleSystemUpdate("repo-update-btn", updateTarget ? updateTarget.id : null));
    }
}
