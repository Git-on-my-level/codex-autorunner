import {
  api,
  flash,
  statusPill,
  confirmModal,
  resolvePath,
  getAuthToken,
  isMobileViewport,
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

const docButtons = document.querySelectorAll(".chip[data-doc]");
let docsCache = { todo: "", progress: "", opinions: "", spec: "", summary: "" };
let snapshotCache = { exists: false, content: "", state: {} };
let snapshotBusy = false;
let activeDoc = "todo";

const chatDecoder = new TextDecoder();
const chatState = Object.fromEntries(
  DOC_TYPES.map((k) => [k, createChatState()])
);

// Track history navigation position for up/down arrow prompt recall
let historyNavIndex = -1;

// ─────────────────────────────────────────────────────────────────────────────
// UI Element References
// ─────────────────────────────────────────────────────────────────────────────

const chatUI = {
  status: document.getElementById("doc-chat-status"),
  response: document.getElementById("doc-chat-response"),
  responseWrapper: document.getElementById("doc-chat-response-wrapper"),
  patchMain: document.getElementById("doc-patch-main"),
  patchSummary: document.getElementById("doc-patch-summary"),
  patchBody: document.getElementById("doc-patch-body"),
  patchApply: document.getElementById("doc-patch-apply"),
  patchDiscard: document.getElementById("doc-patch-discard"),
  patchReload: document.getElementById("doc-patch-reload"),
  history: document.getElementById("doc-chat-history"),
  historyDetails: document.getElementById("doc-chat-history-details"),
  historyCount: document.getElementById("doc-chat-history-count"),
  error: document.getElementById("doc-chat-error"),
  input: document.getElementById("doc-chat-input"),
  send: document.getElementById("doc-chat-send"),
  cancel: document.getElementById("doc-chat-cancel"),
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
    patch: "",
  };
}

function getChatState(kind = activeDoc) {
  if (!chatState[kind]) {
    chatState[kind] = createChatState();
  }
  return chatState[kind];
}

// ─────────────────────────────────────────────────────────────────────────────
// Utilities
// ─────────────────────────────────────────────────────────────────────────────

function parseChatPayload(payload) {
  if (!payload) return { response: "" };
  if (typeof payload === "string") return { response: payload };
  if (payload.status && payload.status !== "ok") {
    return { error: payload.detail || "Doc chat failed" };
  }
  return {
    response: payload.agent_message || payload.message || payload.content || "",
    content: payload.content || "",
    patch: payload.patch || "",
  };
}

function parseMaybeJson(raw) {
  try {
    return JSON.parse(raw);
  } catch (err) {
    return raw;
  }
}

