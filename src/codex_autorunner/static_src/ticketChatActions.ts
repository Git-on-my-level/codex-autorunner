/**
 * Ticket Chat Actions - handles sending messages, applying/discarding patches
 */
import { api, confirmModal, flash, splitMarkdownFrontmatter } from "./utils.js";
import { performTicketChatRequest } from "./ticketChatStream.js";
import { renderTicketMessages, renderTicketEvents } from "./ticketChatEvents.js";
import { publish } from "./bus.js";
import { createDocChat, type ChatState, type ChatStatus } from "./docChatCore.js";
import { saveTicketChatHistory } from "./ticketChatStorage.js";
import { renderDiff } from "./diffRenderer.js";
import { newClientTurnId } from "./fileChat.js";
import { loadPendingTurn, savePendingTurn } from "./turnResume.js";
import { resumeFileChatTurn } from "./turnEvents.js";
import {
  createTurnEventsController,
  startTurnEventsStream as sharedStartTurnEventsStream,
  clearManagedTurn,
} from "./sharedTurnLifecycle.js";
import {
  getSelectedAgent,
  getSelectedProfile,
  getSelectedModel,
  getSelectedReasoning,
  setSelectedAgentProfile,
} from "./agentControls.js";

export type TicketChatStatus = ChatStatus;

export interface TicketDraft {
  content: string;
  patch: string;
  agentMessage: string;
  createdAt: string;
  baseHash: string;
}

export interface TicketChatState extends ChatState {
  ticketIndex: number | null;
  ticketChatKey: string | null;
  draft: TicketDraft | null;
  contextUsagePercent: number | null;
  activeTarget: string | null;
  activePendingKey: string | null;
  activeAgent: string | null;
  activeProfile: string | null;
}

// Limits for events display
export const TICKET_CHAT_EVENT_LIMIT = 8;
export const TICKET_CHAT_EVENT_MAX = 50;
function encodeTicketChatScope(value: string): string {
  return encodeURIComponent(value.trim().toLowerCase());
}

function buildScopedTicketChatTarget(
  index: number | null,
  ticketChatKey: string | null,
  agent: string,
  profile?: string
): string | null {
  const baseTarget = ticketChatKey || (index != null ? `ticket:${index}` : null);
  if (!baseTarget) return null;
  const normalizedAgent = encodeTicketChatScope(agent || "codex");
  const normalizedProfile = profile ? encodeTicketChatScope(profile) : "";
  return normalizedProfile
    ? `${baseTarget}|agent=${normalizedAgent}|profile=${normalizedProfile}`
    : `${baseTarget}|agent=${normalizedAgent}`;
}

function parseScopedTicketChatTarget(target: string): {
  baseTarget: string;
  agent: string;
  profile?: string;
} {
  const [baseTarget, ...parts] = target.split("|");
  let agent = "codex";
  let profile: string | undefined;
  for (const part of parts) {
    if (part.startsWith("agent=")) {
      agent = decodeURIComponent(part.slice("agent=".length)) || "codex";
    } else if (part.startsWith("profile=")) {
      const decoded = decodeURIComponent(part.slice("profile=".length));
      profile = decoded || undefined;
    }
  }
  return { baseTarget, agent, profile };
}

function pendingKeyForTarget(target: string): string {
  return `car.ticketChat.pending.${target}`;
}

