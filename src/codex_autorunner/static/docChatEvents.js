import { chatUI } from "./docsElements.js";
import {
  CHAT_EVENT_LIMIT,
  CHAT_EVENT_MAX,
  getActiveDoc,
} from "./docsState.js";

function extractCommand(item, params) {
  const command = item?.command ?? params?.command;
  if (Array.isArray(command)) {
    return command.map((part) => String(part)).join(" ").trim();
  }
  if (typeof command === "string") return command.trim();
  return "";
}

function extractFiles(payload) {
  const files = [];
  const addEntry = (entry) => {
    if (typeof entry === "string" && entry.trim()) {
      files.push(entry.trim());
      return;
    }
    if (entry && typeof entry === "object") {
      const path = entry.path || entry.file || entry.name;
      if (typeof path === "string" && path.trim()) {
        files.push(path.trim());
      }
    }
  };
  if (!payload || typeof payload !== "object") return files;
  for (const key of ["files", "fileChanges", "paths"]) {
    const value = payload[key];
    if (Array.isArray(value)) {
      value.forEach(addEntry);
    }
  }
  for (const key of ["path", "file", "name"]) {
    addEntry(payload[key]);
  }
  return files;
}

function extractErrorMessage(params) {
  if (!params || typeof params !== "object") return "";
  const err = params.error;
  if (err && typeof err === "object") {
    const message = typeof err.message === "string" ? err.message : "";
    const details =
      typeof err.additionalDetails === "string"
        ? err.additionalDetails
        : typeof err.details === "string"
        ? err.details
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

export function extractOutputDelta(payload) {
  const message =
    payload && typeof payload === "object" ? payload.message || payload : payload;
  if (!message || typeof message !== "object") return "";
  const method = String(message.method || "").toLowerCase();
  if (!method.includes("outputdelta")) return "";
  const params = message.params || {};
  if (typeof params.delta === "string") return params.delta;
  if (typeof params.text === "string") return params.text;
  if (typeof params.output === "string") return params.output;
  return "";
}

function addChatEvent(state, entry) {
  state.events.push(entry);
  if (state.events.length > CHAT_EVENT_MAX) {
    state.events = state.events.slice(-CHAT_EVENT_MAX);
    state.eventItemIndex = {};
    state.events.forEach((evt, idx) => {
      if (evt.itemId) state.eventItemIndex[evt.itemId] = idx;
    });
  }
}

export function applyAppServerEvent(state, payload) {
  const message =
    payload && typeof payload === "object" ? payload.message || payload : payload;
  if (!message || typeof message !== "object") return;
  const method = message.method || "app-server";
  const params = message.params || {};
  const item = params.item || {};
  const itemId = params.itemId || item.id || item.itemId || null;
  const receivedAt =
    payload && typeof payload === "object"
      ? payload.received_at || payload.receivedAt || Date.now()
      : Date.now();

  if (method === "item/reasoning/summaryTextDelta") {
    const delta = params.delta || "";
    if (!delta) return;
    const existingIndex =
      itemId && state.eventItemIndex[itemId] !== undefined
        ? state.eventItemIndex[itemId]
        : null;
    if (existingIndex !== null) {
      const existing = state.events[existingIndex];
      existing.summary = `${existing.summary || ""}${delta}`;
      existing.time = receivedAt;
      return;
    }
    const entry = {
      id: payload?.id || `${Date.now()}`,
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
        ? state.eventItemIndex[itemId]
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

  if (method === "item/completed") {
    const itemType = item.type;
    if (itemType === "commandExecution") {
      title = "Command";
      summary = extractCommand(item, params);
      kind = "command";
      if (item.exitCode !== undefined && item.exitCode !== null) {
        detail = `exit ${item.exitCode}`;
      }
    } else if (itemType === "fileChange") {
      title = "File change";
      const files = extractFiles(item);
      summary = files.join(", ") || "Updated files";
      kind = "file";
    } else if (itemType === "tool") {
      title = "Tool";
      summary = item.name || item.tool || item.id || "Tool call";
      kind = "command";
    } else if (itemType === "agentMessage") {
      title = "Agent";
      summary = item.text || "Agent message";
    } else {
      title = itemType ? `Item ${itemType}` : "Item completed";
      summary = item.text || item.message || "";
    }
  } else if (method === "item/commandExecution/requestApproval") {
    title = "Command approval";
    summary = extractCommand(item, params) || "Approval requested";
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

  const entry = {
    id: payload?.id || `${Date.now()}`,
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

export function renderChatEvents(state) {
  if (getActiveDoc() === "snapshot") return;
  if (!chatUI.eventsMain || !chatUI.eventsList || !chatUI.eventsCount) return;
  const hasEvents = state.events.length > 0;
  const isRunning = state.status === "running";
  const showEvents = hasEvents || isRunning;
  chatUI.eventsMain.classList.toggle("hidden", !showEvents);
  chatUI.eventsCount.textContent = state.events.length;
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
    const wrapper = document.createElement("div");
    wrapper.className = `doc-chat-event ${entry.kind || ""}`.trim();

    const title = document.createElement("div");
    title.className = "doc-chat-event-title";
    title.textContent = entry.title || entry.method || "Update";

    const summary = document.createElement("div");
    summary.className = "doc-chat-event-summary";
    summary.textContent = entry.summary || "(no details)";

    wrapper.appendChild(title);
    wrapper.appendChild(summary);

    if (entry.detail) {
      const detail = document.createElement("div");
      detail.className = "doc-chat-event-detail";
      detail.textContent = entry.detail;
      wrapper.appendChild(detail);
    }

    const meta = document.createElement("div");
    meta.className = "doc-chat-event-meta";
    meta.textContent = entry.time
      ? new Date(entry.time).toLocaleTimeString([], {
          hour: "2-digit",
          minute: "2-digit",
        })
      : "";
    wrapper.appendChild(meta);

    chatUI.eventsList.appendChild(wrapper);
  });
}
