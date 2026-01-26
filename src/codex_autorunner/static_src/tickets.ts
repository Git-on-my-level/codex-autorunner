import { api, flash, getUrlParams, resolvePath, statusPill, getAuthToken, openModal } from "./utils.js";
import { registerAutoRefresh } from "./autoRefresh.js";
import { CONSTANTS } from "./constants.js";
import { subscribe } from "./bus.js";
import { isRepoHealthy } from "./health.js";
import { closeTicketEditor, initTicketEditor, openTicketEditor, TicketData } from "./ticketEditor.js";

type FlowEvent = {
  event_type: string;
  timestamp: string;
  data?: Record<string, unknown>;
  step_id?: string;
};

type FlowRun = {
  id?: string;
  status?: string;
  state?: Record<string, unknown>;
  error_message?: string | null;
  started_at?: string | null;
};

type BootstrapResponse = FlowRun & {
  state?: Record<string, unknown> & { hint?: string };
};

type TicketFile = {
  path?: string;
  index?: number | null;
  frontmatter?: Record<string, unknown> | null;
  body?: string | null;
  errors?: string[];
};

type HandoffAttachment = {
  name?: string;
  rel_path?: string;
  path?: string;
  size?: number | null;
  url?: string;
};

type HandoffEntry = {
  seq?: string;
  message?: Record<string, unknown> | null;
  errors?: string[];
  attachments?: HandoffAttachment[];
};

let currentRunId: string | null = null;
let ticketsExist = false;
let currentActiveTicket: string | null = null;
let currentFlowStatus: string | null = null;
let elapsedTimerId: ReturnType<typeof setInterval> | null = null;
let flowStartedAt: Date | null = null;
let eventSource: EventSource | null = null;
let lastActivityTime: Date | null = null;
let lastActivityTimerId: ReturnType<typeof setInterval> | null = null;
let liveOutputExpanded = false;
let liveOutputBuffer: string[] = [];
const MAX_OUTPUT_LINES = 200;
let currentReasonFull: string | null = null; // Full reason text for modal display

function formatElapsed(startTime: Date): string {
  const now = new Date();
  const diffMs = now.getTime() - startTime.getTime();
  const diffSecs = Math.floor(diffMs / 1000);
  
  if (diffSecs < 60) {
    return `${diffSecs}s`;
  }
  const mins = Math.floor(diffSecs / 60);
  const secs = diffSecs % 60;
  if (mins < 60) {
    return `${mins}m ${secs}s`;
  }
  const hours = Math.floor(mins / 60);
  const remainingMins = mins % 60;
  return `${hours}h ${remainingMins}m`;
}

function startElapsedTimer(): void {
  stopElapsedTimer();
  if (!flowStartedAt) return;
  
  const update = () => {
    const { elapsed } = els();
    if (elapsed && flowStartedAt) {
      elapsed.textContent = formatElapsed(flowStartedAt);
    }
  };
  
  update(); // Update immediately
  elapsedTimerId = setInterval(update, 1000);
}

function stopElapsedTimer(): void {
  if (elapsedTimerId) {
    clearInterval(elapsedTimerId);
    elapsedTimerId = null;
  }
}

// ---- SSE Event Stream Functions ----

