/**
 * Ticket Editor Modal - handles creating, editing, and deleting tickets
 */
import { api, confirmModal, flash, updateUrlParams, splitMarkdownFrontmatter } from "./utils.js";
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
  restoreTicketChatSelectionToActiveTurn,
  syncTicketChatTargetToSelection,
  ticketChatState,
  resumeTicketPendingTurn,
} from "./ticketChatActions.js";
import { initAgentControls } from "./agentControls.js";
import { initTicketVoice } from "./ticketVoice.js";
import { initTicketChatEvents, renderTicketEvents, renderTicketMessages } from "./ticketChatEvents.js";
import { initChatPasteUpload } from "./chatUploads.js";
import { DocEditor } from "./docEditor.js";
import { initTicketTemplates } from "./ticketTemplates.js";
import {
  type FrontmatterState,
  type TicketData,
  DEFAULT_FRONTMATTER,
  sameUndoSnapshot,
  extractFrontmatter,
  getFrontmatterFromForm,
  setFrontmatterForm,
  refreshFrontmatterAgentProfileControls,
  syncFrontmatterAgentProfileControls,
  refreshFmModelOptions,
  refreshFmReasoningOptions,
  getCatalogForAgent,
  buildTicketContent,
} from "./ticketEditorFrontmatter.js";

export type { TicketData };

type EditorState = {
  isOpen: boolean;
  isClosing: boolean;
  mode: "create" | "edit";
  ticketIndex: number | null;
  ticketChatKey: string | null;
  originalBody: string;
  originalFrontmatter: FrontmatterState;
  undoStack: Array<{ body: string; frontmatter: FrontmatterState }>;
  lastSavedBody: string;
  lastSavedFrontmatter: FrontmatterState;
};

const state: EditorState = {
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
let ticketDocEditor: DocEditor | null = null;
let ticketNavCache: TicketData[] = [];
let scheduledAutosaveTimer: ReturnType<typeof setTimeout> | null = null;
let scheduledAutosaveForce = false;
let autosaveInFlight: Promise<void> | null = null;
let autosaveNeedsRerun = false;
let autosaveAllowWhenClosedRequested = false;

type AutosaveOptions = {
  allowWhenClosed?: boolean;
};

function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable;
}

async function fetchTicketList(): Promise<TicketData[]> {
  const data = (await api("/api/flows/ticket_flow/tickets")) as { tickets?: TicketData[] };
  const list = (data?.tickets || []).filter((ticket) => typeof ticket.index === "number");
  list.sort((a, b) => (a.index ?? 0) - (b.index ?? 0));
  return list;
}

