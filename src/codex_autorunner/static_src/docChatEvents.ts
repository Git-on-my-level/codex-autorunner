import { chatUI } from "./docsElements.js";
import {
  CHAT_EVENT_LIMIT,
  CHAT_EVENT_MAX,
  getActiveDoc,
  type ChatState,
} from "./docsState.js";

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

interface ChatEventEntry {
  id: string;
  title: string;
  summary: string;
  detail: string;
  kind: string;
  time: number;
  itemId: string | null;
  method: string;
}

function extractCommand(
  item: CommandItem | null | undefined,
  params: PayloadParams | null | undefined,
): string {
  const command = item?.command ?? params?.command;
  if (Array.isArray(command)) {
    return command.map((part) => String(part)).join(" ").trim();
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
  for (const key of ["files", "fileChanges", "paths"] as Array<
    keyof PayloadParams
  >) {
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

export function extractOutputDelta(payload: unknown): string {
  const message =
    payload && typeof payload === "object"
      ? (payload as EventPayload).message || payload
      : payload;
  if (!message || typeof message !== "object") return "";
  const method = String(
    (message as EventMessage).method || "",
  ).toLowerCase();
  if (!method.includes("outputdelta")) return "";
  const params = (message as EventMessage).params || {};
  if (typeof params.delta === "string") return params.delta;
  if (typeof params.text === "string") return params.text;
  if (typeof params.output === "string") return params.output;
  return "";
}

function addChatEvent(state: ChatState, entry: ChatEventEntry): void {
  state.events.push(entry);
  if (state.events.length > CHAT_EVENT_MAX) {
    state.events = state.events.slice(-CHAT_EVENT_MAX);
    state.eventItemIndex = {};
    state.events.forEach((evt, idx) => {
      const chatEvent = evt as ChatEventEntry;
      if (chatEvent.itemId) state.eventItemIndex[chatEvent.itemId] = idx;
    });
  }
}

export function applyAppServerEvent(
  state: ChatState,
  payload: EventPayload | unknown,
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

  if (method === "item/reasoning/summaryTextDelta") {
    const delta = params.delta || "";
    if (!delta) return;
    const existingIndex =
      itemId && state.eventItemIndex[itemId] !== undefined
        ? state.eventItemIndex[itemId] as number
        : null;
    if (existingIndex !== null) {
      const existing = state.events[existingIndex] as ChatEventEntry;
      existing.summary = `${existing.summary || ""}${delta}`;
      existing.time = receivedAt;
      return;
    }
    const entry: ChatEventEntry = {
      id: (payload as EventPayload)?.id || `${Date.now()}`,
      title: "Thinking",
      summary: delta,
      detail: "",
      kind: "thinking",
      time: receivedAt,
      itemId,
      method,
    };
    addChatEvent(state, entry);
    if (itemId) state.eventItemIndex[itemId] = state.events.length - 1;
    return;
  }

  if (method === "item/reasoning/summaryPartAdded") {
    const existingIndex =
      itemId && state.eventItemIndex[itemId] !== undefined
        ? state.eventItemIndex[itemId] as number
        : null;
    if (existingIndex !== null) {
      const existing = state.events[existingIndex] as ChatEventEntry;
      existing.summary = `${existing.summary || ""}\n\n`;
      existing.time = receivedAt;
    }
    return;
  }

  let title = method;
  let summary = "";
  let detail = "";
  let kind = "event";

  if (method === "item/completed") {
    const itemType = (item as CommandItem).type;
    if (itemType === "commandExecution") {
      title = "Command";
      summary = extractCommand(item as CommandItem, params);
      kind = "command";
      if ((item as CommandItem).exitCode !== undefined && (item as CommandItem).exitCode !== null) {
        detail = `exit ${(item as CommandItem).exitCode}`;
      }
    } else if (itemType === "fileChange") {
      title = "File change";
      const files = extractFiles(item as PayloadParams);
      summary = files.join(", ") || "Updated files";
      kind = "file";
    } else if (itemType === "tool") {
      title = "Tool";
      summary = (item as CommandItem).name || (item as CommandItem).tool || (item as CommandItem).id || "Tool call";
      kind = "command";
    } else if (itemType === "agentMessage") {
      title = "Agent";
      summary = (item as CommandItem).text || "Agent message";
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
  } else if (params.delta) {
    title = "Delta";
    summary = params.delta;
  }

  const entry: ChatEventEntry = {
    id: (payload as EventPayload)?.id || `${Date.now()}`,
    title,
    summary: summary || "(no details)",
    detail,
    kind,
    time: receivedAt,
    itemId,
    method,
  };
  addChatEvent(state, entry);
  if (itemId) state.eventItemIndex[itemId] = state.events.length - 1;
}

export function renderChatEvents(state: ChatState): void {
  if (getActiveDoc() === "snapshot") return;
  if (!chatUI.eventsMain || !chatUI.eventsList || !chatUI.eventsCount) return;
  const hasEvents = state.events.length > 0;
  const isRunning = state.status === "running";
  const showEvents = hasEvents || isRunning;
  chatUI.eventsMain.classList.toggle("hidden", !showEvents);
  chatUI.eventsCount.textContent = String(state.events.length);
  if (!showEvents) return;

  const limit = CHAT_EVENT_LIMIT;
  const expanded = !!state.eventsExpanded;
  const showCount = expanded
    ? state.events.length
    : Math.min(state.events.length, limit);
  const visible = state.events.slice(-showCount);

  if (chatUI.eventsToggle) {
    const hiddenCount = Math.max(0, state.events.length - showCount);
    chatUI.eventsToggle.classList.toggle("hidden", hiddenCount === 0);
    chatUI.eventsToggle.textContent = expanded
      ? "Show recent"
      : `Show more (${hiddenCount})`;
  }

  chatUI.eventsList.innerHTML = "";
  if (state.eventError) {
    const error = document.createElement("div");
    error.className = "doc-chat-event error";
    const title = document.createElement("div");
    title.className = "doc-chat-event-title";
    title.textContent = "Event stream error";
    const summary = document.createElement("div");
    summary.className = "doc-chat-event-summary";
    summary.textContent = state.eventError;
    error.appendChild(title);
    error.appendChild(summary);
    chatUI.eventsList.appendChild(error);
  }
  if (!hasEvents) {
    const empty = document.createElement("div");
    empty.className = "doc-chat-events-empty";
    empty.textContent = isRunning ? "Waiting for updates..." : "No updates yet.";
    chatUI.eventsList.appendChild(empty);
    return;
  }

  visible.forEach((entry) => {
    const chatEvent = entry as ChatEventEntry;
    const wrapper = document.createElement("div");
    wrapper.className = `doc-chat-event ${chatEvent.kind || ""}`.trim();

    const title = document.createElement("div");
    title.className = "doc-chat-event-title";
    title.textContent = chatEvent.title || chatEvent.method || "Update";

    const summary = document.createElement("div");
    summary.className = "doc-chat-event-summary";
    summary.textContent = chatEvent.summary || "(no details)";

    wrapper.appendChild(title);
    wrapper.appendChild(summary);

    if (chatEvent.detail) {
      const detail = document.createElement("div");
      detail.className = "doc-chat-event-detail";
      detail.textContent = chatEvent.detail;
      wrapper.appendChild(detail);
    }

    const meta = document.createElement("div");
    meta.className = "doc-chat-event-meta";
    meta.textContent = chatEvent.time
      ? new Date(chatEvent.time).toLocaleTimeString([], {
          hour: "2-digit",
          minute: "2-digit",
        })
      : "";
    wrapper.appendChild(meta);

    chatUI.eventsList.appendChild(wrapper);
  });
}
