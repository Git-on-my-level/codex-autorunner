// GENERATED FILE - do not edit directly. Source: static_src/
function getAssetSuffix() {
    if (typeof window === "undefined")
        return "";
    return (window.__assetSuffix || "").trim();
}
/**
 * Keep lazy-loaded chunks on the same asset version as the entry module.
 * Without this, a fresh app.js can import stale cached child modules.
 */
export function importVersionedModule(path) {
    return import(`${path}${getAssetSuffix()}`);
}
