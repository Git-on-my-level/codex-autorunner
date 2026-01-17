import { api, flash, confirmModal } from "./utils.js";
import { chatUI } from "./docsElements.js";
import {
  CHAT_HISTORY_LIMIT,
  DOC_TYPES,
  docsState,
  getActiveDoc,
  getChatState,
  getDocChatViewing,
  getDraft,
  isDraftPreview,
  setDraft,
  setDraftPreview,
  setHistoryNavIndex,
  resetChatEvents,
} from "./docsState.js";
import { normalizeDraftPayload, parseChatPayload } from "./docsParse.js";
import { renderChat } from "./docChatRender.js";
import { performDocChatRequest } from "./docChatStream.js";
import { applyDocUpdateFromChat } from "./docsDocUpdates.js";
import { autoResizeTextarea, getDocTextarea, syncDocEditor } from "./docsUi.js";
import {
  getSelectedAgent,
  getSelectedModel,
  getSelectedReasoning,
} from "./agentControls.js";

function markChatError(state, entry, message) {
  entry.status = "error";
  entry.error = message;
  state.error = message;
  state.status = "error";
  renderChat();
}

function restoreChatInput(entry) {
  if (!entry?.prompt || chatUI.input.value) return;
  chatUI.input.value = entry.prompt;
  autoResizeTextarea(chatUI.input);
}

async function interruptDocChat() {
  try {
    await api("/api/docs/chat/interrupt", { method: "POST" });
  } catch (err) {
    flash(err.message || "Failed to interrupt doc chat", "error");
  }
}

export function cancelDocChat() {
  const state = getChatState();
  if (state.status !== "running") return;
  interruptDocChat();
  if (state.controller) state.controller.abort();
  resetChatEvents(state, { preserve: true });
  const entry = state.history[0];
  if (entry && entry.status === "running") {
    entry.status = "interrupted";
    entry.error = "Interrupted";
  }
  state.status = "interrupted";
  state.error = "";
  state.streamText = "";
  state.statusText = "";
  state.controller = null;
  renderChat();
}

export async function startNewDocChatThread() {
  const state = getChatState();
  if (state.status === "running") {
    cancelDocChat();
  }
  const agent = getSelectedAgent();
  const key = agent === "opencode" ? "doc_chat.opencode" : "doc_chat";
  try {
    await api("/api/app-server/threads/reset", {
      method: "POST",
      body: { key },
    });
    state.history = [];
    state.status = "idle";
    state.statusText = "";
    state.error = "";
    state.streamText = "";
    setHistoryNavIndex(-1);
    resetChatEvents(state);
    chatUI.input.value = "";
    renderChat();
    flash("Started a new doc chat thread");
  } catch (err) {
    flash(err.message || "Failed to start a new doc chat thread", "error");
  }
}

export async function sendDocChat() {
  const message = (chatUI.input.value || "").trim();
  const state = getChatState();
  if (!message) {
    state.error = "Enter a message to send.";
    renderChat();
    return;
  }
  if (state.status === "running") {
    state.error = "Doc chat already running.";
    renderChat();
    flash("Doc chat already running", "error");
    return;
  }

  resetChatEvents(state);
  const viewing = getDocChatViewing();
  const entry = {
    id: `${Date.now()}`,
    prompt: message,
    viewing,
    agent: getSelectedAgent(),
    model: getSelectedModel(),
    reasoning: getSelectedReasoning(),
    response: "",
    status: "running",
    time: Date.now(),
    drafts: {},
    updated: [],
  };
  state.history.unshift(entry);
  if (state.history.length > CHAT_HISTORY_LIMIT * 2) {
    state.history.length = CHAT_HISTORY_LIMIT * 2;
  }
  state.status = "running";
  state.error = "";
  state.streamText = "";
  state.statusText = "queued";
  state.controller = new AbortController();

  renderChat();
  chatUI.input.value = "";
  chatUI.input.style.height = "auto";
  chatUI.input.focus();

  try {
    await performDocChatRequest(entry, state);
    refreshAllDrafts().catch(() => {});
    if (entry.status === "interrupted") {
      state.status = "interrupted";
      state.error = "";
    } else if (entry.status !== "error") {
      state.status = "idle";
      state.error = "";
    }
  } catch (err) {
    if (err.name === "AbortError") {
      entry.status = "interrupted";
      entry.error = "Interrupted";
      state.error = "";
      state.status = "interrupted";
      resetChatEvents(state, { preserve: true });
    } else {
      restoreChatInput(entry);
      markChatError(state, entry, err.message || "Doc chat failed");
      resetChatEvents(state, { preserve: true });
    }
  } finally {
    state.controller = null;
    if (state.status !== "running") {
      renderChat();
    }
  }
}