async function updateTicketNavButtons(): Promise<void> {
  const { prevBtn, nextBtn } = els();
  if (!prevBtn || !nextBtn) return;

  if (state.mode !== "edit" || state.ticketIndex == null) {
    prevBtn.disabled = true;
    nextBtn.disabled = true;
    return;
  }

  try {
    const list = await fetchTicketList();
    ticketNavCache = list;
  } catch {
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

async function navigateTicket(delta: -1 | 1): Promise<void> {
  if (state.mode !== "edit" || state.ticketIndex == null) return;

  await performAutosave();

  let list = ticketNavCache;
  if (!list.length) {
    try {
      list = await fetchTicketList();
      ticketNavCache = list;
    } catch {
      return;
    }
  }
  const idx = list.findIndex((ticket) => ticket.index === state.ticketIndex);
  const target = idx >= 0 ? list[idx + delta] : null;
  if (target && target.index != null) {
    try {
      const data = (await api(`/api/flows/ticket_flow/tickets/${target.index}`)) as TicketData;
      openTicketEditor(data);
    } catch (err) {
      flash(`Failed to navigate to ticket: ${(err as Error).message}`, "error");
    }
  }

  void updateTicketNavButtons();
}

function els(): {
  modal: HTMLElement | null;
  content: HTMLTextAreaElement | null;
  error: HTMLElement | null;
  deleteBtn: HTMLButtonElement | null;
  closeBtn: HTMLButtonElement | null;
  newBtn: HTMLButtonElement | null;
  insertCheckboxBtn: HTMLButtonElement | null;
  undoBtn: HTMLButtonElement | null;
  prevBtn: HTMLButtonElement | null;
  nextBtn: HTMLButtonElement | null;
  autosaveStatus: HTMLElement | null;
  fmAgent: HTMLSelectElement | null;
  fmModel: HTMLSelectElement | null;
  fmReasoning: HTMLSelectElement | null;
  fmProfile: HTMLSelectElement | null;
  fmDone: HTMLInputElement | null;
  fmTitle: HTMLInputElement | null;
  chatInput: HTMLTextAreaElement | null;
  chatSendBtn: HTMLButtonElement | null;
  chatVoiceBtn: HTMLButtonElement | null;
  chatCancelBtn: HTMLButtonElement | null;
  chatStatus: HTMLElement | null;
  patchApplyBtn: HTMLButtonElement | null;
  patchDiscardBtn: HTMLButtonElement | null;
  agentSelect: HTMLSelectElement | null;
  profileSelect: HTMLSelectElement | null;
  modelSelect: HTMLSelectElement | null;
  modelInput: HTMLInputElement | null;
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
      prevBtn: document.getElementById("ticket-nav-prev") as HTMLButtonElement | null,
      nextBtn: document.getElementById("ticket-nav-next") as HTMLButtonElement | null,
      autosaveStatus: document.getElementById("ticket-autosave-status"),
      fmAgent: document.getElementById("ticket-fm-agent") as HTMLSelectElement | null,
      fmModel: document.getElementById("ticket-fm-model") as HTMLSelectElement | null,
      fmReasoning: document.getElementById("ticket-fm-reasoning") as HTMLSelectElement | null,
      fmProfile: document.getElementById("ticket-fm-profile") as HTMLSelectElement | null,
      fmDone: document.getElementById("ticket-fm-done") as HTMLInputElement | null,
      fmTitle: document.getElementById("ticket-fm-title") as HTMLInputElement | null,
      chatInput: document.getElementById("ticket-chat-input") as HTMLTextAreaElement | null,
      chatSendBtn: document.getElementById("ticket-chat-send") as HTMLButtonElement | null,
      chatVoiceBtn: document.getElementById("ticket-chat-voice") as HTMLButtonElement | null,
      chatCancelBtn: document.getElementById("ticket-chat-cancel") as HTMLButtonElement | null,
      chatStatus: document.getElementById("ticket-chat-status") as HTMLElement | null,
      patchApplyBtn: document.getElementById("ticket-patch-apply") as HTMLButtonElement | null,
      patchDiscardBtn: document.getElementById("ticket-patch-discard") as HTMLButtonElement | null,
      agentSelect: document.getElementById("ticket-chat-agent-select") as HTMLSelectElement | null,
      profileSelect: document.getElementById("ticket-chat-profile-select") as HTMLSelectElement | null,
      modelSelect: document.getElementById("ticket-chat-model-select") as HTMLSelectElement | null,
      modelInput: document.getElementById("ticket-chat-model-input") as HTMLInputElement | null,
      reasoningSelect: document.getElementById("ticket-chat-reasoning-select") as HTMLSelectElement | null,
    };
  }

const fmEls = () => {
  const { fmAgent, fmModel, fmReasoning, fmProfile, fmDone, fmTitle } = els();
  return { fmAgent, fmModel, fmReasoning, fmProfile, fmDone, fmTitle };
};

function insertCheckbox(): void {
  const { content } = els();
  if (!content) return;

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

function pushUndoState(): void {
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

  if (undoBtn) undoBtn.disabled = state.undoStack.length <= 1;
}

function undoChange(): void {
  const { content, undoBtn } = els();
  if (!content || state.undoStack.length <= 1) return;

  state.undoStack.pop();

  const prev = state.undoStack[state.undoStack.length - 1];
  if (!prev) return;

  content.value = prev.body;
  setFrontmatterForm(fmEls, prev.frontmatter);

  scheduleAutosave(true);

  if (undoBtn) undoBtn.disabled = state.undoStack.length <= 1;
}

function updateUndoButton(): void {
  const { undoBtn } = els();
  if (undoBtn) {
    undoBtn.disabled = state.undoStack.length <= 1;
  }
}

function hasUnsavedChanges(): boolean {
  const { content } = els();
  const currentFm = getFrontmatterFromForm(fmEls, state.lastSavedFrontmatter, state.originalFrontmatter);
  const currentBody = content?.value || "";

  return (
    currentBody !== state.lastSavedBody ||
    currentFm.agent !== state.lastSavedFrontmatter.agent ||
    currentFm.done !== state.lastSavedFrontmatter.done ||
    currentFm.ticketId !== state.lastSavedFrontmatter.ticketId ||
    currentFm.title !== state.lastSavedFrontmatter.title ||
    currentFm.model !== state.lastSavedFrontmatter.model ||
    currentFm.reasoning !== state.lastSavedFrontmatter.reasoning ||
    currentFm.profile !== state.lastSavedFrontmatter.profile
  );
}

function scheduleAutosave(force = false): void {
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

function clearScheduledAutosave(): void {
  if (scheduledAutosaveTimer) {
    clearTimeout(scheduledAutosaveTimer);
    scheduledAutosaveTimer = null;
  }
  scheduledAutosaveForce = false;
}

async function performAutosave(options: AutosaveOptions = {}): Promise<void> {
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
    } finally {
      autosaveInFlight = null;
      autosaveNeedsRerun = false;
      autosaveAllowWhenClosedRequested = false;
    }
  })();

  await autosaveInFlight;
}

