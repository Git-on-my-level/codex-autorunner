// GENERATED FILE - do not edit directly. Source: static_src/
/**
 * Ticket Chat Actions - handles sending messages, applying/discarding patches
 */
import { api, confirmModal, flash, splitMarkdownFrontmatter } from "./utils.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
import { performTicketChatRequest } from "./ticketChatStream.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
import { renderTicketMessages, renderTicketEvents } from "./ticketChatEvents.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
import { publish } from "./bus.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
import { createDocChat } from "./docChatCore.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
import { saveTicketChatHistory } from "./ticketChatStorage.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
import { renderDiff } from "./diffRenderer.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
import { newClientTurnId } from "./fileChat.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
import { loadPendingTurn, savePendingTurn } from "./turnResume.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
import { resumeFileChatTurn } from "./turnEvents.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
import { createTurnEventsController, startTurnEventsStream as sharedStartTurnEventsStream, clearManagedTurn, cancelActiveTurnSync, scheduleRecoveryRetry, createTurnRecoveryTracker, ACTIVE_TURN_RECOVERY_STALE_MESSAGE, } from "./sharedTurnLifecycle.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
import { getSelectedAgent, getSelectedProfile, getSelectedModel, getSelectedReasoning, setSelectedAgentProfile, } from "./agentControls.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
// Limits for events display
export const TICKET_CHAT_EVENT_LIMIT = 8;
export const TICKET_CHAT_EVENT_MAX = 50;
function encodeTicketChatScope(value) {
    return encodeURIComponent(value.trim().toLowerCase());
}
function buildScopedTicketChatTarget(index, ticketChatKey, agent, profile) {
    const baseTarget = ticketChatKey || (index != null ? `ticket:${index}` : null);
    if (!baseTarget)
        return null;
    const normalizedAgent = encodeTicketChatScope(agent || "codex");
    const normalizedProfile = profile ? encodeTicketChatScope(profile) : "";
    return normalizedProfile
        ? `${baseTarget}|agent=${normalizedAgent}|profile=${normalizedProfile}`
        : `${baseTarget}|agent=${normalizedAgent}`;
}
function parseScopedTicketChatTarget(target) {
    const [baseTarget, ...parts] = target.split("|");
    let agent = "codex";
    let profile;
    for (const part of parts) {
        if (part.startsWith("agent=")) {
            agent = decodeURIComponent(part.slice("agent=".length)) || "codex";
        }
        else if (part.startsWith("profile=")) {
            const decoded = decodeURIComponent(part.slice("profile=".length));
            profile = decoded || undefined;
        }
    }
    return { baseTarget, agent, profile };
}
function pendingKeyForTarget(target) {
    return `car.ticketChat.pending.${target}`;
}
function setActiveTurnScope(target, pendingKey) {
    ticketChatState.activeTarget = target;
    ticketChatState.activePendingKey = pendingKey;
    if (!target) {
        ticketChatState.activeAgent = null;
        ticketChatState.activeProfile = null;
        return;
    }
    const parsed = parseScopedTicketChatTarget(target);
    ticketChatState.activeAgent = parsed.agent;
    ticketChatState.activeProfile = parsed.profile || null;
}
function clearActiveTurnScope() {
    setActiveTurnScope(null, null);
}
function resolveCurrentTicketChatSelection() {
    const els = getTicketChatElements();
    const agent = els.agentSelect
        ? (els.agentSelect.value || "codex")
        : (getSelectedAgent() || "codex");
    const profile = els.profileSelect
        ? (els.profileSelect.value || undefined)
        : (getSelectedProfile(agent) || undefined);
    return { agent, profile };
}
function chatTargetForTicket(index, ticketChatKey, agent, profile) {
    const selection = agent
        ? { agent, profile }
        : resolveCurrentTicketChatSelection();
    return buildScopedTicketChatTarget(index, ticketChatKey, selection.agent, selection.profile);
}
const pendingKeyForTicket = (index, ticketChatKey, agent, profile) => {
    const target = chatTargetForTicket(index, ticketChatKey, agent, profile);
    return target ? `car.ticketChat.pending.${target}` : "car.ticketChat.pending";
};
export const ticketChat = createDocChat({
    idPrefix: "ticket-chat",
    storage: { keyPrefix: "car-ticket-chat-", maxMessages: 50, version: 1 },
    limits: { eventVisible: TICKET_CHAT_EVENT_LIMIT, eventMax: TICKET_CHAT_EVENT_MAX },
    styling: {
        eventClass: "ticket-chat-event",
        eventTitleClass: "ticket-chat-event-title",
        eventSummaryClass: "ticket-chat-event-summary",
        eventDetailClass: "ticket-chat-event-detail",
        eventMetaClass: "ticket-chat-event-meta",
        eventsEmptyClass: "ticket-chat-events-empty",
        eventsWaitingClass: "ticket-chat-events-waiting",
        eventsHiddenClass: "hidden",
        messagesClass: "ticket-chat-message",
        messageRoleClass: "ticket-chat-message-role",
        messageContentClass: "ticket-chat-message-content",
        messageMetaClass: "ticket-chat-message-meta",
        messageUserClass: "user",
        messageAssistantClass: "assistant",
        messageAssistantThinkingClass: "thinking",
        messageAssistantFinalClass: "final",
    },
});
// Extend state with ticket-specific fields
export const ticketChatState = Object.assign(ticketChat.state, {
    ticketIndex: null,
    ticketChatKey: null,
    draft: null,
    contextUsagePercent: null,
    activeTarget: null,
    activePendingKey: null,
    activeAgent: null,
    activeProfile: null,
});
const turnEventsCtrl = createTurnEventsController();
export function getTicketChatElements() {
    const base = ticketChat.elements;
    return {
        input: base.input,
        sendBtn: base.sendBtn,
        voiceBtn: base.voiceBtn,
        cancelBtn: base.cancelBtn,
        newThreadBtn: base.newThreadBtn,
        statusEl: base.statusEl,
        streamEl: base.streamEl,
        eventsMain: base.eventsMain,
        eventsList: base.eventsList,
        eventsCount: base.eventsCount,
        eventsToggle: base.eventsToggle,
        messagesEl: base.messagesEl,
        // Content area elements - mutually exclusive with patch preview
        contentTextarea: document.getElementById("ticket-editor-content"),
        contentToolbar: document.getElementById("ticket-editor-toolbar"),
        // Patch preview elements - mutually exclusive with content area
        patchMain: document.getElementById("ticket-patch-main"),
        patchBody: document.getElementById("ticket-patch-body"),
        patchStatus: document.getElementById("ticket-patch-status"),
        applyBtn: document.getElementById("ticket-patch-apply"),
        discardBtn: document.getElementById("ticket-patch-discard"),
        agentSelect: document.getElementById("ticket-chat-agent-select"),
        profileSelect: document.getElementById("ticket-chat-profile-select"),
        modelSelect: document.getElementById("ticket-chat-model-select"),
        modelInput: document.getElementById("ticket-chat-model-input"),
        reasoningSelect: document.getElementById("ticket-chat-reasoning-select"),
    };
}
function resolveTicketChatModel(agent, controls) {
    const selectedModel = controls.modelSelect?.value || "";
    if (selectedModel)
        return selectedModel;
    const manualModel = controls.modelInput?.value?.trim() || "";
    if (manualModel)
        return manualModel;
    return getSelectedModel(agent) || undefined;
}
export function resetTicketChatState() {
    ticketChatState.status = "idle";
    ticketChatState.error = "";
    ticketChatState.streamText = "";
    ticketChatState.statusText = "";
    ticketChatState.controller = null;
    ticketChatState.contextUsagePercent = null;
    // Note: events are cleared at the start of each new request, not here
    // Messages persist across requests within the same ticket
}
export async function restoreTicketChatSelectionToActiveTurn() {
    if (!ticketChatState.activeAgent)
        return;
    await setSelectedAgentProfile(ticketChatState.activeAgent, ticketChatState.activeProfile || "");
}
export async function startNewTicketChatThread() {
    if (ticketChatState.ticketIndex == null)
        return;
    const confirmed = await confirmModal("Start a new conversation thread for this ticket?");
    if (!confirmed)
        return;
    try {
        const els = getTicketChatElements();
        const agent = els.agentSelect
            ? (els.agentSelect.value || "codex")
            : (getSelectedAgent() || "codex");
        const profile = els.profileSelect
            ? (els.profileSelect.value || undefined)
            : (getSelectedProfile(agent) || undefined);
        await api(`/api/tickets/${ticketChatState.ticketIndex}/chat/new-thread`, {
            method: "POST",
            body: { agent, profile },
        });
        // Clear local message history
        const localHistoryKey = chatTargetForTicket(ticketChatState.ticketIndex, ticketChatState.ticketChatKey);
        ticketChatState.messages = [];
        if (localHistoryKey) {
            saveTicketChatHistory(localHistoryKey, []);
        }
        clearTicketEvents();
        flash("New thread started");
    }
    catch (err) {
        flash(`Failed to reset thread: ${err.message}`, "error");
    }
    finally {
        renderTicketChat();
        renderTicketMessages();
    }
}
/**
 * Clear events at the start of a new request.
 * Events are transient (thinking/tool calls) and reset each turn.
 */