function formatTimeAgo(timestamp: Date): string {
  const now = new Date();
  const diffMs = now.getTime() - timestamp.getTime();
  const diffSecs = Math.floor(diffMs / 1000);
  
  if (diffSecs < 5) return "just now";
  if (diffSecs < 60) return `${diffSecs}s ago`;
  const mins = Math.floor(diffSecs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  return `${hours}h ago`;
}

function updateLastActivityDisplay(): void {
  const el = document.getElementById("ticket-flow-last-activity");
  if (el && lastActivityTime) {
    el.textContent = formatTimeAgo(lastActivityTime);
  }
}

function startLastActivityTimer(): void {
  stopLastActivityTimer();
  updateLastActivityDisplay();
  lastActivityTimerId = setInterval(updateLastActivityDisplay, 1000);
}

function stopLastActivityTimer(): void {
  if (lastActivityTimerId) {
    clearInterval(lastActivityTimerId);
    lastActivityTimerId = null;
  }
}

function appendToLiveOutput(text: string): void {
  const outputEl = document.getElementById("ticket-live-output-text");
  if (!outputEl) return;
  
  // Add new lines to buffer
  const newLines = text.split("\n");
  liveOutputBuffer.push(...newLines);
  
  // Trim buffer if it exceeds max lines
  while (liveOutputBuffer.length > MAX_OUTPUT_LINES) {
    liveOutputBuffer.shift();
  }
  
  // Update display
  outputEl.textContent = liveOutputBuffer.join("\n");
  
  // Auto-scroll to bottom
  const contentEl = document.getElementById("ticket-live-output-content");
  if (contentEl) {
    contentEl.scrollTop = contentEl.scrollHeight;
  }
}

function clearLiveOutput(): void {
  liveOutputBuffer = [];
  const outputEl = document.getElementById("ticket-live-output-text");
  if (outputEl) outputEl.textContent = "";
}

function setLiveOutputStatus(status: "disconnected" | "connected" | "streaming"): void {
  const statusEl = document.getElementById("ticket-live-output-status");
  if (!statusEl) return;
  
  statusEl.className = "ticket-live-output-status";
  switch (status) {
    case "disconnected":
      statusEl.textContent = "Disconnected";
      break;
    case "connected":
      statusEl.textContent = "Connected";
      statusEl.classList.add("connected");
      break;
    case "streaming":
      statusEl.textContent = "Streaming";
      statusEl.classList.add("streaming");
      break;
  }
}

function handleFlowEvent(event: FlowEvent): void {
  // Update last activity time
  lastActivityTime = new Date(event.timestamp);
  updateLastActivityDisplay();
  
  // Handle agent stream delta events
  if (event.event_type === "agent_stream_delta") {
    setLiveOutputStatus("streaming");
    const delta = event.data?.delta as string || "";
    if (delta) {
      appendToLiveOutput(delta);
    }
  }
  
  // Handle flow lifecycle events
  if (event.event_type === "flow_completed" || 
      event.event_type === "flow_failed" || 
      event.event_type === "flow_stopped") {
    setLiveOutputStatus("connected");
    // Refresh the flow state
    void loadTicketFlow();
  }
  
  // Handle step events
  if (event.event_type === "step_started") {
    const stepName = event.data?.step_name as string || "";
    if (stepName) {
      appendToLiveOutput(`\n--- Step: ${stepName} ---\n`);
    }
  }
}

function connectEventStream(runId: string): void {
  disconnectEventStream();
  
  const token = getAuthToken();
  let url = resolvePath(`/api/flows/${runId}/events`);
  if (token) {
    url += `?token=${encodeURIComponent(token)}`;
  }
  
  eventSource = new EventSource(url);
  
  eventSource.onopen = () => {
    setLiveOutputStatus("connected");
  };
  
  eventSource.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data) as FlowEvent;
      handleFlowEvent(data);
    } catch (err) {
      // Ignore parse errors
    }
  };
  
  eventSource.onerror = () => {
    setLiveOutputStatus("disconnected");
    // Don't auto-reconnect here - loadTicketFlow will handle it
  };
}

function disconnectEventStream(): void {
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
  setLiveOutputStatus("disconnected");
}

function initLiveOutputPanel(): void {
  const toggleBtn = document.getElementById("ticket-live-output-toggle");
  const expandBtn = document.getElementById("ticket-live-output-expand");
  const contentEl = document.getElementById("ticket-live-output-content");
  const panelEl = document.getElementById("ticket-live-output-panel");
  
  const toggle = () => {
    liveOutputExpanded = !liveOutputExpanded;
    if (contentEl) {
      contentEl.classList.toggle("hidden", !liveOutputExpanded);
    }
    if (panelEl) {
      panelEl.classList.toggle("expanded", liveOutputExpanded);
    }
  };
  
  if (toggleBtn) {
    toggleBtn.addEventListener("click", toggle);
  }
  if (expandBtn) {
    expandBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      toggle();
    });
  }
}

/**
 * Initialize the reason modal click handler.
 */
