import { flash, buildWsUrl } from "./utils.js";
import { CONSTANTS } from "./constants.js";
import { initVoiceInput } from "./voice.js";

let term = null;
let fitAddon = null;
let socket = null;
let statusEl = null;
let overlayEl = null;
let connectBtn = null;
let disconnectBtn = null;
let resumeBtn = null;
let inputDisposable = null;
let intentionalDisconnect = false;
let reconnectTimer = null;
let reconnectAttempts = 0;
let voiceBtn = null;
let voiceStatus = null;
let voiceController = null;
let voiceKeyActive = false;

// Mobile controls state
let mobileControlsEl = null;
let ctrlActive = false;
let altActive = false;

const textEncoder = new TextEncoder();

// Check if device has touch capability
function isTouchDevice() {
  return 'ontouchstart' in window || navigator.maxTouchPoints > 0;
}

// Send a key sequence to the terminal
function sendKey(seq) {
  if (!socket || socket.readyState !== WebSocket.OPEN) return;
  
  // If ctrl modifier is active, convert to ctrl code
  if (ctrlActive && seq.length === 1) {
    const char = seq.toUpperCase();
    const code = char.charCodeAt(0) - 64;
    if (code >= 1 && code <= 26) {
      seq = String.fromCharCode(code);
    }
  }
  
  socket.send(textEncoder.encode(seq));
  
  // Reset modifiers after sending (unless it was just a modifier toggle)
  ctrlActive = false;
  altActive = false;
  updateModifierButtons();
}

// Send Ctrl+key combo
function sendCtrl(char) {
  if (!socket || socket.readyState !== WebSocket.OPEN) return;
  const code = char.toUpperCase().charCodeAt(0) - 64;
  socket.send(textEncoder.encode(String.fromCharCode(code)));
}

// Update modifier button visual states
function updateModifierButtons() {
  const ctrlBtn = document.getElementById('tmb-ctrl');
  const altBtn = document.getElementById('tmb-alt');
  if (ctrlBtn) ctrlBtn.classList.toggle('active', ctrlActive);
  if (altBtn) altBtn.classList.toggle('active', altActive);
}

// Initialize mobile controls
function initMobileControls() {
  mobileControlsEl = document.getElementById('terminal-mobile-controls');
  if (!mobileControlsEl) return;
  
  // Only show on touch devices
  if (!isTouchDevice()) {
    mobileControlsEl.style.display = 'none';
    return;
  }
  
  // Handle all key buttons
  mobileControlsEl.addEventListener('click', (e) => {
    const btn = e.target.closest('.tmb-key');
    if (!btn) return;
    
    e.preventDefault();
    
    // Handle modifier toggles
    const modKey = btn.dataset.key;
    if (modKey === 'ctrl') {
      ctrlActive = !ctrlActive;
      updateModifierButtons();
      return;
    }
    if (modKey === 'alt') {
      altActive = !altActive;
      updateModifierButtons();
      return;
    }
    
    // Handle Ctrl+X combos
    const ctrlChar = btn.dataset.ctrl;
    if (ctrlChar) {
      sendCtrl(ctrlChar);
      return;
    }
    
    // Handle direct sequences (arrows, esc, tab)
    const seq = btn.dataset.seq;
    if (seq) {
      sendKey(seq);
      return;
    }
  });
  
  // Add haptic feedback on touch if available
  mobileControlsEl.addEventListener('touchstart', (e) => {
    if (e.target.closest('.tmb-key') && navigator.vibrate) {
      navigator.vibrate(10);
    }
  }, { passive: true });
}

function setStatus(message) {
  if (statusEl) {
    statusEl.textContent = message;
  }
}

function getFontSize() {
  return window.innerWidth < 640 ? 10 : 13;
}

function ensureTerminal() {
  if (!window.Terminal || !window.FitAddon) {
    setStatus("xterm assets missing; reload or check /static/vendor");
    flash("xterm assets missing; reload the page", "error");
    return false;
  }
  if (term) {
    return true;
  }
  const container = document.getElementById("terminal-container");
  if (!container) return false;
  term = new window.Terminal({
    convertEol: true,
    fontFamily: '"JetBrains Mono", "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace',
    fontSize: getFontSize(),
    cursorBlink: true,
    rows: 24,
    cols: 100,
    theme: CONSTANTS.THEME.XTERM,
  });
  fitAddon = new window.FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  term.open(container);
  term.write('Press "Start session" to launch Codex TUI...\r\n');
  if (!inputDisposable) {
    inputDisposable = term.onData((data) => {
      if (!socket || socket.readyState !== WebSocket.OPEN) return;
      socket.send(textEncoder.encode(data));
    });
  }
  return true;
}

