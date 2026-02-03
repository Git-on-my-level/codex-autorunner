// GENERATED FILE - do not edit directly. Source: static_src/
/**
 * Ticket Editor Modal - handles creating, editing, and deleting tickets
 */
import { api, confirmModal, flash, updateUrlParams, splitMarkdownFrontmatter } from "./utils.js";
import { publish } from "./bus.js";
import { clearTicketChatHistory } from "./ticketChatStorage.js";
import { setTicketIndex, sendTicketChat, cancelTicketChat, applyTicketPatch, discardTicketPatch, loadTicketPending, renderTicketChat, resetTicketChatState, ticketChatState, resumeTicketPendingTurn, } from "./ticketChatActions.js";
import { initAgentControls } from "./agentControls.js";
import { initTicketVoice } from "./ticketVoice.js";
import { initTicketChatEvents, renderTicketEvents, renderTicketMessages } from "./ticketChatEvents.js";
import { initChatPasteUpload } from "./chatUploads.js";
import { DocEditor } from "./docEditor.js";
import { initTicketTemplates } from "./ticketTemplates.js";
const DEFAULT_FRONTMATTER = {
    agent: "codex",
    done: false,
    title: "",
    model: "",
    reasoning: "",
};
const state = {
    isOpen: false,
    mode: "create",
    ticketIndex: null,
    originalBody: "",
    originalFrontmatter: { ...DEFAULT_FRONTMATTER },
    undoStack: [],
    lastSavedBody: "",
    lastSavedFrontmatter: { ...DEFAULT_FRONTMATTER },
};
// Autosave debounce timer
const AUTOSAVE_DELAY_MS = 1000;
let ticketDocEditor = null;
let ticketNavCache = [];
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
        // Frontmatter form elements
        fmAgent: document.getElementById("ticket-fm-agent"),
        fmModel: document.getElementById("ticket-fm-model"),
        fmReasoning: document.getElementById("ticket-fm-reasoning"),
        fmDone: document.getElementById("ticket-fm-done"),
        fmTitle: document.getElementById("ticket-fm-title"),
        // Chat elements
        chatInput: document.getElementById("ticket-chat-input"),
        chatSendBtn: document.getElementById("ticket-chat-send"),
        chatVoiceBtn: document.getElementById("ticket-chat-voice"),
        chatCancelBtn: document.getElementById("ticket-chat-cancel"),
        chatStatus: document.getElementById("ticket-chat-status"),
        patchApplyBtn: document.getElementById("ticket-patch-apply"),
        patchDiscardBtn: document.getElementById("ticket-patch-discard"),
        // Agent control selects (for chat)
        agentSelect: document.getElementById("ticket-chat-agent-select"),
        modelSelect: document.getElementById("ticket-chat-model-select"),
        reasoningSelect: document.getElementById("ticket-chat-reasoning-select"),
    };
}
/**
 * Insert a checkbox at the current cursor position
 */