function setActiveTurnScope(target: string | null, pendingKey: string | null): void {
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

function clearActiveTurnScope(): void {
  setActiveTurnScope(null, null);
}

function resolveCurrentTicketChatSelection(): {
  agent: string;
  profile: string | undefined;
} {
  const els = getTicketChatElements();
  const agent = els.agentSelect
    ? (els.agentSelect.value || "codex")
    : (getSelectedAgent() || "codex");
  const profile = els.profileSelect
    ? (els.profileSelect.value || undefined)
    : (getSelectedProfile(agent) || undefined);
  return { agent, profile };
}

function chatTargetForTicket(
  index: number | null,
  ticketChatKey: string | null,
  agent?: string,
  profile?: string
) {
  const selection = agent
    ? { agent, profile }
    : resolveCurrentTicketChatSelection();
  return buildScopedTicketChatTarget(
    index,
    ticketChatKey,
    selection.agent,
    selection.profile
  );
}

const pendingKeyForTicket = (
  index: number | null,
  ticketChatKey: string | null,
  agent?: string,
  profile?: string
) => {
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
export const ticketChatState: TicketChatState = Object.assign(ticketChat.state, {
  ticketIndex: null,
  ticketChatKey: null,
  draft: null,
  contextUsagePercent: null,
  activeTarget: null,
  activePendingKey: null,
  activeAgent: null,
  activeProfile: null,
});
let turnEventsCtrl = createTurnEventsController();

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
    contentTextarea: document.getElementById("ticket-editor-content") as HTMLTextAreaElement | null,
    contentToolbar: document.getElementById("ticket-editor-toolbar") as HTMLElement | null,
    // Patch preview elements - mutually exclusive with content area
    patchMain: document.getElementById("ticket-patch-main") as HTMLElement | null,
    patchBody: document.getElementById("ticket-patch-body") as HTMLElement | null,
    patchStatus: document.getElementById("ticket-patch-status") as HTMLElement | null,
    applyBtn: document.getElementById("ticket-patch-apply") as HTMLButtonElement | null,
    discardBtn: document.getElementById("ticket-patch-discard") as HTMLButtonElement | null,
    agentSelect: document.getElementById("ticket-chat-agent-select") as HTMLSelectElement | null,
    profileSelect: document.getElementById("ticket-chat-profile-select") as HTMLSelectElement | null,
    modelSelect: document.getElementById("ticket-chat-model-select") as HTMLSelectElement | null,
    modelInput: document.getElementById("ticket-chat-model-input") as HTMLInputElement | null,
    reasoningSelect: document.getElementById("ticket-chat-reasoning-select") as HTMLSelectElement | null,
  };
}

function resolveTicketChatModel(
  agent: string,
  controls: {
    modelSelect?: HTMLSelectElement | null;
    modelInput?: HTMLInputElement | null;
  }
): string | undefined {
  const selectedModel = controls.modelSelect?.value || "";
  if (selectedModel) return selectedModel;
  const manualModel = controls.modelInput?.value?.trim() || "";
  if (manualModel) return manualModel;
  return getSelectedModel(agent) || undefined;
}

export function resetTicketChatState(): void {
  ticketChatState.status = "idle";
  ticketChatState.error = "";
  ticketChatState.streamText = "";
  ticketChatState.statusText = "";
  ticketChatState.controller = null;
  ticketChatState.contextUsagePercent = null;
  // Note: events are cleared at the start of each new request, not here
  // Messages persist across requests within the same ticket
}

export async function restoreTicketChatSelectionToActiveTurn(): Promise<void> {
  if (!ticketChatState.activeAgent) return;
  await setSelectedAgentProfile(
    ticketChatState.activeAgent,
    ticketChatState.activeProfile || ""
  );
}

export async function startNewTicketChatThread(): Promise<void> {
  if (ticketChatState.ticketIndex == null) return;

  const confirmed = await confirmModal("Start a new conversation thread for this ticket?");
  if (!confirmed) return;

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
    const localHistoryKey = chatTargetForTicket(
      ticketChatState.ticketIndex,
      ticketChatState.ticketChatKey
    );
    ticketChatState.messages = [];
    if (localHistoryKey) {
      saveTicketChatHistory(localHistoryKey, []);
    }
    clearTicketEvents();
    
    flash("New thread started");
  } catch (err) {
    flash(`Failed to reset thread: ${(err as Error).message}`, "error");
  } finally {
    renderTicketChat();
    renderTicketMessages();
  }
}

