/** Alias set aligned with `normalize_managed_thread_chat_kind` in `managed_thread_kinds.py`. */
const CODING_AGENT_ALIASES = new Set([
  'coding_agent',
  'coding-agent',
  'agent',
  'direct_agent',
  'direct-agent'
]);

export type ManagedThreadChatKind = 'pma' | 'coding_agent';

export function normalizeManagedThreadChatKind(value: unknown): ManagedThreadChatKind | null {
  if (typeof value !== 'string') return null;
  const text = value.trim().toLowerCase();
  if (!text) return null;
  if (text === 'pma') return 'pma';
  if (CODING_AGENT_ALIASES.has(text)) return 'coding_agent';
  return null;
}