function insertCheckbox() {
    const { content } = els();
    if (!content)
        return;
    const pos = content.selectionStart;
    const text = content.value;
    const insert = "- [ ] ";
    // If at start of line or after newline, insert directly
    // Otherwise, insert on a new line
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
/**
 * Update the autosave status indicator
 */
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
            // Clear after a short delay
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
/**
 * Push current state to undo stack
 */
function pushUndoState() {
    const { content, undoBtn } = els();
    const fm = getFrontmatterFromForm();
    const body = content?.value || "";
    // Don't push if same as last undo state
    const last = state.undoStack[state.undoStack.length - 1];
    if (last && last.body === body &&
        last.frontmatter.agent === fm.agent &&
        last.frontmatter.done === fm.done &&
        last.frontmatter.title === fm.title &&
        last.frontmatter.model === fm.model &&
        last.frontmatter.reasoning === fm.reasoning) {
        return;
    }
    state.undoStack.push({ body, frontmatter: { ...fm } });
    // Limit stack size
    if (state.undoStack.length > 50) {
        state.undoStack.shift();
    }
    // Enable undo button
    if (undoBtn)
        undoBtn.disabled = state.undoStack.length <= 1;
}
/**
 * Undo to previous state
 */
function undoChange() {
    const { content, undoBtn } = els();
    if (!content || state.undoStack.length <= 1)
        return;
    // Pop current state
    state.undoStack.pop();
    // Get previous state
    const prev = state.undoStack[state.undoStack.length - 1];
    if (!prev)
        return;
    // Restore state
    content.value = prev.body;
    setFrontmatterForm(prev.frontmatter);
    // Trigger autosave for the restored state
    scheduleAutosave();
    // Update undo button
    if (undoBtn)
        undoBtn.disabled = state.undoStack.length <= 1;
}
/**
 * Update undo button state
 */
function updateUndoButton() {
    const { undoBtn } = els();
    if (undoBtn) {
        undoBtn.disabled = state.undoStack.length <= 1;
    }
}
/**
 * Get current frontmatter values from form fields
 */
function getFrontmatterFromForm() {
    const { fmAgent, fmModel, fmReasoning, fmDone, fmTitle } = els();
    return {
        agent: fmAgent?.value || "codex",
        done: fmDone?.checked || false,
        title: fmTitle?.value || "",
        model: fmModel?.value || "",
        reasoning: fmReasoning?.value || "",
    };
}
/**
 * Set frontmatter form fields from values
 */
function setFrontmatterForm(fm) {
    const { fmAgent, fmModel, fmReasoning, fmDone, fmTitle } = els();
    if (fmAgent)
        fmAgent.value = fm.agent;
    if (fmModel)
        fmModel.value = fm.model;
    if (fmReasoning)
        fmReasoning.value = fm.reasoning;
    if (fmDone)
        fmDone.checked = fm.done;
    if (fmTitle)
        fmTitle.value = fm.title;
}
/**
 * Extract frontmatter state from ticket data
 */
function extractFrontmatter(ticket) {
    const fm = ticket.frontmatter || {};
    return {
        agent: fm.agent || "codex",
        done: Boolean(fm.done),
        title: fm.title || "",
        model: fm.model || "",
        reasoning: fm.reasoning || "",
    };
}
/**
 * Build full markdown content from frontmatter form + body textarea
 */
function yamlQuote(value) {
    // Use JSON.stringify for simple, safe double-quoted scalars (handles colons, quotes, newlines).
    return JSON.stringify(value ?? "");
}
function buildTicketContent() {
    const { content } = els();
    const fm = getFrontmatterFromForm();
    const body = content?.value || "";
    // Reconstruct frontmatter YAML with quoted scalars to tolerate special characters.
    const lines = ["---"];
    lines.push(`agent: ${yamlQuote(fm.agent)}`);
    lines.push(`done: ${fm.done}`);
    if (fm.title)
        lines.push(`title: ${yamlQuote(fm.title)}`);
    if (fm.model)
        lines.push(`model: ${yamlQuote(fm.model)}`);
    if (fm.reasoning)
        lines.push(`reasoning: ${yamlQuote(fm.reasoning)}`);
    lines.push("---");
    lines.push("");
    lines.push(body);
    return lines.join("\n");
}
// Model catalog cache for frontmatter selects
const fmModelCatalogs = new Map();
/**
 * Load and populate the frontmatter model/reasoning selects based on the selected agent
 */
async function refreshFmModelOptions(agent, preserveSelection = false) {
    const { fmModel, fmReasoning } = els();
    if (!fmModel || !fmReasoning)
        return;
    const currentModel = preserveSelection ? fmModel.value : "";
    const currentReasoning = preserveSelection ? fmReasoning.value : "";
    // Fetch catalog if not cached
    if (!fmModelCatalogs.has(agent)) {
        try {
            const data = await api(`/api/agents/${encodeURIComponent(agent)}/models`, { method: "GET" });
            const models = Array.isArray(data?.models) ? data.models : [];
            const catalog = {
                default_model: data?.default_model || "",
                models,
            };
            fmModelCatalogs.set(agent, catalog);
        }
        catch {
            fmModelCatalogs.set(agent, null);
        }
    }
    const catalog = fmModelCatalogs.get(agent);
    // Populate model select
    fmModel.innerHTML = "";
    const defaultOption = document.createElement("option");
    defaultOption.value = "";
    defaultOption.textContent = "(default)";
    fmModel.appendChild(defaultOption);
    if (catalog?.models?.length) {
        fmModel.disabled = false;
        for (const m of catalog.models) {
            const opt = document.createElement("option");
            opt.value = m.id;
            opt.textContent = m.display_name && m.display_name !== m.id ? `${m.display_name} (${m.id})` : m.id;
            fmModel.appendChild(opt);
        }
        // Restore selection if valid
        if (currentModel && catalog.models.some((m) => m.id === currentModel)) {
            fmModel.value = currentModel;
        }
    }
    else {
        fmModel.disabled = true;
    }
    // Populate reasoning select based on selected model
    refreshFmReasoningOptions(catalog, fmModel.value, currentReasoning);
}
/**
 * Populate reasoning options based on selected model
 */
function refreshFmReasoningOptions(catalog, modelId, currentReasoning = "") {
    const { fmReasoning } = els();
    if (!fmReasoning)
        return;
    fmReasoning.innerHTML = "";
    const defaultOption = document.createElement("option");
    defaultOption.value = "";
    defaultOption.textContent = "(default)";
    fmReasoning.appendChild(defaultOption);
    const model = catalog?.models?.find((m) => m.id === modelId);
    if (model?.supports_reasoning && model.reasoning_options?.length) {
        fmReasoning.disabled = false;
        for (const r of model.reasoning_options) {
            const opt = document.createElement("option");
            opt.value = r;
            opt.textContent = r;
            fmReasoning.appendChild(opt);
        }
        // Restore selection if valid
        if (currentReasoning && model.reasoning_options.includes(currentReasoning)) {
            fmReasoning.value = currentReasoning;
        }
    }
    else {
        fmReasoning.disabled = true;
    }
}
/**
 * Check if there are unsaved changes (compared to last saved state)
 */
function hasUnsavedChanges() {
    const { content } = els();
    const currentFm = getFrontmatterFromForm();
    const currentBody = content?.value || "";
    return (currentBody !== state.lastSavedBody ||
        currentFm.agent !== state.lastSavedFrontmatter.agent ||
        currentFm.done !== state.lastSavedFrontmatter.done ||
        currentFm.title !== state.lastSavedFrontmatter.title ||
        currentFm.model !== state.lastSavedFrontmatter.model ||
        currentFm.reasoning !== state.lastSavedFrontmatter.reasoning);
}
/**
 * Schedule autosave with debounce
 */
function scheduleAutosave() {
    // DocEditor handles debounced autosave; leave for compatibility
    void ticketDocEditor?.save();
}
/**
 * Perform autosave (silent save without closing modal)
 */
async function performAutosave() {
    const { content } = els();
    if (!content || !state.isOpen)
        return;
    // Don't autosave if no changes
    if (!hasUnsavedChanges())
        return;
    const fm = getFrontmatterFromForm();
    const fullContent = buildTicketContent();
    // Validate required fields
    if (!fm.agent)
        return;
    setAutosaveStatus("saving");
    try {
        if (state.mode === "create") {
            // Create with form data
            const createRes = await api("/api/flows/ticket_flow/tickets", {
                method: "POST",
                body: {
                    agent: fm.agent,
                    title: fm.title || undefined,
                    body: content.value,
                },
            });
            if (createRes?.index != null) {
                // Switch to edit mode now that ticket exists
                state.mode = "edit";
                state.ticketIndex = createRes.index;
                // If done is true, update to set done flag
                if (fm.done) {
                    await api(`/api/flows/ticket_flow/tickets/${createRes.index}`, {
                        method: "PUT",
                        body: { content: fullContent },
                    });
                }
                // Set up chat for this ticket
                setTicketIndex(createRes.index);
            }
        }
        else {
            // Update existing
            if (state.ticketIndex == null)
                return;
            await api(`/api/flows/ticket_flow/tickets/${state.ticketIndex}`, {
                method: "PUT",
                body: { content: fullContent },
            });
        }
        // Update saved state
        state.lastSavedBody = content.value;
        state.lastSavedFrontmatter = { ...fm };
        setAutosaveStatus("saved");
        // Notify that tickets changed
        publish("tickets:updated", {});
    }
    catch (err) {
        // Surface the failure to the user and let DocEditor keep the "dirty" state
        // so a retry is attempted instead of falsely reporting success.
        setAutosaveStatus("error");
        flash(err?.message || "Failed to save ticket", "error");
        throw err;
    }
}
/**
 * Trigger change tracking and schedule autosave
 */
function onContentChange() {
    pushUndoState();
    scheduleAutosave();
}
function onFrontmatterChange() {
    pushUndoState();
    void ticketDocEditor?.save(true);
}
/**
 * Open the ticket editor modal
 * @param ticket - If provided, opens in edit mode; otherwise creates new ticket
 */
export function openTicketEditor(ticket) {
    const { modal, content, deleteBtn, chatInput, fmTitle } = els();
    if (!modal || !content)
        return;
    hideError();
    setAutosaveStatus("");
    if (ticket && ticket.index != null) {
        // Edit mode
        state.mode = "edit";
        state.ticketIndex = ticket.index;
        // Extract and set frontmatter
        const fm = extractFrontmatter(ticket);
        state.originalFrontmatter = { ...fm };
        state.lastSavedFrontmatter = { ...fm };
        setFrontmatterForm(fm);
        // Load model/reasoning options for the agent, then restore selections
        void refreshFmModelOptions(fm.agent, false).then(() => {
            const { fmModel, fmReasoning } = els();
            if (fmModel && fm.model)
                fmModel.value = fm.model;
            if (fmReasoning && fm.reasoning) {
                // Refresh reasoning options based on selected model first
                const catalog = fmModelCatalogs.get(fm.agent);
                refreshFmReasoningOptions(catalog, fm.model, fm.reasoning);
            }
        });
        // Set body (without frontmatter)
        let body = ticket.body || "";
        // If the body itself contains frontmatter, strip it if it's well-formed
        const [fmYaml, strippedBody] = splitMarkdownFrontmatter(body);
        if (fmYaml !== null) {
            body = strippedBody.trimStart();
        }
        else if (body.startsWith("---")) {
            // If it starts with --- but splitMarkdownFrontmatter returned null, it's malformed.
            // We keep it in the body so the user can see/fix it.
            flash("Malformed frontmatter detected in body", "error");
        }
        else {
            // Ensure we don't accumulate whitespace from the backend's normalization
            body = body.trimStart();
        }
        state.originalBody = body;
        state.lastSavedBody = body;
        content.value = body;
        if (deleteBtn)
            deleteBtn.classList.remove("hidden");
        // Set up chat for this ticket
        setTicketIndex(ticket.index);
        // Load any pending draft
        void loadTicketPending(ticket.index, true);
    }
    else {
        // Create mode
        state.mode = "create";
        state.ticketIndex = null;
        // Reset frontmatter to defaults
        state.originalFrontmatter = { ...DEFAULT_FRONTMATTER };
        state.lastSavedFrontmatter = { ...DEFAULT_FRONTMATTER };
        setFrontmatterForm(DEFAULT_FRONTMATTER);
        // Load model/reasoning options for the default agent
        void refreshFmModelOptions(DEFAULT_FRONTMATTER.agent, false);
        // Clear body
        state.originalBody = "";
        state.lastSavedBody = "";
        content.value = "";
        if (deleteBtn)
            deleteBtn.classList.add("hidden");
        // Clear chat state for new ticket
        setTicketIndex(null);
    }
    // Initialize undo stack with current state
    state.undoStack = [{ body: content.value, frontmatter: getFrontmatterFromForm() }];
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
    // Clear chat input
    if (chatInput)
        chatInput.value = "";
    renderTicketChat();
    renderTicketEvents();
    renderTicketMessages();
    void resumeTicketPendingTurn(ticket?.index ?? null);
    state.isOpen = true;
    modal.classList.remove("hidden");
    // Update URL with ticket index
    if (ticket?.index != null) {
        updateUrlParams({ ticket: ticket.index });
    }
    if (ticket?.path) {
        publish("ticket-editor:opened", { path: ticket.path, index: ticket.index ?? null });
    }
    void updateTicketNavButtons();
    // Focus on title field for new tickets
    if (state.mode === "create" && fmTitle) {
        fmTitle.focus();
    }
}
/**
 * Close the ticket editor modal (autosaves on close)
 */
export function closeTicketEditor() {
    const { modal } = els();
    if (!modal)
        return;
    // Autosave on close if there are changes
    if (hasUnsavedChanges()) {
        // Fire-and-forget: swallow rejection because the error is already flashed
        // inside performAutosave and DocEditor keeps the buffer dirty for retry.
        void performAutosave().catch(() => { });
    }
    // Cancel any running chat
    if (ticketChatState.status === "running") {
        void cancelTicketChat();
    }
    state.isOpen = false;
    state.ticketIndex = null;
    state.originalBody = "";
    state.originalFrontmatter = { ...DEFAULT_FRONTMATTER };
    state.lastSavedBody = "";
    state.lastSavedFrontmatter = { ...DEFAULT_FRONTMATTER };
    state.undoStack = [];
    modal.classList.add("hidden");
    hideError();
    ticketDocEditor?.destroy();
    ticketDocEditor = null;
    // Clear ticket from URL
    updateUrlParams({ ticket: null });
    void updateTicketNavButtons();
    // Reset chat state
    resetTicketChatState();
    setTicketIndex(null);
    // Notify that editor was closed (for selection state cleanup)
    publish("ticket-editor:closed", {});
}
/**
 * Save the current ticket (triggers immediate autosave)
 */
export async function saveTicket() {
    await performAutosave();
}
/**
 * Delete the current ticket (only available in edit mode)
 */
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
        clearTicketChatHistory(state.ticketIndex);
        flash("Ticket deleted");
        // Close modal
        state.isOpen = false;
        state.originalBody = "";
        state.originalFrontmatter = { ...DEFAULT_FRONTMATTER };
        const { modal } = els();
        if (modal)
            modal.classList.add("hidden");
        // Notify that tickets changed
        publish("tickets:updated", {});
    }
    catch (err) {
        showError(err.message || "Failed to delete ticket");
    }
    finally {
        setButtonsLoading(false);
    }
}
/**
 * Initialize the ticket editor - wire up event listeners
 */
