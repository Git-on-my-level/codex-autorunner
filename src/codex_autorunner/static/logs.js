import { api, flash, streamEvents } from "./utils.js";
import { publish, subscribe } from "./bus.js";

const logRunIdInput = document.getElementById("log-run-id");
const logTailInput = document.getElementById("log-tail");
const toggleLogStreamButton = document.getElementById("toggle-log-stream");
let stopLogStream = null;
let lastKnownRunId = null;

function appendLogLine(line) {
  const output = document.getElementById("log-output");
  
  if (output.dataset.isPlaceholder === "true") {
    output.textContent = line;
    delete output.dataset.isPlaceholder;
  } else {
    // Append using insertAdjacentText to avoid reading/serializing the full text content
    output.insertAdjacentText("beforeend", "\n" + line);
  }

  // Throttle scroll updates
  if (!output.dataset.scrollPending) {
    output.dataset.scrollPending = "true";
    requestAnimationFrame(() => {
      output.scrollTop = output.scrollHeight;
      delete output.dataset.scrollPending;
    });
  }
  
  publish("logs:line", line);
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
      output.textContent = text;
      delete output.dataset.isPlaceholder;
    } else {
      output.textContent = "(empty log)";
      output.dataset.isPlaceholder = "true";
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

  loadLogs();
}
