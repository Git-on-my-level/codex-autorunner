// GENERATED FILE - do not edit directly. Source: static_src/
/**
 * PMA (Project Management Agent) - Hub-level chat interface
 */
import { api, resolvePath, getAuthToken, escapeHtml, flash } from "./utils.js";
import { createDocChat, } from "./docChatCore.js";
import { getSelectedAgent, getSelectedModel, getSelectedReasoning, refreshAgentControls } from "./agentControls.js";
const pmaStyling = {
    eventClass: "chat-event",
    eventTitleClass: "chat-event-title",
    eventSummaryClass: "chat-event-summary",
    eventDetailClass: "chat-event-detail",
    eventMetaClass: "chat-event-meta",
    eventsEmptyClass: "chat-events-empty",
    messagesClass: "chat-message",
    messageRoleClass: "chat-message-role",
    messageContentClass: "chat-message-content",
    messageMetaClass: "chat-message-meta",
    messageUserClass: "chat-message-user",
    messageAssistantClass: "chat-message-assistant",
    messageAssistantThinkingClass: "chat-message-assistant-thinking",
    messageAssistantFinalClass: "chat-message-assistant-final",
};
const pmaConfig = {
    idPrefix: "pma-chat",
    storage: { keyPrefix: "car.pma.", maxMessages: 100, version: 1 },
    limits: {
        eventVisible: 20,
        eventMax: 50,
    },
    styling: pmaStyling,
    compactMode: true,
};
let pmaChat = null;
let currentController = null;
const elements = {
    shell: document.getElementById("pma-shell"),
    input: document.getElementById("pma-chat-input"),
    sendBtn: document.getElementById("pma-chat-send"),
    cancelBtn: document.getElementById("pma-chat-cancel"),
    newThreadBtn: document.getElementById("pma-chat-new-thread"),
    statusEl: document.getElementById("pma-chat-status"),
    errorEl: document.getElementById("pma-chat-error"),
    streamEl: document.getElementById("pma-chat-stream"),
    eventsMain: document.getElementById("pma-chat-events"),
    eventsList: document.getElementById("pma-chat-events-list"),
    eventsToggle: document.getElementById("pma-chat-events-toggle"),
    messagesEl: document.getElementById("pma-chat-messages"),
    historyHeader: document.getElementById("pma-chat-history-header"),
    agentSelect: document.getElementById("pma-chat-agent-select"),
    modelSelect: document.getElementById("pma-chat-model-select"),
    reasoningSelect: document.getElementById("pma-chat-reasoning-select"),
    inboxList: document.getElementById("pma-inbox-list"),
    inboxRefresh: document.getElementById("pma-inbox-refresh"),
};
const decoder = new TextDecoder();
async function initPMA() {
    if (!elements.shell)
        return;
    pmaChat = createDocChat(pmaConfig);
    pmaChat.setTarget("pma");
    pmaChat.render();
    await refreshAgentControls({ force: true, reason: "initial" });
    await loadPMAInbox();
    attachHandlers();
    // Periodically refresh inbox
    setInterval(() => {
        void loadPMAInbox();
    }, 30000);
}
async function loadPMAInbox() {
    if (!elements.inboxList)
        return;
    try {
        const payload = (await api("/hub/messages", { method: "GET" }));
        const items = payload?.items || [];
        const html = !items.length
            ? '<div class="muted">No paused runs</div>'
            : items
                .map((item) => {
                const title = item.message?.title || item.message?.mode || "Message";
                const excerpt = item.message?.body ? item.message.body.slice(0, 160) : "";
                const repoLabel = item.repo_display_name || item.repo_id;
                const href = item.open_url || `/repos/${item.repo_id}/?tab=messages&run_id=${item.run_id}`;
                return `
            <a class="pma-inbox-item" href="${escapeHtml(resolvePath(href))}">
              <div class="pma-inbox-item-header">
                <span class="pma-inbox-repo">${escapeHtml(repoLabel)}</span>
                <span class="pill pill-small pill-warn">paused</span>
              </div>
              <div class="pma-inbox-title">${escapeHtml(title)}</div>
              <div class="pma-inbox-excerpt muted small">${escapeHtml(excerpt)}</div>
            </a>
          `;
            })
                .join("");
        elements.inboxList.innerHTML = html;
    }
    catch (_err) {
        elements.inboxList.innerHTML = '<div class="muted">Failed to load inbox</div>';
    }
}
async function sendMessage() {
    if (!elements.input || !pmaChat)
        return;
    const message = elements.input.value?.trim() || "";
    if (!message)
        return;
    if (currentController) {
        cancelRequest();
        return;
    }
    elements.input.value = "";
    elements.input.style.height = "auto";
    const agent = elements.agentSelect?.value || getSelectedAgent();
    const model = elements.modelSelect?.value || getSelectedModel(agent);
    const reasoning = elements.reasoningSelect?.value || getSelectedReasoning(agent);
    currentController = new AbortController();
    pmaChat.state.controller = currentController;
    pmaChat.state.status = "running";
    pmaChat.state.error = "";
    pmaChat.state.streamText = "";
    pmaChat.clearEvents();
    pmaChat.addUserMessage(message);
    pmaChat.render();
    pmaChat.renderMessages();
    try {
        const endpoint = resolvePath("/hub/pma/chat");
        const headers = {
            "Content-Type": "application/json",
        };
        const token = getAuthToken();
        if (token) {
            headers.Authorization = `Bearer ${token}`;
        }
        const payload = {
            message,
            stream: true,
        };
        if (agent)
            payload.agent = agent;
        if (model)
            payload.model = model;
        if (reasoning)
            payload.reasoning = reasoning;
        const res = await fetch(endpoint, {
            method: "POST",
            headers,
            body: JSON.stringify(payload),
            signal: currentController.signal,
        });
        if (!res.ok) {
            const text = await res.text();
            let detail = text;
            try {
                const parsed = JSON.parse(text);
                detail = parsed.detail || parsed.error || text;
            }
            catch {
                // ignore parse errors
            }
            throw new Error(detail || `Request failed (${res.status})`);
        }
        const contentType = res.headers.get("content-type") || "";
        if (contentType.includes("text/event-stream")) {
            await readPMAStream(res);
        }
        else {
            const responsePayload = contentType.includes("application/json")
                ? await res.json()
                : await res.text();
            applyPMAResult(responsePayload);
        }
    }
    catch (err) {
        const errorMsg = err.message || "Request failed";
        pmaChat.state.status = "error";
        pmaChat.state.error = errorMsg;
        pmaChat.addAssistantMessage(`Error: ${errorMsg}`, true);
        pmaChat.render();
        pmaChat.renderMessages();
    }
    finally {
        currentController = null;
        pmaChat.state.controller = null;
    }
}
async function readPMAStream(res) {
    if (!res.body)
        throw new Error("Streaming not supported in this browser");
    const reader = res.body.getReader();
    let buffer = "";
    let escapedNewlines = false;
    for (;;) {
        const { value, done } = await reader.read();
        if (done)
            break;
        const decoded = decoder.decode(value, { stream: true });
        if (!escapedNewlines) {
            const combined = buffer + decoded;
            if (!combined.includes("\n") && combined.includes("\\n")) {
                escapedNewlines = true;
                buffer = buffer.replace(/\\n(?=event:|data:|\\n)/g, "\n");
            }
        }
        buffer += escapedNewlines
            ? decoded.replace(/\\n(?=event:|data:|\\n)/g, "\n")
            : decoded;
        const chunks = buffer.split("\n\n");
        buffer = chunks.pop() || "";
        for (const chunk of chunks) {
            if (!chunk.trim())
                continue;
            let event = "message";
            const dataLines = [];
            chunk.split("\n").forEach((line) => {
                if (line.startsWith("event:")) {
                    event = line.slice(6).trim();
                }
                else if (line.startsWith("data:")) {
                    dataLines.push(line.slice(5).trimStart());
                }
                else if (line.trim()) {
                    dataLines.push(line);
                }
            });
            if (dataLines.length === 0)
                continue;
            const data = dataLines.join("\n");
            handlePMAStreamEvent(event, data);
        }
    }
}
function handlePMAStreamEvent(event, rawData) {
    const parsed = parseMaybeJson(rawData);
    switch (event) {
        case "status": {
            const status = typeof parsed === "string"
                ? parsed
                : parsed.status || "";
            pmaChat.state.statusText = status;
            pmaChat.render();
            pmaChat.renderEvents();
            break;
        }
        case "token": {
            const token = typeof parsed === "string"
                ? parsed
                : parsed.token ||
                    parsed.text ||
                    rawData ||
                    "";
            pmaChat.state.streamText = (pmaChat.state.streamText || "") + token;
            if (!pmaChat.state.statusText || pmaChat.state.statusText === "queued") {
                pmaChat.state.statusText = "responding";
            }
            pmaChat.render();
            break;
        }
        case "event":
        case "app-server": {
            if (pmaChat) {
                pmaChat.applyAppEvent(parsed);
                pmaChat.renderEvents();
            }
            break;
        }
        case "error": {
            const message = typeof parsed === "object" && parsed !== null
                ? parsed.detail ||
                    parsed.error ||
                    rawData
                : rawData || "PMA chat failed";
            pmaChat.state.status = "error";
            pmaChat.state.error = String(message);
            pmaChat.addAssistantMessage(`Error: ${message}`, true);
            pmaChat.render();
            pmaChat.renderMessages();
            throw new Error(String(message));
        }
        case "interrupted": {
            const message = typeof parsed === "object" && parsed !== null
                ? parsed.detail || rawData
                : rawData || "PMA chat interrupted";
            pmaChat.state.status = "interrupted";
            pmaChat.state.error = "";
            pmaChat.state.statusText = String(message);
            pmaChat.addAssistantMessage("Request interrupted", true);
            pmaChat.render();
            pmaChat.renderMessages();
            break;
        }
        case "done":
        case "finish": {
            pmaChat.state.status = "done";
            pmaChat.render();
            pmaChat.renderMessages();
            pmaChat.renderEvents();
            break;
        }
        default:
            if (typeof parsed === "object" && parsed !== null) {
                const messageObj = parsed;
                if (messageObj.method || messageObj.message) {
                    pmaChat.applyAppEvent(parsed);
                    pmaChat.renderEvents();
                }
            }
            break;
    }
}
function applyPMAResult(payload) {
    if (!payload || typeof payload !== "object")
        return;
    const result = payload;
    if (result.status === "interrupted") {
        pmaChat.state.status = "interrupted";
        pmaChat.state.error = "";
        pmaChat.addAssistantMessage("Request interrupted", true);
        pmaChat.render();
        pmaChat.renderMessages();
        return;
    }
    if (result.status === "error" || result.error) {
        pmaChat.state.status = "error";
        pmaChat.state.error =
            result.detail || result.error || "Chat failed";
        pmaChat.addAssistantMessage(`Error: ${pmaChat.state.error}`, true);
        pmaChat.render();
        pmaChat.renderMessages();
        return;
    }
    pmaChat.state.status = "done";
    if (result.message) {
        pmaChat.state.streamText = result.message;
    }
    const responseText = pmaChat.state.streamText ||
        pmaChat.state.statusText ||
        "Done";
    if (responseText && pmaChat.state.messages.length > 0) {
        const lastMessage = pmaChat.state.messages[pmaChat.state.messages.length - 1];
        if (lastMessage.role === "user") {
            pmaChat.addAssistantMessage(responseText, true);
        }
    }
    pmaChat.render();
    pmaChat.renderMessages();
    pmaChat.renderEvents();
}
function parseMaybeJson(data) {
    try {
        return JSON.parse(data);
    }
    catch {
        return data;
    }
}
function cancelRequest() {
    if (currentController) {
        currentController.abort();
        currentController = null;
    }
    if (pmaChat) {
        pmaChat.state.controller = null;
        pmaChat.state.status = "interrupted";
        pmaChat.state.statusText = "Cancelled";
        pmaChat.render();
    }
}
function resetThread() {
    cancelRequest();
    if (pmaChat) {
        pmaChat.state.messages = [];
        pmaChat.state.events = [];
        pmaChat.state.eventItemIndex = {};
        pmaChat.state.error = "";
        pmaChat.state.streamText = "";
        pmaChat.state.statusText = "";
        pmaChat.state.status = "idle";
        pmaChat.render();
        pmaChat.renderMessages();
    }
    flash("Thread reset", "info");
}
function attachHandlers() {
    if (elements.sendBtn) {
        elements.sendBtn.addEventListener("click", () => {
            void sendMessage();
        });
    }
    if (elements.cancelBtn) {
        elements.cancelBtn.addEventListener("click", () => {
            cancelRequest();
        });
    }
    if (elements.newThreadBtn) {
        elements.newThreadBtn.addEventListener("click", () => {
            resetThread();
        });
    }
    if (elements.input) {
        elements.input.addEventListener("keydown", (e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                void sendMessage();
            }
        });
        elements.input.addEventListener("input", () => {
            if (elements.input) {
                elements.input.style.height = "auto";
                elements.input.style.height = `${elements.input.scrollHeight}px`;
            }
        });
    }
    if (elements.inboxRefresh) {
        elements.inboxRefresh.addEventListener("click", () => {
            void loadPMAInbox();
        });
    }
}
export { initPMA };