async function performAutosaveOnce(options: AutosaveOptions = {}): Promise<void> {
  const { content } = els();
  if (!content || (!state.isOpen && !options.allowWhenClosed)) return;

  if (!hasUnsavedChanges()) return;

  const fm = getFrontmatterFromForm(fmEls, state.lastSavedFrontmatter, state.originalFrontmatter);
  const fullContent = buildTicketContent(els, () => fm);

  if (!fm.agent) return;

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
      }) as TicketData;

      if (createRes?.index != null) {
        state.mode = "edit";
        state.ticketIndex = createRes.index;
        state.ticketChatKey = createRes.chat_key || null;
        const createdFm = (createRes.frontmatter || {}) as Record<string, unknown>;
        const createdExtra =
          typeof createdFm.extra === "object" && createdFm.extra
            ? (createdFm.extra as Record<string, unknown>)
            : {};
        const createdTicketId =
          typeof createdFm.ticket_id === "string"
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
    } else {
      if (state.ticketIndex == null) return;

      await api(`/api/flows/ticket_flow/tickets/${state.ticketIndex}`, {
        method: "PUT",
        body: { content: fullContent },
      });
    }

    state.lastSavedBody = content.value;
    state.lastSavedFrontmatter = { ...fm };

    setAutosaveStatus("saved");

    publish("tickets:updated", {});
  } catch (err) {
    setAutosaveStatus("error");
    flash((err as Error)?.message || "Failed to save ticket", "error");
    throw err;
  }
}

function onContentChange(): void {
  pushUndoState();
}

function onFrontmatterChange(): void {
  pushUndoState();
  scheduleAutosave(true);
}

export function openTicketEditor(ticket?: TicketData): void {
  const { modal, content, deleteBtn, chatInput, fmTitle } = els();
  if (!modal || !content) return;
  if (state.isClosing) return;

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
      if (fmModel && fm.model) fmModel.value = fm.model;
      if (fmReasoning && fm.reasoning) {
        const catalog = getCatalogForAgent(fm.agent);
        refreshFmReasoningOptions(fmEls, catalog, fm.model, fm.reasoning);
      }
    });

    let body = ticket.body || "";

    const [fmYaml, strippedBody] = splitMarkdownFrontmatter(body);
    if (fmYaml !== null) {
      body = strippedBody.trimStart();
    } else if (body.startsWith("---")) {
      flash("Malformed frontmatter detected in body", "error");
    } else {
      body = body.trimStart();
    }

    state.originalBody = body;
    state.lastSavedBody = body;
    content.value = body;

    if (deleteBtn) deleteBtn.classList.remove("hidden");

    setTicketIndex(ticket.index, state.ticketChatKey);
    void loadTicketPending(ticket.index, true);
  } else {
    state.mode = "create";
    state.ticketIndex = null;
    state.ticketChatKey = null;

    state.originalFrontmatter = { ...DEFAULT_FRONTMATTER };
    state.lastSavedFrontmatter = { ...DEFAULT_FRONTMATTER };
    refreshFrontmatterAgentProfileControls(
      fmEls,
      DEFAULT_FRONTMATTER.agent,
      DEFAULT_FRONTMATTER.profile
    );
    setFrontmatterForm(fmEls, DEFAULT_FRONTMATTER);

    void syncFrontmatterAgentProfileControls(
      fmEls,
      DEFAULT_FRONTMATTER.agent,
      DEFAULT_FRONTMATTER.profile
    ).then(() => {
      setFrontmatterForm(fmEls, DEFAULT_FRONTMATTER);
    });
    void refreshFmModelOptions(fmEls, DEFAULT_FRONTMATTER.agent, false);

    state.originalBody = "";
    state.lastSavedBody = "";
    content.value = "";

    if (deleteBtn) deleteBtn.classList.add("hidden");

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

  if (chatInput) chatInput.value = "";
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

export function closeTicketEditor(): void {
  const { modal } = els();
  if (!modal) return;
  if (state.isClosing) return;

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
    void performAutosave({ allowWhenClosed: true }).catch(() => {}).finally(finalizeClose);
    return;
  }

  finalizeClose();
}

