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
    fontFamily: '"SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace',
    fontSize: 14,
    cursorBlink: true,
    rows: 24,
    cols: 100,
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
  if (!fitAddon || !term || !socket || socket.readyState !== WebSocket.OPEN) return;
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
