/**
 * Ticket Editor Modal - handles creating, editing, and deleting tickets
 */
import { api, flash, updateUrlParams } from "./utils.js";
import { publish } from "./bus.js";
import { clearTicketChatHistory } from "./ticketChatStorage.js";
import {
  setTicketIndex,
  sendTicketChat,
  cancelTicketChat,
  applyTicketPatch,
  discardTicketPatch,
  loadTicketPending,
  renderTicketChat,
  resetTicketChatState,
  ticketChatState,
} from "./ticketChatActions.js";
import { initAgentControls } from "./agentControls.js";
import { initTicketVoice } from "./ticketVoice.js";
import { initTicketChatEvents, renderTicketEvents, renderTicketMessages } from "./ticketChatEvents.js";

type TicketData = {
  path?: string;
  index?: number | null;
  frontmatter?: Record<string, unknown> | null;
  body?: string | null;
  errors?: string[];
};

type FrontmatterState = {
  agent: string;
  done: boolean;
  title: string;
};

type EditorState = {
  isOpen: boolean;
  mode: "create" | "edit";
  ticketIndex: number | null;
  originalBody: string;
  originalFrontmatter: FrontmatterState;
  // Undo support
  undoStack: Array<{ body: string; frontmatter: FrontmatterState }>;
  lastSavedBody: string;
  lastSavedFrontmatter: FrontmatterState;
};

const DEFAULT_FRONTMATTER: FrontmatterState = {
  agent: "codex",
  done: false,
  title: "",
};

