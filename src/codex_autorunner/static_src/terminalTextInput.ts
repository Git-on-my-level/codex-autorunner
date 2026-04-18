import { api, flash } from "./utils.js";
import { CONSTANTS } from "./constants.js";
import { publish } from "./bus.js";
import type { PendingTextInput, TextHook } from "./terminalTypes.js";

export const TEXT_INPUT_STORAGE_KEYS = Object.freeze({
  enabled: "codex_terminal_text_input_enabled",
  draft: "codex_terminal_text_input_draft",
  pending: "codex_terminal_text_input_pending",
});

export const TEXT_INPUT_SIZE_LIMITS = Object.freeze({
  warnBytes: 100 * 1024,
  chunkBytes: 256 * 1024,
});

export const TEXT_INPUT_HOOK_STORAGE_PREFIX = "codex_terminal_text_input_hook:";

const textEncoder = new TextEncoder();

export interface TextInputState {
  terminalSectionEl: HTMLElement | null;
  textInputToggleBtn: HTMLButtonElement | null;
  textInputPanelEl: HTMLElement | null;
  textInputTextareaEl: HTMLTextAreaElement | null;
  textInputSendBtn: HTMLButtonElement | null;
  textInputImageBtn: HTMLButtonElement | null;
  textInputImageInputEl: HTMLInputElement | null;
  textInputEnabled: boolean;
  textInputPending: PendingTextInput | null;
  textInputPendingChunks: string[] | null;
  textInputSendBtnLabel: string | null;
  textInputHintBase: string | null;
  textInputHooks: TextHook[];
  textInputSelection: { start: number | null; end: number | null };
  textInputHookInFlight: boolean;
}

export interface TextInputDeps {
  getSocket(): WebSocket | null;
  getTerm(): unknown;
  isTouchDevice(): boolean;
  markSessionActive(): void;
  getSavedSessionId(): string | null;
  connect(options: { mode: string; quiet?: boolean }): void;
  setMobileViewActive(active: boolean): void;
  scheduleResizeAfterLayout(): void;
  captureTerminalScrollState(): void;
  restoreTerminalScrollState(): void;
  scrollToBottomIfNearBottom(): void;
  updateComposerSticky(): void;
  updateJumpBottomVisibility(): void;
  getRepoStorageKey(): string;
  getCurrentSessionId(): string | null;
  getRepoLabel(): string;
  logTerminalDebug(message: string, details?: unknown): void;
}

export function createTextInputState(): TextInputState {
  return {
    terminalSectionEl: null,
    textInputToggleBtn: null,
    textInputPanelEl: null,
    textInputTextareaEl: null,
    textInputSendBtn: null,
    textInputImageBtn: null,
    textInputImageInputEl: null,
    textInputEnabled: false,
    textInputPending: null,
    textInputPendingChunks: null,
    textInputSendBtnLabel: null,
    textInputHintBase: null,
    textInputHooks: [],
    textInputSelection: { start: null, end: null },
    textInputHookInFlight: false,
  };
}

export function readBoolFromStorage(key: string, fallback: boolean): boolean {
  const raw = localStorage.getItem(key);
  if (raw === null) return fallback;
  if (raw === "1" || raw === "true") return true;
  if (raw === "0" || raw === "false") return false;
  return fallback;
}

export function writeBoolToStorage(key: string, value: boolean): void {
  localStorage.setItem(key, value ? "1" : "0");
}

export function safeFocus(el: HTMLElement | null): void {
  if (!el) return;
  try {
    el.focus({ preventScroll: true });
  } catch (_err) {
    try {
      el.focus();
    } catch (__err) {
      // ignore
    }
  }
}

export function normalizeNewlines(text: string): string {
  return (text || "").replace(/\r\n?/g, "\n");
}

export function makeTextInputId(): string {
  return (
    (window.crypto &&
      typeof window.crypto.randomUUID === "function" &&
      window.crypto.randomUUID()) ||
    `${Date.now()}-${Math.random().toString(16).slice(2)}`
  );
}

