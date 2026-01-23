import { api, confirmModal, flash, resolvePath } from "./utils.js";

const ui = {
  settingsBtn: document.getElementById("repo-settings"),
  threadList: document.getElementById("thread-tools-list") as HTMLElement | null,
  threadNew: document.getElementById("thread-new-autorunner") as HTMLButtonElement | null,
  threadArchive: document.getElementById("thread-archive-autorunner") as HTMLButtonElement | null,
  threadResetAll: document.getElementById("thread-reset-all") as HTMLButtonElement | null,
  threadDownload: document.getElementById("thread-backup-download") as HTMLAnchorElement | null,
};



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
  await loadThreadTools();
}

export function initRepoSettingsPanel(): void {
  if (ui.settingsBtn) {
    ui.settingsBtn.addEventListener("click", () => {
      refreshSettings();
    });
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

  // Clear cached logs since log loading is no longer available
  try {
    localStorage.removeItem("logs:tail");
  } catch (_err) {
    // ignore
  }
}
