/**
 * Ticket Chat Events - handles parsing and rendering of agent events (thinking, tool calls, etc.)
 * Ported from docChatEvents.ts for the ticket chat experience.
 */
import {
  getTicketChatElements,
  ticketChatState,
  TICKET_CHAT_EVENT_LIMIT,
  TICKET_CHAT_EVENT_MAX,
  type TicketChatEvent,
  type TicketChatState,
} from "./ticketChatActions.js";

interface CommandItem {
  command?: string | string[];
  type?: string;
  exitCode?: number | null;
  text?: string;
  message?: string;
  name?: string;
  tool?: string;
  id?: string;
  itemId?: string;
}

interface PayloadParams {
  command?: string | string[];
  error?: ErrorObject | string;
  delta?: string;
  text?: string;
  output?: string;
  status?: string;
  message?: string;
  files?: Array<string | { path?: string; file?: string; name?: string }>;
  fileChanges?: Array<string | { path?: string; file?: string; name?: string }>;
  paths?: Array<string | { path?: string; file?: string; name?: string }>;
  path?: string | { path?: string; file?: string; name?: string };
  file?: string | { path?: string; file?: string; name?: string };
  name?: string | { path?: string; file?: string; name?: string };
  item?: CommandItem;
  itemId?: string | null;
}

interface ErrorObject {
  message?: string;
  additionalDetails?: string;
  details?: string;
}

interface EventPayload {
  message?: EventMessage | unknown;
  received_at?: number;
  receivedAt?: number;
  id?: string;
}

interface EventMessage {
  method?: string;
  params?: PayloadParams;
}

function extractCommand(
  item: CommandItem | null | undefined,
  params: PayloadParams | null | undefined
): string {
  const command = item?.command ?? params?.command;
  if (Array.isArray(command)) {
    return command
      .map((part) => String(part))
      .join(" ")
      .trim();
  }
  if (typeof command === "string") return command.trim();
  return "";
}

function extractFiles(payload: PayloadParams | null | undefined): string[] {
  const files: string[] = [];
  const addEntry = (entry: unknown): void => {
    if (typeof entry === "string" && entry.trim()) {
      files.push(entry.trim());
      return;
    }
    if (entry && typeof entry === "object") {
      const entryObj = entry as Record<string, unknown>;
      const path = entryObj.path || entryObj.file || entryObj.name;
      if (typeof path === "string" && path.trim()) {
        files.push(path.trim());
      }
    }
  };
  if (!payload || typeof payload !== "object") return files;
  for (const key of ["files", "fileChanges", "paths"] as Array<keyof PayloadParams>) {
    const value = payload[key];
    if (Array.isArray(value)) {
      value.forEach(addEntry);
    }
  }
  for (const key of ["path", "file", "name"]) {
    addEntry((payload as Record<string, unknown>)[key as string]);
  }
  return files;
}

function extractErrorMessage(params: PayloadParams | null | undefined): string {
  if (!params || typeof params !== "object") return "";
  const err = params.error;
  if (err && typeof err === "object") {
    const errObj = err as ErrorObject;
    const message = typeof errObj.message === "string" ? errObj.message : "";
    const details =
      typeof errObj.additionalDetails === "string"
        ? errObj.additionalDetails
        : typeof errObj.details === "string"
          ? errObj.details
          : "";
    if (message && details && message !== details) {
      return `${message} (${details})`;
    }
    return message || details;
  }
  if (typeof err === "string") return err;
  if (typeof params.message === "string") return params.message;
  return "";
}

/**
 * Extract output delta text from an event payload.
 */
export function extractOutputDelta(payload: unknown): string {
  const message =
    payload && typeof payload === "object"
      ? (payload as EventPayload).message || payload
      : payload;
  if (!message || typeof message !== "object") return "";
  const method = String((message as EventMessage).method || "").toLowerCase();
  if (!method.includes("outputdelta")) return "";
  const params = (message as EventMessage).params || {};
  if (typeof params.delta === "string") return params.delta;
  if (typeof params.text === "string") return params.text;
  if (typeof params.output === "string") return params.output;
  return "";
}

function addTicketEvent(state: TicketChatState, entry: TicketChatEvent): void {
  state.events.push(entry);
  if (state.events.length > TICKET_CHAT_EVENT_MAX) {
    state.events = state.events.slice(-TICKET_CHAT_EVENT_MAX);
    state.eventItemIndex = {};
    state.events.forEach((evt, idx) => {
      if (evt.itemId) state.eventItemIndex[evt.itemId] = idx;
    });
  }
}

/**
 * Apply an App-server event to the ticket chat state.
 * This parses the event and adds it to the events array for display.
 */