const state: EditorState = {
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
let autosaveTimer: ReturnType<typeof setTimeout> | null = null;
const AUTOSAVE_DELAY_MS = 1000;

function els(): {
  modal: HTMLElement | null;
  content: HTMLTextAreaElement | null;
  error: HTMLElement | null;
  deleteBtn: HTMLButtonElement | null;
  closeBtn: HTMLButtonElement | null;
  newBtn: HTMLButtonElement | null;
  insertCheckboxBtn: HTMLButtonElement | null;
  undoBtn: HTMLButtonElement | null;
  autosaveStatus: HTMLElement | null;
  // Frontmatter form elements
  fmAgent: HTMLSelectElement | null;
  fmDone: HTMLInputElement | null;
  fmTitle: HTMLInputElement | null;
  // Chat elements
  chatInput: HTMLTextAreaElement | null;
  chatSendBtn: HTMLButtonElement | null;
  chatVoiceBtn: HTMLButtonElement | null;
  chatCancelBtn: HTMLButtonElement | null;
  chatStatus: HTMLElement | null;
  patchApplyBtn: HTMLButtonElement | null;
  patchDiscardBtn: HTMLButtonElement | null;
  // Agent control selects (for chat)
  agentSelect: HTMLSelectElement | null;
  modelSelect: HTMLSelectElement | null;
  reasoningSelect: HTMLSelectElement | null;
} {
  return {
    modal: document.getElementById("ticket-editor-modal"),
    content: document.getElementById("ticket-editor-content") as HTMLTextAreaElement | null,
    error: document.getElementById("ticket-editor-error"),
    deleteBtn: document.getElementById("ticket-editor-delete") as HTMLButtonElement | null,
    closeBtn: document.getElementById("ticket-editor-close") as HTMLButtonElement | null,
    newBtn: document.getElementById("ticket-new-btn") as HTMLButtonElement | null,
    insertCheckboxBtn: document.getElementById("ticket-insert-checkbox") as HTMLButtonElement | null,
    undoBtn: document.getElementById("ticket-undo-btn") as HTMLButtonElement | null,
    autosaveStatus: document.getElementById("ticket-autosave-status"),
    // Frontmatter form elements
    fmAgent: document.getElementById("ticket-fm-agent") as HTMLSelectElement | null,
    fmDone: document.getElementById("ticket-fm-done") as HTMLInputElement | null,
    fmTitle: document.getElementById("ticket-fm-title") as HTMLInputElement | null,
    // Chat elements
    chatInput: document.getElementById("ticket-chat-input") as HTMLTextAreaElement | null,
    chatSendBtn: document.getElementById("ticket-chat-send") as HTMLButtonElement | null,
    chatVoiceBtn: document.getElementById("ticket-chat-voice") as HTMLButtonElement | null,
    chatCancelBtn: document.getElementById("ticket-chat-cancel") as HTMLButtonElement | null,
    chatStatus: document.getElementById("ticket-chat-status") as HTMLElement | null,
    patchApplyBtn: document.getElementById("ticket-patch-apply") as HTMLButtonElement | null,
    patchDiscardBtn: document.getElementById("ticket-patch-discard") as HTMLButtonElement | null,
    // Agent control selects (for chat)
    agentSelect: document.getElementById("ticket-chat-agent-select") as HTMLSelectElement | null,
    modelSelect: document.getElementById("ticket-chat-model-select") as HTMLSelectElement | null,
    reasoningSelect: document.getElementById("ticket-chat-reasoning-select") as HTMLSelectElement | null,
  };
}

/**
 * Insert a checkbox at the current cursor position
 */
function insertCheckbox(): void {
  const { content } = els();
  if (!content) return;

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

function showError(message: string): void {
  const { error } = els();
  if (!error) return;
  error.textContent = message;
  error.classList.remove("hidden");
}

function hideError(): void {
  const { error } = els();
  if (!error) return;
  error.textContent = "";
  error.classList.add("hidden");
}

function setButtonsLoading(loading: boolean): void {
  const { deleteBtn, closeBtn, undoBtn } = els();
  [deleteBtn, closeBtn, undoBtn].forEach((btn) => {
    if (btn) btn.disabled = loading;
  });
}

/**
 * Update the autosave status indicator
 */
function setAutosaveStatus(status: "saving" | "saved" | "error" | ""): void {
  const { autosaveStatus } = els();
  if (!autosaveStatus) return;
  
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
function pushUndoState(): void {
  const { content, undoBtn } = els();
  const fm = getFrontmatterFromForm();
  const body = content?.value || "";
  
  // Don't push if same as last undo state
  const last = state.undoStack[state.undoStack.length - 1];
  if (last && last.body === body && 
      last.frontmatter.agent === fm.agent &&
      last.frontmatter.done === fm.done &&
      last.frontmatter.title === fm.title) {
    return;
  }
  
  state.undoStack.push({ body, frontmatter: { ...fm } });
  
  // Limit stack size
  if (state.undoStack.length > 50) {
    state.undoStack.shift();
  }
  
  // Enable undo button
  if (undoBtn) undoBtn.disabled = state.undoStack.length <= 1;
}

/**
 * Undo to previous state
 */
function undoChange(): void {
  const { content, undoBtn } = els();
  if (!content || state.undoStack.length <= 1) return;
  
  // Pop current state
  state.undoStack.pop();
  
  // Get previous state
  const prev = state.undoStack[state.undoStack.length - 1];
  if (!prev) return;
  
  // Restore state
  content.value = prev.body;
  setFrontmatterForm(prev.frontmatter);
  
  // Trigger autosave for the restored state
  scheduleAutosave();
  
  // Update undo button
  if (undoBtn) undoBtn.disabled = state.undoStack.length <= 1;
}

/**
 * Update undo button state
 */
function updateUndoButton(): void {
  const { undoBtn } = els();
  if (undoBtn) {
    undoBtn.disabled = state.undoStack.length <= 1;
  }
}

/**
 * Get current frontmatter values from form fields
 */
function getFrontmatterFromForm(): FrontmatterState {
  const { fmAgent, fmDone, fmTitle } = els();
  return {
    agent: fmAgent?.value || "codex",
    done: fmDone?.checked || false,
    title: fmTitle?.value || "",
  };
}

/**
 * Set frontmatter form fields from values
 */
function setFrontmatterForm(fm: FrontmatterState): void {
  const { fmAgent, fmDone, fmTitle } = els();
  if (fmAgent) fmAgent.value = fm.agent;
  if (fmDone) fmDone.checked = fm.done;
  if (fmTitle) fmTitle.value = fm.title;
}

/**
 * Extract frontmatter state from ticket data
 */
function extractFrontmatter(ticket: TicketData): FrontmatterState {
  const fm = ticket.frontmatter || {};
  return {
    agent: (fm.agent as string) || "codex",
    done: Boolean(fm.done),
    title: (fm.title as string) || "",
  };
}

/**
 * Build full markdown content from frontmatter form + body textarea
 */
function buildTicketContent(): string {
  const { content } = els();
  const fm = getFrontmatterFromForm();
  const body = content?.value || "";

  // Reconstruct frontmatter YAML
  const lines: string[] = ["---"];

  lines.push(`agent: ${fm.agent}`);
  lines.push(`done: ${fm.done}`);
  if (fm.title) lines.push(`title: ${fm.title}`);

  lines.push("---");
  lines.push("");
  lines.push(body);

  return lines.join("\n");
}

/**
 * Check if there are unsaved changes (compared to last saved state)
 */
function hasUnsavedChanges(): boolean {
  const { content } = els();
  const currentFm = getFrontmatterFromForm();
  const currentBody = content?.value || "";
  
  return (
    currentBody !== state.lastSavedBody ||
    currentFm.agent !== state.lastSavedFrontmatter.agent ||
    currentFm.done !== state.lastSavedFrontmatter.done ||
    currentFm.title !== state.lastSavedFrontmatter.title
  );
}

/**
 * Schedule autosave with debounce
 */
function scheduleAutosave(): void {
  if (autosaveTimer) {
    clearTimeout(autosaveTimer);
  }
  
  autosaveTimer = setTimeout(() => {
    void performAutosave();
  }, AUTOSAVE_DELAY_MS);
}

/**
 * Perform autosave (silent save without closing modal)
 */
async function performAutosave(): Promise<void> {
  const { content } = els();
  if (!content || !state.isOpen) return;
  
  // Don't autosave if no changes
  if (!hasUnsavedChanges()) return;
  
  const fm = getFrontmatterFromForm();
  const fullContent = buildTicketContent();
  
  // Validate required fields
  if (!fm.agent) return;
  
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
      }) as { index?: number };

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
    } else {
      // Update existing
      if (state.ticketIndex == null) return;

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
  } catch {
    setAutosaveStatus("error");
  }
}

