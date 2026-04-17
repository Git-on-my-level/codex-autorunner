import { parseAppServerEvent, resetOpenCodeEventState, type AgentEvent, type ParsedAgentEvent } from "./agentEvents.js";
import { openModal } from "./utils.js";
import { summarizeEvents, renderCompactSummary, COMPACT_MAX_TEXT_LENGTH } from "./eventSummarizer.js";
import {
  type FlowEvent,
  type FlowRun,
  MAX_OUTPUT_LINES,
  LIVE_EVENT_MAX,
  MAX_REASON_LENGTH,
  formatElapsed,
  formatTimeAgo,
} from "./ticketFlowState.js";

let liveOutputPanelExpanded = false;
let liveOutputDetailExpanded = false;
let liveOutputBuffer: string[] = [];
let liveOutputEvents: AgentEvent[] = [];
let liveOutputEventIndex: Record<string, number> = {};
let currentReasonFull: string | null = null;

let elapsedTimerId: ReturnType<typeof setInterval> | null = null;
let flowStartedAt: Date | null = null;

let lastActivityTime: Date | null = null;
let lastKnownEventAt: Date | null = null;
let lastActivityTimerId: ReturnType<typeof setInterval> | null = null;

let liveOutputRenderPending = false;
let liveOutputTextPending = false;

export function setFlowStartedAt(value: number | null): void {
  flowStartedAt = value == null ? null : new Date(value);
}

export function getFlowStartedAt(): Date | null {
  return flowStartedAt;
}

export function getLastKnownEventAt(): Date | null {
  return lastKnownEventAt;
}

export function setLastKnownEventAt(value: Date | null): void {
  lastKnownEventAt = value;
}

export function getLastActivityTime(): Date | null {
  return lastActivityTime;
}

export function setLastActivityTime(value: Date | null): void {
  lastActivityTime = value;
}

export function getCurrentReasonFull(): string | null {
  return currentReasonFull;
}

export function setCurrentReasonFull(value: string | null): void {
  currentReasonFull = value;
}

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
      const detailEl = document.getElementById("ticket-live-output-detail");
      if (detailEl && liveOutputDetailExpanded) {
        detailEl.scrollTop = detailEl.scrollHeight;
      }
    }
    liveOutputTextPending = false;
  });
}

function startElapsedTimer(): void {
  stopElapsedTimer();
  if (!flowStartedAt) return;

  const update = () => {
    const elapsed = document.getElementById("ticket-flow-elapsed");
    if (elapsed && flowStartedAt) {
      elapsed.textContent = formatElapsed(flowStartedAt);
    }
  };

  update();
  elapsedTimerId = setInterval(update, 1000);
}

export function stopElapsedTimer(): void {
  if (elapsedTimerId) {
    clearInterval(elapsedTimerId);
    elapsedTimerId = null;
  }
}

