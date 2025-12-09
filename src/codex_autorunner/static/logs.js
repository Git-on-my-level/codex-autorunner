import { api, flash, streamEvents } from "./utils.js";
import { publish, subscribe } from "./bus.js";

const logRunIdInput = document.getElementById("log-run-id");
const logTailInput = document.getElementById("log-tail");
const toggleLogStreamButton = document.getElementById("toggle-log-stream");
const showTimestampToggle = document.getElementById("log-show-timestamp");
const showRunToggle = document.getElementById("log-show-run");
let stopLogStream = null;
let lastKnownRunId = null;
let rawLogLines = [];

function appendLogLine(line) {
  const output = document.getElementById("log-output");
  if (output.dataset.isPlaceholder === "true") {
    delete output.dataset.isPlaceholder;
    rawLogLines = [];
  }
  rawLogLines.push(line);
  renderLogs();
  publish("logs:line", line);
}

function scrollLogsToBottom() {
  const output = document.getElementById("log-output");
  if (!output) return;
  requestAnimationFrame(() => {
    output.scrollTop = output.scrollHeight;
  });
}

function setLogStreamButton(active) {
  toggleLogStreamButton.textContent = active ? "Stop stream" : "Start stream";
}

async function loadLogs() {
  const runId = logRunIdInput.value;
  const tail = logTailInput.value || "200";
  const params = new URLSearchParams();
  if (runId) {
    params.set("run_id", runId);
  } else if (tail) {
    params.set("tail", tail);
  }
  const path = params.toString() ? `/api/logs?${params.toString()}` : "/api/logs";
  try {
    const data = await api(path);
    const text = typeof data === "string" ? data : data.log || "";
    const output = document.getElementById("log-output");
    
    if (text) {
      rawLogLines = text.split("\n");
      delete output.dataset.isPlaceholder;
      renderLogs();
    } else {
      output.textContent = "(empty log)";
      output.dataset.isPlaceholder = "true";
      rawLogLines = [];
    }
    
    flash("Logs loaded");
    publish("logs:loaded", { runId, tail, text });
  } catch (err) {
    flash(err.message);
  }
}

function stopLogStreaming() {
  if (stopLogStream) {
    stopLogStream();
    stopLogStream = null;
  }
  setLogStreamButton(false);
  publish("logs:streaming", false);
}

function startLogStreaming() {
  if (stopLogStream) return;
  const output = document.getElementById("log-output");
  output.textContent = "(listening...)";
  output.dataset.isPlaceholder = "true";
  rawLogLines = [];
  
  stopLogStream = streamEvents("/api/logs/stream", {
    onMessage: (data) => {
      appendLogLine(data || "");
    },
    onError: (err) => {
      flash(err.message);
      stopLogStreaming();
    },
    onFinish: () => {
      stopLogStream = null;
      setLogStreamButton(false);
      publish("logs:streaming", false);
    },
  });
  setLogStreamButton(true);
  publish("logs:streaming", true);
  flash("Streaming logs…");
}

function syncRunIdPlaceholder(state) {
  lastKnownRunId = state?.last_run_id ?? null;
  logRunIdInput.placeholder = lastKnownRunId ? `latest (${lastKnownRunId})` : "latest";
}

function renderLogs() {
  const output = document.getElementById("log-output");
  if (output.dataset.isPlaceholder === "true") return;
  const showTimestamp = showTimestampToggle.checked;
  const showRun = showRunToggle.checked;
  const rendered = rawLogLines
    .map((line) => {
      let next = line;
      // Normalize run markers that include "chat"
      next = next.replace(/^=== run (\d+)\s+chat(\s|$)/, "=== run $1$2");

      if (!showTimestamp) {
        next = next.replace(/^\[[^\]]*]\s*/, "");
      }
      if (!showRun) {
        if (next.startsWith("[")) {
          next = next.replace(/^(\[[^\]]+]\s*)run=\d+\s*/, "$1");
        } else {
          next = next.replace(/^run=\d+\s*/, "");
        }
      }
      // Remove redundant channel prefix
      next = next.replace(/^(\[[^\]]+]\s*)?(run=\d+\s*)?chat:\s*/, "$1$2");
      return next;
    })
    .join("\n");
  if (rendered) {
    output.textContent = rendered;
    delete output.dataset.isPlaceholder;
  } else {
    output.textContent = "(empty log)";
    output.dataset.isPlaceholder = "true";
  }
  scrollLogsToBottom();
}

export function initLogs() {
  document.getElementById("load-logs").addEventListener("click", loadLogs);
  toggleLogStreamButton.addEventListener("click", () => {
    if (stopLogStream) {
      stopLogStreaming();
    } else {
      startLogStreaming();
    }
  });

  subscribe("state:update", syncRunIdPlaceholder);
  subscribe("tab:change", (tab) => {
    if (tab !== "logs" && stopLogStream) {
      stopLogStreaming();
    }
  });

  showTimestampToggle.addEventListener("change", renderLogs);
  showRunToggle.addEventListener("change", renderLogs);

  loadLogs();
}
