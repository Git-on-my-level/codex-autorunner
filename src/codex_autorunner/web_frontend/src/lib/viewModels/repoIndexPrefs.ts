/** Browser persistence for the Repos index: per-repo collapse overrides + hide-stale toggle. */

const STORAGE_KEY = 'car.web.repos.indexPrefs.v1';

export type RepoIndexPrefs = {
  /** Explicit user overrides keyed by repo id. true = collapsed, false = expanded. Absent = use default. */
  collapsed: Record<string, boolean>;
  /** Hide worktrees considered stale across the index. */
  hideStale: boolean;
};

function emptyPrefs(): RepoIndexPrefs {
  return { collapsed: {}, hideStale: false };
}

export function loadRepoIndexPrefs(): RepoIndexPrefs {
  if (typeof window === 'undefined') return emptyPrefs();
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return emptyPrefs();
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return emptyPrefs();
    const collapsedRaw = (parsed as Record<string, unknown>).collapsed;
    const collapsed: Record<string, boolean> = {};
    if (collapsedRaw && typeof collapsedRaw === 'object') {
      for (const [id, value] of Object.entries(collapsedRaw as Record<string, unknown>)) {
        if (typeof value === 'boolean') collapsed[id] = value;
      }
    }
    const hideStale = (parsed as Record<string, unknown>).hideStale === true;
    return { collapsed, hideStale };
  } catch {
    return emptyPrefs();
  }
}

export function saveRepoIndexPrefs(prefs: RepoIndexPrefs): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs));
  } catch {
    // Best-effort; ignore quota / disabled storage.
  }
}
