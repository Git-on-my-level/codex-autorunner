import {
  api,
  confirmModal,
  flash,
  getUrlParams,
  resolvePath,
  statusPill,
  getAuthToken,
  openModal,
  inputModal,
  setButtonLoading,
} from "./utils.js";
// Note: activateTab removed - header now used for collapse, not inbox navigation
import { registerAutoRefresh, type RefreshContext } from "./autoRefresh.js";
import { CONSTANTS } from "./constants.js";
import { subscribe } from "./bus.js";
import { isRepoHealthy } from "./health.js";
import { closeTicketEditor, initTicketEditor, openTicketEditor, TicketData } from "./ticketEditor.js";
import { parseAppServerEvent, type AgentEvent, type ParsedAgentEvent } from "./agentEvents.js";
import { summarizeEvents, renderCompactSummary, COMPACT_MAX_TEXT_LENGTH } from "./eventSummarizer.js";
import { refreshBell, renderMarkdown } from "./messages.js";
import { preserveScroll } from "./preserve.js";
import { createSmartRefresh } from "./smartRefresh.js";

type FlowEvent = {
  seq?: number;
  event_type: string;
  timestamp: string;
  data?: Record<string, unknown>;
  step_id?: string;
};

type WorkerHealth = {
  status?: string;
  pid?: number | null;
  is_alive?: boolean;
  message?: string | null;
};

type FlowRun = {
  id?: string;
  status?: string;
  state?: Record<string, unknown>;
  error_message?: string | null;
  started_at?: string | null;
  last_event_seq?: number | null;
  last_event_at?: string | null;
  reason_summary?: string | null;
  worker_health?: WorkerHealth | null;
};

type BootstrapResponse = FlowRun & {
  state?: Record<string, unknown> & { hint?: string };
};

type BootstrapCheckResponse = {
  status: "ready" | "needs_issue";
  github_available?: boolean;
  repo?: string | null;
};

type TicketFile = {
  path?: string;
  index?: number | null;
  frontmatter?: Record<string, unknown> | null;
  body?: string | null;
  errors?: string[];
  diff_stats?: {
    insertions: number;
    deletions: number;
    files_changed: number;
  } | null;
};

type DispatchAttachment = {
  name?: string;
  rel_path?: string;
  path?: string;
  size?: number | null;
  url?: string;
};

type DispatchEntry = {
  seq?: string;
  dispatch?: {
    mode?: string;
    title?: string;
    body?: string;
    extra?: Record<string, unknown>;
    diff_stats?: {
      insertions: number;
      deletions: number;
      files_changed: number;
    } | null;
    is_handoff?: boolean;
  } | null;
  errors?: string[];
  attachments?: DispatchAttachment[];
  created_at?: string | null;
};

function formatDispatchTime(ts?: string | null): string {
  if (!ts) return "";
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return "";
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffSecs = Math.floor(diffMs / 1000);
  if (diffSecs < 60) return "now";
  const diffMins = Math.floor(diffSecs / 60);
  if (diffMins < 60) return `${diffMins}m`;
  const diffHours = Math.floor(diffMins / 60);
  if (diffHours < 24) return `${diffHours}h`;
  const diffDays = Math.floor(diffHours / 24);
  if (diffDays < 7) return `${diffDays}d`;
  return date.toLocaleDateString([], { month: "short", day: "numeric" });
}

/**
 * Format a number for compact display (e.g., 1200 -> "1.2k").
 */
function formatNumber(n: number): string {
  if (n >= 1000000) {
    return `${(n / 1000000).toFixed(1).replace(/\.0$/, "")}M`;
  }
  if (n >= 1000) {
    return `${(n / 1000).toFixed(1).replace(/\.0$/, "")}k`;
  }
  return n.toString();
}

type TicketListPayload = {
  ticket_dir?: string;
  tickets?: TicketFile[];
  lint_errors?: string[];
  activeTicket?: string | null;
  flowStatus?: string | null;
};

type DispatchHistoryPayload = {
  runId: string | null;
  history?: DispatchEntry[];
};

let currentRunId: string | null = null;
let ticketsExist = false;
let currentActiveTicket: string | null = null;
let currentFlowStatus: string | null = null;
let selectedTicketPath: string | null = null;
let elapsedTimerId: ReturnType<typeof setInterval> | null = null;
let flowStartedAt: Date | null = null;
let eventSource: EventSource | null = null;
let eventSourceRunId: string | null = null;
let lastActivityTime: Date | null = null;
let lastActivityTimerId: ReturnType<typeof setInterval> | null = null;
let lastKnownEventSeq: number | null = null;
let lastKnownEventAt: Date | null = null;
let liveOutputDetailExpanded = false; // Start with summary view, one click for full
let liveOutputBuffer: string[] = [];
const MAX_OUTPUT_LINES = 200;
const LIVE_EVENT_MAX = 50;
let liveOutputEvents: AgentEvent[] = [];
let liveOutputEventIndex: Record<string, number> = {};
let currentReasonFull: string | null = null; // Full reason text for modal display
let dispatchHistoryRunId: string | null = null;
let eventSourceRetryAttempt = 0;
let eventSourceRetryTimerId: ReturnType<typeof setTimeout> | null = null;
const lastSeenSeqByRun: Record<string, number> = {};
let ticketListCache: { ticket_dir?: string; tickets?: TicketFile[] } | null = null;
let ticketFlowLoaded = false;

function isFlowActiveStatus(status: string | null): boolean {
  // Mirror backend FlowRunStatus.is_active(): pending | running | stopping
  return status === "pending" || status === "running" || status === "stopping";
}

// Dispatch panel collapse state (persisted to localStorage)
const DISPATCH_PANEL_COLLAPSED_KEY = "car-dispatch-panel-collapsed";
let dispatchPanelCollapsed = false;
const LAST_SEEN_SEQ_KEY_PREFIX = "car-ticket-flow-last-seq:";
const EVENT_STREAM_RETRY_DELAYS_MS = [500, 1000, 2000, 5000, 10000];
const STALE_THRESHOLD_MS = 30000;
let dragSourceIndex: number | null = null;
let dragTargetIndex: number | null = null;
let dragPlaceAfter = false;

// Throttling state
let liveOutputRenderPending = false;
let liveOutputTextPending = false;

const ticketListRefresh = createSmartRefresh<TicketListPayload>({
  getSignature: (payload) => {
    const list = (payload.tickets || []) as TicketFile[];
    const pieces = list.map((ticket) => {
      const fm = (ticket.frontmatter || {}) as Record<string, unknown>;
      const title = fm?.title ? String(fm.title) : "";
      const done = fm?.done ? "1" : "0";
      const agent = fm?.agent ? String(fm.agent) : "";
      const mtime = (ticket as { mtime?: string | number | null }).mtime ?? "";
      const errors = Array.isArray(ticket.errors) ? ticket.errors.join(",") : "";
      return [ticket.path ?? "", ticket.index ?? "", title, done, agent, mtime, errors].join("|");
    });
    return [
      payload.ticket_dir ?? "",
      payload.activeTicket ?? "",
      payload.flowStatus ?? "",
      pieces.join(";"),
    ].join("::");
  },
  render: (payload) => {
    const { tickets } = els();
    preserveScroll(
      tickets,
      () => {
        renderTickets({
          ticket_dir: payload.ticket_dir,
          tickets: payload.tickets,
        });
      },
      { restoreOnNextFrame: true }
    );
  },
  onSkip: () => {
    updateScrollFade();
  },
});

const dispatchHistoryRefresh = createSmartRefresh<DispatchHistoryPayload>({
  getSignature: (payload) => {
    const entries = payload.history || [];
    const latestSeq = entries[0]?.seq ?? "";
    return [payload.runId ?? "", latestSeq, entries.length].join("::");
  },
  render: (payload) => {
    const { history } = els();
    preserveScroll(
      history,
      () => {
        renderDispatchHistory(payload.runId, { history: payload.history });
      },
      { restoreOnNextFrame: true }
    );
  },
  onSkip: () => {
    updateScrollFade();
  },
});

function scheduleLiveOutputRender(): void {
  if (liveOutputRenderPending) return;
  liveOutputRenderPending = true;
  requestAnimationFrame(() => {
    renderLiveOutputView();
    liveOutputRenderPending = false;
  });
}

function scheduleLiveOutputTextUpdate(): void {
  if (liveOutputTextPending) return;
  liveOutputTextPending = true;
  requestAnimationFrame(() => {
    const outputEl = document.getElementById("ticket-live-output-text");
    if (outputEl) {
      const newText = liveOutputBuffer.join("\n");
      if (outputEl.textContent !== newText) {
        outputEl.textContent = newText;
      }
      // Auto-scroll to bottom when detail view is showing
      const detailEl = document.getElementById("ticket-live-output-detail");
      if (detailEl && liveOutputDetailExpanded) {
        detailEl.scrollTop = detailEl.scrollHeight;
      }
    }
    liveOutputTextPending = false;
  });
}

/**
 * Initialize dispatch panel collapse state from localStorage
 */
function initDispatchPanelToggle(): void {
  const { dispatchPanel, dispatchPanelToggle } = els();
  if (!dispatchPanel || !dispatchPanelToggle) return;

  // Restore collapsed state from localStorage
  const stored = localStorage.getItem(DISPATCH_PANEL_COLLAPSED_KEY);
  dispatchPanelCollapsed = stored === "true";
  if (dispatchPanelCollapsed) {
    dispatchPanel.classList.add("collapsed");
  }

  // Handle toggle click
  dispatchPanelToggle.addEventListener("click", () => {
    dispatchPanelCollapsed = !dispatchPanelCollapsed;
    dispatchPanel.classList.toggle("collapsed", dispatchPanelCollapsed);
    localStorage.setItem(DISPATCH_PANEL_COLLAPSED_KEY, String(dispatchPanelCollapsed));
  });
}

