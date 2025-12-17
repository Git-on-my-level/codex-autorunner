import { flash, buildWsUrl } from "./utils.js";
import { CONSTANTS } from "./constants.js";
import { initVoiceInput } from "./voice.js";

const textEncoder = new TextEncoder();

const TEXT_INPUT_STORAGE_KEYS = Object.freeze({
  enabled: "codex_terminal_text_input_enabled",
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
    this.textInputEnabled = false;

    // Mobile controls state
    this.mobileControlsEl = null;
    this.ctrlActive = false;
    this.altActive = false;

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

    if (!this.statusEl || !this.connectBtn || !this.disconnectBtn || !this.resumeBtn) {
      return;
    }

    this.connectBtn.addEventListener("click", () => this.connect({ mode: "new" }));
    this.resumeBtn.addEventListener("click", () => this.connect({ mode: "resume" }));
    this.disconnectBtn.addEventListener("click", () => this.disconnect());
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
        this._handleResize();
      });
    });
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
      this.fitAddon.fit();
      this._handleResize();

      if (isResume) this.term?.write("\r\nLaunching codex resume...\r\n");
    };

    this.socket.onmessage = (event) => {
      if (typeof event.data === "string") {
        try {
          const payload = JSON.parse(event.data);
          if (payload.type === "hello") {
            if (payload.session_id) {
              localStorage.setItem("codex_terminal_session_id", payload.session_id);
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

      if (this.intentionalDisconnect) {
        this._setStatus("Disconnected");
        this.overlayEl?.classList.remove("hidden");
        return;
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

  _setTextInputEnabled(enabled, options = {}) {
    this.textInputEnabled = Boolean(enabled);
    this._writeBoolToStorage(TEXT_INPUT_STORAGE_KEYS.enabled, this.textInputEnabled);

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
    if (this.textInputSendBtn) this.textInputSendBtn.disabled = !connected;
    if (this.textInputTextareaEl) this.textInputTextareaEl.disabled = false;
  }

  _sendFromTextarea() {
    const text = this.textInputTextareaEl?.value || "";
    const ok = this._sendText(text, { appendNewline: true });
    if (!ok) return;

    if (this.textInputTextareaEl) {
      this.textInputTextareaEl.value = "";
    }

    if (this.isTouchDevice()) {
      requestAnimationFrame(() => {
        this._safeFocus(this.textInputTextareaEl);
      });
    } else {
      this.term?.focus();
    }
  }

  _initTextInputPanel() {
    this.terminalSectionEl = document.getElementById("terminal");
    this.textInputToggleBtn = document.getElementById("terminal-text-input-toggle");
    this.textInputPanelEl = document.getElementById("terminal-text-input");
    this.textInputTextareaEl = document.getElementById("terminal-textarea");
    this.textInputSendBtn = document.getElementById("terminal-text-send");

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

    this.textInputSendBtn.addEventListener("click", () => {
      if (this.textInputSendBtn?.disabled) {
        flash("Connect the terminal first", "error");
        return;
      }
      this._sendFromTextarea();
    });

    this.textInputTextareaEl.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" || e.shiftKey) return;
      if (e.isComposing) return;
      const value = this.textInputTextareaEl?.value || "";
      if (this._normalizeNewlines(value).includes("\n")) {
        return;
      }
      e.preventDefault();
      this._sendFromTextarea();
    });

    this._setTextInputEnabled(this.textInputEnabled, { focus: false });
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
        if (this.isTouchDevice() && this.textInputEnabled) {
          setTimeout(() => this._safeFocus(this.textInputTextareaEl), 0);
        }
        return;
      }

      // Handle direct sequences (arrows, esc, tab)
      const seq = btn.dataset.seq;
      if (seq) {
        this._sendKey(seq);
        if (this.isTouchDevice() && this.textInputEnabled) {
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

  _sendVoiceTranscript(text) {
    if (!text) {
      flash("Voice capture returned no transcript", "error");
      return;
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
  }
}