export function splitTextByBytes(text: string, maxBytes: number): { chunks: string[]; totalBytes: number } {
  const chunkLimit = Math.max(
    4,
    Number.isFinite(maxBytes) ? maxBytes : TEXT_INPUT_SIZE_LIMITS.chunkBytes
  );
  const chunks: string[] = [];
  let totalBytes = 0;
  let chunkBytes = 0;
  let chunkParts: string[] = [];

  for (let i = 0; i < text.length; ) {
    const codePoint = text.codePointAt(i) as number;
    const charLen = codePoint > 0xffff ? 2 : 1;
    const charBytes =
      codePoint <= 0x7f
        ? 1
        : codePoint <= 0x7ff
          ? 2
          : codePoint <= 0xffff
            ? 3
            : 4;

    if (chunkBytes + charBytes > chunkLimit && chunkParts.length) {
      chunks.push(chunkParts.join(""));
      chunkParts = [];
      chunkBytes = 0;
    }

    chunkParts.push(text.slice(i, i + charLen));
    chunkBytes += charBytes;
    totalBytes += charBytes;
    i += charLen;
  }

  if (chunkParts.length) {
    chunks.push(chunkParts.join(""));
  }

  return { chunks, totalBytes };
}

export function captureTextInputSelection(state: TextInputState): void {
  if (!state.textInputTextareaEl) return;
  if (document.activeElement !== state.textInputTextareaEl) return;
  const start = Number.isInteger(state.textInputTextareaEl.selectionStart)
    ? state.textInputTextareaEl.selectionStart
    : null;
  const end = Number.isInteger(state.textInputTextareaEl.selectionEnd)
    ? state.textInputTextareaEl.selectionEnd
    : null;
  if (start === null || end === null) return;
  state.textInputSelection = { start, end };
}

export function getTextInputSelection(state: TextInputState): { start: number; end: number } {
  if (!state.textInputTextareaEl) return { start: 0, end: 0 };
  const textarea = state.textInputTextareaEl;
  const value = textarea.value || "";
  const max = value.length;
  const focused = document.activeElement === textarea;
  let start = Number.isInteger(textarea.selectionStart) ? textarea.selectionStart : null;
  let end = Number.isInteger(textarea.selectionEnd) ? textarea.selectionEnd : null;

  if (!focused || start === null || end === null) {
    if (
      Number.isInteger(state.textInputSelection.start) &&
      Number.isInteger(state.textInputSelection.end)
    ) {
      start = state.textInputSelection.start;
      end = state.textInputSelection.end;
    } else {
      start = max;
      end = max;
    }
  }

  start = Math.min(Math.max(0, start ?? 0), max);
  end = Math.min(Math.max(0, end ?? 0), max);
  if (end < start) end = start;
  return { start, end };
}

export function getTextInputHookKey(
  hookId: string,
  sessionId: string | null,
  repoStorageKey: string
): string {
  const scope = sessionId
    ? `session:${sessionId}`
    : `pending:${repoStorageKey}`;
  return `${TEXT_INPUT_HOOK_STORAGE_PREFIX}${hookId}:${scope}`;
}

export function migrateTextInputHookSession(
  hookId: string,
  sessionId: string | null,
  repoStorageKey: string
): void {
  if (!sessionId) return;
  const pendingKey = `${TEXT_INPUT_HOOK_STORAGE_PREFIX}${hookId}:pending:${repoStorageKey}`;
  const sessionKey = `${TEXT_INPUT_HOOK_STORAGE_PREFIX}${hookId}:session:${sessionId}`;
  try {
    if (sessionStorage.getItem(pendingKey) === "1") {
      sessionStorage.setItem(sessionKey, "1");
      sessionStorage.removeItem(pendingKey);
    }
  } catch (_err) {
    // ignore
  }
}

export function hasTextInputHookFired(
  hookId: string,
  sessionId: string | null,
  repoStorageKey: string
): boolean {
  try {
    return sessionStorage.getItem(getTextInputHookKey(hookId, sessionId, repoStorageKey)) === "1";
  } catch (_err) {
    return false;
  }
}

export function markTextInputHookFired(
  hookId: string,
  sessionId: string | null,
  repoStorageKey: string
): void {
  try {
    sessionStorage.setItem(getTextInputHookKey(hookId, sessionId, repoStorageKey), "1");
  } catch (_err) {
    // ignore
  }
}

export function registerTextInputHook(state: TextInputState, hook: TextHook): void {
  if (!hook || typeof hook.apply !== "function") return;
  state.textInputHooks.push(hook);
}

