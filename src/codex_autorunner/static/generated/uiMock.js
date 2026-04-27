// GENERATED FILE - do not edit directly. Source: static_src/
import { getUiMockScenarioOrDefault, getUiMockScenarioList } from "./uiMockScenarios.js?v=62070ab22f9201700f4cbe1ce8b08b2a7cf7419dd93d9677cdfc7ba5c9537a14";
const PARAM = "uiMock";
const STRIP_PARAM = "uiMockStrip";
let initialized = false;
let active = null;
function hasWindow() {
    return typeof window !== "undefined" && typeof window.location !== "undefined";
}
/**
 * When ?uiMock=<scenario> is present, the hub UI uses canned API payloads so you
 * can screenshot consistent states (local dev and QA) without a prepared hub.
 * Optional: &uiMockStrip=1 to remove uiMock* params from the address bar after init.
 */
export function initUiMockFromUrl() {
    if (!hasWindow() || initialized) {
        return;
    }
    initialized = true;
    let raw;
    try {
        raw = new URLSearchParams(window.location.search || "").get(PARAM) || "";
    }
    catch {
        return;
    }
    const trimmed = String(raw).trim();
    if (!trimmed) {
        return;
    }
    const { scenario, resolvedId, fallback } = getUiMockScenarioOrDefault(trimmed);
    void fallback;
    active = { raw: trimmed, resolvedId, bundle: scenario };
    if (typeof window.__CAR_UI_MOCK === "undefined") {
        window.__CAR_UI_MOCK = {
            active: true,
            param: PARAM,
            raw: trimmed,
            scenarioId: resolvedId,
            label: scenario.label,
            scenarios: getUiMockScenarioList(),
        };
    }
    const strip = new URLSearchParams(window.location.search || "").get(STRIP_PARAM);
    if (strip === "1" || strip === "true") {
        try {
            const u = new URL(window.location.href);
            u.searchParams.delete(PARAM);
            u.searchParams.delete(STRIP_PARAM);
            if (typeof history !== "undefined" && history.replaceState) {
                history.replaceState(null, "", u.toString());
            }
        }
        catch {
            // ignore
        }
    }
}
export function isUiMockActive() {
    return active !== null;
}
export function getUiMockScenarioId() {
    return active?.resolvedId ?? null;
}
function pathnameOf(resolvedPath) {
    try {
        const u = new URL(resolvedPath, window.location.href);
        return { pathname: u.pathname, search: u.search };
    }
    catch {
        return { pathname: "", search: "" };
    }
}
function isHubRepoListPath(pathname) {
    return /\/hub\/repos\/?$/.test(pathname);
}
/**
 * If uiMock is active, return a JSON payload for whitelistedGET hub routes; otherwise null (real fetch).
 */
export function getUiMockJsonForRequest(resolvedPath, method) {
    if (!active)
        return null;
    const m = (method || "GET").toUpperCase();
    if (m !== "GET" && m !== "HEAD")
        return null;
    const { pathname } = pathnameOf(resolvedPath);
    const b = active.bundle;
    if (isHubRepoListPath(pathname)) {
        return b.hubData;
    }
    if (pathname.includes("/hub/chat/channels")) {
        return b.channels;
    }
    if (/\/hub\/usage\/?$/.test(pathname)) {
        return b.hubUsage;
    }
    if (/\/hub\/version\/?$/.test(pathname)) {
        return b.hubVersion;
    }
    if (/\/system\/update\/status\/?$/.test(pathname)) {
        if (b.systemUpdateStatus)
            return b.systemUpdateStatus;
        return { status: "idle", at: "mock" };
    }
    if (/\/hub\/pma\/agents\/?$/.test(pathname)) {
        if (b.pmaAgents)
            return b.pmaAgents;
        return null;
    }
    return null;
}