export async function toggleDraftPreview(kind = getActiveDoc()) {
  const draft = getDraft(kind);
  if (!draft) return;
  const nextValue = !isDraftPreview(kind);
  if (nextValue) {
    const textarea = getDocTextarea();
    if (textarea) {
      const cached = docsState.docsCache[kind] || "";
      if (textarea.value !== cached) {
        const ok = await confirmModal(
          `You have unsaved ${kind.toUpperCase()} edits. Overwrite with draft preview?`
        );
        if (!ok) return;
      }
    }
  }
  setDraftPreview(kind, nextValue);
  syncDocEditor(kind, { force: true });
  renderChat();
}

export async function applyPatch(kind = getActiveDoc()) {
  const state = getChatState();
  const draft = getDraft(kind);
  if (!draft) {
    flash("No draft to apply", "error");
    return;
  }
  try {
    const res = await api(`/api/docs/${kind}/chat/apply`, { method: "POST" });
    const applied = parseChatPayload(res);
    if (applied.error) throw new Error(applied.error);
    setDraftPreview(kind, false);
    setDraft(kind, null);
    if (applied.content) {
      await applyDocUpdateFromChat(kind, applied.content, { force: true });
    }
    const latest = state.history[0];
    if (latest) latest.status = "done";
    flash("Draft applied");
  } catch (err) {
    flash(err.message || "Failed to apply draft", "error");
  } finally {
    renderChat();
    syncDocEditor(kind, { force: true });
  }
}

export async function discardPatch(kind = getActiveDoc()) {
  const state = getChatState();
  const draft = getDraft(kind);
  if (!draft) return;
  try {
    const res = await api(`/api/docs/${kind}/chat/discard`, { method: "POST" });
    const parsed = parseChatPayload(res);
    setDraftPreview(kind, false);
    setDraft(kind, null);
    if (parsed.content) {
      await applyDocUpdateFromChat(kind, parsed.content, { force: true });
    }
    const latest = state.history[0];
    if (latest) {
      latest.status = latest.status === "running" ? "done" : latest.status;
    }
    flash("Draft discarded");
  } catch (err) {
    flash(err.message || "Failed to discard draft", "error");
  } finally {
    renderChat();
    syncDocEditor(kind, { force: true });
  }
}

export async function reloadPatch(kind = getActiveDoc(), silent = false) {
  try {
    const res = await api(`/api/docs/${kind}/chat/pending`, { method: "GET" });
    const parsed = parseChatPayload(res);
    if (parsed.error) throw new Error(parsed.error);
    const normalized = normalizeDraftPayload({
      content: parsed.content,
      patch: parsed.patch,
      agent_message: parsed.agentMessage || parsed.response || "",
      created_at: parsed.createdAt || "",
      base_hash: parsed.baseHash || "",
    });
    if (normalized) {
      setDraft(kind, normalized);
      if (isDraftPreview(kind)) {
        syncDocEditor(kind, { force: true });
      }
      renderChat();
      if (!silent) flash("Loaded pending draft");
      return;
    }
  } catch (err) {
    const message = err?.message || "";
    if (message.includes("No pending")) {
      setDraft(kind, null);
      if (isDraftPreview(kind)) {
        setDraftPreview(kind, false);
        syncDocEditor(kind, { force: true });
      }
      if (!silent) flash("No pending draft");
    } else if (!silent) {
      flash(message || "Failed to load pending draft", "error");
    }
    renderChat();
  }
}

export async function refreshAllDrafts() {
  await Promise.all(DOC_TYPES.map((kind) => reloadPatch(kind, true)));
}