export function applyTextInputHooks(
  state: TextInputState,
  text: string,
  manager: unknown
): string {
  let next = text;
  for (const hook of state.textInputHooks) {
    try {
      const result = hook.apply({ text: next, manager });
      if (!result) continue;
      if (typeof result === "string") {
        next = result;
        continue;
      }
      if (typeof result === "object" && result !== null) {
        const objResult = result as { text?: string; stop?: boolean };
        if (typeof objResult.text === "string") {
          next = objResult.text;
        }
        if (objResult.stop) break;
      }
    } catch (_err) {
      // ignore hook failures
    }
  }
  return next;
}

export async function applyTextInputHooksAsync(
  state: TextInputState,
  text: string,
  manager: unknown
): Promise<string> {
  let next = text;
  for (const hook of state.textInputHooks) {
    try {
      let result = hook.apply({ text: next, manager });
      if (result && typeof (result as Promise<unknown>).then === "function") {
        result = await result;
      }
      if (!result) continue;
      if (typeof result === "string") {
        next = result;
        continue;
      }
      if (typeof result === "object" && result !== null) {
        const objResult = result as { text?: string; stop?: boolean };
        if (typeof objResult.text === "string") {
          next = objResult.text;
        }
        if (objResult.stop) break;
      }
    } catch (_err) {
      // ignore hook failures
    }
  }
  return next;
}

export function updateTextInputSendUi(state: TextInputState, socket: WebSocket | null, sessionNotFound: boolean): void {
  if (!state.textInputSendBtn) return;
  const connected = Boolean(socket && socket.readyState === WebSocket.OPEN);
  const pending = Boolean(state.textInputPending);
  state.textInputSendBtn.disabled = sessionNotFound && !connected;
  const ariaDisabled = state.textInputSendBtn.disabled || !connected;
  state.textInputSendBtn.setAttribute("aria-disabled", ariaDisabled ? "true" : "false");
  state.textInputSendBtn.classList.toggle("disconnected", !connected);
  state.textInputSendBtn.classList.toggle("pending", pending);
  if (state.textInputSendBtnLabel === null) {
    state.textInputSendBtnLabel = state.textInputSendBtn.textContent || "Send";
  }
  state.textInputSendBtn.textContent = pending ? "Sending\u2026" : state.textInputSendBtnLabel;

  const hintEl = document.getElementById("terminal-text-hint");
  if (!hintEl) return;
  if (state.textInputHintBase === null) {
    state.textInputHintBase = hintEl.textContent || "";
  }
  if (pending) {
    hintEl.textContent = "Sending\u2026 Your text will stay here until confirmed.";
  } else if (sessionNotFound && !connected) {
    hintEl.textContent = "Session expired. Click New or Resume to reconnect.";
  } else {
    hintEl.textContent = state.textInputHintBase;
  }
}

export function persistTextInputDraft(state: TextInputState): void {
  if (!state.textInputTextareaEl) return;
  try {
    localStorage.setItem(TEXT_INPUT_STORAGE_KEYS.draft, state.textInputTextareaEl.value || "");
  } catch (_err) {
    // ignore
  }
}

export function restoreTextInputDraft(state: TextInputState): void {
  if (!state.textInputTextareaEl) return;
  if (state.textInputTextareaEl.value) return;
  try {
    const draft = localStorage.getItem(TEXT_INPUT_STORAGE_KEYS.draft);
    if (draft) state.textInputTextareaEl.value = draft;
  } catch (_err) {
    // ignore
  }
}

export function loadPendingTextInput(): PendingTextInput | null {
  try {
    const raw = localStorage.getItem(TEXT_INPUT_STORAGE_KEYS.pending);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Record<string, unknown> | null;
    if (!parsed || typeof parsed !== "object") return null;
    if (typeof parsed.id !== "string" || typeof parsed.payload !== "string") return null;
    if (typeof parsed.originalText !== "string") return null;
    if (parsed.sendEnter !== undefined && typeof parsed.sendEnter !== "boolean") return null;
    const pending: PendingTextInput = {
      id: parsed.id as string,
      payload: parsed.payload as string,
      originalText: parsed.originalText as string,
      sentAt: typeof parsed.sentAt === "number" ? (parsed.sentAt as number) : Date.now(),
      lastRetryAt: typeof parsed.lastRetryAt === "number" ? (parsed.lastRetryAt as number) : null,
      sendEnter: parsed.sendEnter === true,
      chunkSize:
        Number.isFinite(parsed.chunkSize) && (parsed.chunkSize as number) > 0
          ? (parsed.chunkSize as number)
          : TEXT_INPUT_SIZE_LIMITS.chunkBytes,
      chunkIndex: Number.isInteger(parsed.chunkIndex) ? (parsed.chunkIndex as number) : 0,
      chunkIds: Array.isArray(parsed.chunkIds)
        ? (parsed.chunkIds as string[]).filter((id) => typeof id === "string")
        : null,
      inFlightId: typeof parsed.inFlightId === "string" ? (parsed.inFlightId as string) : null,
      totalBytes: Number.isFinite(parsed.totalBytes) ? (parsed.totalBytes as number) : null,
    };
    if (pending.chunkIndex < 0) pending.chunkIndex = 0;
    if (pending.chunkIds && pending.chunkIds.length === 0) pending.chunkIds = null;
    return pending;
  } catch (_err) {
    return null;
  }
}

