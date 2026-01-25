// GENERATED FILE - do not edit directly. Source: static_src/
import { publish } from "./bus.js";
import { setAutoRefreshEnabled } from "./autoRefresh.js";
import { getAuthToken, resolvePath } from "./utils.js";
let initialized = false;
let healthState = "unknown";
let lastDetail = null;
let retryTimer = null;
function bannerEls() {
    return {
        banner: document.getElementById("repo-health-banner"),
        title: document.getElementById("repo-health-title"),
        detail: document.getElementById("repo-health-detail"),
        retry: document.getElementById("repo-health-retry"),
    };
}
function renderBanner(status, detail) {
    const { banner, title, detail: detailEl, retry } = bannerEls();
    if (!banner || !title)
        return;
    if (status === "ok") {
        banner.classList.add("hidden");
        banner.classList.remove("error", "warn");
        if (retry)
            retry.disabled = false;
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
        detailEl.textContent =
            detail ||
                (isOffline
                    ? "Check that the repo server is running."
                    : "Create .codex-autorunner/tickets/ to initialize this repo.");
    }
    if (retry)
        retry.disabled = false;
}
function setHealth(status, detail) {
    const changed = status !== healthState || detail !== lastDetail;
    healthState = status;
    lastDetail = detail || null;
    renderBanner(status, detail);
    setAutoRefreshEnabled(status !== "offline");
    if (changed) {
        publish("repo:health", { status, detail });
    }
}
async function tryFetch(path) {
    const target = resolvePath(path);
    const headers = {};
    const token = getAuthToken();
    if (token)
        headers.Authorization = `Bearer ${token}`;
    const res = await fetch(target, { headers });
    const text = await res.text();
    let payload = null;
    try {
        payload = JSON.parse(text);
    }
    catch (_err) {
        payload = null;
    }
    return { ok: res.ok, status: res.status, payload, text };
}
function deriveHealthFromPayload(payload) {
    if (!payload || typeof payload !== "object") {
        return { status: "offline", detail: "Empty health response" };
    }
    const payloadStatus = String(payload.status || "ok").toLowerCase();
    if (payloadStatus !== "ok" && payloadStatus !== "degraded") {
        return { status: "offline", detail: String(payload.detail || payloadStatus) };
    }
    // Ticket-first: the only initialization requirement is `.codex-autorunner/tickets/`.
    const ticketsStatus = String(payload.tickets?.status || "").toLowerCase();
    if (ticketsStatus && ticketsStatus !== "ok") {
        return {
            status: "degraded",
            detail: "Tickets directory missing; create .codex-autorunner/tickets/.",
        };
    }
    // Flows DB is lazily created. Only treat truly unavailable storage as degraded.
    const flowsStatus = String(payload.flows?.status || "").toLowerCase();
    if (flowsStatus && flowsStatus !== "ok" && flowsStatus !== "missing") {
        return { status: "degraded", detail: `Flows unavailable: ${flowsStatus}` };
    }
    return { status: "ok" };
}
async function probeHealth() {
    const paths = ["/api/repo/health", "/health"];
    let lastError = null;
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
        }
        catch (err) {
            lastError = err.message || String(err);
        }
    }
    return { status: "offline", detail: lastError };
}
function scheduleNext(delayMs) {
    if (retryTimer) {
        clearTimeout(retryTimer);
        retryTimer = null;
    }
    retryTimer = setTimeout(() => {
        void refreshRepoHealth();
    }, delayMs);
}
export async function refreshRepoHealth() {
    const result = await probeHealth();
    setHealth(result.status, result.detail);
    const nextDelay = result.status === "ok" ? 20000 : Math.min(60000, result.status === "degraded" ? 20000 : 10000);
    scheduleNext(nextDelay);
}
export async function initHealthGate() {
    if (initialized)
        return;
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
export function isRepoHealthy() {
    return healthState === "ok" || healthState === "degraded";
}
export function currentHealthDetail() {
    return lastDetail;
}