export function clearTicketEvents() {
    ticketChat.clearEvents();
}
function clearPendingTurnState(pendingKey) {
    clearManagedTurn(turnEventsCtrl, pendingKey);
}
function handleTicketTurnMeta(update) {
    const threadId = typeof update.thread_id === "string" ? update.thread_id : "";
    const turnId = typeof update.turn_id === "string" ? update.turn_id : "";
    const agent = typeof update.agent === "string" ? update.agent : "codex";
    if (!threadId || !turnId)
        return;
    sharedStartTurnEventsStream(turnEventsCtrl, { agent, threadId, turnId }, {
        onEvent: (event) => {
            ticketChat.applyAppEvent(event);
            ticketChat.renderEvents();
            ticketChat.render();
        },
    });
}
export function applyTicketChatResult(payload) {
    if (!payload || typeof payload !== "object")
        return;
    if (ticketChatState.activeTarget && ticketChatState.target !== ticketChatState.activeTarget) {
        ticketChat.setTarget(ticketChatState.activeTarget);
    }
    const result = payload;
    handleTicketTurnMeta(result);
    if (result.status === "interrupted") {
        ticketChatState.status = "interrupted";
        ticketChatState.error = "";
        addAssistantMessage("Request interrupted", true);
        renderTicketChat();
        renderTicketMessages();
        return;
    }
    if (result.status === "error" || result.error) {
        ticketChatState.status = "error";
        ticketChatState.error =
            result.detail || result.error || "Chat failed";
        addAssistantMessage(`Error: ${ticketChatState.error}`, true);
        renderTicketChat();
        renderTicketMessages();
        return;
    }
    // Success
    ticketChatState.status = "done";
    if (result.message) {
        ticketChatState.streamText = result.message;
    }
    if (result.agent_message || result.agentMessage) {
        ticketChatState.statusText =
            result.agent_message || result.agentMessage || "";
    }
    // Check for draft/patch in response
    const hasDraft = result.has_draft ?? result.hasDraft;
    if (hasDraft === false) {
        ticketChatState.draft = null;
    }
    else if (hasDraft === true || result.draft || result.patch || result.content) {
        ticketChatState.draft = {
            content: result.content || "",
            patch: result.patch || "",
            agentMessage: result.agent_message || result.agentMessage || "",
            createdAt: result.created_at || result.createdAt || "",
            baseHash: result.base_hash || result.baseHash || "",
            isStale: result.is_stale ?? result.isStale ?? false,
        };
    }
    // Add assistant message from response
    const responseText = ticketChatState.streamText ||
        ticketChatState.statusText ||
        (ticketChatState.draft ? "Changes ready to apply" : "Done");
    if (responseText && ticketChatState.messages.length > 0) {
        // Only add if we have messages (i.e., a user message was sent)
        const lastMessage = ticketChatState.messages[ticketChatState.messages.length - 1];
        // Avoid duplicate assistant messages
        if (lastMessage.role === "user") {
            addAssistantMessage(responseText, true);
        }
    }
    renderTicketChat();
    renderTicketMessages();
    renderTicketEvents();
}
/**
 * Add a user message to the chat history.
 */
