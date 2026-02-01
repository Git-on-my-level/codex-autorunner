/**
 * PMA (Project Management Agent) - Hub-level chat interface
 */
import { api, resolvePath, getAuthToken, escapeHtml, flash } from "./utils.js";
import {
  createDocChat,
  type ChatConfig,
  type ChatStyling,
  type DocChatInstance,
} from "./docChatCore.js";
import { getSelectedAgent, getSelectedModel, getSelectedReasoning, refreshAgentControls, initAgentControls } from "./agentControls.js";
import { createFileBoxWidget, type FileBoxListing } from "./fileboxUi.js";
import { REPO_ID } from "./env.js";

interface PMAInboxItem {
  repo_id: string;
  repo_display_name?: string;
  run_id: string;
  status?: string;
  message?: {
    mode?: string;
    title?: string | null;
    body?: string | null;
  };
  open_url?: string;
}

const pmaStyling: ChatStyling = {
  eventClass: "chat-event",
  eventTitleClass: "chat-event-title",
  eventSummaryClass: "chat-event-summary",
  eventDetailClass: "chat-event-detail",
  eventMetaClass: "chat-event-meta",
  eventsEmptyClass: "chat-events-empty",
  messagesClass: "chat-message",
  messageRoleClass: "chat-message-role",
  messageContentClass: "chat-message-content",
  messageMetaClass: "chat-message-meta",
  messageUserClass: "chat-message-user",
  messageAssistantClass: "chat-message-assistant",
  messageAssistantThinkingClass: "chat-message-assistant-thinking",
  messageAssistantFinalClass: "chat-message-assistant-final",
};

const pmaConfig: ChatConfig = {
  idPrefix: "pma-chat",
  storage: { keyPrefix: "car.pma.", maxMessages: 100, version: 1 },
  limits: {
    eventVisible: 20,
    eventMax: 50,
  },
  styling: pmaStyling,
  compactMode: true,
  // PMA should show agent progress inside the "Thinking" bubble, not in a standalone panel.
  inlineEvents: true,
};

let pmaChat: DocChatInstance | null = null;
let currentController: AbortController | null = null;
let currentOutboxBaseline: Set<string> | null = null;
let queuedTickerId: number | null = null;
let queuedSinceMs: number | null = null;
let isUnloading = false;
let unloadHandlerInstalled = false;
let currentEventsController: AbortController | null = null;
const PMA_PENDING_TURN_KEY = "car.pma.pendingTurn";
let fileBoxRepoId: string | null = null;
let fileBoxCtrl: ReturnType<typeof createFileBoxWidget> | null = null;
let pendingUploadNames: string[] = [];

type PendingTurn = {
  clientTurnId: string;
  message: string;
  startedAtMs: number;
};

