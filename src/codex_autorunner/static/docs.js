import {
  api,
  flash,
  statusPill,
  confirmModal,
  resolvePath,
  getAuthToken,
  isMobileViewport,
  getUrlParams,
  updateUrlParams,
} from "./utils.js";
import { loadState } from "./state.js";
import { publish } from "./bus.js";
import { registerAutoRefresh } from "./autoRefresh.js";
import { CONSTANTS } from "./constants.js";
import { initVoiceInput } from "./voice.js";
import { renderTodoPreview } from "./todoPreview.js";

// ─────────────────────────────────────────────────────────────────────────────
// Constants & State
// ─────────────────────────────────────────────────────────────────────────────

const DOC_TYPES = ["todo", "progress", "opinions", "spec", "summary"];
const CLEARABLE_DOCS = ["todo", "progress", "opinions"];
const COPYABLE_DOCS = ["spec", "summary"];
const PASTEABLE_DOCS = ["spec"];
const CHAT_HISTORY_LIMIT = 8;
const CHAT_EVENT_LIMIT = CONSTANTS.UI?.DOC_CHAT_EVENT_LIMIT || 12;
const CHAT_EVENT_MAX = Math.max(60, CHAT_EVENT_LIMIT * 8);

const docButtons = document.querySelectorAll(".chip[data-doc]");
let docsCache = { todo: "", progress: "", opinions: "", spec: "", summary: "" };
let snapshotCache = { exists: false, content: "", state: {} };
let snapshotBusy = false;
let activeDoc = "todo";

const chatDecoder = new TextDecoder();
const chatState = createChatState();
const draftState = {
  data: {},
  preview: {},
};
const specIngestState = {
  status: "idle",
  patch: "",
  agentMessage: "",
  error: "",
  busy: false,
  controller: null,
};
const VOICE_TRANSCRIPT_DISCLAIMER_TEXT =
  CONSTANTS.PROMPTS?.VOICE_TRANSCRIPT_DISCLAIMER ||
  "Note: transcribed from user voice. If confusing or possibly inaccurate and you cannot infer the intention please clarify before proceeding.";

// Track history navigation position for up/down arrow prompt recall
let historyNavIndex = -1;

// ─────────────────────────────────────────────────────────────────────────────
// UI Element References
// ─────────────────────────────────────────────────────────────────────────────

const chatUI = {
  status: document.getElementById("doc-chat-status"),
  stream: document.getElementById("doc-chat-stream"),
  patchMain: document.getElementById("doc-patch-main"),
  patchSummary: document.getElementById("doc-patch-summary"),
  patchMeta: document.getElementById("doc-patch-meta"),
  patchBody: document.getElementById("doc-patch-body"),
  patchApply: document.getElementById("doc-patch-apply"),
  patchPreview: document.getElementById("doc-patch-preview"),
  patchDiscard: document.getElementById("doc-patch-discard"),
  patchReload: document.getElementById("doc-patch-reload"),
  error: document.getElementById("doc-chat-error"),
  input: document.getElementById("doc-chat-input"),
  send: document.getElementById("doc-chat-send"),
  cancel: document.getElementById("doc-chat-cancel"),
  newThread: document.getElementById("doc-chat-new-thread"),
  voiceBtn: document.getElementById("doc-chat-voice"),
  voiceStatus: document.getElementById("doc-chat-voice-status"),
  hint: document.getElementById("doc-chat-hint"),
};

const specIssueUI = {
  row: document.getElementById("spec-issue-import"),
  toggle: document.getElementById("spec-issue-import-toggle"),
  inputRow: document.getElementById("spec-issue-input-row"),
  input: document.getElementById("spec-issue-input"),
  button: document.getElementById("spec-issue-import-btn"),
};

const snapshotUI = {
  generate: document.getElementById("snapshot-generate"),
  update: document.getElementById("snapshot-update"),
  regenerate: document.getElementById("snapshot-regenerate"),
  copy: document.getElementById("snapshot-copy"),
  refresh: document.getElementById("snapshot-refresh"),
};

const docActionsUI = {
  standard: document.getElementById("doc-actions-standard"),
  snapshot: document.getElementById("doc-actions-snapshot"),
  ingest: document.getElementById("ingest-spec"),
  clear: document.getElementById("clear-docs"),
  copy: document.getElementById("doc-copy"),
  paste: document.getElementById("spec-paste"),
};

const specIngestUI = {
  panel: document.getElementById("spec-ingest-followup"),
  input: document.getElementById("spec-ingest-input"),
  continueBtn: document.getElementById("spec-ingest-continue"),
  cancelBtn: document.getElementById("spec-ingest-cancel"),
  patchMain: document.getElementById("spec-ingest-patch-main"),
  patchSummary: document.getElementById("spec-ingest-patch-summary"),
  patchBody: document.getElementById("spec-ingest-patch-body"),
  patchApply: document.getElementById("spec-ingest-patch-apply"),
  patchDiscard: document.getElementById("spec-ingest-patch-discard"),
  patchReload: document.getElementById("spec-ingest-patch-reload"),
};

const threadRegistryUI = {
  banner: document.getElementById("doc-thread-registry-banner"),
  detail: document.getElementById("doc-thread-registry-detail"),
  reset: document.getElementById("doc-thread-registry-reset"),
  download: document.getElementById("doc-thread-registry-download"),
};

// ─────────────────────────────────────────────────────────────────────────────
// Chat State Management
// ─────────────────────────────────────────────────────────────────────────────

function createChatState() {
  return {
    history: [],
    status: "idle",
    statusText: "",
    error: "",
    streamText: "",
    controller: null,
    events: [],
    eventsExpanded: false,
    eventController: null,
    eventTurnId: null,
    eventThreadId: null,
    eventItemIndex: {},
    eventError: "",
  };
}

function getChatState() {
  return chatState;
}

// ─────────────────────────────────────────────────────────────────────────────
// Utilities
// ─────────────────────────────────────────────────────────────────────────────

function parseChatPayload(payload) {
  if (!payload) return { response: "" };
  if (typeof payload === "string") return { response: payload };
  if (payload.status && payload.status !== "ok") {
    if (payload.status === "interrupted") {
      return {
        interrupted: true,
        detail: payload.detail || "Doc chat interrupted",
      };
    }
    return { error: payload.detail || "Doc chat failed" };
  }
  return {
    response:
      payload.response ||
      payload.message ||
      payload.agent_message ||
      payload.agentMessage ||
      payload.content ||
      "",
    content: payload.content || "",
    patch: payload.patch || "",
    drafts: normalizeDraftMap(payload.drafts || payload.draft),
    updated: Array.isArray(payload.updated)
      ? payload.updated.filter((entry) => typeof entry === "string")
      : [],
    createdAt: payload.created_at || payload.createdAt || "",
    baseHash: payload.base_hash || payload.baseHash || "",
    agentMessage: payload.agent_message || payload.agentMessage || "",
  };
}

function parseSpecIngestPayload(payload) {
  if (!payload || typeof payload !== "object") {
    return { error: "Spec ingest failed" };
  }
  if (payload.status && payload.status !== "ok") {
    if (payload.status === "interrupted") {
      return {
        interrupted: true,
        todo: payload.todo || "",
        progress: payload.progress || "",
        opinions: payload.opinions || "",
        spec: payload.spec || "",
        summary: payload.summary || "",
        patch: payload.patch || "",
        agentMessage: payload.agent_message || payload.agentMessage || "",
      };
    }
    return { error: payload.detail || "Spec ingest failed" };
  }
  return {
    todo: payload.todo || "",
    progress: payload.progress || "",
    opinions: payload.opinions || "",
    spec: payload.spec || "",
    summary: payload.summary || "",
    patch: payload.patch || "",
    agentMessage: payload.agent_message || payload.agentMessage || "",
  };
}

function parseMaybeJson(raw) {
  try {
    return JSON.parse(raw);
  } catch (err) {
    return raw;
  }
}

function normalizeDraftPayload(payload) {
  if (!payload || typeof payload !== "object") return null;
  const content = typeof payload.content === "string" ? payload.content : "";
  const patch = typeof payload.patch === "string" ? payload.patch : "";
  if (!content && !patch) return null;
  return {
    content,
    patch,
    agentMessage:
      typeof payload.agent_message === "string"
        ? payload.agent_message
        : typeof payload.agentMessage === "string"
        ? payload.agentMessage
        : "",
    createdAt:
      typeof payload.created_at === "string"
        ? payload.created_at
        : typeof payload.createdAt === "string"
        ? payload.createdAt
        : "",
    baseHash:
      typeof payload.base_hash === "string"
        ? payload.base_hash
        : typeof payload.baseHash === "string"
        ? payload.baseHash
        : "",
  };
}

function normalizeDraftMap(raw) {
  if (!raw || typeof raw !== "object") return {};
  const drafts = {};
  Object.entries(raw).forEach(([kind, entry]) => {
    const normalized = normalizeDraftPayload(entry);
    if (normalized) drafts[kind] = normalized;
  });
  return drafts;
}

function setDraft(kind, draft) {
  if (!DOC_TYPES.includes(kind)) return;
  if (!draft) {
    delete draftState.data[kind];
    delete draftState.preview[kind];
  } else {
    draftState.data[kind] = draft;
  }
  updateDocDraftIndicators();
}

function getDraft(kind) {
  return draftState.data[kind] || null;
}

function hasDraft(kind) {
  return !!getDraft(kind);
}

function isDraftPreview(kind) {
  return !!draftState.preview[kind];
}

function setDraftPreview(kind, value) {
  if (!DOC_TYPES.includes(kind)) return;
  if (value) {
    draftState.preview[kind] = true;
  } else {
    delete draftState.preview[kind];
  }
  updateDocDraftIndicators();
}

