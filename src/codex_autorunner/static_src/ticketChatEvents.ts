import { ticketChat } from "./ticketChatActions.js";

// This module now delegates to docChatCore for rendering and event parsing.

export function applyTicketEvent(payload: unknown): void {
  ticketChat.applyAppEvent(payload);
}

export function renderTicketEvents(): void {
  ticketChat.renderEvents();
}

export function renderTicketMessages(): void {
  ticketChat.renderMessages();
}

export function initTicketChatEvents(): void {
  // Toggle already wired in docChatCore constructor.
  return;
}