export function savePendingTextInput(pending: PendingTextInput): void {
  try {
    localStorage.setItem(TEXT_INPUT_STORAGE_KEYS.pending, JSON.stringify(pending));
  } catch (_err) {
    // ignore
  }
}

export function queuePendingTextInput(
  state: TextInputState,
  payload: string,
  originalText: string,
  options: { sendEnter?: boolean } = {}
): string {
  const sendEnter = Boolean(options.sendEnter);
  const { chunks, totalBytes } = splitTextByBytes(
    payload,
    TEXT_INPUT_SIZE_LIMITS.chunkBytes
  );
  const chunkIds = chunks.map(() => makeTextInputId());
  const id = makeTextInputId();

  state.textInputPendingChunks = chunks;
  state.textInputPending = {
    id,
    payload,
    originalText,
    sentAt: Date.now(),
    lastRetryAt: null,
    sendEnter,
    chunkIndex: 0,
    chunkIds,
    chunkSize: TEXT_INPUT_SIZE_LIMITS.chunkBytes,
    inFlightId: null,
    totalBytes,
  };
  savePendingTextInput(state.textInputPending);
  return id;
}

export function clearPendingTextInput(state: TextInputState): void {
  state.textInputPending = null;
  state.textInputPendingChunks = null;
  try {
    localStorage.removeItem(TEXT_INPUT_STORAGE_KEYS.pending);
  } catch (_err) {
    // ignore
  }
}

export function ensurePendingTextInputChunks(state: TextInputState): string[] | null {
  if (!state.textInputPending) return null;
  if (Array.isArray(state.textInputPendingChunks) && state.textInputPendingChunks.length) {
    return state.textInputPendingChunks;
  }

  const pending = state.textInputPending;
  const chunkSize =
    Number.isFinite(pending.chunkSize) && pending.chunkSize > 0
      ? pending.chunkSize
      : TEXT_INPUT_SIZE_LIMITS.chunkBytes;
  const { chunks, totalBytes } = splitTextByBytes(pending.payload || "", chunkSize);
  if (!chunks.length) {
    clearPendingTextInput(state);
    return null;
  }

  state.textInputPendingChunks = chunks;
  if (!Array.isArray(pending.chunkIds) || pending.chunkIds.length !== chunks.length) {
    pending.chunkIds = chunks.map(() => makeTextInputId());
  }
  if (!Number.isInteger(pending.chunkIndex) || pending.chunkIndex < 0) {
    pending.chunkIndex = 0;
  }
  if (pending.chunkIndex >= chunks.length) {
    pending.chunkIndex = Math.max(0, chunks.length - 1);
  }
  if (
    pending.inFlightId &&
    (!Array.isArray(pending.chunkIds) || !pending.chunkIds.includes(pending.inFlightId))
  ) {
    pending.inFlightId = null;
  }
  pending.totalBytes = totalBytes;
  savePendingTextInput(pending);
  return chunks;
}

export function sendPendingTextInputChunk(
  state: TextInputState,
  socket: WebSocket | null,
  markSessionActive: () => void
): boolean {
  if (!state.textInputPending) return false;
  if (!socket || socket.readyState !== WebSocket.OPEN) return false;

  const chunks = ensurePendingTextInputChunks(state);
  if (!chunks || !chunks.length) return false;

  const pending = state.textInputPending;
  const index = Number.isInteger(pending.chunkIndex) ? pending.chunkIndex : 0;
  if (index >= chunks.length) {
    clearPendingTextInput(state);
    return false;
  }

  const chunkId =
    pending.inFlightId ||
    (Array.isArray(pending.chunkIds) ? pending.chunkIds[index] : null) ||
    makeTextInputId();
  pending.inFlightId = chunkId;
  if (Array.isArray(pending.chunkIds)) {
    pending.chunkIds[index] = chunkId;
  } else {
    pending.chunkIds = [chunkId];
  }
  savePendingTextInput(pending);

  try {
    socket.send(
      JSON.stringify({
        type: "input",
        id: chunkId,
        data: chunks[index],
      })
    );
    markSessionActive();
    return true;
  } catch (_err) {
    return false;
  }
}

