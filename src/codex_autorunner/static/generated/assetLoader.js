// GENERATED FILE - do not edit directly. Source: static_src/
function getAssetSuffix() {
    const version = new URL(import.meta.url).searchParams.get("v");
    // Use the loader module's own stamped URL so lazy imports share the exact
    // same ESM instance graph as statically imported generated modules.
    return version ? `?v=${encodeURIComponent(version)}` : "";
}
/**
 * Keep lazy-loaded chunks on the same asset version as the entry module.
 * Without this, a fresh app.js can import stale cached child modules.
 */
export function importVersionedModule(path) {
    return import(`${path}${getAssetSuffix()}`);
}
