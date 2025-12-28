import { api, flash, buildWsUrl, isMobileViewport } from "./utils.js";
import { CONSTANTS } from "./constants.js";
import { initVoiceInput } from "./voice.js";
import { publish, subscribe } from "./bus.js";
import { REPO_ID, BASE_PATH } from "./env.js";

const textEncoder = new TextEncoder();

const TEXT_INPUT_STORAGE_KEYS = Object.freeze({
  enabled: "codex_terminal_text_input_enabled",
  draft: "codex_terminal_text_input_draft",
  pending: "codex_terminal_text_input_pending",
});

const TEXT_INPUT_SIZE_LIMITS = Object.freeze({
  warnBytes: 100 * 1024,
  maxBytes: 500 * 1024,
});

const TEXT_INPUT_HOOK_STORAGE_PREFIX = "codex_terminal_text_input_hook:";

const XTERM_COLOR_MODE_DEFAULT = 0;
const XTERM_COLOR_MODE_PALETTE_16 = 0x01000000;
const XTERM_COLOR_MODE_PALETTE_256 = 0x02000000;
const XTERM_COLOR_MODE_RGB = 0x03000000;

const CAR_CONTEXT_HOOK_ID = "car_context";
const CAR_CONTEXT_KEYWORDS = [
  "car",
  "codex",
  "todo",
  "progress",
  "opinions",
  "spec",
  "summary",
  "autorunner",
  "work docs",
];

const LEGACY_SESSION_STORAGE_KEY = "codex_terminal_session_id";
const SESSION_STORAGE_PREFIX = "codex_terminal_session_id:";
const SESSION_STORAGE_TS_PREFIX = "codex_terminal_session_ts:";

const TOUCH_OVERRIDE = (() => {
  try {
    const params = new URLSearchParams(window.location.search);
    const truthy = new Set(["1", "true", "yes", "on"]);
    const falsy = new Set(["0", "false", "no", "off"]);

    const touchParam = params.get("force_touch") ?? params.get("touch");
    if (touchParam !== null) {
      const value = String(touchParam).toLowerCase();
      if (truthy.has(value)) return true;
      if (falsy.has(value)) return false;
    }

    const desktopParam = params.get("force_desktop") ?? params.get("desktop");
    if (desktopParam !== null) {
      const value = String(desktopParam).toLowerCase();
      if (truthy.has(value)) return false;
      if (falsy.has(value)) return true;
    }

    return null;
  } catch (_err) {
    return null;
  }
})();

/**
 * TerminalManager encapsulates all terminal state and logic including:
 * - xterm.js terminal instance and fit addon
 * - WebSocket connection handling with reconnection
 * - Voice input integration
 * - Text input panel
 * - Mobile controls
 */
export class TerminalManager {
  constructor() {
    // Core terminal state
    this.term = null;
    this.fitAddon = null;
    this.socket = null;
    this.inputDisposable = null;
    this.wheelScrollInstalled = false;
    this.wheelScrollRemainder = 0;

    // Connection state
    this.intentionalDisconnect = false;
    this.reconnectTimer = null;
    this.reconnectAttempts = 0;
    this.lastConnectMode = null;
    this.suppressNextNotFoundFlash = false;
    this.currentSessionId = null;
    this.statusBase = "Disconnected";
    this.terminalIdleTimeoutSeconds = null;
    this.sessionNotFound = false;

    // UI element references
    this.statusEl = null;
    this.overlayEl = null;
    this.connectBtn = null;
    this.disconnectBtn = null;
    this.resumeBtn = null;
    this.jumpBottomBtn = null;

    // Voice state
    this.voiceBtn = null;
    this.voiceStatus = null;
    this.voiceController = null;
    this.voiceKeyActive = false;
    this.mobileVoiceBtn = null;
    this.mobileVoiceController = null;

    // Resize state
    this.resizeRaf = null;

    // Text input panel state
    this.terminalSectionEl = null;
    this.textInputToggleBtn = null;
    this.textInputPanelEl = null;
    this.textInputTextareaEl = null;
    this.textInputSendBtn = null;
    this.textInputImageBtn = null;
    this.textInputImageInputEl = null;
    this.textInputEnabled = false;
    this.textInputPending = null;
    this.textInputSendBtnLabel = null;
    this.textInputHintBase = null;
    this.textInputHooks = [];
    this.textInputSelection = { start: null, end: null };

    // Mobile controls state
    this.mobileControlsEl = null;
    this.ctrlActive = false;
    this.altActive = false;
    this.baseViewportHeight = window.innerHeight;
    this.suppressNextSendClick = false;
    this.lastSendTapAt = 0;
    this.textInputWasFocused = false;
    this.deferScrollRestore = false;
    this.savedViewportY = null;
    this.savedAtBottom = null;
    this.mobileViewEl = null;
    // Mobile compose view: a read-only, scrollable mirror of the terminal buffer.
    // Purpose: when the text input is focused on touch devices, allow easy browsing
    // without fighting the on-screen keyboard or accidentally sending keystrokes to the TUI.
    this.mobileViewActive = false;
    this.mobileViewScrollTop = null;
    this.mobileViewAtBottom = true;
    this.mobileViewRaf = null;
    this.mobileViewDirty = false;
    this.mobileViewSuppressAtBottomRecalc = false;

    this.transcriptLines = [];
    this.transcriptLineCells = [];
    this.transcriptCursor = 0;
    this.transcriptMaxLines = 2000;
    this.transcriptHydrated = false;
    this.transcriptAnsiState = {
      mode: "text",
      oscEsc: false,
      csiParams: "",
      fg: null,
      bg: null,
      fgRgb: null,
      bgRgb: null,
      bold: false,
      className: "",
      style: "",
    };
    this.transcriptPersistTimer = null;
    this.transcriptDecoder = new TextDecoder();

    this._registerTextInputHook(this._buildCarContextHook());

    // Bind methods that are used as callbacks
    this._handleResize = this._handleResize.bind(this);
    this._handleVoiceHotkeyDown = this._handleVoiceHotkeyDown.bind(this);
    this._handleVoiceHotkeyUp = this._handleVoiceHotkeyUp.bind(this);
    this._scheduleResizeAfterLayout = this._scheduleResizeAfterLayout.bind(this);
  }

  /**
   * Check if device has touch capability
   */
  isTouchDevice() {
    if (TOUCH_OVERRIDE !== null) return TOUCH_OVERRIDE;
    return "ontouchstart" in window || navigator.maxTouchPoints > 0;
  }

  /**
   * Initialize the terminal manager and all sub-components
   */
  init() {
    this.statusEl = document.getElementById("terminal-status");
    this.overlayEl = document.getElementById("terminal-overlay");
    this.connectBtn = document.getElementById("terminal-connect");
    this.disconnectBtn = document.getElementById("terminal-disconnect");
    this.resumeBtn = document.getElementById("terminal-resume");
    this.jumpBottomBtn = document.getElementById("terminal-jump-bottom");

    if (!this.statusEl || !this.connectBtn || !this.disconnectBtn || !this.resumeBtn) {
      return;
    }

    this.connectBtn.addEventListener("click", () => this.connect({ mode: "new" }));
    this.resumeBtn.addEventListener("click", () => this.connect({ mode: "resume" }));
    this.disconnectBtn.addEventListener("click", () => this.disconnect());
    this.jumpBottomBtn?.addEventListener("click", () => {
      this.term?.scrollToBottom();
      this._updateJumpBottomVisibility();
      this.term?.focus();
    });
    this._updateButtons(false);
    this._setStatus("Disconnected");
    this._restoreTranscript();

    window.addEventListener("resize", this._handleResize);
    if (window.visualViewport) {
      window.visualViewport.addEventListener("resize", this._scheduleResizeAfterLayout);
      window.visualViewport.addEventListener("scroll", this._scheduleResizeAfterLayout);
    }

    // Initialize sub-components
    this._initMobileControls();
    this._initTerminalVoice();
    this._initTextInputPanel();

    subscribe("state:update", (state) => {
      if (
        state &&
        Object.prototype.hasOwnProperty.call(state, "terminal_idle_timeout_seconds")
      ) {
        this.terminalIdleTimeoutSeconds = state.terminal_idle_timeout_seconds;
      }
    });
    if (this.terminalIdleTimeoutSeconds === null) {
      this._loadTerminalIdleTimeout().catch(() => {});
    }

    // Auto-connect if session ID exists
    if (this._getSavedSessionId()) {
      this.connect({ mode: "attach" });
    }
  }

  /**
   * Set terminal status message
   */
  _setStatus(message) {
    this.statusBase = message;
    this._renderStatus();
  }

  _renderStatus() {
    if (!this.statusEl) return;
    const sessionId = this.currentSessionId;
    if (!sessionId) {
      this.statusEl.textContent = this.statusBase;
      return;
    }
    const repoLabel = this._getRepoLabel();
    const suffix = repoLabel
      ? ` (session ${sessionId} · repo ${repoLabel})`
      : ` (session ${sessionId})`;
    this.statusEl.textContent = `${this.statusBase}${suffix}`;
  }

  _getRepoLabel() {
    if (REPO_ID) return REPO_ID;
    if (BASE_PATH) return BASE_PATH;
    return "repo";
  }

  _getRepoStorageKey() {
    return REPO_ID || BASE_PATH || window.location.pathname || "default";
  }

  _getTextInputHookKey(hookId) {
    const sessionId = this.currentSessionId || this._getSavedSessionId();
    const scope = sessionId
      ? `session:${sessionId}`
      : `pending:${this._getRepoStorageKey()}`;
    return `${TEXT_INPUT_HOOK_STORAGE_PREFIX}${hookId}:${scope}`;
  }

