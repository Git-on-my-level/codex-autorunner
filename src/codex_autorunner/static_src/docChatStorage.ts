/**
 * Canonical Chat History Storage
 *
 * This module provides the canonical localStorage contract for chat message history
 * across all chat surfaces (ticket, contextspace, PMA).
 *
 * Namespace conventions (keyPrefix in ChatStorageConfig):
 * - Ticket chat: `car-ticket-chat-` + target (e.g., `car-ticket-chat-ticket:123`)
 * - Contextspace: `car-contextspace-chat-` + target (e.g., `car-contextspace-chat-contextspace:active_context`)
 * - PMA: `car.pma.` + target (e.g., `car.pma.pma`)
 *
 * Version field enables safe schema migrations - loading returns empty if version mismatches.
 */
export interface ChatStorageConfig {
  keyPrefix: string;
  maxMessages: number;
  version?: number;
}

type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  time: string;
  isFinal: boolean;
};

interface StoredChat {
  version: number;
  target: string;
  messages: ChatMessage[];
  lastUpdated: string;
}

const DEFAULT_VERSION = 1;

function buildKey(config: ChatStorageConfig, target: string): string {
  return `${config.keyPrefix}${target}`;
}

export function saveChatHistory(
  config: ChatStorageConfig,
  target: string,
  messages: ChatMessage[]
): void {
  const key = buildKey(config, target);
  const data: StoredChat = {
    version: config.version ?? DEFAULT_VERSION,
    target,
    messages: messages.slice(-(config.maxMessages || 50)),
    lastUpdated: new Date().toISOString(),
  };

  try {
    localStorage.setItem(key, JSON.stringify(data));
  } catch (err) {
    console.warn("localStorage quota exceeded, clearing old chat history", err);
    clearOldChatHistory(config);
    try {
      localStorage.setItem(key, JSON.stringify(data));
    } catch (err2) {
      console.error("Failed to save chat history after cleanup", err2);
    }
  }
}

export function loadChatHistory(
  config: ChatStorageConfig,
  target: string
): ChatMessage[] {
  const key = buildKey(config, target);
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return [];
    const data = JSON.parse(raw) as StoredChat;
    const version = config.version ?? DEFAULT_VERSION;
    if (data.version !== version) return [];
    return data.messages || [];
  } catch {
    return [];
  }
}

export function clearChatHistory(config: ChatStorageConfig, target: string): void {
  localStorage.removeItem(buildKey(config, target));
}

function clearOldChatHistory(config: ChatStorageConfig): void {
  const entries: Array<{ key: string; lastUpdated: string }> = [];
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i);
    if (key?.startsWith(config.keyPrefix)) {
      try {
        const data = JSON.parse(localStorage.getItem(key) || "{}");
        entries.push({ key, lastUpdated: data.lastUpdated || "" });
      } catch {
        // ignore parse errors
      }
    }
  }

  entries
    .sort((a, b) => a.lastUpdated.localeCompare(b.lastUpdated))
    .slice(0, Math.ceil(entries.length / 2))
    .forEach((entry) => localStorage.removeItem(entry.key));
}
