import { TicketChatMessage } from "./ticketChatActions.js";

const STORAGE_KEY_PREFIX = "car-ticket-chat-";
const STORAGE_VERSION = 1;
const MAX_MESSAGES = 50;

interface TicketChatStorage {
  version: number;            // For future migrations
  ticketIndex: number;
  messages: TicketChatMessage[];
  lastUpdated: string;        // ISO timestamp
}

export function saveTicketChatHistory(ticketIndex: number, messages: TicketChatMessage[]): void {
  const key = `${STORAGE_KEY_PREFIX}${ticketIndex}`;
  const data: TicketChatStorage = {
    version: STORAGE_VERSION,
    ticketIndex,
    messages: messages.slice(-MAX_MESSAGES),  // Keep only recent
    lastUpdated: new Date().toISOString(),
  };
  try {
    localStorage.setItem(key, JSON.stringify(data));
  } catch (e) {
    // Handle quota exceeded - clear old entries
    console.warn("localStorage quota exceeded, clearing old chat history");
    clearOldTicketChatHistory();
    try {
      localStorage.setItem(key, JSON.stringify(data));
    } catch (e2) {
      console.error("Failed to save chat history even after clearing old entries", e2);
    }
  }
}

export function loadTicketChatHistory(ticketIndex: number): TicketChatMessage[] {
  const key = `${STORAGE_KEY_PREFIX}${ticketIndex}`;
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return [];
    const data = JSON.parse(raw) as TicketChatStorage;
    if (data.version !== STORAGE_VERSION) return [];  // Migration needed
    return data.messages || [];
  } catch {
    return [];
  }
}

export function clearTicketChatHistory(ticketIndex: number): void {
  localStorage.removeItem(`${STORAGE_KEY_PREFIX}${ticketIndex}`);
}

function clearOldTicketChatHistory(): void {
  // Find and remove oldest ticket chat entries
  const entries: Array<{key: string; lastUpdated: string}> = [];
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i);
    if (key?.startsWith(STORAGE_KEY_PREFIX)) {
      try {
        const data = JSON.parse(localStorage.getItem(key) || "{}");
        entries.push({ key, lastUpdated: data.lastUpdated || "" });
      } catch (e) {
        // Ignore parse errors for individual entries
      }
    }
  }
  // Sort by date, remove oldest half
  entries.sort((a, b) => a.lastUpdated.localeCompare(b.lastUpdated));
  entries.slice(0, Math.ceil(entries.length / 2)).forEach(e => localStorage.removeItem(e.key));
}