export function initTicketEditor() {
    const { modal, content, deleteBtn, closeBtn, newBtn, insertCheckboxBtn, undoBtn, prevBtn, nextBtn, fmAgent, fmModel, fmReasoning, fmDone, fmTitle, chatInput, chatSendBtn, chatCancelBtn, patchApplyBtn, patchDiscardBtn, agentSelect, modelSelect, reasoningSelect, } = els();
    if (!modal)
        return;
    // Prevent double initialization
    if (modal.dataset.editorInitialized === "1")
        return;
    modal.dataset.editorInitialized = "1";
    // Initialize agent controls for ticket chat (populates agent/model/reasoning selects)
    initAgentControls({
        agentSelect,
        modelSelect,
        reasoningSelect,
    });
    // Initialize voice input for ticket chat
    void initTicketVoice();
    // Initialize rich chat experience (events toggle, etc.)
    initTicketChatEvents();
    // Initialize ticket templates picker
    initTicketTemplates();
    // Button handlers
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
    // Autosave on content changes
    if (content) {
        content.addEventListener("input", onContentChange);
    }
    // Autosave on frontmatter changes
    if (fmAgent) {
        fmAgent.addEventListener("change", () => {
            // Refresh model/reasoning options when agent changes
            void refreshFmModelOptions(fmAgent.value, false);
            onFrontmatterChange();
        });
    }
    if (fmModel) {
        fmModel.addEventListener("change", () => {
            // Refresh reasoning options when model changes
            const catalog = fmModelCatalogs.get(fmAgent?.value || "codex");
            refreshFmReasoningOptions(catalog, fmModel.value, fmReasoning?.value || "");
            onFrontmatterChange();
        });
    }
    if (fmReasoning)
        fmReasoning.addEventListener("change", onFrontmatterChange);
    if (fmDone)
        fmDone.addEventListener("change", onFrontmatterChange);
    if (fmTitle)
        fmTitle.addEventListener("input", onFrontmatterChange);
    // Chat button handlers
    if (chatSendBtn)
        chatSendBtn.addEventListener("click", () => void sendTicketChat());
    if (chatCancelBtn)
        chatCancelBtn.addEventListener("click", () => void cancelTicketChat());
    if (patchApplyBtn)
        patchApplyBtn.addEventListener("click", () => void applyTicketPatch());
    if (patchDiscardBtn)
        patchDiscardBtn.addEventListener("click", () => void discardTicketPatch());
    // Cmd/Ctrl+Enter in chat input sends message
    if (chatInput) {
        chatInput.addEventListener("keydown", (e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
                e.preventDefault();
                void sendTicketChat();
            }
        });
        // Auto-resize textarea on input
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
    // Close on backdrop click
    modal.addEventListener("click", (e) => {
        if (e.target === modal) {
            closeTicketEditor();
        }
    });
    // Close on Escape key
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && state.isOpen) {
            closeTicketEditor();
        }
    });
    // Cmd/Ctrl+Z triggers undo
    document.addEventListener("keydown", (e) => {
        if (state.isOpen && (e.metaKey || e.ctrlKey) && e.key === "z" && !e.shiftKey) {
            // Only handle if not in chat input
            const active = document.activeElement;
            if (active === chatInput)
                return;
            e.preventDefault();
            undoChange();
        }
    });
    // Left/Right arrows navigate between tickets when editor is open and not typing
    document.addEventListener("keydown", (e) => {
        if (!state.isOpen)
            return;
        // Check for navigation keys
        if (e.key !== "ArrowLeft" && e.key !== "ArrowRight")
            return;
        // Don't interfere with typing
        if (isTypingTarget(e.target))
            return;
        // Require Alt modifier for navigation (no Ctrl/Meta/Shift)
        if (!e.altKey || e.ctrlKey || e.metaKey || e.shiftKey)
            return;
        e.preventDefault();
        void navigateTicket(e.key === "ArrowLeft" ? -1 : 1);
    });
    // Enter key creates new TODO checkbox when on a checkbox line
    if (content) {
        content.addEventListener("keydown", (e) => {
            // Prevent manual frontmatter entry in the body
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