/**
 * Clear events at the start of a new request.
 * Events are transient (thinking/tool calls) and reset each turn.
 */
export function clearTicketEvents(): void {
  ticketChat.clearEvents();
}

function clearPendingTurnState(pendingKey: string): void {
  clearManagedTurn(turnEventsCtrl, pendingKey);
}

function handleTicketTurnMeta(update: Record<string, unknown>): void {
  const threadId = typeof update.thread_id === "string" ? update.thread_id : "";
  const turnId = typeof update.turn_id === "string" ? update.turn_id : "";
  const agent = typeof update.agent === "string" ? update.agent : "codex";
  if (!threadId || !turnId) return;
  sharedStartTurnEventsStream(turnEventsCtrl, { agent, threadId, turnId }, {
    onEvent: (event) => {
      ticketChat.applyAppEvent(event);
      ticketChat.renderEvents();
      ticketChat.render();
    },
  });
}

export function applyTicketChatResult(payload: unknown): void {
  if (!payload || typeof payload !== "object") return;
  if (ticketChatState.activeTarget && ticketChatState.target !== ticketChatState.activeTarget) {
    ticketChat.setTarget(ticketChatState.activeTarget);
  }

  const result = payload as Record<string, unknown>;
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
      (result.detail as string) || (result.error as string) || "Chat failed";
    addAssistantMessage(`Error: ${ticketChatState.error}`, true);
    renderTicketChat();
    renderTicketMessages();
    return;
  }

  // Success
  ticketChatState.status = "done";

  if (result.message) {
    ticketChatState.streamText = result.message as string;
  }

  if (result.agent_message || result.agentMessage) {
    ticketChatState.statusText =
      (result.agent_message as string) || (result.agentMessage as string) || "";
  }

  // Check for draft/patch in response
  const hasDraft =
    (result.has_draft as boolean | undefined) ?? (result.hasDraft as boolean | undefined);
  if (hasDraft === false) {
    ticketChatState.draft = null;
  } else if (hasDraft === true || result.draft || result.patch || result.content) {
    ticketChatState.draft = {
      content: (result.content as string) || "",
      patch: (result.patch as string) || "",
      agentMessage:
        (result.agent_message as string) || (result.agentMessage as string) || "",
      createdAt: (result.created_at as string) || (result.createdAt as string) || "",
      baseHash: (result.base_hash as string) || (result.baseHash as string) || "",
    };
  }

  // Add assistant message from response
  const responseText =
    ticketChatState.streamText ||
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
export function addUserMessage(content: string): void {
  ticketChat.addUserMessage(content);
}

/**
 * Add an assistant message to the chat history.
 * Prevents duplicates by checking if the same content was just added.
 */
export function addAssistantMessage(content: string, isFinal = true): void {
  ticketChat.addAssistantMessage(content, isFinal);
}

export function setTicketIndex(index: number | null, ticketChatKey: string | null = null): void {
  const nextTarget = chatTargetForTicket(index, ticketChatKey);
  const changed =
    ticketChatState.ticketIndex !== index ||
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

export function syncTicketChatTargetToSelection(): void {
  if (ticketChatState.ticketIndex == null && !ticketChatState.ticketChatKey) {
    return;
  }
  if (ticketChatState.status === "running") {
    if (ticketChatState.activeTarget && ticketChatState.target !== ticketChatState.activeTarget) {
      ticketChat.setTarget(ticketChatState.activeTarget);
    }
    return;
  }
  const nextTarget = chatTargetForTicket(
    ticketChatState.ticketIndex,
    ticketChatState.ticketChatKey
  );
  if (ticketChatState.target === nextTarget) {
    return;
  }
  ticketChatState.draft = null;
  resetTicketChatState();
  turnEventsCtrl.abort();
  ticketChat.setTarget(nextTarget);
}

export function renderTicketChat(): void {
  const els = getTicketChatElements();
  const controlsLocked = ticketChatState.status === "running";
  if (els.agentSelect) els.agentSelect.disabled = controlsLocked;
  if (els.profileSelect) els.profileSelect.disabled = controlsLocked;

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
        renderDiff(ticketChatState.draft!.patch || "(no changes)", els.patchBody);
      }
      if (els.patchStatus) {
        els.patchStatus.textContent = ticketChatState.draft!.agentMessage || "";
      }
    }
  }
}