export function applyTicketEvent(
  state: TicketChatState,
  payload: EventPayload | unknown
): void {
  const message =
    payload && typeof payload === "object"
      ? (payload as EventPayload).message || payload
      : payload;
  if (!message || typeof message !== "object") return;
  const messageObj = message as EventMessage;
  const method = messageObj.method || "app-server";
  const params = messageObj.params || {};
  const item = (params.item as CommandItem) || {};
  const itemId = params.itemId || item.id || item.itemId || null;
  const receivedAt =
    payload && typeof payload === "object"
      ? (payload as EventPayload).received_at ||
        (payload as EventPayload).receivedAt ||
        Date.now()
      : Date.now();

  // Handle reasoning/thinking deltas - accumulate into existing event
  if (method === "item/reasoning/summaryTextDelta") {
    const delta = params.delta || "";
    if (!delta) return;
    const existingIndex =
      itemId && state.eventItemIndex[itemId] !== undefined
        ? (state.eventItemIndex[itemId] as number)
        : null;
    if (existingIndex !== null) {
      const existing = state.events[existingIndex];
      existing.summary = `${existing.summary || ""}${delta}`;
      existing.time = receivedAt;
      return;
    }
    const entry: TicketChatEvent = {
      id: (payload as EventPayload)?.id || `${Date.now()}`,
      title: "Thinking",
      summary: delta,
      detail: "",
      kind: "thinking",
      time: receivedAt,
      itemId,
      method,
    };
    addTicketEvent(state, entry);
    if (itemId) state.eventItemIndex[itemId] = state.events.length - 1;
    return;
  }

  // Handle reasoning part added (paragraph break)
  if (method === "item/reasoning/summaryPartAdded") {
    const existingIndex =
      itemId && state.eventItemIndex[itemId] !== undefined
        ? (state.eventItemIndex[itemId] as number)
        : null;
    if (existingIndex !== null) {
      const existing = state.events[existingIndex];
      existing.summary = `${existing.summary || ""}\n\n`;
      existing.time = receivedAt;
    }
    return;
  }

  let title = method;
  let summary = "";
  let detail = "";
  let kind = "event";

  // Handle generic status updates
  if (method === "status" || params.status) {
    title = "Status";
    summary = params.status || "Processing";
    kind = "status";
  } else if (method === "item/completed") {
    const itemType = (item as CommandItem).type;
    if (itemType === "commandExecution") {
      title = "Command";
      summary = extractCommand(item as CommandItem, params);
      kind = "command";
      if (
        (item as CommandItem).exitCode !== undefined &&
        (item as CommandItem).exitCode !== null
      ) {
        detail = `exit ${(item as CommandItem).exitCode}`;
      }
    } else if (itemType === "fileChange") {
      title = "File change";
      const files = extractFiles(item as PayloadParams);
      summary = files.join(", ") || "Updated files";
      kind = "file";
    } else if (itemType === "tool") {
      title = "Tool";
      summary =
        (item as CommandItem).name ||
        (item as CommandItem).tool ||
        (item as CommandItem).id ||
        "Tool call";
      kind = "tool";
    } else if (itemType === "agentMessage") {
      title = "Agent";
      summary = (item as CommandItem).text || "Agent message";
      kind = "output";
    } else {
      title = itemType ? `Item ${itemType}` : "Item completed";
      summary = (item as CommandItem).text || (item as CommandItem).message || "";
    }
  } else if (method === "item/commandExecution/requestApproval") {
    title = "Command approval";
    summary = extractCommand(item as CommandItem, params) || "Approval requested";
    kind = "command";
  } else if (method === "item/fileChange/requestApproval") {
    title = "File approval";
    const files = extractFiles(params);
    summary = files.join(", ") || "Approval requested";
    kind = "file";
  } else if (method === "turn/completed") {
    title = "Turn completed";
    summary = params.status || "completed";
    kind = "status";
  } else if (method === "error") {
    title = "Error";
    summary = extractErrorMessage(params) || "App-server error";
    kind = "error";
  } else if (method.includes("outputDelta")) {
    title = "Output";
    summary = params.delta || params.text || "";
    kind = "output";
  } else if (params.delta) {
    title = "Delta";
    summary = params.delta;
  }

  const entry: TicketChatEvent = {
    id: (payload as EventPayload)?.id || `${Date.now()}`,
    title,
    summary: summary || "(no details)",
    detail,
    kind,
    time: receivedAt,
    itemId,
    method,
  };
  addTicketEvent(state, entry);
  if (itemId) state.eventItemIndex[itemId] = state.events.length - 1;
}

