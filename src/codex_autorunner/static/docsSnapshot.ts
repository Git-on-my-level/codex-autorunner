import { api, flash } from "./utils.js";
import { snapshotUI } from "./docsElements.js";
import { docsState, getActiveDoc } from "./docsState.js";
import { getDocTextarea, updateCopyButton, getDocCopyText } from "./docsUi.js";

export function setSnapshotBusy(on: boolean): void {
  docsState.snapshotBusy = on;
  const disabled = !!on;
  for (const btn of [
    snapshotUI.generate,
    snapshotUI.update,
    snapshotUI.regenerate,
    snapshotUI.refresh,
  ]) {
    if (btn) btn.disabled = disabled;
  }
  updateCopyButton(snapshotUI.copy, getDocCopyText("snapshot"), disabled);
  const statusEl = document.getElementById("doc-status");
  if (statusEl && getActiveDoc() === "snapshot") {
    statusEl.textContent = on ? "Working…" : "Viewing SNAPSHOT";
  }
}

export function renderSnapshotButtons(): void {
  if (snapshotUI.generate) snapshotUI.generate.classList.toggle("hidden", false);
  if (snapshotUI.update) snapshotUI.update.classList.toggle("hidden", true);
  if (snapshotUI.regenerate)
    snapshotUI.regenerate.classList.toggle("hidden", true);
  updateCopyButton(snapshotUI.copy, getDocCopyText("snapshot"), docsState.snapshotBusy);
}

interface LoadSnapshotOptions {
  notify?: boolean;
}

export async function loadSnapshot({ notify = false }: LoadSnapshotOptions = {}): Promise<void> {
  if (docsState.snapshotBusy) return;
  try {
    setSnapshotBusy(true);
    const data = await api("/api/snapshot");
    docsState.snapshotCache = {
      exists: !!(data as { exists?: boolean })?.exists,
      content: (data as { content?: string })?.content || "",
      state: (data as { state?: Record<string, unknown> })?.state || {},
    };
    if (getActiveDoc() === "snapshot") {
      const textarea = getDocTextarea();
      if (textarea) textarea.value = docsState.snapshotCache.content || "";
    }
    renderSnapshotButtons();
    if (notify)
      flash(docsState.snapshotCache.exists ? "Snapshot loaded" : "No snapshot yet");
  } catch (err) {
    flash((err as Error)?.message || "Failed to load snapshot");
  } finally {
    setSnapshotBusy(false);
  }
}

export async function runSnapshot(): Promise<void> {
  if (docsState.snapshotBusy) return;
  try {
    setSnapshotBusy(true);
    const data = await api("/api/snapshot", {
      method: "POST",
      body: {},
    });
    docsState.snapshotCache = {
      exists: true,
      content: (data as { content?: string })?.content || "",
      state: (data as { state?: Record<string, unknown> })?.state || {},
    };
    if (getActiveDoc() === "snapshot") {
      const textarea = getDocTextarea();
      if (textarea) textarea.value = docsState.snapshotCache.content || "";
    }
    renderSnapshotButtons();
    flash("Snapshot generated");
  } catch (err) {
    flash((err as Error)?.message || "Snapshot generation failed");
  } finally {
    setSnapshotBusy(false);
  }
}