function clearTicketDragState(): void {
  dragSourceIndex = null;
  dragTargetIndex = null;
  dragPlaceAfter = false;
  const ticketList = document.getElementById("ticket-flow-tickets");
  if (!ticketList) return;
  ticketList
    .querySelectorAll(".ticket-item.drag-source, .ticket-item.drop-before, .ticket-item.drop-after")
    .forEach((el) => {
      el.classList.remove("drag-source", "drop-before", "drop-after");
    });
}

function getTicketMoveToPosition(
  tickets: TicketFile[],
  sourceIndex: number,
  destinationIndex: number,
  placeAfter: boolean
): number | null {
  const ordered = tickets
    .map((ticket) => ticket.index)
    .filter((index): index is number => typeof index === "number");
  const sourcePos = ordered.indexOf(sourceIndex) + 1;
  const destinationPos = ordered.indexOf(destinationIndex) + 1;
  if (!sourcePos || !destinationPos) return null;
  const desiredPos = destinationPos + (placeAfter ? 1 : 0);
  const toPos = sourcePos < desiredPos ? desiredPos - 1 : desiredPos;
  return Math.max(1, Math.min(toPos, ordered.length));
}

async function reorderTicket(
  sourceIndex: number,
  destinationIndex: number,
  placeAfter: boolean
): Promise<void> {
  await api("/api/flows/ticket_flow/tickets/reorder", {
    method: "POST",
    body: {
      source_index: sourceIndex,
      destination_index: destinationIndex,
      place_after: placeAfter,
    },
  });
}

/**
 * Render mini dispatch items for collapsed panel view.
 * Shows compact dispatch indicators that can be clicked to expand.
 */
function renderDispatchMiniList(entries: DispatchEntry[]): void {
  const { dispatchMiniList, dispatchPanel } = els();
  if (!dispatchMiniList) return;
  dispatchMiniList.innerHTML = "";

  // Only show first 8 items in mini view
  const maxMiniItems = 8;
  entries.slice(0, maxMiniItems).forEach((entry) => {
    const dispatch = entry.dispatch;
    const isTurnSummary = dispatch?.mode === "turn_summary" || dispatch?.extra?.is_turn_summary;
    const isNotify = dispatch?.mode === "notify";

    const mini = document.createElement("div");
    mini.className = `dispatch-mini-item${isNotify ? " notify" : ""}`;
    mini.textContent = `#${entry.seq || "?"}`;
    mini.title = isTurnSummary
      ? "Agent turn output"
      : dispatch?.title || `Dispatch #${entry.seq}`;

    // Click to expand panel and scroll to this item
    mini.addEventListener("click", () => {
      if (dispatchPanel && dispatchPanelCollapsed) {
        dispatchPanelCollapsed = false;
        dispatchPanel.classList.remove("collapsed");
        localStorage.setItem(DISPATCH_PANEL_COLLAPSED_KEY, "false");
      }
    });

    dispatchMiniList.appendChild(mini);
  });

  // Show overflow indicator if more items
  if (entries.length > maxMiniItems) {
    const more = document.createElement("div");
    more.className = "dispatch-mini-item";
    more.textContent = `+${entries.length - maxMiniItems}`;
    more.title = `${entries.length - maxMiniItems} more dispatches`;
    more.addEventListener("click", () => {
      if (dispatchPanel && dispatchPanelCollapsed) {
        dispatchPanelCollapsed = false;
        dispatchPanel.classList.remove("collapsed");
        localStorage.setItem(DISPATCH_PANEL_COLLAPSED_KEY, "false");
      }
    });
    dispatchMiniList.appendChild(more);
  }
}

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

function updateLastActivityFromTimestamp(timestamp: string | null | undefined): void {
  if (timestamp) {
    const parsed = new Date(timestamp);
    if (!Number.isNaN(parsed.getTime())) {
      lastActivityTime = parsed;
      lastKnownEventAt = parsed;
      startLastActivityTimer();
      return;
    }
  }
  lastActivityTime = null;
  lastKnownEventAt = null;
  stopLastActivityTimer();
  const { lastActivity } = els();
  if (lastActivity) lastActivity.textContent = "–";
}

function getLastSeenSeq(runId: string): number | null {
  if (lastSeenSeqByRun[runId] !== undefined) {
    return lastSeenSeqByRun[runId] as number;
  }
  const stored = localStorage.getItem(`${LAST_SEEN_SEQ_KEY_PREFIX}${runId}`);
  if (!stored) return null;
  const parsed = Number.parseInt(stored, 10);
  if (Number.isNaN(parsed)) return null;
  lastSeenSeqByRun[runId] = parsed;
  return parsed;
}

function setLastSeenSeq(runId: string, seq: number): void {
  if (!Number.isFinite(seq)) return;
  const current = lastSeenSeqByRun[runId];
  if (current !== undefined && seq <= current) return;
  lastSeenSeqByRun[runId] = seq;
  localStorage.setItem(`${LAST_SEEN_SEQ_KEY_PREFIX}${runId}`, String(seq));
}

function parseEventSeq(event: FlowEvent, lastEventId?: string | null): number | null {
  if (typeof event.seq === "number" && Number.isFinite(event.seq)) {
    return event.seq;
  }
  if (lastEventId) {
    const parsed = Number.parseInt(lastEventId, 10);
    if (!Number.isNaN(parsed)) return parsed;
  }
  return null;
}

function clearEventStreamRetry(): void {
  if (eventSourceRetryTimerId) {
    clearTimeout(eventSourceRetryTimerId);
    eventSourceRetryTimerId = null;
  }
}

function scheduleEventStreamReconnect(runId: string): void {
  if (eventSourceRetryTimerId) return;
  const index = Math.min(eventSourceRetryAttempt, EVENT_STREAM_RETRY_DELAYS_MS.length - 1);
  const delay = EVENT_STREAM_RETRY_DELAYS_MS[index] as number;
  eventSourceRetryAttempt += 1;
  eventSourceRetryTimerId = setTimeout(() => {
    eventSourceRetryTimerId = null;
    if (currentRunId !== runId) return;
    if (currentFlowStatus !== "running" && currentFlowStatus !== "pending") return;
    connectEventStream(runId);
  }, delay);
}

function appendToLiveOutput(text: string): void {
  if (!text) return;

  const segments = text.split("\n");

  // Merge first segment into the last buffered line to avoid artificial newlines between deltas
  if (liveOutputBuffer.length === 0) {
    liveOutputBuffer.push(segments[0]);
  } else {
    liveOutputBuffer[liveOutputBuffer.length - 1] += segments[0];
  }

  // Remaining segments represent real new lines
  for (let i = 1; i < segments.length; i++) {
    liveOutputBuffer.push(segments[i]);
  }

  // Trim buffer if it exceeds max lines
  while (liveOutputBuffer.length > MAX_OUTPUT_LINES) {
    liveOutputBuffer.shift();
  }

  scheduleLiveOutputTextUpdate();
}

function addLiveOutputEvent(parsed: ParsedAgentEvent): void {
  const { event, mergeStrategy } = parsed;
  const itemId = event.itemId;

  if (mergeStrategy && itemId && liveOutputEventIndex[itemId] !== undefined) {
    const existingIndex = liveOutputEventIndex[itemId] as number;
    const existing = liveOutputEvents[existingIndex];
    if (mergeStrategy === "append") {
      existing.summary = `${existing.summary || ""}${event.summary}`;
    } else if (mergeStrategy === "newline") {
      existing.summary = `${existing.summary || ""}\n\n`;
    }
    existing.time = event.time;
    return;
  }

  liveOutputEvents.push(event);
  if (liveOutputEvents.length > LIVE_EVENT_MAX) {
    liveOutputEvents = liveOutputEvents.slice(-LIVE_EVENT_MAX);
    liveOutputEventIndex = {};
    liveOutputEvents.forEach((evt, idx) => {
      if (evt.itemId) liveOutputEventIndex[evt.itemId] = idx;
    });
  } else if (itemId) {
    liveOutputEventIndex[itemId] = liveOutputEvents.length - 1;
  }
}

