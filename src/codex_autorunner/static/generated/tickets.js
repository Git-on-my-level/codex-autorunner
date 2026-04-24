// GENERATED FILE - do not edit directly. Source: static_src/
import { api, confirmModal, flash, getUrlParams, statusPill, getAuthToken, openModal, inputModal, setButtonLoading, } from "./utils.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
import { importVersionedModule } from "./assetLoader.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
import { registerAutoRefresh } from "./autoRefresh.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
import { CONSTANTS } from "./constants.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
import { subscribe } from "./bus.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
import { isRepoHealthy } from "./health.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
import { closeTicketEditor, initTicketEditor, openTicketEditor } from "./ticketEditor.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
import { preserveScroll } from "./preserve.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
import { createSmartRefresh } from "./smartRefresh.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
import { refreshBell } from "./messagesBell.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
import { EVENT_STREAM_RETRY_DELAYS_MS, STALE_THRESHOLD_MS, isFlowActiveStatus, getLastSeenSeq, setLastSeenSeq, parseEventSeq, formatElapsedSeconds, diffStatsSignature, } from "./ticketFlowState.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
import { clearLiveOutput, setLiveOutputStatus, renderLiveOutputView, initLiveOutputPanel, initReasonModal, stopElapsedTimer, initElapsedFromStart, stopLastActivityTimer, updateLastActivityFromTimestamp, updateActivityFromEvent, processStreamDelta, processAppServerEvent, processStepStarted, summarizeReason, resetAllStreamState, setFlowStartedAt, getLastKnownEventAt, setLastActivityTime, getCurrentReasonFull, } from "./ticketFlowStream.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
import { renderTickets, renderDispatchHistory, updateSelectedTicket, updateScrollFade, initDispatchPanelToggle, } from "./ticketFlowView.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
let currentRunId = null;
let ticketsExist = false; // eslint-disable-line prefer-const -- reassigned in loadTicketFlow
let currentActiveTicket = null;
let currentFlowStatus = null;
let lastKnownEventSeq = null;
let eventSource = null;
let eventSourceRunId = null;
let eventSourceRetryAttempt = 0;
let eventSourceRetryTimerId = null;
let dispatchHistoryRunId = null;
let ticketListCache = null;
let ticketFlowLoaded = false;
let loadTicketFlowRequestId = 0;
const ticketListRefresh = createSmartRefresh({
    getSignature: (payload) => {
        const list = (payload.tickets || []);
        const pieces = list.map((ticket) => {
            const fm = (ticket.frontmatter || {});
            const title = fm?.title ? String(fm.title) : "";
            const done = fm?.done ? "1" : "0";
            const agent = fm?.agent ? String(fm.agent) : "";
            const mtime = ticket.mtime ?? "";
            const errors = Array.isArray(ticket.errors) ? ticket.errors.join(",") : "";
            const diff = diffStatsSignature(ticket.diff_stats);
            return [ticket.path ?? "", ticket.index ?? "", title, done, agent, mtime, errors, diff].join("|");
        });
        return [
            payload.activeTicket ?? "",
            payload.flowStatus ?? "",
            pieces.join(";"),
        ].join("::");
    },
    render: (payload) => {
        const ticketsEl = document.getElementById("ticket-flow-tickets");
        preserveScroll(ticketsEl, () => {
            renderTickets({
                tickets: payload.tickets,
            }, {
                currentActiveTicket: payload.activeTicket,
                currentFlowStatus: payload.flowStatus,
                ticketListCache,
            });
        }, { restoreOnNextFrame: true });
    },
    onSkip: (payload) => {
        ticketListCache = {
            tickets: payload.tickets,
        };
        updateScrollFade();
    },
});
const dispatchHistoryRefresh = createSmartRefresh({
    getSignature: (payload) => {
        const entries = payload.history || [];
        const pieces = entries.map((entry) => {
            const diffStats = entry.dispatch?.diff_stats ||
                entry.dispatch?.extra?.diff_stats;
            return [
                entry.seq ?? "",
                entry.dispatch?.mode ?? "",
                entry.dispatch?.title ?? "",
                entry.created_at ?? "",
                diffStatsSignature(diffStats),
            ].join("|");
        });
        return [payload.runId ?? "", entries.length, pieces.join(";")].join("::");
    },
    render: (payload) => {
        const historyEl = document.getElementById("ticket-dispatch-history");
        preserveScroll(historyEl, () => {
            renderDispatchHistory(payload.runId, { history: payload.history });
        }, { restoreOnNextFrame: true });
    },
    onSkip: () => {
        updateScrollFade();
    },
});
function clearEventStreamRetry() {
    if (eventSourceRetryTimerId) {
        clearTimeout(eventSourceRetryTimerId);
        eventSourceRetryTimerId = null;
    }
}
function scheduleEventStreamReconnect(runId) {
    if (eventSourceRetryTimerId)
        return;
    const index = Math.min(eventSourceRetryAttempt, EVENT_STREAM_RETRY_DELAYS_MS.length - 1);
    const delay = EVENT_STREAM_RETRY_DELAYS_MS[index];
    eventSourceRetryAttempt += 1;
    eventSourceRetryTimerId = setTimeout(() => {
        eventSourceRetryTimerId = null;
        if (currentRunId !== runId)
            return;
        if (currentFlowStatus !== "running" && currentFlowStatus !== "pending")
            return;
        connectEventStream(runId);
    }, delay);
}
function handleFlowEvent(event) {
    updateActivityFromEvent(event);
    if (event.event_type === "agent_stream_delta") {
        processStreamDelta(event);
    }
    if (event.event_type === "app_server_event") {
        processAppServerEvent(event.data);
    }
    if (event.event_type === "step_progress") {
        const nextTicket = event.data?.current_ticket;
        if (nextTicket) {
            currentActiveTicket = nextTicket;
            const current = document.getElementById("ticket-flow-current");
            if (current)
                current.textContent = currentActiveTicket;
            if (ticketListCache) {
                renderTickets(ticketListCache, {
                    currentActiveTicket,
                    currentFlowStatus,
                    ticketListCache,
                });
            }
        }
    }
    if (event.event_type === "flow_completed" ||
        event.event_type === "flow_failed" ||
        event.event_type === "flow_stopped") {
        setLiveOutputStatus("connected");
        void loadTicketFlow();
    }
    if (event.event_type === "step_started") {
        processStepStarted(event);
    }
}
function connectEventStream(runId, afterSeq) {
    disconnectEventStream();
    clearEventStreamRetry();
    eventSourceRunId = runId;
    const token = getAuthToken();
    const url = new URL(`/api/flows/${runId}/events`, window.location.origin);
    if (token) {
        url.searchParams.set("token", token);
    }
    if (typeof afterSeq === "number") {
        url.searchParams.set("after", String(afterSeq));
    }
    else {
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
    eventSource.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            const seq = parseEventSeq(data, event.lastEventId);
            if (currentRunId && typeof seq === "number") {
                setLastSeenSeq(currentRunId, seq);
                lastKnownEventSeq = seq;
            }
            handleFlowEvent(data);
        }
        catch (err) {
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
function disconnectEventStream() {
    if (eventSource) {
        eventSource.close();
        eventSource = null;
    }
    clearEventStreamRetry();
    eventSourceRunId = null;
    setLiveOutputStatus("disconnected");
}
export const __ticketFlowTest = {
    clearLiveOutput() {
        clearLiveOutput();
        resetAllStreamState();
        renderLiveOutputView();
    },
    handleFlowEvent,
    initLiveOutputPanel,
    renderLiveOutputView,
    setFlowStartedAt(value) {
        setFlowStartedAt(value);
    },
};
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
        stalePill: document.getElementById("ticket-flow-stale"),
        reconnectBtn: document.getElementById("ticket-flow-reconnect"),
        workerStatus: document.getElementById("ticket-flow-worker"),
        workerPill: document.getElementById("ticket-flow-worker-pill"),
        recoverBtn: document.getElementById("ticket-flow-recover"),
        metaDetails: document.getElementById("ticket-meta-details"),
        dir: document.getElementById("ticket-flow-dir"),
        tickets: document.getElementById("ticket-flow-tickets"),
        history: document.getElementById("ticket-dispatch-history"),
        dispatchNote: document.getElementById("ticket-dispatch-note"),
        dispatchPanel: document.getElementById("dispatch-panel"),
        dispatchPanelToggle: document.getElementById("dispatch-panel-toggle"),
        dispatchMiniList: document.getElementById("dispatch-mini-list"),
        bulkSetAgentBtn: document.getElementById("ticket-bulk-set-agent"),
        bulkClearModelBtn: document.getElementById("ticket-bulk-clear-model"),
        bootstrapBtn: document.getElementById("ticket-flow-bootstrap"),
        resumeBtn: document.getElementById("ticket-flow-resume"),
        refreshBtn: document.getElementById("ticket-flow-refresh"),
        stopBtn: document.getElementById("ticket-flow-stop"),
        restartBtn: document.getElementById("ticket-flow-restart"),
        archiveBtn: document.getElementById("ticket-flow-archive"),
        overflowToggle: document.getElementById("ticket-overflow-toggle"),
        overflowDropdown: document.getElementById("ticket-overflow-dropdown"),
        overflowNew: document.getElementById("ticket-overflow-new"),
        overflowRestart: document.getElementById("ticket-overflow-restart"),
        overflowArchive: document.getElementById("ticket-overflow-archive"),
    };
}
function setButtonsDisabled(disabled) {
    const { bootstrapBtn, resumeBtn, refreshBtn, stopBtn, restartBtn, archiveBtn, reconnectBtn, recoverBtn, bulkSetAgentBtn, bulkClearModelBtn, } = els();
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
        if (btn)
            btn.disabled = disabled;
    });
}
async function loadTicketFiles(ctx) {
    const { tickets } = els();
    const isInitial = ticketListRefresh.getSignature() === null;
    if (tickets && isInitial) {
        tickets.textContent = "Loading tickets…";
    }
    try {
        await ticketListRefresh.refresh(async () => {
            const data = (await api("/api/flows/ticket_flow/tickets"));
            return {
                tickets: data.tickets,
                lint_errors: data.lint_errors,
                activeTicket: currentActiveTicket,
                flowStatus: currentFlowStatus,
            };
        }, { reason: ctx?.reason === "manual" ? "manual" : "background" });
    }
    catch (err) {
        ticketListRefresh.reset();
        ticketListCache = null;
        preserveScroll(tickets, () => {
            renderTickets(null, {
                currentActiveTicket,
                currentFlowStatus,
                ticketListCache,
            });
        }, { restoreOnNextFrame: true });
        flash(err.message || "Failed to load tickets", "error");
    }
}
function summarizeBulkResult(action, payload) {
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
async function bulkSetAgent() {
    const { bulkSetAgentBtn } = els();
    const agent = await inputModal("Set agent for tickets", {
        placeholder: "codex",
        confirmText: "Set agent",
    });
    const agentValue = agent?.trim() || "";
    if (!agentValue)
        return;
    const profile = await inputModal("Optional profile (e.g. Hermes profile). Leave blank to clear and use the default.", {
        placeholder: "m4-pma",
        confirmText: "Apply",
        allowEmpty: true,
    });
    if (profile === null)
        return;
    const range = await inputModal("Optional range (A:B). Leave blank for all tickets.", {
        placeholder: "1:20",
        confirmText: "Apply",
        allowEmpty: true,
    });
    if (range === null)
        return;
    const rangeValue = range.trim() || undefined;
    const trimmedProfile = profile.trim();
    const profileValue = trimmedProfile || null;
    setButtonLoading(bulkSetAgentBtn, true);
    try {
        const payload = (await api("/api/flows/ticket_flow/tickets/bulk-set-agent", {
            method: "POST",
            body: {
                agent: agentValue,
                profile: profileValue,
                range: rangeValue,
            },
        }));
        summarizeBulkResult("Bulk set agent", payload);
        await loadTicketFiles({ reason: "manual" });
    }
    catch (err) {
        flash(err.message || "Failed to bulk set agent", "error");
    }
    finally {
        setButtonLoading(bulkSetAgentBtn, false);
    }
}
async function bulkClearModel() {
    const { bulkClearModelBtn } = els();
    const range = await inputModal("Optional range (A:B). Leave blank for all tickets.", {
        placeholder: "1:20",
        confirmText: "Clear",
        allowEmpty: true,
    });
    if (range === null)
        return;
    const rangeValue = range.trim() || undefined;
    const confirmed = await confirmModal(rangeValue
        ? `Clear model/reasoning overrides for tickets ${rangeValue}?`
        : "Clear model/reasoning overrides for all tickets?", { confirmText: "Clear", cancelText: "Cancel", danger: true });
    if (!confirmed)
        return;
    setButtonLoading(bulkClearModelBtn, true);
    try {
        const payload = (await api("/api/flows/ticket_flow/tickets/bulk-clear-model", {
            method: "POST",
            body: {
                range: rangeValue,
            },
        }));
        summarizeBulkResult("Bulk clear model", payload);
        await loadTicketFiles({ reason: "manual" });
    }
    catch (err) {
        flash(err.message || "Failed to clear model overrides", "error");
    }
    finally {
        setButtonLoading(bulkClearModelBtn, false);
    }
}
async function openTicketByIndex(index) {
    try {
        const data = (await api(`/api/flows/ticket_flow/tickets/${index}`));
        if (data) {
            openTicketEditor(data);
        }
        else {
            flash(`Ticket TICKET-${String(index).padStart(3, "0")} not found`, "error");
        }
    }
    catch (err) {
        flash(`Failed to open ticket: ${err.message}`, "error");
    }
}
async function loadDispatchHistory(runId, ctx) {
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
        await dispatchHistoryRefresh.refresh(async () => {
            const data = (await api(`/api/flows/${runId}/dispatch_history`));
            return {
                runId,
                history: data.history,
            };
        }, {
            reason: ctx?.reason === "manual" ? "manual" : "background",
            force: runChanged,
        });
    }
    catch (err) {
        dispatchHistoryRefresh.reset();
        preserveScroll(history, () => {
            renderDispatchHistory(runId, null);
        }, { restoreOnNextFrame: true });
        flash(err.message || "Failed to load dispatch history", "error");
    }
}
async function loadTicketFlow(ctx) {
    const requestId = ++loadTicketFlowRequestId;
    const isLatestRequest = () => requestId === loadTicketFlowRequestId;
    const { status, run, current, turn, elapsed, progress, reason, lastActivity, stalePill, reconnectBtn, workerStatus, workerPill, recoverBtn, resumeBtn, bootstrapBtn, stopBtn, archiveBtn, refreshBtn, } = els();
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
        if (stalePill)
            stalePill.style.display = "none";
        if (reconnectBtn)
            reconnectBtn.style.display = "none";
        if (workerStatus)
            workerStatus.textContent = "–";
        if (workerPill)
            workerPill.style.display = "none";
        if (recoverBtn)
            recoverBtn.style.display = "none";
        if (reason)
            reason.textContent = "Repo offline or uninitialized.";
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
        const runs = (await api("/api/flows/runs?flow_type=ticket_flow"));
        if (!isLatestRequest())
            return;
        const newest = runs?.[0] || null;
        const latest = newest;
        currentRunId = latest?.id || null;
        currentFlowStatus = latest?.status || null;
        const ticketEngine = latest?.state?.ticket_engine;
        const apiActiveTicket = ticketEngine?.current_ticket || null;
        currentActiveTicket = apiActiveTicket;
        const ticketTurns = ticketEngine?.ticket_turns ?? null;
        const totalTurns = ticketEngine?.total_turns ?? null;
        if (status)
            statusPill(status, latest?.status || "idle");
        if (run)
            run.textContent = latest?.id || "–";
        if (current)
            current.textContent = currentActiveTicket || "–";
        if (turn) {
            if (ticketTurns !== null && isFlowActiveStatus(currentFlowStatus)) {
                turn.textContent = `${ticketTurns}${totalTurns !== null ? ` (${totalTurns} total)` : ""}`;
            }
            else {
                turn.textContent = "–";
            }
        }
        if (latest?.started_at && (latest.status === "running" || latest.status === "pending")) {
            setFlowStartedAt(new Date(latest.started_at).getTime());
            initElapsedFromStart();
        }
        else {
            stopElapsedTimer();
            setFlowStartedAt(null);
            if (elapsed) {
                elapsed.textContent =
                    typeof latest?.duration_seconds === "number"
                        ? formatElapsedSeconds(latest.duration_seconds)
                        : "–";
            }
        }
        if (reason) {
            reason.textContent = summarizeReason(latest) || "–";
            const runState = (latest?.state || {});
            const engine = (runState.ticket_engine || {});
            const hasDetails = Boolean(engine.reason_details ||
                (getCurrentReasonFull() && getCurrentReasonFull().length > 60));
            reason.classList.toggle("has-details", hasDetails);
        }
        lastKnownEventSeq = typeof latest?.last_event_seq === "number" ? latest.last_event_seq : null;
        if (currentRunId && typeof lastKnownEventSeq === "number") {
            setLastSeenSeq(currentRunId, lastKnownEventSeq);
        }
        updateLastActivityFromTimestamp(latest?.last_event_at || null);
        const isActive = latest?.status === "running" || latest?.status === "pending";
        const isStale = Boolean(isActive &&
            getLastKnownEventAt() &&
            Date.now() - getLastKnownEventAt().getTime() > STALE_THRESHOLD_MS);
        if (stalePill)
            stalePill.style.display = isStale ? "" : "none";
        if (reconnectBtn) {
            reconnectBtn.style.display = isStale ? "" : "none";
            reconnectBtn.disabled = !currentRunId;
        }
        const worker = latest?.worker_health;
        const workerLabel = worker?.status
            ? `${worker.status}${worker.pid ? ` (pid ${worker.pid})` : ""}`
            : "–";
        if (workerStatus)
            workerStatus.textContent = workerLabel;
        const workerDead = Boolean(isActive &&
            worker &&
            worker.is_alive === false &&
            worker.status !== "absent");
        if (workerPill)
            workerPill.style.display = workerDead ? "" : "none";
        if (recoverBtn) {
            recoverBtn.style.display = workerDead ? "" : "none";
            recoverBtn.disabled = !currentRunId;
        }
        if (resumeBtn) {
            resumeBtn.disabled = !latest?.id || latest.status !== "paused";
        }
        if (stopBtn) {
            const stoppable = latest?.status === "running" || latest?.status === "pending";
            stopBtn.disabled = !latest?.id || !stoppable;
        }
        await loadTicketFiles(ctx);
        if (!isLatestRequest())
            return;
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
        if (currentRunId && (latest?.status === "running" || latest?.status === "pending")) {
            const isSameRun = eventSourceRunId === currentRunId;
            const isClosed = eventSource?.readyState === EventSource.CLOSED;
            if (!eventSource || !isSameRun || isClosed) {
                connectEventStream(currentRunId);
                // startLastActivityTimer is called inside updateLastActivityFromTimestamp above
            }
        }
        else {
            disconnectEventStream();
            if (!getLastKnownEventAt()) {
                stopLastActivityTimer();
                if (lastActivity)
                    lastActivity.textContent = "–";
                setLastActivityTime(null);
            }
        }
        if (bootstrapBtn) {
            const busy = latest?.status === "running" || latest?.status === "pending";
            bootstrapBtn.disabled = busy;
            bootstrapBtn.textContent = busy ? "Running…" : "Start Ticket Flow";
            bootstrapBtn.title = busy ? "Ticket flow in progress" : "";
        }
        const { restartBtn, overflowRestart } = els();
        if (restartBtn) {
            const isPaused = latest?.status === "paused";
            const isStopping = latest?.status === "stopping";
            const isTerminal = latest?.status === "completed" ||
                latest?.status === "stopped" ||
                latest?.status === "failed";
            const canRestart = (isPaused || isStopping || isTerminal || workerDead) &&
                ticketsExist &&
                Boolean(currentRunId);
            restartBtn.style.display = canRestart ? "" : "none";
            restartBtn.disabled = !canRestart;
            if (overflowRestart) {
                overflowRestart.style.display = canRestart ? "" : "none";
            }
        }
        if (archiveBtn) {
            const isPaused = latest?.status === "paused";
            const isStopping = latest?.status === "stopping";
            const isTerminal = latest?.status === "completed" ||
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
        if (!isLatestRequest())
            return;
    }
    catch (err) {
        if (!isLatestRequest())
            return;
        if (reason)
            reason.textContent = err.message || "Ticket flow unavailable";
        flash(err.message || "Failed to load ticket flow state", "error");
    }
    finally {
        ticketFlowLoaded = true;
        if (showRefreshIndicator) {
            if (isLatestRequest()) {
                setButtonLoading(refreshBtn, false);
            }
        }
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
    setButtonsDisabled(true);
    bootstrapBtn.textContent = "Checking…";
    const startFlow = async () => {
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
            clearLiveOutput();
        }
        await loadTicketFlow();
    };
    const seedIssueFromGithub = async (issueRef) => {
        await api("/api/flows/ticket_flow/seed-issue", {
            method: "POST",
            body: { issue_ref: issueRef },
        });
        flash("ISSUE.md created from GitHub", "success");
    };
    const seedIssueFromPlan = async (planText) => {
        await api("/api/flows/ticket_flow/seed-issue", {
            method: "POST",
            body: { plan_text: planText },
        });
        flash("ISSUE.md created from your input", "success");
    };
    const promptIssueRef = async (repo) => {
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
    const promptPlanText = async () => {
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
        return await new Promise((resolve) => {
            let closeModal = null;
            const cleanup = () => {
                if (closeModal)
                    closeModal();
                overlay.remove();
            };
            const finalize = (value) => {
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
        }));
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
            }
            else {
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
        await startFlow();
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
function reconnectTicketFlowStream() {
    if (!currentRunId) {
        flash("No ticket flow run to reconnect", "info");
        return;
    }
    const afterSeq = typeof lastKnownEventSeq === "number"
        ? lastKnownEventSeq
        : getLastSeenSeq(currentRunId);
    connectEventStream(currentRunId, afterSeq ?? undefined);
    flash("Reconnecting event stream", "info");
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
async function recoverTicketFlow() {
    const { recoverBtn } = els();
    if (!recoverBtn)
        return;
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
    }
    catch (err) {
        flash(err.message || "Failed to recover ticket flow", "error");
    }
    finally {
        recoverBtn.textContent = "Recover";
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
    const confirmed = await confirmModal("Restart ticket flow? This will stop the current run and start a new one.");
    if (!confirmed) {
        return;
    }
    setButtonsDisabled(true);
    restartBtn.textContent = "Restarting…";
    try {
        if (currentRunId) {
            await api(`/api/flows/${currentRunId}/stop`, { method: "POST", body: {} });
        }
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
    const force = currentFlowStatus === "stopping" || currentFlowStatus === "paused";
    if (force) {
        const confirmed = await confirmModal("Archive this incomplete flow? Tickets, contextspace, and run artifacts will move into the run archive and the live workspace state will be reset.");
        if (!confirmed) {
            return;
        }
    }
    setButtonsDisabled(true);
    archiveBtn.textContent = "Archiving…";
    try {
        const res = (await api(`/api/flows/${currentRunId}/archive?force=${force}`, {
            method: "POST",
            body: {},
        }));
        const count = res?.tickets_archived ?? 0;
        flash(`Archived ${count} ticket${count !== 1 ? "s" : ""}`);
        clearLiveOutput();
        resetAllStreamState();
        currentRunId = null;
        currentFlowStatus = null;
        currentActiveTicket = null;
        const { status, run, current, turn, elapsed, progress, lastActivity, stalePill, reconnectBtn, workerStatus, workerPill, recoverBtn, bootstrapBtn, resumeBtn, stopBtn, restartBtn, archiveBtn } = els();
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
        if (stalePill)
            stalePill.style.display = "none";
        if (reconnectBtn)
            reconnectBtn.style.display = "none";
        if (workerStatus)
            workerStatus.textContent = "–";
        if (workerPill)
            workerPill.style.display = "none";
        if (recoverBtn)
            recoverBtn.style.display = "none";
        if (reason) {
            reason.textContent = "No ticket flow run yet.";
            reason.classList.remove("has-details");
        }
        renderDispatchHistory(null, null);
        disconnectEventStream();
        stopElapsedTimer();
        stopLastActivityTimer();
        setLastActivityTime(null);
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
        const { overflowRestart, overflowArchive } = els();
        if (overflowRestart)
            overflowRestart.style.display = "none";
        if (archiveBtn)
            archiveBtn.style.display = "none";
        if (overflowArchive)
            overflowArchive.style.display = "none";
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
    const { card, bootstrapBtn, resumeBtn, refreshBtn, stopBtn, restartBtn, archiveBtn, reconnectBtn, recoverBtn, bulkSetAgentBtn, bulkClearModelBtn, } = els();
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
    if (reconnectBtn)
        reconnectBtn.addEventListener("click", reconnectTicketFlowStream);
    if (recoverBtn)
        recoverBtn.addEventListener("click", recoverTicketFlow);
    if (bulkSetAgentBtn)
        bulkSetAgentBtn.addEventListener("click", () => void bulkSetAgent());
    if (bulkClearModelBtn)
        bulkClearModelBtn.addEventListener("click", () => void bulkClearModel());
    if (refreshBtn)
        refreshBtn.addEventListener("click", () => {
            void loadTicketFlow({ reason: "manual" });
        });
    const { overflowToggle, overflowDropdown, overflowNew, overflowRestart, overflowArchive } = els();
    if (overflowToggle && overflowDropdown) {
        const toggleMenu = (e) => {
            e.preventDefault();
            e.stopPropagation();
            const isHidden = overflowDropdown.classList.contains("hidden");
            overflowDropdown.classList.toggle("hidden", !isHidden);
        };
        const closeMenu = () => overflowDropdown.classList.add("hidden");
        overflowToggle.addEventListener("pointerdown", toggleMenu);
        overflowToggle.addEventListener("click", (e) => {
            e.preventDefault();
        });
        overflowToggle.addEventListener("keydown", (e) => {
            if (e.key === "Enter" || e.key === " ")
                toggleMenu(e);
        });
        document.addEventListener("pointerdown", (e) => {
            if (!overflowDropdown.classList.contains("hidden") &&
                !overflowToggle.contains(e.target) &&
                !overflowDropdown.contains(e.target)) {
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
    initReasonModal();
    initLiveOutputPanel();
    initDispatchPanelToggle();
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
            const { startNewTicketChatThread } = await importVersionedModule("./ticketChatActions.js");
            await startNewTicketChatThread();
        });
    }
    initTicketEditor();
    loadTicketFlow();
    registerAutoRefresh("ticket-flow", {
        callback: async (ctx) => {
            await loadTicketFlow(ctx);
        },
        tabId: "tickets",
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
    subscribe("tickets:updated", () => {
        void loadTicketFiles();
    });
    subscribe("ticket-editor:opened", (payload) => {
        const data = payload;
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
    subscribe("ticket-editor:closed", () => {
        updateSelectedTicket(null);
    });
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
    const params = getUrlParams();
    const ticketIndex = params.get("ticket");
    if (ticketIndex) {
        void openTicketByIndex(parseInt(ticketIndex, 10));
    }
}