function teardownSocket() {
  if (socket) {
    socket.onclose = null;
    socket.onerror = null;
    socket.onmessage = null;
    socket.onopen = null;
    try {
      socket.close();
    } catch (err) {
      // ignore
    }
  }
  socket = null;
}

function updateButtons(connected) {
  if (connectBtn) connectBtn.disabled = connected;
  if (disconnectBtn) disconnectBtn.disabled = !connected;
  if (resumeBtn) resumeBtn.disabled = connected;
  const voiceUnavailable = voiceBtn?.classList.contains("disabled");
  if (voiceBtn && !voiceUnavailable) {
    voiceBtn.disabled = !connected;
    voiceBtn.classList.toggle("voice-disconnected", !connected);
  }
  if (voiceStatus && !voiceUnavailable && !connected) {
    voiceStatus.textContent = "Connect to use voice";
    voiceStatus.classList.remove("hidden");
  } else if (
    voiceStatus &&
    !voiceUnavailable &&
    connected &&
    voiceController &&
    voiceStatus.textContent === "Connect to use voice"
  ) {
    voiceStatus.textContent = "Hold to talk (Alt+V)";
    voiceStatus.classList.remove("hidden");
  }
}

function handleResize() {
  if (!fitAddon || !term) return;
  
  // Update font size based on current window width
  const newFontSize = getFontSize();
  if (term.options.fontSize !== newFontSize) {
    term.options.fontSize = newFontSize;
  }

  // Only send resize if connected
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    try {
      fitAddon.fit();
    } catch (e) {
      // ignore fit errors when not visible
    }
    return;
  }

  fitAddon.fit();
  socket.send(
    JSON.stringify({
      type: "resize",
      cols: term.cols,
      rows: term.rows,
    })
  );
}

function connect(options = {}) {
  const resume = Boolean(options.resume);
  if (!ensureTerminal()) return;
  if (socket && socket.readyState === WebSocket.OPEN) return;
  
  // cancel any pending reconnect
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }

  teardownSocket();
  intentionalDisconnect = false;

  const query = resume ? "?mode=resume" : "";
  const wsUrl = buildWsUrl(CONSTANTS.API.TERMINAL_ENDPOINT, query);
  socket = new WebSocket(wsUrl);
  socket.binaryType = "arraybuffer";

  socket.onopen = () => {
    reconnectAttempts = 0;
    overlayEl?.classList.add("hidden");
    setStatus(resume ? "Connected (resume)" : "Connected");
    updateButtons(true);
    fitAddon.fit();
    handleResize();
    if (resume) {
      term?.write("\r\nLaunching resume flow...\r\n");
    }
  };

  socket.onmessage = (event) => {
    if (typeof event.data === "string") {
      try {
        const payload = JSON.parse(event.data);
        if (payload.type === "exit") {
          term?.write(`\r\n[session ended${payload.code !== null ? ` (code ${payload.code})` : ""}] \r\n`);
          // Treat exit as an intentional disconnect or at least not something to auto-reconnect to immediately
          intentionalDisconnect = true; 
          disconnect();
        } else if (payload.type === "error") {
          flash(payload.message || "Terminal error", "error");
        }
      } catch (err) {
        // ignore bad payloads
      }
      return;
    }
    if (term) {
      term.write(new Uint8Array(event.data));
    }
  };

  socket.onerror = () => {
    setStatus("Connection error");
    // Don't flash here, onclose will handle retry or final error
  };

  socket.onclose = () => {
    updateButtons(false);
    
    if (intentionalDisconnect) {
      setStatus("Disconnected");
      overlayEl?.classList.remove("hidden");
      return;
    }

    // Auto-reconnect logic
    if (reconnectAttempts < 5) {
      const delay = Math.min(1000 * Math.pow(1.5, reconnectAttempts), 10000);
      setStatus(`Reconnecting in ${Math.round(delay / 100)}s...`);
      reconnectAttempts++;
      reconnectTimer = setTimeout(() => {
        connect({ resume: true }); // Always try to resume on reconnect
      }, delay);
    } else {
      setStatus("Disconnected (max retries reached)");
      overlayEl?.classList.remove("hidden");
      flash("Terminal connection lost", "error");
    }
  };
}

