/** Browser persistence for `/chats` new-chat pickers. */

const STORAGE_KEY = 'car.web.chat.lastNewChatPreferences.v1';

export type NewChatPreferenceKind = 'pma' | 'agent';

export type NewChatPreference = {
  scopeId: string;
};

export type LastNewChatPreferences = Partial<Record<NewChatPreferenceKind, NewChatPreference>>;

export function loadLastNewChatPreferences(): LastNewChatPreferences {
  if (typeof window === 'undefined') return {};
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return {};
    const out: LastNewChatPreferences = {};
    for (const kind of ['pma', 'agent'] as const) {
      const value = (parsed as Record<string, unknown>)[kind];
      if (!value || typeof value !== 'object') continue;
      const scopeId = (value as Record<string, unknown>).scopeId;
      if (typeof scopeId !== 'string' || !scopeId.trim()) continue;
      out[kind] = { scopeId: scopeId.trim() };
    }
    return out;
  } catch {
    return {};
  }
}

function saveLastNewChatPreferences(preferences: LastNewChatPreferences): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(preferences));
  } catch {
    // Best-effort; ignore quota / disabled storage.
  }
}

export function getLastNewChatPreference(kind: NewChatPreferenceKind): NewChatPreference | null {
  return loadLastNewChatPreferences()[kind] ?? null;
}

export function persistLastNewChatPreference(kind: NewChatPreferenceKind, preference: NewChatPreference): void {
  const scopeId = preference.scopeId.trim();
  if (!scopeId) return;
  const preferences = loadLastNewChatPreferences();
  preferences[kind] = { scopeId };
  saveLastNewChatPreferences(preferences);
}
