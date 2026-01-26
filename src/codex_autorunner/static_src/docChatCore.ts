import { parseAppServerEvent, type ParsedAgentEvent, type AgentEvent } from "./agentEvents.js";
import { summarizeEvents, renderCompactSummary, COMPACT_MAX_ACTIONS, COMPACT_MAX_TEXT_LENGTH } from "./eventSummarizer.js";
import { saveChatHistory, loadChatHistory, type ChatStorageConfig } from "./docChatStorage.js";

export type ChatStatus = "idle" | "running" | "done" | "error" | "interrupted";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  time: string;
  isFinal: boolean;
}

export interface ChatEvent {
  id: string;
  title: string;
  summary: string;
  detail: string;
  kind: string;
  time: number;
  itemId: string | null;
  method: string;
}

export interface ChatState {
  status: ChatStatus;
  target: string | null;
  error: string;
  streamText: string;
  statusText: string;
  controller: AbortController | null;
  draft: unknown | null;
  events: ChatEvent[];
  messages: ChatMessage[];
  eventItemIndex: Record<string, number>;
  eventsExpanded: boolean;
}

export interface ChatElements {
  input: HTMLTextAreaElement | null;
  sendBtn: HTMLButtonElement | null;
  voiceBtn: HTMLButtonElement | null;
  cancelBtn: HTMLButtonElement | null;
  newThreadBtn: HTMLButtonElement | null;
  statusEl: HTMLElement | null;
  errorEl: HTMLElement | null;
  streamEl: HTMLElement | null;
  eventsMain: HTMLElement | null;
  eventsList: HTMLElement | null;
  eventsCount: HTMLElement | null;
  eventsToggle: HTMLButtonElement | null;
  messagesEl: HTMLElement | null;
  historyHeader: HTMLElement | null;
  voiceStatus: HTMLElement | null;
}

export interface ChatStyling {
  eventClass: string;
  eventTitleClass: string;
  eventSummaryClass: string;
  eventDetailClass: string;
  eventMetaClass: string;
  eventsEmptyClass: string;
  eventsWaitingClass?: string;
  eventsHiddenClass?: string;
  messagesClass: string;
  messageRoleClass: string;
  messageContentClass: string;
  messageMetaClass: string;
  messageUserClass: string;
  messageAssistantClass: string;
  messageAssistantThinkingClass?: string;
  messageAssistantFinalClass?: string;
}

export interface ChatLimits {
  eventVisible: number;
  eventMax: number;
}

export interface ChatConfig {
  idPrefix: string;
  storage?: ChatStorageConfig;
  limits: ChatLimits;
  styling: ChatStyling;
  compactMode?: boolean;
  compactOptions?: { maxActions?: number; maxTextLength?: number };
}

export interface DocChatInstance {
  state: ChatState;
  elements: ChatElements;
  render(): void;
  renderMessages(): void;
  renderEvents(): void;
  renderCompactEvents(): void;
  clearEvents(): void;
  applyAppEvent(payload: unknown): void;
  addUserMessage(content: string): void;
  addAssistantMessage(content: string, isFinal?: boolean): void;
  setTarget(target: string | null): void;
}

function getElements(prefix: string): ChatElements {
  return {
    input: document.getElementById(`${prefix}-input`) as HTMLTextAreaElement | null,
    sendBtn: document.getElementById(`${prefix}-send`) as HTMLButtonElement | null,
    voiceBtn: document.getElementById(`${prefix}-voice`) as HTMLButtonElement | null,
    cancelBtn: document.getElementById(`${prefix}-cancel`) as HTMLButtonElement | null,
    newThreadBtn: document.getElementById(`${prefix}-new-thread`) as HTMLButtonElement | null,
    statusEl: document.getElementById(`${prefix}-status`),
    errorEl: document.getElementById(`${prefix}-error`),
    streamEl: document.getElementById(`${prefix}-stream`),
    eventsMain: document.getElementById(`${prefix}-events`),
    eventsList: document.getElementById(`${prefix}-events-list`),
    eventsCount: document.getElementById(`${prefix}-events-count`),
    eventsToggle: document.getElementById(`${prefix}-events-toggle`) as HTMLButtonElement | null,
    messagesEl: document.getElementById(`${prefix}-messages`) ||
      (document.getElementById(`${prefix}-history`) as HTMLElement | null),
    historyHeader: document.getElementById(`${prefix}-history-header`),
    voiceStatus: document.getElementById(`${prefix}-voice-status`),
  };
}

function addEvent(state: ChatState, entry: ChatEvent, limits: ChatLimits): void {
  state.events.push(entry);
  if (state.events.length > limits.eventMax) {
    state.events = state.events.slice(-limits.eventMax);
    state.eventItemIndex = {};
    state.events.forEach((evt, idx) => {
      if (evt.itemId) state.eventItemIndex[evt.itemId] = idx;
    });
  }
}