function disconnect() {
  intentionalDisconnect = true;
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  teardownSocket();
  setStatus("Disconnected");
  overlayEl?.classList.remove("hidden");
  updateButtons(false);
  if (voiceKeyActive) {
    voiceKeyActive = false;
    voiceController?.stop();
  }
}

function sendVoiceTranscript(text) {
  if (!text) {
    flash("Voice capture returned no transcript", "error");
    return;
  }
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    flash("Connect the terminal before using voice input", "error");
    if (voiceStatus) {
      voiceStatus.textContent = "Connect to send voice";
      voiceStatus.classList.remove("hidden");
    }
    return;
  }
  const payload = text.endsWith("\n") ? text : `${text}\n`;
  socket.send(textEncoder.encode(payload));
  term?.focus();
  flash("Voice transcript sent to terminal");
}

function initTerminalVoice() {
  voiceBtn = document.getElementById("terminal-voice");
  voiceStatus = document.getElementById("terminal-voice-status");
  if (!voiceBtn || !voiceStatus) return;

  initVoiceInput({
    button: voiceBtn,
    input: null,
    statusEl: voiceStatus,
    onTranscript: sendVoiceTranscript,
    onError: (msg) => {
      if (!msg) return;
      flash(msg, "error");
      voiceStatus.textContent = msg;
      voiceStatus.classList.remove("hidden");
    },
  })
    .then((controller) => {
      if (!controller) {
        voiceBtn.closest(".terminal-voice")?.classList.add("hidden");
        return;
      }
      voiceController = controller;
      if (voiceStatus) {
        const base = voiceStatus.textContent || "Hold to talk";
        voiceStatus.textContent = `${base} (Alt+V)`;
        voiceStatus.classList.remove("hidden");
      }
      window.addEventListener("keydown", handleVoiceHotkeyDown);
      window.addEventListener("keyup", handleVoiceHotkeyUp);
      window.addEventListener("blur", () => {
        if (voiceKeyActive) {
          voiceKeyActive = false;
          voiceController?.stop();
        }
      });
    })
    .catch((err) => {
      console.error("Voice init failed", err);
      flash("Voice capture unavailable", "error");
      voiceStatus.textContent = "Voice unavailable";
      voiceStatus.classList.remove("hidden");
    });
}

function matchesVoiceHotkey(event) {
  return event.key && event.key.toLowerCase() === "v" && event.altKey;
}

function handleVoiceHotkeyDown(event) {
  if (!voiceController || voiceKeyActive) return;
  if (!matchesVoiceHotkey(event)) return;
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    flash("Connect the terminal before using voice input", "error");
    return;
  }
  event.preventDefault();
  event.stopPropagation();
  voiceKeyActive = true;
  voiceController.start();
}

function handleVoiceHotkeyUp(event) {
  if (!voiceKeyActive) return;
  if (event && matchesVoiceHotkey(event)) {
    event.preventDefault();
    event.stopPropagation();
  }
  voiceKeyActive = false;
  voiceController?.stop();
}

export function initTerminal() {
  statusEl = document.getElementById("terminal-status");
  overlayEl = document.getElementById("terminal-overlay");
  connectBtn = document.getElementById("terminal-connect");
  disconnectBtn = document.getElementById("terminal-disconnect");
  resumeBtn = document.getElementById("terminal-resume");

  if (!statusEl || !connectBtn || !disconnectBtn || !resumeBtn) return;

  connectBtn.addEventListener("click", () => connect({ resume: false }));
  resumeBtn.addEventListener("click", () => connect({ resume: true }));
  disconnectBtn.addEventListener("click", disconnect);
  updateButtons(false);
  setStatus("Disconnected");

  window.addEventListener("resize", handleResize);
  
  // Initialize mobile touch controls
  initMobileControls();
  initTerminalVoice();
}