function initReasonModal(): void {
  const reasonEl = document.getElementById("ticket-flow-reason");
  const modalOverlay = document.getElementById("reason-modal");
  const modalContent = document.getElementById("reason-modal-content");
  const closeBtn = document.getElementById("reason-modal-close");

  if (!reasonEl || !modalOverlay || !modalContent) return;

  let closeModal: (() => void) | null = null;

  const showReasonModal = () => {
    if (!currentReasonFull || !reasonEl.classList.contains("has-details")) return;
    modalContent.textContent = currentReasonFull;
    closeModal = openModal(modalOverlay, {
      closeOnEscape: true,
      closeOnOverlay: true,
      returnFocusTo: reasonEl,
    });
  };

  reasonEl.addEventListener("click", showReasonModal);

  if (closeBtn) {
    closeBtn.addEventListener("click", () => {
      if (closeModal) closeModal();
    });
  }
}

function els(): {
  card: HTMLElement | null;
  status: HTMLElement | null;
  run: HTMLElement | null;
  current: HTMLElement | null;
  turn: HTMLElement | null;
  elapsed: HTMLElement | null;
  progress: HTMLElement | null;
  reason: HTMLElement | null;
  lastActivity: HTMLElement | null;
  dir: HTMLElement | null;
  tickets: HTMLElement | null;
  history: HTMLElement | null;
  handoffNote: HTMLElement | null;
  bootstrapBtn: HTMLButtonElement | null;
  resumeBtn: HTMLButtonElement | null;
  refreshBtn: HTMLButtonElement | null;
  stopBtn: HTMLButtonElement | null;
  restartBtn: HTMLButtonElement | null;
  archiveBtn: HTMLButtonElement | null;
} {
  return {
    card: document.getElementById("ticket-card"),
    status: document.getElementById("ticket-flow-status"),
    run: document.getElementById("ticket-flow-run"),
    current: document.getElementById("ticket-flow-current"),
    turn: document.getElementById("ticket-flow-turn"),
    elapsed: document.getElementById("ticket-flow-elapsed"),
    progress: document.getElementById("ticket-flow-progress"),
    reason: document.getElementById("ticket-flow-reason"),
    lastActivity: document.getElementById("ticket-flow-last-activity"),
    dir: document.getElementById("ticket-flow-dir"),
    tickets: document.getElementById("ticket-flow-tickets"),
    history: document.getElementById("ticket-handoff-history"),
    handoffNote: document.getElementById("ticket-handoff-note"),
    bootstrapBtn: document.getElementById("ticket-flow-bootstrap") as HTMLButtonElement | null,
    resumeBtn: document.getElementById("ticket-flow-resume") as HTMLButtonElement | null,
    refreshBtn: document.getElementById("ticket-flow-refresh") as HTMLButtonElement | null,
    stopBtn: document.getElementById("ticket-flow-stop") as HTMLButtonElement | null,
    restartBtn: document.getElementById("ticket-flow-restart") as HTMLButtonElement | null,
    archiveBtn: document.getElementById("ticket-flow-archive") as HTMLButtonElement | null,
  };
}

function setButtonsDisabled(disabled: boolean): void {
  const { bootstrapBtn, resumeBtn, refreshBtn, stopBtn, restartBtn, archiveBtn } = els();
  [bootstrapBtn, resumeBtn, refreshBtn, stopBtn, restartBtn, archiveBtn].forEach((btn) => {
    if (btn) btn.disabled = disabled;
  });
}

function truncate(text: string, max = 220): string {
  if (text.length <= max) return text;
  return `${text.slice(0, max).trim()}…`;
}

