// GENERATED FILE - do not edit directly. Source: static_src/
import { api, flash } from "./utils.js";
import { initAgentControls, getSelectedAgent, getSelectedModel, getSelectedReasoning } from "./agentControls.js";
import { fetchWorkspace, ingestSpecToTickets, listTickets, listWorkspaceFiles, writeWorkspace, } from "./workspaceApi.js";
import { applyDraft, discardDraft, fetchPendingDraft, sendFileChat, interruptFileChat, } from "./fileChat.js";
import { DocEditor } from "./docEditor.js";
import { WorkspaceFileBrowser } from "./workspaceFileBrowser.js";
import { createDocChat } from "./docChatCore.js";
import { initDocChatVoice } from "./docChatVoice.js";
import { renderDiff } from "./diffRenderer.js";
const state = {
    target: null,
    content: "",
    draft: null,
    loading: false,
    hasTickets: true,
    files: [],
    docEditor: null,
    browser: null,
};
const WORKSPACE_CHAT_EVENT_LIMIT = 8;
const WORKSPACE_CHAT_EVENT_MAX = 50;
const workspaceChat = createDocChat({
    idPrefix: "workspace-chat",
    storage: { keyPrefix: "car-workspace-chat-", maxMessages: 50, version: 1 },
    limits: { eventVisible: WORKSPACE_CHAT_EVENT_LIMIT, eventMax: WORKSPACE_CHAT_EVENT_MAX },
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
const WORKSPACE_DOC_KINDS = new Set(["active_context", "decisions", "spec"]);
function els() {
    return {
        fileList: document.getElementById("workspace-file-list"),
        fileSelect: document.getElementById("workspace-file-select"),
        status: document.getElementById("workspace-status"),
        generateBtn: document.getElementById("workspace-generate-tickets"),
        textarea: document.getElementById("workspace-content"),
        saveBtn: document.getElementById("workspace-save"),
        reloadBtn: document.getElementById("workspace-reload"),
        patchMain: document.getElementById("workspace-patch-main"),
        patchBody: document.getElementById("workspace-patch-body"),
        patchSummary: document.getElementById("workspace-patch-summary"),
        patchMeta: document.getElementById("workspace-patch-meta"),
        patchApply: document.getElementById("workspace-patch-apply"),
        patchReload: document.getElementById("workspace-patch-reload"),
        patchDiscard: document.getElementById("workspace-patch-discard"),
        chatInput: document.getElementById("workspace-chat-input"),
        chatSend: document.getElementById("workspace-chat-send"),
        chatCancel: document.getElementById("workspace-chat-cancel"),
        chatNewThread: document.getElementById("workspace-chat-new-thread"),
        chatStatus: document.getElementById("workspace-chat-status"),
        chatError: document.getElementById("workspace-chat-error"),
        chatMessages: document.getElementById("workspace-chat-history"),
        chatEvents: document.getElementById("workspace-chat-events"),
        chatEventsList: document.getElementById("workspace-chat-events-list"),
        chatEventsToggle: document.getElementById("workspace-chat-events-toggle"),
        agentSelect: document.getElementById("workspace-chat-agent-select"),
        modelSelect: document.getElementById("workspace-chat-model-select"),
        reasoningSelect: document.getElementById("workspace-chat-reasoning-select"),
    };
}
function workspaceKindFromPath(path) {
    const normalized = (path || "").replace(/\\/g, "/").trim();
    if (!normalized)
        return null;
    const baseName = normalized.split("/").pop() || normalized;
    const match = baseName.match(/^([a-z_]+)\.md$/i);
    const kind = match ? match[1].toLowerCase() : "";
    if (WORKSPACE_DOC_KINDS.has(kind)) {
        return kind;
    }
    return null;
}
async function readWorkspaceContent(path) {
    const kind = workspaceKindFromPath(path);
    if (kind) {
        const res = await fetchWorkspace();
        return res[kind] || "";
    }
    return (await api(`/api/workspace/file?path=${encodeURIComponent(path)}`));
}
async function writeWorkspaceContent(path, content) {
    const kind = workspaceKindFromPath(path);
    if (kind) {
        const res = await writeWorkspace(kind, content);
        return res[kind] || "";
    }
    return (await api(`/api/workspace/file?path=${encodeURIComponent(path)}`, {
        method: "PUT",
        body: { content },
    }));
}
function target() {
    if (!state.target)
        return "workspace:active_context";
    return `workspace:${state.target.path}`;
}
function setStatus(text) {
    const statusEl = els().status;
    if (statusEl)
        statusEl.textContent = text;
}
function renderPatch() {
    const { patchMain, patchBody, patchSummary, patchMeta, textarea, saveBtn, reloadBtn } = els();
    if (!patchMain || !patchBody)
        return;
    const draft = state.draft;
    if (draft) {
        patchMain.classList.remove("hidden");
        patchMain.classList.toggle("stale", Boolean(draft.is_stale));
        renderDiff(draft.patch || "(no diff)", patchBody);
        if (patchSummary) {
            patchSummary.textContent = draft.is_stale
                ? "Stale draft — file changed since this draft was created."
                : draft.agent_message || "Changes ready";
            patchSummary.classList.toggle("warn", Boolean(draft.is_stale));
        }
        if (patchMeta) {
            const created = draft.created_at || "";
            patchMeta.textContent = draft.is_stale
                ? `${created} · base ${draft.base_hash || ""} vs current ${draft.current_hash || ""}`.trim()
                : created;
        }
        if (textarea) {
            textarea.classList.add("hidden");
            textarea.disabled = true;
        }
        const patchApply = els().patchApply;
        if (patchApply)
            patchApply.textContent = draft.is_stale ? "Force Apply" : "Apply Draft";
        saveBtn?.setAttribute("disabled", "true");
        reloadBtn?.setAttribute("disabled", "true");
    }
    else {
        patchMain.classList.add("hidden");
        if (textarea) {
            textarea.classList.remove("hidden");
            textarea.disabled = false;
        }
        saveBtn?.removeAttribute("disabled");
        reloadBtn?.removeAttribute("disabled");
    }
}
function renderChat() {
    workspaceChat.render();
}
async function loadWorkspaceFile(path) {
    state.loading = true;
    setStatus("Loading…");
    try {
        const text = await readWorkspaceContent(path);
        state.content = text;
        if (state.docEditor) {
            state.docEditor.destroy();
        }
        const { textarea, saveBtn, status } = els();
        if (!textarea)
            return;
        state.docEditor = new DocEditor({
            target: target(),
            textarea,
            saveButton: saveBtn,
            statusEl: status,
            onLoad: async () => text,
            onSave: async (content) => {
                const saved = await writeWorkspaceContent(path, content);
                state.content = saved;
                if (saved !== content) {
                    textarea.value = saved;
                }
            },
        });
        await loadPendingDraft();
        renderPatch();
        setStatus("Loaded");
    }
    catch (err) {
        const message = err.message || "Failed to load workspace file";
        flash(message, "error");
        setStatus(message);
    }
    finally {
        state.loading = false;
    }
}
async function loadPendingDraft() {
    state.draft = await fetchPendingDraft(target());
    renderPatch();
}
async function reloadWorkspace() {
    if (!state.target)
        return;
    await loadWorkspaceFile(state.target.path);
}
async function maybeShowGenerate() {
    try {
        const res = await listTickets();
        const tickets = Array.isArray(res.tickets)
            ? res.tickets
            : [];
        state.hasTickets = tickets.length > 0;
    }
    catch {
        state.hasTickets = true;
    }
    const btn = els().generateBtn;
    if (btn)
        btn.classList.toggle("hidden", state.hasTickets);
}
async function generateTickets() {
    try {
        const res = await ingestSpecToTickets();
        flash(res.created > 0
            ? `Created ${res.created} ticket${res.created === 1 ? "" : "s"}`
            : "No tickets created", "success");
        await maybeShowGenerate();
    }
    catch (err) {
        flash(err.message || "Failed to generate tickets", "error");
    }
}
async function applyWorkspaceDraft() {
    try {
        const isStale = Boolean(state.draft?.is_stale);
        if (isStale) {
            const confirmForce = window.confirm("This draft is stale because the file changed after it was created. Force apply anyway?");
            if (!confirmForce)
                return;
        }
        const res = await applyDraft(target(), { force: isStale });
        const textarea = els().textarea;
        if (textarea) {
            textarea.value = res.content || "";
        }
        state.content = res.content || "";
        state.draft = null;
        renderPatch();
        flash(res.agent_message || "Draft applied", "success");
    }
    catch (err) {
        flash(err.message || "Failed to apply draft", "error");
    }
}
async function discardWorkspaceDraft() {
    try {
        const res = await discardDraft(target());
        const textarea = els().textarea;
        if (textarea)
            textarea.value = res.content || "";
        state.content = res.content || "";
        state.draft = null;
        renderPatch();
        flash("Draft discarded", "success");
    }
    catch (err) {
        flash(err.message || "Failed to discard draft", "error");
    }
}
async function sendChat() {
    const { chatInput, chatSend, chatCancel } = els();
    const message = (chatInput?.value || "").trim();
    if (!message)
        return;
    const chatState = workspaceChat.state;
    // Abort any in-flight chat first
    if (chatState.controller)
        chatState.controller.abort();
    chatState.controller = new AbortController();
    chatState.status = "running";
    chatState.error = "";
    chatState.statusText = "queued";
    chatState.streamText = "";
    workspaceChat.clearEvents();
    workspaceChat.addUserMessage(message);
    renderChat();
    if (chatInput)
        chatInput.value = "";
    chatSend?.setAttribute("disabled", "true");
    chatCancel?.classList.remove("hidden");
    const agent = getSelectedAgent();
    const model = getSelectedModel(agent) || undefined;
    const reasoning = getSelectedReasoning(agent) || undefined;
    try {
        await sendFileChat(target(), message, chatState.controller, {
            onStatus: (status) => {
                chatState.statusText = status;
                setStatus(status || "Running…");
                renderChat();
            },
            onToken: (token) => {
                chatState.streamText = (chatState.streamText || "") + token;
                workspaceChat.renderMessages();
            },
            onEvent: (event) => {
                workspaceChat.applyAppEvent(event);
                workspaceChat.renderEvents();
            },
            onUpdate: (update) => {
                const hasDraft = update.has_draft ?? update.hasDraft;
                if (hasDraft === false) {
                    chatState.draft = null;
                    if (typeof update.content === "string") {
                        state.content = update.content;
                        const textarea = els().textarea;
                        if (textarea)
                            textarea.value = state.content;
                    }
                    renderPatch();
                }
                else if (hasDraft === true || update.patch || update.content) {
                    state.draft = {
                        target: target(),
                        content: update.content || "",
                        patch: update.patch || "",
                        agent_message: update.agent_message,
                        created_at: update.created_at,
                        base_hash: update.base_hash,
                        current_hash: update.current_hash,
                        is_stale: Boolean(update.is_stale),
                    };
                    renderPatch();
                }
                if (update.message || update.agent_message) {
                    const text = update.message || update.agent_message || "";
                    if (text)
                        workspaceChat.addAssistantMessage(text);
                }
                renderChat();
            },
            onError: (msg) => {
                chatState.status = "error";
                chatState.error = msg;
                renderChat();
                flash(msg, "error");
            },
            onInterrupted: (msg) => {
                chatState.status = "interrupted";
                chatState.error = "";
                chatState.streamText = "";
                renderChat();
                flash(msg, "info");
            },
            onDone: () => {
                if (chatState.streamText) {
                    workspaceChat.addAssistantMessage(chatState.streamText);
                    chatState.streamText = "";
                }
                chatState.status = "done";
                renderChat();
            },
        }, { agent, model, reasoning });
    }
    catch (err) {
        const msg = err.message || "Chat failed";
        const chatStateLocal = workspaceChat.state;
        chatStateLocal.status = "error";
        chatStateLocal.error = msg;
        renderChat();
        flash(msg, "error");
    }
    finally {
        chatSend?.removeAttribute("disabled");
        chatCancel?.classList.add("hidden");
        const chatStateLocal = workspaceChat.state;
        chatStateLocal.controller = null;
    }
}
async function cancelChat() {
    const chatState = workspaceChat.state;
    if (chatState.controller) {
        chatState.controller.abort();
    }
    try {
        await interruptFileChat(target());
    }
    catch {
        // ignore
    }
    chatState.status = "interrupted";
    chatState.streamText = "";
    renderChat();
}
async function resetThread() {
    if (!state.target)
        return;
    try {
        await api("/api/app-server/threads/reset", {
            method: "POST",
            body: { key: `file_chat.workspace.${state.target.path}` },
        });
        const chatState = workspaceChat.state;
        chatState.messages = [];
        chatState.streamText = "";
        workspaceChat.clearEvents();
        renderChat();
        flash("New workspace chat thread", "success");
    }
    catch (err) {
        flash(err.message || "Failed to reset thread", "error");
    }
}
async function loadFiles() {
    const files = await listWorkspaceFiles();
    state.files = files;
    const { fileList, fileSelect } = els();
    if (!fileList)
        return;
    const browser = new WorkspaceFileBrowser({
        container: fileList,
        selectEl: fileSelect,
        onSelect: (file) => {
            state.target = { path: file.path, isPinned: file.is_pinned };
            workspaceChat.setTarget(target());
            void loadWorkspaceFile(file.path);
        },
    });
    state.browser = browser;
    browser.setFiles(files, files.find((f) => f.is_pinned)?.path);
    if (state.target) {
        workspaceChat.setTarget(target());
    }
}
export async function initWorkspace() {
    const { generateBtn, patchApply, patchDiscard, patchReload, chatSend, chatCancel, chatNewThread, } = els();
    if (!document.getElementById("workspace"))
        return;
    initAgentControls({
        agentSelect: els().agentSelect,
        modelSelect: els().modelSelect,
        reasoningSelect: els().reasoningSelect,
    });
    await initDocChatVoice({
        buttonId: "workspace-chat-voice",
        inputId: "workspace-chat-input",
    });
    await maybeShowGenerate();
    await loadFiles();
    workspaceChat.setTarget(target());
    els().saveBtn?.addEventListener("click", () => void state.docEditor?.save(true));
    els().reloadBtn?.addEventListener("click", () => void reloadWorkspace());
    generateBtn?.addEventListener("click", () => void generateTickets());
    patchApply?.addEventListener("click", () => void applyWorkspaceDraft());
    patchDiscard?.addEventListener("click", () => void discardWorkspaceDraft());
    patchReload?.addEventListener("click", () => void loadPendingDraft());
    chatSend?.addEventListener("click", () => void sendChat());
    chatCancel?.addEventListener("click", () => void cancelChat());
    chatNewThread?.addEventListener("click", () => void resetThread());
    const chatInput = els().chatInput;
    if (chatInput) {
        chatInput.addEventListener("keydown", (evt) => {
            if ((evt.metaKey || evt.ctrlKey) && evt.key === "Enter") {
                evt.preventDefault();
                void sendChat();
            }
        });
    }
}