function updateDocDraftIndicators() {
  docButtons.forEach((btn) => {
    const kind = btn.dataset.doc;
    if (!DOC_TYPES.includes(kind)) return;
    btn.classList.toggle("has-draft", hasDraft(kind));
    btn.classList.toggle(
      "previewing",
      hasDraft(kind) && isDraftPreview(kind)
    );
  });
}

function formatDraftTimestamp(value) {
  if (!value) return "";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function applyDraftUpdates(drafts) {
  if (!drafts || typeof drafts !== "object") return;
  Object.entries(drafts).forEach(([kind, entry]) => {
    const normalized = normalizeDraftPayload(entry);
    if (normalized) setDraft(kind, normalized);
  });
  if (hasDraft(activeDoc) && isDraftPreview(activeDoc)) {
    syncDocEditor(activeDoc, { force: true });
  }
}

function resetChatEvents(state, { preserve = false } = {}) {
  if (state.eventController) {
    state.eventController.abort();
  }
  state.eventController = null;
  state.eventTurnId = null;
  state.eventThreadId = null;
  state.eventItemIndex = {};
  state.eventError = "";
  if (!preserve) {
    state.events = [];
    state.eventsExpanded = false;
  }
}

function getDocFromUrl() {
  const params = getUrlParams();
  const kind = params.get("doc");
  if (!kind) return null;
  if (kind === "snapshot") return kind;
  return DOC_TYPES.includes(kind) ? kind : null;
}

function getDocChatTargets() {
  if (!DOC_TYPES.includes(activeDoc)) return [];
  return [activeDoc];
}

/**
 * Render a unified diff with syntax highlighting and line numbers.
 * Returns HTML with colored lines for additions (+), deletions (-),
 * headers (@@), and file paths (--- / +++).
 */
function renderDiffHtml(diffText) {
  if (!diffText) return "";
  const lines = diffText.split("\n");
  let oldLineNum = 0;
  let newLineNum = 0;

  const htmlLines = lines.map((line) => {
    // Escape HTML entities
    const escaped = line
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");

    // Parse hunk header to get line numbers
    if (line.startsWith("@@") && line.includes("@@")) {
      const match = line.match(/@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
      if (match) {
        oldLineNum = parseInt(match[1], 10);
        newLineNum = parseInt(match[2], 10);
      }
      return `<div class="diff-line diff-hunk"><span class="diff-gutter diff-gutter-hunk">···</span><span class="diff-content">${escaped}</span></div>`;
    }

    // File headers (no line numbers)
    if (line.startsWith("+++") || line.startsWith("---")) {
      return `<div class="diff-line diff-file"><span class="diff-gutter"></span><span class="diff-content">${escaped}</span></div>`;
    }

    // Addition line
    if (line.startsWith("+")) {
      const lineNum = newLineNum++;
      const content = escaped.substring(1); // Remove the + prefix
      const isEmpty = content.trim() === "";
      const displayContent = isEmpty
        ? `<span class="diff-empty-marker">↵</span>`
        : content;
      return `<div class="diff-line diff-add"><span class="diff-gutter diff-gutter-add">${lineNum}</span><span class="diff-sign">+</span><span class="diff-content">${displayContent}</span></div>`;
    }

    // Deletion line
    if (line.startsWith("-")) {
      const lineNum = oldLineNum++;
      const content = escaped.substring(1); // Remove the - prefix
      const isEmpty = content.trim() === "";
      const displayContent = isEmpty
        ? `<span class="diff-empty-marker">↵</span>`
        : content;
      return `<div class="diff-line diff-del"><span class="diff-gutter diff-gutter-del">${lineNum}</span><span class="diff-sign">−</span><span class="diff-content">${displayContent}</span></div>`;
    }

    // Context line (unchanged)
    if (
      line.startsWith(" ") ||
      (line.length > 0 && !line.startsWith("\\") && oldLineNum > 0)
    ) {
      const oLine = oldLineNum++;
      newLineNum += 1;
      const content = escaped.startsWith(" ") ? escaped.substring(1) : escaped;
      return `<div class="diff-line diff-ctx"><span class="diff-gutter diff-gutter-ctx">${oLine}</span><span class="diff-sign"> </span><span class="diff-content">${content}</span></div>`;
    }

    // Other lines (like "\ No newline at end of file")
    return `<div class="diff-line diff-meta"><span class="diff-gutter"></span><span class="diff-content diff-note">${escaped}</span></div>`;
  });

  return `<div class="diff-view">${htmlLines.join("")}</div>`;
}

function autoResizeTextarea(textarea) {
  textarea.style.height = "auto";
  textarea.style.height = textarea.scrollHeight + "px";
}

function getDocTextarea() {
  return document.getElementById("doc-content");
}

function updateCopyButton(button, text, disabled = false) {
  if (!button) return;
  const hasText = Boolean((text || "").trim());
  button.disabled = disabled || !hasText;
}

function getDocCopyText(kind = activeDoc) {
  const textarea = getDocTextarea();
  if (textarea && activeDoc === kind) {
    return textarea.value || "";
  }
  if (kind === "snapshot") {
    return snapshotCache.content || "";
  }
  return docsCache[kind] || "";
}

function updateStandardActionButtons(kind = activeDoc) {
  if (docActionsUI.copy) {
    const canCopy = COPYABLE_DOCS.includes(kind);
    docActionsUI.copy.classList.toggle("hidden", !canCopy);
    updateCopyButton(docActionsUI.copy, canCopy ? getDocCopyText(kind) : "");
  }
  if (docActionsUI.paste) {
    const canPaste = PASTEABLE_DOCS.includes(kind);
    docActionsUI.paste.classList.toggle("hidden", !canPaste);
  }
}

async function copyDocToClipboard(kind = activeDoc) {
  const text = getDocCopyText(kind);
  if (!text.trim()) return;
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      flash("Copied to clipboard");
      return;
    }
  } catch {
    // fall through
  }

  let temp = null;
  try {
    temp = document.createElement("textarea");
    temp.value = text;
    temp.setAttribute("readonly", "");
    temp.style.position = "fixed";
    temp.style.top = "-9999px";
    temp.style.opacity = "0";
    document.body.appendChild(temp);
    temp.select();
    const ok = document.execCommand("copy");
    flash(ok ? "Copied to clipboard" : "Copy failed");
  } catch {
    flash("Copy failed");
  } finally {
    if (temp && temp.parentNode) {
      temp.parentNode.removeChild(temp);
    }
  }
}

