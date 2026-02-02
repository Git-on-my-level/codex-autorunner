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
import { initChatPasteUpload } from "./chatUploads.js";
import {
  clearAgentSelectionStorage,
  getSelectedAgent,
  getSelectedModel,
  getSelectedReasoning,
  initAgentControls,
  refreshAgentControls,
} from "./agentControls.js";
import { createFileBoxWidget, type FileBoxListing } from "./fileboxUi.js";

interface PMAInboxItem {
  repo_id: string;
  repo_display_name?: string;
  repo_path?: string;
  run_id: string;
  status?: string;
  seq?: number;
  dispatch?: {
    mode?: string;
    title?: string | null;
    body?: string | null;
    is_handoff?: boolean;
  };
  files?: string[];
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
let isUnloading = false;
let unloadHandlerInstalled = false;
let currentEventsController: AbortController | null = null;
const PMA_PENDING_TURN_KEY = "car.pma.pendingTurn";
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

async function initFileBoxUI(): Promise<void> {
  const elements = getElements();
  if (!elements.inboxFiles || !elements.outboxFiles) return;

  fileBoxCtrl = createFileBoxWidget({
    scope: "pma",
    basePath: "/hub/pma/files",
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
    pausedRunsBar: document.getElementById("pma-paused-runs"),
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
    threadInfo: document.getElementById("pma-thread-info"),
    threadInfoAgent: document.getElementById("pma-thread-info-agent"),
    threadInfoThreadId: document.getElementById("pma-thread-info-thread-id"),
    threadInfoTurnId: document.getElementById("pma-thread-info-turn-id"),
    threadInfoStatus: document.getElementById("pma-thread-info-status"),
    repoActions: document.getElementById("pma-repo-actions"),
    scanReposBtn: document.getElementById("pma-scan-repos-btn"),
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
  await loadPMAThreadInfo();
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
      // Abort any in-flight request immediately.
      // Note: we do NOT send an interrupt request to the server; the run continues
      // in the background and can be recovered after reload via /hub/pma/active.
      if (currentController) {
        try {
          currentController.abort();
        } catch {
          // ignore
        }
      }
    });
  }

  // Periodically refresh inbox and thread info
  setInterval(() => {
    void loadPMAInbox();
    void loadPMAThreadInfo();
    void fileBoxCtrl?.refresh();
  }, 30000);
}

async function loadPMAInbox(): Promise<void> {
  const elements = getElements();
  if (!elements.inboxList) return;

  try {
    const payload = (await api("/hub/messages", { method: "GET" })) as { items?: PMAInboxItem[] };
    const items = payload?.items || [];
    if (!items.length) {
      elements.inboxList.innerHTML = "";
      elements.pausedRunsBar?.classList.add("hidden");
      return;
    }
    const html = items
      .map((item) => {
        const title = item.dispatch?.title || item.dispatch?.mode || "Message";
        const excerpt = item.dispatch?.body ? item.dispatch.body.slice(0, 160) : "";
        const repoLabel = item.repo_display_name || item.repo_id;
        const href = item.open_url || `/repos/${item.repo_id}/?tab=inbox&run_id=${item.run_id}`;
        const seq = item.seq ? `#${item.seq}` : "";
        return `
          <div class="pma-inbox-item">
            <div class="pma-inbox-item-header">
              <span class="pma-inbox-repo">${escapeHtml(repoLabel)} <span class="pma-inbox-run-id muted">(${item.run_id.slice(0, 8)}${seq})</span></span>
              <span class="pill pill-small pill-warn">paused</span>
            </div>
            <div class="pma-inbox-title">${escapeHtml(title)}</div>
            <div class="pma-inbox-excerpt muted small">${escapeHtml(excerpt)}</div>
            <div class="pma-inbox-actions">
              <a class="pma-inbox-action" href="${escapeHtml(resolvePath(href))}" title="Open run page">Open run</a>
              <button class="pma-inbox-action ghost sm" data-action="copy-run-id" data-run-id="${escapeHtml(item.run_id)}" title="Copy run ID">Copy ID</button>
              ${item.repo_id ? `<button class="pma-inbox-action ghost sm" data-action="copy-repo-id" data-repo-id="${escapeHtml(item.repo_id)}" title="Copy repo ID">Copy repo</button>` : ""}
            </div>
          </div>
        `;
      })
      .join("");
    elements.inboxList.innerHTML = html;
    elements.pausedRunsBar?.classList.remove("hidden");
  } catch (_err) {
    elements.inboxList.innerHTML = '<div class="muted">Failed to load inbox</div>';
    elements.pausedRunsBar?.classList.remove("hidden");
  }
}

