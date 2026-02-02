import { parseAppServerEvent, type ParsedAgentEvent, type AgentEvent } from "./agentEvents.js";
import { summarizeEvents, renderCompactSummary, COMPACT_MAX_ACTIONS, COMPACT_MAX_TEXT_LENGTH } from "./eventSummarizer.js";
import { saveChatHistory, loadChatHistory, type ChatStorageConfig } from "./docChatStorage.js";
import { renderMarkdown } from "./messages.js";

export type ChatStatus = "idle" | "running" | "done" | "error" | "interrupted";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  time: string;
  isFinal: boolean;
  meta?: {
    steps?: number;
    duration?: number;
  };
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
  startTime?: number;
  controller: AbortController | null;
  draft: unknown | null;
  events: ChatEvent[];
  messages: ChatMessage[];
  eventItemIndex: Record<string, number>;
  eventsExpanded: boolean;
  contextUsagePercent: number | null;
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
  inlineEvents?: boolean;
  /**
   * If true, the events panel only appears while the chat is running.
   * (Useful for UIs that want “agent progress” inline during the run, but not as a persistent top panel.)
   */
  eventsOnlyWhileRunning?: boolean;
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
  addAssistantMessage(content: string, isFinal?: boolean, meta?: ChatMessage["meta"]): void;
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