async function pasteSpecFromClipboard() {
  if (!PASTEABLE_DOCS.includes(activeDoc)) return;
  if (hasDraft(activeDoc) && isDraftPreview(activeDoc)) {
    flash("Exit draft preview before pasting.", "error");
    return;
  }
  const textarea = getDocTextarea();
  if (!textarea) return;
  try {
    if (!navigator.clipboard?.readText) {
      flash("Paste not supported in this browser", "error");
      return;
    }
    const text = await navigator.clipboard.readText();
    if (!text) {
      flash("Clipboard is empty", "error");
      return;
    }
    textarea.value = text;
    textarea.focus();
    updateDocControls("spec");
    flash("SPEC replaced from clipboard");
  } catch {
    flash("Paste failed", "error");
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Chat UI Rendering
// ─────────────────────────────────────────────────────────────────────────────

async function applyDocUpdateFromChat(kind, content, { force = false } = {}) {
  if (!content) return false;
  const textarea = getDocTextarea();
  const viewingSameDoc = activeDoc === kind;
  const previewing = hasDraft(kind) && isDraftPreview(kind);
  if (viewingSameDoc && textarea) {
    const cached = docsCache[kind] || "";
    if (!force && !previewing && textarea.value !== cached) {
      const ok = await confirmModal(
        `You have unsaved ${kind.toUpperCase()} edits. Overwrite with chat result?`
      );
      if (!ok) {
        flash(
          `Kept your unsaved ${kind.toUpperCase()} edits; chat result not applied.`
        );
        return false;
      }
    }
  }

  docsCache[kind] = content;
  if (viewingSameDoc && textarea && !previewing) {
    textarea.value = content;
    updateDocStatus(kind);
  }
  if (viewingSameDoc) {
    updateDocControls(kind);
  }
  publish("docs:updated", { kind, content });
  if (kind === "todo") {
    renderTodoPreview(content);
    loadState({ notify: false }).catch(() => {});
  }
  return true;
}

function applySpecIngestDocs(payload) {
  if (!payload) return;
  docsCache = {
    ...docsCache,
    todo: payload.todo ?? docsCache.todo,
    progress: payload.progress ?? docsCache.progress,
    opinions: payload.opinions ?? docsCache.opinions,
    spec: payload.spec ?? docsCache.spec,
    summary: payload.summary ?? docsCache.summary,
  };
  if (activeDoc !== "snapshot") {
    syncDocEditor(activeDoc, { force: true });
  }
  updateDocControls(activeDoc);
  renderTodoPreview(docsCache.todo);
  publish("docs:updated", { kind: "todo", content: docsCache.todo });
  publish("docs:updated", { kind: "progress", content: docsCache.progress });
  publish("docs:updated", { kind: "opinions", content: docsCache.opinions });
  publish("docs:updated", { kind: "spec", content: docsCache.spec });
  publish("docs:updated", { kind: "summary", content: docsCache.summary });
  loadState({ notify: false }).catch(() => {});
}

function updateDocVisibility() {
  const docContent = getDocTextarea();
  if (!docContent) return;
  const specHasPatch =
    activeDoc === "spec" && !!(specIngestState.patch || "").trim();
  docContent.classList.toggle("hidden", specHasPatch);
}

function renderSpecIngestPatch() {
  if (!specIngestUI.patchMain) return;
  const isSpec = activeDoc === "spec";
  const hasPatch = !!(specIngestState.patch || "").trim();
  if (specIngestUI.continueBtn)
    specIngestUI.continueBtn.disabled = specIngestState.busy;
  if (specIngestUI.cancelBtn) {
    specIngestUI.cancelBtn.disabled = !specIngestState.busy;
    specIngestUI.cancelBtn.classList.toggle("hidden", !specIngestState.busy);
  }
  specIngestUI.patchMain.classList.toggle("hidden", !isSpec || !hasPatch);
  if (!isSpec || !hasPatch) {
    updateDocVisibility();
    return;
  }
  specIngestUI.patchBody.innerHTML = renderDiffHtml(specIngestState.patch);
  specIngestUI.patchSummary.textContent =
    specIngestState.agentMessage || "Spec ingest patch ready";
  if (specIngestUI.patchApply)
    specIngestUI.patchApply.disabled = specIngestState.busy || !hasPatch;
  if (specIngestUI.patchDiscard)
    specIngestUI.patchDiscard.disabled = specIngestState.busy || !hasPatch;
  if (specIngestUI.patchReload)
    specIngestUI.patchReload.disabled = specIngestState.busy;
  updateDocVisibility();
}

function updateDocStatus(kind) {
  const status = document.getElementById("doc-status");
  if (!status) return;
  if (kind === "snapshot") {
    status.textContent = snapshotBusy ? "Working…" : "Viewing SNAPSHOT";
    return;
  }
  const draft = getDraft(kind);
  if (draft && isDraftPreview(kind)) {
    status.textContent = `Previewing ${kind.toUpperCase()} draft`;
    return;
  }
  status.textContent = `Editing ${kind.toUpperCase()}`;
}

function syncDocEditor(kind, { force = false } = {}) {
  const textarea = getDocTextarea();
  if (!textarea) return;
  if (kind === "snapshot") {
    textarea.readOnly = true;
    textarea.classList.remove("doc-preview");
    textarea.value = snapshotCache.content || "";
    textarea.placeholder = "(snapshot will appear here)";
    updateDocStatus(kind);
    return;
  }
  const draft = getDraft(kind);
  const previewing = !!draft && isDraftPreview(kind);
  const nextValue = previewing ? draft.content : docsCache[kind] || "";
  if (force || textarea.value !== nextValue) {
    textarea.value = nextValue;
  }
  textarea.readOnly = previewing;
  textarea.classList.toggle("doc-preview", previewing);
  textarea.placeholder = previewing ? "(draft preview)" : "";
  updateDocStatus(kind);
}

function updateDocControls(kind) {
  const saveBtn = document.getElementById("save-doc");
  if (saveBtn) {
    const previewing = hasDraft(kind) && isDraftPreview(kind);
    saveBtn.disabled = kind === "snapshot" || previewing;
  }
  updateStandardActionButtons(kind);
}

function extractCommand(item, params) {
  const command = item?.command ?? params?.command;
  if (Array.isArray(command)) {
    return command.map((part) => String(part)).join(" ").trim();
  }
  if (typeof command === "string") return command.trim();
  return "";
}

function extractFiles(payload) {
  const files = [];
  const addEntry = (entry) => {
    if (typeof entry === "string" && entry.trim()) {
      files.push(entry.trim());
      return;
    }
    if (entry && typeof entry === "object") {
      const path = entry.path || entry.file || entry.name;
      if (typeof path === "string" && path.trim()) {
        files.push(path.trim());
      }
    }
  };
  if (!payload || typeof payload !== "object") return files;
  for (const key of ["files", "fileChanges", "paths"]) {
    const value = payload[key];
    if (Array.isArray(value)) {
      value.forEach(addEntry);
    }
  }
  for (const key of ["path", "file", "name"]) {
    addEntry(payload[key]);
  }
  return files;
}

function extractErrorMessage(params) {
  if (!params || typeof params !== "object") return "";
  const err = params.error;
  if (err && typeof err === "object") {
    const message = typeof err.message === "string" ? err.message : "";
    const details =
      typeof err.additionalDetails === "string"
        ? err.additionalDetails
        : typeof err.details === "string"
        ? err.details
        : "";
    if (message && details && message !== details) {
      return `${message} (${details})`;
    }
    return message || details;
  }
  if (typeof err === "string") return err;
  if (typeof params.message === "string") return params.message;
  return "";
}

function extractOutputDelta(payload) {
  const message =
    payload && typeof payload === "object" ? payload.message || payload : payload;
  if (!message || typeof message !== "object") return "";
  const method = String(message.method || "").toLowerCase();
  if (!method.includes("outputdelta")) return "";
  const params = message.params || {};
  if (typeof params.delta === "string") return params.delta;
  if (typeof params.text === "string") return params.text;
  if (typeof params.output === "string") return params.output;
  return "";
}

function addChatEvent(state, entry) {
  state.events.push(entry);
  if (state.events.length > CHAT_EVENT_MAX) {
    state.events = state.events.slice(-CHAT_EVENT_MAX);
    state.eventItemIndex = {};
    state.events.forEach((evt, idx) => {
      if (evt.itemId) state.eventItemIndex[evt.itemId] = idx;
    });
  }
}

function applyAppServerEvent(state, payload) {
  const message =
    payload && typeof payload === "object" ? payload.message || payload : payload;
  if (!message || typeof message !== "object") return;
  const method = message.method || "app-server";
  const params = message.params || {};
  const item = params.item || {};
  const itemId = params.itemId || item.id || item.itemId || null;
  const receivedAt =
    payload && typeof payload === "object"
      ? payload.received_at || payload.receivedAt || Date.now()
      : Date.now();

  if (method === "item/reasoning/summaryTextDelta") {
    const delta = params.delta || "";
    if (!delta) return;
    const existingIndex =
      itemId && state.eventItemIndex[itemId] !== undefined
        ? state.eventItemIndex[itemId]
        : null;
    if (existingIndex !== null) {
      const existing = state.events[existingIndex];
      existing.summary = `${existing.summary || ""}${delta}`;
      existing.time = receivedAt;
      return;
    }
    const entry = {
      id: payload?.id || `${Date.now()}`,
      title: "Thinking",
      summary: delta,
      detail: "",
      kind: "thinking",
      time: receivedAt,
      itemId,
      method,
    };
    addChatEvent(state, entry);
    if (itemId) state.eventItemIndex[itemId] = state.events.length - 1;
    return;
  }

  if (method === "item/reasoning/summaryPartAdded") {
    const existingIndex =
      itemId && state.eventItemIndex[itemId] !== undefined
        ? state.eventItemIndex[itemId]
        : null;
    if (existingIndex !== null) {
      const existing = state.events[existingIndex];
      existing.summary = `${existing.summary || ""}\n\n`;
      existing.time = receivedAt;
    }
    return;
  }

  let title = method;
  let summary = "";
  let detail = "";
  let kind = "event";

  if (method === "item/completed") {
    const itemType = item.type;
    if (itemType === "commandExecution") {
      title = "Command";
      summary = extractCommand(item, params);
      kind = "command";
      if (item.exitCode !== undefined && item.exitCode !== null) {
        detail = `exit ${item.exitCode}`;
      }
    } else if (itemType === "fileChange") {
      title = "File change";
      const files = extractFiles(item);
      summary = files.join(", ") || "Updated files";
      kind = "file";
    } else if (itemType === "tool") {
      title = "Tool";
      summary = item.name || item.tool || item.id || "Tool call";
      kind = "command";
    } else if (itemType === "agentMessage") {
      title = "Agent";
      summary = item.text || "Agent message";
    } else {
      title = itemType ? `Item ${itemType}` : "Item completed";
      summary = item.text || item.message || "";
    }
  } else if (method === "item/commandExecution/requestApproval") {
    title = "Command approval";
    summary = extractCommand(item, params) || "Approval requested";
    kind = "command";
  } else if (method === "item/fileChange/requestApproval") {
    title = "File approval";
    const files = extractFiles(params);
    summary = files.join(", ") || "Approval requested";
    kind = "file";
  } else if (method === "turn/completed") {
    title = "Turn completed";
    summary = params.status || "completed";
    kind = "status";
  } else if (method === "error") {
    title = "Error";
    summary = extractErrorMessage(params) || "App-server error";
    kind = "error";
  } else if (method.includes("outputDelta")) {
    title = "Output";
    summary = params.delta || params.text || "";
  } else if (params.delta) {
    title = "Delta";
    summary = params.delta;
  }

  const entry = {
    id: payload?.id || `${Date.now()}`,
    title,
    summary: summary || "(no details)",
    detail,
    kind,
    time: receivedAt,
    itemId,
    method,
  };
  addChatEvent(state, entry);
  if (itemId) state.eventItemIndex[itemId] = state.events.length - 1;
}

// Constants for unified chat stream
const THINKING_PREVIEW_MAX_LEN = 120;

// Extract a compact thinking preview from events (like Telegram)
function getThinkingPreview(events) {
  // Find the latest thinking event
  for (let i = events.length - 1; i >= 0; i--) {
    const evt = events[i];
    if (evt.kind === "thinking" && evt.summary) {
      // Normalize and truncate
      let text = evt.summary.replace(/\s+/g, " ").trim();
      if (text.length > THINKING_PREVIEW_MAX_LEN) {
        text = text.slice(0, THINKING_PREVIEW_MAX_LEN) + "…";
      }
      return text;
    }
  }
  return null;
}

// Get notable tool calls from events (commands, file changes)
function getToolCalls(events) {
  const tools = [];
  for (const evt of events) {
    if (evt.kind === "command" || evt.kind === "file") {
      tools.push({
        type: evt.kind,
        name: evt.summary || evt.title,
        detail: evt.detail,
      });
    }
  }
  return tools;
}

// Render tool call pills for the current turn
function renderToolCallPills(tools) {
  if (!tools.length) return null;
  const container = document.createElement("div");
  container.className = "chat-tool-calls";
  // Show up to 5 tool calls
  const visibleTools = tools.slice(-5);
  for (const tool of visibleTools) {
    const pill = document.createElement("span");
    pill.className = `chat-tool-pill ${tool.type}`;
    pill.textContent = tool.type === "command" ? `$ ${tool.name}` : `📄 ${tool.name}`;
    if (tool.detail) pill.title = tool.detail;
    container.appendChild(pill);
  }
  if (tools.length > 5) {
    const more = document.createElement("span");
    more.className = "chat-tool-pill muted";
    more.textContent = `+${tools.length - 5} more`;
    container.appendChild(more);
  }
  return container;
}

// Unified chat stream renderer
function renderChatStream(state) {
  if (!chatUI.stream) return;
  if (activeDoc === "snapshot") return;

  chatUI.stream.innerHTML = "";
  const count = state.history.length;

  if (count === 0) {
    const empty = document.createElement("div");
    empty.className = "chat-empty";
    empty.textContent = "No messages yet. Start a conversation about the work docs.";
    chatUI.stream.appendChild(empty);
    return;
  }

  // Render messages - newest at top (already in reverse chronological order)
  state.history.slice(0, CHAT_HISTORY_LIMIT).forEach((entry, idx) => {
    const isLatest = idx === 0;
    const isRunning = entry.status === "running";
    const msg = document.createElement("div");
    msg.className = `chat-message ${entry.status}`;

    // User prompt bubble
    const userBubble = document.createElement("div");
    userBubble.className = "chat-bubble user";
    
    const promptText = document.createElement("div");
    promptText.className = "chat-prompt";
    promptText.textContent = entry.prompt || "(no prompt)";
    userBubble.appendChild(promptText);

    // Timestamp and copy button row
    const userMeta = document.createElement("div");
    userMeta.className = "chat-meta";
    
    const stamp = document.createElement("span");
    stamp.className = "chat-time";
    stamp.textContent = entry.time
      ? new Date(entry.time).toLocaleTimeString([], {
          hour: "2-digit",
          minute: "2-digit",
        })
      : "";
    userMeta.appendChild(stamp);

    const copyBtn = document.createElement("button");
    copyBtn.className = "chat-copy-btn";
    copyBtn.title = "Edit this prompt";
    copyBtn.innerHTML = "✎";
    copyBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      chatUI.input.value = entry.prompt;
      autoResizeTextarea(chatUI.input);
      chatUI.input.focus();
      historyNavIndex = -1;
      flash("Prompt restored to input");
    });
    userMeta.appendChild(copyBtn);
    
    userBubble.appendChild(userMeta);
    msg.appendChild(userBubble);

    // Assistant response bubble
    const assistantBubble = document.createElement("div");
    assistantBubble.className = "chat-bubble assistant";

    // For running entries, show thinking preview and tool calls
    if (isRunning && isLatest) {
      // Show thinking indicator with preview
      const thinkingPreview = getThinkingPreview(state.events);
      const toolCalls = getToolCalls(state.events);

      if (thinkingPreview || toolCalls.length > 0 || state.statusText) {
        // Thinking preview
        if (thinkingPreview) {
          const thinking = document.createElement("div");
          thinking.className = "chat-thinking";
          thinking.innerHTML = `<span class="thinking-indicator">⚡</span> ${escapeHtml(thinkingPreview)}`;
          assistantBubble.appendChild(thinking);
        } else if (state.statusText && state.statusText !== "queued") {
          const status = document.createElement("div");
          status.className = "chat-status-text";
          status.innerHTML = `<span class="thinking-indicator">⚡</span> ${escapeHtml(state.statusText)}`;
          assistantBubble.appendChild(status);
        }

        // Tool calls
        if (toolCalls.length > 0) {
          const toolsEl = renderToolCallPills(toolCalls);
          if (toolsEl) assistantBubble.appendChild(toolsEl);
        }

        // Streaming response text if available
        if (state.streamText || entry.response) {
          const responseDiv = document.createElement("div");
          responseDiv.className = "chat-response streaming";
          responseDiv.textContent = state.streamText || entry.response;
          assistantBubble.appendChild(responseDiv);
        }
      } else {
        // Just show "Working..." if no preview available
        const waiting = document.createElement("div");
        waiting.className = "chat-waiting";
        waiting.innerHTML = '<span class="typing-dots"><span></span><span></span><span></span></span> Working...';
        assistantBubble.appendChild(waiting);
      }
    } else {
      // Completed entry - show response
      const responseDiv = document.createElement("div");
      responseDiv.className = "chat-response";
      
      if (entry.error) {
        responseDiv.className += " error";
        responseDiv.textContent = entry.error;
      } else if (entry.status === "interrupted") {
        responseDiv.className += " interrupted";
        responseDiv.textContent = entry.response || "Interrupted";
      } else {
        responseDiv.textContent = entry.response || "(no response)";
      }
      assistantBubble.appendChild(responseDiv);

      // Show updated docs tags
      if (entry.updated && entry.updated.length) {
        const tagsDiv = document.createElement("div");
        tagsDiv.className = "chat-tags";
        entry.updated.forEach((doc) => {
          const tag = document.createElement("span");
          tag.className = "chat-tag";
          tag.textContent = doc.toUpperCase();
          tagsDiv.appendChild(tag);
        });
        assistantBubble.appendChild(tagsDiv);
      }
    }

    msg.appendChild(assistantBubble);
    chatUI.stream.appendChild(msg);
  });
}