function renderTickets(data: { ticket_dir?: string; tickets?: TicketFile[] } | null): void {
  const { tickets, dir, bootstrapBtn } = els();
  if (dir) dir.textContent = data?.ticket_dir || "–";
  if (!tickets) return;
  tickets.innerHTML = "";

  const list = (data?.tickets || []) as TicketFile[];
  ticketsExist = list.length > 0;

  // Disable start button if no tickets exist
  if (bootstrapBtn && !bootstrapBtn.disabled) {
    bootstrapBtn.disabled = !ticketsExist;
    if (!ticketsExist) {
      bootstrapBtn.title = "Create a ticket first";
    } else {
      bootstrapBtn.title = "";
    }
  }

  if (!list.length) {
    tickets.textContent = "No tickets found. Create TICKET-001.md to begin.";
    return;
  }

  list.forEach((ticket) => {
    const item = document.createElement("div");
    const fm = (ticket.frontmatter || {}) as Record<string, unknown>;
    const done = Boolean(fm?.done);
    // Check if this ticket is currently being worked on
    const isActive = currentActiveTicket && ticket.path === currentActiveTicket && currentFlowStatus === "running";
    item.className = `ticket-item ${done ? "done" : ""} ${isActive ? "active" : ""} clickable`;
    item.title = "Click to edit";

    // Make ticket item clickable to open editor
    item.addEventListener("click", () => {
      openTicketEditor(ticket as TicketData);
    });

    const head = document.createElement("div");
    head.className = "ticket-item-head";
    const name = document.createElement("span");
    name.className = "ticket-name";
    name.textContent = ticket.path || "TICKET";
    const agent = document.createElement("span");
    agent.className = "ticket-agent";
    agent.textContent = (fm?.agent as string) || "codex";
    head.appendChild(name);
    head.appendChild(agent);
    // Add WORKING badge for active ticket
    if (isActive) {
      const workingBadge = document.createElement("span");
      workingBadge.className = "ticket-working-badge";
      workingBadge.textContent = "Working";
      head.appendChild(workingBadge);
    }
    item.appendChild(head);

    if (fm?.title) {
      const title = document.createElement("div");
      title.className = "ticket-body";
      title.textContent = String(fm.title);
      item.appendChild(title);
    }

    if (ticket.errors && ticket.errors.length) {
      const errors = document.createElement("div");
      errors.className = "ticket-errors";
      errors.textContent = `Frontmatter issues: ${ticket.errors.join("; ")}`;
      item.appendChild(errors);
    }

    if (ticket.body) {
      const body = document.createElement("div");
      body.className = "ticket-body";
      body.textContent = truncate(ticket.body.replace(/\s+/g, " ").trim());
      item.appendChild(body);
    }

    tickets.appendChild(item);
  });
}

function renderHandoffHistory(
  runId: string | null,
  data: { history?: HandoffEntry[] } | null
): void {
  const { history, handoffNote } = els();
  if (!history) return;
  history.innerHTML = "";

  if (!runId) {
    history.textContent = "Start the ticket flow to see user handoffs.";
    if (handoffNote) handoffNote.textContent = "–";
    return;
  }

  const entries = (data?.history || []) as HandoffEntry[];
  if (!entries.length) {
    history.textContent = "No handoffs yet.";
    if (handoffNote) handoffNote.textContent = "–";
    return;
  }

  if (handoffNote) handoffNote.textContent = `Latest #${entries[0]?.seq ?? "–"}`;

  entries.forEach((entry) => {
    const container = document.createElement("div");
    container.className = "ticket-item";

    const head = document.createElement("div");
    head.className = "ticket-item-head";
    const seq = document.createElement("span");
    seq.className = "ticket-name";
    seq.textContent = `#${entry.seq || "?"}`;
    const mode = document.createElement("span");
    mode.className = "ticket-agent";
    mode.textContent = ((entry.message?.mode as string) || "notify").toUpperCase();
    head.append(seq, mode);
    container.appendChild(head);

    if (entry.errors && entry.errors.length) {
      const err = document.createElement("div");
      err.className = "ticket-errors";
      err.textContent = entry.errors.join("; ");
      container.appendChild(err);
    }

    const title = entry.message?.title as string | undefined;
    if (title) {
      const titleEl = document.createElement("div");
      titleEl.className = "ticket-body";
      titleEl.textContent = title;
      container.appendChild(titleEl);
    }

    const bodyText = entry.message?.body as string | undefined;
    if (bodyText) {
      const body = document.createElement("div");
      body.className = "ticket-body";
      body.textContent = truncate(bodyText.replace(/\s+/g, " ").trim());
      container.appendChild(body);
    }

    const attachments = (entry.attachments || []) as HandoffAttachment[];
    if (attachments.length) {
      const wrap = document.createElement("div");
      wrap.className = "ticket-attachments";
      attachments.forEach((att) => {
        if (!att.url) return;
        const link = document.createElement("a");
        link.href = resolvePath(att.url);
        link.textContent = att.name || att.rel_path || "attachment";
        link.target = "_blank";
        link.rel = "noreferrer noopener";
        link.title = att.path || "";
        wrap.appendChild(link);
      });
      container.appendChild(wrap);
    }

    history.appendChild(container);
  });
}

