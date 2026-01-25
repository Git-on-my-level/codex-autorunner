import { flash } from "./utils.js";
import { initAgentControls, getSelectedAgent, getSelectedModel, getSelectedReasoning } from "./agentControls.js";
import { fetchWorkspace, writeWorkspace, ingestSpecToTickets, listTickets, } from "./workspaceApi.js";
import { applyDraft, discardDraft, fetchPendingDraft, sendFileChat, interruptFileChat, } from "./fileChat.js";
const state = {
    kind: "active_context",
    content: "",
    draft: null,
    loading: false,
    chatStatus: "idle",
    streamText: "",
    statusText: "",
    error: "",
    controller: null,
    messages: [],
    events: [],
    hasTickets: true,
};
function els() {
    return {
        tabs: Array.from(document.querySelectorAll("[data-workspace]")),
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
function target() {
    return `workspace:${state.kind}`;
}
function setStatus(text) {
    const statusEl = els().status;
    if (statusEl)
        statusEl.textContent = text;
}
function renderTabs() {
    for (const tab of els().tabs) {
        const key = (tab.dataset.workspace || "");
        tab.classList.toggle("active", key === state.kind);
    }
}
function renderPatch() {
    const { patchMain, patchBody, patchSummary, patchMeta, textarea, saveBtn, reloadBtn } = els();
    if (!patchMain || !patchBody)
        return;
    const draft = state.draft;
    if (draft) {
        patchMain.classList.remove("hidden");
        patchBody.textContent = draft.patch || "(no diff)";
        if (patchSummary)
            patchSummary.textContent = draft.agent_message || "Changes ready";
        if (patchMeta)
            patchMeta.textContent = draft.created_at || "";
        if (textarea) {
            textarea.classList.add("hidden");
            textarea.disabled = true;
        }
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
    const { chatStatus, chatError, chatMessages, chatEvents, chatEventsList } = els();
    if (chatStatus) {
        chatStatus.textContent = state.chatStatus;
        chatStatus.classList.toggle("error", state.chatStatus === "error");
    }
    if (chatError) {
        chatError.textContent = state.error;
        chatError.classList.toggle("hidden", !state.error);
    }
    if (chatMessages) {
        chatMessages.innerHTML = "";
        state.messages.forEach((m) => {
            const div = document.createElement("div");
            div.className = `doc-chat-message ${m.role}`;
            div.textContent = m.content;
            chatMessages.appendChild(div);
        });
        if (state.streamText) {
            const streaming = document.createElement("div");
            streaming.className = "doc-chat-message assistant streaming";
            streaming.textContent = state.streamText;
            chatMessages.appendChild(streaming);
        }
    }
    if (chatEvents && chatEventsList) {
        chatEventsList.innerHTML = "";
        state.events.forEach((e) => {
            const row = document.createElement("div");
            row.className = "doc-chat-event";
            row.textContent = e.summary;
            chatEventsList.appendChild(row);
        });
        chatEvents.classList.toggle("hidden", state.events.length === 0);
    }
}
function addMessage(role, content) {
    state.messages.push({ role, content });
}
function resetChat() {
    state.chatStatus = "idle";
    state.error = "";
    state.streamText = "";
    state.statusText = "";
    state.controller = null;
    state.events = [];
}
async function loadWorkspace(kind) {
    state.loading = true;
    setStatus("Loading…");
    try {
        const data = await fetchWorkspace();
        state.kind = kind;
        state.content = data[kind] || "";
        const textarea = els().textarea;
        if (textarea)
            textarea.value = state.content;
        await loadPendingDraft();
        renderTabs();
        renderPatch();
        setStatus("Loaded");
    }
    catch (err) {
        const message = err.message || "Failed to load workspace";
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
async function saveWorkspace() {
    const textarea = els().textarea;
    const content = textarea?.value ?? "";
    try {
        const res = await writeWorkspace(state.kind, content);
        state.content = res[state.kind];
        flash("Workspace saved", "success");
    }
    catch (err) {
        flash(err.message || "Failed to save", "error");
    }
}
async function reloadWorkspace() {
    await loadWorkspace(state.kind);
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
function bindTabClicks() {
    for (const tab of els().tabs) {
        tab.addEventListener("click", () => {
            const key = (tab.dataset.workspace || "");
            if (!key || key === state.kind)
                return;
            loadWorkspace(key).catch((err) => flash(err.message, "error"));
        });
    }
}
async function applyWorkspaceDraft() {
    try {
        const res = await applyDraft(target());
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
    // Abort any in-flight chat first
    if (state.controller)
        state.controller.abort();
    state.controller = new AbortController();
    resetChat();
    state.chatStatus = "running";
    addMessage("user", message);
    renderChat();
    chatInput.value = "";
    chatSend?.setAttribute("disabled", "true");
    chatCancel?.classList.remove("hidden");
    const agent = getSelectedAgent();
    const model = getSelectedModel(agent) || undefined;
    const reasoning = getSelectedReasoning(agent) || undefined;
    try {
        await sendFileChat(target(), message, state.controller, {
            onStatus: (status) => {
                state.statusText = status;
                setStatus(status || "Running…");
            },
            onToken: (token) => {
                state.streamText = (state.streamText || "") + token;
                renderChat();
            },
            onEvent: (event) => {
                state.events.push({ summary: JSON.stringify(event) });
                if (state.events.length > 20)
                    state.events.shift();
                renderChat();
            },
            onUpdate: (update) => {
                if (update.patch || update.content) {
                    state.draft = {
                        target: target(),
                        content: update.content || "",
                        patch: update.patch || "",
                        agent_message: update.agent_message,
                        created_at: update.created_at,
                        base_hash: update.base_hash,
                    };
                    renderPatch();
                }
                if (update.message || update.agent_message) {
                    const text = update.message || update.agent_message || "";
                    if (text)
                        addMessage("assistant", text);
                }
                renderChat();
            },
            onError: (msg) => {
                state.chatStatus = "error";
                state.error = msg;
                renderChat();
                flash(msg, "error");
            },
            onInterrupted: (msg) => {
                state.chatStatus = "interrupted";
                state.error = "";
                state.streamText = "";
                renderChat();
                flash(msg, "info");
            },
            onDone: () => {
                if (state.streamText) {
                    addMessage("assistant", state.streamText);
                    state.streamText = "";
                }
                state.chatStatus = "done";
                renderChat();
            },
        }, { agent, model, reasoning });
    }
    catch (err) {
        const msg = err.message || "Chat failed";
        state.chatStatus = "error";
        state.error = msg;
        renderChat();
        flash(msg, "error");
    }
    finally {
        chatSend?.removeAttribute("disabled");
        chatCancel?.classList.add("hidden");
        state.controller = null;
    }
}
async function cancelChat() {
    if (state.controller) {
        state.controller.abort();
    }
    try {
        await interruptFileChat(target());
    }
    catch {
        // ignore
    }
    state.chatStatus = "interrupted";
    state.streamText = "";
    renderChat();
}
async function resetThread() {
    try {
        await fetch("/api/app-server/threads/reset", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ key: `file_chat.workspace.${state.kind}` }),
        });
        state.messages = [];
        state.events = [];
        resetChat();
        renderChat();
        flash("New workspace chat thread", "success");
    }
    catch (err) {
        flash(err.message || "Failed to reset thread", "error");
    }
}
export async function initWorkspace() {
    const { saveBtn, reloadBtn, generateBtn, patchApply, patchDiscard, patchReload, chatSend, chatCancel, chatNewThread, } = els();
    if (!document.getElementById("workspace"))
        return;
    initAgentControls({
        agentSelect: els().agentSelect,
        modelSelect: els().modelSelect,
        reasoningSelect: els().reasoningSelect,
    });
    bindTabClicks();
    maybeShowGenerate();
    await loadWorkspace(state.kind);
    saveBtn?.addEventListener("click", () => void saveWorkspace());
    reloadBtn?.addEventListener("click", () => void reloadWorkspace());
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
