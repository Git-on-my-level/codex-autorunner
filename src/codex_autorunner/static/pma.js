// GENERATED FILE - do not edit directly. Source: static_src/
/**
 * PMA (Project Management Agent) - Hub-level chat interface
 */
import { api, resolvePath, getAuthToken, escapeHtml, flash } from "./utils.js";
import { createDocChat, } from "./docChatCore.js";
import { clearAgentSelectionStorage, getSelectedAgent, getSelectedModel, getSelectedReasoning, initAgentControls, refreshAgentControls, } from "./agentControls.js";
import { createFileBoxWidget } from "./fileboxUi.js";
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
    // PMA should show agent progress inside the "Thinking" bubble, not in a standalone panel.
    inlineEvents: true,
};
let pmaChat = null;
let currentController = null;
let currentOutboxBaseline = null;
let queuedTickerId = null;
let queuedSinceMs = null;
let isUnloading = false;
let unloadHandlerInstalled = false;
let currentEventsController = null;
const PMA_PENDING_TURN_KEY = "car.pma.pendingTurn";
let fileBoxCtrl = null;
let pendingUploadNames = [];
function newClientTurnId() {
    // crypto.randomUUID is not guaranteed everywhere; keep a safe fallback.
    try {
        if (typeof crypto !== "undefined" && "randomUUID" in crypto && typeof crypto.randomUUID === "function") {
            return crypto.randomUUID();
        }
    }
    catch {
        // ignore
    }
    return `pma-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}
function loadPendingTurn() {
    try {
        const raw = localStorage.getItem(PMA_PENDING_TURN_KEY);
        if (!raw)
            return null;
        const parsed = JSON.parse(raw);
        if (!parsed || typeof parsed !== "object")
            return null;
        if (!parsed.clientTurnId || !parsed.message || !parsed.startedAtMs)
            return null;
        return parsed;
    }
    catch {
        return null;
    }
}
function savePendingTurn(turn) {
    try {
        localStorage.setItem(PMA_PENDING_TURN_KEY, JSON.stringify(turn));
    }
    catch {
        // ignore
    }
}
function clearPendingTurn() {
    try {
        localStorage.removeItem(PMA_PENDING_TURN_KEY);
    }
    catch {
        // ignore
    }
}
async function initFileBoxUI() {
    const elements = getElements();
    if (!elements.inboxFiles || !elements.outboxFiles)
        return;
    fileBoxCtrl = createFileBoxWidget({
        scope: "pma",
        basePath: "/hub/pma/files",
        inboxEl: elements.inboxFiles,
        outboxEl: elements.outboxFiles,
        uploadInput: elements.chatUploadInput,
        uploadBtn: elements.chatUploadBtn,
        refreshBtn: elements.outboxRefresh,
        uploadBox: "inbox",
        emptyMessage: "No files",
        onChange: (listing) => {
            if (pendingUploadNames.length && pmaChat) {
                const links = pendingUploadNames
                    .map((name) => {
                    const match = listing.inbox.find((e) => e.name === name);
                    const href = match?.url ? resolvePath(match.url) : "";
                    const text = escapeMarkdownLinkText(name);
                    return href ? `[${text}](${href})` : text;
                })
                    .join("\n");
                if (links) {
                    pmaChat.addUserMessage(`**Inbox files (uploaded):**\n${links}`);
                    pmaChat.render();
                }
                pendingUploadNames = [];
            }
        },
        onUpload: (names) => {
            pendingUploadNames = names;
        },
    });
    await fileBoxCtrl.refresh();
}
function stopTurnEventsStream() {
    if (currentEventsController) {
        try {
            currentEventsController.abort();
        }
        catch {
            // ignore
        }
        currentEventsController = null;
    }
}
function sleep(ms) {
    return new Promise((resolve) => window.setTimeout(resolve, ms));
}
async function startTurnEventsStream(meta) {
    stopTurnEventsStream();
    if (!meta.threadId || !meta.turnId)
        return;
    const ctrl = new AbortController();
    currentEventsController = ctrl;
    const token = getAuthToken();
    const headers = {};
    if (token)
        headers.Authorization = `Bearer ${token}`;
    const url = resolvePath(`/hub/pma/turns/${encodeURIComponent(meta.turnId)}/events?thread_id=${encodeURIComponent(meta.threadId)}&agent=${encodeURIComponent(meta.agent || "codex")}`);
    try {
        const res = await fetch(url, {
            method: "GET",
            headers,
            signal: ctrl.signal,
        });
        if (!res.ok)
            return;
        const contentType = res.headers.get("content-type") || "";
        if (!contentType.includes("text/event-stream"))
            return;
        await readPMAStream(res);
    }
    catch {
        // ignore (abort / network)
    }
}
async function pollForTurnMeta(clientTurnId, timeoutMs = 8000) {
    if (!clientTurnId)
        return;
    const started = Date.now();
    while (Date.now() - started < timeoutMs) {
        if (!pmaChat || pmaChat.state.status !== "running")
            return;
        if (currentEventsController)
            return;
        try {
            const payload = (await api(`/hub/pma/active?client_turn_id=${encodeURIComponent(clientTurnId)}`, { method: "GET" }));
            const cur = (payload.current || {});
            const threadId = typeof cur.thread_id === "string" ? cur.thread_id : "";
            const turnId = typeof cur.turn_id === "string" ? cur.turn_id : "";
            const agent = typeof cur.agent === "string" ? cur.agent : "codex";
            if (threadId && turnId) {
                void startTurnEventsStream({ agent, threadId, turnId });
                return;
            }
        }
        catch {
            // ignore and retry
        }
        await sleep(250);
    }
}
function clearQueuedTicker() {
    if (queuedTickerId !== null) {
        window.clearInterval(queuedTickerId);
        queuedTickerId = null;
    }
    queuedSinceMs = null;
}
function startQueuedTicker() {
    if (!pmaChat)
        return;
    if (queuedTickerId !== null)
        return;
    if (!queuedSinceMs)
        queuedSinceMs = Date.now();
    queuedTickerId = window.setInterval(() => {
        if (!pmaChat) {
            clearQueuedTicker();
            return;
        }
        if (pmaChat.state.status !== "running") {
            clearQueuedTicker();
            return;
        }
        const status = (pmaChat.state.statusText || "").toLowerCase();
        if (status !== "queued") {
            // Once we transition away from queued, stop the ticker.
            clearQueuedTicker();
            return;
        }
        const elapsed = Math.max(0, Math.floor((Date.now() - (queuedSinceMs || Date.now())) / 1000));
        pmaChat.state.statusText = `waiting to start (${elapsed}s)`;
        pmaChat.render();
    }, 500);
}
function getElements() {
    return {
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
        pausedRunsBar: document.getElementById("pma-paused-runs"),
        agentSelect: document.getElementById("pma-chat-agent-select"),
        modelSelect: document.getElementById("pma-chat-model-select"),
        reasoningSelect: document.getElementById("pma-chat-reasoning-select"),
        inboxList: document.getElementById("pma-inbox-list"),
        inboxRefresh: document.getElementById("pma-inbox-refresh"),
        chatUploadInput: document.getElementById("pma-chat-upload-input"),
        chatUploadBtn: document.getElementById("pma-chat-upload-btn"),
        inboxFiles: document.getElementById("pma-inbox-files"),
        outboxFiles: document.getElementById("pma-outbox-files"),
        outboxRefresh: document.getElementById("pma-outbox-refresh"),
    };
}
const decoder = new TextDecoder();
function escapeMarkdownLinkText(text) {
    // Keep this ES2019-compatible (no String.prototype.replaceAll).
    return text.replace(/\[/g, "\\[").replace(/\]/g, "\\]");
}
function formatOutboxAttachments(listing, names) {
    if (!listing || !names.length)
        return "";
    const lines = names.map((name) => {
        const entry = listing.outbox.find((e) => e.name === name);
        const href = entry?.url ? new URL(resolvePath(entry.url), window.location.origin).toString() : "";
        const label = escapeMarkdownLinkText(name);
        return href ? `[${label}](${href})` : label;
    });
    return lines.length ? `**Outbox files (download):**\n${lines.join("\n")}` : "";
}
async function finalizePMAResponse(responseText) {
    if (!pmaChat)
        return;
    let attachments = "";
    try {
        if (fileBoxCtrl) {
            const current = await fileBoxCtrl.refresh();
            if (currentOutboxBaseline) {
                const baseline = currentOutboxBaseline;
                const added = (current.outbox || []).map((e) => e.name).filter((name) => !baseline.has(name));
                attachments = formatOutboxAttachments(current, added);
            }
        }
    }
    catch {
        attachments = "";
    }
    finally {
        currentOutboxBaseline = null;
        clearPendingTurn();
        stopTurnEventsStream();
    }
    const trimmed = (responseText || "").trim();
    const content = trimmed
        ? (attachments ? `${trimmed}\n\n---\n\n${attachments}` : trimmed)
        : attachments;
    const startTime = pmaChat.state.startTime;
    const duration = startTime ? (Date.now() - startTime) / 1000 : undefined;
    const steps = pmaChat.state.events.length;
    if (content) {
        pmaChat.addAssistantMessage(content, true, { steps, duration });
    }
    pmaChat.state.streamText = "";
    pmaChat.state.status = "done";
    pmaChat.render();
    pmaChat.renderMessages();
    pmaChat.renderEvents();
    void fileBoxCtrl?.refresh();
}
async function initPMA() {
    const elements = getElements();
    if (!elements.shell)
        return;
    pmaChat = createDocChat(pmaConfig);
    pmaChat.setTarget("pma");
    pmaChat.render();
    // Ensure we start at the bottom
    setTimeout(() => {
        const stream = document.getElementById("pma-chat-stream");
        const messages = document.getElementById("pma-chat-messages");
        if (stream)
            stream.scrollTop = stream.scrollHeight;
        if (messages)
            messages.scrollTop = messages.scrollHeight;
    }, 100);
    initAgentControls({
        agentSelect: elements.agentSelect,
        modelSelect: elements.modelSelect,
        reasoningSelect: elements.reasoningSelect,
    });
    await refreshAgentControls({ force: true, reason: "initial" });
    await loadPMAInbox();
    await initFileBoxUI();
    attachHandlers();
    // If we refreshed mid-turn, recover the final output from the server.
    await resumePendingTurn();
    // If the page refreshes/navigates while a turn is running, avoid showing a noisy
    // "network error" and proactively interrupt the running turn on the server to
    // prevent the next request from receiving a stale/previous response.
    if (!unloadHandlerInstalled) {
        unloadHandlerInstalled = true;
        window.addEventListener("beforeunload", () => {
            isUnloading = true;
            clearQueuedTicker();
            // Abort any in-flight request immediately.
            if (currentController) {
                try {
                    currentController.abort();
                }
                catch {
                    // ignore
                }
            }
        });
    }
    // Periodically refresh inbox
    setInterval(() => {
        void loadPMAInbox();
        void fileBoxCtrl?.refresh();
    }, 30000);
}
async function loadPMAInbox() {
    const elements = getElements();
    if (!elements.inboxList)
        return;
    try {
        const payload = (await api("/hub/messages", { method: "GET" }));
        const items = payload?.items || [];
        if (!items.length) {
            elements.inboxList.innerHTML = "";
            elements.pausedRunsBar?.classList.add("hidden");
            return;
        }
        const html = items
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
        elements.pausedRunsBar?.classList.remove("hidden");
    }
    catch (_err) {
        elements.inboxList.innerHTML = '<div class="muted">Failed to load inbox</div>';
        elements.pausedRunsBar?.classList.remove("hidden");
    }
}
async function sendMessage() {
    const elements = getElements();
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
    const clientTurnId = newClientTurnId();
    savePendingTurn({ clientTurnId, message, startedAtMs: Date.now() });
    currentController = new AbortController();
    pmaChat.state.controller = currentController;
    pmaChat.state.status = "running";
    pmaChat.state.error = "";
    pmaChat.state.streamText = "";
    pmaChat.state.startTime = Date.now();
    pmaChat.clearEvents();
    pmaChat.addUserMessage(message);
    pmaChat.render();
    pmaChat.renderMessages();
    try {
        try {
            const listing = fileBoxCtrl ? await fileBoxCtrl.refresh() : null;
            const names = listing?.outbox?.map((e) => e.name) || [];
            currentOutboxBaseline = new Set(names);
        }
        catch {
            currentOutboxBaseline = new Set();
        }
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
            client_turn_id: clientTurnId,
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
        // Stream tool calls/events separately as soon as we have (thread_id, turn_id).
        // The main /hub/pma/chat stream only emits a final "update"/"done" today.
        void pollForTurnMeta(clientTurnId);
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
        // Aborts (including page refresh) shouldn't create an error message that pollutes history.
        const name = err && typeof err === "object" && "name" in err
            ? String(err.name || "")
            : "";
        if (isUnloading || name === "AbortError") {
            pmaChat.state.status = "interrupted";
            pmaChat.state.error = "";
            pmaChat.state.statusText = isUnloading ? "Cancelled (page reload)" : "Cancelled";
            pmaChat.render();
            return;
        }
        const errorMsg = err.message || "Request failed";
        pmaChat.state.status = "error";
        pmaChat.state.error = errorMsg;
        pmaChat.addAssistantMessage(`Error: ${errorMsg}`, true);
        pmaChat.render();
        pmaChat.renderMessages();
        clearPendingTurn();
        stopTurnEventsStream();
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
            if ((status || "").toLowerCase() === "queued") {
                queuedSinceMs = queuedSinceMs ?? Date.now();
                startQueuedTicker();
            }
            else {
                clearQueuedTicker();
            }
            pmaChat.render();
            pmaChat.renderEvents();
            break;
        }
        case "token": {
            clearQueuedTicker();
            const token = typeof parsed === "string"
                ? parsed
                : parsed.token ||
                    parsed.text ||
                    rawData ||
                    "";
            pmaChat.state.streamText = (pmaChat.state.streamText || "") + token;
            // Force status to "responding" if we have tokens, so the stream loop picks it up
            if (!pmaChat.state.statusText || pmaChat.state.statusText === "queued") {
                pmaChat.state.statusText = "responding";
            }
            // Ensure we're in "running" state if receiving tokens
            if (pmaChat.state.status !== "running") {
                pmaChat.state.status = "running";
            }
            pmaChat.render();
            break;
        }
        case "event":
        case "app-server": {
            if (pmaChat) {
                clearQueuedTicker();
                // Ensure we're in "running" state if receiving events
                if (pmaChat.state.status !== "running") {
                    pmaChat.state.status = "running";
                }
                // If we are receiving events but still show "queued", bump status so UI
                // reflects progress even before token streaming starts.
                if (!pmaChat.state.statusText || pmaChat.state.statusText === "queued") {
                    pmaChat.state.statusText = "working";
                }
                pmaChat.applyAppEvent(parsed);
                pmaChat.renderEvents();
                // Force a full render to update the inline thinking indicator
                pmaChat.render();
            }
            break;
        }
        case "token_usage": {
            // Token usage events - context window usage
            if (typeof parsed === "object" && parsed !== null) {
                const usage = parsed;
                const totalTokens = usage.totalTokens;
                const modelContextWindow = usage.modelContextWindow;
                if (totalTokens !== undefined && modelContextWindow !== undefined && modelContextWindow > 0) {
                    const percentRemaining = Math.round(((modelContextWindow - totalTokens) / modelContextWindow) * 100);
                    const percentRemainingClamped = Math.max(0, Math.min(100, percentRemaining));
                    // Store context usage for display in chat UI
                    if (pmaChat) {
                        pmaChat.state.contextUsagePercent = percentRemainingClamped;
                        pmaChat.render();
                    }
                }
            }
            break;
        }
        case "error": {
            clearQueuedTicker();
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
            clearQueuedTicker();
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
        case "update": {
            const data = typeof parsed === "string" ? {} : parsed;
            // If server echoes client_turn_id, we can clear pending when we receive the final payload.
            if (data.client_turn_id) {
                clearPendingTurn();
            }
            if (data.message) {
                pmaChat.state.streamText = data.message;
            }
            pmaChat.render();
            break;
        }
        case "done":
        case "finish": {
            clearQueuedTicker();
            void finalizePMAResponse(pmaChat.state.streamText || "");
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
async function resumePendingTurn() {
    const pending = loadPendingTurn();
    if (!pending || !pmaChat)
        return;
    // Show a running indicator immediately.
    pmaChat.state.status = "running";
    pmaChat.state.statusText = "Recovering previous turn…";
    pmaChat.render();
    pmaChat.renderMessages();
    const poll = async () => {
        try {
            const payload = (await api(`/hub/pma/active?client_turn_id=${encodeURIComponent(pending.clientTurnId)}`, { method: "GET" }));
            const cur = (payload.current || {});
            const threadId = typeof cur.thread_id === "string" ? cur.thread_id : "";
            const turnId = typeof cur.turn_id === "string" ? cur.turn_id : "";
            const agent = typeof cur.agent === "string" ? cur.agent : "codex";
            if (threadId && turnId && !currentEventsController) {
                void startTurnEventsStream({ agent, threadId, turnId });
            }
            const last = (payload.last_result || {});
            const status = String(last.status || "");
            if (status === "ok" && typeof last.message === "string") {
                await finalizePMAResponse(last.message);
                return;
            }
            if (status === "error") {
                const detail = String(last.detail || "PMA chat failed");
                pmaChat.state.status = "error";
                pmaChat.state.error = detail;
                pmaChat.addAssistantMessage(`Error: ${detail}`, true);
                pmaChat.render();
                pmaChat.renderMessages();
                clearPendingTurn();
                stopTurnEventsStream();
                return;
            }
            if (status === "interrupted") {
                pmaChat.state.status = "interrupted";
                pmaChat.state.error = "";
                pmaChat.addAssistantMessage("Request interrupted", true);
                pmaChat.render();
                pmaChat.renderMessages();
                clearPendingTurn();
                stopTurnEventsStream();
                return;
            }
            // Still running; keep polling.
            pmaChat.state.status = "running";
            pmaChat.state.statusText = "Recovering previous turn…";
            pmaChat.render();
            window.setTimeout(() => void poll(), 1000);
        }
        catch {
            // If recovery fails, don't spam errors; just stop trying.
            pmaChat.state.statusText = "Recovering previous turn…";
            pmaChat.render();
        }
    };
    await poll();
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
    if (result.message) {
        pmaChat.state.streamText = result.message;
    }
    const responseText = (pmaChat.state.streamText || pmaChat.state.statusText || "Done");
    void finalizePMAResponse(responseText);
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
    stopTurnEventsStream();
    if (pmaChat) {
        pmaChat.state.controller = null;
        pmaChat.state.status = "interrupted";
        pmaChat.state.statusText = "Cancelled";
        pmaChat.render();
    }
}
function resetThread() {
    cancelRequest();
    clearPendingTurn();
    stopTurnEventsStream();
    if (pmaChat) {
        pmaChat.state.messages = [];
        pmaChat.state.events = [];
        pmaChat.state.eventItemIndex = {};
        pmaChat.state.error = "";
        pmaChat.state.streamText = "";
        pmaChat.state.statusText = "";
        pmaChat.state.status = "idle";
        pmaChat.state.contextUsagePercent = null;
        pmaChat.render();
        pmaChat.renderMessages();
    }
    flash("Thread reset", "info");
}
async function resetThreadOnServer() {
    const elements = getElements();
    const agent = elements.agentSelect?.value || getSelectedAgent();
    const resetAgent = (agent || "").trim() || "all";
    await api("/hub/pma/thread/reset", {
        method: "POST",
        body: { agent: resetAgent },
    });
}
function attachHandlers() {
    const elements = getElements();
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
            void (async () => {
                cancelRequest();
                try {
                    await resetThreadOnServer();
                }
                catch (err) {
                    flash("Failed to reset server thread", "error");
                    return;
                }
                clearAgentSelectionStorage();
                await refreshAgentControls({ force: true, reason: "manual" });
                resetThread();
            })();
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
            void fileBoxCtrl?.refresh();
        });
    }
    if (elements.outboxRefresh) {
        elements.outboxRefresh.addEventListener("click", () => {
            void fileBoxCtrl?.refresh();
        });
    }
}
export { initPMA };
