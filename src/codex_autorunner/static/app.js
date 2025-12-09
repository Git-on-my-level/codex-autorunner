const panels = document.querySelectorAll(".panel");
const tabButtons = document.querySelectorAll(".tab");
const docButtons = document.querySelectorAll(".chip[data-doc]");
const toast = document.getElementById("toast");
const decoder = new TextDecoder();

let authToken = localStorage.getItem("codexAuthToken") || "";
const tokenInput = document.getElementById("auth-token");
tokenInput.value = authToken;
tokenInput.addEventListener("change", () => {
  authToken = tokenInput.value.trim();
  localStorage.setItem("codexAuthToken", authToken);
  flash("Auth token saved locally");
});

function flash(message) {
  toast.textContent = message;
  toast.classList.add("show");
  setTimeout(() => toast.classList.remove("show"), 2200);
}

function setActivePanel(id) {
  panels.forEach((p) => p.classList.toggle("active", p.id === id));
  tabButtons.forEach((btn) => btn.classList.toggle("active", btn.dataset.target === id));
}

tabButtons.forEach((btn) =>
  btn.addEventListener("click", () => {
    setActivePanel(btn.dataset.target);
  })
);

function statusPill(el, status) {
  el.textContent = status || "idle";
  el.classList.remove("pill-idle", "pill-running", "pill-error");
  if (status === "running") {
    el.classList.add("pill-running");
  } else if (status === "error") {
    el.classList.add("pill-error");
  } else {
    el.classList.add("pill-idle");
  }
}

async function api(path, options = {}) {
  const headers = options.headers ? { ...options.headers } : {};
  if (authToken) {
    headers["Authorization"] = `Bearer ${authToken}`;
  }
  const opts = { ...options, headers };
  if (opts.body && typeof opts.body === "object" && !(opts.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, opts);
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `Request failed (${res.status})`);
  }
  const contentType = res.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    return res.json();
  }
  return res.text();
}

function streamEvents(path, { method = "GET", body = null, onMessage, onError, onFinish } = {}) {
  const controller = new AbortController();
  let fetchBody = body;
  const headers = {};
  if (authToken) {
    headers["Authorization"] = `Bearer ${authToken}`;
  }
  if (fetchBody && typeof fetchBody === "object" && !(fetchBody instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    fetchBody = JSON.stringify(fetchBody);
  }
  fetch(path, { method, body: fetchBody, headers, signal: controller.signal })
    .then(async (res) => {
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || `Request failed (${res.status})`);
      }
      if (!res.body) {
        throw new Error("Streaming not supported in this browser");
      }
      const reader = res.body.getReader();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const chunks = buffer.split("\n\n");
        buffer = chunks.pop();
        for (const chunk of chunks) {
          if (!chunk.trim()) continue;
          const lines = chunk.split("\n");
          let event = "message";
          const dataLines = [];
          for (const line of lines) {
            if (line.startsWith("event:")) {
              event = line.slice(6).trim();
            } else if (line.startsWith("data:")) {
              dataLines.push(line.slice(5).trimStart());
            }
          }
          const data = dataLines.join("\n");
          if (onMessage) onMessage(data, event || "message");
        }
      }
      if (!controller.signal.aborted && onFinish) {
        onFinish();
      }
    })
    .catch((err) => {
      if (controller.signal.aborted) {
        if (onFinish) onFinish();
        return;
      }
      if (onError) onError(err);
      if (onFinish) onFinish();
    });

  return () => controller.abort();
}

async function loadState() {
  try {
    const data = await api("/api/state");
    statusPill(document.getElementById("runner-status"), data.status);
    document.getElementById("last-run-id").textContent = data.last_run_id ?? "–";
    document.getElementById("last-exit-code").textContent = data.last_exit_code ?? "–";
    document.getElementById("last-start").textContent = data.last_run_started_at ?? "–";
    document.getElementById("last-finish").textContent = data.last_run_finished_at ?? "–";
    document.getElementById("todo-count").textContent = data.outstanding_count ?? "–";
    document.getElementById("done-count").textContent = data.done_count ?? "–";
    document.getElementById("runner-pid").textContent = `Runner pid: ${data.runner_pid ?? "–"}`;
  } catch (err) {
    flash(err.message);
  }
}