export function sendEnterForTextInput(
  socket: WebSocket | null,
  markSessionActive: () => void
): void {
  if (!socket || socket.readyState !== WebSocket.OPEN) return;
  markSessionActive();
  socket.send(textEncoder.encode("\r"));
}

export function handleTextInputAck(
  state: TextInputState,
  socket: WebSocket | null,
  markSessionActive: () => void,
  payload: { id?: string; ok?: boolean; message?: string }
): boolean {
  if (!state.textInputPending || !payload) return false;
  const ackId = payload.id;
  if (!ackId || typeof ackId !== "string") return false;

  const chunks = ensurePendingTextInputChunks(state);
  if (!chunks || !chunks.length) return false;

  const pending = state.textInputPending;
  const index = Number.isInteger(pending.chunkIndex) ? pending.chunkIndex : 0;
  const expectedId =
    pending.inFlightId ||
    (Array.isArray(pending.chunkIds) ? pending.chunkIds[index] : null);
  if (ackId !== expectedId) return false;

  if (payload.ok === false) {
    flash(payload.message || "Send failed; your text is preserved", "error");
    return true;
  }

  pending.inFlightId = null;
  pending.chunkIndex = index + 1;
  savePendingTextInput(pending);

  if (pending.chunkIndex >= chunks.length) {
    const shouldSendEnter = pending.sendEnter;
    const current = state.textInputTextareaEl?.value || "";
    if (current === pending.originalText) {
      if (state.textInputTextareaEl) {
        state.textInputTextareaEl.value = "";
        persistTextInputDraft(state);
      }
    }
    if (shouldSendEnter) {
      sendEnterForTextInput(socket, markSessionActive);
    }
    clearPendingTextInput(state);
    return true;
  }

  sendPendingTextInputChunk(state, socket, markSessionActive);
  return true;
}

export function sendText(
  text: string,
  socket: WebSocket | null,
  markSessionActive: () => void,
  options: { appendNewline?: boolean } = {}
): boolean {
  const appendNewline = Boolean(options.appendNewline);
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    flash("Connect the terminal first", "error");
    return false;
  }

  let payload = normalizeNewlines(text);
  if (!payload) return false;

  if (appendNewline && !payload.endsWith("\n")) {
    payload = `${payload}\n`;
  }

  const { chunks, totalBytes } = splitTextByBytes(
    payload,
    TEXT_INPUT_SIZE_LIMITS.chunkBytes
  );
  if (!chunks.length) return false;
  if (totalBytes > TEXT_INPUT_SIZE_LIMITS.warnBytes) {
    const chunkNote = chunks.length > 1 ? ` in ${chunks.length} chunks` : "";
    flash(
      `Large paste (${Math.round(totalBytes / 1024)}KB); sending${chunkNote} may be slow.`,
      "info"
    );
  }

  markSessionActive();
  for (const chunk of chunks) {
    socket.send(textEncoder.encode(chunk));
  }
  return true;
}

