// GENERATED FILE - do not edit directly. Source: static_src/
import { api, flash, getUrlParams, resolvePath, statusPill, getAuthToken, openModal } from "./utils.js";
import { activateTab } from "./tabs.js";
import { registerAutoRefresh } from "./autoRefresh.js";
import { CONSTANTS } from "./constants.js";
import { subscribe } from "./bus.js";
import { isRepoHealthy } from "./health.js";
import { closeTicketEditor, initTicketEditor, openTicketEditor } from "./ticketEditor.js";
import { parseAppServerEvent } from "./agentEvents.js";
import { summarizeEvents, renderCompactSummary, COMPACT_MAX_TEXT_LENGTH } from "./eventSummarizer.js";
import { refreshBell, renderMarkdown } from "./messages.js";
let currentRunId = null;
let ticketsExist = false;
let currentActiveTicket = null;
let currentFlowStatus = null;
let elapsedTimerId = null;
let flowStartedAt = null;
let eventSource = null;
let lastActivityTime = null;
let lastActivityTimerId = null;
let liveOutputDetailExpanded = false; // Start with summary view, one click for full
let liveOutputBuffer = [];
const MAX_OUTPUT_LINES = 200;
const LIVE_EVENT_MAX = 50;
let liveOutputEvents = [];
let liveOutputEventIndex = {};
let currentReasonFull = null; // Full reason text for modal display
// Dispatch panel collapse state (persisted to localStorage)
const DISPATCH_PANEL_COLLAPSED_KEY = "car-dispatch-panel-collapsed";
let dispatchPanelCollapsed = false;
// Throttling state
let liveOutputRenderPending = false;
let liveOutputTextPending = false;
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
function initDispatchPanelToggle() {
    const { dispatchPanel, dispatchPanelToggle } = els();
    if (!dispatchPanel || !dispatchPanelToggle)
        return;
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
/**
 * Render mini dispatch items for collapsed panel view.
 * Shows compact dispatch indicators that can be clicked to expand.
 */
function renderDispatchMiniList(entries) {
    const { dispatchMiniList, dispatchPanel } = els();
    if (!dispatchMiniList)
        return;
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
function formatElapsed(startTime) {
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
function startElapsedTimer() {
    stopElapsedTimer();
    if (!flowStartedAt)
        return;
    const update = () => {
        const { elapsed } = els();
        if (elapsed && flowStartedAt) {
            elapsed.textContent = formatElapsed(flowStartedAt);
        }
    };
    update(); // Update immediately
    elapsedTimerId = setInterval(update, 1000);
}
function stopElapsedTimer() {
    if (elapsedTimerId) {
        clearInterval(elapsedTimerId);
        elapsedTimerId = null;
    }
}
// ---- SSE Event Stream Functions ----
function formatTimeAgo(timestamp) {
    const now = new Date();
    const diffMs = now.getTime() - timestamp.getTime();
    const diffSecs = Math.floor(diffMs / 1000);
    if (diffSecs < 5)
        return "just now";
    if (diffSecs < 60)
        return `${diffSecs}s ago`;
    const mins = Math.floor(diffSecs / 60);
    if (mins < 60)
        return `${mins}m ago`;
    const hours = Math.floor(mins / 60);
    return `${hours}h ago`;
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
function stopLastActivityTimer() {
    if (lastActivityTimerId) {
        clearInterval(lastActivityTimerId);
        lastActivityTimerId = null;
    }
}
function appendToLiveOutput(text) {
    if (!text)
        return;
    const segments = text.split("\n");
    // Merge first segment into the last buffered line to avoid artificial newlines between deltas
    if (liveOutputBuffer.length === 0) {
        liveOutputBuffer.push(segments[0]);
    }
    else {
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
function addLiveOutputEvent(parsed) {
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
    const shouldHide = !hasEvents || !liveOutputDetailExpanded;
    if (container.classList.contains("hidden") !== shouldHide) {
        container.classList.toggle("hidden", shouldHide);
    }
    if (shouldHide) {
        if (list.innerHTML !== "")
            list.innerHTML = "";
        return;
    }
    // Track which IDs are currently in the list to remove stale ones
    const currentIds = new Set();
    liveOutputEvents.forEach((entry) => {
        const id = entry.id;
        currentIds.add(id);
        // Safer lookup than querySelector with arbitrary ID
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
        const el = child;
        if (el.dataset.eventId && !currentIds.has(el.dataset.eventId)) {
            el.remove();
        }
    });
    // Only scroll if near bottom or if height changed significantly?
    // For now, just scroll as it's the expected behavior for live logs
    list.scrollTop = list.scrollHeight;
}
function renderLiveOutputCompact() {
    const compactEl = document.getElementById("ticket-live-output-compact");
    if (!compactEl)
        return;
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
function updateLiveOutputViewToggle() {
    const viewToggle = document.getElementById("ticket-live-output-view-toggle");
    if (!viewToggle)
        return;
    if (liveOutputDetailExpanded) {
        if (!viewToggle.classList.contains("active"))
            viewToggle.classList.add("active");
        if (viewToggle.textContent !== "≡")
            viewToggle.textContent = "≡";
        if (viewToggle.title !== "Show summary")
            viewToggle.title = "Show summary";
    }
    else {
        if (viewToggle.classList.contains("active"))
            viewToggle.classList.remove("active");
        if (viewToggle.textContent !== "⋯")
            viewToggle.textContent = "⋯";
        if (viewToggle.title !== "Show full output")
            viewToggle.title = "Show full output";
    }
}
function renderLiveOutputView() {
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
function clearLiveOutput() {
    liveOutputBuffer = [];
    const outputEl = document.getElementById("ticket-live-output-text");
    if (outputEl)
        outputEl.textContent = "";
    liveOutputEvents = [];
    liveOutputEventIndex = {};
    scheduleLiveOutputRender();
}
function setLiveOutputStatus(status) {
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
function handleFlowEvent(event) {
    // Update last activity time
    lastActivityTime = new Date(event.timestamp);
    updateLastActivityDisplay();
    // Handle agent stream delta events
    if (event.event_type === "agent_stream_delta") {
        setLiveOutputStatus("streaming");
        const delta = event.data?.delta || "";
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
        const stepName = event.data?.step_name || "";
        if (stepName) {
            appendToLiveOutput(`\n--- Step: ${stepName} ---\n`);
        }
    }
}
function connectEventStream(runId) {
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
            const data = JSON.parse(event.data);
            handleFlowEvent(data);
        }
        catch (err) {
            // Ignore parse errors
        }
    };
    eventSource.onerror = () => {
        setLiveOutputStatus("disconnected");
        // Don't auto-reconnect here - loadTicketFlow will handle it
    };
}
function disconnectEventStream() {
    if (eventSource) {
        eventSource.close();
        eventSource = null;
    }
    setLiveOutputStatus("disconnected");
}
function initLiveOutputPanel() {
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
function initReasonModal() {
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
function els() {
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
        history: document.getElementById("ticket-dispatch-history"),
        dispatchNote: document.getElementById("ticket-dispatch-note"),
        dispatchPanel: document.getElementById("dispatch-panel"),
        dispatchPanelToggle: document.getElementById("dispatch-panel-toggle"),
        dispatchMiniList: document.getElementById("dispatch-mini-list"),
        bootstrapBtn: document.getElementById("ticket-flow-bootstrap"),
        resumeBtn: document.getElementById("ticket-flow-resume"),
        refreshBtn: document.getElementById("ticket-flow-refresh"),
        stopBtn: document.getElementById("ticket-flow-stop"),
        restartBtn: document.getElementById("ticket-flow-restart"),
        archiveBtn: document.getElementById("ticket-flow-archive"),
    };
}
function setButtonsDisabled(disabled) {
    const { bootstrapBtn, resumeBtn, refreshBtn, stopBtn, restartBtn, archiveBtn } = els();
    [bootstrapBtn, resumeBtn, refreshBtn, stopBtn, restartBtn, archiveBtn].forEach((btn) => {
        if (btn)
            btn.disabled = disabled;
    });
}
function truncate(text, max = 100) {
    if (text.length <= max)
        return text;
    return `${text.slice(0, max).trim()}…`;
}
function renderTickets(data) {
    const { tickets, dir, bootstrapBtn } = els();
    if (dir)
        dir.textContent = data?.ticket_dir || "–";
    if (!tickets)
        return;
    tickets.innerHTML = "";
    const list = (data?.tickets || []);
    ticketsExist = list.length > 0;
    // Disable start button if no tickets exist
    if (bootstrapBtn && !bootstrapBtn.disabled) {
        bootstrapBtn.disabled = !ticketsExist;
        if (!ticketsExist) {
            bootstrapBtn.title = "Create a ticket first";
        }
        else {
            bootstrapBtn.title = "";
        }
    }
    if (!list.length) {
        tickets.textContent = "No tickets found. Create TICKET-001.md to begin.";
        return;
    }
    list.forEach((ticket) => {
        const item = document.createElement("div");
        const fm = (ticket.frontmatter || {});
        const done = Boolean(fm?.done);
        // Check if this ticket is currently being worked on
        const isActive = currentActiveTicket && ticket.path === currentActiveTicket && currentFlowStatus === "running";
        item.className = `ticket-item ${done ? "done" : ""} ${isActive ? "active" : ""} clickable`;
        item.title = "Click to edit";
        // Make ticket item clickable to open editor
        item.addEventListener("click", () => {
            openTicketEditor(ticket);
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
            workingBadge.textContent = "Working";
            badges.appendChild(workingBadge);
        }
        // Add DONE badge for completed tickets
        if (done && !isActive) {
            const doneBadge = document.createElement("span");
            doneBadge.className = "ticket-done-badge";
            doneBadge.textContent = "Done";
            badges.appendChild(doneBadge);
        }
        const agent = document.createElement("span");
        agent.className = "ticket-agent";
        agent.textContent = fm?.agent || "codex";
        badges.appendChild(agent);
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
}
function renderDispatchHistory(runId, data) {
    const { history, dispatchNote } = els();
    if (!history)
        return;
    history.innerHTML = "";
    const { dispatchMiniList } = els();
    if (!runId) {
        history.textContent = "Start the ticket flow to see agent dispatches.";
        if (dispatchNote)
            dispatchNote.textContent = "–";
        if (dispatchMiniList)
            dispatchMiniList.innerHTML = "";
        return;
    }
    const entries = (data?.history || []);
    if (!entries.length) {
        history.textContent = "No dispatches yet.";
        if (dispatchNote)
            dispatchNote.textContent = "–";
        if (dispatchMiniList)
            dispatchMiniList.innerHTML = "";
        return;
    }
    if (dispatchNote)
        dispatchNote.textContent = `Latest #${entries[0]?.seq ?? "–"}`;
    // Also render mini list for collapsed panel view
    renderDispatchMiniList(entries);
    entries.forEach((entry) => {
        const dispatch = entry.dispatch;
        const isTurnSummary = dispatch?.mode === "turn_summary" || dispatch?.extra?.is_turn_summary;
        const isHandoff = dispatch?.mode === "pause";
        const container = document.createElement("div");
        container.className = `dispatch-item${isTurnSummary ? " turn-summary" : ""} clickable`;
        container.title = isTurnSummary ? "Agent turn output" : "Click to view in Inbox";
        // Add click handler to navigate to inbox (skip for turn summaries)
        if (!isTurnSummary) {
            container.addEventListener("click", () => {
                if (runId) {
                    // Update URL with run_id so inbox tab loads the right thread
                    const url = new URL(window.location.href);
                    url.searchParams.set("run_id", runId);
                    window.history.replaceState({}, "", url.toString());
                    // Switch to inbox tab
                    activateTab("inbox");
                }
            });
        }
        // Determine mode label
        let modeLabel;
        if (isTurnSummary) {
            modeLabel = "TURN";
        }
        else if (isHandoff) {
            modeLabel = "HANDOFF";
        }
        else {
            modeLabel = (dispatch?.mode || "notify").toUpperCase();
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
        // Add ticket reference if present
        const ticketId = dispatch?.extra?.ticket_id;
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
        container.appendChild(head);
        if (entry.errors && entry.errors.length) {
            const err = document.createElement("div");
            err.className = "ticket-errors";
            err.textContent = entry.errors.join("; ");
            container.appendChild(err);
        }
        const title = dispatch?.title;
        if (title) {
            const titleEl = document.createElement("div");
            titleEl.className = "ticket-body ticket-dispatch-title";
            titleEl.textContent = title;
            container.appendChild(titleEl);
        }
        const bodyText = dispatch?.body;
        if (bodyText) {
            const body = document.createElement("div");
            body.className = "ticket-body ticket-dispatch-body messages-markdown";
            body.innerHTML = renderMarkdown(bodyText);
            container.appendChild(body);
        }
        const attachments = (entry.attachments || []);
        if (attachments.length) {
            const wrap = document.createElement("div");
            wrap.className = "ticket-attachments";
            attachments.forEach((att) => {
                if (!att.url)
                    return;
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
function getFullReason(run) {
    if (!run)
        return null;
    const state = (run.state || {});
    const engine = (state.ticket_engine || {});
    const reason = engine.reason || run.error_message || "";
    const details = engine.reason_details || "";
    if (!reason && !details)
        return null;
    if (details) {
        return `${reason}\n\n${details}`.trim();
    }
    return reason;
}
/**
 * Get a truncated reason summary for display in the grid.
 * Also updates currentReasonFull for modal access.
 */
function summarizeReason(run) {
    if (!run) {
        currentReasonFull = null;
        return "No ticket flow run yet.";
    }
    const state = (run.state || {});
    const engine = (state.ticket_engine || {});
    const fullReason = getFullReason(run);
    currentReasonFull = fullReason;
    const shortReason = engine.reason ||
        run.error_message ||
        (engine.current_ticket ? `Working on ${engine.current_ticket}` : "") ||
        run.status ||
        "";
    // Truncate if too long
    if (shortReason.length > MAX_REASON_LENGTH) {
        return shortReason.slice(0, MAX_REASON_LENGTH - 3) + "...";
    }
    return shortReason;
}
async function loadTicketFiles() {
    const { tickets } = els();
    if (tickets)
        tickets.textContent = "Loading tickets…";
    try {
        const data = (await api("/api/flows/ticket_flow/tickets"));
        renderTickets(data);
    }
    catch (err) {
        renderTickets(null);
        flash(err.message || "Failed to load tickets", "error");
    }
}
/**
 * Open a ticket by its index
 */
async function openTicketByIndex(index) {
    try {
        const data = (await api("/api/flows/ticket_flow/tickets"));
        const ticket = data.tickets?.find((t) => t.index === index);
        if (ticket) {
            openTicketEditor(ticket);
        }
        else {
            flash(`Ticket TICKET-${String(index).padStart(3, "0")} not found`, "error");
        }
    }
    catch (err) {
        flash(`Failed to open ticket: ${err.message}`, "error");
    }
}
async function loadDispatchHistory(runId) {
    const { history } = els();
    if (history)
        history.textContent = "Loading dispatch history…";
    if (!runId) {
        renderDispatchHistory(null, null);
        return;
    }
    try {
        // Use dispatch_history endpoint
        const data = (await api(`/api/flows/${runId}/dispatch_history`));
        renderDispatchHistory(runId, data);
    }
    catch (err) {
        renderDispatchHistory(runId, null);
        flash(err.message || "Failed to load dispatch history", "error");
    }
}
async function loadTicketFlow() {
    const { status, run, current, turn, elapsed, progress, reason, lastActivity, resumeBtn, bootstrapBtn, stopBtn, archiveBtn } = els();
    if (!isRepoHealthy()) {
        if (status)
            statusPill(status, "error");
        if (run)
            run.textContent = "–";
        if (current)
            current.textContent = "–";
        if (turn)
            turn.textContent = "–";
        if (elapsed)
            elapsed.textContent = "–";
        if (progress)
            progress.textContent = "–";
        if (lastActivity)
            lastActivity.textContent = "–";
        if (reason)
            reason.textContent = "Repo offline or uninitialized.";
        setButtonsDisabled(true);
        stopElapsedTimer();
        stopLastActivityTimer();
        disconnectEventStream();
        return;
    }
    try {
        const runs = (await api("/api/flows/runs?flow_type=ticket_flow"));
        // Only consider the newest run - if it's terminal, flow is idle.
        // This matches the backend's _active_or_paused_run() logic which only checks runs[0].
        // Using find() would incorrectly pick up older paused runs when a newer run has completed.
        const newest = runs?.[0] || null;
        // Keep the newest run even if terminal, so we can archive it or see its final state
        const latest = newest;
        currentRunId = latest?.id || null;
        currentFlowStatus = latest?.status || null;
        // Extract ticket engine state
        const ticketEngine = latest?.state?.ticket_engine;
        currentActiveTicket = ticketEngine?.current_ticket || null;
        const ticketTurns = ticketEngine?.ticket_turns ?? null;
        const totalTurns = ticketEngine?.total_turns ?? null;
        if (status)
            statusPill(status, latest?.status || "idle");
        if (run)
            run.textContent = latest?.id || "–";
        if (current)
            current.textContent = currentActiveTicket || "–";
        // Display turn counter
        if (turn) {
            if (ticketTurns !== null && currentFlowStatus === "running") {
                turn.textContent = `${ticketTurns}${totalTurns !== null ? ` (${totalTurns} total)` : ""}`;
            }
            else {
                turn.textContent = "–";
            }
        }
        // Handle elapsed time
        if (latest?.started_at && (latest.status === "running" || latest.status === "pending")) {
            flowStartedAt = new Date(latest.started_at);
            startElapsedTimer();
        }
        else {
            stopElapsedTimer();
            flowStartedAt = null;
            if (elapsed)
                elapsed.textContent = "–";
        }
        if (reason) {
            reason.textContent = summarizeReason(latest) || "–";
            // Add clickable class if there are details to show
            const state = (latest?.state || {});
            const engine = (state.ticket_engine || {});
            const hasDetails = Boolean(engine.reason_details ||
                (currentReasonFull && currentReasonFull.length > MAX_REASON_LENGTH));
            reason.classList.toggle("has-details", hasDetails);
        }
        if (resumeBtn) {
            resumeBtn.disabled = !latest?.id || latest.status !== "paused";
        }
        if (stopBtn) {
            const stoppable = latest?.status === "running" || latest?.status === "pending";
            stopBtn.disabled = !latest?.id || !stoppable;
        }
        await loadTicketFiles();
        // Calculate and display ticket progress (scoped to tickets container only)
        if (progress) {
            const ticketsContainer = document.getElementById("ticket-flow-tickets");
            const doneCount = ticketsContainer?.querySelectorAll(".ticket-item.done").length ?? 0;
            const totalCount = ticketsContainer?.querySelectorAll(".ticket-item").length ?? 0;
            if (totalCount > 0) {
                progress.textContent = `${doneCount} of ${totalCount} done`;
            }
            else {
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
        }
        else {
            disconnectEventStream();
            stopLastActivityTimer();
            if (lastActivity)
                lastActivity.textContent = "–";
            lastActivityTime = null;
        }
        if (bootstrapBtn) {
            const busy = latest?.status === "running" || latest?.status === "pending";
            // Disable if busy OR if no tickets exist
            bootstrapBtn.disabled = busy || !ticketsExist;
            bootstrapBtn.textContent = busy ? "Running…" : "Start Ticket Flow";
            if (!ticketsExist && !busy) {
                bootstrapBtn.title = "Create a ticket first";
            }
            else {
                bootstrapBtn.title = "";
            }
        }
        // Show restart button when flow is paused, stopping, or in terminal state (allows starting fresh)
        const { restartBtn } = els();
        if (restartBtn) {
            const isPaused = latest?.status === "paused";
            const isStopping = latest?.status === "stopping";
            const isTerminal = latest?.status === "completed" ||
                latest?.status === "stopped" ||
                latest?.status === "failed";
            const canRestart = (isPaused || isStopping || isTerminal) && ticketsExist && Boolean(currentRunId);
            restartBtn.style.display = canRestart ? "" : "none";
            restartBtn.disabled = !canRestart;
        }
        // Show archive button when flow is paused, stopping, or in terminal state and has tickets
        if (archiveBtn) {
            const isPaused = latest?.status === "paused";
            const isStopping = latest?.status === "stopping";
            const isTerminal = latest?.status === "completed" ||
                latest?.status === "stopped" ||
                latest?.status === "failed";
            const canArchive = (isPaused || isStopping || isTerminal) && ticketsExist && Boolean(currentRunId);
            archiveBtn.style.display = canArchive ? "" : "none";
            archiveBtn.disabled = !canArchive;
        }
        await loadDispatchHistory(currentRunId);
    }
    catch (err) {
        if (reason)
            reason.textContent = err.message || "Ticket flow unavailable";
        flash(err.message || "Failed to load ticket flow state", "error");
    }
}
async function bootstrapTicketFlow() {
    const { bootstrapBtn } = els();
    if (!bootstrapBtn)
        return;
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
        }));
        currentRunId = res?.id || null;
        if (res?.state?.hint === "active_run_reused") {
            flash("Ticket flow already running; continuing existing run", "info");
        }
        else {
            flash("Ticket flow started");
            clearLiveOutput(); // Clear output for new run
        }
        await loadTicketFlow();
    }
    catch (err) {
        flash(err.message || "Failed to start ticket flow", "error");
    }
    finally {
        bootstrapBtn.textContent = "Start Ticket Flow";
        setButtonsDisabled(false);
    }
}
async function resumeTicketFlow() {
    const { resumeBtn } = els();
    if (!resumeBtn)
        return;
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
    }
    catch (err) {
        flash(err.message || "Failed to resume", "error");
    }
    finally {
        resumeBtn.textContent = "Resume";
        setButtonsDisabled(false);
    }
}
async function stopTicketFlow() {
    const { stopBtn } = els();
    if (!stopBtn)
        return;
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
    }
    catch (err) {
        flash(err.message || "Failed to stop ticket flow", "error");
    }
    finally {
        stopBtn.textContent = "Stop";
        setButtonsDisabled(false);
    }
}
async function restartTicketFlow() {
    const { restartBtn } = els();
    if (!restartBtn)
        return;
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
        }));
        currentRunId = res?.id || null;
        flash("Ticket flow restarted");
        clearLiveOutput();
        await loadTicketFlow();
    }
    catch (err) {
        flash(err.message || "Failed to restart ticket flow", "error");
    }
    finally {
        restartBtn.textContent = "Restart";
        setButtonsDisabled(false);
    }
}
async function archiveTicketFlow() {
    const { archiveBtn, reason } = els();
    if (!archiveBtn)
        return;
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
        // Force archive if flow is stuck in stopping or paused state
        const force = currentFlowStatus === "stopping" || currentFlowStatus === "paused";
        const res = (await api(`/api/flows/${currentRunId}/archive?force=${force}`, {
            method: "POST",
            body: {},
        }));
        const count = res?.tickets_archived ?? 0;
        flash(`Archived ${count} ticket${count !== 1 ? "s" : ""}`);
        clearLiveOutput();
        // Reset all state variables
        currentRunId = null;
        currentFlowStatus = null;
        currentActiveTicket = null;
        currentReasonFull = null;
        // Reset all UI elements to idle state directly (avoid re-fetching stale data)
        const { status, run, current, turn, elapsed, progress, lastActivity, bootstrapBtn, resumeBtn, stopBtn, restartBtn } = els();
        if (status)
            statusPill(status, "idle");
        if (run)
            run.textContent = "–";
        if (current)
            current.textContent = "–";
        if (turn)
            turn.textContent = "–";
        if (elapsed)
            elapsed.textContent = "–";
        if (progress)
            progress.textContent = "–";
        if (lastActivity)
            lastActivity.textContent = "–";
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
        if (resumeBtn)
            resumeBtn.disabled = true;
        if (stopBtn)
            stopBtn.disabled = true;
        if (restartBtn)
            restartBtn.style.display = "none";
        if (archiveBtn)
            archiveBtn.style.display = "none";
        // Refresh inbox badge and ticket list (tickets were archived/moved)
        void refreshBell();
        await loadTicketFiles();
    }
    catch (err) {
        flash(err.message || "Failed to archive ticket flow", "error");
    }
    finally {
        if (archiveBtn) {
            archiveBtn.textContent = "Archive Flow";
        }
        setButtonsDisabled(false);
    }
}
export function initTicketFlow() {
    const { card, bootstrapBtn, resumeBtn, refreshBtn, stopBtn, restartBtn, archiveBtn } = els();
    if (!card || card.dataset.ticketInitialized === "1")
        return;
    card.dataset.ticketInitialized = "1";
    if (bootstrapBtn)
        bootstrapBtn.addEventListener("click", bootstrapTicketFlow);
    if (resumeBtn)
        resumeBtn.addEventListener("click", resumeTicketFlow);
    if (stopBtn)
        stopBtn.addEventListener("click", stopTicketFlow);
    if (restartBtn)
        restartBtn.addEventListener("click", restartTicketFlow);
    if (archiveBtn)
        archiveBtn.addEventListener("click", archiveTicketFlow);
    if (refreshBtn)
        refreshBtn.addEventListener("click", loadTicketFlow);
    // Initialize reason click handler for modal
    initReasonModal();
    // Initialize live output panel
    initLiveOutputPanel();
    // Initialize dispatch panel toggle for medium screens
    initDispatchPanelToggle();
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
        interval: CONSTANTS.UI?.AUTO_REFRESH_INTERVAL ||
            15000,
        refreshOnActivation: true,
        immediate: false,
    });
    subscribe("repo:health", (payload) => {
        const status = payload?.status || "";
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
        }
        else {
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