function renderLiveOutputEvents(): void {
  const container = document.getElementById("ticket-live-output-events");
  const list = document.getElementById("ticket-live-output-events-list");
  const count = document.getElementById("ticket-live-output-events-count");
  if (!container || !list || !count) return;

  const hasEvents = liveOutputEvents.length > 0;
  if (count.textContent !== String(liveOutputEvents.length)) {
    count.textContent = String(liveOutputEvents.length);
  }
  
  const shouldHide = !hasEvents || !liveOutputDetailExpanded;
  if (container.classList.contains("hidden") !== shouldHide) {
    container.classList.toggle("hidden", shouldHide);
  }
  
  if (shouldHide) {
    if (list.innerHTML !== "") list.innerHTML = "";
    return;
  }

  // Track which IDs are currently in the list to remove stale ones
  const currentIds = new Set<string>();

  liveOutputEvents.forEach((entry) => {
    const id = entry.id;
    currentIds.add(id);

    // Safer lookup than querySelector with arbitrary ID
    let wrapper: HTMLElement | null = null;
    for (let i = 0; i < list.children.length; i++) {
      const child = list.children[i] as HTMLElement;
      if (child.dataset.eventId === id) {
        wrapper = child;
        break;
      }
    }

    if (!wrapper) {
      wrapper = document.createElement("div");
      wrapper.className = `ticket-chat-event ${entry.kind || ""}`.trim();
      wrapper.dataset.eventId = id;

      const title = document.createElement("div");
      title.className = "ticket-chat-event-title";
      wrapper.appendChild(title);

      const summary = document.createElement("div");
      summary.className = "ticket-chat-event-summary";
      wrapper.appendChild(summary);

      const detail = document.createElement("div");
      detail.className = "ticket-chat-event-detail";
      wrapper.appendChild(detail);

      const meta = document.createElement("div");
      meta.className = "ticket-chat-event-meta";
      wrapper.appendChild(meta);

      list.appendChild(wrapper);
    }

    // Efficiently update content only if changed
    const titleEl = wrapper.querySelector(".ticket-chat-event-title");
    const newTitle = entry.title || entry.method || "Update";
    if (titleEl && titleEl.textContent !== newTitle) {
      titleEl.textContent = newTitle;
    }

    const summaryEl = wrapper.querySelector(".ticket-chat-event-summary");
    const newSummary = entry.summary || "";
    if (summaryEl && summaryEl.textContent !== newSummary) {
      summaryEl.textContent = newSummary;
    }

    const detailEl = wrapper.querySelector(".ticket-chat-event-detail");
    const newDetail = entry.detail || "";
    if (detailEl && detailEl.textContent !== newDetail) {
      detailEl.textContent = newDetail;
    }

    const metaEl = wrapper.querySelector(".ticket-chat-event-meta");
    if (metaEl) {
      const newMeta = entry.time
        ? new Date(entry.time).toLocaleTimeString([], {
            hour: "2-digit",
            minute: "2-digit",
          })
        : "";
      if (metaEl.textContent !== newMeta) {
        metaEl.textContent = newMeta;
      }
    }
  });

  // Remove stale events
  Array.from(list.children).forEach((child) => {
    const el = child as HTMLElement;
    if (el.dataset.eventId && !currentIds.has(el.dataset.eventId)) {
      el.remove();
    }
  });

  // Only scroll if near bottom or if height changed significantly?
  // For now, just scroll as it's the expected behavior for live logs
  list.scrollTop = list.scrollHeight;
}

function renderLiveOutputCompact(): void {
  const compactEl = document.getElementById("ticket-live-output-compact");
  if (!compactEl) return;
  const summary = summarizeEvents(liveOutputEvents, {
    maxActions: 1, // Show only 1 action + thinking to fit in 3-line compact view
    maxTextLength: COMPACT_MAX_TEXT_LENGTH,
    startTime: flowStartedAt?.getTime(),
  });
  const text = liveOutputEvents.length ? renderCompactSummary(summary) : "";
  const newText = text || "Waiting for agent output...";
  
  if (compactEl.textContent !== newText) {
    compactEl.textContent = newText;
  }
}

function updateLiveOutputViewToggle(): void {
  const viewToggle = document.getElementById("ticket-live-output-view-toggle");
  if (!viewToggle) return;
  
  if (liveOutputDetailExpanded) {
    if (!viewToggle.classList.contains("active")) viewToggle.classList.add("active");
    if (viewToggle.textContent !== "≡") viewToggle.textContent = "≡";
    if (viewToggle.title !== "Show summary") viewToggle.title = "Show summary";
  } else {
    if (viewToggle.classList.contains("active")) viewToggle.classList.remove("active");
    if (viewToggle.textContent !== "⋯") viewToggle.textContent = "⋯";
    if (viewToggle.title !== "Show full output") viewToggle.title = "Show full output";
  }
}

function renderLiveOutputView(): void {
  const compactEl = document.getElementById("ticket-live-output-compact");
  const detailEl = document.getElementById("ticket-live-output-detail");
  const eventsEl = document.getElementById("ticket-live-output-events");
  
  if (compactEl) {
    compactEl.classList.toggle("hidden", liveOutputDetailExpanded);
  }
  if (detailEl) {
    detailEl.classList.toggle("hidden", !liveOutputDetailExpanded);
  }
  if (eventsEl) {
    eventsEl.classList.toggle("hidden", !liveOutputDetailExpanded);
  }
  
  renderLiveOutputCompact();
  renderLiveOutputEvents();
  updateLiveOutputViewToggle();
}

