import { api, flash, buildWsUrl, isMobileViewport } from "./utils.js";
import { CONSTANTS } from "./constants.js";
import { initVoiceInput } from "./voice.js";
import { publish } from "./bus.js";

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
    this.mobileViewEl = null;
    this.ctrlActive = false;
    this.altActive = false;
    this.baseViewportHeight = window.innerHeight;
    this.suppressNextSendClick = false;
    this.lastSendTapAt = 0;
    this.textInputWasFocused = false;

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
    if (localStorage.getItem("codex_terminal_session_id")) {
      this.connect({ mode: "attach" });
    }
  }

  /**
   * Set terminal status message
   */
  _setStatus(message) {
    if (this.statusEl) {
      this.statusEl.textContent = message;
    }
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
      });
    });
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

    const savedSessionId = localStorage.getItem("codex_terminal_session_id");
    if (isAttach) {
      if (savedSessionId) {
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
      localStorage.removeItem("codex_terminal_session_id");
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
              localStorage.setItem("codex_terminal_session_id", payload.session_id);
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
            localStorage.removeItem("codex_terminal_session_id");
            this.intentionalDisconnect = true;
            this.disconnect();
          } else if (payload.type === "error") {
            if (payload.message && payload.message.includes("Session not found")) {
              localStorage.removeItem("codex_terminal_session_id");
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
      const savedId = localStorage.getItem("codex_terminal_session_id");
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
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
      flash("Connect the terminal first", "error");
      return false;
    }

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
      flash("Reconnect to resend pending input", "error");
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
    if (!file.type || !file.type.startsWith("image/")) {
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
    const images = Array.from(files).filter(
      (file) => file && file.type && file.type.startsWith("image/")
    );
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
    this.textInputSendBtn.addEventListener("touchend", (e) => {
      e.preventDefault();
      triggerSend();
    });
    this.textInputSendBtn.addEventListener("click", () => {
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
      if (this.isTouchDevice() && isMobileViewport()) {
        this.enterMobileInputMode();
      }
    });

    this.textInputTextareaEl.addEventListener("blur", () => {
      // Wait a tick so activeElement updates.
      setTimeout(() => {
        if (document.activeElement !== this.textInputTextareaEl) {
          this.textInputWasFocused = false;
        }
        this._updateComposerSticky();
        if (this.isTouchDevice() && isMobileViewport()) {
          this.exitMobileInputMode();
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
    
    // Create mobile view container if it doesn't exist
    if (!document.getElementById("mobile-terminal-view")) {
      this.mobileViewEl = document.createElement("div");
      this.mobileViewEl.id = "mobile-terminal-view";
      this.mobileViewEl.className = "mobile-terminal-view hidden";
      document.body.appendChild(this.mobileViewEl);
    } else {
      this.mobileViewEl = document.getElementById("mobile-terminal-view");
    }
    if (this.mobileViewScrollTop === undefined) {
      this.mobileViewScrollTop = null;
    }
    this.mobileViewEl?.addEventListener("scroll", () => {
      this.mobileViewScrollTop = this.mobileViewEl.scrollTop;
    });

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

  _getAnsiColor(index) {
    // 0-15: Theme colors
    const theme = CONSTANTS.THEME.XTERM;
    const basic = [
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
    if (index >= 0 && index < 16) return basic[index];

    // 16-231: 6x6x6 Cube
    if (index >= 16 && index < 232) {
      let i = index - 16;
      let b = i % 6;
      let g = Math.floor(i / 6) % 6;
      let r = Math.floor(i / 36);
      const toHex = (v) => (v ? v * 40 + 55 : 0).toString(16).padStart(2, "0");
      return `#${toHex(r)}${toHex(g)}${toHex(b)}`;
    }

    // 232-255: Grayscale
    if (index >= 232 && index < 256) {
      let i = index - 232;
      const v = (i * 10 + 8).toString(16).padStart(2, "0");
      return `#${v}${v}${v}`;
    }

    return null;
  }

  _renderLineAsHtml(line, cell) {
    if (!line) return "";
    let html = "";
    let lastStyle = null;
    let spanOpen = false;
    const useLoader = typeof line.loadCell === "function";

    const loadCell = (index) => {
      if (useLoader) {
        line.loadCell(index, cell);
      } else {
        line.getCell(index, cell);
      }
    };

    // Find last non-empty index
    const lineLength = Number.isInteger(line.length)
      ? line.length
      : typeof line.getTrimmedLength === "function"
      ? line.getTrimmedLength()
      : 0;
    let lastIndex = lineLength - 1;
    while (lastIndex >= 0) {
      try {
        loadCell(lastIndex);
        const hasContent = (cell.getChars() || "").trim().length > 0;
        const bgMode = typeof cell.getBgColorMode === "function" ? cell.getBgColorMode() : cell.getBgColorMode;
        const hasBg = bgMode !== 0 && bgMode !== undefined;
        if (hasContent || hasBg) break;
      } catch (e) {
        break;
      }
      lastIndex--;
    }

    lastIndex = Math.max(lastIndex, 0);

    for (let i = 0; i <= lastIndex; i++) {
      try {
        loadCell(i);
      } catch (e) {
        continue;
      }
      
      const char = cell.getChars() || " ";
      const width = cell.getWidth();
      if (width === 0 && char === "") continue;

      let style = "";

      // Foreground
      let fgMode = typeof cell.getFgColorMode === "function" ? cell.getFgColorMode() : cell.getFgColorMode;
      let fgColor = typeof cell.getFgColor === "function" ? cell.getFgColor() : cell.getFgColor;
      
      // Fallback for older xterm or different internal structures
      if (fgMode === undefined && cell.fg !== undefined) {
          // In some versions, fg is a packed 32-bit integer
          // [2 bits mode][21 bits color][...]
          // This is getting complex, but let's try to detect if it's just an index
          if (cell.fg < 256) {
              fgMode = 1;
              fgColor = cell.fg;
          }
      }

      if (fgMode === 1 || fgMode === 2) {
        const hex = this._getAnsiColor(fgColor);
        if (hex) style += `color:${hex};`;
      } else if (fgMode === 3) {
        const r = (fgColor >>> 16) & 0xff;
        const g = (fgColor >>> 8) & 0xff;
        const b = fgColor & 0xff;
        style += `color:rgb(${r},${g},${b});`;
      }

      // Background
      let bgMode = typeof cell.getBgColorMode === "function" ? cell.getBgColorMode() : cell.getBgColorMode;
      let bgColor = typeof cell.getBgColor === "function" ? cell.getBgColor() : cell.getBgColor;

      if (bgMode === undefined && cell.bg !== undefined) {
          if (cell.bg < 256) {
              bgMode = 1;
              bgColor = cell.bg;
          }
      }
      
      if (bgMode === 1 || bgMode === 2) {
        const hex = this._getAnsiColor(bgColor);
        if (hex) style += `background-color:${hex};`;
      } else if (bgMode === 3) {
        const r = (bgColor >>> 16) & 0xff;
        const g = (bgColor >>> 8) & 0xff;
        const b = bgColor & 0xff;
        style += `background-color:rgb(${r},${g},${b});`;
      }

      if (typeof cell.isBold === "function" && cell.isBold()) style += "font-weight:700;";
      else if (cell.isBold === true) style += "font-weight:700;";
      
      if (typeof cell.isItalic === "function" && cell.isItalic()) style += "font-style:italic;";
      else if (cell.isItalic === true) style += "font-style:italic;";
      
      if (typeof cell.isUnderline === "function" && cell.isUnderline()) style += "text-decoration:underline;";
      else if (cell.isUnderline === true) style += "text-decoration:underline;";

      const isInverse = (typeof cell.isInverse === "function" ? cell.isInverse() : cell.isInverse) === true;
      if (isInverse) {
          // Swap style colors if they exist, or use a generic swap
          // This is a bit simplified but usually works
          style += "filter: invert(1) hue-rotate(180deg);";
      }

      if (style !== lastStyle) {
        if (spanOpen) html += "</span>";
        if (style) {
          html += `<span style="${style}">`;
          spanOpen = true;
        } else {
          spanOpen = false;
        }
        lastStyle = style;
      }

      if (char === " ") html += "&nbsp;";
      else if (char === "&") html += "&amp;";
      else if (char === "<") html += "&lt;";
      else if (char === ">") html += "&gt;";
      else html += char;
    }

    if (spanOpen) html += "</span>";
    return html;
  }

  enterMobileInputMode() {
    if (!this.term || !this.mobileViewEl) return;
    
    const buffer = this.term.buffer.active;
    const coreBuffer = this.term?._core?.bufferService?.buffer;
    const useCore = Boolean(coreBuffer && coreBuffer.lines && typeof coreBuffer.lines.get === "function");
    const rows = this.term.rows || buffer.length || 0;
    const maxIndex = useCore
      ? coreBuffer.lines.length - 1
      : buffer.length - 1;
    const totalLines = Math.min(
      maxIndex,
      useCore ? coreBuffer.ybase + rows - 1 : buffer.baseY + rows - 1
    );
    const start = Math.max(0, totalLines - 500);
    
    let cell;
    // Prefer the internal buffer cell when available to preserve ANSI colors.
    try {
      if (useCore && typeof coreBuffer.getNullCell === "function") {
        cell = coreBuffer.getNullCell();
      } else if (!useCore && typeof buffer.getNullCell === "function") {
        cell = buffer.getNullCell();
      } else {
        // xterm fallback: getCell(0) may return a reusable cell.
        const line0 = useCore ? coreBuffer.lines.get(0) : buffer.getLine(0);
        if (line0 && typeof line0.getCell === "function") {
          const probe = line0.getCell(0);
          if (probe) cell = probe;
        }
      }
    } catch (e) {
      console.warn("Could not initialize cell for HTML rendering", e);
    }

    if (!cell) {
      console.log("Falling back to plain text rendering");
      let content = "";
      for (let i = start; i <= totalLines; i++) {
        const line = buffer.getLine(i);
        if (line) content += line.translateToString(true) + "\n";
      }
      this.mobileViewEl.textContent = content;
      this.mobileViewEl.classList.remove("hidden");
      requestAnimationFrame(() => {
        if (this.mobileViewScrollTop !== null) {
          this.mobileViewEl.scrollTop = this.mobileViewScrollTop;
        } else {
          this.mobileViewEl.scrollTop = this.mobileViewEl.scrollHeight;
        }
      });
      return;
    }
    
    let html = "";
    for (let i = start; i <= totalLines; i++) {
      const line = useCore ? coreBuffer.lines.get(i) : buffer.getLine(i);
      if (line) {
        const lineHtml = this._renderLineAsHtml(line, cell);
        html += `<div class="mobile-terminal-line">${lineHtml || "&nbsp;"}</div>`;
      }
    }
    
    this.mobileViewEl.innerHTML = html;
    this.mobileViewEl.classList.remove("hidden");
    
    requestAnimationFrame(() => {
      if (this.mobileViewScrollTop !== null) {
        this.mobileViewEl.scrollTop = this.mobileViewScrollTop;
      } else {
        this.mobileViewEl.scrollTop = this.mobileViewEl.scrollHeight;
      }
    });
  }

  exitMobileInputMode() {
    if (!this.mobileViewEl) return;
    if (this.mobileViewEl.classList.contains("hidden")) return;
    this.mobileViewScrollTop = this.mobileViewEl.scrollTop;
    this.mobileViewEl.classList.add("hidden");
    this.mobileViewEl.innerHTML = "";
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