/**
 * Trigger change tracking and schedule autosave
 */
function onContentChange(): void {
  pushUndoState();
  scheduleAutosave();
}

/**
 * Open the ticket editor modal
 * @param ticket - If provided, opens in edit mode; otherwise creates new ticket
 */
export function openTicketEditor(ticket?: TicketData): void {
  const { modal, content, deleteBtn, chatInput, fmTitle } = els();
  if (!modal || !content) return;

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
    
    // Set body (without frontmatter)
    const body = ticket.body || "";
    state.originalBody = body;
    state.lastSavedBody = body;
    content.value = body;
    
    if (deleteBtn) deleteBtn.classList.remove("hidden");
    
    // Set up chat for this ticket
    setTicketIndex(ticket.index);
    // Load any pending draft
    void loadTicketPending(ticket.index, true);
  } else {
    // Create mode
    state.mode = "create";
    state.ticketIndex = null;
    
    // Reset frontmatter to defaults
    state.originalFrontmatter = { ...DEFAULT_FRONTMATTER };
    state.lastSavedFrontmatter = { ...DEFAULT_FRONTMATTER };
    setFrontmatterForm(DEFAULT_FRONTMATTER);
    
    // Clear body
    state.originalBody = "";
    state.lastSavedBody = "";
    content.value = "";
    
    if (deleteBtn) deleteBtn.classList.add("hidden");
    
    // Clear chat state for new ticket
    setTicketIndex(null);
  }

  // Initialize undo stack with current state
  state.undoStack = [{ body: content.value, frontmatter: getFrontmatterFromForm() }];
  updateUndoButton();

  // Clear chat input
  if (chatInput) chatInput.value = "";
  renderTicketChat();
  renderTicketEvents();
  renderTicketMessages();

  state.isOpen = true;
  modal.classList.remove("hidden");
  
  // Update URL with ticket index
  if (ticket?.index != null) {
    updateUrlParams({ ticket: ticket.index });
  }

  // Focus on title field for new tickets, body for existing
  if (state.mode === "create" && fmTitle) {
    fmTitle.focus();
  } else {
    content.focus();
  }
}

/**
 * Close the ticket editor modal (autosaves on close)
 */
