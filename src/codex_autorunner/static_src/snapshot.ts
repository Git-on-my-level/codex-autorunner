import { api, flash } from "./utils.js";

interface UIElements {
  status: HTMLElement | null;
  content: HTMLTextAreaElement | null;
  generate: HTMLButtonElement | null;
  update: HTMLButtonElement | null;
  regenerate: HTMLButtonElement | null;
  copy: HTMLButtonElement | null;
  refresh: HTMLButtonElement | null;
}

const UI: UIElements = {
  status: document.getElementById("snapshot-status"),
  content: document.getElementById("snapshot-content") as HTMLTextAreaElement,
  generate: document.getElementById("snapshot-generate") as HTMLButtonElement,
  update: document.getElementById("snapshot-update") as HTMLButtonElement,
  regenerate: document.getElementById("snapshot-regenerate") as HTMLButtonElement,
  copy: document.getElementById("snapshot-copy") as HTMLButtonElement,
  refresh: document.getElementById("snapshot-refresh") as HTMLButtonElement,
};

interface SnapshotData {
  exists: boolean;
  content: string;
  state: Record<string, unknown>;
}

let latest: SnapshotData = { exists: false, content: "", state: {} };
let busy = false;

function setBusy(on: boolean): void {
  busy = on;
  const disabled = !!on;
  for (const btn of [UI.generate, UI.update, UI.regenerate, UI.copy, UI.refresh]) {
    if (!btn) continue;
    btn.disabled = disabled;
  }
  if (UI.status) UI.status.textContent = on ? "Working…" : "";
}

function render(): void {
  if (!UI.content) return;
  UI.content.value = latest.content || "";
  if (UI.generate) UI.generate.classList.toggle("hidden", false);
  if (UI.update) UI.update.classList.toggle("hidden", true);
  if (UI.regenerate) UI.regenerate.classList.toggle("hidden", true);
  if (UI.copy) UI.copy.disabled = busy || !(latest.content || "").trim();
}

async function loadSnapshot({ notify = false }: { notify?: boolean } = {}): Promise<void> {
  if (busy) return;
  try {
    setBusy(true);
    const data = await api("/api/snapshot");
    latest = {
      exists: !!(data as { exists?: boolean })?.exists,
      content: (data as { content?: string })?.content || "",
      state: (data as { state?: Record<string, unknown> })?.state || {},
    };
    render();
    if (notify) flash(latest.exists ? "Snapshot loaded" : "No snapshot yet");
  } catch (err) {
    flash((err as Error)?.message || "Failed to load snapshot");
  } finally {
    setBusy(false);
  }
}

async function runSnapshot(): Promise<void> {
  if (busy) return;
  try {
    setBusy(true);
    const data = await api("/api/snapshot", {
      method: "POST",
      body: {},
    });
    latest = {
      exists: true,
      content: (data as { content?: string })?.content || "",
      state: (data as { state?: Record<string, unknown> })?.state || {},
    };
    render();
    flash("Snapshot generated");
  } catch (err) {
    flash((err as Error)?.message || "Snapshot generation failed");
  } finally {
    setBusy(false);
  }
}

async function copyToClipboard(): Promise<void> {
  const text = UI.content?.value || "";
  if (!text.trim()) return;
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      flash("Copied to clipboard");
      return;
    }
  } catch {
    // fall through
  }
  try {
    UI.content?.focus();
    UI.content?.select();
    const ok = document.execCommand("copy");
    flash(ok ? "Copied to clipboard" : "Copy failed");
  } catch {
    flash("Copy failed");
  } finally {
    try {
      UI.content?.setSelectionRange(0, 0);
    } catch {
      // ignore
    }
  }
}

export function initSnapshot(): void {
  if (!UI.content) return;

  UI.generate?.addEventListener("click", () => runSnapshot());
  UI.update?.addEventListener("click", () => runSnapshot());
  UI.regenerate?.addEventListener("click", () => runSnapshot());
  UI.copy?.addEventListener("click", copyToClipboard);
  UI.refresh?.addEventListener("click", () => loadSnapshot({ notify: true }));

  loadSnapshot().catch(() => {});
}