const MAX_REASON_LENGTH = 60;

/**
 * Get the full reason text (summary + details) for modal display.
 */
function getFullReason(run: FlowRun | null): string | null {
  if (!run) return null;
  const state = (run.state || {}) as Record<string, unknown>;
  const engine = (state.ticket_engine || {}) as Record<string, unknown>;
  const reason = (engine.reason as string) || (run.error_message as string) || "";
  const details = (engine.reason_details as string) || "";
  if (!reason && !details) return null;
  if (details) {
    return `${reason}\n\n${details}`.trim();
  }
  return reason;
}

/**
 * Get a truncated reason summary for display in the grid.
 * Also updates currentReasonFull for modal access.
 */
function summarizeReason(run: FlowRun | null): string {
  if (!run) {
    currentReasonFull = null;
    return "No ticket flow run yet.";
  }
  const state = (run.state || {}) as Record<string, unknown>;
  const engine = (state.ticket_engine || {}) as Record<string, unknown>;
  const fullReason = getFullReason(run);
  currentReasonFull = fullReason;
  const shortReason =
    (engine.reason as string) ||
    (run.error_message as string) ||
    (engine.current_ticket ? `Working on ${engine.current_ticket}` : "") ||
    run.status ||
    "";
  // Truncate if too long
  if (shortReason.length > MAX_REASON_LENGTH) {
    return shortReason.slice(0, MAX_REASON_LENGTH - 3) + "...";
  }
  return shortReason;
}

async function loadTicketFiles(): Promise<void> {
  const { tickets } = els();
  if (tickets) tickets.textContent = "Loading tickets…";
  try {
    const data = (await api("/api/flows/ticket_flow/tickets")) as {
      ticket_dir?: string;
      tickets?: TicketFile[];
    };
    renderTickets(data);
  } catch (err) {
    renderTickets(null);
    flash((err as Error).message || "Failed to load tickets", "error");
  }
}

/**
 * Open a ticket by its index
 */
async function openTicketByIndex(index: number): Promise<void> {
  try {
    const data = (await api("/api/flows/ticket_flow/tickets")) as {
      tickets?: TicketFile[];
    };
    const ticket = data.tickets?.find((t) => t.index === index);
    if (ticket) {
      openTicketEditor(ticket as TicketData);
    } else {
      flash(`Ticket TICKET-${String(index).padStart(3, "0")} not found`, "error");
    }
  } catch (err) {
    flash(`Failed to open ticket: ${(err as Error).message}`, "error");
  }
}

async function loadHandoffHistory(runId: string | null): Promise<void> {
  const { history } = els();
  if (history) history.textContent = "Loading handoff history…";
  if (!runId) {
    renderHandoffHistory(null, null);
    return;
  }
  try {
    const data = (await api(`/api/flows/${runId}/handoff_history`)) as {
      history?: HandoffEntry[];
    };
    renderHandoffHistory(runId, data);
  } catch (err) {
    renderHandoffHistory(runId, null);
    flash((err as Error).message || "Failed to load handoff history", "error");
  }
}