async function startRun(once = false) {
  const btn = once ? document.getElementById("start-once") : document.getElementById("start-run");
  btn.disabled = true;
  try {
    await api("/api/run/start", { method: "POST", body: { once } });
    flash(once ? "Started one-off run" : "Runner starting");
    await loadState();
  } catch (err) {
    flash(err.message);
  } finally {
    btn.disabled = false;
  }
}

async function stopRun() {
  const btn = document.getElementById("stop-run");
  btn.disabled = true;
  try {
    await api("/api/run/stop", { method: "POST" });
    flash("Stop signal sent");
    await loadState();
  } catch (err) {
    flash(err.message);
  } finally {
    btn.disabled = false;
  }
}

async function resumeRun() {
  const btn = document.getElementById("resume-run");
  btn.disabled = true;
  try {
    await api("/api/run/resume", { method: "POST" });
    flash("Resume requested");
    await loadState();
  } catch (err) {
    flash(err.message);
  } finally {
    btn.disabled = false;
  }
}

async function killRun() {
  const btn = document.getElementById("kill-run");
  btn.disabled = true;
  try {
    await api("/api/run/kill", { method: "POST" });
    flash("Kill signal sent");
    await loadState();
  } catch (err) {
    flash(err.message);
  } finally {
    btn.disabled = false;
  }
}

document.getElementById("start-run").addEventListener("click", () => startRun(false));
document.getElementById("start-once").addEventListener("click", () => startRun(true));
document.getElementById("stop-run").addEventListener("click", stopRun);
document.getElementById("resume-run").addEventListener("click", resumeRun);
document.getElementById("kill-run").addEventListener("click", killRun);
document.getElementById("refresh-state").addEventListener("click", loadState);

let docsCache = {
  todo: "",
  progress: "",
  opinions: "",
};
let activeDoc = "todo";

async function loadDocs() {
  try {
    const data = await api("/api/docs");
    docsCache = { ...docsCache, ...data };
    setDoc(activeDoc);
    renderTodoPreview(docsCache.todo);
    document.getElementById("doc-status").textContent = "Loaded";
  } catch (err) {
    flash(err.message);
  }
}

function setDoc(kind) {
  activeDoc = kind;
  docButtons.forEach((btn) => btn.classList.toggle("active", btn.dataset.doc === kind));
  const textarea = document.getElementById("doc-content");
  textarea.value = docsCache[kind] || "";
  document.getElementById("doc-status").textContent = `Editing ${kind.toUpperCase()}`;
}

docButtons.forEach((btn) =>
  btn.addEventListener("click", () => {
    setDoc(btn.dataset.doc);
  })
);

async function saveDoc() {
  const content = document.getElementById("doc-content").value;
  const saveBtn = document.getElementById("save-doc");
  saveBtn.disabled = true;
  try {
    await api(`/api/docs/${activeDoc}`, { method: "PUT", body: { content } });
    docsCache[activeDoc] = content;
    flash(`${activeDoc.toUpperCase()} saved`);
    if (activeDoc === "todo") {
      renderTodoPreview(content);
      await loadState();
    }
  } catch (err) {
    flash(err.message);
  } finally {
    saveBtn.disabled = false;
  }
}

document.getElementById("save-doc").addEventListener("click", saveDoc);
document.getElementById("reload-doc").addEventListener("click", loadDocs);

function renderTodoPreview(text) {
  const list = document.getElementById("todo-preview-list");
  list.innerHTML = "";
  const lines = text.split("\n").map((l) => l.trim());
  const todos = lines.filter((l) => l.startsWith("- [")).slice(0, 8);
  if (todos.length === 0) {
    const li = document.createElement("li");
    li.textContent = "No TODO items found.";
    list.appendChild(li);
    return;
  }
  todos.forEach((line) => {
    const li = document.createElement("li");
    const box = document.createElement("div");
    box.className = "box";
    const done = line.toLowerCase().startsWith("- [x]");
    if (done) box.classList.add("done");
    const textSpan = document.createElement("span");
    textSpan.textContent = line.substring(5).trim();
    li.appendChild(box);
    li.appendChild(textSpan);
    list.appendChild(li);
  });
}