// Helper to escape HTML in thinking preview
function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

function renderChat() {
  const state = getChatState();
  const latest = state.history[0];
  const isRunning = state.status === "running";
  const hasError = !!state.error;

  // Update status pill
  const pillState = isRunning
    ? "running"
    : state.status === "error"
    ? "error"
    : state.status === "interrupted"
    ? "interrupted"
    : "idle";
  if (chatUI.status) {
    statusPill(chatUI.status, pillState);
  }

  // Update input state
  if (chatUI.send) {
    chatUI.send.disabled = isRunning;
  }
  if (chatUI.input) {
    chatUI.input.disabled = isRunning;
  }
  if (chatUI.cancel) {
    chatUI.cancel.classList.toggle("hidden", !isRunning);
  }
  if (chatUI.voiceBtn) {
    chatUI.voiceBtn.disabled =
      isRunning && !chatUI.voiceBtn.classList.contains("voice-retry");
    chatUI.voiceBtn.classList.toggle("disabled", chatUI.voiceBtn.disabled);
    if (typeof chatUI.voiceBtn.setAttribute === "function") {
      chatUI.voiceBtn.setAttribute(
        "aria-disabled",
        chatUI.voiceBtn.disabled ? "true" : "false"
      );
    }
  }
  if (chatUI.newThread) {
    chatUI.newThread.disabled = isRunning;
    chatUI.newThread.classList.toggle("disabled", isRunning);
  }

  // Update hint text - show status inline when running
  if (chatUI.hint) {
    if (isRunning) {
      const statusText = state.statusText || "processing";
      chatUI.hint.textContent = statusText;
      chatUI.hint.classList.add("loading");
    } else {
      const sendHint = isMobileViewport()
        ? "Tap Send to send · Enter for newline"
        : "Cmd+Enter / Ctrl+Enter to send · Enter for newline";
      chatUI.hint.textContent = sendHint;
      chatUI.hint.classList.remove("loading");
    }
  }

  // Handle error display
  if (hasError) {
    chatUI.error.textContent = state.error;
    chatUI.error.classList.remove("hidden");
  } else {
    chatUI.error.textContent = "";
    chatUI.error.classList.add("hidden");
  }

  const draft = getDraft(activeDoc);
  const hasPatch = !!(draft && (draft.patch || "").trim());
  const previewing = hasPatch && isDraftPreview(activeDoc);
  if (chatUI.patchMain) {
    chatUI.patchMain.classList.toggle("hidden", !hasPatch);
    chatUI.patchMain.classList.toggle("previewing", previewing);
    // Use syntax-highlighted diff rendering
    chatUI.patchBody.innerHTML = hasPatch
      ? renderDiffHtml(draft.patch)
      : "(no draft)";
    if (hasPatch) {
      chatUI.patchSummary.textContent =
        draft?.agentMessage ||
        latest?.response ||
        state.error ||
        "Draft ready";
    } else {
      chatUI.patchSummary.textContent = "";
    }
    if (chatUI.patchMeta) {
      const metaParts = [];
      if (hasPatch && draft?.createdAt) {
        metaParts.push(`drafted ${formatDraftTimestamp(draft.createdAt)}`);
      }
      if (hasPatch && draft?.baseHash) {
        metaParts.push(`base ${draft.baseHash.slice(0, 7)}`);
      }
      chatUI.patchMeta.textContent = metaParts.join(" · ");
    }
    if (chatUI.patchApply) chatUI.patchApply.disabled = isRunning || !hasPatch;
    if (chatUI.patchDiscard)
      chatUI.patchDiscard.disabled = isRunning || !hasPatch;
    if (chatUI.patchReload) chatUI.patchReload.disabled = isRunning;
    if (chatUI.patchPreview) {
      chatUI.patchPreview.disabled = isRunning || !hasPatch;
      chatUI.patchPreview.textContent = previewing
        ? "Hide preview"
        : "Preview draft";
      chatUI.patchPreview.classList.toggle("active", previewing);
      chatUI.patchPreview.setAttribute(
        "aria-pressed",
        previewing ? "true" : "false"
      );
    }
  }

  updateDocVisibility();
  updateDocControls(activeDoc);

  renderChatStream(state);
}


