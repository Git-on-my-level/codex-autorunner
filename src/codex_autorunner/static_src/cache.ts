import { BASE_PATH, REPO_ID } from "./env.js";


function cachePrefix(): string {
  const scope = REPO_ID ? `repo:${REPO_ID}` : `base:${BASE_PATH || ""}`;
  return `car:${encodeURIComponent(scope)}:`;
}

function scopedKey(key: string): string {
  return cachePrefix() + key;
}

export function saveToCache(key: string, data: unknown): void {
  try {
    const json = JSON.stringify(data);
    localStorage.setItem(scopedKey(key), json);
  } catch (err) {
    console.warn("Failed to save to cache", key, err);
  }
}

export function loadFromCache<T = unknown>(key: string): T | null {
  try {
    const json = localStorage.getItem(scopedKey(key));
    if (!json) return null;
    return JSON.parse(json) as T;
  } catch (err) {
    console.warn("Failed to load from cache", key, err);
    return null;
  }
}

export function clearCache(key: string): void {
  try {
    localStorage.removeItem(scopedKey(key));
  } catch (err) {
    console.warn("Failed to clear cache", key, err);
  }
}