function truncateText(text, maxLen) {
  if (!text) return "";
  const normalized = text.replace(/\s+/g, " ").trim();
  return normalized.length > maxLen
    ? normalized.slice(0, maxLen) + "…"
    : normalized;
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
    updateStandardActionButtons("spec");
    flash("SPEC replaced from clipboard");
  } catch {
    flash("Paste failed", "error");
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Chat UI Rendering
// ─────────────────────────────────────────────────────────────────────────────

async function applyDocUpdateFromChat(kind, content) {
  if (!content) return false;
  const textarea = getDocTextarea();
  const viewingSameDoc = activeDoc === kind;
  if (viewingSameDoc && textarea) {
    const cached = docsCache[kind] || "";
    if (textarea.value !== cached) {
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
  if (viewingSameDoc && textarea) {
    textarea.value = content;
    document.getElementById(
      "doc-status"
    ).textContent = `Editing ${kind.toUpperCase()}`;
  }
  if (viewingSameDoc) {
    updateStandardActionButtons(kind);
  }
  publish("docs:updated", { kind, content });
  if (kind === "todo") {
    renderTodoPreview(content);
    loadState({ notify: false }).catch(() => {});
  }
  return true;
}

function renderChat(kind = activeDoc) {
  if (kind !== activeDoc) return;
  const state = getChatState(kind);
  const latest = state.history[0];
  const isRunning = state.status === "running";
  const hasError = !!state.error;

  // Update status pill
  const pillState = isRunning
    ? "running"
    : state.status === "error"
    ? "error"
    : "idle";
  statusPill(chatUI.status, pillState);

  // Update input state
  chatUI.send.disabled = isRunning;
  chatUI.input.disabled = isRunning;
  chatUI.cancel.classList.toggle("hidden", !isRunning);
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

  // Update hint text - show status inline when running
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

  // Handle error display
  if (hasError) {
    chatUI.error.textContent = state.error;
    chatUI.error.classList.remove("hidden");
  } else {
    chatUI.error.textContent = "";
    chatUI.error.classList.add("hidden");
  }

  // Compute response text - only show actual content, not placeholders
  let responseText = "";
  if (isRunning && state.streamText) {
    responseText = state.streamText;
  } else if (!isRunning && latest && (latest.response || latest.error)) {
    responseText = latest.response || latest.error;
  }

  // Show response wrapper only when there's real content or an error
  const showResponse = !!responseText || hasError;
  chatUI.responseWrapper.classList.toggle("hidden", !showResponse);
  chatUI.response.textContent = responseText;
  chatUI.response.classList.toggle("streaming", isRunning && state.streamText);

  const hasPatch = !!(state.patch && state.patch.trim());
  if (chatUI.patchMain) {
    chatUI.patchMain.classList.toggle("hidden", !hasPatch);
    // Use syntax-highlighted diff rendering
    chatUI.patchBody.innerHTML = hasPatch
      ? renderDiffHtml(state.patch)
      : "(no patch)";
    chatUI.patchSummary.textContent = latest?.response || state.error || "";
    if (chatUI.patchApply) chatUI.patchApply.disabled = isRunning || !hasPatch;
    if (chatUI.patchDiscard)
      chatUI.patchDiscard.disabled = isRunning || !hasPatch;
    if (chatUI.patchReload) chatUI.patchReload.disabled = isRunning;
  }

  const docContent = getDocTextarea();
  if (docContent) {
    docContent.classList.toggle("hidden", hasPatch);
  }

  renderChatHistory(state);
}

function renderChatHistory(state) {
  if (!chatUI.history) return;

  const count = state.history.length;
  chatUI.historyCount.textContent = count;

  // Hide history details if empty
  if (chatUI.historyDetails) {
    chatUI.historyDetails.style.display = count === 0 ? "none" : "";
  }

  chatUI.history.innerHTML = "";
  if (count === 0) return;

  state.history.slice(0, CHAT_HISTORY_LIMIT).forEach((entry) => {
    const wrapper = document.createElement("div");
    wrapper.className = `doc-chat-entry ${entry.status}`;

    // Prompt row with copy button
    const promptRow = document.createElement("div");
    promptRow.className = "prompt-row";

    const prompt = document.createElement("div");
    prompt.className = "prompt";
    prompt.textContent = truncateText(entry.prompt, 60);
    prompt.title = entry.prompt;

    const copyBtn = document.createElement("button");
    copyBtn.className = "copy-prompt-btn";
    copyBtn.title = "Copy to input";
    copyBtn.innerHTML = "↑";
    copyBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      chatUI.input.value = entry.prompt;
      autoResizeTextarea(chatUI.input);
      chatUI.input.focus();
      historyNavIndex = -1;
      flash("Prompt restored to input");
    });

    promptRow.appendChild(prompt);
    promptRow.appendChild(copyBtn);

    const response = document.createElement("div");
    response.className = "response";
    const preview = entry.error || entry.response || "(pending...)";
    response.textContent = truncateText(preview, 80);
    response.title = preview;

    const detail = document.createElement("details");
    detail.className = "doc-chat-entry-detail";
    const summary = document.createElement("summary");
    summary.textContent = "View details";
    const body = document.createElement("div");
    body.className = "doc-chat-entry-body";
    if (entry.response) {
      const respBlock = document.createElement("pre");
      respBlock.textContent = entry.response;
      body.appendChild(respBlock);
    }
    if (entry.patch) {
      const patchBlock = document.createElement("pre");
      patchBlock.className = "doc-chat-entry-patch";
      patchBlock.textContent = entry.patch;
      body.appendChild(patchBlock);
    }
    detail.appendChild(summary);
    detail.appendChild(body);

    const meta = document.createElement("div");
    meta.className = "meta";

    const dot = document.createElement("span");
    dot.className = "status-dot";

    const stamp = document.createElement("span");
    stamp.textContent = entry.time
      ? new Date(entry.time).toLocaleTimeString([], {
          hour: "2-digit",
          minute: "2-digit",
        })
      : entry.status;

    meta.appendChild(dot);
    meta.appendChild(stamp);

    wrapper.appendChild(promptRow);
    wrapper.appendChild(response);
    wrapper.appendChild(detail);
    wrapper.appendChild(meta);
    chatUI.history.appendChild(wrapper);
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// Chat Actions & Error Handling
// ─────────────────────────────────────────────────────────────────────────────

function markChatError(state, entry, message) {
  entry.status = "error";
  entry.error = message;
  state.error = message;
  state.status = "error";
  state.patch = "";
  renderChat();
}

function cancelDocChat() {
  const state = getChatState(activeDoc);
  if (state.status !== "running") return;
  if (state.controller) state.controller.abort();
  const entry = state.history[0];
  if (entry && entry.status === "running") {
    entry.status = "error";
    entry.error = "Cancelled";
  }
  state.status = "idle";
  state.controller = null;
  renderChat();
}

async function sendDocChat() {
  const message = (chatUI.input.value || "").trim();
  const state = getChatState(activeDoc);
  if (!message) {
    state.error = "Enter a message to send.";
    renderChat();
    return;
  }
  if (state.status === "running") return;

  const entry = {
    id: `${Date.now()}`,
    prompt: message,
    response: "",
    status: "running",
    time: Date.now(),
    lastAppliedContent: null,
    patch: "",
  };
  state.history.unshift(entry);
  if (state.history.length > CHAT_HISTORY_LIMIT * 2) {
    state.history.length = CHAT_HISTORY_LIMIT * 2;
  }
  state.status = "running";
  state.error = "";
  state.streamText = "";
  state.patch = "";
  state.statusText = "queued";
  state.controller = new AbortController();

  // Collapse history when starting new request for compact view
  if (chatUI.historyDetails) {
    chatUI.historyDetails.removeAttribute("open");
  }

  renderChat();
  chatUI.input.value = "";
  chatUI.input.style.height = "auto"; // Reset textarea height
  chatUI.input.focus();

  try {
    await performDocChatRequest(activeDoc, entry, state);
    if (entry.status !== "error") {
      state.status = "idle";
      state.error = "";
    }
  } catch (err) {
    if (err.name === "AbortError") {
      entry.status = "error";
      entry.error = "Cancelled";
      state.error = "";
      state.status = "idle";
    } else {
      markChatError(state, entry, err.message || "Doc chat failed");
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

async function performDocChatRequest(kind, entry, state) {
  const endpoint = resolvePath(`/api/docs/${kind}/chat`);
  const headers = { "Content-Type": "application/json" };
  const token = getAuthToken();
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  const res = await fetch(endpoint, {
    method: "POST",
    headers,
    body: JSON.stringify({ message: entry.prompt, stream: true }),
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
    await readChatStream(res, state, entry, kind);
    if (entry.status !== "error" && entry.status !== "done") {
      entry.status = "done";
    }
  } else {
    const payload = contentType.includes("application/json")
      ? await res.json()
      : await res.text();
    applyChatResult(payload, state, entry);
  }
}

async function applyPatch(kind = activeDoc) {
  const state = getChatState(kind);
  if (!state.patch) {
    flash("No patch to apply", "error");
    return;
  }
  try {
    const res = await api(`/api/docs/${kind}/chat/apply`, { method: "POST" });
    const applied = parseChatPayload(res);
    if (applied.error) throw new Error(applied.error);
    if (applied.content) {
      await applyDocUpdateFromChat(kind, applied.content);
    }
    state.patch = "";
    const latest = state.history[0];
    if (latest) latest.status = "done";
    flash("Patch applied");
  } catch (err) {
    flash(err.message || "Failed to apply patch", "error");
  } finally {
    renderChat(kind);
  }
}

async function discardPatch(kind = activeDoc) {
  const state = getChatState(kind);
  if (!state.patch) return;
  try {
    const res = await api(`/api/docs/${kind}/chat/discard`, { method: "POST" });
    const parsed = parseChatPayload(res);
    if (parsed.content) {
      await applyDocUpdateFromChat(kind, parsed.content);
    }
    state.patch = "";
    const latest = state.history[0];
    if (latest && latest.status === "needs-apply") {
      latest.status = "done";
    }
    flash("Discarded chat patch");
  } catch (err) {
    flash(err.message || "Failed to discard patch", "error");
  } finally {
    renderChat(kind);
  }
}

async function reloadPatch(kind = activeDoc, silent = false) {
  const state = getChatState(kind);
  try {
    const res = await api(`/api/docs/${kind}/chat/pending`, { method: "GET" });
    const parsed = parseChatPayload(res);
    if (parsed.error) throw new Error(parsed.error);
    if (parsed.patch) {
      state.patch = parsed.patch;
      const entry = state.history[0] || {
        id: `${Date.now()}`,
        prompt: "(pending patch)",
        response: parsed.response || "",
        status: "needs-apply",
        time: Date.now(),
        lastAppliedContent: null,
        patch: parsed.patch,
      };
      entry.patch = parsed.patch;
      entry.response = parsed.response || entry.response;
      entry.status = "needs-apply";
      if (!state.history[0]) state.history.unshift(entry);
      if (parsed.content) {
        await applyDocUpdateFromChat(kind, parsed.content);
      }
      renderChat(kind);
      if (!silent) flash("Loaded pending patch");
    }
  } catch (err) {
    if (!silent) flash(err.message || "No pending patch", "error");
  }
}

async function readChatStream(res, state, entry, kind) {
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
      await handleStreamEvent(event || "message", data, state, entry, kind);
    }
  }
}

async function handleStreamEvent(event, rawData, state, entry, kind) {
  const parsed = parseMaybeJson(rawData);
  if (event === "status") {
    state.statusText =
      typeof parsed === "string" ? parsed : parsed.status || "";
    renderChat(kind);
    return;
  }
  if (event === "token") {
    const token =
      typeof parsed === "string"
        ? parsed
        : parsed.token || parsed.text || rawData || "";
    entry.response = (entry.response || "") + token;
    state.streamText = entry.response;
    renderChat(kind);
    return;
  }
  if (event === "update") {
    const payload = parseChatPayload(parsed);
    entry.response = payload.response || entry.response;
    state.streamText = entry.response;
    if (payload.patch) {
      state.patch = payload.patch;
      entry.patch = payload.patch;
      entry.status = "needs-apply";
      entry.response = payload.response || entry.response;
      if (payload.content) {
        await applyDocUpdateFromChat(kind, payload.content);
      }
    }
    renderChat(kind);
    return;
  }
  if (event === "error") {
    const message =
      (parsed && parsed.detail) ||
      (parsed && parsed.error) ||
      rawData ||
      "Doc chat failed";
    markChatError(state, entry, message);
    throw new Error(message);
  }
  if (event === "done" || event === "finish") {
    entry.status = "done";
    return;
  }
}

function applyChatResult(payload, state, entry) {
  const parsed = parseChatPayload(payload);
  if (parsed.error) {
    markChatError(state, entry, parsed.error);
    return;
  }
  entry.status = "done";
  entry.response = parsed.response || "(no response)";
  state.streamText = entry.response;
  if (parsed.patch) {
    state.patch = parsed.patch;
    entry.patch = parsed.patch;
    entry.status = "needs-apply";
    entry.response = parsed.response || entry.response;
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
    document.getElementById("doc-status").textContent = "Loaded";
    publish("docs:loaded", docsCache);
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
  if (textarea) {
    const currentValue = textarea.value;
    const cachedValue = docsCache[activeDoc] || "";
    // Skip refresh if there are unsaved local changes
    if (currentValue !== cachedValue) {
      return;
    }
  }
  // Also skip if a chat operation is in progress
  const state = getChatState(activeDoc);
  if (state.status === "running") {
    return;
  }
  try {
    const data = await api("/api/docs");
    // Check again after fetch - user might have started editing
    if (textarea && textarea.value !== (docsCache[activeDoc] || "")) {
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
  const textarea = document.getElementById("doc-content");
  const isSnapshot = kind === "snapshot";
  
  // Handle snapshot vs regular doc display
  if (isSnapshot) {
    textarea.value = snapshotCache.content || "";
    textarea.placeholder = "(snapshot will appear here)";
    document.getElementById("doc-status").textContent = "Viewing SNAPSHOT";
  } else {
    textarea.value = docsCache[kind] || "";
    textarea.placeholder = "";
    document.getElementById("doc-status").textContent = `Editing ${kind.toUpperCase()}`;
  }
  
  // Toggle spec issue import UI
  if (specIssueUI.row) {
    specIssueUI.row.classList.toggle("hidden", kind !== "spec");
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
  updateStandardActionButtons(kind);
  
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
  
  // Update snapshot button states when switching to snapshot
  if (isSnapshot) {
    renderSnapshotButtons();
  } else {
    reloadPatch(kind, true);
    renderChat(kind);
  }
}

async function importIssueToSpec() {
  if (!specIssueUI.input || !specIssueUI.button) return;
  const issue = (specIssueUI.input.value || "").trim();
  if (!issue) {
    flash("Enter a GitHub issue number or URL", "error");
    return;
  }
  const state = getChatState("spec");
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
      response: "",
      status: "running",
      time: Date.now(),
      lastAppliedContent: null,
      patch: "",
    };
    state.history.unshift(entry);
    state.status = "running";
    state.error = "";
    state.streamText = "";
    state.patch = "";
    state.statusText = "importing issue";
    renderChat("spec");

    const res = await api("/api/github/spec/from-issue", {
      method: "POST",
      body: { issue },
    });
    applyChatResult(res, state, entry);
    if (res?.content) {
      await applyDocUpdateFromChat("spec", res.content);
    }
    if (res?.patch) {
      state.patch = res.patch;
      entry.patch = res.patch;
      entry.status = "needs-apply";
    } else {
      entry.status = "done";
    }
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
    flash("Imported issue into pending SPEC patch");
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
    renderChat("spec");
  }
}

async function saveDoc() {
  // Snapshot is read-only, no saving
  if (activeDoc === "snapshot") {
    flash("Snapshot is read-only. Use Generate to update.", "error");
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
  chatUI.input.value = `${prefix}${text}`.trim();
  autoResizeTextarea(chatUI.input);
  chatUI.input.focus();
  flash("Voice transcript added");
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

export function initDocs() {
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
  if (docActionsUI.copy) {
    docActionsUI.copy.addEventListener("click", () =>
      copyDocToClipboard(activeDoc)
    );
  }
  if (docActionsUI.paste) {
    docActionsUI.paste.addEventListener("click", pasteSpecFromClipboard);
  }
  const docContent = getDocTextarea();
  if (docContent) {
    docContent.addEventListener("input", () => {
      if (activeDoc !== "snapshot") {
        updateStandardActionButtons(activeDoc);
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
  chatUI.cancel.addEventListener("click", cancelDocChat);
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
  reloadPatch(activeDoc, true);

  // Cmd+Enter or Ctrl+Enter sends, Enter adds newline on all devices.
  // Up/Down arrows navigate prompt history when input is empty
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
      const state = getChatState(activeDoc);
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
      const state = getChatState(activeDoc);
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

  // Clear errors on input, auto-resize textarea, and reset history navigation
  chatUI.input.addEventListener("input", () => {
    const state = getChatState(activeDoc);
    if (state.error) {
      state.error = "";
      renderChat();
    }
    // Reset history navigation when user types
    historyNavIndex = -1;
    autoResizeTextarea(chatUI.input);
  });

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
  renderChat(activeDoc);
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
  try {
    const data = await api("/api/ingest-spec", {
      method: "POST",
      body: { force: needsForce },
    });
    docsCache = { ...docsCache, ...data };
    setDoc(activeDoc);
    renderTodoPreview(docsCache.todo);
    publish("docs:updated", { kind: "todo", content: docsCache.todo });
    publish("docs:updated", { kind: "progress", content: docsCache.progress });
    publish("docs:updated", { kind: "opinions", content: docsCache.opinions });
    await loadState({ notify: false });
    flash("Ingested SPEC into docs");
  } catch (err) {
    flash(err.message, "error");
  } finally {
    button.disabled = false;
    button.classList.remove("loading");
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Spec Ingestion & Doc Clearing
// ─────────────────────────────────────────────────────────────────────────────

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