function buildMessage(role: "user" | "assistant", content: string, isFinal: boolean, meta?: ChatMessage["meta"]): ChatMessage {
  return {
    id: `${role}-${Date.now()}`,
    role,
    content,
    time: new Date().toISOString(),
    isFinal,
    meta,
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
    contextUsagePercent: null,
  };

  const elements = getElements(config.idPrefix);

  function decorateFileLinks(root: HTMLElement): void {
    const links = Array.from(root.querySelectorAll("a")) as HTMLAnchorElement[];
    for (const link of links) {
      const href = link.getAttribute("href") || "";
      if (!href) continue;
      // Only decorate PMA file links.
      if (!href.includes("/hub/pma/files/")) continue;

      link.classList.add("pma-file-link");
      link.setAttribute("download", "");
      // Ensure downloads happen in-place (no new tab).
      link.removeAttribute("target");
      link.setAttribute("rel", "noopener");
      if (!link.title) link.title = "Download file";
    }
  }

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

  function addAssistantMessage(content: string, isFinal = true, meta?: ChatMessage["meta"]): void {
    if (!content) return;
    const last = state.messages[state.messages.length - 1];
    if (last && last.role === "assistant" && last.content === content) {
        if (meta) last.meta = meta;
        return;
    }
    state.messages.push(buildMessage("assistant", content, isFinal, meta));
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

    // If inlineEvents is enabled, we don't render to the separate events container
    if (config.inlineEvents) {
      // Still need to calculate showEvents to hide the container properly
      // but return early before modifying innerHTML
      if (eventsMain) eventsMain.classList.add("hidden");
      return;
    }

    const hasEvents = state.events.length > 0;
    const isRunning = state.status === "running";
    const showEvents = config.eventsOnlyWhileRunning ? isRunning : (hasEvents || isRunning);
    const compactMode = !!config.compactMode;
    const expanded = !!state.eventsExpanded;

    if (config.styling.eventsHiddenClass) {
      eventsMain.classList.toggle(config.styling.eventsHiddenClass, !showEvents);
    } else {
      // In inline mode, never show the main event container since we render inline
      if (config.inlineEvents) {
        eventsMain.classList.add("hidden");
      } else {
        eventsMain.classList.toggle("hidden", !showEvents);
      }
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
      contextUsagePercent: state.contextUsagePercent ?? undefined,
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
      content.className = `${config.styling.messageContentClass} messages-markdown`;
      // Use markdown rendering for assistant messages.
      // For user messages, keep plain text unless the message includes PMA file links
      // (used for "uploaded file" pills).
      const shouldRenderMarkdown =
        msg.role === "assistant" ||
        msg.content.includes("/hub/pma/files/") ||
        msg.content.includes("/api/filebox/") ||
        msg.content.includes("/hub/filebox/");
      if (shouldRenderMarkdown) {
        content.innerHTML = renderMarkdown(msg.content);
        decorateFileLinks(content);
      } else {
        content.textContent = msg.content;
      }
      wrapper.appendChild(content);

      const meta = document.createElement("div");
      meta.className = config.styling.messageMetaClass;
      const time = msg.time ? new Date(msg.time) : new Date();
      let metaText = time.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      
      if (msg.meta) {
        const parts = [];
        if (msg.meta.steps) parts.push(`${msg.meta.steps} steps`);
        if (msg.meta.duration) parts.push(`${msg.meta.duration.toFixed(1)}s`);
        if (state.contextUsagePercent !== null && msg.isFinal) {
          parts.push(`ctx ${state.contextUsagePercent}%`);
        }
        if (parts.length) metaText += ` · ${parts.join(" · ")}`;
      }
      
      meta.textContent = metaText;
      wrapper.appendChild(meta);

      messagesEl.appendChild(wrapper);
    });

    // While running, show an inline "Thinking" bubble at the bottom where the
    // final assistant message will appear (even if we don't have streamed text yet).
    if (hasStream || state.status === "running") {
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
      content.className = `${config.styling.messageContentClass} messages-markdown`;

      // If we have streamed text, show it. Otherwise show a compact "working" summary
      // based on the most recent event/tool-call.
      if (state.streamText) {
        content.innerHTML = renderMarkdown(state.streamText);
        decorateFileLinks(content);
      } else {
        const stepCount = state.events.length;
        const statusText = (state.statusText || "").trim();
        const isNoiseEvent = (evt: ChatEvent): boolean => {
          const title = (evt.title || "").toLowerCase();
          const method = (evt.method || "").toLowerCase();
          // Hide token/partial deltas; they are too granular for the UI.
          if (title === "delta") return true;
          if (method.includes("delta")) return true;
          return false;
        };

        const meaningfulEvents = state.events.filter((evt) => !isNoiseEvent(evt));
        const lastMeaningful = meaningfulEvents[meaningfulEvents.length - 1];
        const headline = lastMeaningful
          ? (lastMeaningful.title || lastMeaningful.summary || statusText || "Working...")
          : (statusText || "Thinking...");

        // Build DOM so we can attach a "Show details" toggle inside the Thinking bubble.
        content.innerHTML = "";

        const header = document.createElement("div");
        header.className = "chat-thinking-inline";

        const spinner = document.createElement("span");
        spinner.className = "chat-thinking-spinner";
        header.appendChild(spinner);

        const headlineSpan = document.createElement("span");
        headlineSpan.textContent = String(headline);
        header.appendChild(headlineSpan);

        if (stepCount > 0) {
          const steps = document.createElement("span");
          steps.className = "chat-thinking-steps";
          steps.textContent = `(${stepCount} steps)`;
          header.appendChild(steps);

          if (state.contextUsagePercent !== null) {
            const context = document.createElement("span");
            context.className = "chat-thinking-steps";
            context.textContent = ` · ctx ${state.contextUsagePercent}%`;
            header.appendChild(context);
          }

          // Only show the toggle if we have more than a couple steps.
          if (meaningfulEvents.length > 2) {
            const toggle = document.createElement("button");
            toggle.type = "button";
            toggle.className = "ghost sm chat-thinking-details-btn";
            toggle.textContent = state.eventsExpanded ? "Hide details" : "Show details";
            toggle.addEventListener("click", (e) => {
              e.preventDefault();
              state.eventsExpanded = !state.eventsExpanded;
              renderMessages();
            });
            header.appendChild(toggle);
          }
        }

        content.appendChild(header);

        const maxRecent = state.eventsExpanded
          ? Math.min(meaningfulEvents.length, config.limits.eventVisible || 20)
          : 3;
        const recentEvents = meaningfulEvents.slice(-maxRecent);
        if (recentEvents.length) {
          const list = document.createElement("ul");
          list.className = "chat-thinking-steps-list";
          for (const evt of recentEvents) {
            const li = document.createElement("li");

            const title = document.createElement("span");
            title.className = "chat-thinking-step-title";
            title.textContent = (evt.title || evt.kind || evt.method || "step").trim();
            li.appendChild(title);

            const summaryText = (evt.summary || "").trim();
            if (summaryText) {
              const summary = document.createElement("span");
              summary.className = "chat-thinking-step-summary";
              summary.textContent = ` — ${summaryText}`;
              li.appendChild(summary);
            }

            list.appendChild(li);
          }
          content.appendChild(list);
        }
      }
      
      streaming.appendChild(content);

      messagesEl.appendChild(streaming);
    }

    messagesEl.scrollTop = messagesEl.scrollHeight;
    
    // Also scroll the parent container if it exists
    if (elements.streamEl) {
        elements.streamEl.scrollTop = elements.streamEl.scrollHeight;
    }
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
      // In inline mode, we always want to show the stream element if there's any activity
      // or history, because the "Thinking" state is rendered as a message in the history list
      // (technically in the messagesEl container), but we need the parent container visible.
      const hasContent =
        state.events.length > 0 ||
        state.messages.length > 0 ||
        !!state.streamText ||
        state.status === "running";
      streamEl.classList.toggle("hidden", !hasContent);
      // Auto-scroll to bottom when new content appears
      streamEl.scrollTop = streamEl.scrollHeight;
    }

    // Important: renderMessages handles the "Thinking" bubble creation
    // when state.status === 'running' or we have a streamText.
    // However, if we only have events but no streamText yet, we need to ensure
    // renderMessages is called with a "virtual" stream state to trigger the bubble.
    // We do this by checking if we are running.
    
    // We need to pass a flag or rely on state.status in renderMessages?
    // Actually renderMessages uses state.streamText. 
    // Let's force a "pending" indicator in renderMessages if running but no text.
    
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