export function addUserMessage(content) {
    ticketChat.addUserMessage(content);
}
/**
 * Add an assistant message to the chat history.
 * Prevents duplicates by checking if the same content was just added.
 */
export function addAssistantMessage(content, isFinal = true) {
    ticketChat.addAssistantMessage(content, isFinal);
}
export function setTicketIndex(index, ticketChatKey = null) {
    const nextTarget = chatTargetForTicket(index, ticketChatKey);
    const changed = ticketChatState.ticketIndex !== index ||
        ticketChatState.target !== nextTarget;
    ticketChatState.ticketIndex = index;
    ticketChatState.ticketChatKey = ticketChatKey;
    ticketChatState.draft = null;
    clearActiveTurnScope();
    resetTicketChatState();
    turnEventsCtrl.abort();
    // Clear chat history when switching tickets
    if (changed) {
        ticketChat.setTarget(nextTarget);
    }
}
export function syncTicketChatTargetToSelection() {
    if (ticketChatState.ticketIndex == null && !ticketChatState.ticketChatKey) {
        return;
    }
    if (ticketChatState.status === "running") {
        if (ticketChatState.activeTarget && ticketChatState.target !== ticketChatState.activeTarget) {
            ticketChat.setTarget(ticketChatState.activeTarget);
        }
        return;
    }
    const nextTarget = chatTargetForTicket(ticketChatState.ticketIndex, ticketChatState.ticketChatKey);
    if (ticketChatState.target === nextTarget) {
        return;
    }
    ticketChatState.draft = null;
    resetTicketChatState();
    turnEventsCtrl.abort();
    ticketChat.setTarget(nextTarget);
}
export function renderTicketChat() {
    const els = getTicketChatElements();
    const controlsLocked = ticketChatState.status === "running";
    if (els.agentSelect)
        els.agentSelect.disabled = controlsLocked;
    if (els.profileSelect)
        els.profileSelect.disabled = controlsLocked;
    // Shared chat render (status, events, messages)
    ticketChat.render();
    // MUTUALLY EXCLUSIVE: Show either the content editor OR the patch preview, never both.
    // This prevents confusion about which view is the "current" state.
    const hasDraft = !!ticketChatState.draft;
    // Hide content area when showing patch preview
    if (els.contentTextarea) {
        els.contentTextarea.classList.toggle("hidden", hasDraft);
    }
    if (els.contentToolbar) {
        els.contentToolbar.classList.toggle("hidden", hasDraft);
    }
    // Show patch preview only when there's a draft
    if (els.patchMain) {
        els.patchMain.classList.toggle("hidden", !hasDraft);
        if (hasDraft) {
            if (els.patchBody) {
                renderDiff(ticketChatState.draft.patch || "(no changes)", els.patchBody);
            }
            if (els.patchStatus) {
                const draft = ticketChatState.draft;
                const staleNotice = draft.isStale
                    ? "⚠ File changed since this draft was created. Applying may overwrite newer changes."
                    : "";
                els.patchStatus.textContent = staleNotice || draft.agentMessage || "";
                els.patchStatus.classList.toggle("pill-warn", draft.isStale);
                els.patchStatus.classList.toggle("muted", !draft.isStale);
            }
        }
    }
}
export async function sendTicketChat() {
    const els = getTicketChatElements();
    const message = (els.input?.value || "").trim();
    if (!message) {
        ticketChatState.error = "Enter a message to send.";
        renderTicketChat();
        return;
    }
    if (ticketChatState.status === "running") {
        cancelActiveTurnSync({
            abortController() {
                if (ticketChatState.controller) {
                    ticketChatState.controller.abort();
                    ticketChatState.controller = null;
                }
            },
            turnEventsCtrl,
            interruptServer: async () => {
                if (ticketChatState.ticketIndex != null) {
                    await api(`/api/tickets/${ticketChatState.ticketIndex}/chat/interrupt`, {
                        method: "POST",
                    });
                }
            },
            clearPending: () => {
                if (ticketChatState.activePendingKey) {
                    clearPendingTurnState(ticketChatState.activePendingKey);
                }
                clearActiveTurnScope();
            },
        });
    }
    if (ticketChatState.ticketIndex == null) {
        ticketChatState.error = "No ticket selected.";
        renderTicketChat();
        return;
    }
    resetTicketChatState();
    ticketChatState.status = "running";
    ticketChatState.statusText = "queued";
    turnEventsCtrl.abort();
    ticketChatState.controller = new AbortController();
    const agent = els.agentSelect
        ? (els.agentSelect.value || "codex")
        : (getSelectedAgent() || "codex");
    const profile = els.profileSelect
        ? (els.profileSelect.value || undefined)
        : (getSelectedProfile(agent) || undefined);
    const pendingKey = pendingKeyForTicket(ticketChatState.ticketIndex, ticketChatState.ticketChatKey, agent, profile);
    const clientTurnId = newClientTurnId("ticket");
    const targetKey = chatTargetForTicket(ticketChatState.ticketIndex, ticketChatState.ticketChatKey, agent, profile);
    savePendingTurn(pendingKey, {
        clientTurnId,
        message,
        startedAtMs: Date.now(),
        target: targetKey || "ticket",
    });
    setActiveTurnScope(targetKey, pendingKey);
    if (targetKey && ticketChatState.target !== targetKey) {
        ticketChat.setTarget(targetKey);
    }
    renderTicketChat();
    if (els.input) {
        els.input.value = "";
    }
    const model = resolveTicketChatModel(agent, els);
    const reasoning = els.reasoningSelect
        ? (els.reasoningSelect.value || undefined)
        : (getSelectedReasoning(agent) || undefined);
    try {
        await performTicketChatRequest(ticketChatState.ticketIndex, message, ticketChatState.controller.signal, {
            agent,
            profile,
            model,
            reasoning,
            clientTurnId,
        });
        // Try to load any pending draft
        await loadTicketPending(ticketChatState.ticketIndex, true);
        if (ticketChatState.status === "running") {
            ticketChatState.status = "done";
        }
        clearPendingTurnState(pendingKey);
    }
    catch (err) {
        const error = err;
        if (error.name === "AbortError") {
            ticketChatState.status = "interrupted";
            ticketChatState.error = "";
        }
        else {
            ticketChatState.status = "error";
            ticketChatState.error = error.message || "Ticket chat failed";
        }
        clearPendingTurnState(pendingKey);
    }
    finally {
        ticketChatState.controller = null;
        clearActiveTurnScope();
        renderTicketChat();
    }
}
export const __ticketChatActionsTest = {
    buildScopedTicketChatTarget,
    findPendingTicketTurn,
    pendingKeyForTicket,
    parseScopedTicketChatTarget,
    resolveTicketChatModel,
    applyTicketChatResult,
    getTicketChatState: () => ticketChatState,
};
export async function cancelTicketChat() {
    if (ticketChatState.status !== "running")
        return;
    // Abort the request
    if (ticketChatState.controller) {
        ticketChatState.controller.abort();
    }
    turnEventsCtrl.abort();
    // Send interrupt to server
    if (ticketChatState.ticketIndex != null) {
        try {
            await api(`/api/tickets/${ticketChatState.ticketIndex}/chat/interrupt`, {
                method: "POST",
            });
        }
        catch (err) {
            // Ignore interrupt errors
        }
    }
    ticketChatState.status = "interrupted";
    ticketChatState.error = "";
    ticketChatState.statusText = "";
    ticketChatState.controller = null;
    renderTicketChat();
    if (ticketChatState.activePendingKey) {
        clearPendingTurnState(ticketChatState.activePendingKey);
    }
    clearActiveTurnScope();
}
function findPendingTicketTurn(index, ticketChatKey, agent, profile) {
    const preferredTarget = chatTargetForTicket(index, ticketChatKey, agent, profile);
    if (preferredTarget) {
        const preferredKey = pendingKeyForTarget(preferredTarget);
        const preferredPending = loadPendingTurn(preferredKey);
        if (preferredPending?.target === preferredTarget) {
            return { pendingKey: preferredKey, pending: preferredPending, target: preferredTarget };
        }
    }
    const baseTarget = ticketChatKey || (index != null ? `ticket:${index}` : null);
    if (!baseTarget)
        return null;
    try {
        for (let idx = 0; idx < localStorage.length; idx += 1) {
            const key = localStorage.key(idx);
            if (!key || !key.startsWith("car.ticketChat.pending."))
                continue;
            const pending = loadPendingTurn(key);
            if (!pending?.target)
                continue;
            const parsed = parseScopedTicketChatTarget(pending.target);
            if (parsed.baseTarget !== baseTarget)
                continue;
            return { pendingKey: key, pending, target: pending.target };
        }
    }
    catch {
        return null;
    }
    return null;
}
export async function resumeTicketPendingTurn(index, ticketChatKey = null) {
    if (index == null)
        return;
    const pendingMatch = findPendingTicketTurn(index, ticketChatKey);
    if (!pendingMatch)
        return;
    const { pendingKey, target } = pendingMatch;
    const parsedTarget = parseScopedTicketChatTarget(target);
    await setSelectedAgentProfile(parsedTarget.agent, parsedTarget.profile || "");
    setActiveTurnScope(target, pendingKey);
    if (ticketChatState.target !== target) {
        ticketChat.setTarget(target);
    }
    const chatState = ticketChatState;
    chatState.status = "running";
    chatState.statusText = "Recovering previous turn…";
    ticketChat.render();
    ticketChat.renderMessages();
    await doResumeTicketTurn(pendingMatch, createTurnRecoveryTracker());
}
async function doResumeTicketTurn(pendingMatch, tracker) {
    const { pendingKey, pending } = pendingMatch;
    const chatState = ticketChatState;
    const onStale = () => {
        chatState.status = "error";
        chatState.error = ACTIVE_TURN_RECOVERY_STALE_MESSAGE;
        clearPendingTurnState(pendingKey);
        clearActiveTurnScope();
        renderTicketChat();
    };
    try {
        const outcome = await resumeFileChatTurn(pending.clientTurnId, {
            onEvent: (event) => {
                ticketChat.applyAppEvent(event);
                ticketChat.renderEvents();
                ticketChat.render();
            },
            onResult: (result) => {
                applyTicketChatResult(result);
                const status = result.status;
                if (status === "ok" || status === "error" || status === "interrupted") {
                    clearPendingTurnState(pendingKey);
                    clearActiveTurnScope();
                }
            },
            onError: (msg) => {
                chatState.statusText = msg;
                renderTicketChat();
            },
        });
        turnEventsCtrl.current = outcome.controller;
        if (outcome.lastResult && outcome.lastResult.status) {
            applyTicketChatResult(outcome.lastResult);
            clearPendingTurnState(pendingKey);
            clearActiveTurnScope();
            return;
        }
        if (!outcome.controller) {
            scheduleRecoveryRetry({
                tracker,
                retryFn: () => doResumeTicketTurn(pendingMatch, tracker),
                onStale,
            });
        }
    }
    catch (err) {
        const msg = err.message || "Failed to resume turn";
        chatState.statusText = msg;
        renderTicketChat();
        scheduleRecoveryRetry({
            tracker,
            retryFn: () => doResumeTicketTurn(pendingMatch, tracker),
            onStale,
        });
    }
}
export async function applyTicketPatch() {
    if (ticketChatState.ticketIndex == null) {
        flash("No ticket selected", "error");
        return;
    }
    if (!ticketChatState.draft) {
        flash("No draft to apply", "error");
        return;
    }
    if (ticketChatState.draft.isStale) {
        const confirmed = await confirmModal("This draft is stale — the underlying file has changed since it was created. Apply anyway?");
        if (!confirmed)
            return;
    }
    try {
        const res = await api(`/api/tickets/${ticketChatState.ticketIndex}/chat/apply`, { method: "POST", body: { force: ticketChatState.draft.isStale } });
        ticketChatState.draft = null;
        flash("Draft applied");
        // Notify that tickets changed
        publish("tickets:updated", {});
        // Update the editor textarea if content is returned
        if (res.content) {
            const textarea = document.getElementById("ticket-editor-content");
            if (textarea) {
                const [fmYaml, body] = splitMarkdownFrontmatter(res.content);
                if (fmYaml !== null) {
                    textarea.value = body.trimStart();
                }
                else {
                    textarea.value = res.content.trimStart();
                }
                // Trigger input event to update undo stack and autosave
                textarea.dispatchEvent(new Event("input", { bubbles: true }));
            }
        }
    }
    catch (err) {
        const error = err;
        flash(error.message || "Failed to apply draft", "error");
    }
    finally {
        renderTicketChat();
    }
}
export async function discardTicketPatch() {
    if (ticketChatState.ticketIndex == null) {
        flash("No ticket selected", "error");
        return;
    }
    try {
        await api(`/api/tickets/${ticketChatState.ticketIndex}/chat/discard`, { method: "POST" });
        ticketChatState.draft = null;
        flash("Draft discarded");
    }
    catch (err) {
        const error = err;
        flash(error.message || "Failed to discard draft", "error");
    }
    finally {
        renderTicketChat();
    }
}
export async function loadTicketPending(index, silent = false) {
    try {
        const res = await api(`/api/tickets/${index}/chat/pending`, { method: "GET" });
        ticketChatState.draft = {
            patch: res.patch || "",
            content: res.content || "",
            agentMessage: res.agent_message || "",
            createdAt: res.created_at || "",
            baseHash: res.base_hash || "",
            isStale: res.is_stale ?? false,
        };
        if (!silent) {
            flash("Loaded pending draft");
        }
    }
    catch (err) {
        const error = err;
        const message = error?.message || "";
        if (message.includes("No pending")) {
            ticketChatState.draft = null;
            if (!silent) {
                flash("No pending draft");
            }
        }
        else if (!silent) {
            flash(message || "Failed to load pending draft", "error");
        }
    }
    finally {
        renderTicketChat();
    }
}
