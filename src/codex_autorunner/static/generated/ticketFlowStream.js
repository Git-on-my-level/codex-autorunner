// GENERATED FILE - do not edit directly. Source: static_src/
import { parseAppServerEvent, resetOpenCodeEventState } from "./agentEvents.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
import { openModal } from "./utils.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
import { MAX_OUTPUT_LINES, LIVE_EVENT_MAX, MAX_REASON_LENGTH, formatElapsed, formatTimeAgo, } from "./ticketFlowState.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
let liveOutputPanelExpanded = false;
let liveOutputBuffer = [];
let liveOutputEvents = [];
let liveOutputEventIndex = {};
let currentReasonFull = null;
let elapsedTimerId = null;
let flowStartedAt = null;
let lastActivityTime = null;
let lastKnownEventAt = null;
let lastActivityTimerId = null;
let liveOutputRenderPending = false;
let liveOutputTextPending = false;
export function setFlowStartedAt(value) {
    flowStartedAt = value == null ? null : new Date(value);
}
export function getFlowStartedAt() {
    return flowStartedAt;
}
export function getLastKnownEventAt() {
    return lastKnownEventAt;
}
export function setLastKnownEventAt(value) {
    lastKnownEventAt = value;
}
export function getLastActivityTime() {
    return lastActivityTime;
}
export function setLastActivityTime(value) {
    lastActivityTime = value;
}
export function getCurrentReasonFull() {
    return currentReasonFull;
}
export function setCurrentReasonFull(value) {
    currentReasonFull = value;
}
function scheduleLiveOutputRender() {
    if (liveOutputRenderPending)
        return;
    liveOutputRenderPending = true;
    requestAnimationFrame(() => {
        renderLiveOutputView();
        liveOutputRenderPending = false;
    });
}
function scheduleLiveOutputTextUpdate() {
    if (liveOutputTextPending)
        return;
    liveOutputTextPending = true;
    requestAnimationFrame(() => {
        const outputEl = document.getElementById("ticket-live-output-text");
        if (outputEl) {
            const newText = liveOutputBuffer.join("\n");
            if (outputEl.textContent !== newText) {
                outputEl.textContent = newText;
            }
            const detailEl = document.getElementById("ticket-live-output-detail");
            if (detailEl && liveOutputPanelExpanded) {
                detailEl.scrollTop = detailEl.scrollHeight;
            }
        }
        liveOutputTextPending = false;
    });
}
function startElapsedTimer() {
    stopElapsedTimer();
    if (!flowStartedAt)
        return;
    const update = () => {
        const elapsed = document.getElementById("ticket-flow-elapsed");
        if (elapsed && flowStartedAt) {
            elapsed.textContent = formatElapsed(flowStartedAt);
        }
    };
    update();
    elapsedTimerId = setInterval(update, 1000);
}
export function stopElapsedTimer() {
    if (elapsedTimerId) {
        clearInterval(elapsedTimerId);
        elapsedTimerId = null;
    }
}
export function initElapsedFromStart() {
    if (flowStartedAt) {
        startElapsedTimer();
    }
}
function updateLastActivityDisplay() {
    const el = document.getElementById("ticket-flow-last-activity");
    if (el && lastActivityTime) {
        el.textContent = formatTimeAgo(lastActivityTime);
    }
}
function startLastActivityTimer() {
    stopLastActivityTimer();
    updateLastActivityDisplay();
    lastActivityTimerId = setInterval(updateLastActivityDisplay, 1000);
}
export function stopLastActivityTimer() {
    if (lastActivityTimerId) {
        clearInterval(lastActivityTimerId);
        lastActivityTimerId = null;
    }
}
export function updateLastActivityFromTimestamp(timestamp) {
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
    if (lastActivity)
        lastActivity.textContent = "–";
}
export function appendToLiveOutput(text) {
    if (!text)
        return;
    const segments = text.split("\n");
    if (liveOutputBuffer.length === 0) {
        liveOutputBuffer.push(segments[0]);
    }
    else {
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
export function addLiveOutputEvent(parsed) {
    const { event, mergeStrategy } = parsed;
    const itemId = event.itemId;
    if (mergeStrategy && itemId && liveOutputEventIndex[itemId] !== undefined) {
        const existingIndex = liveOutputEventIndex[itemId];
        const existing = liveOutputEvents[existingIndex];
        if (mergeStrategy === "append") {
            existing.summary = `${existing.summary || ""}${event.summary}`;
        }
        else if (mergeStrategy === "newline") {
            existing.summary = `${existing.summary || ""}\n\n`;
        }
        else if (mergeStrategy === "replace") {
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
            if (evt.itemId)
                liveOutputEventIndex[evt.itemId] = idx;
        });
    }
    else if (itemId) {
        liveOutputEventIndex[itemId] = liveOutputEvents.length - 1;
    }
}
function renderLiveOutputEvents() {
    const container = document.getElementById("ticket-live-output-events");
    const list = document.getElementById("ticket-live-output-events-list");
    const count = document.getElementById("ticket-live-output-events-count");
    if (!container || !list || !count)
        return;
    const hasEvents = liveOutputEvents.length > 0;
    if (count.textContent !== String(liveOutputEvents.length)) {
        count.textContent = String(liveOutputEvents.length);
    }
    const shouldHide = !hasEvents || !liveOutputPanelExpanded;
    if (container.classList.contains("hidden") !== shouldHide) {
        container.classList.toggle("hidden", shouldHide);
    }
    if (shouldHide) {
        if (list.innerHTML !== "")
            list.innerHTML = "";
        return;
    }
    const currentIds = new Set();
    liveOutputEvents.forEach((entry) => {
        const id = entry.id;
        currentIds.add(id);
        let wrapper = null;
        for (let i = 0; i < list.children.length; i++) {
            const child = list.children[i];
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
        const el = child;
        if (el.dataset.eventId && !currentIds.has(el.dataset.eventId)) {
            el.remove();
        }
    });
    list.scrollTop = list.scrollHeight;
}
function updateLiveOutputPanelToggle() {
    const panelToggle = document.getElementById("ticket-live-output-panel-toggle");
    const panel = document.getElementById("ticket-live-output-panel");
    const chevron = document.getElementById("ticket-live-output-chevron");
    if (!panelToggle)
        return;
    panel?.classList.toggle("collapsed", !liveOutputPanelExpanded);
    panelToggle.classList.toggle("active", liveOutputPanelExpanded);
    panelToggle.setAttribute("aria-expanded", String(liveOutputPanelExpanded));
    panelToggle.setAttribute("title", liveOutputPanelExpanded ? "Hide agent output" : "Show agent output");
    if (chevron) {
        chevron.textContent = liveOutputPanelExpanded ? "▴" : "▾";
    }
}
export function renderLiveOutputView() {
    const compactEl = document.getElementById("ticket-live-output-compact");
    const detailEl = document.getElementById("ticket-live-output-detail");
    if (compactEl) {
        compactEl.classList.add("hidden");
    }
    if (detailEl) {
        detailEl.classList.toggle("hidden", !liveOutputPanelExpanded);
    }
    renderLiveOutputEvents();
    updateLiveOutputPanelToggle();
}
export function clearLiveOutput() {
    liveOutputBuffer = [];
    const outputEl = document.getElementById("ticket-live-output-text");
    if (outputEl)
        outputEl.textContent = "";
    liveOutputEvents = [];
    liveOutputEventIndex = {};
    resetOpenCodeEventState();
    scheduleLiveOutputRender();
}
export function setLiveOutputStatus(status) {
    const statusEl = document.getElementById("ticket-live-output-status");
    if (!statusEl)
        return;
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
export function initLiveOutputPanel() {
    const panelToggleBtn = document.getElementById("ticket-live-output-panel-toggle");
    const panel = document.getElementById("ticket-live-output-panel");
    if (panel) {
        liveOutputPanelExpanded = !panel.classList.contains("collapsed");
    }
    const togglePanel = () => {
        liveOutputPanelExpanded = !liveOutputPanelExpanded;
        renderLiveOutputView();
    };
    if (panelToggleBtn) {
        panelToggleBtn.addEventListener("click", togglePanel);
    }
    renderLiveOutputView();
}
export function resetAllStreamState() {
    liveOutputPanelExpanded = false;
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
export function processStreamDelta(event) {
    setLiveOutputStatus("streaming");
    const delta = event.data?.delta || "";
    if (delta) {
        appendToLiveOutput(delta);
    }
}
export function processAppServerEvent(data) {
    const parsed = parseAppServerEvent(data);
    if (parsed) {
        addLiveOutputEvent(parsed);
        scheduleLiveOutputRender();
    }
}
export function processStepStarted(event) {
    const stepName = event.data?.step_name || "";
    if (stepName) {
        appendToLiveOutput(`\n--- Step: ${stepName} ---\n`);
    }
}
export function updateActivityFromEvent(event) {
    lastActivityTime = new Date(event.timestamp);
    lastKnownEventAt = lastActivityTime;
    updateLastActivityDisplay();
}
export function initReasonModal() {
    const reasonEl = document.getElementById("ticket-flow-reason");
    const modalOverlay = document.getElementById("reason-modal");
    const modalContent = document.getElementById("reason-modal-content");
    const closeBtn = document.getElementById("reason-modal-close");
    if (!reasonEl || !modalOverlay || !modalContent)
        return;
    let closeModal = null;
    const showReasonModal = () => {
        if (!currentReasonFull || !reasonEl.classList.contains("has-details"))
            return;
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
            if (closeModal)
                closeModal();
        });
    }
}
export function getFullReason(run) {
    if (!run)
        return null;
    const runState = (run.state || {});
    const engine = (runState.ticket_engine || {});
    const reason = engine.reason || run.error_message || "";
    const details = engine.reason_details || "";
    if (!reason && !details)
        return null;
    if (details) {
        return `${reason}\n\n${details}`.trim();
    }
    return reason;
}
export function summarizeReason(run) {
    if (!run) {
        currentReasonFull = null;
        return "No ticket flow run yet.";
    }
    const runState = (run.state || {});
    const engine = (runState.ticket_engine || {});
    const fullReason = getFullReason(run);
    currentReasonFull = fullReason;
    const reasonSummary = typeof run.reason_summary === "string" ? run.reason_summary : "";
    const useSummary = run.status === "paused" || run.status === "failed" || run.status === "stopped";
    const shortReason = (useSummary && reasonSummary ? reasonSummary : "") ||
        engine.reason ||
        run.error_message ||
        (engine.current_ticket ? `Working on ${engine.current_ticket}` : "") ||
        run.status ||
        "";
    if (shortReason.length > MAX_REASON_LENGTH) {
        return shortReason.slice(0, MAX_REASON_LENGTH - 3) + "...";
    }
    return shortReason;
}