// ─────────────────────────────────────────────────────────────────────────────
// Chat Actions & Error Handling
// ─────────────────────────────────────────────────────────────────────────────

function markChatError(state, entry, message) {
  entry.status = "error";
  entry.error = message;
  state.error = message;
  state.status = "error";
  renderChat();
}

async function interruptDocChat() {
  try {
    await api("/api/docs/chat/interrupt", { method: "POST" });
  } catch (err) {
    flash(err.message || "Failed to interrupt doc chat", "error");
  }
}

function cancelDocChat() {
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

async function startNewDocChatThread() {
  const state = getChatState();
  if (state.status === "running") {
    cancelDocChat();
  }
  try {
    await api("/api/app-server/threads/reset", {
      method: "POST",
      body: { key: "doc_chat" },
    });
    state.history = [];
    state.status = "idle";
    state.statusText = "";
    state.error = "";
    state.streamText = "";
    historyNavIndex = -1;
    resetChatEvents(state);
    chatUI.input.value = "";
    renderChat();
    flash("Started a new doc chat thread");
  } catch (err) {
    flash(err.message || "Failed to start a new doc chat thread", "error");
  }
}

async function sendDocChat() {
  const message = (chatUI.input.value || "").trim();
  const state = getChatState();
  if (!message) {
    state.error = "Enter a message to send.";
    renderChat();
    return;
  }
  if (state.status === "running") return;

  resetChatEvents(state);
  const targets = getDocChatTargets();
  const entry = {
    id: `${Date.now()}`,
    prompt: message,
    targets,
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

  // Collapse history when starting new request for compact view
  renderChat();
  chatUI.input.value = "";
  chatUI.input.style.height = "auto"; // Reset textarea height
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

// ─────────────────────────────────────────────────────────────────────────────
// Chat Networking & Streaming
// ─────────────────────────────────────────────────────────────────────────────

async function performDocChatRequest(entry, state) {
  const endpoint = resolvePath("/api/docs/chat");
  const headers = { "Content-Type": "application/json" };
  const token = getAuthToken();
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  const payload = { message: entry.prompt, stream: true };
  if (entry.targets && entry.targets.length) {
    payload.targets = entry.targets;
  }
  const res = await fetch(endpoint, {
    method: "POST",
    headers,
    body: JSON.stringify(payload),
    signal: state.controller.signal,
  });

  if (!res.ok) {
    const text = await res.text();
    let detail = text;
    try {
      const parsed = JSON.parse(text);
      detail = parsed.detail || parsed.error || text;
    } catch (err) {
      // ignore parse errors
    }
    throw new Error(detail || `Request failed (${res.status})`);
  }

  const contentType = res.headers.get("content-type") || "";
  if (contentType.includes("text/event-stream")) {
    await readChatStream(res, state, entry);
    if (
      entry.status !== "error" &&
      entry.status !== "done" &&
      entry.status !== "interrupted"
    ) {
      entry.status = "done";
    }
  } else {
    const payload = contentType.includes("application/json")
      ? await res.json()
      : await res.text();
    applyChatResult(payload, state, entry);
  }
}

async function startDocChatEventStream(payload) {
  const threadId = payload?.thread_id || payload?.threadId;
  const turnId = payload?.turn_id || payload?.turnId;
  if (!threadId || !turnId) return;
  const state = getChatState();
  if (state.eventTurnId === turnId && state.eventThreadId === threadId) {
    return;
  }
  resetChatEvents(state);
  state.eventTurnId = turnId;
  state.eventThreadId = threadId;
  state.eventController = new AbortController();
  renderChatStream(state);

  const endpoint = resolvePath(
    `/api/app-server/turns/${encodeURIComponent(turnId)}/events`
  );
  const url = `${endpoint}?thread_id=${encodeURIComponent(threadId)}`;
  const headers = {};
  const token = getAuthToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  try {
    const res = await fetch(url, {
      method: "GET",
      headers,
      signal: state.eventController.signal,
    });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(text || `Event stream failed (${res.status})`);
    }
    const contentType = res.headers.get("content-type") || "";
    if (!contentType.includes("text/event-stream")) {
      throw new Error("Event stream unavailable");
    }
    await readAppServerEventStream(res, state);
  } catch (err) {
    if (err.name === "AbortError") return;
    state.eventError = err.message || "Failed to stream app-server events";
    renderChatStream(state);
  }
}

async function readAppServerEventStream(res, state) {
  if (!res.body) throw new Error("Streaming not supported in this browser");
  const reader = res.body.getReader();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += chatDecoder.decode(value, { stream: true });
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop();
    for (const chunk of chunks) {
      if (!chunk.trim()) continue;
      let event = "message";
      const dataLines = [];
      chunk.split("\n").forEach((line) => {
        if (line.startsWith("event:")) {
          event = line.slice(6).trim();
        } else if (line.startsWith("data:")) {
          dataLines.push(line.slice(5).trimStart());
        }
      });
      if (dataLines.length === 0) continue;
      const data = dataLines.join("\n");
      await handleAppServerStreamEvent(event || "message", data, state);
    }
  }
}

async function handleAppServerStreamEvent(_event, rawData, state) {
  if (!rawData) return;
  const parsed = parseMaybeJson(rawData);
  applyAppServerEvent(state, parsed);
  const delta = extractOutputDelta(parsed);
  if (delta) {
    const entry = state.history[0];
    if (entry && entry.status === "running") {
      entry.response = (entry.response || "") + delta;
      state.streamText = entry.response;
      renderChat();
    }
  }
  renderChatStream(state);
}

async function toggleDraftPreview(kind = activeDoc) {
  const draft = getDraft(kind);
  if (!draft) return;
  const nextValue = !isDraftPreview(kind);
  if (nextValue) {
    const textarea = getDocTextarea();
    if (textarea) {
      const cached = docsCache[kind] || "";
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

async function applyPatch(kind = activeDoc) {
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

async function discardPatch(kind = activeDoc) {
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

async function reloadPatch(kind = activeDoc, silent = false) {
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
      if (!silent) flash("No pending draft", "error");
    } else if (!silent) {
      flash(message || "Failed to load pending draft", "error");
    }
    renderChat();
  }
}

async function refreshAllDrafts() {
  await Promise.all(DOC_TYPES.map((kind) => reloadPatch(kind, true)));
}

async function readChatStream(res, state, entry) {
  if (!res.body) throw new Error("Streaming not supported in this browser");
  const reader = res.body.getReader();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += chatDecoder.decode(value, { stream: true });
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop();
    for (const chunk of chunks) {
      if (!chunk.trim()) continue;
      let event = "message";
      const dataLines = [];
      chunk.split("\n").forEach((line) => {
        if (line.startsWith("event:")) {
          event = line.slice(6).trim();
        } else if (line.startsWith("data:")) {
          dataLines.push(line.slice(5).trimStart());
        }
      });
      const data = dataLines.join("\n");
      await handleStreamEvent(event || "message", data, state, entry);
    }
  }
}

async function handleStreamEvent(event, rawData, state, entry) {
  const parsed = parseMaybeJson(rawData);
  if (event === "turn") {
    void startDocChatEventStream(parsed);
    return;
  }
  if (event === "status") {
    state.statusText =
      typeof parsed === "string" ? parsed : parsed.status || "";
    renderChat();
    return;
  }
  if (event === "token") {
    const token =
      typeof parsed === "string"
        ? parsed
        : parsed.token || parsed.text || rawData || "";
    entry.response = (entry.response || "") + token;
    state.streamText = entry.response;
    if (!state.statusText || state.statusText === "queued") {
      state.statusText = "responding";
    }
    renderChat();
    return;
  }
  if (event === "update") {
    const payload = parseChatPayload(parsed);
    if (payload.response) {
      entry.response = payload.response;
    }
    state.streamText = entry.response;
    const updated =
      (payload.updated && payload.updated.length
        ? payload.updated
        : Object.keys(payload.drafts || {})) || [];
    if (updated.length) {
      entry.updated = updated;
      entry.drafts = payload.drafts || {};
      applyDraftUpdates(payload.drafts);
      entry.status = "done";
    }
    renderChat();
    return;
  }
  if (event === "error") {
    const message =
      (parsed && parsed.detail) ||
      (parsed && parsed.error) ||
      rawData ||
      "Doc chat failed";
    markChatError(state, entry, message);
    resetChatEvents(state, { preserve: true });
    throw new Error(message);
  }
  if (event === "interrupted") {
    const message =
      (parsed && parsed.detail) || rawData || "Doc chat interrupted";
    entry.status = "interrupted";
    entry.error = message;
    state.error = "";
    state.status = "interrupted";
    state.streamText = entry.response || "";
    resetChatEvents(state, { preserve: true });
    renderChat();
    return;
  }
  if (event === "done" || event === "finish") {
    entry.status = "done";
    resetChatEvents(state, { preserve: true });
    return;
  }
}

function applyChatResult(payload, state, entry) {
  const parsed = parseChatPayload(payload);
  if (parsed.interrupted) {
    entry.status = "interrupted";
    entry.error = parsed.detail || "Doc chat interrupted";
    state.status = "interrupted";
    state.error = "";
    return;
  }
  if (parsed.error) {
    markChatError(state, entry, parsed.error);
    return;
  }
  entry.status = "done";
  entry.response = parsed.response || "(no response)";
  state.streamText = entry.response;
  const updated =
    (parsed.updated && parsed.updated.length
      ? parsed.updated
      : Object.keys(parsed.drafts || {})) || [];
  if (updated.length) {
    entry.updated = updated;
    entry.drafts = parsed.drafts || {};
    applyDraftUpdates(parsed.drafts);
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Doc CRUD Operations
// ─────────────────────────────────────────────────────────────────────────────

async function loadDocs() {
  try {
    const data = await api("/api/docs");
    docsCache = { ...docsCache, ...data };
    setDoc(activeDoc);
    renderTodoPreview(docsCache.todo);
    publish("docs:loaded", docsCache);
    refreshAllDrafts().catch(() => {});
  } catch (err) {
    flash(err.message);
  }
}

/**
 * Safe auto-refresh for docs that skips if there are unsaved changes.
 * This prevents overwriting user edits during background refresh.
 */
async function safeLoadDocs() {
  // Skip auto-refresh for snapshot (it has its own refresh mechanism)
  if (activeDoc === "snapshot") {
    return;
  }
  const textarea = getDocTextarea();
  const draft = getDraft(activeDoc);
  const previewing = !!draft && isDraftPreview(activeDoc);
  if (textarea) {
    const currentValue = textarea.value;
    const cachedValue = previewing ? draft.content : docsCache[activeDoc] || "";
    // Skip refresh if there are unsaved local changes
    if (currentValue !== cachedValue) {
      return;
    }
  }
  // Also skip if a chat operation is in progress
  const state = getChatState();
  if (state.status === "running") {
    return;
  }
  try {
    const data = await api("/api/docs");
    // Check again after fetch - user might have started editing
    if (
      textarea &&
      textarea.value !== (previewing ? draft.content : docsCache[activeDoc] || "")
    ) {
      return;
    }
    docsCache = { ...docsCache, ...data };
    setDoc(activeDoc);
    renderTodoPreview(docsCache.todo);
    publish("docs:loaded", docsCache);
  } catch (err) {
    // Silently fail for background refresh
    console.error("Auto-refresh docs failed:", err);
  }
}

function setDoc(kind) {
  activeDoc = kind;
  docButtons.forEach((btn) =>
    btn.classList.toggle("active", btn.dataset.doc === kind)
  );
  const isSnapshot = kind === "snapshot";
  
  // Handle snapshot vs regular doc display
  syncDocEditor(kind, { force: true });
  
  // Toggle spec issue import UI
  if (specIssueUI.row) {
    specIssueUI.row.classList.toggle("hidden", kind !== "spec");
  }
  if (specIngestUI.panel) {
    specIngestUI.panel.classList.toggle("hidden", kind !== "spec");
  }
  
  // Toggle action button sets - snapshot has its own, others share standard
  if (docActionsUI.standard) {
    docActionsUI.standard.classList.toggle("hidden", isSnapshot);
  }
  if (docActionsUI.snapshot) {
    docActionsUI.snapshot.classList.toggle("hidden", !isSnapshot);
  }
  
  // Toggle document-specific buttons within standard actions
  if (docActionsUI.ingest) {
    docActionsUI.ingest.classList.toggle("hidden", kind !== "spec");
  }
  if (docActionsUI.clear) {
    docActionsUI.clear.classList.toggle("hidden", !CLEARABLE_DOCS.includes(kind));
  }
  updateDocControls(kind);
  
  // Toggle chat panel visibility - hide for snapshot
  const chatPanel = document.querySelector(".doc-chat-panel");
  if (chatPanel) {
    chatPanel.classList.toggle("hidden", isSnapshot);
  }
  
  // Toggle patch panel visibility - hide for snapshot
  if (chatUI.patchMain) {
    if (isSnapshot) {
      chatUI.patchMain.classList.add("hidden");
    }
  }
  if (specIngestUI.patchMain) {
    if (isSnapshot) {
      specIngestUI.patchMain.classList.add("hidden");
    }
  }
  
  // Update snapshot button states when switching to snapshot
  if (isSnapshot) {
    renderSnapshotButtons();
  } else {
    reloadPatch(kind, true);
    renderChat();
    if (kind === "spec") {
      reloadSpecIngestPatch(true);
    } else {
      renderSpecIngestPatch();
    }
  }
  updateUrlParams({ doc: kind });
}

async function importIssueToSpec() {
  if (!specIssueUI.input || !specIssueUI.button) return;
  const issue = (specIssueUI.input.value || "").trim();
  if (!issue) {
    flash("Enter a GitHub issue number or URL", "error");
    return;
  }
  const state = getChatState();
  if (state.status === "running") {
    flash("SPEC chat is running; try again shortly", "error");
    return;
  }

  specIssueUI.button.disabled = true;
  specIssueUI.button.classList.add("loading");
  try {
    const entry = {
      id: `${Date.now()}`,
      prompt: `Import issue → SPEC: ${issue}`,
      targets: ["spec"],
      response: "",
      status: "running",
      time: Date.now(),
      drafts: {},
      updated: [],
    };
    state.history.unshift(entry);
    state.status = "running";
    state.error = "";
    state.streamText = "";
    state.statusText = "importing issue";
    renderChat();

    const res = await api("/api/github/spec/from-issue", {
      method: "POST",
      body: { issue },
    });
    applyChatResult(res, state, entry);
    entry.status = "done";
    state.status = "idle";
    // Hide input row and reset toggle after successful import
    if (specIssueUI.inputRow) {
      specIssueUI.inputRow.classList.add("hidden");
    }
    if (specIssueUI.toggle) {
      specIssueUI.toggle.textContent = "Import Issue → SPEC";
    }
    if (specIssueUI.input) {
      specIssueUI.input.value = "";
    }
    flash("Imported issue into pending SPEC draft");
  } catch (err) {
    const message = err?.message || "Issue import failed";
    const entry = state.history[0];
    if (entry) {
      entry.status = "error";
      entry.error = message;
    }
    state.status = "idle";
    state.error = message;
    flash(message, "error");
  } finally {
    specIssueUI.button.disabled = false;
    specIssueUI.button.classList.remove("loading");
    renderChat();
  }
}

async function saveDoc() {
  // Snapshot is read-only, no saving
  if (activeDoc === "snapshot") {
    flash("Snapshot is read-only. Use Generate to update.", "error");
    return;
  }
  if (hasDraft(activeDoc) && isDraftPreview(activeDoc)) {
    flash("Exit draft preview before saving.", "error");
    return;
  }
  const content = document.getElementById("doc-content").value;
  const saveBtn = document.getElementById("save-doc");
  saveBtn.disabled = true;
  saveBtn.classList.add("loading");
  try {
    await api(`/api/docs/${activeDoc}`, { method: "PUT", body: { content } });
    docsCache[activeDoc] = content;
    flash(`${activeDoc.toUpperCase()} saved`);
    publish("docs:updated", { kind: activeDoc, content });
    if (activeDoc === "todo") {
      renderTodoPreview(content);
      await loadState({ notify: false });
    }
  } catch (err) {
    flash(err.message);
  } finally {
    saveBtn.disabled = false;
    saveBtn.classList.remove("loading");
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Snapshot Functions
// ─────────────────────────────────────────────────────────────────────────────

function setSnapshotBusy(on) {
  snapshotBusy = on;
  const disabled = !!on;
  for (const btn of [snapshotUI.generate, snapshotUI.update, snapshotUI.regenerate, snapshotUI.refresh]) {
    if (btn) btn.disabled = disabled;
  }
  updateCopyButton(snapshotUI.copy, getDocCopyText("snapshot"), disabled);
  const statusEl = document.getElementById("doc-status");
  if (statusEl && activeDoc === "snapshot") {
    statusEl.textContent = on ? "Working…" : "Viewing SNAPSHOT";
  }
}

function renderSnapshotButtons() {
  // Single default behavior: one "Run snapshot" action.
  if (snapshotUI.generate) snapshotUI.generate.classList.toggle("hidden", false);
  if (snapshotUI.update) snapshotUI.update.classList.toggle("hidden", true);
  if (snapshotUI.regenerate) snapshotUI.regenerate.classList.toggle("hidden", true);
  updateCopyButton(snapshotUI.copy, getDocCopyText("snapshot"), snapshotBusy);
}

async function loadSnapshot({ notify = false } = {}) {
  if (snapshotBusy) return;
  try {
    setSnapshotBusy(true);
    const data = await api("/api/snapshot");
    snapshotCache = {
      exists: !!data?.exists,
      content: data?.content || "",
      state: data?.state || {},
    };
    if (activeDoc === "snapshot") {
      const textarea = getDocTextarea();
      if (textarea) textarea.value = snapshotCache.content || "";
    }
    renderSnapshotButtons();
    if (notify) flash(snapshotCache.exists ? "Snapshot loaded" : "No snapshot yet");
  } catch (err) {
    flash(err?.message || "Failed to load snapshot");
  } finally {
    setSnapshotBusy(false);
  }
}

async function runSnapshot() {
  if (snapshotBusy) return;
  try {
    setSnapshotBusy(true);
    const data = await api("/api/snapshot", {
      method: "POST",
      body: {},
    });
    snapshotCache = {
      exists: true,
      content: data?.content || "",
      state: data?.state || {},
    };
    if (activeDoc === "snapshot") {
      const textarea = getDocTextarea();
      if (textarea) textarea.value = snapshotCache.content || "";
    }
    renderSnapshotButtons();
    flash("Snapshot generated");
  } catch (err) {
    flash(err?.message || "Snapshot generation failed");
  } finally {
    setSnapshotBusy(false);
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Initialization
// ─────────────────────────────────────────────────────────────────────────────

function applyVoiceTranscript(text) {
  if (!text) {
    flash("Voice capture returned no transcript", "error");
    return;
  }
  const current = chatUI.input.value.trim();
  const prefix = current ? current + " " : "";
  let next = `${prefix}${text}`.trim();
  next = appendVoiceTranscriptDisclaimer(next);
  chatUI.input.value = next;
  autoResizeTextarea(chatUI.input);
  chatUI.input.focus();
  flash("Voice transcript added");
}

function appendVoiceTranscriptDisclaimer(text) {
  const base = text === undefined || text === null ? "" : String(text);
  if (!base.trim()) return base;
  const injection = wrapInjectedContext(VOICE_TRANSCRIPT_DISCLAIMER_TEXT);
  if (base.includes(VOICE_TRANSCRIPT_DISCLAIMER_TEXT) || base.includes(injection)) {
    return base;
  }
  const separator = base.endsWith("\n") ? "\n" : "\n\n";
  return `${base}${separator}${injection}`;
}

function wrapInjectedContext(text) {
  return `<injected context>\n${text}\n</injected context>`;
}

function initDocVoice() {
  if (!chatUI.voiceBtn || !chatUI.input) {
    return;
  }
  initVoiceInput({
    button: chatUI.voiceBtn,
    input: chatUI.input,
    statusEl: chatUI.voiceStatus,
    onTranscript: applyVoiceTranscript,
    onError: (msg) => {
      if (msg) {
        flash(msg, "error");
        if (chatUI.voiceStatus) {
          chatUI.voiceStatus.textContent = msg;
          chatUI.voiceStatus.classList.remove("hidden");
        }
      }
    },
  }).catch((err) => {
    console.error("Voice init failed", err);
    flash("Voice capture unavailable", "error");
  });
}

function renderThreadRegistryBanner(notice) {
  if (!threadRegistryUI.banner) return;
  const active = notice && notice.status === "corrupt";
  threadRegistryUI.banner.classList.toggle("hidden", !active);
  if (!active) return;
  const backupPath =
    notice && typeof notice.backup_path === "string" ? notice.backup_path : "";
  if (threadRegistryUI.detail) {
    threadRegistryUI.detail.textContent = backupPath
      ? `Backup: ${backupPath}`
      : "Backup unavailable";
    threadRegistryUI.detail.title = backupPath || "";
  }
  if (threadRegistryUI.download) {
    threadRegistryUI.download.classList.toggle("hidden", !backupPath);
  }
}

async function loadThreadRegistryStatus() {
  if (!threadRegistryUI.banner) return;
  try {
    const data = await api("/api/app-server/threads");
    renderThreadRegistryBanner(data?.corruption);
  } catch (err) {
    console.error("Failed to load thread registry status", err);
  }
}

async function resetThreadRegistry() {
  try {
    await api("/api/app-server/threads/reset-all", { method: "POST" });
    renderThreadRegistryBanner(null);
    flash("Conversations reset");
  } catch (err) {
    flash(err.message || "Failed to reset conversations", "error");
  }
}

function downloadThreadRegistryBackup() {
  window.location.href = resolvePath("/api/app-server/threads/backup");
}

export function initDocs() {
  if (!chatUI.send || !chatUI.input) {
    console.warn("Doc chat UI elements missing; skipping doc chat init.");
  }
  const urlDoc = getDocFromUrl();
  if (urlDoc) {
    activeDoc = urlDoc;
  }
  docButtons.forEach((btn) =>
    btn.addEventListener("click", () => {
      setDoc(btn.dataset.doc);
    })
  );
  document.getElementById("save-doc").addEventListener("click", saveDoc);
  document.getElementById("reload-doc").addEventListener("click", () => {
    if (activeDoc === "snapshot") {
      loadSnapshot({ notify: true });
    } else {
      loadDocs();
    }
  });
  document.getElementById("ingest-spec").addEventListener("click", ingestSpec);
  document.getElementById("clear-docs").addEventListener("click", clearDocs);
  if (specIngestUI.continueBtn) {
    specIngestUI.continueBtn.addEventListener("click", continueSpecIngest);
  }
  if (specIngestUI.cancelBtn) {
    specIngestUI.cancelBtn.addEventListener("click", cancelSpecIngest);
  }
  if (specIngestUI.input) {
    specIngestUI.input.addEventListener("input", () => {
      autoResizeTextarea(specIngestUI.input);
    });
    specIngestUI.input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        continueSpecIngest();
      }
    });
  }
  if (specIngestUI.patchApply)
    specIngestUI.patchApply.addEventListener("click", applySpecIngestPatch);
  if (specIngestUI.patchDiscard)
    specIngestUI.patchDiscard.addEventListener("click", discardSpecIngestPatch);
  if (specIngestUI.patchReload)
    specIngestUI.patchReload.addEventListener("click", () =>
      reloadSpecIngestPatch(false)
    );
  if (docActionsUI.copy) {
    docActionsUI.copy.addEventListener("click", () =>
      copyDocToClipboard(activeDoc)
    );
  }
  if (docActionsUI.paste) {
    docActionsUI.paste.addEventListener("click", pasteSpecFromClipboard);
  }
  if (threadRegistryUI.reset) {
    threadRegistryUI.reset.addEventListener("click", resetThreadRegistry);
  }
  if (threadRegistryUI.download) {
    threadRegistryUI.download.addEventListener(
      "click",
      downloadThreadRegistryBackup
    );
  }
  const docContent = getDocTextarea();
  if (docContent) {
    docContent.addEventListener("input", () => {
      if (activeDoc !== "snapshot") {
        updateDocControls(activeDoc);
      }
    });
  }
  let suppressNextSendClick = false;
  let lastSendTapAt = 0;
  const triggerSend = () => {
    const now = Date.now();
    if (now - lastSendTapAt < 300) return;
    lastSendTapAt = now;
    sendDocChat();
  };
  if (chatUI.send) {
    chatUI.send.addEventListener("pointerup", (e) => {
      if (e.pointerType !== "touch") return;
      if (e.cancelable) e.preventDefault();
      suppressNextSendClick = true;
      triggerSend();
    });
    chatUI.send.addEventListener("click", () => {
      if (suppressNextSendClick) {
        suppressNextSendClick = false;
        return;
      }
      triggerSend();
    });
  }
  if (chatUI.cancel) {
    chatUI.cancel.addEventListener("click", cancelDocChat);
  }
  if (chatUI.newThread) {
    chatUI.newThread.addEventListener("click", startNewDocChatThread);
  }
  if (chatUI.patchApply)
    chatUI.patchApply.addEventListener("click", () => applyPatch(activeDoc));
  if (chatUI.patchDiscard)
    chatUI.patchDiscard.addEventListener("click", () =>
      discardPatch(activeDoc)
    );
  if (chatUI.patchReload)
    chatUI.patchReload.addEventListener("click", () =>
      reloadPatch(activeDoc, true)
    );
  if (chatUI.patchPreview)
    chatUI.patchPreview.addEventListener("click", () =>
      toggleDraftPreview(activeDoc)
    );
  if (specIssueUI.toggle) {
    specIssueUI.toggle.addEventListener("click", () => {
      if (specIssueUI.inputRow) {
        const isHidden = specIssueUI.inputRow.classList.toggle("hidden");
        if (!isHidden && specIssueUI.input) {
          specIssueUI.input.focus();
        }
        // Update toggle button text
        specIssueUI.toggle.textContent = isHidden
          ? "Import Issue → SPEC"
          : "Cancel";
      }
    });
  }
  if (specIssueUI.button) {
    specIssueUI.button.addEventListener("click", () => {
      if (activeDoc !== "spec") setDoc("spec");
      importIssueToSpec();
    });
  }
  if (specIssueUI.input) {
    specIssueUI.input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        if (activeDoc !== "spec") setDoc("spec");
        importIssueToSpec();
      }
    });
  }
  
  // Snapshot event handlers
  if (snapshotUI.generate) {
    snapshotUI.generate.addEventListener("click", () => runSnapshot());
  }
  if (snapshotUI.update) {
    snapshotUI.update.addEventListener("click", () => runSnapshot());
  }
  if (snapshotUI.regenerate) {
    snapshotUI.regenerate.addEventListener("click", () => runSnapshot());
  }
  if (snapshotUI.copy) {
    snapshotUI.copy.addEventListener("click", () =>
      copyDocToClipboard("snapshot")
    );
  }
  if (snapshotUI.refresh) {
    snapshotUI.refresh.addEventListener("click", () => loadSnapshot({ notify: true }));
  }
  
  initDocVoice();
  loadThreadRegistryStatus();
  refreshAllDrafts();
  reloadSpecIngestPatch(true);

  // Cmd+Enter or Ctrl+Enter sends, Enter adds newline on all devices.
  // Up/Down arrows navigate prompt history when input is empty
  if (chatUI.input) {
    chatUI.input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.isComposing) {
        const shouldSend = e.metaKey || e.ctrlKey;
        if (shouldSend) {
          e.preventDefault();
          sendDocChat();
        }
        e.stopPropagation();
        return;
      }

      // Up arrow: recall previous prompts from history
      if (e.key === "ArrowUp") {
        const state = getChatState();
        const isEmpty = chatUI.input.value.trim() === "";
        const atStart = chatUI.input.selectionStart === 0;
        if ((isEmpty || atStart) && state.history.length > 0) {
          e.preventDefault();
          const maxIndex = state.history.length - 1;
          if (historyNavIndex < maxIndex) {
            historyNavIndex++;
            chatUI.input.value = state.history[historyNavIndex].prompt || "";
            autoResizeTextarea(chatUI.input);
            // Move cursor to end
            chatUI.input.setSelectionRange(
              chatUI.input.value.length,
              chatUI.input.value.length
            );
          }
        }
        return;
      }

      // Down arrow: navigate forward in history or clear
      if (e.key === "ArrowDown") {
        const state = getChatState();
        const atEnd = chatUI.input.selectionStart === chatUI.input.value.length;
        if (historyNavIndex >= 0 && atEnd) {
          e.preventDefault();
          historyNavIndex--;
          if (historyNavIndex >= 0) {
            chatUI.input.value = state.history[historyNavIndex].prompt || "";
          } else {
            chatUI.input.value = "";
          }
          autoResizeTextarea(chatUI.input);
          chatUI.input.setSelectionRange(
            chatUI.input.value.length,
            chatUI.input.value.length
          );
        }
        return;
      }
    });
  }

  // Clear errors on input, auto-resize textarea, and reset history navigation
  if (chatUI.input) {
    chatUI.input.addEventListener("input", () => {
      const state = getChatState();
      if (state.error) {
        state.error = "";
        renderChat();
      }
      // Reset history navigation when user types
      historyNavIndex = -1;
      autoResizeTextarea(chatUI.input);
    });
  }

  // Ctrl+S / Cmd+S saves the current doc
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "s") {
      // Only handle if docs tab is active
      const docsTab = document.getElementById("docs");
      if (docsTab && !docsTab.classList.contains("hidden")) {
        e.preventDefault();
        saveDoc();
      }
    }
  });

  loadDocs();
  loadSnapshot().catch(() => {}); // Pre-load snapshot data
  renderChat();
  document.body.dataset.docsReady = "true";
  publish("docs:ready");

  // Register auto-refresh for docs (only when docs tab is active)
  // Uses a smart refresh that checks for unsaved changes
  registerAutoRefresh("docs-content", {
    callback: safeLoadDocs,
    tabId: "docs",
    interval: CONSTANTS.UI.AUTO_REFRESH_INTERVAL,
    refreshOnActivation: true,
    immediate: false, // Already called loadDocs() above
  });
}