async function loadPMAThreadInfo(): Promise<void> {
  const elements = getElements();
  if (!elements.threadInfo) return;

  try {
    const payload = (await api("/hub/pma/active", { method: "GET" })) as {
      active?: boolean;
      current?: Record<string, unknown>;
      last_result?: Record<string, unknown>;
    };

    const current = payload.current || {};
    const last = payload.last_result || {};
    const info = (payload.active && current.thread_id) ? current : last;

    if (!info || !info.thread_id) {
      elements.threadInfo.classList.add("hidden");
      return;
    }

    if (elements.threadInfoAgent) {
      elements.threadInfoAgent.textContent = String(info.agent || "unknown");
    }
    if (elements.threadInfoThreadId) {
      const threadId = String(info.thread_id || "");
      elements.threadInfoThreadId.textContent = threadId.slice(0, 12);
      elements.threadInfoThreadId.title = threadId;
    }
    if (elements.threadInfoTurnId) {
      const turnId = String(info.turn_id || "");
      elements.threadInfoTurnId.textContent = turnId.slice(0, 12);
      elements.threadInfoTurnId.title = turnId;
    }
    if (elements.threadInfoStatus) {
      const status = String(info.status || (payload.active ? "active" : "idle"));
      elements.threadInfoStatus.textContent = status;
      if (payload.active) {
        elements.threadInfoStatus.classList.add("pill-warn");
        elements.threadInfoStatus.classList.remove("pill-idle");
      } else {
        elements.threadInfoStatus.classList.add("pill-idle");
        elements.threadInfoStatus.classList.remove("pill-warn");
      }
    }

    elements.threadInfo.classList.remove("hidden");
  } catch {
    elements.threadInfo?.classList.add("hidden");
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
      pmaChat!.render();
      pmaChat!.renderEvents();
      break;
    }

    case "token": {
      const token =
        typeof parsed === "string"
          ? parsed
          : ((parsed as Record<string, unknown>).token as string) ||
            ((parsed as Record<string, unknown>).text as string) ||
            rawData ||
            "";
      pmaChat!.state.streamText = (pmaChat!.state.streamText || "") + token;
      // Force status to "responding" if we have tokens, so the stream loop picks it up
      if (!pmaChat!.state.statusText || pmaChat!.state.statusText === "starting") {
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
        // Ensure we're in "running" state if receiving events
        if (pmaChat!.state.status !== "running") {
            pmaChat!.state.status = "running";
        }
        // If we are receiving events but still show "starting", bump status so UI
        // reflects progress even before token streaming starts.
        if (!pmaChat!.state.statusText || pmaChat!.state.statusText === "starting") {
          pmaChat!.state.statusText = "working";
        }
        pmaChat.applyAppEvent(parsed);
        pmaChat.renderEvents();
        // Force a full render to update the inline thinking indicator
        pmaChat.render();
      }
      break;
    }

    case "token_usage": {
      // Token usage events - context window usage
      if (typeof parsed === "object" && parsed !== null) {
        const usage = parsed as Record<string, unknown>;
        const totalTokens = usage.totalTokens as number | undefined;
        const modelContextWindow = usage.modelContextWindow as number | undefined;
        if (totalTokens !== undefined && modelContextWindow !== undefined && modelContextWindow > 0) {
          const percentRemaining = Math.round(((modelContextWindow - totalTokens) / modelContextWindow) * 100);
          const percentRemainingClamped = Math.max(0, Math.min(100, percentRemaining));
          // Store context usage for display in chat UI
          if (pmaChat) {
            pmaChat!.state.contextUsagePercent = percentRemainingClamped;
            pmaChat!.render();
          }
        }
      }
      break;
    }

    case "error": {
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
  clearPendingTurn();
  stopTurnEventsStream();
  if (pmaChat) {
    pmaChat.state.messages = [];
    pmaChat.state.events = [];
    pmaChat.state.eventItemIndex = {};
    pmaChat.state.error = "";
    pmaChat.state.streamText = "";
    pmaChat.state.statusText = "";
    pmaChat.state.status = "idle";
    pmaChat.state.contextUsagePercent = null;
    pmaChat.render();
    pmaChat.renderMessages();
  }
  flash("Thread reset", "info");
}

async function resetThreadOnServer(): Promise<void> {
  const elements = getElements();
  const agent = elements.agentSelect?.value || getSelectedAgent();
  const resetAgent = (agent || "").trim() || "all";
  await api("/hub/pma/thread/reset", {
    method: "POST",
    body: { agent: resetAgent },
  });
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
      void (async () => {
        cancelRequest();
        try {
          await resetThreadOnServer();
        } catch (err) {
          flash("Failed to reset server thread", "error");
          return;
        }
        clearAgentSelectionStorage();
        await refreshAgentControls({ force: true, reason: "manual" });
        resetThread();
      })();
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

    initChatPasteUpload({
      textarea: elements.input,
      basePath: "/hub/pma/files",
      box: "inbox",
      insertStyle: "markdown",
      onUploaded: () => {
        void fileBoxCtrl?.refresh();
      },
    });
  }

  if (elements.inboxRefresh) {
    elements.inboxRefresh.addEventListener("click", () => {
      void loadPMAInbox();
      void fileBoxCtrl?.refresh();
    });
  }

  if (elements.inboxList) {
    elements.inboxList.addEventListener("click", (e) => {
      const target = e.target as HTMLElement;
      if (target.classList.contains("pma-inbox-action")) {
        if (target.dataset.action === "copy-run-id") {
          const runId = target.dataset.runId;
          if (runId) {
            void navigator.clipboard.writeText(runId).then(() => {
              flash("Copied run ID", "info");
            });
          }
        } else if (target.dataset.action === "copy-repo-id") {
          const repoId = target.dataset.repoId;
          if (repoId) {
            void navigator.clipboard.writeText(repoId).then(() => {
              flash("Copied repo ID", "info");
            });
          }
        }
      }
    });
  }

  if (elements.outboxRefresh) {
    elements.outboxRefresh.addEventListener("click", () => {
      void fileBoxCtrl?.refresh();
    });
  }

  if (elements.scanReposBtn) {
    elements.scanReposBtn.addEventListener("click", async () => {
      try {
        const btn = elements.scanReposBtn as HTMLButtonElement;
        btn.disabled = true;
        btn.textContent = "Scanning…";
        await api("/hub/repos/scan", { method: "POST" });
        flash("Repositories scanned", "info");
        await loadPMAInbox();
      } catch (err) {
        flash("Failed to scan repos", "error");
      } finally {
        const btn = elements.scanReposBtn as HTMLButtonElement;
        btn.disabled = false;
        btn.textContent = "Scan repos";
      }
    });
  }

  if (elements.threadInfoThreadId) {
    elements.threadInfoThreadId.addEventListener("click", () => {
      const fullId = elements.threadInfoThreadId?.title || "";
      if (fullId) {
        void navigator.clipboard.writeText(fullId).then(() => {
          flash("Copied thread ID", "info");
        });
      }
    });
    elements.threadInfoThreadId.style.cursor = "pointer";
  }

  if (elements.threadInfoTurnId) {
    elements.threadInfoTurnId.addEventListener("click", () => {
      const fullId = elements.threadInfoTurnId?.title || "";
      if (fullId) {
        void navigator.clipboard.writeText(fullId).then(() => {
          flash("Copied turn ID", "info");
        });
      }
    });
    elements.threadInfoTurnId.style.cursor = "pointer";
  }
}

export { initPMA };