function buildMessage(role: "user" | "assistant", content: string, isFinal: boolean): ChatMessage {
  return {
    id: `${role}-${Date.now()}`,
    role,
    content,
    time: new Date().toISOString(),
    isFinal,
  };
}

export function createDocChat(config: ChatConfig): DocChatInstance {
  const state: ChatState = {
    status: "idle",
    target: null,
    error: "",
    streamText: "",
    statusText: "",
    controller: null,
    draft: null,
    events: [],
    messages: [],
    eventItemIndex: {},
    eventsExpanded: false,
  };

  const elements = getElements(config.idPrefix);

  function saveHistory(): void {
    if (!config.storage || !state.target) return;
    saveChatHistory(config.storage, state.target, state.messages);
  }

  function loadHistory(): void {
    if (!config.storage || !state.target) {
      state.messages = [];
      return;
    }
    state.messages = loadChatHistory(config.storage, state.target);
  }

  function setTarget(target: string | null): void {
    state.target = target;
    loadHistory();
    clearEvents();
    render();
  }

  function addUserMessage(content: string): void {
    state.messages.push(buildMessage("user", content, true));
    saveHistory();
  }

  function addAssistantMessage(content: string, isFinal = true): void {
    if (!content) return;
    const last = state.messages[state.messages.length - 1];
    if (last && last.role === "assistant" && last.content === content) return;
    state.messages.push(buildMessage("assistant", content, isFinal));
    saveHistory();
  }

  function clearEvents(): void {
    state.events = [];
    state.eventItemIndex = {};
  }

  function applyAppEvent(payload: unknown): void {
    const parsed = parseAppServerEvent(payload) as ParsedAgentEvent | null;
    if (!parsed) return;
    const { event, mergeStrategy } = parsed;
    const itemId = event.itemId;

    if (mergeStrategy && itemId && state.eventItemIndex[itemId] !== undefined) {
      const existingIndex = state.eventItemIndex[itemId] as number;
      const existing = state.events[existingIndex];
      if (mergeStrategy === "append") {
        existing.summary = `${existing.summary || ""}${event.summary}`;
      } else if (mergeStrategy === "newline") {
        existing.summary = `${existing.summary || ""}\n\n`;
      }
      existing.time = event.time;
      return;
    }

    addEvent(state, { ...event }, config.limits);
    if (itemId) state.eventItemIndex[itemId] = state.events.length - 1;
  }

  function renderEvents(): void {
    const { eventsMain, eventsList, eventsCount, eventsToggle } = elements;
    if (!eventsMain || !eventsList || !eventsCount) return;

    const hasEvents = state.events.length > 0;
    const isRunning = state.status === "running";
    const showEvents = hasEvents || isRunning;
    const compactMode = !!config.compactMode;
    const expanded = !!state.eventsExpanded;

    if (config.styling.eventsHiddenClass) {
      eventsMain.classList.toggle(config.styling.eventsHiddenClass, !showEvents);
    } else {
      eventsMain.classList.toggle("hidden", !showEvents);
    }

    eventsCount.textContent = String(state.events.length);
    if (!showEvents) {
      eventsList.innerHTML = "";
      return;
    }

    if (compactMode && !expanded) {
      renderCompactEvents();
      if (eventsToggle) {
        eventsToggle.classList.toggle("hidden", !hasEvents);
        eventsToggle.textContent = "Show details";
      }
      return;
    }

    const limit = config.limits.eventVisible;
    const showCount = compactMode ? state.events.length : expanded ? state.events.length : Math.min(state.events.length, limit);
    const visible = state.events.slice(-showCount);

    if (eventsToggle) {
      if (compactMode) {
        eventsToggle.classList.toggle("hidden", !hasEvents);
        eventsToggle.textContent = "Show compact";
      } else {
        const hiddenCount = Math.max(0, state.events.length - showCount);
        eventsToggle.classList.toggle("hidden", hiddenCount === 0);
        eventsToggle.textContent = expanded ? "Show recent" : `Show more (${hiddenCount})`;
      }
    }

    eventsList.innerHTML = "";

    if (!hasEvents && isRunning) {
      const empty = document.createElement("div");
      empty.className =
        config.styling.eventsWaitingClass || config.styling.eventsEmptyClass || "chat-events-empty";
      empty.textContent = "Processing...";
      eventsList.appendChild(empty);
      return;
    }

    visible.forEach((entry) => {
      const wrapper = document.createElement("div");
      wrapper.className = `${config.styling.eventClass} ${entry.kind || ""}`.trim();

      const title = document.createElement("div");
      title.className = config.styling.eventTitleClass;
      title.textContent = entry.title || entry.method || "Update";

      wrapper.appendChild(title);
      if (entry.summary) {
        const summary = document.createElement("div");
        summary.className = config.styling.eventSummaryClass;
        summary.textContent = entry.summary;
        wrapper.appendChild(summary);
      }

      if (entry.detail) {
        const detail = document.createElement("div");
        detail.className = config.styling.eventDetailClass;
        detail.textContent = entry.detail;
        wrapper.appendChild(detail);
      }

      const meta = document.createElement("div");
      meta.className = config.styling.eventMetaClass;
      meta.textContent = entry.time
        ? new Date(entry.time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
        : "";
      wrapper.appendChild(meta);

      eventsList.appendChild(wrapper);
    });

    eventsList.scrollTop = eventsList.scrollHeight;
  }

  function renderCompactEvents(): void {
    const { eventsList } = elements;
    if (!eventsList) return;
    eventsList.innerHTML = "";
    const summary = summarizeEvents(state.events as unknown as AgentEvent[], {
      maxActions: config.compactOptions?.maxActions ?? COMPACT_MAX_ACTIONS,
      maxTextLength: config.compactOptions?.maxTextLength ?? COMPACT_MAX_TEXT_LENGTH,
    });
    const text = state.events.length ? renderCompactSummary(summary) : "";
    const wrapper = document.createElement("pre");
    wrapper.className = "chat-events-compact";
    wrapper.textContent = text || (state.status === "running" ? "Processing..." : "No events yet.");
    eventsList.appendChild(wrapper);
  }

  function renderMessages(): void {
    const { messagesEl, historyHeader } = elements;
    if (!messagesEl) return;
    messagesEl.innerHTML = "";

    const hasMessages = state.messages.length > 0;
    const hasStream = !!state.streamText;

    if (historyHeader) {
      historyHeader.classList.toggle("hidden", !(hasMessages || hasStream));
    }
    messagesEl.classList.toggle("chat-history-empty", !(hasMessages || hasStream));

    if (!hasMessages && !hasStream) {
      return;
    }

    state.messages.forEach((msg) => {
      const wrapper = document.createElement("div");
      const roleClass = msg.role === "user" ? config.styling.messageUserClass : config.styling.messageAssistantClass;
      const finalClass = msg.role === "assistant"
        ? (msg.isFinal ? config.styling.messageAssistantFinalClass : config.styling.messageAssistantThinkingClass)
        : "";
      wrapper.className = [config.styling.messagesClass, roleClass, finalClass].filter(Boolean).join(" ").trim();

      const roleLabel = document.createElement("div");
      roleLabel.className = config.styling.messageRoleClass;
      if (msg.role === "user") {
        roleLabel.textContent = "You";
      } else {
        roleLabel.textContent = msg.isFinal ? "Response" : "Thinking";
      }
      wrapper.appendChild(roleLabel);

      const content = document.createElement("div");
      content.className = config.styling.messageContentClass;
      content.textContent = msg.content;
      wrapper.appendChild(content);

      const meta = document.createElement("div");
      meta.className = config.styling.messageMetaClass;
      const time = msg.time ? new Date(msg.time) : new Date();
      meta.textContent = time.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      wrapper.appendChild(meta);

      messagesEl.appendChild(wrapper);
    });

    if (hasStream) {
      const streaming = document.createElement("div");
      streaming.className = [
        config.styling.messagesClass,
        config.styling.messageAssistantClass,
        config.styling.messageAssistantThinkingClass || "",
      ]
        .filter(Boolean)
        .join(" ")
        .trim();

      const roleLabel = document.createElement("div");
      roleLabel.className = config.styling.messageRoleClass;
      roleLabel.textContent = "Thinking";
      streaming.appendChild(roleLabel);

      const content = document.createElement("div");
      content.className = config.styling.messageContentClass;
      content.textContent = state.streamText;
      streaming.appendChild(content);

      messagesEl.appendChild(streaming);
    }

    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function render(): void {
    const {
      statusEl,
      errorEl,
      cancelBtn,
      newThreadBtn,
      streamEl,
    } = elements;

    if (statusEl) {
      const status = state.error ? "error" : state.statusText || state.status;
      statusEl.textContent = status;
      statusEl.classList.toggle("error", !!state.error || state.status === "error");
      statusEl.classList.toggle("running", state.status === "running");
    }

    if (errorEl) {
      errorEl.textContent = state.error || "";
      errorEl.classList.toggle("hidden", !state.error);
    }

    if (cancelBtn) {
      cancelBtn.classList.toggle("hidden", state.status !== "running");
    }

    if (newThreadBtn) {
      const hasHistory = state.messages.length > 0;
      newThreadBtn.classList.toggle("hidden", !hasHistory || state.status === "running");
    }

    if (streamEl) {
      const hasContent =
        state.events.length > 0 ||
        state.messages.length > 0 ||
        !!state.streamText ||
        state.status === "running";
      streamEl.classList.toggle("hidden", !hasContent);
    }

    renderEvents();
    renderMessages();
  }

  // wire toggle
  if (elements.eventsToggle) {
    elements.eventsToggle.addEventListener("click", () => {
      state.eventsExpanded = !state.eventsExpanded;
      renderEvents();
    });
  }

  return {
    state,
    elements,
    render,
    renderMessages,
    renderEvents,
    renderCompactEvents,
    clearEvents,
    applyAppEvent,
    addUserMessage,
    addAssistantMessage,
    setTarget,
  };
}
