/** Browser persistence for `/chats` new-chat pickers. */

const STORAGE_KEY = 'car.web.chat.lastNewChatPreference.v2';

export type NewChatPreferenceKind = 'pma' | 'agent';

export type LastNewChatPreference = {
  scopeId: string;
  kind: NewChatPreferenceKind;
};

export function loadLastNewChatPreference(): LastNewChatPreference | null {
  if (typeof window === 'undefined') return null;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return null;
    const scopeId = (parsed as Record<string, unknown>).scopeId;
    const kind = (parsed as Record<string, unknown>).kind;
    if (typeof scopeId !== 'string' || !scopeId.trim()) return null;
    if (kind !== 'pma' && kind !== 'agent') return null;
    return { scopeId: scopeId.trim(), kind };
  } catch {
    return null;
  }
}

function saveLastNewChatPreference(preference: LastNewChatPreference): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(preference));
  } catch {
    // Best-effort; ignore quota / disabled storage.
  }
}

export function getLastNewChatPreference(): LastNewChatPreference | null {
  return loadLastNewChatPreference();
}

export function persistLastNewChatPreference(preference: LastNewChatPreference): void {
  const scopeId = preference.scopeId.trim();
  if (!scopeId) return;
  saveLastNewChatPreference({ scopeId, kind: preference.kind });
}