export function initElapsedFromStart(): void {
  if (flowStartedAt) {
    startElapsedTimer();
  }
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

export function stopLastActivityTimer(): void {
  if (lastActivityTimerId) {
    clearInterval(lastActivityTimerId);
    lastActivityTimerId = null;
  }
}

export function updateLastActivityFromTimestamp(timestamp: string | null | undefined): void {
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
  const lastActivity = document.getElementById("ticket-flow-last-activity");
  if (lastActivity) lastActivity.textContent = "–";
}

export function appendToLiveOutput(text: string): void {
  if (!text) return;

  const segments = text.split("\n");

  if (liveOutputBuffer.length === 0) {
    liveOutputBuffer.push(segments[0]);
  } else {
    liveOutputBuffer[liveOutputBuffer.length - 1] += segments[0];
  }

  for (let i = 1; i < segments.length; i++) {
    liveOutputBuffer.push(segments[i]);
  }

  while (liveOutputBuffer.length > MAX_OUTPUT_LINES) {
    liveOutputBuffer.shift();
  }

  scheduleLiveOutputTextUpdate();
  scheduleLiveOutputRender();
}

export function addLiveOutputEvent(parsed: ParsedAgentEvent): void {
  const { event, mergeStrategy } = parsed;
  const itemId = event.itemId;

  if (mergeStrategy && itemId && liveOutputEventIndex[itemId] !== undefined) {
    const existingIndex = liveOutputEventIndex[itemId] as number;
    const existing = liveOutputEvents[existingIndex];
    if (mergeStrategy === "append") {
      existing.summary = `${existing.summary || ""}${event.summary}`;
    } else if (mergeStrategy === "newline") {
      existing.summary = `${existing.summary || ""}\n\n`;
    } else if (mergeStrategy === "replace") {
      existing.summary = event.summary;
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

  const currentIds = new Set<string>();

  liveOutputEvents.forEach((entry) => {
    const id = entry.id;
    currentIds.add(id);

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

  Array.from(list.children).forEach((child) => {
    const el = child as HTMLElement;
    if (el.dataset.eventId && !currentIds.has(el.dataset.eventId)) {
      el.remove();
    }
  });

  list.scrollTop = list.scrollHeight;
}

function renderLiveOutputCompact(): void {
  const compactEl = document.getElementById("ticket-live-output-compact");
  if (!compactEl) return;
  const text = renderCompactLiveOutputText();
  const newText = text || "Waiting for agent output...";

  if (compactEl.textContent !== newText) {
    compactEl.textContent = newText;
  }
}

function renderCompactLiveOutputText(): string {
  if (liveOutputEvents.length) {
    const summary = summarizeEvents(liveOutputEvents, {
      maxActions: 1,
      maxTextLength: COMPACT_MAX_TEXT_LENGTH,
      startTime: flowStartedAt?.getTime(),
    });
    return renderCompactSummary(summary);
  }

  const fallbackText = compactLiveOutputBufferText();
  if (!fallbackText) return "";

  const summary = summarizeEvents(
    [
      {
        id: "ticket-live-output-fallback",
        title: "Output",
        summary: fallbackText,
        detail: "",
        kind: "output",
        isSignificant: true,
        time: flowStartedAt?.getTime() || Date.now(),
        itemId: null,
        method: "agent_stream_delta",
      },
    ],
    {
      maxActions: 1,
      maxTextLength: COMPACT_MAX_TEXT_LENGTH,
      startTime: flowStartedAt?.getTime(),
    }
  );
  return renderCompactSummary(summary);
}

function compactLiveOutputBufferText(): string {
  const recentLines = liveOutputBuffer
    .map((line) => line.trim())
    .filter((line) => line);
  if (!recentLines.length) return "";

  const nonStepLines = recentLines.filter((line) => !/^--- Step: .* ---$/.test(line));
  const compactLines = nonStepLines.length ? nonStepLines : recentLines;
  return compactLines.slice(-3).join(" ").replace(/\s+/g, " ").trim();
}

function updateLiveOutputPanelToggle(): void {
  const panelToggle = document.getElementById("ticket-live-output-panel-toggle");
  const panel = document.getElementById("ticket-live-output-panel");
  const chevron = document.getElementById("ticket-live-output-chevron");
  if (!panelToggle) return;

  panel?.classList.toggle("collapsed", !liveOutputPanelExpanded);
  panelToggle.classList.toggle("active", liveOutputPanelExpanded);
  panelToggle.setAttribute("aria-expanded", String(liveOutputPanelExpanded));
  panelToggle.setAttribute(
    "title",
    liveOutputPanelExpanded ? "Hide agent output" : "Show agent output"
  );
  if (chevron) {
    chevron.textContent = liveOutputPanelExpanded ? "▴" : "▾";
  }
}

function updateLiveOutputDetailToggle(): void {
  const detailToggle = document.getElementById("ticket-live-output-detail-toggle");
  if (!detailToggle) return;

  if (liveOutputDetailExpanded) {
    if (!detailToggle.classList.contains("active")) detailToggle.classList.add("active");
    if (detailToggle.textContent !== "≡") detailToggle.textContent = "≡";
    if (detailToggle.title !== "Show summary") detailToggle.title = "Show summary";
  } else {
    if (detailToggle.classList.contains("active")) detailToggle.classList.remove("active");
    if (detailToggle.textContent !== "⋯") detailToggle.textContent = "⋯";
    if (detailToggle.title !== "Show full output") detailToggle.title = "Show full output";
  }
}

export function renderLiveOutputView(): void {
  const compactEl = document.getElementById("ticket-live-output-compact");
  const detailEl = document.getElementById("ticket-live-output-detail");
  const eventsEl = document.getElementById("ticket-live-output-events");

  if (compactEl) {
    compactEl.classList.toggle("hidden", liveOutputDetailExpanded || !liveOutputPanelExpanded);
  }
  if (detailEl) {
    detailEl.classList.toggle("hidden", !liveOutputDetailExpanded || !liveOutputPanelExpanded);
  }
  if (eventsEl) {
    eventsEl.classList.toggle("hidden", !liveOutputDetailExpanded || !liveOutputPanelExpanded);
  }

  renderLiveOutputCompact();
  renderLiveOutputEvents();
  updateLiveOutputPanelToggle();
  updateLiveOutputDetailToggle();
}

export function clearLiveOutput(): void {
  liveOutputBuffer = [];
  const outputEl = document.getElementById("ticket-live-output-text");
  if (outputEl) outputEl.textContent = "";
  liveOutputEvents = [];
  liveOutputEventIndex = {};
  resetOpenCodeEventState();
  scheduleLiveOutputRender();
}

export function setLiveOutputStatus(status: "disconnected" | "connected" | "streaming"): void {
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

export function initLiveOutputPanel(): void {
  const panelToggleBtn = document.getElementById("ticket-live-output-panel-toggle");
  const detailToggleBtn = document.getElementById("ticket-live-output-detail-toggle");

  const panel = document.getElementById("ticket-live-output-panel");
  if (panel) {
    liveOutputPanelExpanded = !panel.classList.contains("collapsed");
  }
  const detailEl = document.getElementById("ticket-live-output-detail");
  if (detailEl) {
    liveOutputDetailExpanded = !detailEl.classList.contains("hidden");
  }

  const togglePanel = () => {
    liveOutputPanelExpanded = !liveOutputPanelExpanded;
    renderLiveOutputView();
  };

  const toggleDetail = () => {
    liveOutputDetailExpanded = !liveOutputDetailExpanded;
    renderLiveOutputView();
  };

  if (panelToggleBtn) {
    panelToggleBtn.addEventListener("click", togglePanel);
  }
  if (detailToggleBtn) {
    detailToggleBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      toggleDetail();
    });
  }

  renderLiveOutputView();
}

export function resetAllStreamState(): void {
  liveOutputPanelExpanded = false;
  liveOutputDetailExpanded = false;
  flowStartedAt = null;
  lastActivityTime = null;
  lastKnownEventAt = null;
  currentReasonFull = null;
  liveOutputBuffer = [];
  liveOutputEvents = [];
  liveOutputEventIndex = {};
  liveOutputRenderPending = false;
  liveOutputTextPending = false;
  stopElapsedTimer();
  stopLastActivityTimer();
}

export function processStreamDelta(event: FlowEvent): void {
  setLiveOutputStatus("streaming");
  const delta = (event.data?.delta as string) || "";
  if (delta) {
    appendToLiveOutput(delta);
  }
}

export function processAppServerEvent(data: unknown): void {
  const parsed = parseAppServerEvent(data);
  if (parsed) {
    addLiveOutputEvent(parsed);
    scheduleLiveOutputRender();
  }
}

export function processStepStarted(event: FlowEvent): void {
  const stepName = (event.data?.step_name as string) || "";
  if (stepName) {
    appendToLiveOutput(`\n--- Step: ${stepName} ---\n`);
  }
}

export function updateActivityFromEvent(event: FlowEvent): void {
  lastActivityTime = new Date(event.timestamp);
  lastKnownEventAt = lastActivityTime;
  updateLastActivityDisplay();
}

export function initReasonModal(): void {
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

export function getFullReason(run: FlowRun | null): string | null {
  if (!run) return null;
  const runState = (run.state || {}) as Record<string, unknown>;
  const engine = (runState.ticket_engine || {}) as Record<string, unknown>;
  const reason = (engine.reason as string) || (run.error_message as string) || "";
  const details = (engine.reason_details as string) || "";
  if (!reason && !details) return null;
  if (details) {
    return `${reason}\n\n${details}`.trim();
  }
  return reason;
}

export function summarizeReason(run: FlowRun | null): string {
  if (!run) {
    currentReasonFull = null;
    return "No ticket flow run yet.";
  }
  const runState = (run.state || {}) as Record<string, unknown>;
  const engine = (runState.ticket_engine || {}) as Record<string, unknown>;
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
  if (shortReason.length > MAX_REASON_LENGTH) {
    return shortReason.slice(0, MAX_REASON_LENGTH - 3) + "...";
  }
  return shortReason;
}