function clearLiveOutput(): void {
  liveOutputBuffer = [];
  const outputEl = document.getElementById("ticket-live-output-text");
  if (outputEl) outputEl.textContent = "";
  liveOutputEvents = [];
  liveOutputEventIndex = {};
  scheduleLiveOutputRender();
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
  lastKnownEventAt = lastActivityTime;
  updateLastActivityDisplay();
  
  // Handle agent stream delta events
  if (event.event_type === "agent_stream_delta") {
    setLiveOutputStatus("streaming");
    const delta = event.data?.delta as string || "";
    if (delta) {
      appendToLiveOutput(delta);
    }
  }

  // Handle rich app-server events (tools, commands, files, thinking, etc.)
  if (event.event_type === "app_server_event") {
    const parsed = parseAppServerEvent(event.data);
    if (parsed) {
      addLiveOutputEvent(parsed);
      scheduleLiveOutputRender();
    }
  }

  // Handle step progress events carrying ticket selection so UI can highlight immediately
  if (event.event_type === "step_progress") {
    const nextTicket = event.data?.current_ticket as string | undefined;
    if (nextTicket) {
      currentActiveTicket = nextTicket;
      // Don't force flow status here; it comes from the runs endpoint.
      const { current } = els();
      if (current) current.textContent = currentActiveTicket;
      if (ticketListCache) {
        renderTickets(ticketListCache);
      }
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

function connectEventStream(runId: string, afterSeq?: number | null): void {
  disconnectEventStream();
  clearEventStreamRetry();
  eventSourceRunId = runId;
  
  const token = getAuthToken();
  const url = new URL(resolvePath(`/api/flows/${runId}/events`), window.location.origin);
  if (token) {
    url.searchParams.set("token", token);
  }
  if (typeof afterSeq === "number") {
    url.searchParams.set("after", String(afterSeq));
  } else {
    const lastSeenSeq = getLastSeenSeq(runId);
    if (typeof lastSeenSeq === "number") {
      url.searchParams.set("after", String(lastSeenSeq));
    }
  }
  
  eventSource = new EventSource(url.toString());
  
  eventSource.onopen = () => {
    setLiveOutputStatus("connected");
    eventSourceRetryAttempt = 0;
    clearEventStreamRetry();
  };
  
  eventSource.onmessage = (event: MessageEvent) => {
    try {
      const data = JSON.parse(event.data) as FlowEvent;
      const seq = parseEventSeq(data, event.lastEventId);
      if (currentRunId && typeof seq === "number") {
        setLastSeenSeq(currentRunId, seq);
        lastKnownEventSeq = seq;
      }
      handleFlowEvent(data);
    } catch (err) {
      // Ignore parse errors
    }
  };
  
  eventSource.onerror = () => {
    setLiveOutputStatus("disconnected");
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
    scheduleEventStreamReconnect(runId);
  };
}

function disconnectEventStream(): void {
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
  clearEventStreamRetry();
  eventSourceRunId = null;
  setLiveOutputStatus("disconnected");
}

function initLiveOutputPanel(): void {
  const viewToggleBtn = document.getElementById("ticket-live-output-view-toggle");
  
  // Toggle between summary and full view (one click)
  const toggleView = () => {
    liveOutputDetailExpanded = !liveOutputDetailExpanded;
    renderLiveOutputView();
  };
  
  if (viewToggleBtn) {
    viewToggleBtn.addEventListener("click", toggleView);
  }
  
  // Initial render
  updateLiveOutputViewToggle();
  renderLiveOutputView();
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
  stalePill: HTMLElement | null;
  reconnectBtn: HTMLButtonElement | null;
  workerStatus: HTMLElement | null;
  workerPill: HTMLElement | null;
  recoverBtn: HTMLButtonElement | null;
  metaDetails: HTMLElement | null;
  dir: HTMLElement | null;
  tickets: HTMLElement | null;
  history: HTMLElement | null;
  dispatchNote: HTMLElement | null;
  dispatchPanel: HTMLElement | null;
  dispatchPanelToggle: HTMLButtonElement | null;
  dispatchMiniList: HTMLElement | null;
  bulkSetAgentBtn: HTMLButtonElement | null;
  bulkClearModelBtn: HTMLButtonElement | null;
  bootstrapBtn: HTMLButtonElement | null;
  resumeBtn: HTMLButtonElement | null;
  refreshBtn: HTMLButtonElement | null;
  stopBtn: HTMLButtonElement | null;
    restartBtn: HTMLButtonElement | null;
    archiveBtn: HTMLButtonElement | null;
    overflowToggle: HTMLButtonElement | null;
    overflowDropdown: HTMLElement | null;
    overflowNew: HTMLButtonElement | null;
    overflowRestart: HTMLButtonElement | null;
    overflowArchive: HTMLButtonElement | null;
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
    stalePill: document.getElementById("ticket-flow-stale"),
    reconnectBtn: document.getElementById("ticket-flow-reconnect") as HTMLButtonElement | null,
    workerStatus: document.getElementById("ticket-flow-worker"),
    workerPill: document.getElementById("ticket-flow-worker-pill"),
    recoverBtn: document.getElementById("ticket-flow-recover") as HTMLButtonElement | null,
    metaDetails: document.getElementById("ticket-meta-details"),
    dir: document.getElementById("ticket-flow-dir"),
    tickets: document.getElementById("ticket-flow-tickets"),
    history: document.getElementById("ticket-dispatch-history"),
    dispatchNote: document.getElementById("ticket-dispatch-note"),
    dispatchPanel: document.getElementById("dispatch-panel"),
    dispatchPanelToggle: document.getElementById("dispatch-panel-toggle") as HTMLButtonElement | null,
    dispatchMiniList: document.getElementById("dispatch-mini-list"),
    bulkSetAgentBtn: document.getElementById("ticket-bulk-set-agent") as HTMLButtonElement | null,
    bulkClearModelBtn: document.getElementById("ticket-bulk-clear-model") as HTMLButtonElement | null,
    bootstrapBtn: document.getElementById("ticket-flow-bootstrap") as HTMLButtonElement | null,
    resumeBtn: document.getElementById("ticket-flow-resume") as HTMLButtonElement | null,
    refreshBtn: document.getElementById("ticket-flow-refresh") as HTMLButtonElement | null,
    stopBtn: document.getElementById("ticket-flow-stop") as HTMLButtonElement | null,
    restartBtn: document.getElementById("ticket-flow-restart") as HTMLButtonElement | null,
    archiveBtn: document.getElementById("ticket-flow-archive") as HTMLButtonElement | null,
    overflowToggle: document.getElementById("ticket-overflow-toggle") as HTMLButtonElement | null,
    overflowDropdown: document.getElementById("ticket-overflow-dropdown"),
    overflowNew: document.getElementById("ticket-overflow-new") as HTMLButtonElement | null,
    overflowRestart: document.getElementById("ticket-overflow-restart") as HTMLButtonElement | null,
    overflowArchive: document.getElementById("ticket-overflow-archive") as HTMLButtonElement | null,
  };
}

function setButtonsDisabled(disabled: boolean): void {
  const {
    bootstrapBtn,
    resumeBtn,
    refreshBtn,
    stopBtn,
    restartBtn,
    archiveBtn,
    reconnectBtn,
    recoverBtn,
    bulkSetAgentBtn,
    bulkClearModelBtn,
  } = els();
  [
    bootstrapBtn,
    resumeBtn,
    refreshBtn,
    stopBtn,
    restartBtn,
    archiveBtn,
    reconnectBtn,
    recoverBtn,
    bulkSetAgentBtn,
    bulkClearModelBtn,
  ].forEach((btn) => {
    if (btn) btn.disabled = disabled;
  });
}

/**
 * Updates the selected class on ticket items based on selectedTicketPath.
 */
function updateSelectedTicket(path: string | null): void {
  selectedTicketPath = path;
  const ticketList = document.getElementById("ticket-flow-tickets");
  if (!ticketList) return;
  
  const items = ticketList.querySelectorAll(".ticket-item");
  items.forEach((item) => {
    const ticketPath = item.getAttribute("data-ticket-path");
    if (ticketPath === path) {
      item.classList.add("selected");
    } else {
      item.classList.remove("selected");
    }
  });
}

/**
 * Updates the scroll fade indicator on ticket panels.
 * Adds 'has-scroll-bottom' class when content is scrollable and not at bottom.
 */
function updateScrollFade(): void {
  const ticketList = document.getElementById("ticket-flow-tickets");
  const dispatchHistory = document.getElementById("ticket-dispatch-history");
  
  [ticketList, dispatchHistory].forEach((list) => {
    if (!list) return;
    const panel = list.closest(".ticket-panel");
    if (!panel) return;
    
    // Check if scrollable and not scrolled to bottom
    const hasScrollableContent = list.scrollHeight > list.clientHeight;
    const isNotAtBottom = list.scrollTop + list.clientHeight < list.scrollHeight - 10;
    
    if (hasScrollableContent && isNotAtBottom) {
      panel.classList.add("has-scroll-bottom");
    } else {
      panel.classList.remove("has-scroll-bottom");
    }
  });
}


function truncate(text: string, max = 100): string {
  if (text.length <= max) return text;
  return `${text.slice(0, max).trim()}…`;
}

function renderTickets(data: { ticket_dir?: string; tickets?: TicketFile[]; lint_errors?: string[] } | null): void {
  ticketListCache = data;
  clearTicketDragState();
  const { tickets, dir } = els();
  if (dir) dir.textContent = data?.ticket_dir || "–";
  if (!tickets) return;
  tickets.innerHTML = "";

  // Display lint errors if present
  if (data?.lint_errors && data.lint_errors.length > 0) {
    const lintBanner = document.createElement("div");
    lintBanner.className = "ticket-lint-errors";
    data.lint_errors.forEach((error) => {
      const errorLine = document.createElement("div");
      errorLine.textContent = error;
      lintBanner.appendChild(errorLine);
    });
    tickets.appendChild(lintBanner);
  }

  const list = (data?.tickets || []) as TicketFile[];
  ticketsExist = list.length > 0;

  // Update progress bar
  const progressBar = document.getElementById("ticket-progress-bar");
  const progressFill = document.getElementById("ticket-progress-fill");
  if (progressBar && progressFill) {
    if (list.length === 0) {
      progressBar.classList.add("hidden");
    } else {
      progressBar.classList.remove("hidden");
      const doneCount = list.filter((t) => Boolean((t.frontmatter || {})?.done)).length;
      const percent = Math.round((doneCount / list.length) * 100);
      progressFill.style.width = `${percent}%`;
      progressBar.title = `${doneCount} of ${list.length} tickets done`;
    }
  }

  if (!list.length) {
    tickets.textContent = "No tickets found. Start the ticket flow to create TICKET-001.md.";
    return;
  }

  list.forEach((ticket) => {
    const item = document.createElement("div");
    const fm = (ticket.frontmatter || {}) as Record<string, unknown>;
    const done = Boolean(fm?.done);
    // Check if this ticket is currently being worked on
    const isActive = Boolean(
      currentActiveTicket &&
        ticket.path === currentActiveTicket &&
        isFlowActiveStatus(currentFlowStatus)
    );
    item.className = `ticket-item ${done ? "done" : ""} ${isActive ? "active" : ""} ${selectedTicketPath === ticket.path ? "selected" : ""} clickable`;
    item.title = "Click to edit";
    item.setAttribute("data-ticket-path", ticket.path || "");

    const ticketIndex = typeof ticket.index === "number" ? ticket.index : null;

    // Left-edge drag handle for ticket reordering.
    if (ticketIndex !== null) {
      const dragHandle = document.createElement("button");
      dragHandle.className = "ticket-reorder-handle";
      dragHandle.type = "button";
      dragHandle.title = "Drag to reorder ticket";
      dragHandle.setAttribute("aria-label", "Drag to reorder ticket");
      dragHandle.draggable = true;
      for (let i = 0; i < 6; i++) {
        dragHandle.appendChild(document.createElement("span"));
      }
      dragHandle.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
      });
      dragHandle.addEventListener("dragstart", (e: DragEvent) => {
        dragSourceIndex = ticketIndex;
        dragTargetIndex = null;
        dragPlaceAfter = false;
        item.classList.add("drag-source");
        if (e.dataTransfer) {
          e.dataTransfer.effectAllowed = "move";
          e.dataTransfer.setData("text/plain", String(ticketIndex));
        }
      });
      dragHandle.addEventListener("dragend", () => {
        clearTicketDragState();
      });
      item.appendChild(dragHandle);
    }

    // Make ticket item clickable to open editor
    item.addEventListener("click", async () => {
      updateSelectedTicket(ticket.path || null);
      try {
        if (ticket.index == null) {
          flash("Invalid ticket: missing index", "error");
          return;
        }
        const data = (await api(`/api/flows/ticket_flow/tickets/${ticket.index}`)) as TicketFile;
        openTicketEditor(data as TicketData);
      } catch (err) {
        flash(`Failed to load ticket: ${(err as Error).message}`, "error");
      }
    });
    item.addEventListener("dragover", (e: DragEvent) => {
      if (dragSourceIndex === null || ticketIndex === null || dragSourceIndex === ticketIndex) {
        return;
      }
      e.preventDefault();
      const rect = item.getBoundingClientRect();
      dragPlaceAfter = e.clientY > rect.top + rect.height / 2;
      dragTargetIndex = ticketIndex;
      item.classList.toggle("drop-before", !dragPlaceAfter);
      item.classList.toggle("drop-after", dragPlaceAfter);
      if (e.dataTransfer) {
        e.dataTransfer.dropEffect = "move";
      }
    });
    item.addEventListener("dragleave", () => {
      item.classList.remove("drop-before", "drop-after");
    });
    item.addEventListener("drop", async (e: DragEvent) => {
      if (dragSourceIndex === null || dragTargetIndex === null) return;
      e.preventDefault();
      const sourceIndex = dragSourceIndex;
      const destinationIndex = dragTargetIndex;
      const placeAfter = dragPlaceAfter;
      clearTicketDragState();
      const toPos = getTicketMoveToPosition(list, sourceIndex, destinationIndex, placeAfter);
      if (toPos === null) return;
      const ordered = list
        .map((t) => t.index)
        .filter((idx): idx is number => typeof idx === "number");
      const fromPos = ordered.indexOf(sourceIndex) + 1;
      if (!fromPos || toPos === fromPos) return;
      try {
        await reorderTicket(sourceIndex, destinationIndex, placeAfter);
        await loadTicketFiles({ reason: "manual" });
      } catch (err) {
        flash((err as Error).message || "Failed to reorder ticket", "error");
      }
    });

    const head = document.createElement("div");
    head.className = "ticket-item-head";

    // Extract ticket number from path (e.g., "TICKET-001" from ".codex-autorunner/tickets/TICKET-001.md")
    const ticketPath = ticket.path || "";
    const ticketMatch = ticketPath.match(/TICKET-\d+/);
    const ticketNumber = ticketMatch ? ticketMatch[0] : "TICKET";
    const ticketTitle = fm?.title ? String(fm.title) : "";

    const name = document.createElement("span");
    name.className = "ticket-name";
    
    // Split number and title into separate spans for responsive control
    const numSpan = document.createElement("span");
    numSpan.className = "ticket-num";
    // Extract just the number (e.g., "001" from "TICKET-001")
    const numMatch = ticketNumber.match(/\d+/);
    numSpan.textContent = numMatch ? numMatch[0] : ticketNumber;
    name.appendChild(numSpan);
    
    if (ticketTitle) {
      const titleSpan = document.createElement("span");
      titleSpan.className = "ticket-title-text";
      titleSpan.textContent = `: ${ticketTitle}`;
      name.appendChild(titleSpan);
    }
    
    // Set full text as title attribute for tooltip on hover
    item.title = ticketTitle ? `${ticketNumber}: ${ticketTitle}` : ticketNumber;
    head.appendChild(name);

    // Badge container for status + agent badges
    const badges = document.createElement("span");
    badges.className = "ticket-badges";

    // Add WORKING badge for active ticket (to the left of agent badge)
    if (isActive) {
      const workingBadge = document.createElement("span");
      workingBadge.className = "ticket-working-badge";
      // Text content used on middle responsive view; CSS hides text on desktop/mobile
      const workingText = document.createElement("span");
      workingText.className = "badge-text";
      workingText.textContent = "Working";
      workingBadge.appendChild(workingText);
      badges.appendChild(workingBadge);
    }

    // Add DONE badge for completed tickets
    if (done && !isActive) {
      const doneBadge = document.createElement("span");
      doneBadge.className = "ticket-done-badge";
      // Text content used on middle responsive view; CSS hides text on desktop/mobile
      const doneText = document.createElement("span");
      doneText.className = "badge-text";
      doneText.textContent = "Done";
      doneBadge.appendChild(doneText);
      badges.appendChild(doneBadge);
    }

    const agent = document.createElement("span");
    agent.className = "ticket-agent";
    agent.textContent = (fm?.agent as string) || "codex";
    badges.appendChild(agent);

    // Cumulative diff stats (from FlowStore DIFF_UPDATED aggregation).
    const diffStats = ticket.diff_stats || null;
    if (diffStats && (diffStats.insertions > 0 || diffStats.deletions > 0)) {
      const statsEl = document.createElement("span");
      statsEl.className = "ticket-diff-stats";
      const ins = diffStats.insertions || 0;
      const del = diffStats.deletions || 0;
      statsEl.innerHTML = `<span class="diff-add">+${formatNumber(ins)}</span><span class="diff-del">-${formatNumber(del)}</span>`;
      statsEl.title = `${ins} insertions, ${del} deletions${diffStats.files_changed ? `, ${diffStats.files_changed} files` : ""}`;
      badges.appendChild(statsEl);
    }

    head.appendChild(badges);
    item.appendChild(head);

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

  // Update scroll fade indicator after rendering
  updateScrollFade();
}

function renderDispatchHistory(
  runId: string | null,
  data: { history?: DispatchEntry[] } | null
): void {
  const { history, dispatchNote } = els();
  if (!history) return;
  history.innerHTML = "";

  const { dispatchMiniList } = els();

  if (!runId) {
    history.textContent = "Start the ticket flow to see agent dispatches.";
    if (dispatchNote) dispatchNote.textContent = "–";
    if (dispatchMiniList) dispatchMiniList.innerHTML = "";
    return;
  }

  const entries = (data?.history || []) as DispatchEntry[];
  if (!entries.length) {
    history.textContent = "No dispatches yet.";
    if (dispatchNote) dispatchNote.textContent = "–";
    if (dispatchMiniList) dispatchMiniList.innerHTML = "";
    return;
  }

  if (dispatchNote) dispatchNote.textContent = `Latest #${entries[0]?.seq ?? "–"}`;

  // Also render mini list for collapsed panel view
  renderDispatchMiniList(entries);

  entries.forEach((entry, index) => {
    const dispatch = entry.dispatch;
    const isTurnSummary = dispatch?.mode === "turn_summary" || dispatch?.extra?.is_turn_summary;
    const isHandoff = dispatch?.mode === "pause";
    const isNotify = dispatch?.mode === "notify";
    
    // Expand only the first (newest) dispatch by default - entries are newest-first
    const isFirst = index === 0;
    const isCollapsed = !isFirst;
    
    const container = document.createElement("div");
    container.className = `dispatch-item${isTurnSummary ? " turn-summary" : ""}${isHandoff ? " pause" : ""}${isNotify ? " notify" : ""}${isCollapsed ? " collapsed" : ""}`;
    
    // Reddit-style thin collapse bar on the left
    const collapseBar = document.createElement("div");
    collapseBar.className = "dispatch-collapse-bar";
    collapseBar.title = isCollapsed ? "Click to expand" : "Click to collapse";
    collapseBar.setAttribute("role", "button");
    collapseBar.setAttribute("tabindex", "0");
    collapseBar.setAttribute("aria-label", isCollapsed ? "Expand dispatch" : "Collapse dispatch");
    collapseBar.setAttribute("aria-expanded", String(!isCollapsed));
    
    const toggleCollapse = () => {
      container.classList.toggle("collapsed");
      const isNowCollapsed = container.classList.contains("collapsed");
      collapseBar.title = isNowCollapsed ? "Click to expand" : "Click to collapse";
      collapseBar.setAttribute("aria-expanded", String(!isNowCollapsed));
      collapseBar.setAttribute("aria-label", isNowCollapsed ? "Expand dispatch" : "Collapse dispatch");
    };
    
    collapseBar.addEventListener("click", (e) => {
      e.stopPropagation();
      toggleCollapse();
    });
    collapseBar.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        toggleCollapse();
      }
    });
    
    // Content wrapper for header and body
    const contentWrapper = document.createElement("div");
    contentWrapper.className = "dispatch-content-wrapper";
    
    // Create collapsible structure
    const header = document.createElement("div");
    header.className = "dispatch-header";
    
    // Make header clickable to toggle collapse
    header.addEventListener("click", (e) => {
      // Don't toggle if clicking on a link or navigating to inbox
      if ((e.target as HTMLElement).closest("a")) return;
      toggleCollapse();
    });
    
    // Header content area
    const headerContent = document.createElement("div");
    headerContent.className = "dispatch-header-content";
    headerContent.title = isTurnSummary ? "Agent turn output" : "Click header to expand/collapse";

    // Determine mode label
    let modeLabel: string;
    if (isTurnSummary) {
      modeLabel = "TURN";
    } else if (isHandoff) {
      modeLabel = "HANDOFF";
    } else {
      modeLabel = ((dispatch?.mode as string) || "notify").toUpperCase();
    }
    
    const head = document.createElement("div");
    head.className = "dispatch-item-head";
    const seq = document.createElement("span");
    seq.className = "ticket-name";
    seq.textContent = `#${entry.seq || "?"}`;
    const mode = document.createElement("span");
    mode.className = `ticket-agent${isTurnSummary ? " turn-summary-badge" : ""}`;
    mode.textContent = modeLabel;
    head.append(seq, mode);
    
    headerContent.appendChild(head);
    header.appendChild(headerContent);
    contentWrapper.appendChild(header);
    container.append(collapseBar, contentWrapper);

    // Add diff stats if present (for turn summaries)
    // New path: dispatch.diff_stats (from FlowStore DIFF_UPDATED merge)
    // Legacy fallback: dispatch.extra.diff_stats (DISPATCH.md frontmatter)
    const diffStats = (dispatch?.diff_stats ||
      (dispatch?.extra?.diff_stats as
        | { insertions?: number; deletions?: number; files_changed?: number }
        | undefined)) as
      | { insertions?: number; deletions?: number; files_changed?: number }
      | undefined;
    if (diffStats && (diffStats.insertions || diffStats.deletions)) {
      const statsEl = document.createElement("span");
      statsEl.className = "dispatch-diff-stats";
      const ins = diffStats.insertions || 0;
      const del = diffStats.deletions || 0;
      statsEl.innerHTML = `<span class="diff-add">+${formatNumber(ins)}</span><span class="diff-del">-${formatNumber(del)}</span>`;
      statsEl.title = `${ins} insertions, ${del} deletions${diffStats.files_changed ? `, ${diffStats.files_changed} files` : ""}`;
      head.appendChild(statsEl);
    }
    
    // Add ticket reference if present
    const ticketId = dispatch?.extra?.ticket_id as string | undefined;
    if (ticketId) {
      // Extract ticket number from path (e.g., "TICKET-009" from ".codex-autorunner/tickets/TICKET-009.md")
      const ticketMatch = ticketId.match(/TICKET-\d+/);
      if (ticketMatch) {
        const ticketLabel = document.createElement("span");
        ticketLabel.className = "dispatch-ticket-ref";
        ticketLabel.textContent = ticketMatch[0];
        ticketLabel.title = ticketId;
        head.appendChild(ticketLabel);
      }
    }
    
    // Add timestamp
    const timeAgo = formatDispatchTime(entry.created_at);
    if (timeAgo) {
      const timeLabel = document.createElement("span");
      timeLabel.className = "dispatch-time";
      timeLabel.textContent = timeAgo;
      head.appendChild(timeLabel);
    }
    
    // Create collapsible body content
    const bodyWrapper = document.createElement("div");
    bodyWrapper.className = "dispatch-body-wrapper";

    if (entry.errors && entry.errors.length) {
      const err = document.createElement("div");
      err.className = "ticket-errors";
      err.textContent = entry.errors.join("; ");
      bodyWrapper.appendChild(err);
    }

    const title = dispatch?.title as string | undefined;
    if (title) {
      const titleEl = document.createElement("div");
      titleEl.className = "ticket-body ticket-dispatch-title";
      titleEl.textContent = title;
      bodyWrapper.appendChild(titleEl);
    }

    const bodyText = dispatch?.body as string | undefined;
    if (bodyText) {
      const body = document.createElement("div");
      body.className = "ticket-body ticket-dispatch-body messages-markdown";
      body.innerHTML = renderMarkdown(bodyText);
      bodyWrapper.appendChild(body);
    }

    const attachments = (entry.attachments || []) as DispatchAttachment[];
    if (attachments.length) {
      const wrap = document.createElement("div");
      wrap.className = "ticket-attachments";
      attachments.forEach((att) => {
        if (!att.url) return;
        const link = document.createElement("a");
        const resolved = new URL(resolvePath(att.url), window.location.origin);
        link.href = resolved.toString();
        link.textContent = att.name || att.rel_path || "attachment";
        // Prefer direct downloads for same-origin attachments.
        if (resolved.origin === window.location.origin) {
          link.download = "";
          link.rel = "noopener";
        } else {
          link.target = "_blank";
          link.rel = "noreferrer noopener";
        }
        link.title = att.path || "";
        wrap.appendChild(link);
      });
      bodyWrapper.appendChild(wrap);
    }
    
    contentWrapper.appendChild(bodyWrapper);
    history.appendChild(container);
  });

  // Update scroll fade indicator after rendering
  updateScrollFade();
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
  const reasonSummary =
    typeof run.reason_summary === "string" ? run.reason_summary : "";
  const useSummary =
    run.status === "paused" || run.status === "failed" || run.status === "stopped";
  const shortReason =
    (useSummary && reasonSummary ? reasonSummary : "") ||
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

