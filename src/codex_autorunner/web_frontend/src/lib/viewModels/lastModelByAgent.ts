/** Browser persistence for `/chats` model picker: remember last explicit choice per agent id. */

const STORAGE_KEY = 'pma:lastModelByAgent';

export type LastModelByAgentMap = Record<string, string>;

export function loadLastModelMap(): LastModelByAgentMap {
  if (typeof window === 'undefined') return {};
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return {};
    const out: LastModelByAgentMap = {};
    for (const [key, value] of Object.entries(parsed as Record<string, unknown>)) {
      if (typeof key !== 'string' || typeof value !== 'string') continue;
      const trimmed = value.trim();
      if (!trimmed) continue;
      out[key.toLowerCase()] = trimmed;
    }
    return out;
  } catch {
    return {};
  }
}

function saveLastModelMap(map: LastModelByAgentMap): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(map));
  } catch {
    // Best-effort; ignore quota / disabled storage.
  }
}

/** Prefer this model id when listing catalogs for the agent (if still available). */
export function getLastModelForAgent(agentId: string): string {
  const id = agentId.trim().toLowerCase();
  if (!id) return '';
  return loadLastModelMap()[id] ?? '';
}

/** Remember user choice for `/chats` picker (best-effort). Empty clears storage for that agent. */
export function persistLastModelForAgent(agentId: string, model: string | null | undefined): void {
  const key = agentId.trim().toLowerCase();
  if (!key) return;
  const map = loadLastModelMap();
  const trimmed = typeof model === 'string' ? model.trim() : '';
  if (!trimmed) delete map[key];
  else map[key] = trimmed;
  saveLastModelMap(map);
}
