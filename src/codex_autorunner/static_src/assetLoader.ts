type AssetWindow = {
  __assetSuffix?: string;
};

function getAssetSuffix(): string {
  if (typeof window === "undefined") return "";
  return ((window as unknown as AssetWindow).__assetSuffix || "").trim();
}

/**
 * Keep lazy-loaded chunks on the same asset version as the entry module.
 * Without this, a fresh app.js can import stale cached child modules.
 */
export function importVersionedModule<T>(path: string): Promise<T> {
  return import(`${path}${getAssetSuffix()}`) as Promise<T>;
}