function newClientTurnId(): string {
  // crypto.randomUUID is not guaranteed everywhere; keep a safe fallback.
  try {
     
    if (typeof crypto !== "undefined" && "randomUUID" in crypto && typeof crypto.randomUUID === "function") {
      return crypto.randomUUID();
    }
  } catch {
    // ignore
  }
  return `pma-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function loadPendingTurn(): PendingTurn | null {
  try {
    const raw = localStorage.getItem(PMA_PENDING_TURN_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<PendingTurn>;
    if (!parsed || typeof parsed !== "object") return null;
    if (!parsed.clientTurnId || !parsed.message || !parsed.startedAtMs) return null;
    return parsed as PendingTurn;
  } catch {
    return null;
  }
}

function savePendingTurn(turn: PendingTurn): void {
  try {
    localStorage.setItem(PMA_PENDING_TURN_KEY, JSON.stringify(turn));
  } catch {
    // ignore
  }
}

function clearPendingTurn(): void {
  try {
    localStorage.removeItem(PMA_PENDING_TURN_KEY);
  } catch {
    // ignore
  }
}

async function resolveFileBoxRepoId(): Promise<string | null> {
  if (fileBoxRepoId) return fileBoxRepoId;
  if (REPO_ID) {
    fileBoxRepoId = REPO_ID;
    return fileBoxRepoId;
  }
  try {
    const payload = (await api("/hub/repos", { method: "GET" })) as { repos?: Array<{ id?: string; initialized?: boolean; exists_on_disk?: boolean }> };
    const repo = (payload.repos || []).find((r) => r?.id && r.initialized && r.exists_on_disk !== false);
    if (repo?.id) {
      fileBoxRepoId = repo.id;
    }
  } catch {
    // best-effort; UI will stay empty if unknown
  }
  return fileBoxRepoId;
}

async function initFileBoxUI(): Promise<void> {
  const elements = getElements();
  const repoId = await resolveFileBoxRepoId();
  if (!elements.inboxFiles || !elements.outboxFiles) return;
  if (!repoId) {
    elements.inboxFiles.innerHTML = '<div class="muted small">FileBox unavailable</div>';
    elements.outboxFiles.innerHTML = '<div class="muted small">FileBox unavailable</div>';
    return;
  }

  fileBoxCtrl = createFileBoxWidget({
    scope: REPO_ID ? "repo" : "hub",
    repoId,
    inboxEl: elements.inboxFiles,
    outboxEl: elements.outboxFiles,
    uploadInput: elements.chatUploadInput,
    uploadBtn: elements.chatUploadBtn,
    refreshBtn: elements.outboxRefresh,
    uploadBox: "inbox",
    emptyMessage: "No files",
    onChange: (listing) => {
      if (pendingUploadNames.length && pmaChat) {
        const links = pendingUploadNames
          .map((name) => {
            const match = listing.inbox.find((e) => e.name === name);
            const href = match?.url ? resolvePath(match.url) : "";
            const text = escapeMarkdownLinkText(name);
            return href ? `[${text}](${href})` : text;
          })
          .join("\n");
        if (links) {
          pmaChat.addUserMessage(`**Inbox files (uploaded):**\n${links}`);
          pmaChat.render();
        }
        pendingUploadNames = [];
      }
    },
    onUpload: (names) => {
      pendingUploadNames = names;
    },
  });

  await fileBoxCtrl.refresh();
}

function stopTurnEventsStream(): void {
  if (currentEventsController) {
    try {
      currentEventsController.abort();
    } catch {
      // ignore
    }
    currentEventsController = null;
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

async function startTurnEventsStream(meta: {
  agent: string;
  threadId: string;
  turnId: string;
}): Promise<void> {
  stopTurnEventsStream();
  if (!meta.threadId || !meta.turnId) return;

  const ctrl = new AbortController();
  currentEventsController = ctrl;

  const token = getAuthToken();
  const headers: Record<string, string> = {};
  if (token) headers.Authorization = `Bearer ${token}`;

  const url = resolvePath(
    `/hub/pma/turns/${encodeURIComponent(meta.turnId)}/events?thread_id=${encodeURIComponent(
      meta.threadId
    )}&agent=${encodeURIComponent(meta.agent || "codex")}`
  );

  try {
    const res = await fetch(url, {
      method: "GET",
      headers,
      signal: ctrl.signal,
    });
    if (!res.ok) return;
    const contentType = res.headers.get("content-type") || "";
    if (!contentType.includes("text/event-stream")) return;
    await readPMAStream(res);
  } catch {
    // ignore (abort / network)
  }
}

async function pollForTurnMeta(clientTurnId: string, timeoutMs = 8000): Promise<void> {
  if (!clientTurnId) return;
  const started = Date.now();

  while (Date.now() - started < timeoutMs) {
    if (!pmaChat || pmaChat.state.status !== "running") return;
    if (currentEventsController) return;

    try {
      const payload = (await api(
        `/hub/pma/active?client_turn_id=${encodeURIComponent(clientTurnId)}`,
        { method: "GET" }
      )) as { current?: Record<string, unknown> };
      const cur = (payload.current || {}) as Record<string, unknown>;
      const threadId = typeof cur.thread_id === "string" ? cur.thread_id : "";
      const turnId = typeof cur.turn_id === "string" ? cur.turn_id : "";
      const agent = typeof cur.agent === "string" ? cur.agent : "codex";
      if (threadId && turnId) {
        void startTurnEventsStream({ agent, threadId, turnId });
        return;
      }
    } catch {
      // ignore and retry
    }

    await sleep(250);
  }
}

function clearQueuedTicker(): void {
  if (queuedTickerId !== null) {
    window.clearInterval(queuedTickerId);
    queuedTickerId = null;
  }
  queuedSinceMs = null;
}

function startQueuedTicker(): void {
  if (!pmaChat) return;
  if (queuedTickerId !== null) return;
  if (!queuedSinceMs) queuedSinceMs = Date.now();
  queuedTickerId = window.setInterval(() => {
    if (!pmaChat) {
      clearQueuedTicker();
      return;
    }
    if (pmaChat.state.status !== "running") {
      clearQueuedTicker();
      return;
    }
    const status = (pmaChat.state.statusText || "").toLowerCase();
    if (status !== "queued") {
      // Once we transition away from queued, stop the ticker.
      clearQueuedTicker();
      return;
    }
    const elapsed = Math.max(0, Math.floor((Date.now() - (queuedSinceMs || Date.now())) / 1000));
    pmaChat.state.statusText = `waiting to start (${elapsed}s)`;
    pmaChat.render();
  }, 500);
}

function getElements() {
  return {
    shell: document.getElementById("pma-shell"),
    input: document.getElementById("pma-chat-input") as HTMLTextAreaElement | null,
    sendBtn: document.getElementById("pma-chat-send") as HTMLButtonElement | null,
    cancelBtn: document.getElementById("pma-chat-cancel") as HTMLButtonElement | null,
    newThreadBtn: document.getElementById("pma-chat-new-thread") as HTMLButtonElement | null,
    statusEl: document.getElementById("pma-chat-status"),
    errorEl: document.getElementById("pma-chat-error"),
    streamEl: document.getElementById("pma-chat-stream"),
    eventsMain: document.getElementById("pma-chat-events"),
    eventsList: document.getElementById("pma-chat-events-list"),
    eventsToggle: document.getElementById("pma-chat-events-toggle") as HTMLButtonElement | null,
    messagesEl: document.getElementById("pma-chat-messages"),
    historyHeader: document.getElementById("pma-chat-history-header"),
    agentSelect: document.getElementById("pma-chat-agent-select") as HTMLSelectElement | null,
    modelSelect: document.getElementById("pma-chat-model-select") as HTMLSelectElement | null,
    reasoningSelect: document.getElementById("pma-chat-reasoning-select") as HTMLSelectElement | null,
    inboxList: document.getElementById("pma-inbox-list"),
    inboxRefresh: document.getElementById("pma-inbox-refresh") as HTMLButtonElement | null,
    chatUploadInput: document.getElementById("pma-chat-upload-input") as HTMLInputElement | null,
    chatUploadBtn: document.getElementById("pma-chat-upload-btn") as HTMLButtonElement | null,
    inboxFiles: document.getElementById("pma-inbox-files"),
    outboxFiles: document.getElementById("pma-outbox-files"),
    outboxRefresh: document.getElementById("pma-outbox-refresh") as HTMLButtonElement | null,
  };
}

const decoder = new TextDecoder();

function escapeMarkdownLinkText(text: string): string {
  // Keep this ES2019-compatible (no String.prototype.replaceAll).
  return text.replace(/\[/g, "\\[").replace(/\]/g, "\\]");
}

function formatOutboxAttachments(listing: FileBoxListing | null, names: string[]): string {
  if (!listing || !names.length) return "";
  const lines = names.map((name) => {
    const entry = listing.outbox.find((e) => e.name === name);
    const href = entry?.url ? new URL(resolvePath(entry.url), window.location.origin).toString() : "";
    const label = escapeMarkdownLinkText(name);
    return href ? `[${label}](${href})` : label;
  });
  return lines.length ? `**Outbox files (download):**\n${lines.join("\n")}` : "";
}

async function finalizePMAResponse(responseText: string): Promise<void> {
  if (!pmaChat) return;

  let attachments = "";
  try {
    if (fileBoxCtrl) {
      const current = await fileBoxCtrl.refresh();
      if (currentOutboxBaseline) {
        const baseline = currentOutboxBaseline;
        const added = (current.outbox || []).map((e) => e.name).filter((name) => !baseline.has(name));
        attachments = formatOutboxAttachments(current, added);
      }
    }
  } catch {
    attachments = "";
  } finally {
    currentOutboxBaseline = null;
    clearPendingTurn();
    stopTurnEventsStream();
  }

  const trimmed = (responseText || "").trim();
  const content = trimmed
    ? (attachments ? `${trimmed}\n\n---\n\n${attachments}` : trimmed)
    : attachments;

  const startTime = pmaChat.state.startTime;
  const duration = startTime ? (Date.now() - startTime) / 1000 : undefined;
  const steps = pmaChat.state.events.length;

  if (content) {
    pmaChat.addAssistantMessage(content, true, { steps, duration });
  }
  pmaChat.state.streamText = "";
  pmaChat.state.status = "done";
  pmaChat.render();
  pmaChat.renderMessages();
  pmaChat.renderEvents();
  void fileBoxCtrl?.refresh();
}

async function initPMA(): Promise<void> {
  const elements = getElements();
  if (!elements.shell) return;

  pmaChat = createDocChat(pmaConfig);
  pmaChat.setTarget("pma");
  pmaChat.render();
  // Ensure we start at the bottom
  setTimeout(() => {
    const stream = document.getElementById("pma-chat-stream");
    const messages = document.getElementById("pma-chat-messages");
    if (stream) stream.scrollTop = stream.scrollHeight;
    if (messages) messages.scrollTop = messages.scrollHeight;
  }, 100);

  initAgentControls({
    agentSelect: elements.agentSelect,
    modelSelect: elements.modelSelect,
    reasoningSelect: elements.reasoningSelect,
  });

  await refreshAgentControls({ force: true, reason: "initial" });
  await loadPMAInbox();
  await initFileBoxUI();
  attachHandlers();

  // If we refreshed mid-turn, recover the final output from the server.
  await resumePendingTurn();

  // If the page refreshes/navigates while a turn is running, avoid showing a noisy
  // "network error" and proactively interrupt the running turn on the server to
  // prevent the next request from receiving a stale/previous response.
  if (!unloadHandlerInstalled) {
    unloadHandlerInstalled = true;
    window.addEventListener("beforeunload", () => {
      isUnloading = true;
      clearQueuedTicker();
      // Abort any in-flight request immediately.
      if (currentController) {
        try {
          currentController.abort();
        } catch {
          // ignore
        }
      }
    });
  }

  // Periodically refresh inbox
  setInterval(() => {
    void loadPMAInbox();
    void fileBoxCtrl?.refresh();
  }, 30000);
}

async function loadPMAInbox(): Promise<void> {
  const elements = getElements();
  if (!elements.inboxList) return;

  try {
    const payload = (await api("/hub/messages", { method: "GET" })) as { items?: PMAInboxItem[] };
    const items = payload?.items || [];
    const html = !items.length
      ? '<div class="muted">No paused runs</div>'
      : items
        .map((item) => {
          const title = item.message?.title || item.message?.mode || "Message";
          const excerpt = item.message?.body ? item.message.body.slice(0, 160) : "";
          const repoLabel = item.repo_display_name || item.repo_id;
          const href = item.open_url || `/repos/${item.repo_id}/?tab=messages&run_id=${item.run_id}`;
          return `
            <a class="pma-inbox-item" href="${escapeHtml(resolvePath(href))}">
              <div class="pma-inbox-item-header">
                <span class="pma-inbox-repo">${escapeHtml(repoLabel)}</span>
                <span class="pill pill-small pill-warn">paused</span>
              </div>
              <div class="pma-inbox-title">${escapeHtml(title)}</div>
              <div class="pma-inbox-excerpt muted small">${escapeHtml(excerpt)}</div>
            </a>
          `;
        })
        .join("");
    elements.inboxList.innerHTML = html;
  } catch (_err) {
    elements.inboxList.innerHTML = '<div class="muted">Failed to load inbox</div>';
  }
}

async function sendMessage(): Promise<void> {
  const elements = getElements();
  if (!elements.input || !pmaChat) return;

  const message = elements.input.value?.trim() || "";
  if (!message) return;

  if (currentController) {
    cancelRequest();
    return;
  }

  elements.input.value = "";
  elements.input.style.height = "auto";

  const agent = elements.agentSelect?.value || getSelectedAgent();
  const model = elements.modelSelect?.value || getSelectedModel(agent);
  const reasoning = elements.reasoningSelect?.value || getSelectedReasoning(agent);
  const clientTurnId = newClientTurnId();
  savePendingTurn({ clientTurnId, message, startedAtMs: Date.now() });

  currentController = new AbortController();
  pmaChat.state.controller = currentController;
  pmaChat.state.status = "running";
  pmaChat.state.error = "";
  pmaChat.state.streamText = "";
  pmaChat.state.startTime = Date.now();
  pmaChat.clearEvents();
  pmaChat.addUserMessage(message);
  pmaChat.render();
  pmaChat.renderMessages();

  try {
    try {
      const listing = fileBoxCtrl ? await fileBoxCtrl.refresh() : null;
      const names = listing?.outbox?.map((e) => e.name) || [];
      currentOutboxBaseline = new Set(names);
    } catch {
      currentOutboxBaseline = new Set();
    }

    const endpoint = resolvePath("/hub/pma/chat");
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
    };
    const token = getAuthToken();
    if (token) {
      headers.Authorization = `Bearer ${token}`;
    }

    const payload: Record<string, unknown> = {
      message,
      stream: true,
      client_turn_id: clientTurnId,
    };
    if (agent) payload.agent = agent;
    if (model) payload.model = model;
    if (reasoning) payload.reasoning = reasoning;

    const res = await fetch(endpoint, {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
      signal: currentController.signal,
    });

    if (!res.ok) {
      const text = await res.text();
      let detail = text;
      try {
        const parsed = JSON.parse(text) as Record<string, unknown>;
        detail = (parsed.detail as string) || (parsed.error as string) || text;
      } catch {
        // ignore parse errors
      }
      throw new Error(detail || `Request failed (${res.status})`);
    }

    // Stream tool calls/events separately as soon as we have (thread_id, turn_id).
    // The main /hub/pma/chat stream only emits a final "update"/"done" today.
    void pollForTurnMeta(clientTurnId);

    const contentType = res.headers.get("content-type") || "";
    if (contentType.includes("text/event-stream")) {
      await readPMAStream(res);
    } else {
      const responsePayload = contentType.includes("application/json")
        ? await res.json()
        : await res.text();
      applyPMAResult(responsePayload);
    }
  } catch (err) {
    // Aborts (including page refresh) shouldn't create an error message that pollutes history.
    const name =
      err && typeof err === "object" && "name" in err
        ? String((err as { name?: unknown }).name || "")
        : "";
    if (isUnloading || name === "AbortError") {
      pmaChat.state.status = "interrupted";
      pmaChat.state.error = "";
      pmaChat.state.statusText = isUnloading ? "Cancelled (page reload)" : "Cancelled";
      pmaChat.render();
      return;
    }
    const errorMsg = (err as Error).message || "Request failed";
    pmaChat.state.status = "error";
    pmaChat.state.error = errorMsg;
    pmaChat.addAssistantMessage(`Error: ${errorMsg}`, true);
    pmaChat.render();
    pmaChat.renderMessages();
    clearPendingTurn();
    stopTurnEventsStream();
  } finally {
    currentController = null;
    pmaChat.state.controller = null;
  }
}

async function readPMAStream(res: Response): Promise<void> {
  if (!res.body) throw new Error("Streaming not supported in this browser");

  const reader = res.body.getReader();
  let buffer = "";
  let escapedNewlines = false;

  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;

    const decoded = decoder.decode(value, { stream: true });

    if (!escapedNewlines) {
      const combined = buffer + decoded;
      if (!combined.includes("\n") && combined.includes("\\n")) {
        escapedNewlines = true;
        buffer = buffer.replace(/\\n(?=event:|data:|\\n)/g, "\n");
      }
    }

    buffer += escapedNewlines
      ? decoded.replace(/\\n(?=event:|data:|\\n)/g, "\n")
      : decoded;

    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() || "";

    for (const chunk of chunks) {
      if (!chunk.trim()) continue;

      let event = "message";
      const dataLines: string[] = [];

      chunk.split("\n").forEach((line) => {
        if (line.startsWith("event:")) {
          event = line.slice(6).trim();
        } else if (line.startsWith("data:")) {
          dataLines.push(line.slice(5).trimStart());
        } else if (line.trim()) {
          dataLines.push(line);
        }
      });

      if (dataLines.length === 0) continue;
      const data = dataLines.join("\n");
      handlePMAStreamEvent(event, data);
    }
  }
}

function handlePMAStreamEvent(event: string, rawData: string): void {
  const parsed = parseMaybeJson(rawData) as Record<string, unknown> | string;

  switch (event) {
    case "status": {
      const status =
        typeof parsed === "string"
          ? parsed
          : ((parsed as Record<string, unknown>).status as string) || "";
      pmaChat!.state.statusText = status;
      if ((status || "").toLowerCase() === "queued") {
        queuedSinceMs = queuedSinceMs ?? Date.now();
        startQueuedTicker();
      } else {
        clearQueuedTicker();
      }
      pmaChat!.render();
      pmaChat!.renderEvents();
      break;
    }

    case "token": {
      clearQueuedTicker();
      const token =
        typeof parsed === "string"
          ? parsed
          : ((parsed as Record<string, unknown>).token as string) ||
            ((parsed as Record<string, unknown>).text as string) ||
            rawData ||
            "";
      pmaChat!.state.streamText = (pmaChat!.state.streamText || "") + token;
      // Force status to "responding" if we have tokens, so the stream loop picks it up
      if (!pmaChat!.state.statusText || pmaChat!.state.statusText === "queued") {
        pmaChat!.state.statusText = "responding";
      }
      // Ensure we're in "running" state if receiving tokens
      if (pmaChat!.state.status !== "running") {
          pmaChat!.state.status = "running";
      }
      pmaChat!.render();
      break;
    }

    case "event":
    case "app-server": {
      if (pmaChat) {
        clearQueuedTicker();
        // Ensure we're in "running" state if receiving events
        if (pmaChat!.state.status !== "running") {
            pmaChat!.state.status = "running";
        }
        // If we are receiving events but still show "queued", bump status so the UI
        // reflects progress even before token streaming starts.
        if (!pmaChat!.state.statusText || pmaChat!.state.statusText === "queued") {
          pmaChat!.state.statusText = "working";
        }
        pmaChat.applyAppEvent(parsed);
        pmaChat.renderEvents();
        // Force a full render to update the inline thinking indicator
        pmaChat.render();
      }
      break;
    }

    case "error": {
      clearQueuedTicker();
      const message =
        typeof parsed === "object" && parsed !== null
          ? ((parsed as Record<string, unknown>).detail as string) ||
            ((parsed as Record<string, unknown>).error as string) ||
            rawData
          : rawData || "PMA chat failed";
      pmaChat!.state.status = "error";
      pmaChat!.state.error = String(message);
      pmaChat!.addAssistantMessage(`Error: ${message}`, true);
      pmaChat!.render();
      pmaChat!.renderMessages();
      throw new Error(String(message));
    }

    case "interrupted": {
      clearQueuedTicker();
      const message =
        typeof parsed === "object" && parsed !== null
          ? ((parsed as Record<string, unknown>).detail as string) || rawData
          : rawData || "PMA chat interrupted";
      pmaChat!.state.status = "interrupted";
      pmaChat!.state.error = "";
      pmaChat!.state.statusText = String(message);
      pmaChat!.addAssistantMessage("Request interrupted", true);
      pmaChat!.render();
      pmaChat!.renderMessages();
      break;
    }

    case "update": {
      const data = typeof parsed === "string" ? {} : (parsed as Record<string, unknown>);
      // If server echoes client_turn_id, we can clear pending when we receive the final payload.
      if (data.client_turn_id) {
        clearPendingTurn();
      }
      if (data.message) {
        pmaChat!.state.streamText = data.message as string;
      }
      pmaChat!.render();
      break;
    }

    case "done":
    case "finish": {
      clearQueuedTicker();
      void finalizePMAResponse(pmaChat!.state.streamText || "");
      break;
    }

    default:
      if (typeof parsed === "object" && parsed !== null) {
        const messageObj = parsed as Record<string, unknown>;
        if (messageObj.method || messageObj.message) {
          pmaChat!.applyAppEvent(parsed);
          pmaChat!.renderEvents();
        }
      }
      break;
  }
}

async function resumePendingTurn(): Promise<void> {
  const pending = loadPendingTurn();
  if (!pending || !pmaChat) return;

  // Show a running indicator immediately.
  pmaChat.state.status = "running";
  pmaChat.state.statusText = "Recovering previous turn…";
  pmaChat.render();
  pmaChat.renderMessages();

  const poll = async (): Promise<void> => {
    try {
      const payload = (await api(
        `/hub/pma/active?client_turn_id=${encodeURIComponent(pending.clientTurnId)}`,
        { method: "GET" }
      )) as { active?: boolean; current?: Record<string, unknown>; last_result?: Record<string, unknown> };

      const cur = (payload.current || {}) as Record<string, unknown>;
      const threadId = typeof cur.thread_id === "string" ? cur.thread_id : "";
      const turnId = typeof cur.turn_id === "string" ? cur.turn_id : "";
      const agent = typeof cur.agent === "string" ? cur.agent : "codex";
      if (threadId && turnId && !currentEventsController) {
        void startTurnEventsStream({ agent, threadId, turnId });
      }

      const last = (payload.last_result || {}) as Record<string, unknown>;
      const status = String(last.status || "");
      if (status === "ok" && typeof last.message === "string") {
        await finalizePMAResponse(last.message);
        return;
      }
      if (status === "error") {
        const detail = String(last.detail || "PMA chat failed");
        pmaChat.state.status = "error";
        pmaChat.state.error = detail;
        pmaChat.addAssistantMessage(`Error: ${detail}`, true);
        pmaChat.render();
        pmaChat.renderMessages();
        clearPendingTurn();
        stopTurnEventsStream();
        return;
      }
      if (status === "interrupted") {
        pmaChat.state.status = "interrupted";
        pmaChat.state.error = "";
        pmaChat.addAssistantMessage("Request interrupted", true);
        pmaChat.render();
        pmaChat.renderMessages();
        clearPendingTurn();
        stopTurnEventsStream();
        return;
      }

      // Still running; keep polling.
      pmaChat.state.status = "running";
      pmaChat.state.statusText = "Recovering previous turn…";
      pmaChat.render();
      window.setTimeout(() => void poll(), 1000);
    } catch {
      // If recovery fails, don't spam errors; just stop trying.
      pmaChat.state.statusText = "Recovering previous turn…";
      pmaChat.render();
    }
  };

  await poll();
}

function applyPMAResult(payload: unknown): void {
  if (!payload || typeof payload !== "object") return;

  const result = payload as Record<string, unknown>;

  if (result.status === "interrupted") {
    pmaChat!.state.status = "interrupted";
    pmaChat!.state.error = "";
    pmaChat!.addAssistantMessage("Request interrupted", true);
    pmaChat!.render();
    pmaChat!.renderMessages();
    return;
  }

  if (result.status === "error" || result.error) {
    pmaChat!.state.status = "error";
    pmaChat!.state.error =
      (result.detail as string) || (result.error as string) || "Chat failed";
    pmaChat!.addAssistantMessage(`Error: ${pmaChat!.state.error}`, true);
    pmaChat!.render();
    pmaChat!.renderMessages();
    return;
  }

  if (result.message) {
    pmaChat!.state.streamText = result.message as string;
  }

  const responseText = (pmaChat!.state.streamText || pmaChat!.state.statusText || "Done") as string;
  void finalizePMAResponse(responseText);
}

function parseMaybeJson(data: string): unknown {
  try {
    return JSON.parse(data);
  } catch {
    return data;
  }
}

function cancelRequest(): void {
  if (currentController) {
    currentController.abort();
    currentController = null;
  }
  stopTurnEventsStream();
  if (pmaChat) {
    pmaChat.state.controller = null;
    pmaChat.state.status = "interrupted";
    pmaChat.state.statusText = "Cancelled";
    pmaChat.render();
  }
}

function resetThread(): void {
  cancelRequest();
  if (pmaChat) {
    pmaChat.state.messages = [];
    pmaChat.state.events = [];
    pmaChat.state.eventItemIndex = {};
    pmaChat.state.error = "";
    pmaChat.state.streamText = "";
    pmaChat.state.statusText = "";
    pmaChat.state.status = "idle";
    pmaChat.render();
    pmaChat.renderMessages();
  }
  flash("Thread reset", "info");
}

function attachHandlers(): void {
  const elements = getElements();
  if (elements.sendBtn) {
    elements.sendBtn.addEventListener("click", () => {
      void sendMessage();
    });
  }

  if (elements.cancelBtn) {
    elements.cancelBtn.addEventListener("click", () => {
      cancelRequest();
    });
  }

  if (elements.newThreadBtn) {
    elements.newThreadBtn.addEventListener("click", () => {
      resetThread();
    });
  }

  if (elements.input) {
    elements.input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        void sendMessage();
      }
    });

    elements.input.addEventListener("input", () => {
      if (elements.input) {
        elements.input.style.height = "auto";
        elements.input.style.height = `${elements.input.scrollHeight}px`;
      }
    });
  }

  if (elements.inboxRefresh) {
    elements.inboxRefresh.addEventListener("click", () => {
      void loadPMAInbox();
      void fileBoxCtrl?.refresh();
    });
  }

  if (elements.outboxRefresh) {
    elements.outboxRefresh.addEventListener("click", () => {
      void fileBoxCtrl?.refresh();
    });
  }
}

export { initPMA };