export async function saveTicket(): Promise<void> {
  await performAutosave();
}

export async function deleteTicket(): Promise<void> {
  if (state.mode !== "edit" || state.ticketIndex == null) {
    flash("Cannot delete: no ticket selected", "error");
    return;
  }

  const confirmed = await confirmModal(
    `Delete TICKET-${String(state.ticketIndex).padStart(3, "0")}.md? This cannot be undone.`
  );
  if (!confirmed) return;

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
    if (modal) modal.classList.add("hidden");

    publish("tickets:updated", {});
  } catch (err) {
    showError((err as Error).message || "Failed to delete ticket");
  } finally {
    setButtonsLoading(false);
  }
}

export function initTicketEditor(): void {
  const {
    modal,
    content,
    deleteBtn,
    closeBtn,
    newBtn,
    insertCheckboxBtn,
    undoBtn,
    prevBtn,
    nextBtn,
    fmAgent,
    fmModel,
    fmReasoning,
    fmProfile,
    fmDone,
    fmTitle,
    chatInput,
    chatSendBtn,
    chatCancelBtn,
    patchApplyBtn,
    patchDiscardBtn,
    agentSelect,
    profileSelect,
    modelSelect,
    modelInput,
    reasoningSelect,
  } = els();
  if (!modal) return;

  if (modal.dataset.editorInitialized === "1") return;
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

  if (deleteBtn) deleteBtn.addEventListener("click", () => void deleteTicket());
  if (closeBtn) closeBtn.addEventListener("click", closeTicketEditor);
  if (newBtn) newBtn.addEventListener("click", () => openTicketEditor());
  if (insertCheckboxBtn) insertCheckboxBtn.addEventListener("click", insertCheckbox);
  if (undoBtn) undoBtn.addEventListener("click", undoChange);
  if (prevBtn) prevBtn.addEventListener("click", (e) => {
    e.preventDefault();
    void navigateTicket(-1);
  });
  if (nextBtn) nextBtn.addEventListener("click", (e) => {
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
  if (fmReasoning) fmReasoning.addEventListener("change", onFrontmatterChange);
  if (fmDone) fmDone.addEventListener("change", onFrontmatterChange);
  if (fmTitle) fmTitle.addEventListener("input", onFrontmatterChange);
  if (fmProfile) fmProfile.addEventListener("change", onFrontmatterChange);

  if (chatSendBtn) chatSendBtn.addEventListener("click", () => void sendTicketChat());
  if (chatCancelBtn) chatCancelBtn.addEventListener("click", () => void cancelTicketChat());
  if (patchApplyBtn) patchApplyBtn.addEventListener("click", () => void applyTicketPatch());
  if (patchDiscardBtn) patchDiscardBtn.addEventListener("click", () => void discardTicketPatch());
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
      if (active === chatInput) return;
      e.preventDefault();
      undoChange();
    }
  });

  document.addEventListener("keydown", (e) => {
    if (!state.isOpen) return;

    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;

    if (isTypingTarget(e.target)) return;

    if (!e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;

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
  isHermesAliasAgentId: (id: string) => {
    const normalized = (id || "").trim().toLowerCase();
    if (!normalized || normalized === "hermes") return false;
    return normalized.startsWith("hermes-") || normalized.startsWith("hermes_");
  },
  sameUndoSnapshot,
};
