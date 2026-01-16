import { api, flash, resolvePath } from "./utils.js";
import { threadRegistryUI } from "./docsElements.js";

export function renderThreadRegistryBanner(notice) {
  if (!threadRegistryUI.banner) return;
  const active = notice && notice.status === "corrupt";
  threadRegistryUI.banner.classList.toggle("hidden", !active);
  if (!active) return;
  const backupPath =
    notice && typeof notice.backup_path === "string" ? notice.backup_path : "";
  if (threadRegistryUI.detail) {
    threadRegistryUI.detail.textContent = backupPath
      ? `Backup: ${backupPath}`
      : "Backup unavailable";
    threadRegistryUI.detail.title = backupPath || "";
  }
  if (threadRegistryUI.download) {
    threadRegistryUI.download.classList.toggle("hidden", !backupPath);
  }
}

export async function loadThreadRegistryStatus() {
  if (!threadRegistryUI.banner) return;
  try {
    const data = await api("/api/app-server/threads");
    renderThreadRegistryBanner(data?.corruption);
  } catch (err) {
    console.error("Failed to load thread registry status", err);
  }
}

export async function resetThreadRegistry() {
  try {
    await api("/api/app-server/threads/reset-all", { method: "POST" });
    renderThreadRegistryBanner(null);
    flash("Conversations reset");
  } catch (err) {
    flash(err.message || "Failed to reset conversations", "error");
  }
}

export function downloadThreadRegistryBackup() {
  window.location.href = resolvePath("/api/app-server/threads/backup");
}
