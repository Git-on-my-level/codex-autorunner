// GENERATED FILE - do not edit directly. Source: static_src/
import { HUB_BASE } from "./env.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
export const HUB_CACHE_TTL_MS = 30000;
export const HUB_PERSISTENT_CACHE_TTL_MS = 24 * 60 * 60 * 1000;
export const HUB_CACHE_KEY = `car:hub:v2:${HUB_BASE || "/"}`;
export const HUB_USAGE_CACHE_KEY = `car:hub-usage:${HUB_BASE || "/"}`;
let hubUsageIndex = {};
let hubUsageUnmatched = null;
export function getHubUsageUnmatched() {
    return hubUsageUnmatched;
}
export function saveSessionCache(key, value) {
    try {
        const payload = { at: Date.now(), value };
        sessionStorage.setItem(key, JSON.stringify(payload));
    }
    catch (_err) {
        // Ignore storage errors; cache is best-effort.
    }
}
export function loadSessionCache(key, maxAgeMs) {
    try {
        const raw = sessionStorage.getItem(key);
        if (!raw)
            return null;
        const payload = JSON.parse(raw);
        if (!payload || typeof payload.at !== "number")
            return null;
        if (maxAgeMs && Date.now() - payload.at > maxAgeMs)
            return null;
        return payload.value;
    }
    catch (_err) {
        return null;
    }
}
export function savePersistentCache(key, value) {
    try {
        const payload = { at: Date.now(), value };
        localStorage.setItem(key, JSON.stringify(payload));
    }
    catch (_err) {
        // Ignore storage errors; cache is best-effort.
    }
}
export function loadPersistentCache(key, maxAgeMs) {
    try {
        const raw = localStorage.getItem(key);
        if (!raw)
            return null;
        const payload = JSON.parse(raw);
        if (!payload || typeof payload.at !== "number")
            return null;
        if (maxAgeMs && Date.now() - payload.at > maxAgeMs)
            return null;
        return payload.value;
    }
    catch (_err) {
        return null;
    }
}
export function saveHubBootstrapCache(value) {
    saveSessionCache(HUB_CACHE_KEY, value);
    savePersistentCache(HUB_CACHE_KEY, value);
}
export function loadHubBootstrapCache() {
    const sessionValue = loadSessionCache(HUB_CACHE_KEY, HUB_CACHE_TTL_MS);
    if (sessionValue)
        return sessionValue;
    const persistentValue = loadPersistentCache(HUB_CACHE_KEY, HUB_PERSISTENT_CACHE_TTL_MS);
    if (persistentValue) {
        saveSessionCache(HUB_CACHE_KEY, persistentValue);
        return persistentValue;
    }
    return null;
}
export function formatTokensCompact(val) {
    if (val === null || val === undefined)
        return "0";
    const num = Number(val);
    if (Number.isNaN(num))
        return String(val);
    if (num >= 1000000)
        return `${(num / 1000000).toFixed(1)}M`;
    if (num >= 1000)
        return `${(num / 1000).toFixed(0)}k`;
    return num.toLocaleString();
}
export function indexHubUsage(data) {
    hubUsageIndex = {};
    hubUsageUnmatched = data?.unmatched || null;
    if (!data?.repos)
        return;
    data.repos.forEach((repo) => {
        if (repo?.id)
            hubUsageIndex[repo.id] = repo;
    });
}
export function getRepoUsage(repoId) {
    const usage = hubUsageIndex[repoId];
    if (!usage)
        return { label: "—", hasData: false };
    const totals = usage.totals || {};
    return {
        label: formatTokensCompact(totals.total_tokens),
        hasData: true,
    };
}