async function loadTicketFlow(): Promise<void> {
  const { status, run, current, turn, elapsed, progress, reason, lastActivity, resumeBtn, bootstrapBtn, stopBtn, archiveBtn } = els();
  if (!isRepoHealthy()) {
    if (status) statusPill(status, "error");
    if (run) run.textContent = "–";
    if (current) current.textContent = "–";
    if (turn) turn.textContent = "–";
    if (elapsed) elapsed.textContent = "–";
    if (progress) progress.textContent = "–";
    if (lastActivity) lastActivity.textContent = "–";
    if (reason) reason.textContent = "Repo offline or uninitialized.";
    setButtonsDisabled(true);
    stopElapsedTimer();
    stopLastActivityTimer();
    disconnectEventStream();
    return;
  }
  try {
    const runs = (await api("/api/flows/runs?flow_type=ticket_flow")) as FlowRun[];
    const latest = (runs && runs[0]) || null;
    currentRunId = (latest?.id as string) || null;
    currentFlowStatus = (latest?.status as string) || null;
    
    // Extract ticket engine state
    const ticketEngine = (latest?.state as Record<string, unknown> | undefined)?.ticket_engine as
      | Record<string, unknown>
      | undefined;
    currentActiveTicket = (ticketEngine?.current_ticket as string) || null;
    const ticketTurns = (ticketEngine?.ticket_turns as number) ?? null;
    const totalTurns = (ticketEngine?.total_turns as number) ?? null;

    if (status) statusPill(status, (latest?.status as string) || "idle");
    if (run) run.textContent = latest?.id || "–";
    if (current)
      current.textContent = currentActiveTicket || "–";
    
    // Display turn counter
    if (turn) {
      if (ticketTurns !== null && currentFlowStatus === "running") {
        turn.textContent = `${ticketTurns}${totalTurns !== null ? ` (${totalTurns} total)` : ""}`;
      } else {
        turn.textContent = "–";
      }
    }
    
    // Handle elapsed time
    if (latest?.started_at && (latest.status === "running" || latest.status === "pending")) {
      flowStartedAt = new Date(latest.started_at);
      startElapsedTimer();
    } else {
      stopElapsedTimer();
      flowStartedAt = null;
      if (elapsed) elapsed.textContent = "–";
    }
    
    if (reason) {
      reason.textContent = summarizeReason(latest) || "–";
      // Add clickable class if there are details to show
      const state = (latest?.state || {}) as Record<string, unknown>;
      const engine = (state.ticket_engine || {}) as Record<string, unknown>;
      const hasDetails = Boolean(
        engine.reason_details ||
          (currentReasonFull && currentReasonFull.length > MAX_REASON_LENGTH)
      );
      reason.classList.toggle("has-details", hasDetails);
    }

    if (resumeBtn) {
      resumeBtn.disabled = !latest?.id || latest.status !== "paused";
    }
    if (stopBtn) {
      const stoppable =
        latest?.status === "running" || latest?.status === "pending";
      stopBtn.disabled = !latest?.id || !stoppable;
    }
    await loadTicketFiles();
    
    // Calculate and display ticket progress
    if (progress) {
      const doneCount = ticketsExist ? 
        document.querySelectorAll(".ticket-item.done").length : 0;
      const totalCount = ticketsExist ? 
        document.querySelectorAll(".ticket-item").length : 0;
      if (totalCount > 0) {
        progress.textContent = `${doneCount} of ${totalCount} done`;
      } else {
        progress.textContent = "–";
      }
    }
    
    // Connect/disconnect event stream based on flow status
    if (currentRunId && (latest?.status === "running" || latest?.status === "pending")) {
      // Only connect if not already connected to this run
      if (!eventSource || eventSource.url?.indexOf(currentRunId) === -1) {
        connectEventStream(currentRunId);
        startLastActivityTimer();
      }
    } else {
      disconnectEventStream();
      stopLastActivityTimer();
      if (lastActivity) lastActivity.textContent = "–";
      lastActivityTime = null;
    }

    if (bootstrapBtn) {
      const busy = latest?.status === "running" || latest?.status === "pending";
      // Disable if busy OR if no tickets exist
      bootstrapBtn.disabled = busy || !ticketsExist;
      bootstrapBtn.textContent = busy ? "Running…" : "Start Ticket Flow";
      if (!ticketsExist && !busy) {
        bootstrapBtn.title = "Create a ticket first";
      } else {
        bootstrapBtn.title = "";
      }
    }
    
    // Show restart button when flow is paused or in terminal state (allows starting fresh)
    const { restartBtn } = els();
    if (restartBtn) {
      const isPaused = latest?.status === "paused";
      const isTerminal =
        latest?.status === "completed" ||
        latest?.status === "stopped" ||
        latest?.status === "failed";
      const canRestart = (isPaused || isTerminal) && ticketsExist && Boolean(currentRunId);
      restartBtn.style.display = canRestart ? "" : "none";
      restartBtn.disabled = !canRestart;
    }
    
    // Show archive button when flow is paused or in terminal state and has tickets
    if (archiveBtn) {
      const isPaused = latest?.status === "paused";
      const isTerminal =
        latest?.status === "completed" ||
        latest?.status === "stopped" ||
        latest?.status === "failed";
      const canArchive = (isPaused || isTerminal) && ticketsExist && Boolean(currentRunId);
      archiveBtn.style.display = canArchive ? "" : "none";
      archiveBtn.disabled = !canArchive;
    }
    await loadHandoffHistory(currentRunId);
  } catch (err) {
    if (reason) reason.textContent = (err as Error).message || "Ticket flow unavailable";
    flash((err as Error).message || "Failed to load ticket flow state", "error");
  }
}