export async function sendTicketChat(): Promise<void> {
  const els = getTicketChatElements();
  const message = (els.input?.value || "").trim();
  
  if (!message) {
    ticketChatState.error = "Enter a message to send.";
    renderTicketChat();
    return;
  }

  if (ticketChatState.status === "running") {
    ticketChatState.error = "Ticket chat already running.";
    renderTicketChat();
    flash("Ticket chat already running", "error");
    return;
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
  const pendingKey = pendingKeyForTicket(
    ticketChatState.ticketIndex,
    ticketChatState.ticketChatKey,
    agent,
    profile
  );
  const clientTurnId = newClientTurnId("ticket");
  const targetKey = chatTargetForTicket(
    ticketChatState.ticketIndex,
    ticketChatState.ticketChatKey,
    agent,
    profile
  );
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
    await performTicketChatRequest(
      ticketChatState.ticketIndex,
      message,
      ticketChatState.controller.signal,
      {
        agent,
        profile,
        model,
        reasoning,
        clientTurnId,
      }
    );
    
    // Try to load any pending draft
    await loadTicketPending(ticketChatState.ticketIndex, true);

    if (ticketChatState.status === "running") {
      ticketChatState.status = "done";
    }
    clearPendingTurnState(pendingKey);
  } catch (err) {
    const error = err as Error;
    if (error.name === "AbortError") {
      ticketChatState.status = "interrupted";
      ticketChatState.error = "";
    } else {
      ticketChatState.status = "error";
      ticketChatState.error = error.message || "Ticket chat failed";
    }
    clearPendingTurnState(pendingKey);
  } finally {
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
};

export async function cancelTicketChat(): Promise<void> {
  if (ticketChatState.status !== "running") return;
  
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
    } catch (err) {
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

function findPendingTicketTurn(
  index: number | null,
  ticketChatKey: string | null,
  agent?: string,
  profile?: string
): { pendingKey: string; pending: ReturnType<typeof loadPendingTurn>; target: string } | null {
  const preferredTarget = chatTargetForTicket(index, ticketChatKey, agent, profile);
  if (preferredTarget) {
    const preferredKey = pendingKeyForTarget(preferredTarget);
    const preferredPending = loadPendingTurn(preferredKey);
    if (preferredPending?.target === preferredTarget) {
      return { pendingKey: preferredKey, pending: preferredPending, target: preferredTarget };
    }
  }
  const baseTarget = ticketChatKey || (index != null ? `ticket:${index}` : null);
  if (!baseTarget) return null;
  try {
    for (let idx = 0; idx < localStorage.length; idx += 1) {
      const key = localStorage.key(idx);
      if (!key || !key.startsWith("car.ticketChat.pending.")) continue;
      const pending = loadPendingTurn(key);
      if (!pending?.target) continue;
      const parsed = parseScopedTicketChatTarget(pending.target);
      if (parsed.baseTarget !== baseTarget) continue;
      return { pendingKey: key, pending, target: pending.target };
    }
  } catch {
    return null;
  }
  return null;
}

export async function resumeTicketPendingTurn(
  index: number | null,
  ticketChatKey: string | null = null
): Promise<void> {
  if (index == null) return;
  const pendingMatch = findPendingTicketTurn(index, ticketChatKey);
  if (!pendingMatch) return;
  const { pendingKey, pending, target } = pendingMatch;
  const parsedTarget = parseScopedTicketChatTarget(target);
  await setSelectedAgentProfile(parsedTarget.agent, parsedTarget.profile || "");
  setActiveTurnScope(target, pendingKey);
  if (ticketChatState.target !== target) {
    ticketChat.setTarget(target);
  }
  const chatState = ticketChatState as ChatState;
  chatState.status = "running";
  chatState.statusText = "Recovering previous turn…";
  ticketChat.render();
  ticketChat.renderMessages();

  try {
    const outcome = await resumeFileChatTurn(pending.clientTurnId, {
      onEvent: (event) => {
        ticketChat.applyAppEvent(event);
        ticketChat.renderEvents();
        ticketChat.render();
      },
      onResult: (result) => {
        applyTicketChatResult(result);
        const status = (result as Record<string, unknown>).status;
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
    if (outcome.lastResult && (outcome.lastResult as Record<string, unknown>).status) {
      applyTicketChatResult(outcome.lastResult as Record<string, unknown>);
      clearPendingTurnState(pendingKey);
      clearActiveTurnScope();
      return;
    }
    if (!outcome.controller) {
      window.setTimeout(
        () => void resumeTicketPendingTurn(index, ticketChatKey),
        1000
      );
    }
  } catch (err) {
    const msg = (err as Error).message || "Failed to resume turn";
    chatState.statusText = msg;
    renderTicketChat();
  }
}

export async function applyTicketPatch(): Promise<void> {
  if (ticketChatState.ticketIndex == null) {
    flash("No ticket selected", "error");
    return;
  }

  if (!ticketChatState.draft) {
    flash("No draft to apply", "error");
    return;
  }

  try {
    const res = await api(
      `/api/tickets/${ticketChatState.ticketIndex}/chat/apply`,
      { method: "POST" }
    ) as { content?: string };

    ticketChatState.draft = null;
    flash("Draft applied");
    
    // Notify that tickets changed
    publish("tickets:updated", {});
    
    // Update the editor textarea if content is returned
    if (res.content) {
      const textarea = document.getElementById("ticket-editor-content") as HTMLTextAreaElement | null;
      if (textarea) {
        const [fmYaml, body] = splitMarkdownFrontmatter(res.content);
        if (fmYaml !== null) {
          textarea.value = body.trimStart();
        } else {
          textarea.value = res.content.trimStart();
        }
        // Trigger input event to update undo stack and autosave
        textarea.dispatchEvent(new Event("input", { bubbles: true }));
      }
    }
  } catch (err) {
    const error = err as Error;
    flash(error.message || "Failed to apply draft", "error");
  } finally {
    renderTicketChat();
  }
}

export async function discardTicketPatch(): Promise<void> {
  if (ticketChatState.ticketIndex == null) {
    flash("No ticket selected", "error");
    return;
  }

  try {
    await api(
      `/api/tickets/${ticketChatState.ticketIndex}/chat/discard`,
      { method: "POST" }
    );

    ticketChatState.draft = null;
    flash("Draft discarded");
  } catch (err) {
    const error = err as Error;
    flash(error.message || "Failed to discard draft", "error");
  } finally {
    renderTicketChat();
  }
}

export async function loadTicketPending(index: number, silent = false): Promise<void> {
  try {
    const res = await api(`/api/tickets/${index}/chat/pending`, { method: "GET" }) as {
      patch?: string;
      content?: string;
      agent_message?: string;
      created_at?: string;
      base_hash?: string;
    };

    ticketChatState.draft = {
      patch: res.patch || "",
      content: res.content || "",
      agentMessage: res.agent_message || "",
      createdAt: res.created_at || "",
      baseHash: res.base_hash || "",
    };

    if (!silent) {
      flash("Loaded pending draft");
    }
  } catch (err) {
    const error = err as Error;
    const message = error?.message || "";
    
    if (message.includes("No pending")) {
      ticketChatState.draft = null;
      if (!silent) {
        flash("No pending draft");
      }
    } else if (!silent) {
      flash(message || "Failed to load pending draft", "error");
    }
  } finally {
    renderTicketChat();
  }
}