export function sendTextWithAck(
  state: TextInputState,
  text: string,
  socket: WebSocket | null,
  deps: TextInputDeps,
  options: { appendNewline?: boolean; sendEnter?: boolean; originalText?: string } = {}
): boolean {
  const appendNewline = Boolean(options.appendNewline);
  const sendEnter = Boolean(options.sendEnter);

  let payload = normalizeNewlines(text);
  if (!payload) return false;

  const originalText =
    typeof options.originalText === "string"
      ? normalizeNewlines(options.originalText)
      : payload;
  if (appendNewline && !payload.endsWith("\n")) {
    payload = `${payload}\n`;
  }

  const socketOpen = Boolean(socket && socket.readyState === WebSocket.OPEN);
  queuePendingTextInput(state, payload, originalText, { sendEnter });

  const totalBytes = state.textInputPending?.totalBytes || 0;
  const chunkCount = state.textInputPendingChunks?.length || 0;
  if (totalBytes > TEXT_INPUT_SIZE_LIMITS.warnBytes) {
    const chunkNote = chunkCount > 1 ? ` in ${chunkCount} chunks` : "";
    flash(
      `Large paste (${Math.round(totalBytes / 1024)}KB); sending${chunkNote} may be slow.`,
      "info"
    );
  }

  if (!socketOpen) {
    const savedSessionId = deps.getSavedSessionId();
    if (!socket || socket.readyState !== WebSocket.CONNECTING) {
      if (savedSessionId) {
        deps.connect({ mode: "attach", quiet: true });
      } else {
        deps.connect({ mode: "new", quiet: true });
      }
    }
    return true;
  }

  if (!sendPendingTextInputChunk(state, socket, deps.markSessionActive)) {
    flash("Send failed; your text is preserved", "error");
    updateTextInputSendUi(state, socket, false);
    return false;
  }
  return true;
}

export async function sendFromTextarea(
  state: TextInputState,
  socket: WebSocket | null,
  deps: TextInputDeps,
  manager: unknown
): Promise<void> {
  const text = state.textInputTextareaEl?.value || "";
  const normalized = normalizeNewlines(text);
  if (state.textInputPending) {
    if (normalized && normalized !== state.textInputPending.originalText) {
      clearPendingTextInput(state);
    } else {
      retryPendingTextInput(state, socket, deps);
      return;
    }
  }
  persistTextInputDraft(state);
  if (state.textInputHookInFlight) {
    flash("Send already in progress", "error");
    return;
  }
  state.textInputHookInFlight = true;
  let payload: string;
  try {
    payload = await applyTextInputHooksAsync(state, normalized, manager);
  } finally {
    state.textInputHookInFlight = false;
  }
  const needsEnter = Boolean(payload && !payload.endsWith("\n"));
  const ok = sendTextWithAck(state, payload, socket, deps, {
    appendNewline: false,
    sendEnter: needsEnter,
    originalText: normalized,
  });
  if (!ok) return;
  deps.scrollToBottomIfNearBottom();

  if (deps.isTouchDevice()) {
    requestAnimationFrame(() => {
      safeFocus(state.textInputTextareaEl);
    });
  }
}

export function retryPendingTextInput(
  state: TextInputState,
  socket: WebSocket | null,
  deps: TextInputDeps
): void {
  if (!state.textInputPending) return;
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    const savedSessionId = deps.getSavedSessionId();
    if (!socket || socket.readyState !== WebSocket.CONNECTING) {
      if (savedSessionId) {
        deps.connect({ mode: "attach", quiet: true });
      } else {
        deps.connect({ mode: "new", quiet: true });
      }
    }
    flash("Reconnecting to resend pending input\u2026", "info");
    return;
  }
  const now = Date.now();
  const lastRetryAt: number = state.textInputPending.lastRetryAt || 0;
  if (now - lastRetryAt < 1500) {
    return;
  }
  state.textInputPending.lastRetryAt = now;
  savePendingTextInput(state.textInputPending);
  if (sendPendingTextInputChunk(state, socket, deps.markSessionActive)) {
    flash("Retrying send\u2026", "info");
  } else {
    flash("Retry failed; your text is preserved", "error");
  }
}

export function setTextInputEnabled(
  state: TextInputState,
  enabled: boolean,
  isTouchDevice: boolean,
  deps: TextInputDeps,
  options: { focus?: boolean; focusTextarea?: boolean } = {}
): void {
  state.textInputEnabled = Boolean(enabled);
  writeBoolToStorage(TEXT_INPUT_STORAGE_KEYS.enabled, state.textInputEnabled);
  publish("terminal:compose", { open: state.textInputEnabled });

  const focus = options.focus !== false;
  const shouldFocusTextarea = focus && (isTouchDevice || options.focusTextarea);

  state.textInputToggleBtn?.setAttribute(
    "aria-expanded",
    state.textInputEnabled ? "true" : "false"
  );
  state.textInputPanelEl?.classList.toggle("hidden", !state.textInputEnabled);
  state.textInputPanelEl?.setAttribute(
    "aria-hidden",
    state.textInputEnabled ? "false" : "true"
  );
  state.terminalSectionEl?.classList.toggle("text-input-open", state.textInputEnabled);
  deps.updateComposerSticky();

  deps.captureTerminalScrollState();

  if (state.textInputEnabled && shouldFocusTextarea) {
    requestAnimationFrame(() => {
      safeFocus(state.textInputTextareaEl);
    });
  } else if (!isTouchDevice) {
    const term = deps.getTerm() as { focus?(): void } | null;
    term?.focus?.();
  }
}