async function bootstrapTicketFlow(): Promise<void> {
  const { bootstrapBtn } = els();
  if (!bootstrapBtn) return;
  if (!isRepoHealthy()) {
    flash("Repo offline; cannot start ticket flow.", "error");
    return;
  }
  if (!ticketsExist) {
    flash("Create a ticket first before starting the flow.", "error");
    return;
  }
  setButtonsDisabled(true);
  bootstrapBtn.textContent = "Starting…";
  try {
    const res = (await api("/api/flows/ticket_flow/bootstrap", {
      method: "POST",
      body: {},
    })) as BootstrapResponse;
    currentRunId = res?.id || null;
    if (res?.state?.hint === "active_run_reused") {
      flash("Ticket flow already running; continuing existing run", "info");
    } else {
      flash("Ticket flow started");
      clearLiveOutput(); // Clear output for new run
    }
    await loadTicketFlow();
  } catch (err) {
    flash((err as Error).message || "Failed to start ticket flow", "error");
  } finally {
    bootstrapBtn.textContent = "Start Ticket Flow";
    setButtonsDisabled(false);
  }
}

async function resumeTicketFlow(): Promise<void> {
  const { resumeBtn } = els();
  if (!resumeBtn) return;
  if (!isRepoHealthy()) {
    flash("Repo offline; cannot resume ticket flow.", "error");
    return;
  }
  if (!currentRunId) {
    flash("No ticket flow run to resume", "info");
    return;
  }
  setButtonsDisabled(true);
  resumeBtn.textContent = "Resuming…";
  try {
    await api(`/api/flows/${currentRunId}/resume`, { method: "POST", body: {} });
    flash("Ticket flow resumed");
    await loadTicketFlow();
  } catch (err) {
    flash((err as Error).message || "Failed to resume", "error");
  } finally {
    resumeBtn.textContent = "Resume";
    setButtonsDisabled(false);
  }
}

async function stopTicketFlow(): Promise<void> {
  const { stopBtn } = els();
  if (!stopBtn) return;
  if (!isRepoHealthy()) {
    flash("Repo offline; cannot stop ticket flow.", "error");
    return;
  }
  if (!currentRunId) {
    flash("No ticket flow run to stop", "info");
    return;
  }
  setButtonsDisabled(true);
  stopBtn.textContent = "Stopping…";
  try {
    await api(`/api/flows/${currentRunId}/stop`, { method: "POST", body: {} });
    flash("Ticket flow stopping");
    await loadTicketFlow();
  } catch (err) {
    flash((err as Error).message || "Failed to stop ticket flow", "error");
  } finally {
    stopBtn.textContent = "Stop";
    setButtonsDisabled(false);
  }
}

async function restartTicketFlow(): Promise<void> {
  const { restartBtn } = els();
  if (!restartBtn) return;
  if (!isRepoHealthy()) {
    flash("Repo offline; cannot restart ticket flow.", "error");
    return;
  }
  if (!ticketsExist) {
    flash("Create a ticket first before restarting the flow.", "error");
    return;
  }
  if (!confirm("Restart ticket flow? This will stop the current run and start a new one.")) {
    return;
  }
  setButtonsDisabled(true);
  restartBtn.textContent = "Restarting…";
  try {
    // Stop the current run first if it exists
    if (currentRunId) {
      await api(`/api/flows/${currentRunId}/stop`, { method: "POST", body: {} });
    }
    // Start a new run with force_new to bypass reuse logic
    const res = (await api("/api/flows/ticket_flow/bootstrap", {
      method: "POST",
      body: { metadata: { force_new: true } },
    })) as BootstrapResponse;
    currentRunId = res?.id || null;
    flash("Ticket flow restarted");
    clearLiveOutput();
    await loadTicketFlow();
  } catch (err) {
    flash((err as Error).message || "Failed to restart ticket flow", "error");
  } finally {
    restartBtn.textContent = "Restart";
    setButtonsDisabled(false);
  }
}

