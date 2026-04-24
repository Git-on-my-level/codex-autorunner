// GENERATED FILE - do not edit directly. Source: static_src/
import { parseAppServerEvent, resetOpenCodeEventState } from "./agentEvents.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
import { summarizeEvents, renderCompactSummary, COMPACT_MAX_ACTIONS, COMPACT_MAX_TEXT_LENGTH } from "./eventSummarizer.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
import { saveChatHistory, loadChatHistory } from "./docChatStorage.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
import { renderMarkdown } from "./markdown.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
function getElements(prefix) {
    return {
        input: document.getElementById(`${prefix}-input`),
        sendBtn: document.getElementById(`${prefix}-send`),
        voiceBtn: document.getElementById(`${prefix}-voice`),
        cancelBtn: document.getElementById(`${prefix}-cancel`),
        newThreadBtn: document.getElementById(`${prefix}-new-thread`),
        statusEl: document.getElementById(`${prefix}-status`),
        errorEl: document.getElementById(`${prefix}-error`),
        streamEl: document.getElementById(`${prefix}-stream`),
        eventsMain: document.getElementById(`${prefix}-events`),
        eventsList: document.getElementById(`${prefix}-events-list`),
        eventsCount: document.getElementById(`${prefix}-events-count`),
        eventsToggle: document.getElementById(`${prefix}-events-toggle`),
        messagesEl: document.getElementById(`${prefix}-messages`) ||
            document.getElementById(`${prefix}-history`),
        historyHeader: document.getElementById(`${prefix}-history-header`),
        voiceStatus: document.getElementById(`${prefix}-voice-status`),
    };
}
function addEvent(state, entry, limits) {
    state.events.push(entry);
    if (state.events.length > limits.eventMax) {
        state.events = state.events.slice(-limits.eventMax);
        state.eventItemIndex = {};
        state.events.forEach((evt, idx) => {
            if (evt.itemId)
                state.eventItemIndex[evt.itemId] = idx;
        });
    }
}
function buildMessage(role, content, isFinal, meta) {
    return {
        id: `${role}-${Date.now()}`,
        role,
        content,
        time: new Date().toISOString(),
        isFinal,
        meta,
    };
}
export function createDocChat(config) {
    const state = {
        status: "idle",
        target: null,
        error: "",
        streamText: "",
        statusText: "",
        controller: null,
        draft: null,
        events: [],
        totalEvents: 0,
        messages: [],
        eventItemIndex: {},
        eventsExpanded: false,
        contextUsagePercent: null,
    };
    const elements = getElements(config.idPrefix);
    function decorateFileLinks(root) {
        const links = Array.from(root.querySelectorAll("a"));
        for (const link of links) {
            const href = link.getAttribute("href") || "";
            if (!href)
                continue;
            // Only decorate PMA file links.
            if (!href.includes("/hub/pma/files/"))
                continue;
            link.classList.add("pma-file-link");
            link.setAttribute("download", "");
            // Ensure downloads happen in-place (no new tab).
            link.removeAttribute("target");
            link.setAttribute("rel", "noopener");
            if (!link.title)
                link.title = "Download file";
        }
    }
    function saveHistory() {
        if (!config.storage || !state.target)
            return;
        saveChatHistory(config.storage, state.target, state.messages);
    }
    function loadHistory() {
        if (!config.storage || !state.target) {
            state.messages = [];
            return;
        }
        state.messages = loadChatHistory(config.storage, state.target);
    }
    function setTarget(target) {
        state.target = target;
        loadHistory();
        clearEvents();
        render();
    }
    function addUserMessage(content) {
        state.messages.push(buildMessage("user", content, true));
        saveHistory();
    }
    function addAssistantMessage(content, isFinal = true, meta) {
        if (!content)
            return;
        const last = state.messages[state.messages.length - 1];
        if (last && last.role === "assistant" && last.content === content) {
            if (meta)
                last.meta = meta;
            return;
        }
        state.messages.push(buildMessage("assistant", content, isFinal, meta));
        saveHistory();
    }
    function clearEvents() {
        state.events = [];
        state.totalEvents = 0;
        state.eventItemIndex = {};
        _prevMessageSnapshot = "";
        _streamingEl = null;
        resetOpenCodeEventState();
    }
    function applyAppEvent(payload) {
        const parsed = parseAppServerEvent(payload);
        if (!parsed)
            return;
        const { event, mergeStrategy } = parsed;
        const itemId = event.itemId;
        if (mergeStrategy && itemId && state.eventItemIndex[itemId] !== undefined) {
            const existingIndex = state.eventItemIndex[itemId];
            const existing = state.events[existingIndex];
            if (mergeStrategy === "append") {
                existing.summary = `${existing.summary || ""}${event.summary}`;
            }
            else if (mergeStrategy === "newline") {
                existing.summary = `${existing.summary || ""}\n\n`;
            }
            else if (mergeStrategy === "replace") {
                existing.summary = event.summary;
            }
            existing.time = event.time;
            return;
        }
        addEvent(state, { ...event }, config.limits);
        state.totalEvents += 1;
        if (itemId)
            state.eventItemIndex[itemId] = state.events.length - 1;
    }
    function renderEvents() {
        const { eventsMain, eventsList, eventsCount, eventsToggle } = elements;
        if (!eventsMain || !eventsList || !eventsCount)
            return;
        // If inlineEvents is enabled, we don't render to the separate events container
        if (config.inlineEvents) {
            // Still need to calculate showEvents to hide the container properly
            // but return early before modifying innerHTML
            if (eventsMain)
                eventsMain.classList.add("hidden");
            return;
        }
        const hasEvents = state.events.length > 0;
        const isRunning = state.status === "running";
        const showEvents = config.eventsOnlyWhileRunning ? isRunning : (hasEvents || isRunning);
        const compactMode = !!config.compactMode;
        const expanded = !!state.eventsExpanded;
        if (config.styling.eventsHiddenClass) {
            eventsMain.classList.toggle(config.styling.eventsHiddenClass, !showEvents);
        }
        else {
            // In inline mode, never show the main event container since we render inline
            if (config.inlineEvents) {
                eventsMain.classList.add("hidden");
            }
            else {
                eventsMain.classList.toggle("hidden", !showEvents);
            }
        }
        const eventCount = state.totalEvents || state.events.length;
        eventsCount.textContent = String(eventCount);
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
            }
            else {
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
    function renderCompactEvents() {
        const { eventsList } = elements;
        if (!eventsList)
            return;
        eventsList.innerHTML = "";
        const summary = summarizeEvents(state.events, {
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
    let _prevMessageSnapshot = "";
    let _streamingEl = null;
    function isNearBottom(el) {
        if (!el)
            return true;
        return el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    }
    function scrollToBottom(el) {
        if (el)
            el.scrollTop = el.scrollHeight;
    }
    function userMessageUsesMarkdownFeatures(body) {
        if (body.includes("/hub/pma/files/"))
            return true;
        if (body.includes("```") || body.includes("`") || body.includes("**"))
            return true;
        if (/^\s*[-*]\s/m.test(body))
            return true;
        return /\[[^\]]+\]\([^)]+\)/.test(body);
    }
    function buildMessageEl(msg) {
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
        }
        else {
            roleLabel.textContent = msg.isFinal ? "Response" : "Thinking";
        }
        wrapper.appendChild(roleLabel);
        const content = document.createElement("div");
        content.className = `${config.styling.messageContentClass} messages-markdown`;
        const shouldRenderMarkdown = msg.role === "assistant" || (msg.role === "user" && userMessageUsesMarkdownFeatures(msg.content));
        if (shouldRenderMarkdown) {
            content.innerHTML = renderMarkdown(msg.content);
            decorateFileLinks(content);
        }
        else {
            content.textContent = msg.content;
        }
        wrapper.appendChild(content);
        const meta = document.createElement("div");
        meta.className = config.styling.messageMetaClass;
        const time = msg.time ? new Date(msg.time) : new Date();
        let metaText = time.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
        if (msg.meta) {
            const parts = [];
            if (msg.meta.steps)
                parts.push(`${msg.meta.steps} steps`);
            if (msg.meta.duration)
                parts.push(`${msg.meta.duration.toFixed(1)}s`);
            if (msg.meta.tag)
                parts.push(String(msg.meta.tag));
            if (state.contextUsagePercent !== null && msg.isFinal) {
                parts.push(`ctx left ${state.contextUsagePercent}%`);
            }
            if (parts.length)
                metaText += ` · ${parts.join(" · ")}`;
        }
        meta.textContent = metaText;
        wrapper.appendChild(meta);
        return wrapper;
    }
    function buildStreamingEl() {
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
        if (state.streamText) {
            content.innerHTML = renderMarkdown(state.streamText);
            decorateFileLinks(content);
        }
        else {
            const stepCount = state.totalEvents || state.events.length;
            const statusText = (state.statusText || "").trim();
            const isNoiseEvent = (evt) => {
                const title = (evt.title || "").toLowerCase();
                const method = (evt.method || "").toLowerCase();
                if (title === "delta")
                    return true;
                if (method.includes("delta"))
                    return true;
                return false;
            };
            const meaningfulEvents = state.events.filter((evt) => !isNoiseEvent(evt));
            const lastMeaningful = meaningfulEvents[meaningfulEvents.length - 1];
            const headline = lastMeaningful
                ? (lastMeaningful.title || lastMeaningful.summary || statusText || "Working...")
                : (statusText || "Thinking...");
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
                    context.textContent = ` · ctx left ${state.contextUsagePercent}%`;
                    header.appendChild(context);
                }
                if (meaningfulEvents.length > 2) {
                    const toggle = document.createElement("button");
                    toggle.type = "button";
                    toggle.className = "ghost sm chat-thinking-details-btn";
                    toggle.textContent = state.eventsExpanded ? "Hide details" : "Show details";
                    toggle.addEventListener("click", (e) => {
                        e.preventDefault();
                        state.eventsExpanded = !state.eventsExpanded;
                        _prevMessageSnapshot = "";
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
        return streaming;
    }
    function renderMessages() {
        const { messagesEl, historyHeader } = elements;
        if (!messagesEl)
            return;
        const hasMessages = state.messages.length > 0;
        const hasStream = !!state.streamText;
        const isStreaming = hasStream || state.status === "running";
        if (historyHeader) {
            historyHeader.classList.toggle("hidden", !(hasMessages || isStreaming));
        }
        messagesEl.classList.toggle("chat-history-empty", !(hasMessages || isStreaming));
        if (!hasMessages && !isStreaming) {
            if (messagesEl.innerHTML !== "") {
                messagesEl.innerHTML = "";
                _prevMessageSnapshot = "";
                _streamingEl = null;
            }
            return;
        }
        const snapshot = state.messages.map((m) => m.id).join(",");
        const messagesChanged = snapshot !== _prevMessageSnapshot;
        if (messagesChanged) {
            const wasNearBottom = isNearBottom(messagesEl);
            messagesEl.innerHTML = "";
            _prevMessageSnapshot = snapshot;
            _streamingEl = null;
            state.messages.forEach((msg) => {
                messagesEl.appendChild(buildMessageEl(msg));
            });
            if (isStreaming) {
                _streamingEl = buildStreamingEl();
                messagesEl.appendChild(_streamingEl);
            }
            if (wasNearBottom) {
                scrollToBottom(messagesEl);
                scrollToBottom(elements.streamEl);
            }
        }
        else {
            if (isStreaming) {
                if (state.streamText && _streamingEl && messagesEl.contains(_streamingEl)) {
                    const contentEl = _streamingEl.querySelector(`.${config.styling.messageContentClass}`);
                    if (contentEl) {
                        contentEl.innerHTML = renderMarkdown(state.streamText);
                        decorateFileLinks(contentEl);
                    }
                }
                else {
                    if (_streamingEl && messagesEl.contains(_streamingEl)) {
                        _streamingEl.remove();
                    }
                    _streamingEl = buildStreamingEl();
                    messagesEl.appendChild(_streamingEl);
                }
            }
            else if (_streamingEl && messagesEl.contains(_streamingEl)) {
                _streamingEl.remove();
                _streamingEl = null;
            }
            if (isNearBottom(messagesEl)) {
                scrollToBottom(messagesEl);
                scrollToBottom(elements.streamEl);
            }
        }
    }
    function render() {
        const { statusEl, errorEl, cancelBtn, newThreadBtn, streamEl, } = elements;
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
            const hasContent = state.events.length > 0 ||
                state.messages.length > 0 ||
                !!state.streamText ||
                state.status === "running";
            streamEl.classList.toggle("hidden", !hasContent);
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
