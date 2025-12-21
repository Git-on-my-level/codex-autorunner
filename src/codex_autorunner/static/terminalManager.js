import { api, flash, buildWsUrl, isMobileViewport } from "./utils.js";
import { CONSTANTS } from "./constants.js";
import { initVoiceInput } from "./voice.js";
import { publish } from "./bus.js";
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

const LEGACY_SESSION_STORAGE_KEY = "codex_terminal_session_id";
const SESSION_STORAGE_PREFIX = "codex_terminal_session_id:";

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

    // Connection state
    this.intentionalDisconnect = false;
    this.reconnectTimer = null;
    this.reconnectAttempts = 0;
    this.lastConnectMode = null;
    this.suppressNextNotFoundFlash = false;
    this.currentSessionId = null;
    this.statusBase = "Disconnected";

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

    window.addEventListener("resize", this._handleResize);
    if (window.visualViewport) {
      window.visualViewport.addEventListener("resize", this._scheduleResizeAfterLayout);
      window.visualViewport.addEventListener("scroll", this._scheduleResizeAfterLayout);
    }

    // Initialize sub-components
    this._initMobileControls();
    this._initTerminalVoice();
    this._initTextInputPanel();

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

  _getSessionStorageKey() {
    return `${SESSION_STORAGE_PREFIX}${this._getRepoStorageKey()}`;
  }

  _getSavedSessionId() {
    const scopedKey = this._getSessionStorageKey();
    const scoped = localStorage.getItem(scopedKey);
    if (scoped) return scoped;
    const legacy = localStorage.getItem(LEGACY_SESSION_STORAGE_KEY);
    if (!legacy) return null;
    const hasScoped = Object.keys(localStorage).some((key) =>
      key.startsWith(SESSION_STORAGE_PREFIX)
    );
    if (!hasScoped) {
      localStorage.setItem(scopedKey, legacy);
      localStorage.removeItem(LEGACY_SESSION_STORAGE_KEY);
      return legacy;
    }
    return null;
  }

  _setSavedSessionId(sessionId) {
    if (!sessionId) return;
    localStorage.setItem(this._getSessionStorageKey(), sessionId);
  }

  _clearSavedSessionId() {
    localStorage.removeItem(this._getSessionStorageKey());
  }

  _setCurrentSessionId(sessionId) {
    this.currentSessionId = sessionId || null;
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

  _initTouchTerminalScroll(container) {
    if (!this.isTouchDevice()) return;
    let tracking = false;
    let lastX = 0;
    let lastY = 0;
    let remainderPx = 0;

    const estimateCellHeight = () => {
      const internal =
        this.term?._core?._renderService?.dimensions?.actualCellHeight ??
        this.term?._core?._renderService?.dimensions?.css?.cellHeight;
      if (typeof internal === "number" && internal > 0) return internal;
      const fontSize = Number.parseFloat(getComputedStyle(container).fontSize || "12");
      return fontSize > 0 ? fontSize * 1.25 : 15;
    };

    container.addEventListener(
      "touchstart",
      (e) => {
        if (!this.term) return;
        if (e.touches.length !== 1) return;
        tracking = true;
        lastX = e.touches[0].clientX;
        lastY = e.touches[0].clientY;
        remainderPx = 0;
      },
      { passive: true }
    );

    container.addEventListener(
      "touchmove",
      (e) => {
        if (!tracking || !this.term) return;
        if (e.touches.length !== 1) return;
        const x = e.touches[0].clientX;
        const y = e.touches[0].clientY;
        const dx = x - lastX;
        const dy = y - lastY;
        lastX = x;
        lastY = y;

        if (Math.abs(dx) > Math.abs(dy)) {
          return;
        }

        e.preventDefault();
        remainderPx += dy;
        const cellHeight = estimateCellHeight();
        const lines = Math.trunc(remainderPx / cellHeight);
        if (lines !== 0) {
          remainderPx -= lines * cellHeight;
          // Finger down should scroll up (toward earlier output).
          this.term.scrollLines(-lines);
          this._updateJumpBottomVisibility();
        }
      },
      { passive: false }
    );

    container.addEventListener(
      "touchend",
      () => {
        tracking = false;
      },
      { passive: true }
    );
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
    this.mobileViewActive = Boolean(active);
    if (!this.mobileViewActive) {
      this.mobileViewEl.classList.add("hidden");
      return;
    }
    const buffer = this.term?.buffer?.active;
    if (buffer) {
      const atBottom = buffer.viewportY >= buffer.baseY;
      this.mobileViewAtBottom = atBottom;
    }
    this._renderMobileView();
    this.mobileViewEl.classList.remove("hidden");
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
    const buffer = this.term.buffer?.active;
    if (!buffer) return;
    // This view mirrors the live buffer as plain text; it is intentionally read-only
    // and is hidden whenever the user wants to interact with the real TUI.
    if (!this.mobileViewEl.classList.contains("hidden")) {
      const threshold = 4;
      this.mobileViewAtBottom =
        this.mobileViewEl.scrollTop + this.mobileViewEl.clientHeight >=
        this.mobileViewEl.scrollHeight - threshold;
    }
    const rows = this.term.rows || buffer.length || 0;
    const totalLines = Math.max(0, buffer.baseY + rows - 1);
    const maxLines = 800;
    const start = Math.max(0, totalLines - maxLines);
    let content = "";
    for (let i = start; i <= totalLines; i++) {
      const line = buffer.getLine(i);
      if (line) {
        content += line.translateToString(true) + "\n";
      }
    }
    this.mobileViewEl.textContent = content;
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
      cursorBlink: true,
      rows: 24,
      cols: 100,
      theme: CONSTANTS.THEME.XTERM,
    });

    this.fitAddon = new window.FitAddon.FitAddon();
    this.term.loadAddon(this.fitAddon);
    this.term.open(container);
    this.term.write('Press "New" or "Resume" to launch Codex TUI...\r\n');
    this.term.onScroll(() => this._updateJumpBottomVisibility());
    this.term.onRender(() => this._scheduleMobileViewRender());
    this._updateJumpBottomVisibility();
    this._initTouchTerminalScroll(container);

    if (!this.inputDisposable) {
      this.inputDisposable = this.term.onData((data) => {
        if (!this.socket || this.socket.readyState !== WebSocket.OPEN) return;
        this.socket.send(textEncoder.encode(data));
      });
    }
    return true;
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
      if (typeof event.data === "string") {
        try {
          const payload = JSON.parse(event.data);
          if (payload.type === "hello") {
            if (payload.session_id) {
              this._setSavedSessionId(payload.session_id);
              this._setCurrentSessionId(payload.session_id);
            }
          } else if (payload.type === "ack") {
            const ackId = payload.id;
            if (this.textInputPending && ackId === this.textInputPending.id) {
              if (payload.ok === false) {
                flash(payload.message || "Send failed; your text is preserved", "error");
                this._updateTextInputSendUi();
              } else {
                const current = this.textInputTextareaEl?.value || "";
                if (current === this.textInputPending.originalText) {
                  if (this.textInputTextareaEl) {
                    this.textInputTextareaEl.value = "";
                    this._persistTextInputDraft();
                  }
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
            this._setCurrentSessionId(null);
            this.intentionalDisconnect = true;
            this.disconnect();
          } else if (payload.type === "error") {
            if (payload.message && payload.message.includes("Session not found")) {
              this._clearSavedSessionId();
              this._setCurrentSessionId(null);
              if (this.lastConnectMode === "attach") {
                if (!this.suppressNextNotFoundFlash) {
                  flash(payload.message || "Terminal error", "error");
                }
                this.suppressNextNotFoundFlash = false;
                this.disconnect();
                return;
              }
            }
            flash(payload.message || "Terminal error", "error");
          }
        } catch (err) {
          // ignore bad payloads
        }
        return;
      }
      if (this.term) {
        this.term.write(new Uint8Array(event.data));
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

  _normalizeNewlines(text) {
    return (text || "").replace(/\r\n?/g, "\n");
  }

  _updateTextInputSendUi() {
    if (!this.textInputSendBtn) return;
    const connected = Boolean(this.socket && this.socket.readyState === WebSocket.OPEN);
    const pending = Boolean(this.textInputPending);
    this.textInputSendBtn.disabled = false;
    this.textInputSendBtn.setAttribute("aria-disabled", connected ? "false" : "true");
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

  _queuePendingTextInput(payload, originalText) {
    const id =
      (window.crypto && typeof window.crypto.randomUUID === "function" && window.crypto.randomUUID()) ||
      `${Date.now()}-${Math.random().toString(16).slice(2)}`;

    this.textInputPending = {
      id,
      payload,
      originalText,
      sentAt: Date.now(),
      lastRetryAt: null,
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

    this.socket.send(encoded);
    return true;
  }

  _sendTextWithAck(text, options = {}) {
    const appendNewline = Boolean(options.appendNewline);

    let payload = this._normalizeNewlines(text);
    if (!payload) return false;

    const originalText = payload;
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
      this._queuePendingTextInput(payload, originalText);
      if (!this.socket || this.socket.readyState !== WebSocket.CONNECTING) {
        if (savedSessionId) {
          this.connect({ mode: "attach", quiet: true });
        } else {
          this.connect({ mode: "new", quiet: true });
        }
      }
      return true;
    }

    const id = this._queuePendingTextInput(payload, originalText);

    try {
      this.socket.send(
        JSON.stringify({
          type: "input",
          id,
          data: payload,
        })
      );
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
    if (this.textInputPending) {
      const normalized = this._normalizeNewlines(text);
      if (normalized && normalized !== this.textInputPending.originalText) {
        // New draft should be sendable even if a previous payload is pending.
        this._clearPendingTextInput();
      } else {
        this._retryPendingTextInput();
        return;
      }
    }
    this._persistTextInputDraft();
    const ok = this._sendTextWithAck(text, { appendNewline: true });
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
    const start = Number.isInteger(textarea.selectionStart)
      ? textarea.selectionStart
      : value.length;
    const end = Number.isInteger(textarea.selectionEnd) ? textarea.selectionEnd : start;
    const prefix = value.slice(0, start);
    const suffix = value.slice(end);

    let insert = String(text);
    if (options.separator === "newline") {
      insert = `${prefix && !prefix.endsWith("\n") ? "\n" : ""}${insert}`;
    } else if (options.separator === "space") {
      insert = `${prefix && !/\s$/.test(prefix) ? " " : ""}${insert}`;
    }

    textarea.value = `${prefix}${insert}${suffix}`;
    const cursor = prefix.length + insert.length;
    textarea.setSelectionRange(cursor, cursor);
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
      const imagePath = response?.path;
      if (!imagePath) {
        throw new Error("Upload returned no path");
      }
      this._insertTextIntoTextInput(imagePath, { separator: "newline" });
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
    });

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

    this.socket.send(textEncoder.encode(seq));

    // Reset modifiers after sending
    this.ctrlActive = false;
    this.altActive = false;
    this._updateModifierButtons();
  }

  _sendCtrl(char) {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) return;
    const code = char.toUpperCase().charCodeAt(0) - 64;
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