async function loadTicketFiles(ctx?: RefreshContext): Promise<void> {
  const { tickets } = els();
  const isInitial = ticketListRefresh.getSignature() === null;
  if (tickets && isInitial) {
    tickets.textContent = "Loading tickets…";
  }
  try {
    await ticketListRefresh.refresh(
      async () => {
        const data = (await api("/api/flows/ticket_flow/tickets")) as {
          ticket_dir?: string;
          tickets?: TicketFile[];
          lint_errors?: string[];
        };
        return {
          ticket_dir: data.ticket_dir,
          tickets: data.tickets,
          lint_errors: data.lint_errors,
          activeTicket: currentActiveTicket,
          flowStatus: currentFlowStatus,
        };
      },
      { reason: ctx?.reason === "manual" ? "manual" : "background" }
    );
  } catch (err) {
    ticketListRefresh.reset();
    ticketListCache = null;
    preserveScroll(tickets, () => {
      renderTickets(null);
    }, { restoreOnNextFrame: true });
    flash((err as Error).message || "Failed to load tickets", "error");
  }
}

type TicketBulkUpdateResponse = {
  status?: string;
  updated?: number;
  skipped?: number;
  errors?: string[];
  lint_errors?: string[];
};

function summarizeBulkResult(action: string, payload: TicketBulkUpdateResponse): void {
  const updated = payload.updated ?? 0;
  const skipped = payload.skipped ?? 0;
  const errors = payload.errors || [];
  const lintErrors = payload.lint_errors || [];
  if (!errors.length && !lintErrors.length) {
    flash(`${action}: updated ${updated}, skipped ${skipped}.`, "success");
    return;
  }
  const combined = [...errors, ...lintErrors];
  const head = combined[0] ? ` ${combined[0]}` : "";
  flash(`${action} completed with issues.${head}`, "error");
  if (combined.length > 1) {
    console.warn(`${action} issues:`, combined);
  }
}

