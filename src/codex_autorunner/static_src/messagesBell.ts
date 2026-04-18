import { api } from "./utils.js";
import { registerAutoRefresh, type RefreshContext } from "./autoRefresh.js";
import { CONSTANTS } from "./constants.js";
import { subscribe } from "./bus.js";
import { isRepoHealthy } from "./health.js";

interface ActiveMessageResponse {
  active?: boolean;
  run_id?: string;
}

let bellInitialized = false;
let activeRunId: string | null = null;
let messageBellCleanup: (() => void) | null = null;

function setBadge(count: number): void {
  const badge = document.getElementById("tab-badge-inbox");
  if (!badge) return;
  if (count > 0) {
    badge.textContent = String(count);
    badge.classList.remove("hidden");
  } else {
    badge.textContent = "";
    badge.classList.add("hidden");
  }
}

function clearActiveRun(): void {
  activeRunId = null;
  setBadge(0);
}

export function getActiveMessageRunId(): string | null {
  return activeRunId;
}

export async function refreshBell(): Promise<void> {
  if (!isRepoHealthy()) {
    clearActiveRun();
    return;
  }
  try {
    const res = (await api("/api/messages/active")) as ActiveMessageResponse;
    if (res?.active && res.run_id) {
      activeRunId = res.run_id;
      setBadge(1);
    } else {
      clearActiveRun();
    }
  } catch (_err) {
    clearActiveRun();
  }
}

export function initMessageBell(): void {
  if (bellInitialized) return;
  bellInitialized = true;

  if (messageBellCleanup) {
    messageBellCleanup();
  }
  messageBellCleanup = registerAutoRefresh("messages:bell", {
    callback: async (_ctx: RefreshContext) => {
      if (!isRepoHealthy()) {
        clearActiveRun();
        return;
      }
      await refreshBell();
    },
    interval: CONSTANTS.UI.POLLING_INTERVAL,
    refreshOnActivation: true,
    immediate: true,
  });

  subscribe("repo:health", (payload: unknown) => {
    const status = (payload as { status?: string } | null)?.status || "";
    if (status === "ok" || status === "degraded") {
      void refreshBell();
    }
  });
}
