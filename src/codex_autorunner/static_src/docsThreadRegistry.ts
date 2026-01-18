import { api, flash, resolvePath } from "./utils.js";
import { threadRegistryUI } from "./docsElements.js";

interface CorruptionNotice {
  status?: string;
  backup_path?: string;
}

export function renderThreadRegistryBanner(notice: CorruptionNotice | null): void {
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

export async function loadThreadRegistryStatus(): Promise<void> {
  if (!threadRegistryUI.banner) return;
  try {
    const data = await api("/api/app-server/threads");
    renderThreadRegistryBanner((data as { corruption?: CorruptionNotice })?.corruption);
  } catch (err) {
    console.error("Failed to load thread registry status", err);
  }
}

export async function resetThreadRegistry(): Promise<void> {
  try {
    await api("/api/app-server/threads/reset-all", { method: "POST" });
    renderThreadRegistryBanner(null);
    flash("Conversations reset");
  } catch (err) {
    flash((err as Error).message || "Failed to reset conversations", "error");
  }
}

export function downloadThreadRegistryBackup(): void {
  window.location.href = resolvePath("/api/app-server/threads/backup");
}