export function insertTextIntoTextInput(
  state: TextInputState,
  text: string,
  isTouchDevice: boolean,
  deps: TextInputDeps,
  options: { replaceSelection?: boolean; separator?: string } = {}
): boolean {
  if (!text) return false;
  if (!state.textInputTextareaEl) return false;

  if (!state.textInputEnabled) {
    setTextInputEnabled(state, true, isTouchDevice, deps, { focus: true, focusTextarea: true });
  }

  const textarea = state.textInputTextareaEl;
  const value = textarea.value || "";
  const replaceSelection = options.replaceSelection !== false;
  const selection = getTextInputSelection(state);
  const insertAt = replaceSelection ? selection.start : selection.end;
  const suffix = value.slice(replaceSelection ? selection.end : insertAt);
  const prefix = value.slice(0, insertAt);

  let insert = String(text);
  if (options.separator === "newline") {
    insert = `${prefix && !prefix.endsWith("\n") ? "\n" : ""}${insert}`;
  } else if (options.separator === "space") {
    insert = `${prefix && !/\s$/.test(prefix) ? " " : ""}${insert}`;
  }

  textarea.value = `${prefix}${insert}${suffix}`;
  const cursor = prefix.length + insert.length;
  textarea.setSelectionRange(cursor, cursor);
  state.textInputSelection = { start: cursor, end: cursor };
  persistTextInputDraft(state);
  deps.updateComposerSticky();
  safeFocus(textarea);
  return true;
}

export async function uploadTerminalImage(
  state: TextInputState,
  file: File,
  deps: TextInputDeps
): Promise<void> {
  if (!file) return;
  const fileName = (file.name || "").toLowerCase();
  const looksLikeImage =
    (file.type && file.type.startsWith("image/")) ||
    /\.(png|jpe?g|gif|webp|heic|heif)$/.test(fileName);
  if (!looksLikeImage) {
    flash("That file is not an image", "error");
    return;
  }

  const formData = new FormData();
  formData.append("file", file, file.name || "image");

  if (state.textInputImageBtn) {
    state.textInputImageBtn.disabled = true;
  }

  try {
    const response = (await api(CONSTANTS.API.TERMINAL_IMAGE_ENDPOINT, {
      method: "POST",
      body: formData,
    })) as Record<string, unknown>;
    const imagePath = (response.path as string | undefined) || (response.abs_path as string | undefined);
    if (!imagePath) {
      throw new Error("Upload returned no path");
    }
    insertTextIntoTextInput(state, imagePath, deps.isTouchDevice(), deps, {
      separator: "newline",
      replaceSelection: false,
    });
    flash(`Image saved to ${imagePath}`);
  } catch (err) {
    const message = (err as Error)?.message ? String((err as Error).message) : "Image upload failed";
    flash(message, "error");
  } finally {
    if (state.textInputImageBtn) {
      state.textInputImageBtn.disabled = false;
    }
  }
}

export async function handleImageFiles(
  state: TextInputState,
  files: FileList | File[],
  deps: TextInputDeps
): Promise<void> {
  if (!files || files.length === 0) return;
  const images = Array.from(files).filter((file) => {
    if (!file) return false;
    if (file.type && file.type.startsWith("image/")) return true;
    const fileName = (file.name || "").toLowerCase();
    return /\.(png|jpe?g|gif|webp|heic|heif)$/.test(fileName);
  });
  if (!images.length) {
    flash("No image found in clipboard", "error");
    return;
  }
  for (const file of images) {
    await uploadTerminalImage(state, file, deps);
  }
}

export function updateComposerSticky(state: TextInputState, isTouchDevice: boolean): void {
  if (!state.terminalSectionEl) return;
  if (!isTouchDevice || !state.textInputEnabled || !state.textInputTextareaEl) {
    state.terminalSectionEl.classList.remove("composer-sticky");
    return;
  }
  const hasText = Boolean((state.textInputTextareaEl.value || "").trim());
  const focused = document.activeElement === state.textInputTextareaEl;
  state.terminalSectionEl.classList.toggle("composer-sticky", hasText || focused);
}