async function ingestSpec() {
  if (specIngestState.busy) return;
  const needsForce = ["todo", "progress", "opinions"].some(
    (k) => (docsCache[k] || "").trim().length > 0
  );
  if (needsForce) {
    const ok = await confirmModal(
      "Overwrite TODO, PROGRESS, and OPINIONS from SPEC? Existing content will be replaced."
    );
    if (!ok) return;
  }
  const button = document.getElementById("ingest-spec");
  button.disabled = true;
  button.classList.add("loading");
  specIngestState.busy = true;
  specIngestState.controller = new AbortController();
  renderSpecIngestPatch();
  try {
    const data = await api("/api/ingest-spec", {
      method: "POST",
      body: { force: needsForce },
      signal: specIngestState.controller.signal,
    });
    const parsed = parseSpecIngestPayload(data);
    if (parsed.error) throw new Error(parsed.error);
    if (parsed.interrupted) {
      specIngestState.patch = "";
      specIngestState.agentMessage = parsed.agentMessage || "";
      applySpecIngestDocs(parsed);
      renderSpecIngestPatch();
      flash("Spec ingest interrupted");
      return;
    }
    specIngestState.patch = parsed.patch || "";
    specIngestState.agentMessage = parsed.agentMessage || "";
    applySpecIngestDocs(parsed);
    renderSpecIngestPatch();
    flash(parsed.patch ? "Spec ingest patch ready" : "Ingested SPEC into docs");
  } catch (err) {
    if (err.name === "AbortError") {
      return;
    } else {
      flash(err.message, "error");
    }
  } finally {
    button.disabled = false;
    button.classList.remove("loading");
    specIngestState.busy = false;
    specIngestState.controller = null;
    renderSpecIngestPatch();
  }
}