async function archiveTicketFlow(): Promise<void> {
  const { archiveBtn } = els();
  if (!archiveBtn) return;
  if (!isRepoHealthy()) {
    flash("Repo offline; cannot archive ticket flow.", "error");
    return;
  }
  if (!currentRunId) {
    flash("No ticket flow run to archive", "info");
    return;
  }
  if (!confirm("Archive all tickets from this flow? They will be moved to the run's artifact directory.")) {
    return;
  }
  setButtonsDisabled(true);
  archiveBtn.textContent = "Archiving…";
  try {
    const res = (await api(`/api/flows/${currentRunId}/archive`, {
      method: "POST",
      body: {},
    })) as { status?: string; tickets_archived?: number };
    const count = res?.tickets_archived ?? 0;
    flash(`Archived ${count} ticket${count !== 1 ? "s" : ""}`);
    clearLiveOutput();
    currentRunId = null;
    await loadTicketFlow();
  } catch (err) {
    flash((err as Error).message || "Failed to archive ticket flow", "error");
  } finally {
    archiveBtn.textContent = "Archive Flow";
    setButtonsDisabled(false);
  }
}

export function initTicketFlow(): void {
  const { card, bootstrapBtn, resumeBtn, refreshBtn, stopBtn, restartBtn, archiveBtn } = els();
  if (!card || card.dataset.ticketInitialized === "1") return;
  card.dataset.ticketInitialized = "1";

  if (bootstrapBtn) bootstrapBtn.addEventListener("click", bootstrapTicketFlow);
  if (resumeBtn) resumeBtn.addEventListener("click", resumeTicketFlow);
  if (stopBtn) stopBtn.addEventListener("click", stopTicketFlow);
  if (restartBtn) restartBtn.addEventListener("click", restartTicketFlow);
  if (archiveBtn) archiveBtn.addEventListener("click", archiveTicketFlow);
  if (refreshBtn) refreshBtn.addEventListener("click", loadTicketFlow);

  // Initialize reason click handler for modal
  initReasonModal();

  // Initialize live output panel
  initLiveOutputPanel();

  const newThreadBtn = document.getElementById("ticket-chat-new-thread");
  if (newThreadBtn) {
    newThreadBtn.addEventListener("click", async () => {
      const { startNewTicketChatThread } = await import("./ticketChatActions.js");
      await startNewTicketChatThread();
    });
  }

  // Initialize the ticket editor modal
  initTicketEditor();

  loadTicketFlow();
  registerAutoRefresh("ticket-flow", {
    callback: loadTicketFlow,
    tabId: null,
    interval:
      (CONSTANTS.UI?.AUTO_REFRESH_INTERVAL as number | undefined) ||
      15000,
    refreshOnActivation: true,
    immediate: false,
  });

  subscribe("repo:health", (payload: unknown) => {
    const status = (payload as { status?: string } | null)?.status || "";
    if (status === "ok" || status === "degraded") {
      void loadTicketFlow();
    }
  });

  // Refresh ticket list when tickets are updated (from editor)
  subscribe("tickets:updated", () => {
    void loadTicketFiles();
  });

  // Handle browser navigation (back/forward)
  window.addEventListener("popstate", () => {
    const params = getUrlParams();
    const ticketIndex = params.get("ticket");
    if (ticketIndex) {
      void openTicketByIndex(parseInt(ticketIndex, 10));
    } else {
      closeTicketEditor();
    }
  });

  // Check URL for ticket param on initial load
  const params = getUrlParams();
  const ticketIndex = params.get("ticket");
  if (ticketIndex) {
    void openTicketByIndex(parseInt(ticketIndex, 10));
  }
}
