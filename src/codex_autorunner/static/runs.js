import { api, flash, openModal } from "./utils.js";

let initialized = false;
let runsState = {
  runs: [],
  attributionMode: "split",
  todoSearch: "",
};
let closeDetailsModal = null;

const ui = {
  refresh: document.getElementById("runs-refresh"),
  tableBody: document.getElementById("runs-table-body"),
  todoList: document.getElementById("runs-todo-list"),
  todoSummary: document.getElementById("runs-todo-summary"),
  todoSearch: document.getElementById("runs-todo-search"),
  attribution: document.getElementById("runs-attribution-mode"),
  modal: document.getElementById("run-details-modal"),
  modalClose: document.getElementById("run-details-close"),
  modalTitle: document.getElementById("run-details-title"),
  modalMeta: document.getElementById("run-details-meta"),
  modalTodos: document.getElementById("run-details-todos"),
  modalTokens: document.getElementById("run-details-tokens"),
  modalPlan: document.getElementById("run-details-plan"),
  modalDiff: document.getElementById("run-details-diff"),
  modalLog: document.getElementById("run-details-log"),
};

const STATUS_LABELS = {
  ok: "ok",
  error: "error",
  running: "running",
};

function formatTimestamp(ts) {
  if (!ts) return "–";
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return ts;
  return date.toLocaleString();
}

function formatDuration(seconds) {
  if (typeof seconds !== "number" || Number.isNaN(seconds)) return "–";
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const mins = Math.floor(seconds / 60);
  const secs = Math.round(seconds % 60);
  return `${mins}m ${secs}s`;
}

function formatNumber(value) {
  if (typeof value !== "number" || Number.isNaN(value)) return "–";
  return value.toLocaleString();
}

function runStatus(entry) {
  if (entry.exit_code === 0) return "ok";
  if (entry.exit_code != null) return "error";
  return entry.finished_at ? "error" : "running";
}

function renderRunsTable() {
  if (!ui.tableBody) return;
  ui.tableBody.innerHTML = "";
  if (!runsState.runs.length) {
    const row = document.createElement("tr");
    row.innerHTML =
      '<td colspan="6" class="runs-empty">No runs recorded yet.</td>';
    ui.tableBody.appendChild(row);
    return;
  }
  runsState.runs.forEach((run) => {
    const status = runStatus(run);
    const row = document.createElement("tr");
    row.className = "runs-row";
    row.dataset.runId = run.run_id;
    row.innerHTML = `
      <td>#${run.run_id}</td>
      <td><span class="runs-pill runs-pill-${status}">${STATUS_LABELS[status]}</span></td>
      <td>${formatTimestamp(run.started_at)}</td>
      <td>${run.app_server?.model || "–"}</td>
      <td>${formatNumber(run.token_total)}</td>
      <td>${formatNumber(run.completed_todo_count || 0)}</td>
    `;
    row.addEventListener("click", () => openRunDetails(run));
    ui.tableBody.appendChild(row);
  });
}

function computeTodoAttribution() {
  const map = new Map();
  runsState.runs.forEach((run) => {
    const completed = run.todo?.completed;
    if (!Array.isArray(completed) || completed.length === 0) return;
    const runTokens = typeof run.token_total === "number" ? run.token_total : 0;
    const perTodo =
      runsState.attributionMode === "split"
        ? runTokens / completed.length
        : runTokens;
    completed.forEach((item) => {
      if (!item) return;
      const key = String(item);
      if (!map.has(key)) {
        map.set(key, {
          text: key,
          tokens: 0,
          count: 0,
          runs: new Set(),
        });
      }
      const entry = map.get(key);
      entry.tokens += perTodo;
      entry.count += 1;
      entry.runs.add(run.run_id);
    });
  });
  const list = Array.from(map.values()).map((entry) => ({
    text: entry.text,
    tokens: entry.tokens,
    count: entry.count,
    runs: Array.from(entry.runs).sort((a, b) => b - a),
  }));
  list.sort((a, b) => b.tokens - a.tokens);
  return list;
}

function renderTodoAnalytics() {
  if (!ui.todoList) return;
  const list = computeTodoAttribution();
  const filtered = list.filter((entry) =>
    entry.text.toLowerCase().includes(runsState.todoSearch.toLowerCase())
  );
  ui.todoList.innerHTML = "";
  if (!filtered.length) {
    ui.todoList.textContent = "No TODOs yet.";
  } else {
    filtered.forEach((entry) => {
      const item = document.createElement("div");
      item.className = "runs-todo-item";
      const runsMarkup = entry.runs
        .map(
          (runId) =>
            `<button class="runs-chip" data-run-id="${runId}" title="Open run #${runId}">#${runId}</button>`
        )
        .join("");
      item.innerHTML = `
        <div class="runs-todo-text">${entry.text}</div>
        <div class="runs-todo-meta">
          <span>${formatNumber(entry.tokens)} tokens</span>
          <span>${entry.count} completions</span>
          <span class="runs-todo-runs">${runsMarkup}</span>
        </div>
      `;
      item.querySelectorAll(".runs-chip").forEach((btn) => {
        btn.addEventListener("click", (event) => {
          event.stopPropagation();
          const id = Number(btn.dataset.runId);
          const run = runsState.runs.find((candidate) => candidate.run_id === id);
          if (run) openRunDetails(run);
        });
      });
      ui.todoList.appendChild(item);
    });
  }
  if (ui.todoSummary) {
    ui.todoSummary.textContent = `${filtered.length} TODOs`;
  }
}