export function closeTicketEditor(): void {
  const { modal } = els();
  if (!modal) return;

  // Cancel any pending autosave timer
  if (autosaveTimer) {
    clearTimeout(autosaveTimer);
    autosaveTimer = null;
  }

  // Autosave on close if there are changes
  if (hasUnsavedChanges()) {
    void performAutosave();
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

  // Clear ticket from URL
  updateUrlParams({ ticket: null });
  
  // Reset chat state
  resetTicketChatState();
  setTicketIndex(null);
}

/**
 * Save the current ticket (triggers immediate autosave)
 */
export async function saveTicket(): Promise<void> {
  await performAutosave();
}

/**
 * Delete the current ticket (only available in edit mode)
 */
export async function deleteTicket(): Promise<void> {
  if (state.mode !== "edit" || state.ticketIndex == null) {
    flash("Cannot delete: no ticket selected", "error");
    return;
  }

  const confirmed = window.confirm(
    `Delete TICKET-${String(state.ticketIndex).padStart(3, "0")}.md? This cannot be undone.`
  );
  if (!confirmed) return;

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
    if (modal) modal.classList.add("hidden");

    // Notify that tickets changed
    publish("tickets:updated", {});
  } catch (err) {
    showError((err as Error).message || "Failed to delete ticket");
  } finally {
    setButtonsLoading(false);
  }
}

/**
 * Initialize the ticket editor - wire up event listeners
 */
export function initTicketEditor(): void {
  const {
    modal,
    content,
    deleteBtn,
    closeBtn,
    newBtn,
    insertCheckboxBtn,
    undoBtn,
    fmAgent,
    fmDone,
    fmTitle,
    chatInput,
    chatSendBtn,
    chatCancelBtn,
    patchApplyBtn,
    patchDiscardBtn,
    agentSelect,
    modelSelect,
    reasoningSelect,
  } = els();
  if (!modal) return;

  // Prevent double initialization
  if (modal.dataset.editorInitialized === "1") return;
  modal.dataset.editorInitialized = "1";

  // Initialize agent controls (populates agent/model/reasoning selects)
  initAgentControls({
    agentSelect,
    modelSelect,
    reasoningSelect,
  });

  // Initialize voice input for ticket chat
  void initTicketVoice();

  // Initialize rich chat experience (events toggle, etc.)
  initTicketChatEvents();

  // Button handlers
  if (deleteBtn) deleteBtn.addEventListener("click", () => void deleteTicket());
  if (closeBtn) closeBtn.addEventListener("click", closeTicketEditor);
  if (newBtn) newBtn.addEventListener("click", () => openTicketEditor());
  if (insertCheckboxBtn) insertCheckboxBtn.addEventListener("click", insertCheckbox);
  if (undoBtn) undoBtn.addEventListener("click", undoChange);

  // Autosave on content changes
  if (content) {
    content.addEventListener("input", onContentChange);
  }
  
  // Autosave on frontmatter changes
  if (fmAgent) fmAgent.addEventListener("change", onContentChange);
  if (fmDone) fmDone.addEventListener("change", onContentChange);
  if (fmTitle) fmTitle.addEventListener("input", onContentChange);

  // Chat button handlers
  if (chatSendBtn) chatSendBtn.addEventListener("click", () => void sendTicketChat());
  if (chatCancelBtn) chatCancelBtn.addEventListener("click", () => void cancelTicketChat());
  if (patchApplyBtn) patchApplyBtn.addEventListener("click", () => void applyTicketPatch());
  if (patchDiscardBtn) patchDiscardBtn.addEventListener("click", () => void discardTicketPatch());

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

  // Cmd/Ctrl+S triggers immediate save
  document.addEventListener("keydown", (e) => {
    if (state.isOpen && (e.metaKey || e.ctrlKey) && e.key === "s") {
      e.preventDefault();
      void performAutosave();
    }
  });
  
  // Cmd/Ctrl+Z triggers undo
  document.addEventListener("keydown", (e) => {
    if (state.isOpen && (e.metaKey || e.ctrlKey) && e.key === "z" && !e.shiftKey) {
      // Only handle if not in chat input
      const active = document.activeElement;
      if (active === chatInput) return;
      e.preventDefault();
      undoChange();
    }
  });

  // Enter key creates new TODO checkbox when on a checkbox line
  if (content) {
    content.addEventListener("keydown", (e) => {
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

export { TicketData };
