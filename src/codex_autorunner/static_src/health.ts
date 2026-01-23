import { publish } from "./bus.js";
import { setAutoRefreshEnabled } from "./autoRefresh.js";
import { getAuthToken, resolvePath } from "./utils.js";

type HealthStatus = "ok" | "degraded" | "offline" | "unknown";

interface RepoHealth {
  status: HealthStatus;
  detail?: string | null;
}

let initialized = false;
let healthState: HealthStatus = "unknown";
let lastDetail: string | null = null;
let retryTimer: ReturnType<typeof setTimeout> | null = null;

function bannerEls(): {
  banner: HTMLElement | null;
  title: HTMLElement | null;
  detail: HTMLElement | null;
  retry: HTMLButtonElement | null;
} {
  return {
    banner: document.getElementById("repo-health-banner"),
    title: document.getElementById("repo-health-title"),
    detail: document.getElementById("repo-health-detail"),
    retry: document.getElementById("repo-health-retry") as HTMLButtonElement | null,
  };
}

function renderBanner(status: HealthStatus, detail?: string | null): void {
  const { banner, title, detail: detailEl, retry } = bannerEls();
  if (!banner || !title) return;
  if (status === "ok") {
    banner.classList.add("hidden");
    banner.classList.remove("error", "warn");
    if (retry) retry.disabled = false;
    return;
  }

  const isOffline = status === "offline";
  banner.classList.remove("hidden");
  banner.classList.toggle("error", isOffline);
  banner.classList.toggle("warn", !isOffline);
  title.textContent = isOffline
    ? "Repo server offline or unreachable"
    : "Repo server uninitialized";
  if (detailEl) {
    detailEl.textContent = detail || (isOffline ? "Check that the repo server is running." : "Initialize the repo to enable flows/docs.");
  }
  if (retry) retry.disabled = false;
}

function setHealth(status: HealthStatus, detail?: string | null): void {
  const changed = status !== healthState || detail !== lastDetail;
  healthState = status;
  lastDetail = detail || null;
  renderBanner(status, detail);
  setAutoRefreshEnabled(status !== "offline");
  if (changed) {
    publish("repo:health", { status, detail });
  }
}

async function tryFetch(path: string): Promise<{ ok: boolean; status: number; payload: any; text: string }> {
  const target = resolvePath(path);
  const headers: Record<string, string> = {};
  const token = getAuthToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch(target, { headers });
  const text = await res.text();
  let payload: any = null;
  try {
    payload = JSON.parse(text);
  } catch (_err) {
    payload = null;
  }
  return { ok: res.ok, status: res.status, payload, text };
}

function deriveHealthFromPayload(payload: any): RepoHealth {
  if (!payload || typeof payload !== "object") {
    return { status: "offline", detail: "Empty health response" };
  }
  const payloadStatus = String((payload as Record<string, unknown>).status || "ok").toLowerCase();
  const flowsStatus = String((payload as Record<string, any>).flows?.status || "").toLowerCase();
  const docsStatus = String((payload as Record<string, any>).docs?.status || "").toLowerCase();

  if (payloadStatus !== "ok" && payloadStatus !== "degraded") {
    return { status: "offline", detail: String((payload as Record<string, any>).detail || payloadStatus) };
  }

  if (flowsStatus && flowsStatus !== "ok") {
    return {
      status: "degraded",
      detail: flowsStatus === "missing" ? "Flows DB missing; repo not initialized." : `Flows unavailable: ${flowsStatus}`,
    };
  }
  if (docsStatus && docsStatus !== "ok") {
    return {
      status: "degraded",
      detail: "Work docs missing; initialize .codex-autorunner.",
    };
  }
  // If the server is reachable but flows/docs are missing, surface a degraded state
  if (flowsStatus && flowsStatus !== "ok") {
    return {
      status: "degraded",
      detail:
        flowsStatus === "missing"
          ? "Flows DB missing; initialize the repo."
          : `Flows unavailable: ${flowsStatus}`,
    };
  }
  if (docsStatus && docsStatus !== "ok") {
    return {
      status: "degraded",
      detail: "Work docs missing; initialize .codex-autorunner.",
    };
  }

  return { status: "ok" };
}

async function probeHealth(): Promise<RepoHealth> {
  const paths = ["/api/repo/health", "/health"];
  let lastError: string | null = null;
  for (const path of paths) {
    try {
      const res = await tryFetch(path);
      if (res.ok) {
        return deriveHealthFromPayload(res.payload);
      }
      lastError = `${path} → ${res.status}`;
      if (res.status === 404) {
        continue;
      }
      break;
    } catch (err) {
      lastError = (err as Error).message || String(err);
    }
  }
  return { status: "offline", detail: lastError };
}

function scheduleNext(delayMs: number): void {
  if (retryTimer) {
    clearTimeout(retryTimer);
    retryTimer = null;
  }
  retryTimer = setTimeout(() => {
    void refreshRepoHealth();
  }, delayMs);
}

export async function refreshRepoHealth(): Promise<void> {
  const result = await probeHealth();
  setHealth(result.status, result.detail);
  const nextDelay =
    result.status === "ok" ? 20000 : Math.min(60000, result.status === "degraded" ? 20000 : 10000);
  scheduleNext(nextDelay);
}

export async function initHealthGate(): Promise<void> {
  if (initialized) return;
  initialized = true;
  const { retry } = bannerEls();
  if (retry) {
    retry.addEventListener("click", () => {
      if (retryTimer) {
        clearTimeout(retryTimer);
        retryTimer = null;
      }
      void refreshRepoHealth();
    });
  }
  await refreshRepoHealth();
}

export function isRepoHealthy(): boolean {
  return healthState === "ok" || healthState === "degraded";
}

export function currentHealthDetail(): string | null {
  return lastDetail;
}