async function loadArtifact(runId, kind, targetEl) {
  if (!targetEl) return;
  targetEl.textContent = "Loading…";
  try {
    const text = await api(`/api/runs/${runId}/${kind}`);
    targetEl.textContent = text || "Not available.";
  } catch (err) {
    const message = err instanceof Error ? err.message : "";
    targetEl.textContent =
      message.includes("401") || message.includes("403") || message.includes("404")
        ? "Not available."
        : "Unable to load.";
  }
}

function renderTokenBreakdown(run) {
  if (!ui.modalTokens) return;
  const usage = run.token_usage?.delta;
  if (!usage || typeof usage !== "object") {
    ui.modalTokens.textContent = "Token data unavailable.";
    return;
  }
  ui.modalTokens.innerHTML = `
    <div>Input: ${formatNumber(usage.input_tokens || usage.inputTokens)}</div>
    <div>Cached input: ${formatNumber(
      usage.cached_input_tokens || usage.cachedInputTokens
    )}</div>
    <div>Output: ${formatNumber(
      usage.output_tokens || usage.outputTokens
    )}</div>
    <div>Reasoning: ${formatNumber(
      usage.reasoning_output_tokens || usage.reasoningOutputTokens
    )}</div>
    <div>Total: ${formatNumber(
      usage.total_tokens || usage.totalTokens || usage.total
    )}</div>
  `;
}

function renderCompletedTodos(run) {
  if (!ui.modalTodos) return;
  const completed = Array.isArray(run.todo?.completed) ? run.todo.completed : [];
  ui.modalTodos.innerHTML = "";
  if (!completed.length) {
    ui.modalTodos.textContent = "No TODOs completed in this run.";
    return;
  }
  const list = document.createElement("ul");
  completed.forEach((item) => {
    const li = document.createElement("li");
    li.textContent = item;
    li.addEventListener("click", () => {
      if (ui.todoSearch) {
        ui.todoSearch.value = item;
        runsState.todoSearch = item;
        renderTodoAnalytics();
      }
    });
    list.appendChild(li);
  });
  ui.modalTodos.appendChild(list);
}

function openRunDetails(run) {
  if (!ui.modal) return;
  if (closeDetailsModal) {
    closeDetailsModal();
  }
  ui.modalTitle.textContent = `Run #${run.run_id}`;
  ui.modalMeta.textContent = `Started ${formatTimestamp(
    run.started_at
  )} · Duration ${formatDuration(run.duration_seconds)} · Status ${
    runStatus(run) || "–"
  }`;
  if (ui.modalLog) {
    ui.modalLog.href = `/api/logs?run_id=${run.run_id}`;
  }
  renderTokenBreakdown(run);
  renderCompletedTodos(run);
  if (ui.modalPlan) loadArtifact(run.run_id, "plan", ui.modalPlan);
  if (ui.modalDiff) loadArtifact(run.run_id, "diff", ui.modalDiff);
  const triggerEl = document.activeElement;
  closeDetailsModal = openModal(ui.modal, {
    initialFocus: ui.modalClose || ui.modal,
    returnFocusTo: triggerEl,
  });
}

async function loadRuns() {
  try {
    const data = await api("/api/runs?limit=200");
    runsState.runs = Array.isArray(data?.runs) ? data.runs : [];
    renderRunsTable();
    renderTodoAnalytics();
  } catch (err) {
    flash(err.message || "Failed to load runs", "error");
  }
}

export function initRuns() {
  if (initialized) return;
  initialized = true;

  if (ui.refresh) {
    ui.refresh.addEventListener("click", () => loadRuns());
  }
  if (ui.todoSearch) {
    ui.todoSearch.addEventListener("input", () => {
      runsState.todoSearch = ui.todoSearch.value || "";
      renderTodoAnalytics();
    });
  }
  if (ui.attribution) {
    ui.attribution.value = runsState.attributionMode;
    ui.attribution.addEventListener("change", () => {
      runsState.attributionMode = ui.attribution.value || "split";
      renderTodoAnalytics();
    });
  }
  if (ui.modalClose) {
    ui.modalClose.addEventListener("click", () => {
      if (closeDetailsModal) closeDetailsModal();
    });
  }

  loadRuns();
}
