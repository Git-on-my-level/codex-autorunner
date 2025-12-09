import { flash } from "./utils.js";

let term = null;
let fitAddon = null;
let socket = null;
let statusEl = null;
let overlayEl = null;
let connectBtn = null;
let disconnectBtn = null;
let inputDisposable = null;

const textEncoder = new TextEncoder();

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
    theme: {
      background: '#0a0c12',
      foreground: '#e5ecff',
      cursor: '#6cf5d8',
      selectionBackground: 'rgba(108, 245, 216, 0.3)',
      black: '#000000',
      red: '#ff5566',
      green: '#6cf5d8',
      yellow: '#f1fa8c',
      blue: '#6ca8ff',
      magenta: '#bd93f9',
      cyan: '#8be9fd',
      white: '#e5ecff',
      brightBlack: '#6272a4',
      brightRed: '#ff6e6e',
      brightGreen: '#69ff94',
      brightYellow: '#ffffa5',
      brightBlue: '#d6acff',
      brightMagenta: '#ff92df',
      brightCyan: '#a4ffff',
      brightWhite: '#ffffff',
    },
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

function connect() {
  if (!ensureTerminal()) return;
  if (socket && socket.readyState === WebSocket.OPEN) return;
  teardownSocket();

  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${proto}://${window.location.host}/api/terminal`);
  socket.binaryType = "arraybuffer";

  socket.onopen = () => {
    overlayEl?.classList.add("hidden");
    setStatus("Connected");
    updateButtons(true);
    fitAddon.fit();
    handleResize();
  };

  socket.onmessage = (event) => {
    if (typeof event.data === "string") {
      try {
        const payload = JSON.parse(event.data);
        if (payload.type === "exit") {
          term?.write(`\r\n[session ended${payload.code !== null ? ` (code ${payload.code})` : ""}] \r\n`);
          setStatus("Disconnected");
          updateButtons(false);
          overlayEl?.classList.remove("hidden");
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
    flash("Terminal connection error", "error");
  };

  socket.onclose = () => {
    setStatus("Disconnected");
    overlayEl?.classList.remove("hidden");
    updateButtons(false);
  };
}

function disconnect() {
  teardownSocket();
  setStatus("Disconnected");
  overlayEl?.classList.remove("hidden");
  updateButtons(false);
}

export function initTerminal() {
  statusEl = document.getElementById("terminal-status");
  overlayEl = document.getElementById("terminal-overlay");
  connectBtn = document.getElementById("terminal-connect");
  disconnectBtn = document.getElementById("terminal-disconnect");

  if (!statusEl || !connectBtn || !disconnectBtn) return;

  connectBtn.addEventListener("click", connect);
  disconnectBtn.addEventListener("click", disconnect);
  updateButtons(false);
  setStatus("Disconnected");

  window.addEventListener("resize", handleResize);
}
