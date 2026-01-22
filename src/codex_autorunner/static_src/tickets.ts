import { api, flash, resolvePath, statusPill } from "./utils.js";
import { registerAutoRefresh } from "./autoRefresh.js";
import { CONSTANTS } from "./constants.js";

type FlowRun = {
  id?: string;
  status?: string;
  state?: Record<string, unknown>;
  error_message?: string | null;
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

function els(): {
  card: HTMLElement | null;
  status: HTMLElement | null;
  run: HTMLElement | null;
  current: HTMLElement | null;
  reason: HTMLElement | null;
  dir: HTMLElement | null;
  tickets: HTMLElement | null;
  history: HTMLElement | null;
  handoffNote: HTMLElement | null;
  bootstrapBtn: HTMLButtonElement | null;
  resumeBtn: HTMLButtonElement | null;
  refreshBtn: HTMLButtonElement | null;
  stopBtn: HTMLButtonElement | null;
} {
  return {
    card: document.getElementById("ticket-card"),
    status: document.getElementById("ticket-flow-status"),
    run: document.getElementById("ticket-flow-run"),
    current: document.getElementById("ticket-flow-current"),
    reason: document.getElementById("ticket-flow-reason"),
    dir: document.getElementById("ticket-flow-dir"),
    tickets: document.getElementById("ticket-flow-tickets"),
    history: document.getElementById("ticket-handoff-history"),
    handoffNote: document.getElementById("ticket-handoff-note"),
    bootstrapBtn: document.getElementById("ticket-flow-bootstrap") as HTMLButtonElement | null,
    resumeBtn: document.getElementById("ticket-flow-resume") as HTMLButtonElement | null,
    refreshBtn: document.getElementById("ticket-flow-refresh") as HTMLButtonElement | null,
    stopBtn: document.getElementById("ticket-flow-stop") as HTMLButtonElement | null,
  };
}

function setButtonsDisabled(disabled: boolean): void {
  const { bootstrapBtn, resumeBtn, refreshBtn, stopBtn } = els();
  [bootstrapBtn, resumeBtn, refreshBtn, stopBtn].forEach((btn) => {
    if (btn) btn.disabled = disabled;
  });
}

function truncate(text: string, max = 220): string {
  if (text.length <= max) return text;
  return `${text.slice(0, max).trim()}…`;
}

function renderTickets(data: { ticket_dir?: string; tickets?: TicketFile[] } | null): void {
  const { tickets, dir } = els();
  if (dir) dir.textContent = data?.ticket_dir || "–";
  if (!tickets) return;
  tickets.innerHTML = "";

  const list = (data?.tickets || []) as TicketFile[];
  if (!list.length) {
    tickets.textContent = "No tickets found. Create TICKET-001.md to begin.";
    return;
  }

  list.forEach((ticket) => {
    const item = document.createElement("div");
    const fm = (ticket.frontmatter || {}) as Record<string, unknown>;
    const done = Boolean(fm?.done);
    item.className = `ticket-item ${done ? "done" : ""}`;

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

function summarizeReason(run: FlowRun | null): string {
  if (!run) return "No ticket flow run yet.";
  const state = (run.state || {}) as Record<string, unknown>;
  const engine = (state.ticket_engine || {}) as Record<string, unknown>;
  return (
    (engine.reason as string) ||
    (run.error_message as string) ||
    (engine.current_ticket ? `Working on ${engine.current_ticket}` : "") ||
    run.status ||
    ""
  );
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
  const { status, run, current, reason, resumeBtn, bootstrapBtn, stopBtn } = els();
  try {
    const runs = (await api("/api/flows/runs?flow_type=ticket_flow")) as FlowRun[];
    const latest = (runs && runs[0]) || null;
    currentRunId = (latest?.id as string) || null;

    if (status) statusPill(status, (latest?.status as string) || "idle");
    if (run) run.textContent = latest?.id || "–";
    if (current)
      current.textContent =
        ((latest?.state as Record<string, unknown> | undefined)?.ticket_engine as
          | Record<string, unknown>
          | undefined)?.current_ticket?.toString() || "–";
    if (reason) reason.textContent = summarizeReason(latest) || "–";

    if (resumeBtn) {
      resumeBtn.disabled = !latest?.id || latest.status !== "paused";
    }
    if (stopBtn) {
      const stoppable =
        latest?.status === "running" || latest?.status === "pending";
      stopBtn.disabled = !latest?.id || !stoppable;
    }
    if (bootstrapBtn) {
      const busy = latest?.status === "running" || latest?.status === "pending";
      bootstrapBtn.disabled = busy;
      bootstrapBtn.textContent = busy ? "Running…" : "Start Ticket Flow";
    }

    await loadTicketFiles();
    await loadHandoffHistory(currentRunId);
  } catch (err) {
    if (reason) reason.textContent = (err as Error).message || "Ticket flow unavailable";
    flash((err as Error).message || "Failed to load ticket flow state", "error");
  }
}

async function bootstrapTicketFlow(): Promise<void> {
  const { bootstrapBtn } = els();
  if (!bootstrapBtn) return;
  const confirmed = window.confirm(
    "Create TICKET-001.md (if missing) and start the ticket flow?"
  );
  if (!confirmed) return;
  setButtonsDisabled(true);
  bootstrapBtn.textContent = "Starting…";
  try {
    const res = (await api("/api/flows/ticket_flow/bootstrap", {
      method: "POST",
      body: {},
    })) as FlowRun;
    currentRunId = res?.id || null;
    flash("Ticket flow started");
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

export function initTicketFlow(): void {
  const { card, bootstrapBtn, resumeBtn, refreshBtn, stopBtn } = els();
  if (!card || card.dataset.ticketInitialized === "1") return;
  card.dataset.ticketInitialized = "1";

  if (bootstrapBtn) bootstrapBtn.addEventListener("click", bootstrapTicketFlow);
  if (resumeBtn) resumeBtn.addEventListener("click", resumeTicketFlow);
  if (stopBtn) stopBtn.addEventListener("click", stopTicketFlow);
  if (refreshBtn) refreshBtn.addEventListener("click", loadTicketFlow);

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
}