async function bulkSetAgent(): Promise<void> {
  const { bulkSetAgentBtn } = els();
  const agent = await inputModal("Set agent for tickets", {
    placeholder: "codex",
    confirmText: "Set agent",
  });
  if (!agent) return;
  const range = await inputModal("Optional range (A:B). Leave blank for all tickets.", {
    placeholder: "1:20",
    confirmText: "Apply",
    allowEmpty: true,
  });
  if (range === null) return;
  const rangeValue = range.trim() || undefined;

  setButtonLoading(bulkSetAgentBtn, true);
  try {
    const payload = (await api("/api/flows/ticket_flow/tickets/bulk-set-agent", {
      method: "POST",
      body: {
        agent,
        range: rangeValue,
      },
    })) as TicketBulkUpdateResponse;
    summarizeBulkResult("Bulk set agent", payload);
    await loadTicketFiles({ reason: "manual" });
  } catch (err) {
    flash((err as Error).message || "Failed to bulk set agent", "error");
  } finally {
    setButtonLoading(bulkSetAgentBtn, false);
  }
}

async function bulkClearModel(): Promise<void> {
  const { bulkClearModelBtn } = els();
  const range = await inputModal("Optional range (A:B). Leave blank for all tickets.", {
    placeholder: "1:20",
    confirmText: "Clear",
    allowEmpty: true,
  });
  if (range === null) return;
  const rangeValue = range.trim() || undefined;

  const confirmed = await confirmModal(
    rangeValue
      ? `Clear model/reasoning overrides for tickets ${rangeValue}?`
      : "Clear model/reasoning overrides for all tickets?",
    { confirmText: "Clear", cancelText: "Cancel", danger: true }
  );
  if (!confirmed) return;

  setButtonLoading(bulkClearModelBtn, true);
  try {
    const payload = (await api("/api/flows/ticket_flow/tickets/bulk-clear-model", {
      method: "POST",
      body: {
        range: rangeValue,
      },
    })) as TicketBulkUpdateResponse;
    summarizeBulkResult("Bulk clear model", payload);
    await loadTicketFiles({ reason: "manual" });
  } catch (err) {
    flash((err as Error).message || "Failed to clear model overrides", "error");
  } finally {
    setButtonLoading(bulkClearModelBtn, false);
  }
}

/**
 * Open a ticket by its index
 */
async function openTicketByIndex(index: number): Promise<void> {
  try {
    const data = (await api(`/api/flows/ticket_flow/tickets/${index}`)) as TicketFile;
    if (data) {
      openTicketEditor(data as TicketData);
    } else {
      flash(`Ticket TICKET-${String(index).padStart(3, "0")} not found`, "error");
    }
  } catch (err) {
    flash(`Failed to open ticket: ${(err as Error).message}`, "error");
  }
}

async function loadDispatchHistory(runId: string | null, ctx?: RefreshContext): Promise<void> {
  const { history } = els();
  const runChanged = dispatchHistoryRunId !== runId;
  if (!runId) {
    renderDispatchHistory(null, null);
    dispatchHistoryRefresh.reset();
    dispatchHistoryRunId = null;
    return;
  }
  if (runChanged) {
    dispatchHistoryRunId = runId;
    dispatchHistoryRefresh.reset();
  }
  const isInitial = dispatchHistoryRefresh.getSignature() === null;
  if (history && isInitial) {
    history.textContent = "Loading dispatch history…";
  }
  try {
    await dispatchHistoryRefresh.refresh(
      async () => {
        const data = (await api(`/api/flows/${runId}/dispatch_history`)) as {
          history?: DispatchEntry[];
        };
        return {
          runId,
          history: data.history,
        };
      },
      {
        reason: ctx?.reason === "manual" ? "manual" : "background",
        force: runChanged,
      }
    );
  } catch (err) {
    dispatchHistoryRefresh.reset();
    preserveScroll(history, () => {
      renderDispatchHistory(runId, null);
    }, { restoreOnNextFrame: true });
    flash((err as Error).message || "Failed to load dispatch history", "error");
  }
}

