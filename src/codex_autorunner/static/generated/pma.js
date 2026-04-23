// GENERATED FILE - do not edit directly. Source: static_src/
/**
 * PMA (Project Management Agent) - Hub-level chat interface
 */
import { api, resolvePath, getAuthToken, flash, escapeHtml } from "./utils.js";
import { createDocChat, } from "./docChatCore.js";
import { initChatPasteUpload } from "./chatUploads.js";
import { DEFAULT_FILEBOX_BOX, FILEBOX_BOXES, } from "./fileboxCatalog.js";
import { getSelectedAgent, getSelectedProfile, getSelectedModel, getSelectedReasoning, initAgentControls, refreshAgentControls, } from "./agentControls.js";
import { createFileBoxWidget } from "./fileboxUi.js";
import { readEventStream, handleStreamEvent, } from "./streamUtils.js";
import { newClientTurnId as newFileChatTurnId } from "./fileChat.js";
import { initNotificationBell } from "./notificationBell.js";
import { registerAutoRefresh } from "./autoRefresh.js";
import { CONSTANTS } from "./constants.js";
import { createTurnEventsController, cancelActiveTurnAndWait, scheduleRecoveryRetry, createTurnRecoveryTracker, ACTIVE_TURN_RECOVERY_STALE_MESSAGE, } from "./sharedTurnLifecycle.js";
import { loadPendingTurn, savePendingTurn, clearPendingTurn, } from "./turnResume.js";
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
const EDITABLE_DOCS = ["AGENTS.md", "active_context.md"];
let activeContextMaxLines = 200;
const pmaConfig = {
    idPrefix: "pma-chat",
    storage: { keyPrefix: "car.pma.", maxMessages: 100, version: 1 },
    limits: {
        eventVisible: 20,
        eventMax: 50,
    },
    styling: pmaStyling,
    compactMode: true,
    inlineEvents: true,
};
let pmaChat = null;
let currentController = null;
let currentOutboxBaseline = null;
let isUnloading = false;
let unloadHandlerInstalled = false;
const PMA_PENDING_TURN_KEY = "car.pma.pendingTurn";
const PMA_VIEW_KEY = "car.pma.view";
const DEFAULT_PMA_LANE_ID = "pma:default";
let fileBoxCtrl = null;
let pendingUploadNames = [];
let currentDocName = null;
const docsInfo = new Map();
let isSavingDoc = false;
let activeContextAutoPrune = null;
let pendingDeliverySummary = null;
let pmaRefreshCleanup = null;
const turnEventsCtrl = createTurnEventsController();
let latestPhase = null;
let latestGuidance = null;
let latestElapsed = null;
let currentPMATurnToken = 0;
function advancePMATurnToken() {
    currentPMATurnToken += 1;
    return currentPMATurnToken;
}
function shouldAppendAsyncOutboxSummary(finalizedTurnToken, currentTurnToken, chatStatus) {
    return finalizedTurnToken === currentTurnToken && (chatStatus || "") === "done";
}
function loadPMAPendingTurn() {
    return loadPendingTurn(PMA_PENDING_TURN_KEY);
}
function savePMAPendingTurn(turn) {
    savePendingTurn(PMA_PENDING_TURN_KEY, turn);
}
function clearPMAPendingTurn() {
    clearPendingTurn(PMA_PENDING_TURN_KEY);
}
function loadPMAView() {
    const raw = localStorage.getItem(PMA_VIEW_KEY);
    if (raw === "memory")
        return "memory";
    return "chat";
}
function setPMAView(view, options = {}) {
    const elements = getElements();
    const { persist = true } = options;
    if (persist) {
        localStorage.setItem(PMA_VIEW_KEY, view);
    }
    if (elements.shell) {
        elements.shell.setAttribute("data-pma-view", view);
    }
    document.querySelectorAll(".pma-view-btn").forEach((btn) => {
        const isActive = btn.dataset.view === view;
        btn.classList.toggle("active", isActive);
        btn.setAttribute("aria-selected", isActive ? "true" : "false");
    });
    elements.chatSection?.classList.toggle("hidden", view !== "chat");
    elements.docsSection?.classList.toggle("hidden", view !== "memory");
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
        uploadBox: DEFAULT_FILEBOX_BOX,
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
            updateClearButtons(listing);
        },
        onUpload: (names) => {
            pendingUploadNames = names;
        },
    });
    await fileBoxCtrl.refresh();
}
async function startTurnEventsStream(meta) {
    turnEventsCtrl.abort();
    if (!meta.threadId || !meta.turnId)
        return;
    if ((meta.agent || "codex").trim().toLowerCase() === "opencode")
        return;
    const ctrl = new AbortController();
    turnEventsCtrl.current = ctrl;
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
        await readPMAStream(res, false);
    }
    catch {
        // ignore (abort / network)
    }
}
async function loadPMADocs() {
    try {
        const payload = (await api("/hub/pma/docs", { method: "GET" }));
        docsInfo.clear();
        if (payload?.docs) {
            payload.docs.forEach((doc) => {
                docsInfo.set(doc.name, doc);
            });
        }
        activeContextMaxLines =
            typeof payload?.active_context_max_lines === "number"
                ? payload.active_context_max_lines
                : 200;
        activeContextAutoPrune = payload?.active_context_auto_prune || null;
        renderPMADocsMeta();
    }
    catch (err) {
        flash("Failed to load PMA docs", "error");
    }
}
async function loadPMADocContent(name) {
    try {
        const payload = (await api(`/hub/pma/docs/${encodeURIComponent(name)}`, {
            method: "GET",
        }));
        return payload?.content || "";
    }
    catch (err) {
        const content = await bootstrapPMADoc(name);
        if (content) {
            return content;
        }
        flash(`Failed to load ${name}`, "error");
        return "";
    }
}
async function loadPMADocDefaultContent(name, options = {}) {
    try {
        const payload = (await api(`/hub/pma/docs/default/${encodeURIComponent(name)}`, {
            method: "GET",
        }));
        return payload?.content || "";
    }
    catch (err) {
        if (!options.silent) {
            flash(`Failed to load default ${name}`, "error");
        }
        return "";
    }
}
async function bootstrapPMADoc(name) {
    const content = await loadPMADocDefaultContent(name, { silent: true });
    if (!content)
        return "";
    try {
        await api(`/hub/pma/docs/${encodeURIComponent(name)}`, {
            method: "PUT",
            body: { content },
        });
        await loadPMADocs();
        return content;
    }
    catch {
        return content;
    }
}
async function savePMADoc(name, content) {
    if (isSavingDoc)
        return;
    isSavingDoc = true;
    try {
        const payload = (await api(`/hub/pma/docs/${encodeURIComponent(name)}`, {
            method: "PUT",
            body: { content },
        }));
        if (payload?.status === "ok") {
            flash(`Saved ${name}`, "info");
            await loadPMADocs();
        }
    }
    catch (err) {
        flash(`Failed to save ${name}`, "error");
    }
    finally {
        isSavingDoc = false;
    }
}
function renderPMADocsMeta() {
    const metaEl = document.getElementById("pma-docs-meta");
    if (!metaEl)
        return;
    const activeInfo = docsInfo.get("active_context.md");
    if (!activeInfo) {
        metaEl.innerHTML = "";
        return;
    }
    const lineCount = activeInfo.line_count || 0;
    const maxLines = activeContextMaxLines || 200;
    const percent = Math.min(100, Math.round((lineCount / maxLines) * 100));
    const statusClass = percent >= 90 ? "pill-warn" : percent >= 70 ? "pill-caution" : "pill-idle";
    let autoPruneHtml = "";
    const autoPrunedAt = activeContextAutoPrune?.last_auto_pruned_at;
    if (typeof autoPrunedAt === "string" && autoPrunedAt) {
        const before = typeof activeContextAutoPrune?.line_count_before === "number"
            ? String(activeContextAutoPrune.line_count_before)
            : "?";
        const budget = typeof activeContextAutoPrune?.line_budget === "number"
            ? String(activeContextAutoPrune.line_budget)
            : String(maxLines);
        autoPruneHtml = `
      <div class="pma-docs-meta-item">
        <span class="muted">Last auto-prune:</span>
        <span class="pill pill-small pill-caution">${escapeHtml(autoPrunedAt)}</span>
        <span class="muted small">(${escapeHtml(before)} -> ${escapeHtml(budget)} lines)</span>
      </div>
    `;
    }
    metaEl.innerHTML = `
    <div class="pma-docs-meta-item">
      <span class="muted">Active context:</span>
      <span class="pill pill-small ${statusClass}">${lineCount} / ${maxLines} lines</span>
    </div>
    <div class="pma-docs-meta-item">
      <span class="muted">Automation:</span>
      <span class="muted small">See Ops Guide (ABOUT_CAR.md) for subscription/timer recipes.</span>
    </div>
    ${autoPruneHtml}
  `;
}
function switchPMADoc(name) {
    currentDocName = name;
    const tabs = document.querySelectorAll(".pma-docs-tab");
    tabs.forEach((tab) => {
        if (tab instanceof HTMLElement && tab.dataset.doc === name) {
            tab.classList.add("active");
        }
        else {
            tab.classList.remove("active");
        }
    });
    const editor = document.getElementById("pma-docs-editor");
    const resetBtn = document.getElementById("pma-docs-reset");
    const snapshotBtn = document.getElementById("pma-docs-snapshot");
    const saveBtn = document.getElementById("pma-docs-save");
    if (!editor)
        return;
    const isEditable = EDITABLE_DOCS.includes(name);
    editor.readOnly = !isEditable;
    if (resetBtn)
        resetBtn.disabled = name !== "active_context.md";
    if (snapshotBtn)
        snapshotBtn.disabled = name !== "active_context.md";
    if (saveBtn)
        saveBtn.disabled = !isEditable;
    void loadPMADocContent(name).then((content) => {
        editor.value = content;
    });
}
async function snapshotActiveContext() {
    const editor = document.getElementById("pma-docs-editor");
    if (!editor)
        return;
    try {
        const payload = (await api("/hub/pma/context/snapshot", {
            method: "POST",
            body: { reset: true },
        }));
        if (payload?.status !== "ok") {
            throw new Error("snapshot failed");
        }
        const resetContent = await loadPMADocContent("active_context.md");
        editor.value = resetContent;
        const message = payload?.warning
            ? `Active context snapshot saved (${payload.warning})`
            : "Active context snapshot saved";
        flash(message, "info");
        await loadPMADocs();
    }
    catch (err) {
        flash("Failed to snapshot active context", "error");
    }
}
function resetActiveContext() {
    if (!confirm("Reset active context to default?"))
        return;
    const editor = document.getElementById("pma-docs-editor");
    if (!editor)
        return;
    void loadPMADocDefaultContent("active_context.md").then((resetContent) => {
        if (!resetContent)
            return;
        editor.value = resetContent;
        void savePMADoc("active_context.md", resetContent);
    });
}
function sleep(ms) {
    return new Promise((resolve) => window.setTimeout(resolve, ms));
}
async function pollForTurnMeta(clientTurnId, options = {}) {
    if (!clientTurnId)
        return;
    const timeoutMs = options.timeoutMs ?? 8000;
    const started = Date.now();
    while (Date.now() - started < timeoutMs) {
        if (!pmaChat || pmaChat.state.status !== "running")
            return;
        if (turnEventsCtrl.current)
            return;
        if (options.signal?.aborted)
            return;
        try {
            const payload = (await api(`/hub/pma/active?client_turn_id=${encodeURIComponent(clientTurnId)}`, { method: "GET" }));
            const cur = (payload.current || {});
            const threadId = typeof cur.thread_id === "string" ? cur.thread_id : "";
            const turnId = typeof cur.turn_id === "string" ? cur.turn_id : "";
            const agent = typeof cur.agent === "string" ? cur.agent : "codex";
            if (threadId && turnId && agent.trim().toLowerCase() !== "opencode") {
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
function getElements() {
    return {
        shell: document.getElementById("pma-shell"),
        chatSection: document.getElementById("pma-chat-section"),
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
        profileSelect: document.getElementById("pma-chat-profile-select"),
        modelSelect: document.getElementById("pma-chat-model-select"),
        modelInput: document.getElementById("pma-chat-model-input"),
        reasoningSelect: document.getElementById("pma-chat-reasoning-select"),
        chatUploadInput: document.getElementById("pma-chat-upload-input"),
        chatUploadBtn: document.getElementById("pma-chat-upload-btn"),
        inboxFiles: document.getElementById("pma-inbox-files"),
        inboxHint: document.getElementById("pma-inbox-hint"),
        outboxFiles: document.getElementById("pma-outbox-files"),
        outboxRefresh: document.getElementById("pma-outbox-refresh"),
        inboxClear: document.getElementById("pma-inbox-clear"),
        outboxClear: document.getElementById("pma-outbox-clear"),
        threadInfo: document.getElementById("pma-thread-info"),
        threadInfoAgent: document.getElementById("pma-thread-info-agent"),
        threadInfoThreadId: document.getElementById("pma-thread-info-thread-id"),
        threadInfoTurnId: document.getElementById("pma-thread-info-turn-id"),
        threadInfoStatus: document.getElementById("pma-thread-info-status"),
        repoActions: document.getElementById("pma-repo-actions"),
        scanReposBtn: document.getElementById("pma-scan-repos-btn"),
        docsSection: document.getElementById("pma-docs-section"),
        docsEditor: document.getElementById("pma-docs-editor"),
        docsSaveBtn: document.getElementById("pma-docs-save"),
        docsResetBtn: document.getElementById("pma-docs-reset"),
        docsSnapshotBtn: document.getElementById("pma-docs-snapshot"),
    };
}
function escapeMarkdownLinkText(text) {
    // Keep this ES2019-compatible (no String.prototype.replaceAll).
    return text.replace(/\\/g, "\\\\").replace(/\[/g, "\\[").replace(/\]/g, "\\]");
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
function buildOutboxAttachmentSummary(listing, baseline) {
    if (!listing || !baseline)
        return "";
    const added = (listing.outbox || [])
        .map((entry) => entry.name)
        .filter((name) => !baseline.has(name));
    return formatOutboxAttachments(listing, added);
}
function normalizeDeliveryStatus(value) {
    if (typeof value !== "string")
        return null;
    const normalized = value.trim().toLowerCase();
    if (normalized === "success" ||
        normalized === "partial_success" ||
        normalized === "failed" ||
        normalized === "duplicate_only" ||
        normalized === "skipped") {
        return normalized;
    }
    return null;
}
function summarizeDeliveryStatus(result) {
    return normalizeDeliveryStatus(result.delivery_status);
}
function deriveDeliverySummary(payload) {
    if (!payload || typeof payload !== "object")
        return null;
    const status = summarizeDeliveryStatus(payload);
    if (!status)
        return null;
    if (status === "success") {
        return {
            status,
            statusText: "Done · delivery sent",
            messageTag: "delivery:success",
        };
    }
    if (status === "partial_success") {
        return {
            status,
            statusText: "Done · delivery partial",
            messageTag: "delivery:partial_success",
        };
    }
    if (status === "failed") {
        return {
            status,
            statusText: "Done · delivery failed",
            messageTag: "delivery:failed",
        };
    }
    if (status === "duplicate_only") {
        return {
            status,
            statusText: "Done · delivery duplicate",
            messageTag: "delivery:duplicate_only",
        };
    }
    return {
        status,
        statusText: "Done · delivery skipped",
        messageTag: "delivery:skipped",
    };
}
async function finalizePMAResponse(responseText, options = {}) {
    if (!pmaChat)
        return;
    const deliverySummary = options.deliverySummary ?? pendingDeliverySummary;
    const finalizedTurnToken = currentPMATurnToken;
    const outboxBaseline = currentOutboxBaseline
        ? new Set(currentOutboxBaseline)
        : null;
    currentOutboxBaseline = null;
    clearPMAPendingTurn();
    turnEventsCtrl.abort();
    latestPhase = null;
    latestGuidance = null;
    latestElapsed = null;
    const trimmed = (responseText || "").trim();
    const content = trimmed;
    const startTime = pmaChat.state.startTime;
    const duration = startTime ? (Date.now() - startTime) / 1000 : undefined;
    const steps = pmaChat.state.totalEvents || pmaChat.state.events.length;
    if (content) {
        pmaChat.addAssistantMessage(content, true, {
            steps,
            duration,
            tag: deliverySummary?.messageTag,
        });
    }
    pmaChat.state.streamText = "";
    pmaChat.state.status = "done";
    pmaChat.state.statusText = deliverySummary?.statusText || "Done";
    pmaChat.render();
    pmaChat.renderMessages();
    pmaChat.renderEvents();
    pendingDeliverySummary = null;
    void (async () => {
        let attachments = "";
        try {
            if (fileBoxCtrl) {
                const current = await fileBoxCtrl.refresh();
                attachments = buildOutboxAttachmentSummary(current, outboxBaseline);
            }
        }
        catch {
            attachments = "";
        }
        if (attachments &&
            pmaChat &&
            shouldAppendAsyncOutboxSummary(finalizedTurnToken, currentPMATurnToken, pmaChat.state.status)) {
            pmaChat.addAssistantMessage(attachments, true);
            pmaChat.renderMessages();
        }
        void fileBoxCtrl?.refresh();
    })();
}
/**
 * Applies a walkthrough preset from sessionStorage (set before navigating to PMA).
 * Does not auto-send: the spec is to prefill the composer so the user can review and send.
 * Exported so the hub shell can run this after `showPMAView` when PMA was already initialized.
 */
function drainPendingPrompt() {
    let pending = "";
    try {
        pending = sessionStorage.getItem("car-pma-pending-prompt") || "";
        if (pending)
            sessionStorage.removeItem("car-pma-pending-prompt");
    }
    catch {
        return;
    }
    if (!pending)
        return;
    const elements = getElements();
    if (!elements.input)
        return;
    elements.input.value = pending;
    elements.input.dispatchEvent(new Event("input", { bubbles: true }));
    elements.input.focus();
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
        profileSelect: elements.profileSelect,
        modelSelect: elements.modelSelect,
        modelInput: elements.modelInput,
        reasoningSelect: elements.reasoningSelect,
    });
    await refreshAgentControls({ force: true, reason: "initial" });
    await loadPMAThreadInfo();
    await initFileBoxUI();
    await loadPMADocs();
    if (!currentDocName) {
        switchPMADoc("AGENTS.md");
    }
    attachHandlers();
    setPMAView(loadPMAView(), { persist: false });
    initNotificationBell();
    drainPendingPrompt();
    // If we refreshed mid-turn, recover the final output from the server.
    await resumePendingTurn();
    // If the page refreshes/navigates while a turn is running, avoid showing a noisy
    // "network error" and proactively interrupt the running turn on the server to
    // prevent the next request from receiving a stale/previous response.
    if (!unloadHandlerInstalled) {
        unloadHandlerInstalled = true;
        window.addEventListener("beforeunload", () => {
            isUnloading = true;
            stopPMARefreshLoop();
            // Abort any in-flight request immediately.
            // Note: we do NOT send an interrupt request to the server; the run continues
            // in the background and can be recovered after reload via /hub/pma/active.
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
    startPMARefreshLoop();
}
function startPMARefreshLoop() {
    if (pmaRefreshCleanup)
        return;
    pmaRefreshCleanup = registerAutoRefresh("pma:refresh", {
        callback: async () => {
            const shell = document.getElementById("pma-shell");
            if (!shell || shell.classList.contains("hidden"))
                return;
            await loadPMAThreadInfo();
            await fileBoxCtrl?.refresh();
        },
        interval: CONSTANTS.UI.AUTO_REFRESH_INTERVAL,
        refreshOnActivation: true,
        immediate: false,
    });
}
function stopPMARefreshLoop() {
    if (pmaRefreshCleanup) {
        pmaRefreshCleanup();
        pmaRefreshCleanup = null;
    }
}
export function setPMARefreshActive(active) {
    if (active) {
        startPMARefreshLoop();
    }
    else {
        stopPMARefreshLoop();
    }
}
function deriveThreadPillState(active, currentStatus, queuePending) {
    const status = currentStatus.trim().toLowerCase();
    if (status === "running" && active)
        return { label: "running", pillClass: "pill-running" };
    if (status === "interrupted")
        return { label: "interrupted", pillClass: "pill-warn" };
    if (status === "error" || status === "failed")
        return { label: "failed", pillClass: "pill-error" };
    if (status === "ok" || status === "completed")
        return { label: "completed", pillClass: "pill-completed" };
    if (active && queuePending > 0)
        return { label: "queued", pillClass: "pill-queued" };
    if (active)
        return { label: "accepted", pillClass: "pill-accepted" };
    if (status === "stalled")
        return { label: "stalled", pillClass: "pill-stalled" };
    return { label: "idle", pillClass: "pill-idle" };
}
function formatElapsedSeconds(seconds) {
    if (seconds < 60)
        return `${seconds}s`;
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    return `${m}m ${s}s`;
}
async function loadPMAThreadInfo() {
    const elements = getElements();
    if (!elements.threadInfo)
        return;
    try {
        const payload = (await api("/hub/pma/active", { method: "GET" }));
        const current = payload.current || {};
        const last = payload.last_result || {};
        const info = (payload.active && current.thread_id) ? current : last;
        if (!info || !info.thread_id) {
            elements.threadInfo.classList.add("hidden");
            return;
        }
        let queuePending = 0;
        let queuedItems = [];
        try {
            const queuePayload = (await api("/hub/pma/queue/pma:default", {
                method: "GET",
            }));
            queuedItems = (queuePayload.items || []).filter((item) => item.state === "pending" || item.state === "running");
            queuePending = queuedItems.filter((item) => item.state === "pending").length;
        }
        catch {
            // queue endpoint may not be available
        }
        const turnStatus = String(info.status || (payload.active ? "running" : (last.status || "idle")));
        if (elements.threadInfoAgent) {
            elements.threadInfoAgent.textContent = String(info.agent || "unknown");
        }
        if (elements.threadInfoThreadId) {
            const threadId = String(info.thread_id || "");
            elements.threadInfoThreadId.textContent = threadId.slice(0, 12);
            elements.threadInfoThreadId.title = threadId;
        }
        if (elements.threadInfoTurnId) {
            const turnId = String(info.turn_id || "");
            elements.threadInfoTurnId.textContent = turnId.slice(0, 12);
            elements.threadInfoTurnId.title = turnId;
        }
        const { label, pillClass } = deriveThreadPillState(!!payload.active, turnStatus, queuePending);
        if (elements.threadInfoStatus) {
            elements.threadInfoStatus.textContent = label;
            elements.threadInfoStatus.className = `pill pill-small ${pillClass}`;
        }
        const phaseEl = document.getElementById("pma-thread-info-phase");
        if (phaseEl) {
            const phase = latestPhase || String(info.phase || "");
            if (phase && payload.active) {
                phaseEl.textContent = phase;
                phaseEl.classList.remove("hidden");
            }
            else {
                phaseEl.classList.add("hidden");
            }
        }
        const queueRow = document.getElementById("pma-thread-info-queue-row");
        const queueValue = document.getElementById("pma-thread-info-queue");
        if (queueRow && queueValue) {
            if (queuePending > 0 || queuedItems.length > 0) {
                queueValue.textContent =
                    queuePending > 0
                        ? `${queuePending} waiting`
                        : `${queuedItems.length} active`;
                queueRow.classList.remove("hidden");
            }
            else {
                queueRow.classList.add("hidden");
            }
        }
        const elapsedRow = document.getElementById("pma-thread-info-elapsed-row");
        const elapsedValue = document.getElementById("pma-thread-info-elapsed");
        if (elapsedRow && elapsedValue) {
            const elapsed = latestElapsed;
            if (elapsed != null && elapsed > 0 && payload.active) {
                elapsedValue.textContent = formatElapsedSeconds(elapsed);
                elapsedRow.classList.remove("hidden");
            }
            else {
                elapsedRow.classList.add("hidden");
            }
        }
        const guidanceEl = document.getElementById("pma-thread-info-guidance");
        if (guidanceEl) {
            const guidance = latestGuidance || String(info.guidance || "");
            if (guidance && payload.active) {
                guidanceEl.textContent = guidance;
                guidanceEl.classList.remove("hidden");
            }
            else {
                guidanceEl.classList.add("hidden");
            }
        }
        const queueListEl = document.getElementById("pma-thread-info-queue-list");
        if (queueListEl) {
            const pendingItems = queuedItems.filter((item) => item.state === "pending");
            if (pendingItems.length > 0) {
                queueListEl.innerHTML = pendingItems
                    .slice(0, 3)
                    .map((item) => `<div class="pma-thread-info-queue-list-item"><span class="pill pill-small pill-queued">${escapeHtml(item.state)}</span><span class="muted small">${escapeHtml(item.enqueued_at || "")}</span></div>`)
                    .join("");
                queueListEl.classList.remove("hidden");
            }
            else {
                queueListEl.classList.add("hidden");
            }
        }
        elements.threadInfo.classList.remove("hidden");
    }
    catch {
        elements.threadInfo?.classList.add("hidden");
    }
}
function updateClearButtons(listing) {
    const elements = getElements();
    if (!elements.inboxClear || !elements.outboxClear)
        return;
    const inboxCount = listing?.inbox?.length ?? 0;
    const outboxCount = listing?.outbox?.length ?? 0;
    elements.inboxClear.classList.toggle("hidden", inboxCount <= 1);
    elements.outboxClear.classList.toggle("hidden", outboxCount <= 1);
    if (elements.inboxHint) {
        const hasInbox = inboxCount > 0;
        elements.inboxHint.textContent = hasInbox ? "Next: Process uploaded files" : "";
        elements.inboxHint.classList.toggle("hidden", !hasInbox);
    }
}
async function clearPMABox(box) {
    const confirmed = window.confirm(`Clear ${box}? This will delete all files.`);
    if (!confirmed)
        return;
    await api(`/hub/pma/files/${box}`, { method: "DELETE" });
    flash(`Cleared ${box}`, "info");
    await fileBoxCtrl?.refresh();
}
async function sendMessage() {
    const elements = getElements();
    if (!elements.input || !pmaChat)
        return;
    const message = elements.input.value?.trim() || "";
    if (!message)
        return;
    advancePMATurnToken();
    if (currentController) {
        await cancelActiveTurnAndWait({
            abortController() {
                if (currentController) {
                    currentController.abort();
                    currentController = null;
                }
                pmaChat.state.controller = null;
            },
            turnEventsCtrl,
            interruptServer: () => interruptActiveTurn(),
            clearPending: clearPMAPendingTurn,
        });
    }
    // Ensure prior turn event streams are cleared so we don't render stale actions.
    turnEventsCtrl.abort();
    pendingDeliverySummary = null;
    latestPhase = null;
    latestGuidance = null;
    latestElapsed = null;
    elements.input.value = "";
    elements.input.style.height = "auto";
    const agent = elements.agentSelect?.value || getSelectedAgent();
    const profile = elements.profileSelect?.value || getSelectedProfile(agent);
    const model = elements.modelSelect?.value || getSelectedModel(agent);
    const reasoning = elements.reasoningSelect?.value || getSelectedReasoning(agent);
    const clientTurnId = newFileChatTurnId("pma");
    savePMAPendingTurn({ clientTurnId, message, startedAtMs: Date.now() });
    currentController = new AbortController();
    pmaChat.state.controller = currentController;
    pmaChat.state.status = "running";
    pmaChat.state.error = "";
    pmaChat.state.statusText = "";
    pmaChat.state.contextUsagePercent = null;
    pmaChat.state.streamText = "";
    pmaChat.state.contextUsagePercent = null;
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
        if (profile)
            payload.profile = profile;
        if (model)
            payload.model = model;
        if (reasoning)
            payload.reasoning = reasoning;
        const responsePromise = fetch(endpoint, {
            method: "POST",
            headers,
            body: JSON.stringify(payload),
            signal: currentController.signal,
        });
        // Stream tool calls/events separately as soon as we have (thread_id, turn_id).
        // The main /hub/pma/chat stream only emits a final "update"/"done" today.
        void pollForTurnMeta(clientTurnId, { signal: currentController.signal });
        const res = await responsePromise;
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
            await readPMAStream(res, true);
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
            pendingDeliverySummary = null;
            pmaChat.render();
            return;
        }
        const errorMsg = err.message || "Request failed";
        pmaChat.state.status = "error";
        pmaChat.state.error = errorMsg;
        pmaChat.addAssistantMessage(`Error: ${errorMsg}`, true);
        pmaChat.render();
        pmaChat.renderMessages();
        clearPMAPendingTurn();
        turnEventsCtrl.abort();
        pendingDeliverySummary = null;
    }
    finally {
        currentController = null;
        pmaChat.state.controller = null;
    }
}
async function readPMAStream(res, finalizeOnClose = false) {
    await readEventStream(res, (event, data) => handleStreamEvent(event, data, pmaStreamHandlers), { handleEscapedNewlines: true });
    if (finalizeOnClose && pmaChat && pmaChat.state.status === "running") {
        const responseText = pmaChat.state.streamText || "";
        if (responseText.trim()) {
            void finalizePMAResponse(responseText);
        }
    }
}
const pmaStreamHandlers = {
    onStatus(status) {
        pmaChat.state.statusText = status;
        latestPhase = status;
        pmaChat.render();
        pmaChat.renderEvents();
    },
    onToken(token) {
        pmaChat.state.streamText = (pmaChat.state.streamText || "") + token;
        if (!pmaChat.state.statusText || pmaChat.state.statusText === "starting") {
            pmaChat.state.statusText = "responding";
            latestPhase = "responding";
        }
        if (pmaChat.state.status !== "running") {
            pmaChat.state.status = "running";
        }
        if (pmaChat.state.startTime) {
            latestElapsed = Math.round((Date.now() - pmaChat.state.startTime) / 1000);
        }
        pmaChat.render();
    },
    onTokenUsage(percentRemaining) {
        if (pmaChat) {
            pmaChat.state.contextUsagePercent = percentRemaining;
            pmaChat.render();
        }
    },
    onUpdate(payload) {
        if (payload.client_turn_id) {
            clearPMAPendingTurn();
        }
        if (payload.message) {
            pmaChat.state.streamText = payload.message;
        }
        if (payload.phase && typeof payload.phase === "string") {
            latestPhase = payload.phase;
        }
        if (payload.guidance && typeof payload.guidance === "string") {
            latestGuidance = payload.guidance;
        }
        if (payload.elapsed_seconds != null) {
            latestElapsed = typeof payload.elapsed_seconds === "number"
                ? payload.elapsed_seconds
                : null;
        }
        const summary = deriveDeliverySummary(payload);
        if (summary) {
            pendingDeliverySummary = summary;
        }
        pmaChat.render();
    },
    onEvent(event) {
        if (pmaChat) {
            if (pmaChat.state.status !== "running") {
                pmaChat.state.status = "running";
            }
            if (!pmaChat.state.statusText || pmaChat.state.statusText === "starting") {
                pmaChat.state.statusText = "working";
            }
            pmaChat.applyAppEvent(event);
            pmaChat.renderEvents();
            pmaChat.render();
        }
        if (event && typeof event === "object") {
            const evt = event;
            const title = String(evt.title || evt.kind || "");
            const summary = String(evt.summary || "");
            if (title || summary) {
                latestPhase = title || latestPhase;
                if (!latestGuidance || summary) {
                    latestGuidance = summary || latestGuidance;
                }
            }
        }
    },
    onError(message) {
        pendingDeliverySummary = null;
        latestPhase = null;
        latestGuidance = null;
        latestElapsed = null;
        pmaChat.state.status = "error";
        pmaChat.state.error = message;
        pmaChat.addAssistantMessage(`Error: ${message}`, true);
        pmaChat.render();
        pmaChat.renderMessages();
        throw new Error(message);
    },
    onInterrupted(message) {
        pendingDeliverySummary = null;
        latestPhase = null;
        latestGuidance = null;
        latestElapsed = null;
        pmaChat.state.status = "interrupted";
        pmaChat.state.error = "";
        pmaChat.state.statusText = message;
        pmaChat.addAssistantMessage("Request interrupted", true);
        pmaChat.render();
        pmaChat.renderMessages();
    },
    onDone() {
        void finalizePMAResponse(pmaChat.state.streamText || "");
    },
};
async function resumePendingTurn() {
    const pending = loadPMAPendingTurn();
    if (!pending || !pmaChat)
        return;
    // Show a running indicator immediately.
    pmaChat.state.status = "running";
    pmaChat.state.statusText = "Recovering previous turn…";
    pmaChat.render();
    pmaChat.renderMessages();
    const tracker = createTurnRecoveryTracker();
    const onStale = () => {
        if (!pmaChat)
            return;
        pmaChat.state.status = "error";
        pmaChat.state.error = ACTIVE_TURN_RECOVERY_STALE_MESSAGE;
        clearPMAPendingTurn();
        turnEventsCtrl.abort();
        pmaChat.render();
        pmaChat.renderMessages();
    };
    const poll = async () => {
        try {
            const payload = (await api(`/hub/pma/active?client_turn_id=${encodeURIComponent(pending.clientTurnId)}`, { method: "GET" }));
            const cur = (payload.current || {});
            const threadId = typeof cur.thread_id === "string" ? cur.thread_id : "";
            const turnId = typeof cur.turn_id === "string" ? cur.turn_id : "";
            const agent = typeof cur.agent === "string" ? cur.agent : "codex";
            if (threadId &&
                turnId &&
                !turnEventsCtrl.current &&
                agent.trim().toLowerCase() !== "opencode") {
                void startTurnEventsStream({ agent, threadId, turnId });
            }
            const last = (payload.last_result || {});
            const status = String(last.status || "");
            if (status === "ok" && typeof last.message === "string") {
                const summary = deriveDeliverySummary(last);
                pendingDeliverySummary = summary;
                await finalizePMAResponse(last.message, { deliverySummary: summary });
                return;
            }
            if (status === "error") {
                const detail = String(last.detail || "PMA chat failed");
                pendingDeliverySummary = null;
                pmaChat.state.status = "error";
                pmaChat.state.error = detail;
                pmaChat.addAssistantMessage(`Error: ${detail}`, true);
                pmaChat.render();
                pmaChat.renderMessages();
                clearPMAPendingTurn();
                turnEventsCtrl.abort();
                return;
            }
            if (status === "interrupted") {
                pendingDeliverySummary = null;
                pmaChat.state.status = "interrupted";
                pmaChat.state.error = "";
                pmaChat.addAssistantMessage("Request interrupted", true);
                pmaChat.render();
                pmaChat.renderMessages();
                clearPMAPendingTurn();
                turnEventsCtrl.abort();
                return;
            }
            // Still running; schedule bounded retry.
            pmaChat.state.status = "running";
            pmaChat.state.statusText = "Recovering previous turn…";
            pmaChat.render();
            scheduleRecoveryRetry({ tracker, retryFn: poll, onStale });
        }
        catch {
            scheduleRecoveryRetry({ tracker, retryFn: poll, onStale });
        }
    };
    await poll();
}
function applyPMAResult(payload) {
    if (!payload || typeof payload !== "object")
        return;
    const result = payload;
    if (result.status === "interrupted") {
        pendingDeliverySummary = null;
        pmaChat.state.status = "interrupted";
        pmaChat.state.error = "";
        pmaChat.addAssistantMessage("Request interrupted", true);
        pmaChat.render();
        pmaChat.renderMessages();
        return;
    }
    if (result.status === "error" || result.error) {
        pendingDeliverySummary = null;
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
    const summary = deriveDeliverySummary(result);
    pendingDeliverySummary = summary;
    const responseText = (pmaChat.state.streamText || pmaChat.state.statusText || "Done");
    void finalizePMAResponse(responseText, { deliverySummary: summary });
}
async function interruptActiveTurn(options = {}) {
    try {
        if (options.stopLane) {
            await api("/hub/pma/stop", {
                method: "POST",
                body: { lane_id: DEFAULT_PMA_LANE_ID },
            });
            return;
        }
        await api("/hub/pma/interrupt", { method: "POST" });
    }
    catch {
        // Best-effort; UI state already reflects cancellation.
    }
}
async function cancelRequest(options = {}) {
    const { clearPending = false, interruptServer = false, stopLane = false, statusText } = options;
    advancePMATurnToken();
    if (currentController) {
        currentController.abort();
        currentController = null;
    }
    pendingDeliverySummary = null;
    turnEventsCtrl.abort();
    latestPhase = null;
    latestGuidance = null;
    latestElapsed = null;
    if (interruptServer || stopLane) {
        await interruptActiveTurn({ stopLane });
    }
    if (clearPending) {
        clearPMAPendingTurn();
    }
    if (pmaChat) {
        pmaChat.state.controller = null;
        pmaChat.state.status = "interrupted";
        pmaChat.state.statusText = statusText || "Cancelled";
        pmaChat.state.contextUsagePercent = null;
        pmaChat.render();
    }
}
function resetThread() {
    advancePMATurnToken();
    clearPMAPendingTurn();
    pendingDeliverySummary = null;
    turnEventsCtrl.abort();
    latestPhase = null;
    latestGuidance = null;
    latestElapsed = null;
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
async function startNewThreadOnServer() {
    const elements = getElements();
    const rawAgent = (elements.agentSelect?.value || getSelectedAgent() || "").trim().toLowerCase();
    const selectedAgent = rawAgent || undefined;
    const selectedProfile = elements.profileSelect?.value || getSelectedProfile(rawAgent);
    await api("/hub/pma/new", {
        method: "POST",
        body: {
            agent: selectedAgent,
            profile: selectedProfile || undefined,
            lane_id: DEFAULT_PMA_LANE_ID,
        },
    });
}
function attachHandlers() {
    const elements = getElements();
    document.addEventListener("click", (event) => {
        const target = event.target;
        const btn = target?.closest?.(".pma-view-btn");
        if (!btn)
            return;
        const value = (btn.dataset.view || "chat");
        setPMAView(value);
    });
    if (elements.sendBtn) {
        elements.sendBtn.addEventListener("click", () => {
            void sendMessage();
        });
    }
    document.addEventListener("pma:inject-prompt", (evt) => {
        const detail = evt.detail;
        const prompt = typeof detail?.prompt === "string" ? detail.prompt : "";
        if (!prompt || !elements.input)
            return;
        try {
            sessionStorage.removeItem("car-pma-pending-prompt");
        }
        catch {
            // ignore
        }
        elements.input.value = prompt;
        elements.input.dispatchEvent(new Event("input", { bubbles: true }));
        elements.input.focus();
    });
    if (elements.cancelBtn) {
        elements.cancelBtn.addEventListener("click", () => {
            void cancelRequest({ clearPending: true, interruptServer: true });
        });
    }
    if (elements.newThreadBtn) {
        elements.newThreadBtn.addEventListener("click", () => {
            void (async () => {
                await cancelRequest({
                    clearPending: true,
                    interruptServer: true,
                    stopLane: true,
                    statusText: "Cancelled (new thread)",
                });
                try {
                    await startNewThreadOnServer();
                }
                catch (err) {
                    flash("Failed to start new session", "error");
                    return;
                }
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
        initChatPasteUpload({
            textarea: elements.input,
            basePath: "/hub/pma/files",
            box: DEFAULT_FILEBOX_BOX,
            insertStyle: "markdown",
            onUploaded: () => {
                void fileBoxCtrl?.refresh();
            },
        });
    }
    if (elements.outboxRefresh) {
        elements.outboxRefresh.addEventListener("click", () => {
            void fileBoxCtrl?.refresh();
        });
    }
    if (elements.inboxClear) {
        elements.inboxClear.addEventListener("click", () => {
            void clearPMABox(FILEBOX_BOXES[0]);
        });
    }
    if (elements.outboxClear) {
        elements.outboxClear.addEventListener("click", () => {
            void clearPMABox(FILEBOX_BOXES[1]);
        });
    }
    if (elements.scanReposBtn) {
        elements.scanReposBtn.addEventListener("click", async () => {
            const btn = elements.scanReposBtn;
            const originalText = btn.textContent || "";
            try {
                btn.disabled = true;
                await api("/hub/repos/scan", { method: "POST" });
                flash("Repositories scanned", "info");
            }
            catch (err) {
                flash("Failed to scan repos", "error");
            }
            finally {
                btn.disabled = false;
                btn.textContent = btn.textContent || originalText;
            }
        });
    }
    if (elements.threadInfoThreadId) {
        elements.threadInfoThreadId.addEventListener("click", () => {
            const fullId = elements.threadInfoThreadId?.title || "";
            if (fullId) {
                void navigator.clipboard.writeText(fullId).then(() => {
                    flash("Copied thread ID", "info");
                });
            }
        });
        elements.threadInfoThreadId.style.cursor = "pointer";
    }
    if (elements.threadInfoTurnId) {
        elements.threadInfoTurnId.addEventListener("click", () => {
            const fullId = elements.threadInfoTurnId?.title || "";
            if (fullId) {
                void navigator.clipboard.writeText(fullId).then(() => {
                    flash("Copied turn ID", "info");
                });
            }
        });
        elements.threadInfoTurnId.style.cursor = "pointer";
    }
    document.querySelectorAll(".pma-docs-tab").forEach((tab) => {
        if (tab instanceof HTMLElement) {
            tab.addEventListener("click", () => {
                const docName = tab.dataset.doc;
                if (docName)
                    switchPMADoc(docName);
            });
        }
    });
    if (elements.docsSaveBtn) {
        elements.docsSaveBtn.addEventListener("click", () => {
            const editor = elements.docsEditor;
            if (editor && currentDocName && EDITABLE_DOCS.includes(currentDocName)) {
                void savePMADoc(currentDocName, editor.value);
            }
        });
    }
    if (elements.docsResetBtn) {
        elements.docsResetBtn.addEventListener("click", resetActiveContext);
    }
    if (elements.docsSnapshotBtn) {
        elements.docsSnapshotBtn.addEventListener("click", () => {
            void snapshotActiveContext();
        });
    }
    if (elements.docsEditor) {
        elements.docsEditor.addEventListener("input", () => {
            elements.docsEditor.style.height = "auto";
            elements.docsEditor.style.height = `${elements.docsEditor.scrollHeight}px`;
        });
    }
}
const __pmaTest = {
    buildOutboxAttachmentSummary,
    shouldAppendAsyncOutboxSummary,
};
export { __pmaTest, drainPendingPrompt, initPMA };