  _migrateTextInputHookSession(hookId, sessionId) {
    if (!sessionId) return;
    const pendingKey = `${TEXT_INPUT_HOOK_STORAGE_PREFIX}${hookId}:pending:${this._getRepoStorageKey()}`;
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

  _hasTextInputHookFired(hookId) {
    try {
      return sessionStorage.getItem(this._getTextInputHookKey(hookId)) === "1";
    } catch (_err) {
      return false;
    }
  }

  _markTextInputHookFired(hookId) {
    try {
      sessionStorage.setItem(this._getTextInputHookKey(hookId), "1");
    } catch (_err) {
      // ignore
    }
  }

  _registerTextInputHook(hook) {
    if (!hook || typeof hook.apply !== "function") return;
    this.textInputHooks.push(hook);
  }

  _applyTextInputHooks(text) {
    let next = text;
    for (const hook of this.textInputHooks) {
      try {
        const result = hook.apply({ text: next, manager: this });
        if (!result) continue;
        if (typeof result === "string") {
          next = result;
          continue;
        }
        if (result && typeof result.text === "string") {
          next = result.text;
        }
        if (result && result.stop) break;
      } catch (_err) {
        // ignore hook failures
      }
    }
    return next;
  }

  _buildCarContextHook() {
    return {
      id: CAR_CONTEXT_HOOK_ID,
      apply: ({ text, manager }) => {
        if (!text || !text.trim()) return null;
        if (manager._hasTextInputHookFired(CAR_CONTEXT_HOOK_ID)) return null;

        const lowered = text.toLowerCase();
        const hit = CAR_CONTEXT_KEYWORDS.some((kw) => lowered.includes(kw));
        if (!hit) return null;
        if (lowered.includes("about_car.md")) return null;

        manager._markTextInputHookFired(CAR_CONTEXT_HOOK_ID);
        const injection =
          "Context: read .codex-autorunner/ABOUT_CAR.md for repo-specific rules.";
        const separator = text.endsWith("\n") ? "\n" : "\n\n";
        return { text: `${text}${separator}${injection}` };
      },
    };
  }

  async _loadTerminalIdleTimeout() {
    try {
      const data = await api(CONSTANTS.API.STATE_ENDPOINT);
      if (
        data &&
        Object.prototype.hasOwnProperty.call(data, "terminal_idle_timeout_seconds")
      ) {
        this.terminalIdleTimeoutSeconds = data.terminal_idle_timeout_seconds;
      }
    } catch (_err) {
      // ignore
    }
  }

  _getSessionStorageKey() {
    return `${SESSION_STORAGE_PREFIX}${this._getRepoStorageKey()}`;
  }

  _getSessionTimestampKey() {
    return `${SESSION_STORAGE_TS_PREFIX}${this._getRepoStorageKey()}`;
  }

  _getSavedSessionTimestamp() {
    const raw = localStorage.getItem(this._getSessionTimestampKey());
    if (!raw) return null;
    const parsed = Number(raw);
    if (!Number.isFinite(parsed)) return null;
    return parsed;
  }

  _setSavedSessionTimestamp(stamp) {
    if (!stamp) return;
    localStorage.setItem(this._getSessionTimestampKey(), String(stamp));
  }

  _clearSavedSessionTimestamp() {
    localStorage.removeItem(this._getSessionTimestampKey());
  }

  _isSessionStale(lastActiveAt) {
    if (lastActiveAt === null || lastActiveAt === undefined) return false;
    if (
      this.terminalIdleTimeoutSeconds === null ||
      this.terminalIdleTimeoutSeconds === undefined
    ) {
      return false;
    }
    if (typeof this.terminalIdleTimeoutSeconds !== "number") return false;
    if (this.terminalIdleTimeoutSeconds <= 0) return false;
    const maxAgeMs = this.terminalIdleTimeoutSeconds * 1000;
    return Date.now() - lastActiveAt > maxAgeMs;
  }

  _getSavedSessionId() {
    const scopedKey = this._getSessionStorageKey();
    const scoped = localStorage.getItem(scopedKey);
    if (scoped) {
      const lastActiveAt = this._getSavedSessionTimestamp();
      if (this._isSessionStale(lastActiveAt)) {
        this._clearSavedSessionId();
        this._clearSavedSessionTimestamp();
        return null;
      }
      return scoped;
    }
    const legacy = localStorage.getItem(LEGACY_SESSION_STORAGE_KEY);
    if (!legacy) return null;
    const hasScoped = Object.keys(localStorage).some((key) =>
      key.startsWith(SESSION_STORAGE_PREFIX)
    );
    if (!hasScoped) {
      localStorage.setItem(scopedKey, legacy);
      this._setSavedSessionTimestamp(Date.now());
      localStorage.removeItem(LEGACY_SESSION_STORAGE_KEY);
      return legacy;
    }
    return null;
  }

  _setSavedSessionId(sessionId) {
    if (!sessionId) return;
    localStorage.setItem(this._getSessionStorageKey(), sessionId);
    this._setSavedSessionTimestamp(Date.now());
  }

  _clearSavedSessionId() {
    localStorage.removeItem(this._getSessionStorageKey());
    this._clearSavedSessionTimestamp();
  }

  _markSessionActive() {
    this._setSavedSessionTimestamp(Date.now());
  }

  _setCurrentSessionId(sessionId) {
    this.currentSessionId = sessionId || null;
    if (this.currentSessionId) {
      this._migrateTextInputHookSession(CAR_CONTEXT_HOOK_ID, this.currentSessionId);
    }
    this._renderStatus();
  }

  /**
   * Get appropriate font size based on screen width
   */
  _getFontSize() {
    return window.innerWidth < 640 ? 10 : 13;
  }

  _updateJumpBottomVisibility() {
    if (!this.jumpBottomBtn || !this.term) return;
    const buffer = this.term.buffer?.active;
    if (!buffer) {
      this.jumpBottomBtn.classList.add("hidden");
      return;
    }
    const atBottom = buffer.viewportY >= buffer.baseY;
    this.jumpBottomBtn.classList.toggle("hidden", atBottom);
    if (this.mobileViewActive) {
      this.mobileViewAtBottom = atBottom;
    }
  }

  _captureTerminalScrollState() {
    if (!this.term) return;
    const buffer = this.term.buffer?.active;
    if (!buffer) return;
    this.savedViewportY = buffer.viewportY;
    this.savedAtBottom = buffer.viewportY >= buffer.baseY;
  }

  _restoreTerminalScrollState() {
    if (!this.term) return;
    const buffer = this.term.buffer?.active;
    if (!buffer) return;
    if (this.savedAtBottom) {
      this.term.scrollToBottom();
    } else if (Number.isInteger(this.savedViewportY)) {
      const delta = this.savedViewportY - buffer.viewportY;
      if (delta !== 0) {
        this.term.scrollLines(delta);
      }
    }
    this._updateJumpBottomVisibility();
    this.savedViewportY = null;
    this.savedAtBottom = null;
  }

  _scrollToBottomIfNearBottom() {
    if (!this.term) return;
    const buffer = this.term.buffer?.active;
    if (!buffer) return;
    const atBottom = buffer.viewportY >= buffer.baseY - 1;
    if (atBottom) {
      this.term.scrollToBottom();
      this._updateJumpBottomVisibility();
    }
  }

  _resetTranscript() {
    this.transcriptLines = [];
    this.transcriptLineCells = [];
    this.transcriptCursor = 0;
    this.transcriptHydrated = false;
    this.altScrollbackLines = [];
    this.altSnapshotPlain = null;
    this.altSnapshotHtml = null;
    this.transcriptAnsiState = {
      mode: "text",
      oscEsc: false,
      csiParams: "",
      fg: null,
      bg: null,
      fgRgb: null,
      bgRgb: null,
      bold: false,
      className: "",
      style: "",
    };
    this.transcriptDecoder = new TextDecoder();
    this._persistTranscript(true);
  }

  _transcriptStorageKey() {
    const scope = REPO_ID || BASE_PATH || "default";
    return `codex_terminal_transcript:${scope}`;
  }

  _restoreTranscript() {
    try {
      const key = this._transcriptStorageKey();
      let raw = null;
      let fromSessionStorage = false;
      try {
        raw = localStorage.getItem(key);
      } catch (_err) {
        raw = null;
      }
      if (!raw) {
        try {
          raw = sessionStorage.getItem(key);
          fromSessionStorage = Boolean(raw);
        } catch (_err) {
          raw = null;
        }
      }
      if (!raw) return;
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed?.lines)) {
        this.transcriptLines = parsed.lines
          .map((line) => this._segmentsToCells(line))
          .filter(Boolean);
      }
      if (Array.isArray(parsed?.line)) {
        this.transcriptLineCells = this._segmentsToCells(parsed.line) || [];
      }
      if (Number.isInteger(parsed?.cursor)) {
        this.transcriptCursor = Math.max(0, parsed.cursor);
      }
      if (fromSessionStorage) {
        try {
          localStorage.setItem(key, raw);
        } catch (_err) {
          // ignore storage errors
        }
      }
    } catch (_err) {
      // ignore restore errors
    }
  }

  _persistTranscript(clear = false) {
    try {
      const key = this._transcriptStorageKey();
      if (clear) {
        try {
          localStorage.removeItem(key);
        } catch (_err) {
          // ignore storage errors
        }
        try {
          sessionStorage.removeItem(key);
        } catch (_err) {
          // ignore storage errors
        }
        return;
      }
      const payload = JSON.stringify({
        lines: this.transcriptLines.map((line) => this._cellsToSegments(line)),
        line: this._cellsToSegments(this.transcriptLineCells),
        cursor: this.transcriptCursor,
      });
      try {
        localStorage.setItem(key, payload);
        return;
      } catch (_err) {
        // ignore storage errors
      }
      try {
        sessionStorage.setItem(key, payload);
      } catch (_err) {
        // ignore storage errors
      }
    } catch (_err) {
      // ignore storage errors
    }
  }

  _persistTranscriptSoon() {
    if (this.transcriptPersistTimer) return;
    this.transcriptPersistTimer = setTimeout(() => {
      this.transcriptPersistTimer = null;
      this._persistTranscript(false);
    }, 500);
  }

  _getTranscriptLines() {
    const lines = this.transcriptLines.slice();
    if (this.transcriptLineCells.length) {
      lines.push(this.transcriptLineCells);
    }
    return lines;
  }

  _bufferLineToText(line) {
    if (!line) return "";
    if (typeof line.translateToString === "function") {
      return line.translateToString(true);
    }
    if (typeof line.toString === "function") {
      return line.toString();
    }
    return "";
  }

  _getBufferSnapshot() {
    if (!this.term?.buffer?.active) return null;
    const bufferNamespace = this.term.buffer;
    const buffer = bufferNamespace.active;
    const lineCount =
      Number.isInteger(buffer.length) ? buffer.length : buffer.lines?.length;
    if (!Number.isInteger(lineCount)) return null;
    const start = Math.max(0, lineCount - this.transcriptMaxLines);
    const lines = [];
    for (let idx = start; idx < lineCount; idx++) {
      let line = null;
      if (typeof buffer.getLine === "function") {
        line = buffer.getLine(idx);
      } else if (typeof buffer.lines?.get === "function") {
        line = buffer.lines.get(idx);
      }
      lines.push(line);
    }
    const cols = Number.isInteger(buffer.cols) ? buffer.cols : this.term.cols;
    return { bufferNamespace, buffer, lines, cols };
  }

  _snapshotBufferLines(bufferSnapshot) {
    if (!bufferSnapshot) return null;
    const cols = bufferSnapshot.cols ?? this.term?.cols;
    const plain = [];
    const html = [];
    for (const line of bufferSnapshot.lines) {
      plain.push(this._bufferLineToText(line));
      html.push(this._bufferLineToHtml(line, cols));
    }
    return { plain, html };
  }

  _findLineOverlap(prevRegion, nextRegion) {
    const maxOverlap = Math.min(prevRegion.length, nextRegion.length);
    for (let overlap = maxOverlap; overlap > 0; overlap -= 1) {
      let matches = true;
      for (let idx = 0; idx < overlap; idx += 1) {
        if (prevRegion[prevRegion.length - overlap + idx] !== nextRegion[idx]) {
          matches = false;
          break;
        }
      }
      if (matches) return overlap;
    }
    return 0;
  }

  _trimAltScrollback() {
    if (!Array.isArray(this.altScrollbackLines)) return;
    const overflow = this.altScrollbackLines.length - this.transcriptMaxLines;
    if (overflow > 0) {
      this.altScrollbackLines.splice(0, overflow);
    }
  }

  _updateAltScrollback(snapshotPlain, snapshotHtml) {
    if (!Array.isArray(snapshotPlain) || !Array.isArray(snapshotHtml)) return;
    if (!Array.isArray(this.altScrollbackLines)) {
      this.altScrollbackLines = [];
    }
    if (!Array.isArray(this.altSnapshotPlain)) {
      this.altSnapshotPlain = snapshotPlain;
      this.altSnapshotHtml = snapshotHtml;
      return;
    }
    const prevPlain = this.altSnapshotPlain;
    const prevHtml = this.altSnapshotHtml || [];
    const nextPlain = snapshotPlain;

    const len = Math.min(prevPlain.length, nextPlain.length);
    let prefix = 0;
    while (prefix < len && prevPlain[prefix] === nextPlain[prefix]) {
      prefix += 1;
    }
    let suffix = 0;
    while (
      suffix < len - prefix &&
      prevPlain[prevPlain.length - 1 - suffix] ===
        nextPlain[nextPlain.length - 1 - suffix]
    ) {
      suffix += 1;
    }

    const prevStart = prefix;
    const prevEnd = prevPlain.length - suffix;
    const nextStart = prefix;
    const nextEnd = nextPlain.length - suffix;

    const prevRegion = prevPlain.slice(prevStart, prevEnd);
    const nextRegion = nextPlain.slice(nextStart, nextEnd);
    const overlap = this._findLineOverlap(prevRegion, nextRegion);
    if (overlap > 0) {
      const removedCount = prevRegion.length - overlap;
      if (removedCount > 0) {
        const removedLines = prevHtml.slice(prevStart, prevStart + removedCount);
        this.altScrollbackLines.push(...removedLines);
        this._trimAltScrollback();
      }
    }

    this.altSnapshotPlain = nextPlain;
    this.altSnapshotHtml = snapshotHtml;
  }

  _paletteIndexToCss(index) {
    if (!Number.isInteger(index) || index < 0) return null;
    if (!this._xtermPalette) {
      const theme = CONSTANTS.THEME.XTERM;
      this._xtermPalette = [
        theme.black,
        theme.red,
        theme.green,
        theme.yellow,
        theme.blue,
        theme.magenta,
        theme.cyan,
        theme.white,
        theme.brightBlack,
        theme.brightRed,
        theme.brightGreen,
        theme.brightYellow,
        theme.brightBlue,
        theme.brightMagenta,
        theme.brightCyan,
        theme.brightWhite,
      ];
    }
    if (index < this._xtermPalette.length) {
      return this._xtermPalette[index];
    }
    return this._ansi256ToRgb(index);
  }

  _rgbNumberToCss(value) {
    if (!Number.isInteger(value) || value < 0) return null;
    const r = (value >> 16) & 0xff;
    const g = (value >> 8) & 0xff;
    const b = value & 0xff;
    return `rgb(${r}, ${g}, ${b})`;
  }

  _resolveXtermColor(mode, value) {
    if (!Number.isInteger(mode) || value === -1) return null;
    if (mode === XTERM_COLOR_MODE_DEFAULT) return null;
    if (mode === XTERM_COLOR_MODE_RGB) {
      return this._rgbNumberToCss(value);
    }
    if (
      mode === XTERM_COLOR_MODE_PALETTE_16 ||
      mode === XTERM_COLOR_MODE_PALETTE_256
    ) {
      return this._paletteIndexToCss(value);
    }
    if (Number.isInteger(value)) {
      return value > 255 ? this._rgbNumberToCss(value) : this._paletteIndexToCss(value);
    }
    return null;
  }

  _getCellStyle(cell) {
    const bold = typeof cell.isBold === "function" ? cell.isBold() : false;
    const inverse =
      typeof cell.isInverse === "function" ? cell.isInverse() : false;
    const fgMode =
      typeof cell.getFgColorMode === "function" ? cell.getFgColorMode() : null;
    const bgMode =
      typeof cell.getBgColorMode === "function" ? cell.getBgColorMode() : null;
    const fgValue =
      typeof cell.getFgColor === "function" ? cell.getFgColor() : null;
    const bgValue =
      typeof cell.getBgColor === "function" ? cell.getBgColor() : null;
    let fg = this._resolveXtermColor(fgMode, fgValue);
    let bg = this._resolveXtermColor(bgMode, bgValue);
    if (inverse) {
      const theme = CONSTANTS.THEME.XTERM;
      const defaultFg = theme.foreground;
      const defaultBg = theme.background;
      const resolvedFg = fg ?? defaultFg;
      const resolvedBg = bg ?? defaultBg;
      fg = resolvedBg;
      bg = resolvedFg;
    }
    const styles = [];
    if (fg) styles.push(`color: ${fg}`);
    if (bg) styles.push(`background-color: ${bg}`);
    return {
      className: bold ? "ansi-bold" : "",
      style: styles.join("; "),
    };
  }

  _getCellWidth(cell) {
    if (typeof cell.getWidth === "function") {
      return cell.getWidth();
    }
    if (Number.isInteger(cell.width)) {
      return cell.width;
    }
    return 1;
  }

  _getCellChars(cell, width) {
    let chars = "";
    if (typeof cell.getChars === "function") {
      chars = cell.getChars();
    }
    if (!chars && typeof cell.getCode === "function") {
      const code = cell.getCode();
      if (Number.isInteger(code) && code > 0) {
        chars = String.fromCodePoint(code);
      }
    }
    if (!chars) {
      chars = " ";
    }
    if (width > 1 && chars === " ") {
      return " ".repeat(width);
    }
    return chars;
  }

  _bufferLineToHtml(line, cols) {
    if (!line) return "";
    if (typeof line.getCell !== "function") {
      return this._escapeHtml(this._bufferLineToText(line));
    }
    let html = "";
    let currentText = "";
    let currentClass = "";
    let currentStyle = "";
    const flush = () => {
      if (!currentText) return;
      const text = this._escapeHtml(currentText);
      if (!currentClass && !currentStyle) {
        html += text;
      } else if (currentClass && currentStyle) {
        html += `<span class="${currentClass}" style="${currentStyle}">${text}</span>`;
      } else if (currentClass) {
        html += `<span class="${currentClass}">${text}</span>`;
      } else {
        html += `<span style="${currentStyle}">${text}</span>`;
      }
      currentText = "";
    };
    const lineLength = Number.isInteger(line.length) ? line.length : this.term?.cols || 0;
    const maxCols = Number.isInteger(cols) ? cols : lineLength;
    for (let col = 0; col < maxCols; col++) {
      const cell = line.getCell(col);
      if (!cell) {
        if (currentClass || currentStyle) {
          flush();
          currentClass = "";
          currentStyle = "";
        }
        currentText += " ";
        continue;
      }
      const width = this._getCellWidth(cell);
      if (width === 0) {
        continue;
      }
      const isInvisible =
        typeof cell.isInvisible === "function" ? cell.isInvisible() : false;
      const { className, style } = this._getCellStyle(cell);
      const chars = isInvisible
        ? " ".repeat(Math.max(1, width))
        : this._getCellChars(cell, width);
      if (className !== currentClass || style !== currentStyle) {
        flush();
        currentClass = className;
        currentStyle = style;
      }
      currentText += chars;
    }
    flush();
    return html;
  }

  _isAltBufferActive() {
    const bufferNamespace = this.term?.buffer;
    if (!bufferNamespace?.active || !bufferNamespace?.alternate) return false;
    return bufferNamespace.active === bufferNamespace.alternate;
  }

  _pushTranscriptLine(lineCells) {
    this.transcriptLines.push(lineCells.slice());
    const overflow = this.transcriptLines.length - this.transcriptMaxLines;
    if (overflow > 0) {
      this.transcriptLines.splice(0, overflow);
    }
  }

  _cellsToSegments(cells) {
    if (!Array.isArray(cells)) return [];
    const segments = [];
    let current = null;
    for (const cell of cells) {
      if (!cell) continue;
      const cls = cell.c || "";
      const style = cell.s || "";
      if (!current || current.c !== cls || (current.s || "") !== style) {
        current = { t: cell.t || "", c: cls };
        if (style) {
          current.s = style;
        }
        segments.push(current);
      } else {
        current.t += cell.t || "";
      }
    }
    return segments;
  }

  _segmentsToCells(segments) {
    if (typeof segments === "string") {
      return Array.from(segments).map((ch) => ({ t: ch, c: "", s: "" }));
    }
    if (!Array.isArray(segments)) return null;
    const cells = [];
    for (const seg of segments) {
      if (!seg || typeof seg.t !== "string") continue;
      const cls = typeof seg.c === "string" ? seg.c : "";
      const style = typeof seg.s === "string" ? seg.s : "";
      for (const ch of seg.t) {
        cells.push({ t: ch, c: cls, s: style });
      }
    }
    return cells;
  }

  _escapeHtml(text) {
    return String(text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/\"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  _cellsToHtml(cells) {
    if (!cells.length) return "";
    const segments = this._cellsToSegments(cells);
    let html = "";
    for (const seg of segments) {
      const text = this._escapeHtml(seg.t);
      if (!seg.c && !seg.s) {
        html += text;
      } else if (seg.c && seg.s) {
        html += `<span class="${seg.c}" style="${seg.s}">${text}</span>`;
      } else if (seg.c) {
        html += `<span class="${seg.c}">${text}</span>`;
      } else {
        html += `<span style="${seg.s}">${text}</span>`;
      }
    }
    return html;
  }

  _cellsToPlainText(cells) {
    if (!Array.isArray(cells) || !cells.length) return "";
    let text = "";
    for (const cell of cells) {
      if (!cell) {
        text += " ";
        continue;
      }
      text += cell.t || " ";
    }
    return text;
  }

  _hydrateTerminalFromTranscript() {
    if (!this.term || this.transcriptHydrated) return;
    const lines = this._getTranscriptLines();
    if (!lines.length) return;
    const output = lines.map((line) => this._cellsToPlainText(line)).join("\r\n");
    if (output) {
      this.term.write(output);
      this.transcriptHydrated = true;
      this._updateJumpBottomVisibility();
    }
  }

  _ansiClassName() {
    const state = this.transcriptAnsiState;
    const parts = [];
    if (state.bold) parts.push("ansi-bold");
    if (state.fg) parts.push(`ansi-fg-${state.fg}`);
    if (state.bg) parts.push(`ansi-bg-${state.bg}`);
    return parts.join(" ");
  }

  _ansiStyle() {
    const state = this.transcriptAnsiState;
    const styles = [];
    if (state.fgRgb) styles.push(`color: ${state.fgRgb}`);
    if (state.bgRgb) styles.push(`background-color: ${state.bgRgb}`);
    return styles.join("; ");
  }

  _ansi256ToRgb(value) {
    if (!Number.isInteger(value) || value < 0 || value > 255) return null;
    if (value >= 232) {
      const shade = 8 + (value - 232) * 10;
      return `rgb(${shade}, ${shade}, ${shade})`;
    }
    if (value < 16) return null;
    const index = value - 16;
    const r = Math.floor(index / 36);
    const g = Math.floor((index % 36) / 6);
    const b = index % 6;
    const steps = [0, 95, 135, 175, 215, 255];
    return `rgb(${steps[r]}, ${steps[g]}, ${steps[b]})`;
  }

  _applyAnsiPaletteColor(isForeground, value, state) {
    if (!Number.isInteger(value)) return;
    if (value >= 0 && value <= 7) {
      if (isForeground) {
        state.fg = String(30 + value);
        state.fgRgb = null;
      } else {
        state.bg = String(40 + value);
        state.bgRgb = null;
      }
      return;
    }
    if (value >= 8 && value <= 15) {
      if (isForeground) {
        state.fg = String(90 + (value - 8));
        state.fgRgb = null;
      } else {
        state.bg = String(100 + (value - 8));
        state.bgRgb = null;
      }
      return;
    }
    const rgb = this._ansi256ToRgb(value);
    if (!rgb) return;
    if (isForeground) {
      state.fg = null;
      state.fgRgb = rgb;
    } else {
      state.bg = null;
      state.bgRgb = rgb;
    }
  }

  _appendTranscriptChunk(data) {
    if (!data) return;
    const text =
      typeof data === "string"
        ? data
        : this.transcriptDecoder.decode(data, { stream: true });
    if (!text) return;
    const state = this.transcriptAnsiState;
    let didChange = false;

    const parseParams = (raw) => {
      if (!raw) return [];
      return raw.split(";").map((part) => {
        const match = part.match(/(\d+)/);
        return match ? Number.parseInt(match[1], 10) : null;
      });
    };

    const getParam = (params, index, fallback) => {
      const value = params[index];
      return Number.isInteger(value) ? value : fallback;
    };

    const writeChar = (char) => {
      if (this.transcriptCursor > this.transcriptLineCells.length) {
        const padCount = this.transcriptCursor - this.transcriptLineCells.length;
        for (let idx = 0; idx < padCount; idx++) {
          this.transcriptLineCells.push({ t: " ", c: "" });
        }
      }
      const cell = { t: char, c: state.className };
      if (state.style) {
        cell.s = state.style;
      }
      if (this.transcriptCursor === this.transcriptLineCells.length) {
        this.transcriptLineCells.push(cell);
      } else {
        this.transcriptLineCells[this.transcriptCursor] = cell;
      }
      this.transcriptCursor += 1;
      didChange = true;
    };

    for (let i = 0; i < text.length; i++) {
      const ch = text[i];
      if (state.mode === "osc") {
        if (state.oscEsc) {
          state.oscEsc = false;
          if (ch === "\\") {
            state.mode = "text";
          }
          continue;
        }
        if (ch === "\x07") {
          state.mode = "text";
          continue;
        }
        if (ch === "\x1b") {
          state.oscEsc = true;
        }
        continue;
      }

      if (state.mode === "csi") {
        if (ch >= "@" && ch <= "~") {
          const params = parseParams(state.csiParams);
          const param = getParam(params, 0, 0);
          if (ch === "m") {
            const codes = params.length ? params : [0];
            for (let idx = 0; idx < codes.length; idx++) {
              const code = codes[idx];
              if (code === 0 || code === null) {
                state.fg = null;
                state.bg = null;
                state.fgRgb = null;
                state.bgRgb = null;
                state.bold = false;
                continue;
              }
              if (code === 1) {
                state.bold = true;
                continue;
              }
              if (code === 22) {
                state.bold = false;
                continue;
              }
              if (code === 38 || code === 48) {
                const isForeground = code === 38;
                const mode = codes[idx + 1];
                if (mode === 2) {
                  const r = codes[idx + 2];
                  const g = codes[idx + 3];
                  const b = codes[idx + 4];
                  if (
                    Number.isInteger(r) &&
                    Number.isInteger(g) &&
                    Number.isInteger(b)
                  ) {
                    const rr = Math.max(0, Math.min(255, r));
                    const gg = Math.max(0, Math.min(255, g));
                    const bb = Math.max(0, Math.min(255, b));
                    const rgb = `rgb(${rr}, ${gg}, ${bb})`;
                    if (isForeground) {
                      state.fg = null;
                      state.fgRgb = rgb;
                    } else {
                      state.bg = null;
                      state.bgRgb = rgb;
                    }
                  }
                  idx += 4;
                } else if (mode === 5) {
                  const colorIndex = codes[idx + 2];
                  this._applyAnsiPaletteColor(isForeground, colorIndex, state);
                  idx += 2;
                }
                continue;
              }
              if (code >= 30 && code <= 37) {
                state.fg = String(code);
                state.fgRgb = null;
                continue;
              }
              if (code === 39) {
                state.fg = null;
                state.fgRgb = null;
                continue;
              }
              if (code >= 40 && code <= 47) {
                state.bg = String(code);
                state.bgRgb = null;
                continue;
              }
              if (code === 49) {
                state.bg = null;
                state.bgRgb = null;
                continue;
              }
              if (code >= 90 && code <= 97) {
                state.fg = String(code);
                state.fgRgb = null;
                continue;
              }
              if (code >= 100 && code <= 107) {
                state.bg = String(code);
                state.bgRgb = null;
              }
            }
            state.className = this._ansiClassName();
            state.style = this._ansiStyle();
          } else if (ch === "K") {
            if (param === 2) {
              this.transcriptLineCells = [];
              this.transcriptCursor = 0;
            } else if (param === 1) {
              for (let idx = 0; idx < this.transcriptCursor; idx++) {
                if (this.transcriptLineCells[idx]) {
                  this.transcriptLineCells[idx].t = " ";
                } else {
                  this.transcriptLineCells[idx] = { t: " ", c: "" };
                }
              }
            } else {
              this.transcriptLineCells = this.transcriptLineCells.slice(
                0,
                this.transcriptCursor
              );
            }
            didChange = true;
          } else if (ch === "G") {
            this.transcriptCursor = Math.max(0, param - 1);
          } else if (ch === "C") {
            this.transcriptCursor = Math.max(0, this.transcriptCursor + (param || 1));
          } else if (ch === "D") {
            this.transcriptCursor = Math.max(0, this.transcriptCursor - (param || 1));
          } else if (ch === "H" || ch === "f") {
            const col = getParam(params, 1, getParam(params, 0, 1));
            this.transcriptCursor = Math.max(0, (col || 1) - 1);
          }
          state.mode = "text";
          state.csiParams = "";
        } else {
          state.csiParams += ch;
        }
        continue;
      }

      if (state.mode === "esc") {
        if (ch === "[") {
          state.mode = "csi";
          state.csiParams = "";
          continue;
        }
        if (ch === "]") {
          state.mode = "osc";
          state.oscEsc = false;
          continue;
        }
        state.mode = "text";
        continue;
      }

      if (ch === "\x1b") {
        state.mode = "esc";
        continue;
      }
      if (ch === "\x07") {
        continue;
      }
      if (ch === "\r") {
        this.transcriptCursor = 0;
        continue;
      }
      if (ch === "\n") {
        this._pushTranscriptLine(this.transcriptLineCells);
        this.transcriptLineCells = [];
        this.transcriptCursor = 0;
        didChange = true;
        continue;
      }
      if (ch === "\b") {
        if (this.transcriptCursor > 0) {
          const idx = this.transcriptCursor - 1;
          if (this.transcriptLineCells[idx]) {
            this.transcriptLineCells[idx].t = " ";
          }
          this.transcriptCursor = idx;
          didChange = true;
        }
        continue;
      }
      if (ch >= " " || ch === "\t") {
        if (ch === "\t") {
          writeChar(" ");
          writeChar(" ");
        } else {
          writeChar(ch);
        }
      }
    }

    if (didChange) {
      this._persistTranscriptSoon();
    }
  }

  _initMobileView() {
    if (this.mobileViewEl) return;
    const existing = document.getElementById("mobile-terminal-view");
    if (existing) {
      this.mobileViewEl = existing;
    } else {
      this.mobileViewEl = document.createElement("div");
      this.mobileViewEl.id = "mobile-terminal-view";
      this.mobileViewEl.className = "mobile-terminal-view hidden";
      document.body.appendChild(this.mobileViewEl);
    }

    this.mobileViewEl.addEventListener("scroll", () => {
      if (!this.mobileViewEl) return;
      this.mobileViewScrollTop = this.mobileViewEl.scrollTop;
      const threshold = 4;
      this.mobileViewAtBottom =
        this.mobileViewEl.scrollTop + this.mobileViewEl.clientHeight >=
        this.mobileViewEl.scrollHeight - threshold;
    });
  }

  _setMobileViewActive(active) {
    if (!this.isTouchDevice() || !isMobileViewport()) return;
    this._initMobileView();
    if (!this.mobileViewEl) return;
    const wasActive = this.mobileViewActive;
    this.mobileViewActive = Boolean(active);
    if (!this.mobileViewActive) {
      this.mobileViewEl.classList.add("hidden");
      return;
    }
    if (!wasActive) {
      this.mobileViewAtBottom = true;
      this.mobileViewScrollTop = null;
    } else {
      const buffer = this.term?.buffer?.active;
      if (buffer) {
        const atBottom = buffer.viewportY >= buffer.baseY;
        this.mobileViewAtBottom = atBottom;
      }
    }
    const shouldScrollToBottom = this.mobileViewAtBottom;
    this.mobileViewSuppressAtBottomRecalc = true;
    this.mobileViewEl.classList.remove("hidden");
    this._renderMobileView();
    this.mobileViewSuppressAtBottomRecalc = false;
    if (shouldScrollToBottom) {
      requestAnimationFrame(() => {
        if (!this.mobileViewEl || !this.mobileViewActive) return;
        this.mobileViewEl.scrollTop = this.mobileViewEl.scrollHeight;
      });
    }
  }

  _scheduleMobileViewRender() {
    if (!this.mobileViewActive) return;
    this.mobileViewDirty = true;
    if (this.mobileViewRaf) return;
    this.mobileViewRaf = requestAnimationFrame(() => {
      this.mobileViewRaf = null;
      if (!this.mobileViewDirty) return;
      this.mobileViewDirty = false;
      this._renderMobileView();
    });
  }

  _renderMobileView() {
    if (!this.mobileViewActive || !this.mobileViewEl || !this.term) return;
    const bufferSnapshot = this._getBufferSnapshot();
    if (!Array.isArray(bufferSnapshot?.lines)) {
      this.mobileViewEl.innerHTML = "";
      this.altScrollbackLines = [];
      this.altSnapshotPlain = null;
      this.altSnapshotHtml = null;
      return;
    }
    const useAltBuffer = this._isAltBufferActive();
    const bufferSnapshotLines = this._snapshotBufferLines(bufferSnapshot);
    if (!bufferSnapshotLines?.html) {
      this.mobileViewEl.innerHTML = "";
      return;
    }
    if (useAltBuffer) {
      this._updateAltScrollback(
        bufferSnapshotLines.plain,
        bufferSnapshotLines.html
      );
    } else {
      // Reset alternate buffer scrollback when we're showing the normal buffer.
      this.altScrollbackLines = [];
      this.altSnapshotPlain = null;
      this.altSnapshotHtml = null;
    }
    // This view mirrors the live output as plain text; it is intentionally read-only
    // and is hidden whenever the user wants to interact with the real TUI.
    if (
      !this.mobileViewEl.classList.contains("hidden") &&
      !this.mobileViewSuppressAtBottomRecalc
    ) {
      const threshold = 4;
      this.mobileViewAtBottom =
        this.mobileViewEl.scrollTop + this.mobileViewEl.clientHeight >=
        this.mobileViewEl.scrollHeight - threshold;
    }
    let content = "";
    if (useAltBuffer) {
      for (const line of this.altScrollbackLines || []) {
        content += `${line}\n`;
      }
    }
    for (const line of bufferSnapshotLines.html) {
      content += `${line}\n`;
    }
    this.mobileViewEl.innerHTML = content;
    if (this.mobileViewAtBottom) {
      this.mobileViewEl.scrollTop = this.mobileViewEl.scrollHeight;
    } else if (this.mobileViewScrollTop !== null) {
      const maxScroll =
        this.mobileViewEl.scrollHeight - this.mobileViewEl.clientHeight;
      this.mobileViewEl.scrollTop = Math.min(this.mobileViewScrollTop, maxScroll);
    }
  }

  /**
   * Ensure xterm terminal is initialized
   */
  _ensureTerminal() {
    if (!window.Terminal || !window.FitAddon) {
      this._setStatus("xterm assets missing; reload or check /static/vendor");
      flash("xterm assets missing; reload the page", "error");
      return false;
    }
    if (this.term) {
      return true;
    }
    const container = document.getElementById("terminal-container");
    if (!container) return false;

    this.term = new window.Terminal({
      convertEol: true,
      fontFamily:
        '"JetBrains Mono", "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace',
      fontSize: this._getFontSize(),
      scrollSensitivity: 1,
      fastScrollSensitivity: 5,
      cursorBlink: true,
      rows: 24,
      cols: 100,
      scrollback: this.transcriptMaxLines,
      theme: CONSTANTS.THEME.XTERM,
    });

    this.fitAddon = new window.FitAddon.FitAddon();
    this.term.loadAddon(this.fitAddon);
    this.term.open(container);
    this.term.write('Press "New" or "Resume" to launch Codex TUI...\r\n');
    this._installWheelScroll();
    this.term.onScroll(() => this._updateJumpBottomVisibility());
    this.term.onRender(() => this._scheduleMobileViewRender());
    this._updateJumpBottomVisibility();

    if (!this.inputDisposable) {
      this.inputDisposable = this.term.onData((data) => {
        if (!this.socket || this.socket.readyState !== WebSocket.OPEN) return;
        this._markSessionActive();
        this.socket.send(textEncoder.encode(data));
      });
    }
    return true;
  }

  _installWheelScroll() {
    if (this.wheelScrollInstalled || !this.term || !this.term.element) return;
    if (this.isTouchDevice()) return;

    const wheelTarget = this.term.element;
    const wheelListener = (event) => {
      if (!this.term || !event) return;
      if (event.ctrlKey) return;
      const buffer = this.term.buffer?.active;
      const mouseTracking = this.term?.modes?.mouseTrackingMode;
      // Let the TUI handle wheel events when mouse tracking is active.
      if (mouseTracking && mouseTracking !== "none") {
        return;
      }
      // Only consume wheel events when xterm has scrollback; alt screen should pass through to TUI.
      if (!buffer || buffer.baseY <= 0) {
        return;
      }

      event.preventDefault();
      event.stopImmediatePropagation();

      let deltaLines = 0;
      if (event.deltaMode === WheelEvent.DOM_DELTA_LINE) {
        deltaLines = event.deltaY;
      } else if (event.deltaMode === WheelEvent.DOM_DELTA_PAGE) {
        deltaLines = event.deltaY * this.term.rows;
      } else {
        deltaLines = event.deltaY / 40;
      }

      const options = this.term.options || {};
      if (Number.isFinite(options.scrollSensitivity)) {
        deltaLines *= options.scrollSensitivity;
      }

      // Respect xterm's fast-scroll modifier and sensitivity settings.
      const modifier = options.fastScrollModifier || "alt";
      const fastSensitivity = Number.isFinite(options.fastScrollSensitivity)
        ? options.fastScrollSensitivity
        : 5;
      const modifierActive =
        modifier !== "none" &&
        ((modifier === "alt" && event.altKey) ||
          (modifier === "ctrl" && event.ctrlKey) ||
          (modifier === "shift" && event.shiftKey) ||
          (modifier === "meta" && event.metaKey));
      if (modifierActive) {
        deltaLines *= fastSensitivity;
      }

      this.wheelScrollRemainder += deltaLines;
      const wholeLines = Math.trunc(this.wheelScrollRemainder);
      if (wholeLines !== 0) {
        this.term.scrollLines(wholeLines);
        this.wheelScrollRemainder -= wholeLines;
      }
    };

    wheelTarget.addEventListener("wheel", wheelListener, {
      passive: false,
      capture: true,
    });
    this.wheelScrollInstalled = true;
  }

  /**
   * Clean up WebSocket connection
   */
  _teardownSocket() {
    if (this.socket) {
      this.socket.onclose = null;
      this.socket.onerror = null;
      this.socket.onmessage = null;
      this.socket.onopen = null;
      try {
        this.socket.close();
      } catch (err) {
        // ignore
      }
    }
    this.socket = null;
  }

  /**
   * Update button enabled states
   */
  _updateButtons(connected) {
    if (this.connectBtn) this.connectBtn.disabled = connected;
    if (this.disconnectBtn) this.disconnectBtn.disabled = !connected;
    if (this.resumeBtn) this.resumeBtn.disabled = connected;
    this._updateTextInputConnected(connected);

    const voiceUnavailable = this.voiceBtn?.classList.contains("disabled");
    if (this.voiceBtn && !voiceUnavailable) {
      this.voiceBtn.disabled = !connected;
      this.voiceBtn.classList.toggle("voice-disconnected", !connected);
    }

    // Also update mobile voice button state
    const mobileVoiceUnavailable = this.mobileVoiceBtn?.classList.contains("disabled");
    if (this.mobileVoiceBtn && !mobileVoiceUnavailable) {
      this.mobileVoiceBtn.disabled = !connected;
      this.mobileVoiceBtn.classList.toggle("voice-disconnected", !connected);
    }

    if (this.voiceStatus && !voiceUnavailable && !connected) {
      this.voiceStatus.textContent = "Connect to use voice";
      this.voiceStatus.classList.remove("hidden");
    } else if (
      this.voiceStatus &&
      !voiceUnavailable &&
      connected &&
      this.voiceController &&
      this.voiceStatus.textContent === "Connect to use voice"
    ) {
      this.voiceStatus.textContent = "Hold to talk (Alt+V)";
      this.voiceStatus.classList.remove("hidden");
    }
  }

  /**
   * Handle terminal resize
   */
  _handleResize() {
    if (!this.fitAddon || !this.term) return;

    // Update font size based on current window width
    const newFontSize = this._getFontSize();
    if (this.term.options.fontSize !== newFontSize) {
      this.term.options.fontSize = newFontSize;
    }

    // Only send resize if connected
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
      try {
        this.fitAddon.fit();
      } catch (e) {
        // ignore fit errors when not visible
      }
      return;
    }

    this.fitAddon.fit();
    this.socket.send(
      JSON.stringify({
        type: "resize",
        cols: this.term.cols,
        rows: this.term.rows,
      })
    );
  }

  /**
   * Schedule resize after layout changes
   */
  _scheduleResizeAfterLayout() {
    if (this.resizeRaf) {
      cancelAnimationFrame(this.resizeRaf);
      this.resizeRaf = null;
    }

    // Double-rAF helps ensure layout changes have applied
    this.resizeRaf = requestAnimationFrame(() => {
      this.resizeRaf = requestAnimationFrame(() => {
        this.resizeRaf = null;
        this._updateViewportInsets();
        this._handleResize();
        if (this.deferScrollRestore) {
          this.deferScrollRestore = false;
          this._restoreTerminalScrollState();
        }
      });
    });
  }

  scheduleResizeAfterLayout() {
    this._scheduleResizeAfterLayout();
  }

  _updateViewportInsets() {
    const viewportHeight = window.innerHeight;
    if (viewportHeight > this.baseViewportHeight) {
      this.baseViewportHeight = viewportHeight;
    }
    let bottom = 0;
    if (window.visualViewport) {
      const vv = window.visualViewport;
      const referenceHeight = Math.max(this.baseViewportHeight, viewportHeight);
      bottom = Math.max(0, referenceHeight - (vv.height + vv.offsetTop));
    }
    const keyboardFallback = window.visualViewport
      ? 0
      : Math.max(0, this.baseViewportHeight - viewportHeight);
    const inset = bottom || keyboardFallback;
    document.documentElement.style.setProperty("--vv-bottom", `${inset}px`);
    this.terminalSectionEl?.style.setProperty("--vv-bottom", `${inset}px`);
  }

  _updateComposerSticky() {
    if (!this.terminalSectionEl) return;
    if (!this.isTouchDevice() || !this.textInputEnabled || !this.textInputTextareaEl) {
      this.terminalSectionEl.classList.remove("composer-sticky");
      return;
    }
    const hasText = Boolean((this.textInputTextareaEl.value || "").trim());
    const focused = document.activeElement === this.textInputTextareaEl;
    this.terminalSectionEl.classList.toggle("composer-sticky", hasText || focused);
  }

  /**
   * Connect to the terminal WebSocket
   */
  connect(options = {}) {
    const mode = (options.mode || (options.resume ? "resume" : "new")).toLowerCase();
    const isAttach = mode === "attach";
    const isResume = mode === "resume";
    const quiet = Boolean(options.quiet);

    this.sessionNotFound = false;
    if (!this._ensureTerminal()) return;
    if (this.socket && this.socket.readyState === WebSocket.OPEN) return;

    // Cancel any pending reconnect
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }

    this._teardownSocket();
    this.intentionalDisconnect = false;
    this.lastConnectMode = mode;

    if (!isAttach) {
      this._resetTranscript();
    }

    const queryParams = new URLSearchParams();
    if (mode) queryParams.append("mode", mode);

    const savedSessionId = this._getSavedSessionId();
    if (isAttach) {
      if (savedSessionId) {
        this._setCurrentSessionId(savedSessionId);
        queryParams.append("session_id", savedSessionId);
      } else {
        if (!quiet) flash("No saved terminal session to attach to", "error");
        return;
      }
    } else {
      // Starting a new PTY session should not accidentally attach to an old session
      if (savedSessionId) {
        queryParams.append("close_session_id", savedSessionId);
      }
      this._clearSavedSessionId();
      this._setCurrentSessionId(null);
    }

    const queryString = queryParams.toString();
    const wsUrl = buildWsUrl(
      CONSTANTS.API.TERMINAL_ENDPOINT,
      queryString ? `?${queryString}` : ""
    );
    this.socket = new WebSocket(wsUrl);
    this.socket.binaryType = "arraybuffer";

    this.socket.onopen = () => {
      this.reconnectAttempts = 0;
      this.overlayEl?.classList.add("hidden");
      this._markSessionActive();

      // On attach, clear the local terminal first
      if (isAttach && this.term) {
        try {
          this.term.reset();
        } catch (_err) {
          try {
            this.term.clear();
          } catch (__err) {
            // ignore
          }
        }
        this.transcriptHydrated = false;
        this._hydrateTerminalFromTranscript();
      }

      if (isAttach) this._setStatus("Connected (reattached)");
      else if (isResume) this._setStatus("Connected (codex resume)");
      else this._setStatus("Connected");

      this._updateButtons(true);
      this._updateTextInputSendUi();
      this.fitAddon.fit();
      this._handleResize();

      if (isResume) this.term?.write("\r\nLaunching codex resume...\r\n");

      if (this.textInputPending) {
        try {
          this.socket.send(
            JSON.stringify({
              type: "input",
              id: this.textInputPending.id,
              data: this.textInputPending.payload,
            })
          );
        } catch (_err) {
          // ignore
        }
      }
    };

    this.socket.onmessage = (event) => {
      this._markSessionActive();
      if (typeof event.data === "string") {
        try {
          const payload = JSON.parse(event.data);
          if (payload.type === "hello") {
            if (payload.session_id) {
              this._setSavedSessionId(payload.session_id);
              this._setCurrentSessionId(payload.session_id);
            }
            this._markSessionActive();
          } else if (payload.type === "ack") {
            const ackId = payload.id;
            if (this.textInputPending && ackId === this.textInputPending.id) {
              if (payload.ok === false) {
                flash(payload.message || "Send failed; your text is preserved", "error");
                this._updateTextInputSendUi();
              } else {
                const shouldSendEnter = this.textInputPending.sendEnter;
                const current = this.textInputTextareaEl?.value || "";
                if (current === this.textInputPending.originalText) {
                  if (this.textInputTextareaEl) {
                    this.textInputTextareaEl.value = "";
                    this._persistTextInputDraft();
                  }
                }
                if (shouldSendEnter) {
                  this._sendEnterForTextInput();
                }
                this._clearPendingTextInput();
              }
            }
          } else if (payload.type === "exit") {
            this.term?.write(
              `\r\n[session ended${
                payload.code !== null ? ` (code ${payload.code})` : ""
              }] \r\n`
            );
            this._clearSavedSessionId();
            this._clearSavedSessionTimestamp();
            this._setCurrentSessionId(null);
            this.intentionalDisconnect = true;
            this.disconnect();
          } else if (payload.type === "error") {
            if (payload.message && payload.message.includes("Session not found")) {
              this.sessionNotFound = true;
              this._clearSavedSessionId();
              this._clearSavedSessionTimestamp();
              this._setCurrentSessionId(null);
              if (this.lastConnectMode === "attach") {
                if (!this.suppressNextNotFoundFlash) {
                  flash(payload.message || "Terminal error", "error");
                }
                this.suppressNextNotFoundFlash = false;
                this.disconnect();
                return;
              }
              this._updateTextInputSendUi();
              return;
            }
            flash(payload.message || "Terminal error", "error");
          }
        } catch (err) {
          // ignore bad payloads
        }
        return;
      }
      if (this.term) {
        const chunk = new Uint8Array(event.data);
        this._appendTranscriptChunk(chunk);
        this._scheduleMobileViewRender();
        this.term.write(chunk);
      }
    };

    this.socket.onerror = () => {
      this._setStatus("Connection error");
    };

    this.socket.onclose = () => {
      this._updateButtons(false);
      this._updateTextInputSendUi();

      if (this.intentionalDisconnect) {
        this._setStatus("Disconnected");
        this.overlayEl?.classList.remove("hidden");
        return;
      }

      if (this.textInputPending) {
        flash("Send not confirmed; your text is preserved and will retry on reconnect", "info");
      }

      // Auto-reconnect logic
      const savedId = this._getSavedSessionId();
      if (!savedId) {
        this._setStatus("Disconnected");
        this.overlayEl?.classList.remove("hidden");
        return;
      }

      if (this.reconnectAttempts < 3) {
        const delay = Math.min(1000 * Math.pow(2, this.reconnectAttempts), 8000);
        this._setStatus(`Reconnecting in ${Math.round(delay / 100)}s...`);
        this.reconnectAttempts++;
        this.reconnectTimer = setTimeout(() => {
          this.suppressNextNotFoundFlash = true;
          this.connect({ mode: "attach", quiet: true });
        }, delay);
      } else {
        this._setStatus("Disconnected (max retries reached)");
        this.overlayEl?.classList.remove("hidden");
        flash("Terminal connection lost", "error");
      }
    };
  }

  /**
   * Disconnect from terminal
   */
  disconnect() {
    this.intentionalDisconnect = true;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this._teardownSocket();
    this._setStatus("Disconnected");
    this.overlayEl?.classList.remove("hidden");
    this._updateButtons(false);

    if (this.voiceKeyActive) {
      this.voiceKeyActive = false;
      this.voiceController?.stop();
    }
  }

  // ==================== TEXT INPUT PANEL ====================

  _readBoolFromStorage(key, fallback) {
    const raw = localStorage.getItem(key);
    if (raw === null) return fallback;
    if (raw === "1" || raw === "true") return true;
    if (raw === "0" || raw === "false") return false;
    return fallback;
  }

  _writeBoolToStorage(key, value) {
    localStorage.setItem(key, value ? "1" : "0");
  }

  _safeFocus(el) {
    if (!el) return;
    try {
      el.focus({ preventScroll: true });
    } catch (err) {
      try {
        el.focus();
      } catch (_err) {
        // ignore
      }
    }
  }

  _captureTextInputSelection() {
    if (!this.textInputTextareaEl) return;
    if (document.activeElement !== this.textInputTextareaEl) return;
    const start = Number.isInteger(this.textInputTextareaEl.selectionStart)
      ? this.textInputTextareaEl.selectionStart
      : null;
    const end = Number.isInteger(this.textInputTextareaEl.selectionEnd)
      ? this.textInputTextareaEl.selectionEnd
      : null;
    if (start === null || end === null) return;
    this.textInputSelection = { start, end };
  }

  _getTextInputSelection() {
    if (!this.textInputTextareaEl) return { start: 0, end: 0 };
    const textarea = this.textInputTextareaEl;
    const value = textarea.value || "";
    const max = value.length;
    const focused = document.activeElement === textarea;
    let start = Number.isInteger(textarea.selectionStart) ? textarea.selectionStart : null;
    let end = Number.isInteger(textarea.selectionEnd) ? textarea.selectionEnd : null;

    if (!focused || start === null || end === null) {
      if (
        Number.isInteger(this.textInputSelection.start) &&
        Number.isInteger(this.textInputSelection.end)
      ) {
        start = this.textInputSelection.start;
        end = this.textInputSelection.end;
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

  _normalizeNewlines(text) {
    return (text || "").replace(/\r\n?/g, "\n");
  }

  _updateTextInputSendUi() {
    if (!this.textInputSendBtn) return;
    const connected = Boolean(this.socket && this.socket.readyState === WebSocket.OPEN);
    const pending = Boolean(this.textInputPending);
    this.textInputSendBtn.disabled = this.sessionNotFound && !connected;
    const ariaDisabled = this.textInputSendBtn.disabled || !connected;
    this.textInputSendBtn.setAttribute("aria-disabled", ariaDisabled ? "true" : "false");
    this.textInputSendBtn.classList.toggle("disconnected", !connected);
    this.textInputSendBtn.classList.toggle("pending", pending);
    if (this.textInputSendBtnLabel === null) {
      this.textInputSendBtnLabel = this.textInputSendBtn.textContent || "Send";
    }
    this.textInputSendBtn.textContent = pending ? "Sending…" : this.textInputSendBtnLabel;

    const hintEl = document.getElementById("terminal-text-hint");
    if (!hintEl) return;
    if (this.textInputHintBase === null) {
      this.textInputHintBase = hintEl.textContent || "";
    }
    if (pending) {
      hintEl.textContent = "Sending… Your text will stay here until confirmed.";
    } else if (this.sessionNotFound && !connected) {
      hintEl.textContent = "Session expired. Click New or Resume to reconnect.";
    } else {
      hintEl.textContent = this.textInputHintBase;
    }
  }

  _persistTextInputDraft() {
    if (!this.textInputTextareaEl) return;
    try {
      localStorage.setItem(TEXT_INPUT_STORAGE_KEYS.draft, this.textInputTextareaEl.value || "");
    } catch (_err) {
      // ignore
    }
  }

  _restoreTextInputDraft() {
    if (!this.textInputTextareaEl) return;
    if (this.textInputTextareaEl.value) return;
    try {
      const draft = localStorage.getItem(TEXT_INPUT_STORAGE_KEYS.draft);
      if (draft) this.textInputTextareaEl.value = draft;
    } catch (_err) {
      // ignore
    }
  }

  _loadPendingTextInput() {
    try {
      const raw = localStorage.getItem(TEXT_INPUT_STORAGE_KEYS.pending);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== "object") return null;
      if (typeof parsed.id !== "string" || typeof parsed.payload !== "string") return null;
      if (typeof parsed.originalText !== "string") return null;
      if (parsed.sendEnter !== undefined && typeof parsed.sendEnter !== "boolean") return null;
      return parsed;
    } catch (_err) {
      return null;
    }
  }

  _savePendingTextInput(pending) {
    try {
      localStorage.setItem(TEXT_INPUT_STORAGE_KEYS.pending, JSON.stringify(pending));
    } catch (_err) {
      // ignore
    }
  }

  _queuePendingTextInput(payload, originalText, options = {}) {
    const sendEnter = Boolean(options.sendEnter);
    const id =
      (window.crypto && typeof window.crypto.randomUUID === "function" && window.crypto.randomUUID()) ||
      `${Date.now()}-${Math.random().toString(16).slice(2)}`;

    this.textInputPending = {
      id,
      payload,
      originalText,
      sentAt: Date.now(),
      lastRetryAt: null,
      sendEnter,
    };
    this._savePendingTextInput(this.textInputPending);
    this._updateTextInputSendUi();
    return id;
  }

  _clearPendingTextInput() {
    this.textInputPending = null;
    try {
      localStorage.removeItem(TEXT_INPUT_STORAGE_KEYS.pending);
    } catch (_err) {
      // ignore
    }
    this._updateTextInputSendUi();
  }

  _sendText(text, options = {}) {
    const appendNewline = Boolean(options.appendNewline);
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
      flash("Connect the terminal first", "error");
      return false;
    }

    let payload = this._normalizeNewlines(text);
    if (!payload) return false;

    if (appendNewline && !payload.endsWith("\n")) {
      payload = `${payload}\n`;
    }

    const encoded = textEncoder.encode(payload);
    if (encoded.byteLength > TEXT_INPUT_SIZE_LIMITS.maxBytes) {
      flash(
        `Text is too large to send (${Math.round(encoded.byteLength / 1024)}KB).`,
        "error"
      );
      return false;
    }
    if (encoded.byteLength > TEXT_INPUT_SIZE_LIMITS.warnBytes) {
      flash(
        `Large paste (${Math.round(encoded.byteLength / 1024)}KB); sending may be slow.`,
        "info"
      );
    }

    this._markSessionActive();
    this.socket.send(encoded);
    return true;
  }

  _sendEnterForTextInput() {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) return;
    this._markSessionActive();
    this.socket.send(textEncoder.encode("\r"));
  }

  _sendTextWithAck(text, options = {}) {
    const appendNewline = Boolean(options.appendNewline);
    const sendEnter = Boolean(options.sendEnter);

    let payload = this._normalizeNewlines(text);
    if (!payload) return false;

    const originalText =
      typeof options.originalText === "string"
        ? this._normalizeNewlines(options.originalText)
        : payload;
    if (appendNewline && !payload.endsWith("\n")) {
      payload = `${payload}\n`;
    }

    const encoded = textEncoder.encode(payload);
    if (encoded.byteLength > TEXT_INPUT_SIZE_LIMITS.maxBytes) {
      flash(
        `Text is too large to send (${Math.round(encoded.byteLength / 1024)}KB).`,
        "error"
      );
      return false;
    }
    if (encoded.byteLength > TEXT_INPUT_SIZE_LIMITS.warnBytes) {
      flash(
        `Large paste (${Math.round(encoded.byteLength / 1024)}KB); sending may be slow.`,
        "info"
      );
    }

    const socketOpen = Boolean(this.socket && this.socket.readyState === WebSocket.OPEN);
    if (!socketOpen) {
      const savedSessionId = this._getSavedSessionId();
      this._queuePendingTextInput(payload, originalText, { sendEnter });
      if (!this.socket || this.socket.readyState !== WebSocket.CONNECTING) {
        if (savedSessionId) {
          this.connect({ mode: "attach", quiet: true });
        } else {
          this.connect({ mode: "new", quiet: true });
        }
      }
      return true;
    }

    const id = this._queuePendingTextInput(payload, originalText, { sendEnter });

    try {
      this.socket.send(
        JSON.stringify({
          type: "input",
          id,
          data: payload,
        })
      );
      this._markSessionActive();
      return true;
    } catch (_err) {
      flash("Send failed; your text is preserved", "error");
      this._updateTextInputSendUi();
      return false;
    }
  }

  _retryPendingTextInput() {
    if (!this.textInputPending) return;
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
      const savedSessionId = this._getSavedSessionId();
      if (!this.socket || this.socket.readyState !== WebSocket.CONNECTING) {
        if (savedSessionId) {
          this.connect({ mode: "attach", quiet: true });
        } else {
          this.connect({ mode: "new", quiet: true });
        }
      }
      flash("Reconnecting to resend pending input…", "info");
      return;
    }
    const now = Date.now();
    const lastRetryAt = this.textInputPending.lastRetryAt || 0;
    if (now - lastRetryAt < 1500) {
      return;
    }
    this.textInputPending.lastRetryAt = now;
    this._savePendingTextInput(this.textInputPending);
    try {
      this.socket.send(
        JSON.stringify({
          type: "input",
          id: this.textInputPending.id,
          data: this.textInputPending.payload,
        })
      );
      flash("Retrying send…", "info");
    } catch (_err) {
      flash("Retry failed; your text is preserved", "error");
    }
  }

  _setTextInputEnabled(enabled, options = {}) {
    this.textInputEnabled = Boolean(enabled);
    this._writeBoolToStorage(TEXT_INPUT_STORAGE_KEYS.enabled, this.textInputEnabled);
    publish("terminal:compose", { open: this.textInputEnabled });

    const focus = options.focus !== false;
    const shouldFocusTextarea = focus && (this.isTouchDevice() || options.focusTextarea);

    this.textInputToggleBtn?.setAttribute(
      "aria-expanded",
      this.textInputEnabled ? "true" : "false"
    );
    this.textInputPanelEl?.classList.toggle("hidden", !this.textInputEnabled);
    this.textInputPanelEl?.setAttribute(
      "aria-hidden",
      this.textInputEnabled ? "false" : "true"
    );
    this.terminalSectionEl?.classList.toggle("text-input-open", this.textInputEnabled);
    this._updateComposerSticky();

    // The panel changes the terminal container height via CSS; refit xterm
    this._captureTerminalScrollState();
    this.deferScrollRestore = true;
    this._scheduleResizeAfterLayout();

    if (this.textInputEnabled && shouldFocusTextarea) {
      requestAnimationFrame(() => {
        this._safeFocus(this.textInputTextareaEl);
      });
    } else if (!this.isTouchDevice()) {
      this.term?.focus();
    }
  }

  _updateTextInputConnected(connected) {
    if (this.textInputTextareaEl) this.textInputTextareaEl.disabled = false;
    this._updateTextInputSendUi();
  }

  _sendFromTextarea() {
    const text = this.textInputTextareaEl?.value || "";
    const normalized = this._normalizeNewlines(text);
    if (this.textInputPending) {
      if (normalized && normalized !== this.textInputPending.originalText) {
        // New draft should be sendable even if a previous payload is pending.
        this._clearPendingTextInput();
      } else {
        this._retryPendingTextInput();
        return;
      }
    }
    this._persistTextInputDraft();
    const payload = this._applyTextInputHooks(normalized);
    const needsEnter = Boolean(payload && !payload.endsWith("\n"));
    const ok = this._sendTextWithAck(payload, {
      appendNewline: false,
      sendEnter: needsEnter,
      originalText: normalized,
    });
    if (!ok) return;
    this._scrollToBottomIfNearBottom();

    if (this.isTouchDevice()) {
      requestAnimationFrame(() => {
        this._safeFocus(this.textInputTextareaEl);
      });
    } else {
      this.term?.focus();
    }
  }

  _insertTextIntoTextInput(text, options = {}) {
    if (!text) return false;
    if (!this.textInputTextareaEl) return false;

    if (!this.textInputEnabled) {
      this._setTextInputEnabled(true, { focus: true, focusTextarea: true });
    }

    const textarea = this.textInputTextareaEl;
    const value = textarea.value || "";
    const replaceSelection = options.replaceSelection !== false;
    const selection = this._getTextInputSelection();
    const insertAt = replaceSelection ? selection.start : selection.end;
    const prefix = value.slice(0, insertAt);
    const suffix = value.slice(replaceSelection ? selection.end : insertAt);

    let insert = String(text);
    if (options.separator === "newline") {
      insert = `${prefix && !prefix.endsWith("\n") ? "\n" : ""}${insert}`;
    } else if (options.separator === "space") {
      insert = `${prefix && !/\s$/.test(prefix) ? " " : ""}${insert}`;
    }

    textarea.value = `${prefix}${insert}${suffix}`;
    const cursor = prefix.length + insert.length;
    textarea.setSelectionRange(cursor, cursor);
    this.textInputSelection = { start: cursor, end: cursor };
    this._persistTextInputDraft();
    this._updateComposerSticky();
    this._safeFocus(textarea);
    return true;
  }

  async _uploadTerminalImage(file) {
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

    if (this.textInputImageBtn) {
      this.textInputImageBtn.disabled = true;
    }

    try {
      const response = await api(CONSTANTS.API.TERMINAL_IMAGE_ENDPOINT, {
        method: "POST",
        body: formData,
      });
      const imagePath = response?.abs_path || response?.path;
      if (!imagePath) {
        throw new Error("Upload returned no path");
      }
      this._insertTextIntoTextInput(imagePath, {
        separator: "newline",
        replaceSelection: false,
      });
      flash(`Image saved to ${imagePath}`);
    } catch (err) {
      const message = err?.message ? String(err.message) : "Image upload failed";
      flash(message, "error");
    } finally {
      if (this.textInputImageBtn) {
        this.textInputImageBtn.disabled = false;
      }
    }
  }

  async _handleImageFiles(files) {
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
      await this._uploadTerminalImage(file);
    }
  }

  _initTextInputPanel() {
    this.terminalSectionEl = document.getElementById("terminal");
    this.textInputToggleBtn = document.getElementById("terminal-text-input-toggle");
    this.textInputPanelEl = document.getElementById("terminal-text-input");
    this.textInputTextareaEl = document.getElementById("terminal-textarea");
    this.textInputSendBtn = document.getElementById("terminal-text-send");
    this.textInputImageBtn = document.getElementById("terminal-text-image");
    this.textInputImageInputEl = document.getElementById("terminal-text-image-input");

    if (this.textInputSendBtn) {
      console.log("TerminalManager: initialized send button");
    }

    if (
      !this.terminalSectionEl ||
      !this.textInputToggleBtn ||
      !this.textInputPanelEl ||
      !this.textInputTextareaEl ||
      !this.textInputSendBtn
    ) {
      return;
    }

    this.textInputEnabled = this._readBoolFromStorage(
      TEXT_INPUT_STORAGE_KEYS.enabled,
      this.isTouchDevice()
    );

    this.textInputToggleBtn.addEventListener("click", () => {
      this._setTextInputEnabled(!this.textInputEnabled, { focus: true, focusTextarea: true });
    });

    const triggerSend = () => {
      if (this.textInputSendBtn?.disabled) {
        flash("Connect the terminal first", "error");
        return;
      }
      const now = Date.now();
      // Debounce to prevent double-firing from touch+click or rapid taps
      if (now - this.lastSendTapAt < 300) return;
      this.lastSendTapAt = now;
      console.log("TerminalManager: sending text input");
      this._sendFromTextarea();
    };
    this.textInputSendBtn.addEventListener("pointerup", (e) => {
      if (e.pointerType !== "touch") return;
      if (e.cancelable) e.preventDefault();
      this.suppressNextSendClick = true;
      triggerSend();
    });
    this.textInputSendBtn.addEventListener("touchend", (e) => {
      if (e.cancelable) e.preventDefault();
      this.suppressNextSendClick = true;
      triggerSend();
    });
    this.textInputSendBtn.addEventListener("click", () => {
      if (this.suppressNextSendClick) {
        this.suppressNextSendClick = false;
        return;
      }
      triggerSend();
    });

    this.textInputTextareaEl.addEventListener("input", () => {
      this._persistTextInputDraft();
      this._updateComposerSticky();
      this._captureTextInputSelection();
    });

    this.textInputTextareaEl.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" || e.isComposing) return;
      const sendOnEnter = this.isTouchDevice() && isMobileViewport();
      const shouldSend = sendOnEnter ? !e.shiftKey : e.metaKey || e.ctrlKey;
      if (shouldSend) {
        e.preventDefault();
        triggerSend();
      }
    });

    const captureSelection = () => this._captureTextInputSelection();
    this.textInputTextareaEl.addEventListener("select", captureSelection);
    this.textInputTextareaEl.addEventListener("keyup", captureSelection);
    this.textInputTextareaEl.addEventListener("mouseup", captureSelection);
    this.textInputTextareaEl.addEventListener("touchend", captureSelection);

    if (this.textInputImageBtn && this.textInputImageInputEl) {
      this.textInputTextareaEl.addEventListener("paste", (e) => {
        const items = e.clipboardData?.items;
        if (!items || !items.length) return;
        const files = [];
        for (const item of items) {
          if (item.type && item.type.startsWith("image/")) {
            const file = item.getAsFile();
            if (file) files.push(file);
          }
        }
        if (!files.length) return;
        e.preventDefault();
        this._handleImageFiles(files);
      });

      this.textInputImageBtn.addEventListener("click", () => {
        this._captureTextInputSelection();
        this.textInputImageInputEl?.click();
      });

      this.textInputImageInputEl.addEventListener("change", () => {
        const files = Array.from(this.textInputImageInputEl?.files || []);
        if (!files.length) return;
        this._handleImageFiles(files);
        this.textInputImageInputEl.value = "";
      });
    }

    this.textInputTextareaEl.addEventListener("focus", () => {
      this.textInputWasFocused = true;
      this._updateComposerSticky();
      this._updateViewportInsets();
      this._captureTextInputSelection();
      this._captureTerminalScrollState();
      this.deferScrollRestore = true;
      if (this.isTouchDevice() && isMobileViewport()) {
        // Enter the mobile scroll-only view when composing; keep the real TUI visible
        // only when the user is not focused on the text input.
        this._scheduleResizeAfterLayout();
        this._setMobileViewActive(true);
        if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
          const savedSessionId = this._getSavedSessionId();
          if (savedSessionId) {
            this.connect({ mode: "attach", quiet: true });
          } else {
            this.connect({ mode: "new", quiet: true });
          }
        }
      }
    });

    this.textInputTextareaEl.addEventListener("blur", () => {
      // Wait a tick so activeElement updates.
      setTimeout(() => {
        if (document.activeElement !== this.textInputTextareaEl) {
          this.textInputWasFocused = false;
        }
        this._updateComposerSticky();
        this._captureTerminalScrollState();
        this.deferScrollRestore = true;
        if (this.isTouchDevice() && isMobileViewport()) {
          // Exit the scroll-only view so taps go directly to the TUI again.
          this._scheduleResizeAfterLayout();
          this._setMobileViewActive(false);
        }
      }, 0);
    });

    if (this.textInputImageBtn && this.textInputImageInputEl) {
      this.terminalSectionEl.addEventListener("paste", (e) => {
        if (document.activeElement === this.textInputTextareaEl) return;
        const items = e.clipboardData?.items;
        if (!items || !items.length) return;
        const files = [];
        for (const item of items) {
          if (item.type && item.type.startsWith("image/")) {
            const file = item.getAsFile();
            if (file) files.push(file);
          }
        }
        if (!files.length) return;
        e.preventDefault();
        this._handleImageFiles(files);
      });
    }

    this.textInputPending = this._loadPendingTextInput();
    this._restoreTextInputDraft();
    if (this.textInputPending && this.textInputTextareaEl && !this.textInputTextareaEl.value) {
      this.textInputTextareaEl.value = this.textInputPending.originalText || "";
    }

    this._setTextInputEnabled(this.textInputEnabled, { focus: false });
    this._updateViewportInsets();
    this._updateComposerSticky();
    this._updateTextInputConnected(
      Boolean(this.socket && this.socket.readyState === WebSocket.OPEN)
    );

    if (this.textInputPending) {
      const savedSessionId = this._getSavedSessionId();
      if (savedSessionId && (!this.socket || this.socket.readyState !== WebSocket.OPEN)) {
        this.connect({ mode: "attach", quiet: true });
      }
    }
  }

  // ==================== MOBILE CONTROLS ====================

  _sendKey(seq) {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) return;

    // If ctrl modifier is active, convert to ctrl code
    if (this.ctrlActive && seq.length === 1) {
      const char = seq.toUpperCase();
      const code = char.charCodeAt(0) - 64;
      if (code >= 1 && code <= 26) {
        seq = String.fromCharCode(code);
      }
    }

    this._markSessionActive();
    this.socket.send(textEncoder.encode(seq));

    // Reset modifiers after sending
    this.ctrlActive = false;
    this.altActive = false;
    this._updateModifierButtons();
  }

  _sendCtrl(char) {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) return;
    const code = char.toUpperCase().charCodeAt(0) - 64;
    this._markSessionActive();
    this.socket.send(textEncoder.encode(String.fromCharCode(code)));
  }

  _updateModifierButtons() {
    const ctrlBtn = document.getElementById("tmb-ctrl");
    const altBtn = document.getElementById("tmb-alt");
    if (ctrlBtn) ctrlBtn.classList.toggle("active", this.ctrlActive);
    if (altBtn) altBtn.classList.toggle("active", this.altActive);
  }

  _initMobileControls() {
    this.mobileControlsEl = document.getElementById("terminal-mobile-controls");

    if (!this.mobileControlsEl) return;

    // Only show on touch devices
    if (!this.isTouchDevice()) {
      this.mobileControlsEl.style.display = "none";
      return;
    }

    // Handle all key buttons
    this.mobileControlsEl.addEventListener("click", (e) => {
      const btn = e.target.closest(".tmb-key");
      if (!btn) return;

      e.preventDefault();

      // Handle modifier toggles
      const modKey = btn.dataset.key;
      if (modKey === "ctrl") {
        this.ctrlActive = !this.ctrlActive;
        this._updateModifierButtons();
        return;
      }
      if (modKey === "alt") {
        this.altActive = !this.altActive;
        this._updateModifierButtons();
        return;
      }

      // Handle Ctrl+X combos
      const ctrlChar = btn.dataset.ctrl;
      if (ctrlChar) {
        this._sendCtrl(ctrlChar);
        if (this.isTouchDevice() && this.textInputEnabled && this.textInputWasFocused) {
          setTimeout(() => this._safeFocus(this.textInputTextareaEl), 0);
        }
        return;
      }

      // Handle direct sequences (arrows, esc, tab)
      const seq = btn.dataset.seq;
      if (seq) {
        this._sendKey(seq);
        if (this.isTouchDevice() && this.textInputEnabled && this.textInputWasFocused) {
          setTimeout(() => this._safeFocus(this.textInputTextareaEl), 0);
        }
        return;
      }
    });

    // Add haptic feedback on touch if available
    this.mobileControlsEl.addEventListener(
      "touchstart",
      (e) => {
        if (e.target.closest(".tmb-key") && navigator.vibrate) {
          navigator.vibrate(10);
        }
      },
      { passive: true }
    );
  }

  // ==================== VOICE INPUT ====================

  _insertTranscriptIntoTextInput(text) {
    if (!text) return false;
    if (!this.textInputTextareaEl) return false;

    if (!this.textInputEnabled) {
      this._setTextInputEnabled(true, { focus: true, focusTextarea: true });
    }

    const transcript = String(text).trim();
    if (!transcript) return false;

    const existing = this.textInputTextareaEl.value || "";
    let next = existing;
    if (existing && !/\s$/.test(existing)) {
      next += " ";
    }
    next += transcript;
    this.textInputTextareaEl.value = next;
    this._persistTextInputDraft();
    this._updateComposerSticky();
    this._safeFocus(this.textInputTextareaEl);
    return true;
  }

  _sendVoiceTranscript(text) {
    if (!text) {
      flash("Voice capture returned no transcript", "error");
      return;
    }
    if (this.isTouchDevice() || this.textInputEnabled) {
      if (this._insertTranscriptIntoTextInput(text)) {
        flash("Voice transcript added to text input");
        return;
      }
    }
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
      flash("Connect the terminal before using voice input", "error");
      if (this.voiceStatus) {
        this.voiceStatus.textContent = "Connect to send voice";
        this.voiceStatus.classList.remove("hidden");
      }
      return;
    }
    const payload = text.endsWith("\n") ? text : `${text}\n`;
    this.socket.send(textEncoder.encode(payload));
    this.term?.focus();
    flash("Voice transcript sent to terminal");
  }

  _matchesVoiceHotkey(event) {
    return event.key && event.key.toLowerCase() === "v" && event.altKey;
  }

  _handleVoiceHotkeyDown(event) {
    if (!this.voiceController || this.voiceKeyActive) return;
    if (!this._matchesVoiceHotkey(event)) return;
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
      flash("Connect the terminal before using voice input", "error");
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    this.voiceKeyActive = true;
    this.voiceController.start();
  }

  _handleVoiceHotkeyUp(event) {
    if (!this.voiceKeyActive) return;
    if (event && this._matchesVoiceHotkey(event)) {
      event.preventDefault();
      event.stopPropagation();
    }
    this.voiceKeyActive = false;
    this.voiceController?.stop();
  }

  _initTerminalVoice() {
    this.voiceBtn = document.getElementById("terminal-voice");
    this.voiceStatus = document.getElementById("terminal-voice-status");
    this.mobileVoiceBtn = document.getElementById("terminal-mobile-voice");
    this.textVoiceBtn = document.getElementById("terminal-text-voice");

    // Initialize desktop toolbar voice button
    if (this.voiceBtn && this.voiceStatus) {
      initVoiceInput({
        button: this.voiceBtn,
        input: null,
        statusEl: this.voiceStatus,
        onTranscript: (text) => this._sendVoiceTranscript(text),
        onError: (msg) => {
          if (!msg) return;
          flash(msg, "error");
          this.voiceStatus.textContent = msg;
          this.voiceStatus.classList.remove("hidden");
        },
      })
        .then((controller) => {
          if (!controller) {
            this.voiceBtn.closest(".terminal-voice")?.classList.add("hidden");
            return;
          }
          this.voiceController = controller;
          if (this.voiceStatus) {
            const base = this.voiceStatus.textContent || "Hold to talk";
            this.voiceStatus.textContent = `${base} (Alt+V)`;
            this.voiceStatus.classList.remove("hidden");
          }
          window.addEventListener("keydown", this._handleVoiceHotkeyDown);
          window.addEventListener("keyup", this._handleVoiceHotkeyUp);
          window.addEventListener("blur", () => {
            if (this.voiceKeyActive) {
              this.voiceKeyActive = false;
              this.voiceController?.stop();
            }
          });
        })
        .catch((err) => {
          console.error("Voice init failed", err);
          flash("Voice capture unavailable", "error");
          this.voiceStatus.textContent = "Voice unavailable";
          this.voiceStatus.classList.remove("hidden");
        });
    }

    // Initialize mobile voice button
    if (this.mobileVoiceBtn) {
      initVoiceInput({
        button: this.mobileVoiceBtn,
        input: null,
        statusEl: null,
        onTranscript: (text) => this._sendVoiceTranscript(text),
        onError: (msg) => {
          if (!msg) return;
          flash(msg, "error");
        },
      })
        .then((controller) => {
          if (!controller) {
            this.mobileVoiceBtn.classList.add("hidden");
            return;
          }
          this.mobileVoiceController = controller;
        })
        .catch((err) => {
          console.error("Mobile voice init failed", err);
          this.mobileVoiceBtn.classList.add("hidden");
        });
    }

    // Initialize text-input voice button (compact waveform mode)
    if (this.textVoiceBtn) {
      initVoiceInput({
        button: this.textVoiceBtn,
        input: null,
        statusEl: null,
        onTranscript: (text) => this._sendVoiceTranscript(text),
        onError: (msg) => {
          if (!msg) return;
          flash(msg, "error");
        },
      })
        .then((controller) => {
          if (!controller) {
            this.textVoiceBtn.classList.add("hidden");
            return;
          }
          this.textVoiceController = controller;
        })
        .catch((err) => {
          console.error("Text voice init failed", err);
          this.textVoiceBtn.classList.add("hidden");
        });
    }
  }
}