async function loadTicketFlow(ctx?: RefreshContext): Promise<void> {
  const {
    status,
    run,
    current,
    turn,
    elapsed,
    progress,
    reason,
    lastActivity,
    stalePill,
    reconnectBtn,
    workerStatus,
    workerPill,
    recoverBtn,
    resumeBtn,
    bootstrapBtn,
    stopBtn,
    archiveBtn,
    refreshBtn,
  } = els();
  if (!isRepoHealthy()) {
    if (status) statusPill(status, "error");
    if (run) run.textContent = "–";
    if (current) current.textContent = "–";
    if (turn) turn.textContent = "–";
    if (elapsed) elapsed.textContent = "–";
    if (progress) progress.textContent = "–";
    if (lastActivity) lastActivity.textContent = "–";
    if (stalePill) stalePill.style.display = "none";
    if (reconnectBtn) reconnectBtn.style.display = "none";
    if (workerStatus) workerStatus.textContent = "–";
    if (workerPill) workerPill.style.display = "none";
    if (recoverBtn) recoverBtn.style.display = "none";
    if (reason) reason.textContent = "Repo offline or uninitialized.";
    setButtonsDisabled(true);
    setButtonLoading(refreshBtn, false);
    stopElapsedTimer();
    stopLastActivityTimer();
    disconnectEventStream();
    return;
  }
  const showRefreshIndicator = ticketFlowLoaded;
  if (showRefreshIndicator) {
    setButtonLoading(refreshBtn, true);
  }
  try {
    const runs = (await api("/api/flows/runs?flow_type=ticket_flow")) as FlowRun[];
    // Only consider the newest run - if it's terminal, flow is idle.
    // This matches the backend's _active_or_paused_run() logic which only checks runs[0].
    // Using find() would incorrectly pick up older paused runs when a newer run has completed.
    const newest = runs?.[0] || null;
    // Keep the newest run even if terminal, so we can archive it or see its final state
    const latest = newest;
    currentRunId = (latest?.id as string) || null;
    currentFlowStatus = (latest?.status as string) || null;
    
    // Extract ticket engine state
    const ticketEngine = (latest?.state as Record<string, unknown> | undefined)?.ticket_engine as
      | Record<string, unknown>
      | undefined;
    // The server now provides an effective current_ticket during in-flight steps.
    // Trust the API value even when null so we don't show stale DONE+WORKING between steps.
    const apiActiveTicket = (ticketEngine?.current_ticket as string) || null;
    currentActiveTicket = apiActiveTicket;
    const ticketTurns = (ticketEngine?.ticket_turns as number) ?? null;
    const totalTurns = (ticketEngine?.total_turns as number) ?? null;

    if (status) statusPill(status, (latest?.status as string) || "idle");
    if (run) run.textContent = latest?.id || "–";
    if (current)
      current.textContent = currentActiveTicket || "–";
    
    // Display turn counter
    if (turn) {
      if (ticketTurns !== null && isFlowActiveStatus(currentFlowStatus)) {
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

    lastKnownEventSeq = typeof latest?.last_event_seq === "number" ? latest.last_event_seq : null;
    if (currentRunId && typeof lastKnownEventSeq === "number") {
      setLastSeenSeq(currentRunId, lastKnownEventSeq);
    }
    updateLastActivityFromTimestamp(latest?.last_event_at || null);
    const isActive = latest?.status === "running" || latest?.status === "pending";
    const isStale = Boolean(
      isActive &&
        lastKnownEventAt &&
        Date.now() - lastKnownEventAt.getTime() > STALE_THRESHOLD_MS
    );
    if (stalePill) stalePill.style.display = isStale ? "" : "none";
    if (reconnectBtn) {
      reconnectBtn.style.display = isStale ? "" : "none";
      reconnectBtn.disabled = !currentRunId;
    }

    const worker = latest?.worker_health as WorkerHealth | null | undefined;
    const workerLabel = worker?.status
      ? `${worker.status}${worker.pid ? ` (pid ${worker.pid})` : ""}`
      : "–";
    if (workerStatus) workerStatus.textContent = workerLabel;
    const workerDead = Boolean(
      isActive &&
        worker &&
        worker.is_alive === false &&
        worker.status !== "absent"
    );
    if (workerPill) workerPill.style.display = workerDead ? "" : "none";
    if (recoverBtn) {
      recoverBtn.style.display = workerDead ? "" : "none";
      recoverBtn.disabled = !currentRunId;
    }

    if (resumeBtn) {
      resumeBtn.disabled = !latest?.id || latest.status !== "paused";
    }
    if (stopBtn) {
      const stoppable =
        latest?.status === "running" || latest?.status === "pending";
      stopBtn.disabled = !latest?.id || !stoppable;
    }
    await loadTicketFiles(ctx);
    
    // Calculate and display ticket progress (scoped to tickets container only)
    if (progress) {
      const ticketsContainer = document.getElementById("ticket-flow-tickets");
      const doneCount = ticketsContainer?.querySelectorAll(".ticket-item.done").length ?? 0;
      const totalCount = ticketsContainer?.querySelectorAll(".ticket-item").length ?? 0;
      if (totalCount > 0) {
        progress.textContent = `${doneCount} of ${totalCount} done`;
      } else {
        progress.textContent = "–";
      }
    }
    
    // Connect/disconnect event stream based on flow status
    if (currentRunId && (latest?.status === "running" || latest?.status === "pending")) {
      // Only connect if not already connected to this run
      const isSameRun = eventSourceRunId === currentRunId;
      const isClosed = eventSource?.readyState === EventSource.CLOSED;
      if (!eventSource || !isSameRun || isClosed) {
        connectEventStream(currentRunId);
        startLastActivityTimer();
      }
    } else {
      disconnectEventStream();
      if (!lastKnownEventAt) {
        stopLastActivityTimer();
        if (lastActivity) lastActivity.textContent = "–";
        lastActivityTime = null;
      }
    }

    if (bootstrapBtn) {
      const busy = latest?.status === "running" || latest?.status === "pending";
      // Disable only if busy; bootstrap will create initial ticket when missing
      bootstrapBtn.disabled = busy;
      bootstrapBtn.textContent = busy ? "Running…" : "Start Ticket Flow";
      bootstrapBtn.title = busy ? "Ticket flow in progress" : "";
    }
    
    // Show restart button when flow is paused, stopping, or in terminal state (allows starting fresh)
    const { restartBtn, overflowRestart } = els();
    if (restartBtn) {
      const isPaused = latest?.status === "paused";
      const isStopping = latest?.status === "stopping";
      const isTerminal =
        latest?.status === "completed" ||
        latest?.status === "stopped" ||
        latest?.status === "failed";
      const canRestart =
        (isPaused || isStopping || isTerminal || workerDead) &&
        ticketsExist &&
        Boolean(currentRunId);
      restartBtn.style.display = canRestart ? "" : "none";
      restartBtn.disabled = !canRestart;
      if (overflowRestart) {
        overflowRestart.style.display = canRestart ? "" : "none";
      }
    }
    
    // Show archive button when flow is paused, stopping, or in terminal state and has tickets
    if (archiveBtn) {
      const isPaused = latest?.status === "paused";
      const isStopping = latest?.status === "stopping";
      const isTerminal =
        latest?.status === "completed" ||
        latest?.status === "stopped" ||
        latest?.status === "failed";
      const canArchive = (isPaused || isStopping || isTerminal) && ticketsExist && Boolean(currentRunId);
      archiveBtn.style.display = canArchive ? "" : "none";
      archiveBtn.disabled = !canArchive;
      const { overflowArchive } = els();
      if (overflowArchive) {
        overflowArchive.style.display = canArchive ? "" : "none";
      }
    }
    await loadDispatchHistory(currentRunId, ctx);
  } catch (err) {
    if (reason) reason.textContent = (err as Error).message || "Ticket flow unavailable";
    flash((err as Error).message || "Failed to load ticket flow state", "error");
  } finally {
    ticketFlowLoaded = true;
    if (showRefreshIndicator) {
      setButtonLoading(refreshBtn, false);
    }
  }
}

async function bootstrapTicketFlow(): Promise<void> {
  const { bootstrapBtn } = els();
  if (!bootstrapBtn) return;
  if (!isRepoHealthy()) {
    flash("Repo offline; cannot start ticket flow.", "error");
    return;
  }
  setButtonsDisabled(true);
  bootstrapBtn.textContent = "Checking…";

  const startFlow = async () => {
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
  };

  const seedIssueFromGithub = async (issueRef: string) => {
    await api("/api/flows/ticket_flow/seed-issue", {
      method: "POST",
      body: { issue_ref: issueRef },
    });
    flash("ISSUE.md created from GitHub", "success");
  };

  const seedIssueFromPlan = async (planText: string) => {
    await api("/api/flows/ticket_flow/seed-issue", {
      method: "POST",
      body: { plan_text: planText },
    });
    flash("ISSUE.md created from your input", "success");
  };

  const promptIssueRef = async (repo?: string | null): Promise<string | null> => {
    const message = repo
      ? `Enter GitHub issue number or URL for ${repo}`
      : "Enter GitHub issue number or URL";
    const input = await inputModal(message, {
      placeholder: "#123 or https://github.com/org/repo/issues/123",
      confirmText: "Fetch issue",
    });
    const value = (input || "").trim();
    return value || null;
  };

  const promptPlanText = async (): Promise<string | null> => {
    // Build a simple textarea modal dynamically to avoid new HTML templates.
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    overlay.hidden = true;
    const dialog = document.createElement("div");
    dialog.className = "modal-dialog";
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    dialog.tabIndex = -1;
    const title = document.createElement("h3");
    title.textContent = "Describe the work";
    const textarea = document.createElement("textarea");
    textarea.placeholder = "Describe the scope/requirements to seed ISSUE.md";
    textarea.rows = 6;
    textarea.style.width = "100%";
    textarea.style.resize = "vertical";
    const actions = document.createElement("div");
    actions.className = "modal-actions";
    const cancel = document.createElement("button");
    cancel.className = "ghost";
    cancel.textContent = "Cancel";
    const submit = document.createElement("button");
    submit.className = "primary";
    submit.textContent = "Create ISSUE.md";
    actions.append(cancel, submit);
    dialog.append(title, textarea, actions);
    overlay.append(dialog);
    document.body.append(overlay);

    return await new Promise<string | null>((resolve) => {
      let closeModal: (() => void) | null = null;
      const cleanup = () => {
        if (closeModal) closeModal();
        overlay.remove();
      };
      const finalize = (value: string | null) => {
        cleanup();
        resolve(value);
      };
      closeModal = openModal(overlay, {
        initialFocus: textarea,
        returnFocusTo: bootstrapBtn,
        onRequestClose: () => finalize(null),
      });
      submit.addEventListener("click", () => {
        finalize(textarea.value.trim() || null);
      });
      cancel.addEventListener("click", () => finalize(null));
    });
  };

  try {
    const check = (await api("/api/flows/ticket_flow/bootstrap-check", {
      method: "GET",
    })) as BootstrapCheckResponse;

    if (check.status === "ready") {
      await startFlow();
      return;
    }

    if (check.status === "needs_issue") {
      if (check.github_available) {
        const issueRef = await promptIssueRef(check.repo);
        if (!issueRef) {
          flash("Bootstrap cancelled (no issue provided)", "info");
          return;
        }
        await seedIssueFromGithub(issueRef);
      } else {
        const planText = await promptPlanText();
        if (!planText) {
          flash("Bootstrap cancelled (no description provided)", "info");
          return;
        }
        await seedIssueFromPlan(planText);
      }
      await startFlow();
      return;
    }

    // Fallback: start normally
    await startFlow();
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

function reconnectTicketFlowStream(): void {
  if (!currentRunId) {
    flash("No ticket flow run to reconnect", "info");
    return;
  }
  const afterSeq =
    typeof lastKnownEventSeq === "number"
      ? lastKnownEventSeq
      : getLastSeenSeq(currentRunId);
  connectEventStream(currentRunId, afterSeq ?? undefined);
  flash("Reconnecting event stream", "info");
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

async function recoverTicketFlow(): Promise<void> {
  const { recoverBtn } = els();
  if (!recoverBtn) return;
  if (!isRepoHealthy()) {
    flash("Repo offline; cannot recover ticket flow.", "error");
    return;
  }
  if (!currentRunId) {
    flash("No ticket flow run to recover", "info");
    return;
  }
  setButtonsDisabled(true);
  recoverBtn.textContent = "Recovering…";
  try {
    await api(`/api/flows/${currentRunId}/reconcile`, { method: "POST", body: {} });
    flash("Flow reconciled");
    await loadTicketFlow();
  } catch (err) {
    flash((err as Error).message || "Failed to recover ticket flow", "error");
  } finally {
    recoverBtn.textContent = "Recover";
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
  const confirmed = await confirmModal(
    "Restart ticket flow? This will stop the current run and start a new one."
  );
  if (!confirmed) {
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
  const { archiveBtn, reason } = els();
  if (!archiveBtn) return;
  if (!isRepoHealthy()) {
    flash("Repo offline; cannot archive ticket flow.", "error");
    return;
  }
  if (!currentRunId) {
    flash("No ticket flow run to archive", "info");
    return;
  }
  const confirmed = await confirmModal(
    "Archive all tickets from this flow? They will be moved to the run's artifact directory."
  );
  if (!confirmed) {
    return;
  }
  setButtonsDisabled(true);
  archiveBtn.textContent = "Archiving…";
  try {
    // Force archive if flow is stuck in stopping or paused state
    const force = currentFlowStatus === "stopping" || currentFlowStatus === "paused";
    const res = (await api(`/api/flows/${currentRunId}/archive?force=${force}`, {
      method: "POST",
      body: {},
    })) as { status?: string; tickets_archived?: number };
    const count = res?.tickets_archived ?? 0;
    flash(`Archived ${count} ticket${count !== 1 ? "s" : ""}`);
    clearLiveOutput();

    // Reset all state variables
    currentRunId = null;
    currentFlowStatus = null;
    currentActiveTicket = null;
    currentReasonFull = null;

    // Reset all UI elements to idle state directly (avoid re-fetching stale data)
    const { status, run, current, turn, elapsed, progress, lastActivity, stalePill, reconnectBtn, workerStatus, workerPill, recoverBtn, bootstrapBtn, resumeBtn, stopBtn, restartBtn, archiveBtn } = els();
    if (status) statusPill(status, "idle");
    if (run) run.textContent = "–";
    if (current) current.textContent = "–";
    if (turn) turn.textContent = "–";
    if (elapsed) elapsed.textContent = "–";
    if (progress) progress.textContent = "–";
    if (lastActivity) lastActivity.textContent = "–";
    if (stalePill) stalePill.style.display = "none";
    if (reconnectBtn) reconnectBtn.style.display = "none";
    if (workerStatus) workerStatus.textContent = "–";
    if (workerPill) workerPill.style.display = "none";
    if (recoverBtn) recoverBtn.style.display = "none";
    if (reason) {
      reason.textContent = "No ticket flow run yet.";
      reason.classList.remove("has-details");
    }
    renderDispatchHistory(null, null);

    // Stop timers and disconnect event stream
    disconnectEventStream();
    stopElapsedTimer();
    stopLastActivityTimer();
    lastActivityTime = null;

    // Update button states for no active run
    if (bootstrapBtn) {
      bootstrapBtn.disabled = false;
      bootstrapBtn.textContent = "Start Ticket Flow";
      bootstrapBtn.title = "";
    }
    if (resumeBtn) resumeBtn.disabled = true;
    if (stopBtn) stopBtn.disabled = true;
    if (restartBtn) restartBtn.style.display = "none";
    const { overflowRestart, overflowArchive } = els();
    if (overflowRestart) overflowRestart.style.display = "none";
    if (archiveBtn) archiveBtn.style.display = "none";
    if (overflowArchive) overflowArchive.style.display = "none";

    // Refresh inbox badge and ticket list (tickets were archived/moved)
    void refreshBell();
    await loadTicketFiles();
  } catch (err) {
    flash((err as Error).message || "Failed to archive ticket flow", "error");
  } finally {
    if (archiveBtn) {
      archiveBtn.textContent = "Archive Flow";
    }
    setButtonsDisabled(false);
  }
}

export function initTicketFlow(): void {
  const {
    card,
    bootstrapBtn,
    resumeBtn,
    refreshBtn,
    stopBtn,
    restartBtn,
    archiveBtn,
    reconnectBtn,
    recoverBtn,
    bulkSetAgentBtn,
    bulkClearModelBtn,
  } = els();
  if (!card || card.dataset.ticketInitialized === "1") return;
  card.dataset.ticketInitialized = "1";

  if (bootstrapBtn) bootstrapBtn.addEventListener("click", bootstrapTicketFlow);
  if (resumeBtn) resumeBtn.addEventListener("click", resumeTicketFlow);
  if (stopBtn) stopBtn.addEventListener("click", stopTicketFlow);
  if (restartBtn) restartBtn.addEventListener("click", restartTicketFlow);
  if (archiveBtn) archiveBtn.addEventListener("click", archiveTicketFlow);
  if (reconnectBtn) reconnectBtn.addEventListener("click", reconnectTicketFlowStream);
  if (recoverBtn) recoverBtn.addEventListener("click", recoverTicketFlow);
  if (bulkSetAgentBtn) bulkSetAgentBtn.addEventListener("click", () => void bulkSetAgent());
  if (bulkClearModelBtn) bulkClearModelBtn.addEventListener("click", () => void bulkClearModel());
  if (refreshBtn) refreshBtn.addEventListener("click", () => {
    void loadTicketFlow({ reason: "manual" });
  });

  const { overflowToggle, overflowDropdown, overflowNew, overflowRestart, overflowArchive } = els();

  if (overflowToggle && overflowDropdown) {
    const toggleMenu = (e: Event) => {
      e.preventDefault();
      e.stopPropagation();
      const isHidden = overflowDropdown.classList.contains("hidden");
      overflowDropdown.classList.toggle("hidden", !isHidden);
    };
    const closeMenu = () => overflowDropdown.classList.add("hidden");

    overflowToggle.addEventListener("pointerdown", toggleMenu);
    overflowToggle.addEventListener("click", (e) => {
      e.preventDefault(); // swallow synthetic click after pointerdown
    });
    overflowToggle.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") toggleMenu(e);
    });

    // Close on outside click
    document.addEventListener("pointerdown", (e) => {
      if (!overflowDropdown.classList.contains("hidden") && 
          !overflowToggle.contains(e.target as Node) && 
          !overflowDropdown.contains(e.target as Node)) {
        closeMenu();
      }
    });
  }

  if (overflowNew) {
    overflowNew.addEventListener("click", () => {
      const newBtn = document.getElementById("ticket-new-btn");
      newBtn?.click();
      overflowDropdown?.classList.add("hidden");
    });
  }

  if (overflowRestart) {
    overflowRestart.addEventListener("click", () => {
      void restartTicketFlow();
      overflowDropdown?.classList.add("hidden");
    });
  }

  if (overflowArchive) {
    overflowArchive.addEventListener("click", () => {
      void archiveTicketFlow();
      overflowDropdown?.classList.add("hidden");
    });
  }

  // Initialize reason click handler for modal
  initReasonModal();

  // Initialize live output panel
  initLiveOutputPanel();

  // Initialize dispatch panel toggle for medium screens
  initDispatchPanelToggle();
  // Set up scroll listeners for fade indicator
  const ticketList = document.getElementById("ticket-flow-tickets");
  const dispatchHistory = document.getElementById("ticket-dispatch-history");
  [ticketList, dispatchHistory].forEach((el) => {
    if (el) {
      el.addEventListener("scroll", updateScrollFade, { passive: true });
    }
  });

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
    callback: async (ctx) => {
      await loadTicketFlow(ctx);
    },
    tabId: "tickets",
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

  // Update selection when editor opens a ticket
  subscribe("ticket-editor:opened", (payload: unknown) => {
    const data = payload as { path?: string | null; index?: number | null } | null;
    if (data?.path) {
      updateSelectedTicket(data.path);
      return;
    }
    if (data?.index != null && ticketListCache?.tickets?.length) {
      const match = ticketListCache.tickets.find((ticket) => ticket.index === data.index);
      if (match?.path) {
        updateSelectedTicket(match.path);
      }
    }
  });

  // Clear selection when editor is closed
  subscribe("ticket-editor:closed", () => {
    updateSelectedTicket(null);
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
