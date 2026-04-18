// GENERATED FILE - do not edit directly. Source: static_src/
import { api, flash, setButtonLoading } from "./utils.js";
import { initAgentControls, getSelectedAgent, getSelectedProfile, getSelectedModel, getSelectedReasoning, } from "./agentControls.js";
import { fetchContextspace, ingestSpecToTickets, listTickets, writeContextspace, } from "./contextspaceApi.js";
import { applyDraft, discardDraft, fetchPendingDraft, sendFileChat, interruptFileChat, newClientTurnId, } from "./fileChat.js";
import { DocEditor } from "./docEditor.js";
import { createDocChat } from "./docChatCore.js";
import { initChatPasteUpload } from "./chatUploads.js";
import { initDocChatVoice } from "./docChatVoice.js";
import { renderDiff } from "./diffRenderer.js";
import { subscribe } from "./bus.js";
import { DEFAULT_FILEBOX_BOX } from "./fileboxCatalog.js";
import { isRepoHealthy } from "./health.js";
import { loadPendingTurn, savePendingTurn } from "./turnResume.js";
import { resumeFileChatTurn } from "./turnEvents.js";
import { createTurnEventsController, startTurnEventsStream as sharedStartTurnEventsStream, clearManagedTurn, } from "./sharedTurnLifecycle.js";
const CONTEXTSPACE_CHAT_EVENT_LIMIT = 8;
const CONTEXTSPACE_CHAT_EVENT_MAX = 50;
const CONTEXTSPACE_PENDING_KEY = "car.contextspace.pendingTurn";
const state = {
    target: "active_context",
    content: "",
    draft: null,
    hasTickets: true,
    loading: false,
    docEditor: null,
    docs: [],
};
const workspaceChat = createDocChat({
    idPrefix: "contextspace-chat",
    storage: { keyPrefix: "car-contextspace-chat-", maxMessages: 50, version: 1 },
    limits: { eventVisible: CONTEXTSPACE_CHAT_EVENT_LIMIT, eventMax: CONTEXTSPACE_CHAT_EVENT_MAX },
    styling: {
        eventClass: "doc-chat-event",
        eventTitleClass: "doc-chat-event-title",
        eventSummaryClass: "doc-chat-event-summary",
        eventDetailClass: "doc-chat-event-detail",
        eventMetaClass: "doc-chat-event-meta",
        eventsEmptyClass: "doc-chat-events-empty",
        eventsHiddenClass: "hidden",
        messagesClass: "doc-chat-message",
        messageRoleClass: "doc-chat-message-role",
        messageContentClass: "doc-chat-message-content",
        messageMetaClass: "doc-chat-message-meta",
        messageUserClass: "user",
        messageAssistantClass: "assistant",
        messageAssistantThinkingClass: "streaming",
        messageAssistantFinalClass: "final",
    },
});
const turnEventsCtrl = createTurnEventsController();
function els() {
    return {
        root: document.getElementById("contextspace"),
        fileList: document.getElementById("contextspace-file-list"),
        fileSelect: document.getElementById("contextspace-file-select"),
        breadcrumbs: document.getElementById("contextspace-breadcrumbs"),
        filePillName: document.getElementById("contextspace-file-pill-name"),
        status: document.getElementById("contextspace-status"),
        statusMobile: document.getElementById("contextspace-status-mobile"),
        uploadBtn: document.getElementById("contextspace-upload"),
        uploadInput: document.getElementById("contextspace-upload-input"),
        newFolderBtn: document.getElementById("contextspace-new-folder"),
        newFileBtn: document.getElementById("contextspace-new-file"),
        downloadAllBtn: document.getElementById("contextspace-download-all"),
        mobileMenuToggle: document.getElementById("contextspace-mobile-menu-toggle"),
        mobileDropdown: document.getElementById("contextspace-mobile-dropdown"),
        mobileUpload: document.getElementById("contextspace-mobile-upload"),
        mobileNewFolder: document.getElementById("contextspace-mobile-new-folder"),
        mobileNewFile: document.getElementById("contextspace-mobile-new-file"),
        mobileDownload: document.getElementById("contextspace-mobile-download"),
        generateBtn: document.getElementById("contextspace-generate-tickets"),
        mobileGenerate: document.getElementById("contextspace-mobile-generate"),
        textarea: document.getElementById("contextspace-content"),
        saveBtn: document.getElementById("contextspace-save"),
        saveBtnMobile: document.getElementById("contextspace-save-mobile"),
        reloadBtn: document.getElementById("contextspace-reload"),
        reloadBtnMobile: document.getElementById("contextspace-reload-mobile"),
        patchMain: document.getElementById("contextspace-patch-main"),
        patchBody: document.getElementById("contextspace-patch-body"),
        patchSummary: document.getElementById("contextspace-patch-summary"),
        patchMeta: document.getElementById("contextspace-patch-meta"),
        patchApply: document.getElementById("contextspace-patch-apply"),
        patchReload: document.getElementById("contextspace-patch-reload"),
        patchDiscard: document.getElementById("contextspace-patch-discard"),
        chatInput: document.getElementById("contextspace-chat-input"),
        chatSend: document.getElementById("contextspace-chat-send"),
        chatCancel: document.getElementById("contextspace-chat-cancel"),
        chatNewThread: document.getElementById("contextspace-chat-new-thread"),
        chatStatus: document.getElementById("contextspace-chat-status"),
        chatError: document.getElementById("contextspace-chat-error"),
        chatMessages: document.getElementById("contextspace-chat-history"),
        chatEvents: document.getElementById("contextspace-chat-events"),
        chatEventsList: document.getElementById("contextspace-chat-events-list"),
        chatEventsToggle: document.getElementById("contextspace-chat-events-toggle"),
        agentSelect: document.getElementById("contextspace-chat-agent-select"),
        profileSelect: document.getElementById("contextspace-chat-profile-select"),
        modelSelect: document.getElementById("contextspace-chat-model-select"),
        modelInput: document.getElementById("contextspace-chat-model-input"),
        reasoningSelect: document.getElementById("contextspace-chat-reasoning-select"),
    };
}
function normalizeDocCatalog(docs) {
    if (!Array.isArray(docs))
        return [];
    const seen = new Set();
    const normalized = [];
    for (const doc of docs) {
        if (!doc || typeof doc.kind !== "string")
            continue;
        const kind = doc.kind.trim().toLowerCase();
        const path = typeof doc.path === "string" ? doc.path.trim() : "";
        const label = typeof doc.label === "string" ? doc.label.trim() : "";
        const description = typeof doc.description === "string" ? doc.description.trim() : "";
        if (!kind || !path || seen.has(kind))
            continue;
        seen.add(kind);
        normalized.push({
            kind,
            path,
            label: label || kind,
            description: description || kind,
        });
    }
    return normalized;
}
function _fallbackDoc(kind) {
    const safeKind = (kind || "").trim().toLowerCase() || "contextspace";
    const label = safeKind.replace(/_/g, " ");
    return {
        kind: safeKind,
        path: `${safeKind}.md`,
        label,
        description: "Contextspace document.",
    };
}
function currentDocs() {
    return state.docs;
}
function docForKind(kind) {
    return currentDocs().find((doc) => doc.kind === kind) || _fallbackDoc(kind);
}
function normalizeKind(value) {
    const trimmed = (value || "").trim().toLowerCase().replace(/\.md$/, "");
    const match = currentDocs().find((doc) => doc.kind === trimmed);
    return match?.kind || null;
}
function kindFromPendingTarget(targetValue) {
    const raw = (targetValue || "").trim();
    if (!raw.toLowerCase().startsWith("contextspace:"))
        return null;
    const [, suffix = ""] = raw.split(":", 2);
    return normalizeKind(suffix);
}
function currentDoc() {
    return docForKind(state.target);
}
function currentTarget() {
    return `contextspace:${state.target}`;
}
function contextspaceThreadKey(kind) {
    return `file_chat.contextspace_${docForKind(kind).path}`;
}
function contentForKind(response, kind) {
    const value = response[kind];
    return typeof value === "string" ? value : "";
}
function setStatus(text) {
    const { status, statusMobile } = els();
    if (status)
        status.textContent = text;
    if (statusMobile)
        statusMobile.textContent = text;
}
function setReloading(active) {
    const { reloadBtn, reloadBtnMobile } = els();
    setButtonLoading(reloadBtn, active);
    setButtonLoading(reloadBtnMobile, active);
}
function updateDraftVisibility() {
    const { patchMain, patchBody, patchSummary, patchMeta, textarea, saveBtn, reloadBtn } = els();
    if (!patchMain || !patchBody || !textarea)
        return;
    const draft = state.draft;
    if (!draft) {
        patchMain.classList.add("hidden");
        textarea.classList.remove("hidden");
        textarea.disabled = false;
        saveBtn?.removeAttribute("disabled");
        reloadBtn?.removeAttribute("disabled");
        return;
    }
    patchMain.classList.remove("hidden");
    patchMain.classList.toggle("stale", Boolean(draft.is_stale));
    renderDiff(draft.patch || "(no diff)", patchBody);
    if (patchSummary) {
        patchSummary.textContent = draft.is_stale
            ? "Stale draft: the live file changed after this draft was created."
            : draft.agent_message || "Draft ready";
        patchSummary.classList.toggle("warn", Boolean(draft.is_stale));
    }
    if (patchMeta) {
        const created = draft.created_at || "";
        patchMeta.textContent = draft.is_stale
            ? `${created} · base ${draft.base_hash || ""} vs current ${draft.current_hash || ""}`.trim()
            : created;
    }
    textarea.classList.add("hidden");
    textarea.disabled = true;
    const patchApply = els().patchApply;
    if (patchApply) {
        patchApply.textContent = draft.is_stale ? "Force Apply" : "Apply Draft";
    }
    saveBtn?.setAttribute("disabled", "true");
    reloadBtn?.setAttribute("disabled", "true");
}
function hideRemovedControls() {
    const elements = [
        els().uploadBtn,
        els().newFolderBtn,
        els().newFileBtn,
        els().downloadAllBtn,
        els().mobileMenuToggle,
        els().mobileDropdown,
        els().mobileUpload,
        els().mobileNewFolder,
        els().mobileNewFile,
        els().mobileDownload,
    ];
    elements.forEach((el) => {
        if (!el)
            return;
        el.classList.add("hidden");
        el.style.display = "none";
    });
}
function renderDocTargets() {
    const { fileList, fileSelect, breadcrumbs, filePillName } = els();
    const active = state.target;
    const docs = currentDocs();
    if (fileList) {
        fileList.innerHTML = "";
        docs.forEach((doc) => {
            const button = document.createElement("button");
            button.className = `workspace-file-row${doc.kind === active ? " active" : ""}`;
            button.type = "button";
            button.dataset.kind = doc.kind;
            button.innerHTML = `
        <span class="workspace-file-name">${doc.path}</span>
        <span class="workspace-file-meta muted small">${doc.label}</span>
      `;
            button.addEventListener("click", () => {
                void loadDoc(doc.kind, { reason: "manual" });
            });
            fileList.appendChild(button);
        });
    }
    if (fileSelect) {
        fileSelect.innerHTML = "";
        docs.forEach((doc) => {
            const option = document.createElement("option");
            option.value = doc.kind;
            option.textContent = doc.path;
            option.selected = doc.kind === active;
            fileSelect.appendChild(option);
        });
        fileSelect.value = active;
    }
    if (breadcrumbs) {
        breadcrumbs.innerHTML = "";
        const label = document.createElement("span");
        label.className = "muted small";
        label.textContent = `.codex-autorunner/contextspace/${currentDoc().path}`;
        breadcrumbs.appendChild(label);
    }
    if (filePillName) {
        filePillName.textContent = currentDoc().path;
    }
}
async function maybeShowGenerate() {
    try {
        const res = await listTickets();
        const tickets = Array.isArray(res?.tickets) ? res.tickets : [];
        state.hasTickets = tickets.length > 0;
    }
    catch {
        state.hasTickets = true;
    }
    const hidden = state.hasTickets;
    const { generateBtn, mobileGenerate } = els();
    if (generateBtn)
        generateBtn.classList.toggle("hidden", hidden);
    if (mobileGenerate)
        mobileGenerate.classList.toggle("hidden", hidden);
}
async function generateTickets() {
    try {
        const res = await ingestSpecToTickets();
        flash(res.created > 0 ? `Created ${res.created} ticket${res.created === 1 ? "" : "s"}` : "No tickets created", "success");
        await maybeShowGenerate();
    }
    catch (err) {
        flash(err.message || "Failed to generate tickets", "error");
    }
}
async function loadPendingDraft() {
    state.draft = await fetchPendingDraft(currentTarget());
    updateDraftVisibility();
}
function recreateEditor(content) {
    const { textarea, saveBtn, status } = els();
    if (!textarea)
        return;
    state.docEditor?.destroy();
    state.docEditor = new DocEditor({
        target: currentTarget(),
        textarea,
        saveButton: saveBtn,
        statusEl: status,
        onLoad: async () => content,
        onSave: async (nextContent) => {
            const response = await writeContextspace(state.target, nextContent);
            state.content = response[state.target] || "";
            if (textarea.value !== state.content) {
                textarea.value = state.content;
            }
        },
    });
}
async function loadDoc(kind, options = {}) {
    const reason = options.reason || "manual";
    const isInitial = reason === "initial";
    const showLoading = reason !== "background";
    if (showLoading) {
        if (isInitial) {
            state.loading = true;
            setStatus("Loading…");
        }
        else {
            setReloading(true);
        }
    }
    try {
        const response = await fetchContextspace();
        state.docs = normalizeDocCatalog(response.kinds);
        if (!state.docs.length) {
            throw new Error("Contextspace catalog unavailable from API response");
        }
        const resolvedKind = state.docs.some((doc) => doc.kind === kind)
            ? kind
            : state.docs[0].kind;
        state.target = resolvedKind;
        state.content = contentForKind(response, resolvedKind);
        workspaceChat.setTarget(currentTarget());
        renderDocTargets();
        recreateEditor(state.content);
        await loadPendingDraft();
        if (reason !== "background") {
            setStatus(currentDoc().description);
        }
    }
    catch (err) {
        const message = err.message || "Failed to load contextspace doc";
        flash(message, "error");
        setStatus(message);
    }
    finally {
        state.loading = false;
        if (!isInitial && showLoading) {
            setReloading(false);
        }
    }
}
async function reloadCurrentDoc(reason = "manual") {
    await loadDoc(state.target, { reason });
}
async function applyWorkspaceDraft() {
    try {
        const isStale = Boolean(state.draft?.is_stale);
        if (isStale && !window.confirm("This draft is stale. Force apply it anyway?")) {
            return;
        }
        const response = await applyDraft(currentTarget(), { force: isStale });
        state.content = response.content || "";
        state.draft = null;
        const { textarea } = els();
        if (textarea) {
            textarea.value = state.content;
        }
        updateDraftVisibility();
        flash(response.agent_message || "Draft applied", "success");
    }
    catch (err) {
        flash(err.message || "Failed to apply draft", "error");
    }
}
async function discardWorkspaceDraft() {
    try {
        const response = await discardDraft(currentTarget());
        state.content = response.content || "";
        state.draft = null;
        const { textarea } = els();
        if (textarea) {
            textarea.value = state.content;
        }
        updateDraftVisibility();
        flash("Draft discarded", "success");
    }
    catch (err) {
        flash(err.message || "Failed to discard draft", "error");
    }
}
function clearPendingTurnState() {
    clearManagedTurn(turnEventsCtrl, CONTEXTSPACE_PENDING_KEY);
}
function maybeStartTurnEventsFromUpdate(update) {
    const meta = update;
    const threadId = typeof meta.thread_id === "string" ? meta.thread_id : "";
    const turnId = typeof meta.turn_id === "string" ? meta.turn_id : "";
    const agent = typeof meta.agent === "string" ? meta.agent : undefined;
    if (!threadId || !turnId)
        return;
    sharedStartTurnEventsStream(turnEventsCtrl, { agent, threadId, turnId }, {
        onEvent: (event) => {
            workspaceChat.applyAppEvent(event);
            workspaceChat.renderEvents();
            workspaceChat.render();
        },
    });
}
function applyChatUpdate(update) {
    const hasDraft = update.has_draft ?? update.hasDraft;
    if (hasDraft === false) {
        state.draft = null;
        if (typeof update.content === "string") {
            state.content = update.content;
            const { textarea } = els();
            if (textarea) {
                textarea.value = update.content;
            }
        }
    }
    else if (hasDraft === true || update.patch || update.content) {
        state.draft = {
            target: currentTarget(),
            content: update.content || "",
            patch: update.patch || "",
            agent_message: update.agent_message,
            created_at: update.created_at,
            base_hash: update.base_hash,
            current_hash: update.current_hash,
            is_stale: Boolean(update.is_stale),
        };
    }
    updateDraftVisibility();
    const message = update.message || update.agent_message || "";
    if (message) {
        workspaceChat.addAssistantMessage(message);
    }
    workspaceChat.render();
}
function applyFinalResult(result) {
    const chatState = workspaceChat.state;
    const status = String(result.status || "");
    if (status === "ok") {
        applyChatUpdate(result);
        chatState.status = "done";
        chatState.error = "";
        chatState.streamText = "";
        clearPendingTurnState();
        workspaceChat.render();
        return;
    }
    if (status === "error") {
        const detail = String(result.detail || "Chat failed");
        chatState.status = "error";
        chatState.error = detail;
        workspaceChat.render();
        flash(detail, "error");
        clearPendingTurnState();
        return;
    }
    if (status === "interrupted") {
        chatState.status = "interrupted";
        chatState.error = "";
        chatState.streamText = "";
        workspaceChat.render();
        clearPendingTurnState();
    }
}
async function resumePendingWorkspaceTurn() {
    const pending = loadPendingTurn(CONTEXTSPACE_PENDING_KEY);
    if (!pending)
        return;
    const pendingTarget = typeof pending.target === "string" ? pending.target : "";
    const pendingKind = kindFromPendingTarget(pendingTarget);
    if (pendingKind && pendingKind !== state.target) {
        await loadDoc(pendingKind, { reason: "manual" });
    }
    const chatState = workspaceChat.state;
    chatState.status = "running";
    chatState.statusText = "Recovering previous turn…";
    workspaceChat.render();
    workspaceChat.renderMessages();
    try {
        const clientTurnId = typeof pending.clientTurnId === "string" ? pending.clientTurnId : "";
        const outcome = await resumeFileChatTurn(clientTurnId, {
            onEvent: (event) => {
                workspaceChat.applyAppEvent(event);
                workspaceChat.renderEvents();
                workspaceChat.render();
            },
            onResult: (result) => applyFinalResult(result),
            onError: (message) => {
                chatState.statusText = message;
                workspaceChat.render();
            },
        });
        turnEventsCtrl.current = outcome.controller;
        if (outcome.lastResult && outcome.lastResult.status) {
            applyFinalResult(outcome.lastResult);
            return;
        }
        if (!outcome.controller) {
            window.setTimeout(() => {
                void resumePendingWorkspaceTurn();
            }, 1000);
        }
    }
    catch (err) {
        const message = err.message || "Failed to resume turn";
        chatState.statusText = message;
        workspaceChat.render();
    }
}
async function sendChat() {
    const { chatInput, chatSend, chatCancel } = els();
    const message = (chatInput?.value || "").trim();
    if (!message)
        return;
    const chatState = workspaceChat.state;
    if (chatState.controller) {
        chatState.controller.abort();
    }
    chatState.controller = new AbortController();
    chatState.status = "running";
    chatState.error = "";
    chatState.statusText = "queued";
    chatState.streamText = "";
    chatState.contextUsagePercent = null;
    workspaceChat.clearEvents();
    workspaceChat.addUserMessage(message);
    workspaceChat.render();
    if (chatInput)
        chatInput.value = "";
    chatSend?.setAttribute("disabled", "true");
    chatCancel?.classList.remove("hidden");
    turnEventsCtrl.abort();
    const clientTurnId = newClientTurnId("contextspace");
    savePendingTurn(CONTEXTSPACE_PENDING_KEY, {
        clientTurnId,
        message,
        startedAtMs: Date.now(),
        target: currentTarget(),
    });
    const agent = getSelectedAgent();
    const profile = getSelectedProfile(agent) || undefined;
    const model = getSelectedModel(agent) || undefined;
    const reasoning = getSelectedReasoning(agent) || undefined;
    try {
        await sendFileChat(currentTarget(), message, chatState.controller, {
            onStatus: (status) => {
                chatState.statusText = status;
                setStatus(status || currentDoc().description);
                workspaceChat.render();
            },
            onToken: (token) => {
                chatState.streamText = (chatState.streamText || "") + token;
                workspaceChat.renderMessages();
            },
            onEvent: (event) => {
                workspaceChat.applyAppEvent(event);
                workspaceChat.renderEvents();
            },
            onTokenUsage: (percent) => {
                chatState.contextUsagePercent = percent;
                workspaceChat.render();
            },
            onUpdate: (update) => {
                applyChatUpdate(update);
                maybeStartTurnEventsFromUpdate(update);
            },
            onError: (message) => {
                chatState.status = "error";
                chatState.error = message;
                workspaceChat.render();
                flash(message, "error");
                clearPendingTurnState();
            },
            onInterrupted: (message) => {
                chatState.status = "interrupted";
                chatState.error = "";
                chatState.streamText = "";
                workspaceChat.render();
                flash(message, "info");
                clearPendingTurnState();
            },
            onDone: () => {
                if (chatState.streamText) {
                    workspaceChat.addAssistantMessage(chatState.streamText);
                    chatState.streamText = "";
                }
                chatState.status = "done";
                workspaceChat.render();
                clearPendingTurnState();
            },
        }, { agent, profile, model, reasoning, clientTurnId });
    }
    catch (err) {
        const message = err.message || "Chat failed";
        chatState.status = "error";
        chatState.error = message;
        workspaceChat.render();
        flash(message, "error");
        clearPendingTurnState();
    }
    finally {
        chatSend?.removeAttribute("disabled");
        chatCancel?.classList.add("hidden");
        chatState.controller = null;
    }
}
async function cancelChat() {
    const chatState = workspaceChat.state;
    if (chatState.controller) {
        chatState.controller.abort();
    }
    try {
        await interruptFileChat(currentTarget());
    }
    catch {
        // ignore
    }
    chatState.status = "interrupted";
    chatState.streamText = "";
    chatState.contextUsagePercent = null;
    workspaceChat.render();
    clearPendingTurnState();
}
async function resetThread() {
    try {
        await api("/api/app-server/threads/reset", {
            method: "POST",
            body: { key: contextspaceThreadKey(state.target) },
        });
        const chatState = workspaceChat.state;
        chatState.messages = [];
        chatState.streamText = "";
        chatState.contextUsagePercent = null;
        workspaceChat.clearEvents();
        clearPendingTurnState();
        workspaceChat.render();
        flash("New contextspace chat thread", "success");
    }
    catch (err) {
        flash(err.message || "Failed to reset thread", "error");
    }
}
export async function initContextspace() {
    const { root, fileSelect, saveBtn, saveBtnMobile, reloadBtn, reloadBtnMobile, patchApply, patchDiscard, patchReload, generateBtn, mobileGenerate, chatInput, chatSend, chatCancel, chatNewThread, agentSelect, profileSelect, modelSelect, modelInput, reasoningSelect, } = els();
    if (!root)
        return;
    hideRemovedControls();
    initAgentControls({
        agentSelect,
        profileSelect,
        modelSelect,
        modelInput,
        reasoningSelect,
    });
    await initDocChatVoice({
        buttonId: "contextspace-chat-voice",
        inputId: "contextspace-chat-input",
    });
    fileSelect?.addEventListener("change", () => {
        const kind = normalizeKind(fileSelect.value);
        if (!kind)
            return;
        void loadDoc(kind, { reason: "manual" });
    });
    saveBtn?.addEventListener("click", () => void state.docEditor?.save(true));
    saveBtnMobile?.addEventListener("click", () => void state.docEditor?.save(true));
    reloadBtn?.addEventListener("click", () => void reloadCurrentDoc("manual"));
    reloadBtnMobile?.addEventListener("click", () => void reloadCurrentDoc("manual"));
    patchApply?.addEventListener("click", () => void applyWorkspaceDraft());
    patchDiscard?.addEventListener("click", () => void discardWorkspaceDraft());
    patchReload?.addEventListener("click", () => void loadPendingDraft());
    generateBtn?.addEventListener("click", () => void generateTickets());
    mobileGenerate?.addEventListener("click", () => void generateTickets());
    chatSend?.addEventListener("click", () => void sendChat());
    chatCancel?.addEventListener("click", () => void cancelChat());
    chatNewThread?.addEventListener("click", () => void resetThread());
    if (chatInput) {
        chatInput.addEventListener("keydown", (event) => {
            if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
                event.preventDefault();
                void sendChat();
            }
        });
        initChatPasteUpload({
            textarea: chatInput,
            basePath: "/api/filebox",
            box: DEFAULT_FILEBOX_BOX,
            insertStyle: "both",
            pathPrefix: ".codex-autorunner/filebox",
        });
    }
    await maybeShowGenerate();
    await loadDoc(state.target, { reason: "initial" });
    void resumePendingWorkspaceTurn();
    subscribe("repo:health", () => {
        if (!isRepoHealthy() || state.draft)
            return;
        const { textarea } = els();
        const hasLocalEdits = Boolean(textarea && textarea.value !== state.content);
        if (hasLocalEdits)
            return;
        void reloadCurrentDoc("background");
    });
}