async function interruptSpecIngest() {
  try {
    await api("/api/ingest-spec/interrupt", { method: "POST" });
  } catch (err) {
    flash(err.message || "Failed to interrupt spec ingest", "error");
  }
}

function cancelSpecIngest() {
  if (!specIngestState.busy) return;
  interruptSpecIngest();
  if (specIngestState.controller) specIngestState.controller.abort();
  specIngestState.busy = false;
  specIngestState.controller = null;
  if (specIngestUI.continueBtn) specIngestUI.continueBtn.disabled = false;
  flash("Spec ingest interrupted");
  renderSpecIngestPatch();
}

async function continueSpecIngest() {
  if (specIngestState.busy) return;
  if (!specIngestUI.input) return;
  const message = (specIngestUI.input.value || "").trim();
  if (!message) {
    flash("Enter a follow-up prompt to continue", "error");
    return;
  }
  const needsForce = ["todo", "progress", "opinions"].some(
    (k) => (docsCache[k] || "").trim().length > 0
  );
  specIngestState.busy = true;
  if (specIngestUI.continueBtn) specIngestUI.continueBtn.disabled = true;
  specIngestState.controller = new AbortController();
  renderSpecIngestPatch();
  try {
    const data = await api("/api/ingest-spec", {
      method: "POST",
      body: { force: needsForce, message },
      signal: specIngestState.controller.signal,
    });
    const parsed = parseSpecIngestPayload(data);
    if (parsed.error) throw new Error(parsed.error);
    if (parsed.interrupted) {
      specIngestState.patch = "";
      specIngestState.agentMessage = parsed.agentMessage || "";
      applySpecIngestDocs(parsed);
      renderSpecIngestPatch();
      flash("Spec ingest interrupted");
      return;
    }
    specIngestState.patch = parsed.patch || "";
    specIngestState.agentMessage = parsed.agentMessage || "";
    applySpecIngestDocs(parsed);
    renderSpecIngestPatch();
    specIngestUI.input.value = "";
    autoResizeTextarea(specIngestUI.input);
    flash(parsed.patch ? "Spec ingest patch updated" : "Spec ingest updated docs");
  } catch (err) {
    if (err.name === "AbortError") {
      return;
    } else {
      flash(err.message, "error");
    }
  } finally {
    specIngestState.busy = false;
    if (specIngestUI.continueBtn) specIngestUI.continueBtn.disabled = false;
    specIngestState.controller = null;
    renderSpecIngestPatch();
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Spec Ingestion & Doc Clearing
// ─────────────────────────────────────────────────────────────────────────────

async function applySpecIngestPatch() {
  if (!specIngestState.patch) {
    flash("No spec ingest patch to apply", "error");
    return;
  }
  specIngestState.busy = true;
  renderSpecIngestPatch();
  try {
    const res = await api("/api/ingest-spec/apply", { method: "POST" });
    const parsed = parseSpecIngestPayload(res);
    if (parsed.error) throw new Error(parsed.error);
    specIngestState.patch = "";
    specIngestState.agentMessage = "";
    applySpecIngestDocs(parsed);
    flash("Spec ingest patch applied");
  } catch (err) {
    flash(err.message || "Failed to apply spec ingest patch", "error");
  } finally {
    specIngestState.busy = false;
    renderSpecIngestPatch();
  }
}

async function discardSpecIngestPatch() {
  if (!specIngestState.patch) return;
  specIngestState.busy = true;
  renderSpecIngestPatch();
  try {
    const res = await api("/api/ingest-spec/discard", { method: "POST" });
    const parsed = parseSpecIngestPayload(res);
    if (parsed.error) throw new Error(parsed.error);
    specIngestState.patch = "";
    specIngestState.agentMessage = "";
    applySpecIngestDocs(parsed);
    flash("Spec ingest patch discarded");
  } catch (err) {
    flash(err.message || "Failed to discard spec ingest patch", "error");
  } finally {
    specIngestState.busy = false;
    renderSpecIngestPatch();
  }
}

async function reloadSpecIngestPatch(silent = false) {
  try {
    const res = await api("/api/ingest-spec/pending", { method: "GET" });
    const parsed = parseSpecIngestPayload(res);
    if (parsed.error) throw new Error(parsed.error);
    if (parsed.patch) {
      specIngestState.patch = parsed.patch;
      specIngestState.agentMessage = parsed.agentMessage || "";
      applySpecIngestDocs(parsed);
      renderSpecIngestPatch();
      if (!silent) flash("Loaded spec ingest patch");
      return;
    }
  } catch (err) {
    const message = err?.message || "";
    if (message.includes("No pending spec ingest patch")) {
      specIngestState.patch = "";
      specIngestState.agentMessage = "";
      renderSpecIngestPatch();
      return;
    }
    if (!silent) {
      flash(message || "Failed to load spec ingest patch", "error");
    }
  }
  if (!specIngestState.patch) {
    renderSpecIngestPatch();
  }
}

async function clearDocs() {
  const confirmed = await confirmModal(
    "Clear TODO, PROGRESS, and OPINIONS? This action cannot be undone."
  );
  if (!confirmed) {
    flash("Clear cancelled");
    return;
  }
  const button = document.getElementById("clear-docs");
  button.disabled = true;
  button.classList.add("loading");
  try {
    const data = await api("/api/docs/clear", { method: "POST" });
    docsCache = { ...docsCache, ...data };
    // Update UI directly (consistent with ingestSpec)
    setDoc(activeDoc);
    renderTodoPreview(docsCache.todo);
    publish("docs:updated", { kind: "todo", content: docsCache.todo });
    publish("docs:updated", { kind: "progress", content: docsCache.progress });
    publish("docs:updated", { kind: "opinions", content: docsCache.opinions });
    flash("Cleared TODO/PROGRESS/OPINIONS");
  } catch (err) {
    flash(err.message, "error");
  } finally {
    button.disabled = false;
    button.classList.remove("loading");
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Test Exports
// ─────────────────────────────────────────────────────────────────────────────

export const __docChatTest = {
  applyChatResult,
  applyDocUpdateFromChat,
  applyPatch,
  reloadPatch,
  discardPatch,
  getChatState,
  handleStreamEvent,
  performDocChatRequest,
  renderChat,
  setDoc,
};
