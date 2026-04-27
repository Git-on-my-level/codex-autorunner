// GENERATED FILE - do not edit directly. Source: static_src/
/**
 * Ticket Editor Modal - handles creating, editing, and deleting tickets
 */
import { api, confirmModal, flash, updateUrlParams, splitMarkdownFrontmatter } from "./utils.js?v=62070ab22f9201700f4cbe1ce8b08b2a7cf7419dd93d9677cdfc7ba5c9537a14";
import { publish } from "./bus.js?v=62070ab22f9201700f4cbe1ce8b08b2a7cf7419dd93d9677cdfc7ba5c9537a14";
import { clearTicketChatHistory } from "./ticketChatStorage.js?v=62070ab22f9201700f4cbe1ce8b08b2a7cf7419dd93d9677cdfc7ba5c9537a14";
import { setTicketIndex, sendTicketChat, cancelTicketChat, applyTicketPatch, discardTicketPatch, loadTicketPending, renderTicketChat, resetTicketChatState, restoreTicketChatSelectionToActiveTurn, syncTicketChatTargetToSelection, ticketChatState, resumeTicketPendingTurn, } from "./ticketChatActions.js?v=62070ab22f9201700f4cbe1ce8b08b2a7cf7419dd93d9677cdfc7ba5c9537a14";
import { initAgentControls } from "./agentControls.js?v=62070ab22f9201700f4cbe1ce8b08b2a7cf7419dd93d9677cdfc7ba5c9537a14";
import { initTicketVoice } from "./ticketVoice.js?v=62070ab22f9201700f4cbe1ce8b08b2a7cf7419dd93d9677cdfc7ba5c9537a14";
import { initTicketChatEvents, renderTicketEvents, renderTicketMessages } from "./ticketChatEvents.js?v=62070ab22f9201700f4cbe1ce8b08b2a7cf7419dd93d9677cdfc7ba5c9537a14";
import { initChatPasteUpload } from "./chatUploads.js?v=62070ab22f9201700f4cbe1ce8b08b2a7cf7419dd93d9677cdfc7ba5c9537a14";
import { DocEditor } from "./docEditor.js?v=62070ab22f9201700f4cbe1ce8b08b2a7cf7419dd93d9677cdfc7ba5c9537a14";
import { initTicketTemplates } from "./ticketTemplates.js?v=62070ab22f9201700f4cbe1ce8b08b2a7cf7419dd93d9677cdfc7ba5c9537a14";
import { DEFAULT_FRONTMATTER, sameUndoSnapshot, extractFrontmatter, getFrontmatterFromForm, setFrontmatterForm, refreshFrontmatterAgentProfileControls, syncFrontmatterAgentProfileControls, refreshFmModelOptions, refreshFmReasoningOptions, getCatalogForAgent, buildTicketContent, } from "./ticketEditorFrontmatter.js?v=62070ab22f9201700f4cbe1ce8b08b2a7cf7419dd93d9677cdfc7ba5c9537a14";
const state = {
    isOpen: false,
    isClosing: false,
    mode: "create",
    ticketIndex: null,
    ticketChatKey: null,
    originalBody: "",
    originalFrontmatter: { ...DEFAULT_FRONTMATTER },
    undoStack: [],
    lastSavedBody: "",
    lastSavedFrontmatter: { ...DEFAULT_FRONTMATTER },
};
const AUTOSAVE_DELAY_MS = 1000;
let ticketDocEditor = null;
let ticketNavCache = [];
let scheduledAutosaveTimer = null;
let scheduledAutosaveForce = false;
let autosaveInFlight = null;
let autosaveNeedsRerun = false;
let autosaveAllowWhenClosedRequested = false;
function isTypingTarget(target) {
    if (!(target instanceof HTMLElement))
        return false;
    const tag = target.tagName;
    return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable;
}
async function fetchTicketList() {
    const data = (await api("/api/flows/ticket_flow/tickets"));
    const list = (data?.tickets || []).filter((ticket) => typeof ticket.index === "number");
    list.sort((a, b) => (a.index ?? 0) - (b.index ?? 0));
    return list;
}
async function updateTicketNavButtons() {
    const { prevBtn, nextBtn } = els();
    if (!prevBtn || !nextBtn)
        return;
    if (state.mode !== "edit" || state.ticketIndex == null) {
        prevBtn.disabled = true;
        nextBtn.disabled = true;
        return;
    }
    try {
        const list = await fetchTicketList();
        ticketNavCache = list;
    }
    catch {
        // If fetch fails, fall back to the last known list.
    }
    const list = ticketNavCache;
    if (!list.length) {
        prevBtn.disabled = true;
        nextBtn.disabled = true;
        return;
    }
    const idx = list.findIndex((ticket) => ticket.index === state.ticketIndex);
    const hasPrev = idx > 0;
    const hasNext = idx >= 0 && idx < list.length - 1;
    prevBtn.disabled = !hasPrev;
    nextBtn.disabled = !hasNext;
}
async function navigateTicket(delta) {
    if (state.mode !== "edit" || state.ticketIndex == null)
        return;
    await performAutosave();
    let list = ticketNavCache;
    if (!list.length) {
        try {
            list = await fetchTicketList();
            ticketNavCache = list;
        }
        catch {
            return;
        }
    }
    const idx = list.findIndex((ticket) => ticket.index === state.ticketIndex);
    const target = idx >= 0 ? list[idx + delta] : null;
    if (target && target.index != null) {
        try {
            const data = (await api(`/api/flows/ticket_flow/tickets/${target.index}`));
            openTicketEditor(data);
        }
        catch (err) {
            flash(`Failed to navigate to ticket: ${err.message}`, "error");
        }
    }
    void updateTicketNavButtons();
}
function els() {
    return {
        modal: document.getElementById("ticket-editor-modal"),
        content: document.getElementById("ticket-editor-content"),
        error: document.getElementById("ticket-editor-error"),
        deleteBtn: document.getElementById("ticket-editor-delete"),
        closeBtn: document.getElementById("ticket-editor-close"),
        newBtn: document.getElementById("ticket-new-btn"),
        insertCheckboxBtn: document.getElementById("ticket-insert-checkbox"),
        undoBtn: document.getElementById("ticket-undo-btn"),
        prevBtn: document.getElementById("ticket-nav-prev"),
        nextBtn: document.getElementById("ticket-nav-next"),
        autosaveStatus: document.getElementById("ticket-autosave-status"),
        fmAgent: document.getElementById("ticket-fm-agent"),
        fmModel: document.getElementById("ticket-fm-model"),
        fmReasoning: document.getElementById("ticket-fm-reasoning"),
        fmProfile: document.getElementById("ticket-fm-profile"),
        fmDone: document.getElementById("ticket-fm-done"),
        fmTitle: document.getElementById("ticket-fm-title"),
        chatInput: document.getElementById("ticket-chat-input"),
        chatSendBtn: document.getElementById("ticket-chat-send"),
        chatVoiceBtn: document.getElementById("ticket-chat-voice"),
        chatCancelBtn: document.getElementById("ticket-chat-cancel"),
        chatStatus: document.getElementById("ticket-chat-status"),
        patchApplyBtn: document.getElementById("ticket-patch-apply"),
        patchDiscardBtn: document.getElementById("ticket-patch-discard"),
        agentSelect: document.getElementById("ticket-chat-agent-select"),
        profileSelect: document.getElementById("ticket-chat-profile-select"),
        modelSelect: document.getElementById("ticket-chat-model-select"),
        modelInput: document.getElementById("ticket-chat-model-input"),
        reasoningSelect: document.getElementById("ticket-chat-reasoning-select"),
    };
}
const fmEls = () => {
    const { fmAgent, fmModel, fmReasoning, fmProfile, fmDone, fmTitle } = els();
    return { fmAgent, fmModel, fmReasoning, fmProfile, fmDone, fmTitle };
};
function insertCheckbox() {
    const { content } = els();
    if (!content)
        return;
    const pos = content.selectionStart;
    const text = content.value;
    const insert = "- [ ] ";
    const needsNewline = pos > 0 && text[pos - 1] !== "\n";
    const toInsert = needsNewline ? "\n" + insert : insert;
    content.value = text.slice(0, pos) + toInsert + text.slice(pos);
    const newPos = pos + toInsert.length;
    content.setSelectionRange(newPos, newPos);
    content.focus();
}
function showError(message) {
    const { error } = els();
    if (!error)
        return;
    error.textContent = message;
    error.classList.remove("hidden");
}
function hideError() {
    const { error } = els();
    if (!error)
        return;
    error.textContent = "";
    error.classList.add("hidden");
}
function setButtonsLoading(loading) {
    const { deleteBtn, closeBtn, undoBtn } = els();
    [deleteBtn, closeBtn, undoBtn].forEach((btn) => {
        if (btn)
            btn.disabled = loading;
    });
}
function setAutosaveStatus(status) {
    const { autosaveStatus } = els();
    if (!autosaveStatus)
        return;
    switch (status) {
        case "saving":
            autosaveStatus.textContent = "Saving…";
            autosaveStatus.classList.remove("error");
            break;
        case "saved":
            autosaveStatus.textContent = "Saved";
            autosaveStatus.classList.remove("error");
            setTimeout(() => {
                if (autosaveStatus.textContent === "Saved") {
                    autosaveStatus.textContent = "";
                }
            }, 2000);
            break;
        case "error":
            autosaveStatus.textContent = "Save failed";
            autosaveStatus.classList.add("error");
            break;
        default:
            autosaveStatus.textContent = "";
            autosaveStatus.classList.remove("error");
    }
}
function pushUndoState() {
    const { content, undoBtn } = els();
    const fm = getFrontmatterFromForm(fmEls, state.lastSavedFrontmatter, state.originalFrontmatter);
    const body = content?.value || "";
    const nextState = { body, frontmatter: { ...fm } };
    const last = state.undoStack[state.undoStack.length - 1];
    if (sameUndoSnapshot(last, nextState)) {
        return;
    }
    state.undoStack.push(nextState);
    if (state.undoStack.length > 50) {
        state.undoStack.shift();
    }
    if (undoBtn)
        undoBtn.disabled = state.undoStack.length <= 1;
}
function undoChange() {
    const { content, undoBtn } = els();
    if (!content || state.undoStack.length <= 1)
        return;
    state.undoStack.pop();
    const prev = state.undoStack[state.undoStack.length - 1];
    if (!prev)
        return;
    content.value = prev.body;
    setFrontmatterForm(fmEls, prev.frontmatter);
    scheduleAutosave(true);
    if (undoBtn)
        undoBtn.disabled = state.undoStack.length <= 1;
}
function updateUndoButton() {
    const { undoBtn } = els();
    if (undoBtn) {
        undoBtn.disabled = state.undoStack.length <= 1;
    }
}
function hasUnsavedChanges() {
    const { content } = els();
    const currentFm = getFrontmatterFromForm(fmEls, state.lastSavedFrontmatter, state.originalFrontmatter);
    const currentBody = content?.value || "";
    return (currentBody !== state.lastSavedBody ||
        currentFm.agent !== state.lastSavedFrontmatter.agent ||
        currentFm.done !== state.lastSavedFrontmatter.done ||
        currentFm.ticketId !== state.lastSavedFrontmatter.ticketId ||
        currentFm.title !== state.lastSavedFrontmatter.title ||
        currentFm.model !== state.lastSavedFrontmatter.model ||
        currentFm.reasoning !== state.lastSavedFrontmatter.reasoning ||
        currentFm.profile !== state.lastSavedFrontmatter.profile);
}
function scheduleAutosave(force = false) {
    scheduledAutosaveForce = scheduledAutosaveForce || force;
    if (scheduledAutosaveTimer) {
        clearTimeout(scheduledAutosaveTimer);
    }
    scheduledAutosaveTimer = setTimeout(() => {
        scheduledAutosaveTimer = null;
        const runForce = scheduledAutosaveForce;
        scheduledAutosaveForce = false;
        void ticketDocEditor?.save(runForce);
    }, AUTOSAVE_DELAY_MS);
}
function clearScheduledAutosave() {
    if (scheduledAutosaveTimer) {
        clearTimeout(scheduledAutosaveTimer);
        scheduledAutosaveTimer = null;
    }
    scheduledAutosaveForce = false;
}
async function performAutosave(options = {}) {
    if (options.allowWhenClosed) {
        autosaveAllowWhenClosedRequested = true;
    }
    if (autosaveInFlight) {
        autosaveNeedsRerun = true;
        await autosaveInFlight;
        return;
    }
    autosaveInFlight = (async () => {
        try {
            do {
                const allowWhenClosed = autosaveAllowWhenClosedRequested;
                autosaveAllowWhenClosedRequested = false;
                autosaveNeedsRerun = false;
                await performAutosaveOnce({ allowWhenClosed });
            } while (autosaveNeedsRerun);
        }
        finally {
            autosaveInFlight = null;
            autosaveNeedsRerun = false;
            autosaveAllowWhenClosedRequested = false;
        }
    })();
    await autosaveInFlight;
}
async function performAutosaveOnce(options = {}) {
    const { content } = els();
    if (!content || (!state.isOpen && !options.allowWhenClosed))
        return;
    if (!hasUnsavedChanges())
        return;
    const fm = getFrontmatterFromForm(fmEls, state.lastSavedFrontmatter, state.originalFrontmatter);
    const fullContent = buildTicketContent(els, () => fm);
    if (!fm.agent)
        return;
    setAutosaveStatus("saving");
    try {
        if (state.mode === "create") {
            const createRes = await api("/api/flows/ticket_flow/tickets", {
                method: "POST",
                body: {
                    agent: fm.agent,
                    title: fm.title || undefined,
                    body: content.value,
                    profile: fm.profile || undefined,
                },
            });
            if (createRes?.index != null) {
                state.mode = "edit";
                state.ticketIndex = createRes.index;
                state.ticketChatKey = createRes.chat_key || null;
                const createdFm = (createRes.frontmatter || {});
                const createdExtra = typeof createdFm.extra === "object" && createdFm.extra
                    ? createdFm.extra
                    : {};
                const createdTicketId = typeof createdFm.ticket_id === "string"
                    ? createdFm.ticket_id
                    : typeof createdExtra.ticket_id === "string"
                        ? createdExtra.ticket_id
                        : "";
                if (createdTicketId) {
                    fm.ticketId = createdTicketId;
                }
                if (fm.done) {
                    await api(`/api/flows/ticket_flow/tickets/${createRes.index}`, {
                        method: "PUT",
                        body: { content: fullContent },
                    });
                }
                setTicketIndex(createRes.index, state.ticketChatKey);
            }
        }
        else {
            if (state.ticketIndex == null)
                return;
            await api(`/api/flows/ticket_flow/tickets/${state.ticketIndex}`, {
                method: "PUT",
                body: { content: fullContent },
            });
        }
        state.lastSavedBody = content.value;
        state.lastSavedFrontmatter = { ...fm };
        setAutosaveStatus("saved");
        publish("tickets:updated", {});
    }
    catch (err) {
        setAutosaveStatus("error");
        flash(err?.message || "Failed to save ticket", "error");
        throw err;
    }
}
function onContentChange() {
    pushUndoState();
}
function onFrontmatterChange() {
    pushUndoState();
    scheduleAutosave(true);
}
export function openTicketEditor(ticket) {
    const { modal, content, deleteBtn, chatInput, fmTitle } = els();
    if (!modal || !content)
        return;
    if (state.isClosing)
        return;
    clearScheduledAutosave();
    hideError();
    setAutosaveStatus("");
    if (ticket && ticket.index != null) {
        state.mode = "edit";
        state.ticketIndex = ticket.index;
        state.ticketChatKey = ticket.chat_key || null;
        const fm = extractFrontmatter(ticket);
        state.originalFrontmatter = { ...fm };
        state.lastSavedFrontmatter = { ...fm };
        refreshFrontmatterAgentProfileControls(fmEls, fm.agent, fm.profile);
        setFrontmatterForm(fmEls, fm);
        void syncFrontmatterAgentProfileControls(fmEls, fm.agent, fm.profile).then(() => {
            setFrontmatterForm(fmEls, fm);
        });
        void refreshFmModelOptions(fmEls, fm.agent, false).then(() => {
            const { fmModel, fmReasoning } = els();
            if (fmModel && fm.model)
                fmModel.value = fm.model;
            if (fmReasoning && fm.reasoning) {
                const catalog = getCatalogForAgent(fm.agent);
                refreshFmReasoningOptions(fmEls, catalog, fm.model, fm.reasoning);
            }
        });
        let body = ticket.body || "";
        const [fmYaml, strippedBody] = splitMarkdownFrontmatter(body);
        if (fmYaml !== null) {
            body = strippedBody.trimStart();
        }
        else if (body.startsWith("---")) {
            flash("Malformed frontmatter detected in body", "error");
        }
        else {
            body = body.trimStart();
        }
        state.originalBody = body;
        state.lastSavedBody = body;
        content.value = body;
        if (deleteBtn)
            deleteBtn.classList.remove("hidden");
        setTicketIndex(ticket.index, state.ticketChatKey);
        void loadTicketPending(ticket.index, true);
    }
    else {
        state.mode = "create";
        state.ticketIndex = null;
        state.ticketChatKey = null;
        state.originalFrontmatter = { ...DEFAULT_FRONTMATTER };
        state.lastSavedFrontmatter = { ...DEFAULT_FRONTMATTER };
        refreshFrontmatterAgentProfileControls(fmEls, DEFAULT_FRONTMATTER.agent, DEFAULT_FRONTMATTER.profile);
        setFrontmatterForm(fmEls, DEFAULT_FRONTMATTER);
        void syncFrontmatterAgentProfileControls(fmEls, DEFAULT_FRONTMATTER.agent, DEFAULT_FRONTMATTER.profile).then(() => {
            setFrontmatterForm(fmEls, DEFAULT_FRONTMATTER);
        });
        void refreshFmModelOptions(fmEls, DEFAULT_FRONTMATTER.agent, false);
        state.originalBody = "";
        state.lastSavedBody = "";
        content.value = "";
        if (deleteBtn)
            deleteBtn.classList.add("hidden");
        setTicketIndex(null, null);
    }
    state.undoStack = [{ body: content.value, frontmatter: getFrontmatterFromForm(fmEls, state.lastSavedFrontmatter, state.originalFrontmatter) }];
    updateUndoButton();
    if (ticketDocEditor) {
        ticketDocEditor.destroy();
    }
    ticketDocEditor = new DocEditor({
        target: state.ticketIndex != null ? `ticket:${state.ticketIndex}` : "ticket:new",
        textarea: content,
        statusEl: els().autosaveStatus,
        autoSaveDelay: AUTOSAVE_DELAY_MS,
        onLoad: async () => content.value,
        onSave: async () => {
            await performAutosave();
        },
    });
    if (chatInput)
        chatInput.value = "";
    renderTicketChat();
    renderTicketEvents();
    renderTicketMessages();
    void resumeTicketPendingTurn(ticket?.index ?? null, ticket?.chat_key || null);
    state.isOpen = true;
    modal.classList.remove("hidden");
    if (ticket?.index != null) {
        updateUrlParams({ ticket: ticket.index });
    }
    if (ticket?.path) {
        publish("ticket-editor:opened", { path: ticket.path, index: ticket.index ?? null });
    }
    void updateTicketNavButtons();
    if (state.mode === "create" && fmTitle) {
        fmTitle.focus();
    }
}
export function closeTicketEditor() {
    const { modal } = els();
    if (!modal)
        return;
    if (state.isClosing)
        return;
    clearScheduledAutosave();
    state.isOpen = false;
    state.isClosing = true;
    modal.classList.add("hidden");
    hideError();
    const finalizeClose = () => {
        if (ticketChatState.status === "running") {
            void cancelTicketChat();
        }
        state.ticketIndex = null;
        state.ticketChatKey = null;
        state.originalBody = "";
        state.originalFrontmatter = { ...DEFAULT_FRONTMATTER };
        state.lastSavedBody = "";
        state.lastSavedFrontmatter = { ...DEFAULT_FRONTMATTER };
        state.undoStack = [];
        ticketDocEditor?.destroy();
        ticketDocEditor = null;
        state.isClosing = false;
        updateUrlParams({ ticket: null });
        void updateTicketNavButtons();
        resetTicketChatState();
        setTicketIndex(null, null);
        publish("ticket-editor:closed", {});
    };
    if (hasUnsavedChanges()) {
        void performAutosave({ allowWhenClosed: true }).catch(() => { }).finally(finalizeClose);
        return;
    }
    finalizeClose();
}
export async function saveTicket() {
    await performAutosave();
}
export async function deleteTicket() {
    if (state.mode !== "edit" || state.ticketIndex == null) {
        flash("Cannot delete: no ticket selected", "error");
        return;
    }
    const confirmed = await confirmModal(`Delete TICKET-${String(state.ticketIndex).padStart(3, "0")}.md? This cannot be undone.`);
    if (!confirmed)
        return;
    setButtonsLoading(true);
    hideError();
    try {
        await api(`/api/flows/ticket_flow/tickets/${state.ticketIndex}`, {
            method: "DELETE",
        });
        clearTicketChatHistory(state.ticketChatKey || state.ticketIndex);
        flash("Ticket deleted");
        state.isOpen = false;
        state.originalBody = "";
        state.originalFrontmatter = { ...DEFAULT_FRONTMATTER };
        const { modal } = els();
        if (modal)
            modal.classList.add("hidden");
        publish("tickets:updated", {});
    }
    catch (err) {
        showError(err.message || "Failed to delete ticket");
    }
    finally {
        setButtonsLoading(false);
    }
}
export function initTicketEditor() {
    const { modal, content, deleteBtn, closeBtn, newBtn, insertCheckboxBtn, undoBtn, prevBtn, nextBtn, fmAgent, fmModel, fmReasoning, fmProfile, fmDone, fmTitle, chatInput, chatSendBtn, chatCancelBtn, patchApplyBtn, patchDiscardBtn, agentSelect, profileSelect, modelSelect, modelInput, reasoningSelect, } = els();
    if (!modal)
        return;
    if (modal.dataset.editorInitialized === "1")
        return;
    modal.dataset.editorInitialized = "1";
    initAgentControls({
        agentSelect,
        profileSelect,
        modelSelect,
        modelInput,
        reasoningSelect,
    });
    void initTicketVoice();
    initTicketChatEvents();
    initTicketTemplates();
    if (deleteBtn)
        deleteBtn.addEventListener("click", () => void deleteTicket());
    if (closeBtn)
        closeBtn.addEventListener("click", closeTicketEditor);
    if (newBtn)
        newBtn.addEventListener("click", () => openTicketEditor());
    if (insertCheckboxBtn)
        insertCheckboxBtn.addEventListener("click", insertCheckbox);
    if (undoBtn)
        undoBtn.addEventListener("click", undoChange);
    if (prevBtn)
        prevBtn.addEventListener("click", (e) => {
            e.preventDefault();
            void navigateTicket(-1);
        });
    if (nextBtn)
        nextBtn.addEventListener("click", (e) => {
            e.preventDefault();
            void navigateTicket(1);
        });
    if (content) {
        content.addEventListener("input", onContentChange);
    }
    if (fmAgent) {
        fmAgent.addEventListener("change", () => {
            void (async () => {
                await syncFrontmatterAgentProfileControls(fmEls, fmAgent.value, "");
                await refreshFmModelOptions(fmEls, fmAgent.value, false);
                onFrontmatterChange();
            })();
        });
    }
    if (fmModel) {
        fmModel.addEventListener("change", () => {
            const catalog = getCatalogForAgent(fmAgent?.value || "codex");
            refreshFmReasoningOptions(fmEls, catalog, fmModel.value, fmReasoning?.value || "");
            onFrontmatterChange();
        });
    }
    if (fmReasoning)
        fmReasoning.addEventListener("change", onFrontmatterChange);
    if (fmDone)
        fmDone.addEventListener("change", onFrontmatterChange);
    if (fmTitle)
        fmTitle.addEventListener("input", onFrontmatterChange);
    if (fmProfile)
        fmProfile.addEventListener("change", onFrontmatterChange);
    if (chatSendBtn)
        chatSendBtn.addEventListener("click", () => void sendTicketChat());
    if (chatCancelBtn)
        chatCancelBtn.addEventListener("click", () => void cancelTicketChat());
    if (patchApplyBtn)
        patchApplyBtn.addEventListener("click", () => void applyTicketPatch());
    if (patchDiscardBtn)
        patchDiscardBtn.addEventListener("click", () => void discardTicketPatch());
    if (agentSelect) {
        agentSelect.addEventListener("change", () => {
            if (ticketChatState.status === "running") {
                flash("Finish or cancel the current ticket chat turn before switching agents.", "error");
                void restoreTicketChatSelectionToActiveTurn();
                return;
            }
            syncTicketChatTargetToSelection();
            void resumeTicketPendingTurn(ticketChatState.ticketIndex, ticketChatState.ticketChatKey);
        });
    }
    if (profileSelect) {
        profileSelect.addEventListener("change", () => {
            if (ticketChatState.status === "running") {
                flash("Finish or cancel the current ticket chat turn before switching profiles.", "error");
                void restoreTicketChatSelectionToActiveTurn();
                return;
            }
            syncTicketChatTargetToSelection();
            void resumeTicketPendingTurn(ticketChatState.ticketIndex, ticketChatState.ticketChatKey);
        });
    }
    if (chatInput) {
        chatInput.addEventListener("keydown", (e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
                e.preventDefault();
                void sendTicketChat();
            }
        });
        chatInput.addEventListener("input", () => {
            chatInput.style.height = "auto";
            chatInput.style.height = Math.min(chatInput.scrollHeight, 100) + "px";
        });
        initChatPasteUpload({
            textarea: chatInput,
            basePath: "/api/filebox",
            box: "inbox",
            insertStyle: "both",
            pathPrefix: ".codex-autorunner/filebox",
        });
    }
    modal.addEventListener("click", (e) => {
        if (e.target === modal) {
            closeTicketEditor();
        }
    });
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && state.isOpen) {
            closeTicketEditor();
        }
    });
    document.addEventListener("keydown", (e) => {
        if (state.isOpen && (e.metaKey || e.ctrlKey) && e.key === "z" && !e.shiftKey) {
            const active = document.activeElement;
            if (active === chatInput)
                return;
            e.preventDefault();
            undoChange();
        }
    });
    document.addEventListener("keydown", (e) => {
        if (!state.isOpen)
            return;
        if (e.key !== "ArrowLeft" && e.key !== "ArrowRight")
            return;
        if (isTypingTarget(e.target))
            return;
        if (!e.altKey || e.ctrlKey || e.metaKey || e.shiftKey)
            return;
        e.preventDefault();
        void navigateTicket(e.key === "ArrowLeft" ? -1 : 1);
    });
    if (content) {
        content.addEventListener("keydown", (e) => {
            if (e.key === "-" && content.selectionStart === 2 && content.value.startsWith("--") && !content.value.includes("\n")) {
                flash("Please use the frontmatter editor above", "error");
                e.preventDefault();
                return;
            }
            if (e.key === "Enter" && !e.isComposing && !e.shiftKey) {
                const text = content.value;
                const pos = content.selectionStart;
                const lineStart = text.lastIndexOf("\n", pos - 1) + 1;
                const lineEnd = text.indexOf("\n", pos);
                const currentLine = text.slice(lineStart, lineEnd === -1 ? text.length : lineEnd);
                const match = currentLine.match(/^(\s*)- \[(x|X| )?\]/);
                if (match) {
                    e.preventDefault();
                    const indent = match[1];
                    const newLine = "\n" + indent + "- [ ] ";
                    const endOfCurrentLine = lineEnd === -1 ? text.length : lineEnd;
                    const newValue = text.slice(0, endOfCurrentLine) + newLine + text.slice(endOfCurrentLine);
                    content.value = newValue;
                    const newPos = endOfCurrentLine + newLine.length;
                    content.setSelectionRange(newPos, newPos);
                }
            }
        });
    }
}
export const __ticketEditorTest = {
    isHermesAliasAgentId: (id) => {
        const normalized = (id || "").trim().toLowerCase();
        if (!normalized || normalized === "hermes")
            return false;
        return normalized.startsWith("hermes-") || normalized.startsWith("hermes_");
    },
    sameUndoSnapshot,
};
