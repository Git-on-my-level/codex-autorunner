// GENERATED FILE - do not edit directly. Source: static_src/
import { BASE_PATH, REPO_ID } from "./env.js?v=7fa8004f6840e214503b15a447aff6b141a7ad76cba89a9cf20138dbd2d88456";
function cachePrefix() {
    const scope = REPO_ID ? `repo:${REPO_ID}` : `base:${BASE_PATH || ""}`;
    return `car:${encodeURIComponent(scope)}:`;
}
function scopedKey(key) {
    return cachePrefix() + key;
}
export function saveToCache(key, data) {
    try {
        const json = JSON.stringify(data);
        localStorage.setItem(scopedKey(key), json);
    }
    catch (err) {
        console.warn("Failed to save to cache", key, err);
    }
}
export function loadFromCache(key) {
    try {
        const json = localStorage.getItem(scopedKey(key));
        if (!json)
            return null;
        return JSON.parse(json);
    }
    catch (err) {
        console.warn("Failed to load from cache", key, err);
        return null;
    }
}
export function clearCache(key) {
    try {
        localStorage.removeItem(scopedKey(key));
    }
    catch (err) {
        console.warn("Failed to clear cache", key, err);
    }
}