document.getElementById("refresh-preview").addEventListener("click", loadDocs);

function appendLogLine(line) {
  const output = document.getElementById("log-output");
  const current = output.textContent;
  const base = current === "(no log loaded yet)" || current === "(listening...)" ? "" : current;
  output.textContent = base ? `${base}\n${line}` : line;
  output.scrollTop = output.scrollHeight;
}

async function loadLogs() {
  const runId = document.getElementById("log-run-id").value;
  const tail = document.getElementById("log-tail").value || "200";
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
    document.getElementById("log-output").textContent = text || "(empty log)";
    flash("Logs loaded");
  } catch (err) {
    flash(err.message);
  }
}

document.getElementById("load-logs").addEventListener("click", loadLogs);
const toggleLogStreamButton = document.getElementById("toggle-log-stream");
let stopLogStream = null;

function setLogStreamButton(active) {
  toggleLogStreamButton.textContent = active ? "Stop stream" : "Start stream";
}

function startLogStreaming() {
  if (stopLogStream) return;
  document.getElementById("log-output").textContent = "(listening...)";
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
    },
  });
  setLogStreamButton(true);
  flash("Streaming logs…");
}

function stopLogStreaming() {
  if (stopLogStream) {
    stopLogStream();
    stopLogStream = null;
  }
  setLogStreamButton(false);
}

toggleLogStreamButton.addEventListener("click", () => {
  if (stopLogStream) {
    stopLogStreaming();
  } else {
    startLogStreaming();
  }
});

const chatForm = document.getElementById("chat-form");
const chatInput = document.getElementById("chat-input");
const chatThread = document.getElementById("chat-thread");
const chatHint = document.getElementById("chat-hint");
let stopChatStream = null;

function appendMessage(role, text) {
  const bubble = document.createElement("div");
  bubble.className = `bubble ${role}`;
  bubble.textContent = text;
  chatThread.appendChild(bubble);
  chatThread.scrollTop = chatThread.scrollHeight;
}

async function sendChat(message) {
  const includeTodo = document.getElementById("include-todo").checked;
  const includeProgress = document.getElementById("include-progress").checked;
  const includeOpinions = document.getElementById("include-opinions").checked;
  const button = chatForm.querySelector("button[type=submit]");
  button.disabled = true;
  appendMessage("user", message);

  const botBubble = document.createElement("div");
  botBubble.className = "bubble codex";
  chatThread.appendChild(botBubble);
  chatThread.scrollTop = chatThread.scrollHeight;
  chatHint.textContent = "Streaming response...";

  if (stopChatStream) {
    stopChatStream();
    stopChatStream = null;
  }

  stopChatStream = streamEvents("/api/chat/stream", {
    method: "POST",
    body: {
      message,
      include_todo: includeTodo,
      include_progress: includeProgress,
      include_opinions: includeOpinions,
    },
    onMessage: (data, event) => {
      if (event === "done") {
        if (data && data !== "0") {
          flash(`Chat finished with exit ${data}`);
        }
        button.disabled = false;
        chatHint.textContent = "Responses stream live from Codex.";
        if (stopChatStream) {
          stopChatStream();
          stopChatStream = null;
        }
        return;
      }
      if (event === "error") {
        botBubble.textContent = `Error: ${data}`;
        button.disabled = false;
        chatHint.textContent = "Responses stream live from Codex.";
        if (stopChatStream) {
          stopChatStream();
          stopChatStream = null;
        }
        return;
      }
      botBubble.textContent = botBubble.textContent ? `${botBubble.textContent}\n${data}` : data;
      chatThread.scrollTop = chatThread.scrollHeight;
    },
    onError: (err) => {
      botBubble.textContent = `Error: ${err.message}`;
      button.disabled = false;
      chatHint.textContent = "Responses stream live from Codex.";
      stopChatStream = null;
    },
    onFinish: () => {
      stopChatStream = null;
    },
  });
}

chatForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const message = chatInput.value.trim();
  if (!message) return;
  chatInput.value = "";
  await sendChat(message);
});

loadState();
loadDocs();
loadLogs();