/**
 * Render the ticket chat events list.
 * Shows agent activity (thinking, tool calls, etc.) during processing.
 */
export function renderTicketEvents(): void {
  const els = getTicketChatElements();
  if (!els.eventsMain || !els.eventsList || !els.eventsCount) return;

  const state = ticketChatState;
  const hasEvents = state.events.length > 0;
  const isRunning = state.status === "running";
  const showEvents = hasEvents || isRunning;

  els.eventsMain.classList.toggle("hidden", !showEvents);
  els.eventsCount.textContent = String(state.events.length);
  if (!showEvents) return;

  const limit = TICKET_CHAT_EVENT_LIMIT;
  const expanded = !!state.eventsExpanded;
  const showCount = expanded ? state.events.length : Math.min(state.events.length, limit);
  const visible = state.events.slice(-showCount);

  if (els.eventsToggle) {
    const hiddenCount = Math.max(0, state.events.length - showCount);
    els.eventsToggle.classList.toggle("hidden", hiddenCount === 0);
    els.eventsToggle.textContent = expanded ? "Show recent" : `Show more (${hiddenCount})`;
  }

  els.eventsList.innerHTML = "";

  if (!hasEvents) {
    if (isRunning) {
      const empty = document.createElement("div");
      empty.className = "ticket-chat-events-empty ticket-chat-events-waiting";
      empty.textContent = "Processing...";
      els.eventsList.appendChild(empty);
    }
    return;
  }

  visible.forEach((entry) => {
    const wrapper = document.createElement("div");
    wrapper.className = `ticket-chat-event ${entry.kind || ""}`.trim();

    const title = document.createElement("div");
    title.className = "ticket-chat-event-title";
    title.textContent = entry.title || entry.method || "Update";

    const summary = document.createElement("div");
    summary.className = "ticket-chat-event-summary";
    summary.textContent = entry.summary || "(no details)";

    wrapper.appendChild(title);
    wrapper.appendChild(summary);

    if (entry.detail) {
      const detail = document.createElement("div");
      detail.className = "ticket-chat-event-detail";
      detail.textContent = entry.detail;
      wrapper.appendChild(detail);
    }

    const meta = document.createElement("div");
    meta.className = "ticket-chat-event-meta";
    meta.textContent = entry.time
      ? new Date(entry.time).toLocaleTimeString([], {
          hour: "2-digit",
          minute: "2-digit",
        })
      : "";
    wrapper.appendChild(meta);

    els.eventsList.appendChild(wrapper);
  });

  // Auto-scroll events list to bottom
  els.eventsList.scrollTop = els.eventsList.scrollHeight;
}

/**
 * Render the ticket chat messages history.
 * Shows user prompts and assistant responses with clear role labels.
 */
export function renderTicketMessages(): void {
  const els = getTicketChatElements();
  if (!els.messagesEl) return;

  const state = ticketChatState;
  els.messagesEl.innerHTML = "";

  if (state.messages.length === 0) {
    return;
  }

  state.messages.forEach((msg) => {
    const wrapper = document.createElement("div");
    const roleClass = msg.role === "user" ? "user" : "assistant";
    const finalClass = msg.isFinal ? "final" : "thinking";
    wrapper.className = `ticket-chat-message ${roleClass} ${finalClass}`.trim();

    // Add role label for clear differentiation
    const roleLabel = document.createElement("div");
    roleLabel.className = "ticket-chat-message-role";
    if (msg.role === "user") {
      roleLabel.textContent = "You";
    } else {
      // Show different label for thinking vs final response
      roleLabel.textContent = msg.isFinal ? "Response" : "Thinking";
    }
    wrapper.appendChild(roleLabel);

    const content = document.createElement("div");
    content.className = "ticket-chat-message-content";
    content.textContent = msg.content;
    wrapper.appendChild(content);

    const meta = document.createElement("div");
    meta.className = "ticket-chat-message-meta";
    const time = msg.time ? new Date(msg.time) : new Date();
    meta.textContent = time.toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
    });
    wrapper.appendChild(meta);

    els.messagesEl!.appendChild(wrapper);
  });

  // Auto-scroll messages to bottom
  els.messagesEl.scrollTop = els.messagesEl.scrollHeight;
}

/**
 * Initialize event handlers for the ticket chat events UI.
 */
export function initTicketChatEvents(): void {
  const els = getTicketChatElements();

  // Toggle events expansion
  if (els.eventsToggle) {
    els.eventsToggle.addEventListener("click", () => {
      ticketChatState.eventsExpanded = !ticketChatState.eventsExpanded;
      renderTicketEvents();
    });
  }
}
