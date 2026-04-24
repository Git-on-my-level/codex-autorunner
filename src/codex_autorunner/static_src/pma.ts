/**
 * PMA (Project Management Agent) - Hub-level chat interface
 */
import { api, resolvePath, getAuthToken, flash, escapeHtml } from "./utils.js";
import {
  createDocChat,
  type ChatConfig,
  type ChatStyling,
  type DocChatInstance,
} from "./docChatCore.js";
import { initChatPasteUpload } from "./chatUploads.js";
import {
  DEFAULT_FILEBOX_BOX,
  FILEBOX_BOXES,
  type FileBoxBox,
} from "./fileboxCatalog.js";
import * as agentControlsModule from "./agentControls.js";
import { createFileBoxWidget, type FileBoxListing } from "./fileboxUi.js";
import {
  readEventStream,
  handleStreamEvent,
  type StreamEventHandler,
} from "./streamUtils.js";
import { newClientTurnId as newFileChatTurnId } from "./fileChat.js";

import { initNotificationBell } from "./notificationBell.js";
import { registerAutoRefresh } from "./autoRefresh.js";
import { CONSTANTS } from "./constants.js";
import {
  createTurnEventsController,
  type PendingTurn,
  cancelActiveTurnAndWait,
  scheduleRecoveryRetry,
  createTurnRecoveryTracker,
  ACTIVE_TURN_RECOVERY_STALE_MESSAGE,
} from "./sharedTurnLifecycle.js";
import {
  loadPendingTurn,
  savePendingTurn,
  clearPendingTurn,
} from "./turnResume.js";
import type { OnboardingPreset } from "./walkthrough.js";

// PMA is often the first lazily loaded surface users open after a rebuild. Use a
// namespace import so a stale cached `agentControls.js` cannot fail module linking
// before PMA gets a chance to render recovery/onboarding UI.
function getSelectedAgent(): string {
  if (typeof agentControlsModule.getSelectedAgent === "function") {
    return agentControlsModule.getSelectedAgent();
  }
  return "";
}

function getSelectedProfile(agent = getSelectedAgent()): string {
  if (typeof agentControlsModule.getSelectedProfile === "function") {
    return agentControlsModule.getSelectedProfile(agent);
  }
  return "";
}

function getSelectedModel(agent = getSelectedAgent()): string {
  if (typeof agentControlsModule.getSelectedModel === "function") {
    return agentControlsModule.getSelectedModel(agent);
  }
  return "";
}

function getSelectedReasoning(agent = getSelectedAgent()): string {
  if (typeof agentControlsModule.getSelectedReasoning === "function") {
    return agentControlsModule.getSelectedReasoning(agent);
  }
  return "";
}

function initAgentControls(
  config: Parameters<NonNullable<typeof agentControlsModule.initAgentControls>>[0]
): void {
  if (typeof agentControlsModule.initAgentControls === "function") {
    agentControlsModule.initAgentControls(config);
  }
}

async function refreshAgentControls(
  request?: Parameters<
    NonNullable<typeof agentControlsModule.refreshAgentControls>
  >[0]
): Promise<void> {
  if (typeof agentControlsModule.refreshAgentControls === "function") {
    await agentControlsModule.refreshAgentControls(request);
  }
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

type PMADocInfo = {
  name: string;
  exists: boolean;
  size?: number;
  mtime?: string;
  line_count?: number;
};

type PMADocsResponse = {
  docs: PMADocInfo[];
  active_context_max_lines?: number;
  active_context_auto_prune?: {
    last_auto_pruned_at?: string;
    line_count_before?: number;
    line_budget?: number;
  } | null;
};

type PMADocContent = {
  name: string;
  content: string;
};

type PMADocUpdate = {
  name: string;
  status: string;
};

const EDITABLE_DOCS = ["AGENTS.md", "active_context.md"];
let activeContextMaxLines = 200;

const pmaConfig: ChatConfig = {
  idPrefix: "pma-chat",
  storage: { keyPrefix: "car.pma.", maxMessages: 100, version: 1 },
  limits: {
    eventVisible: 20,
    eventMax: 50,
  },
  styling: pmaStyling,
  compactMode: true,
  inlineEvents: true,
};

let pmaChat: DocChatInstance | null = null;
let currentController: AbortController | null = null;
let currentOutboxBaseline: Set<string> | null = null;
let isUnloading = false;
let unloadHandlerInstalled = false;
const PMA_PENDING_TURN_KEY = "car.pma.pendingTurn";
const PMA_VIEW_KEY = "car.pma.view";
const DEFAULT_PMA_LANE_ID = "pma:default";
let fileBoxCtrl: ReturnType<typeof createFileBoxWidget> | null = null;
let pendingUploadNames: string[] = [];
let currentDocName: string | null = null;
const docsInfo: Map<string, PMADocInfo> = new Map();
let isSavingDoc = false;
let activeContextAutoPrune: PMADocsResponse["active_context_auto_prune"] = null;
let pendingDeliverySummary: PMADeliverySummary | null = null;
let pmaRefreshCleanup: (() => void) | null = null;
const turnEventsCtrl = createTurnEventsController();
let latestPhase: string | null = null;
let latestGuidance: string | null = null;
let latestElapsed: number | null = null;
let currentPMATurnToken = 0;
type PMAView = "chat" | "memory";
type PendingPromptPreset = {
  assistantIntro: string;
  prompt: string;
};

function advancePMATurnToken(): number {
  currentPMATurnToken += 1;
  return currentPMATurnToken;
}

function shouldAppendAsyncOutboxSummary(
  finalizedTurnToken: number,
  currentTurnToken: number,
  chatStatus: string | null | undefined
): boolean {
  return finalizedTurnToken === currentTurnToken && (chatStatus || "") === "done";
}

function loadPMAPendingTurn(): PendingTurn | null {
  return loadPendingTurn(PMA_PENDING_TURN_KEY);
}

function savePMAPendingTurn(turn: PendingTurn): void {
  savePendingTurn(PMA_PENDING_TURN_KEY, turn);
}

function clearPMAPendingTurn(): void {
  clearPendingTurn(PMA_PENDING_TURN_KEY);
}

type PMADeliveryStatus =
  | "success"
  | "partial_success"
  | "failed"
  | "duplicate_only"
  | "skipped";

type PMADeliverySummary = {
  status: PMADeliveryStatus;
  statusText: string;
  messageTag: string;
};

function loadPMAView(): PMAView {
  const raw = localStorage.getItem(PMA_VIEW_KEY);
  if (raw === "memory") return "memory";
  return "chat";
}

function setPMAView(view: PMAView, options: { persist?: boolean } = {}): void {
  const elements = getElements();
  const { persist = true } = options;
  if (persist) {
    localStorage.setItem(PMA_VIEW_KEY, view);
  }
  if (elements.shell) {
    elements.shell.setAttribute("data-pma-view", view);
  }
  document.querySelectorAll<HTMLButtonElement>(".pma-view-btn").forEach((btn) => {
    const isActive = btn.dataset.view === view;
    btn.classList.toggle("active", isActive);
    btn.setAttribute("aria-selected", isActive ? "true" : "false");
  });
  elements.chatSection?.classList.toggle("hidden", view !== "chat");
  elements.docsSection?.classList.toggle("hidden", view !== "memory");
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
    uploadBox: DEFAULT_FILEBOX_BOX,
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
      updateClearButtons(listing);
    },
    onUpload: (names) => {
      pendingUploadNames = names;
    },
  });

  await fileBoxCtrl.refresh();
}

async function startTurnEventsStream(meta: {
  agent: string;
  threadId: string;
  turnId: string;
}): Promise<void> {
  turnEventsCtrl.abort();
  if (!meta.threadId || !meta.turnId) return;
  if ((meta.agent || "codex").trim().toLowerCase() === "opencode") return;

  const ctrl = new AbortController();
  turnEventsCtrl.current = ctrl;

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
    await readPMAStream(res, false);
  } catch {
    // ignore (abort / network)
  }
}

async function loadPMADocs(): Promise<void> {
  try {
    const payload = (await api("/hub/pma/docs", { method: "GET" })) as PMADocsResponse;
    docsInfo.clear();
    if (payload?.docs) {
      payload.docs.forEach((doc) => {
        docsInfo.set(doc.name, doc);
      });
    }
    activeContextMaxLines =
      typeof payload?.active_context_max_lines === "number"
        ? payload.active_context_max_lines
        : 200;
    activeContextAutoPrune = payload?.active_context_auto_prune || null;
    renderPMADocsMeta();
  } catch (err) {
    flash("Failed to load PMA docs", "error");
  }
}

async function loadPMADocContent(name: string): Promise<string> {
  try {
    const payload = (await api(`/hub/pma/docs/${encodeURIComponent(name)}`, {
      method: "GET",
    })) as PMADocContent;
    return payload?.content || "";
  } catch (err) {
    const content = await bootstrapPMADoc(name);
    if (content) {
      return content;
    }
    flash(`Failed to load ${name}`, "error");
    return "";
  }
}

async function loadPMADocDefaultContent(
  name: string,
  options: { silent?: boolean } = {}
): Promise<string> {
  try {
    const payload = (await api(`/hub/pma/docs/default/${encodeURIComponent(name)}`, {
      method: "GET",
    })) as PMADocContent;
    return payload?.content || "";
  } catch (err) {
    if (!options.silent) {
      flash(`Failed to load default ${name}`, "error");
    }
    return "";
  }
}

async function bootstrapPMADoc(name: string): Promise<string> {
  const content = await loadPMADocDefaultContent(name, { silent: true });
  if (!content) return "";
  try {
    await api(`/hub/pma/docs/${encodeURIComponent(name)}`, {
      method: "PUT",
      body: { content },
    });
    await loadPMADocs();
    return content;
  } catch {
    return content;
  }
}

async function savePMADoc(name: string, content: string): Promise<void> {
  if (isSavingDoc) return;
  isSavingDoc = true;
  try {
    const payload = (await api(`/hub/pma/docs/${encodeURIComponent(name)}`, {
      method: "PUT",
      body: { content },
    })) as PMADocUpdate;
    if (payload?.status === "ok") {
      flash(`Saved ${name}`, "info");
      await loadPMADocs();
    }
  } catch (err) {
    flash(`Failed to save ${name}`, "error");
  } finally {
    isSavingDoc = false;
  }
}

function renderPMADocsMeta(): void {
  const metaEl = document.getElementById("pma-docs-meta");
  if (!metaEl) return;
  const activeInfo = docsInfo.get("active_context.md");
  if (!activeInfo) {
    metaEl.innerHTML = "";
    return;
  }
  const lineCount = activeInfo.line_count || 0;
  const maxLines = activeContextMaxLines || 200;
  const percent = Math.min(100, Math.round((lineCount / maxLines) * 100));
  const statusClass = percent >= 90 ? "pill-warn" : percent >= 70 ? "pill-caution" : "pill-idle";
  let autoPruneHtml = "";
  const autoPrunedAt = activeContextAutoPrune?.last_auto_pruned_at;
  if (typeof autoPrunedAt === "string" && autoPrunedAt) {
    const before = typeof activeContextAutoPrune?.line_count_before === "number"
      ? String(activeContextAutoPrune.line_count_before)
      : "?";
    const budget = typeof activeContextAutoPrune?.line_budget === "number"
      ? String(activeContextAutoPrune.line_budget)
      : String(maxLines);
    autoPruneHtml = `
      <div class="pma-docs-meta-item">
        <span class="muted">Last auto-prune:</span>
        <span class="pill pill-small pill-caution">${escapeHtml(autoPrunedAt)}</span>
        <span class="muted small">(${escapeHtml(before)} -> ${escapeHtml(budget)} lines)</span>
      </div>
    `;
  }
  metaEl.innerHTML = `
    <div class="pma-docs-meta-item">
      <span class="muted">Active context:</span>
      <span class="pill pill-small ${statusClass}">${lineCount} / ${maxLines} lines</span>
    </div>
    <div class="pma-docs-meta-item">
      <span class="muted">Automation:</span>
      <span class="muted small">See Ops Guide (ABOUT_CAR.md) for subscription/timer recipes.</span>
    </div>
    ${autoPruneHtml}
  `;
}

function switchPMADoc(name: string): void {
  currentDocName = name;
  const tabs = document.querySelectorAll(".pma-docs-tab");
  tabs.forEach((tab) => {
    if (tab instanceof HTMLElement && tab.dataset.doc === name) {
      tab.classList.add("active");
    } else {
      tab.classList.remove("active");
    }
  });
  const editor = document.getElementById("pma-docs-editor") as HTMLTextAreaElement | null;
  const resetBtn = document.getElementById("pma-docs-reset") as HTMLButtonElement | null;
  const snapshotBtn = document.getElementById("pma-docs-snapshot") as HTMLButtonElement | null;
  const saveBtn = document.getElementById("pma-docs-save") as HTMLButtonElement | null;

  if (!editor) return;

  const isEditable = EDITABLE_DOCS.includes(name);
  editor.readOnly = !isEditable;
  if (resetBtn) resetBtn.disabled = name !== "active_context.md";
  if (snapshotBtn) snapshotBtn.disabled = name !== "active_context.md";
  if (saveBtn) saveBtn.disabled = !isEditable;

  void loadPMADocContent(name).then((content) => {
    editor.value = content;
  });
}

async function snapshotActiveContext(): Promise<void> {
  const editor = document.getElementById("pma-docs-editor") as HTMLTextAreaElement | null;
  if (!editor) return;

  try {
    const payload = (await api("/hub/pma/context/snapshot", {
      method: "POST",
      body: { reset: true },
    })) as { status?: string; warning?: string };
    if (payload?.status !== "ok") {
      throw new Error("snapshot failed");
    }
    const resetContent = await loadPMADocContent("active_context.md");
    editor.value = resetContent;
    const message = payload?.warning
      ? `Active context snapshot saved (${payload.warning})`
      : "Active context snapshot saved";
    flash(message, "info");
    await loadPMADocs();
  } catch (err) {
    flash("Failed to snapshot active context", "error");
  }
}

function resetActiveContext(): void {
  if (!confirm("Reset active context to default?")) return;
  const editor = document.getElementById("pma-docs-editor") as HTMLTextAreaElement | null;
  if (!editor) return;
  void loadPMADocDefaultContent("active_context.md").then((resetContent) => {
    if (!resetContent) return;
    editor.value = resetContent;
    void savePMADoc("active_context.md", resetContent);
  });
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

async function pollForTurnMeta(
  clientTurnId: string,
  options: { timeoutMs?: number; signal?: AbortSignal } = {}
): Promise<void> {
  if (!clientTurnId) return;
  const timeoutMs = options.timeoutMs ?? 8000;
  const started = Date.now();

  while (Date.now() - started < timeoutMs) {
    if (!pmaChat || pmaChat.state.status !== "running") return;
    if (turnEventsCtrl.current) return;
    if (options.signal?.aborted) return;

    try {
      const payload = (await api(
        `/hub/pma/active?client_turn_id=${encodeURIComponent(clientTurnId)}`,
        { method: "GET" }
      )) as { current?: Record<string, unknown> };
      const cur = (payload.current || {}) as Record<string, unknown>;
      const threadId = typeof cur.thread_id === "string" ? cur.thread_id : "";
      const turnId = typeof cur.turn_id === "string" ? cur.turn_id : "";
      const agent = typeof cur.agent === "string" ? cur.agent : "codex";
      if (threadId && turnId && agent.trim().toLowerCase() !== "opencode") {
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
    chatSection: document.getElementById("pma-chat-section"),
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
    profileSelect: document.getElementById("pma-chat-profile-select") as HTMLSelectElement | null,
    modelSelect: document.getElementById("pma-chat-model-select") as HTMLSelectElement | null,
    modelInput: document.getElementById("pma-chat-model-input") as HTMLInputElement | null,
    reasoningSelect: document.getElementById("pma-chat-reasoning-select") as HTMLSelectElement | null,
    chatUploadInput: document.getElementById("pma-chat-upload-input") as HTMLInputElement | null,
    chatUploadBtn: document.getElementById("pma-chat-upload-btn") as HTMLButtonElement | null,
    inboxFiles: document.getElementById("pma-inbox-files"),
    inboxHint: document.getElementById("pma-inbox-hint"),
    outboxFiles: document.getElementById("pma-outbox-files"),
    outboxRefresh: document.getElementById("pma-outbox-refresh") as HTMLButtonElement | null,
    inboxClear: document.getElementById("pma-inbox-clear") as HTMLButtonElement | null,
    outboxClear: document.getElementById("pma-outbox-clear") as HTMLButtonElement | null,
    threadInfo: document.getElementById("pma-thread-info"),
    threadInfoAgent: document.getElementById("pma-thread-info-agent"),
    threadInfoThreadId: document.getElementById("pma-thread-info-thread-id"),
    threadInfoTurnId: document.getElementById("pma-thread-info-turn-id"),
    threadInfoStatus: document.getElementById("pma-thread-info-status"),
    repoActions: document.getElementById("pma-repo-actions"),
    scanReposBtn: document.getElementById("pma-scan-repos-btn"),
    docsSection: document.getElementById("pma-docs-section"),
    docsEditor: document.getElementById("pma-docs-editor") as HTMLTextAreaElement | null,
    docsSaveBtn: document.getElementById("pma-docs-save") as HTMLButtonElement | null,
    docsResetBtn: document.getElementById("pma-docs-reset") as HTMLButtonElement | null,
    docsSnapshotBtn: document.getElementById("pma-docs-snapshot") as HTMLButtonElement | null,
  };
}

function escapeMarkdownLinkText(text: string): string {
  // Keep this ES2019-compatible (no String.prototype.replaceAll).
  return text.replace(/\\/g, "\\\\").replace(/\[/g, "\\[").replace(/\]/g, "\\]");
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

function buildOutboxAttachmentSummary(
  listing: FileBoxListing | null,
  baseline: ReadonlySet<string> | null
): string {
  if (!listing || !baseline) return "";
  const added = (listing.outbox || [])
    .map((entry) => entry.name)
    .filter((name) => !baseline.has(name));
  return formatOutboxAttachments(listing, added);
}

function normalizeDeliveryStatus(value: unknown): PMADeliveryStatus | null {
  if (typeof value !== "string") return null;
  const normalized = value.trim().toLowerCase();
  if (
    normalized === "success" ||
    normalized === "partial_success" ||
    normalized === "failed" ||
    normalized === "duplicate_only" ||
    normalized === "skipped"
  ) {
    return normalized;
  }
  return null;
}

function summarizeDeliveryStatus(result: Record<string, unknown>): PMADeliveryStatus | null {
  return normalizeDeliveryStatus(result.delivery_status);
}

function deriveDeliverySummary(payload: unknown): PMADeliverySummary | null {
  if (!payload || typeof payload !== "object") return null;
  const status = summarizeDeliveryStatus(payload as Record<string, unknown>);
  if (!status) return null;

  if (status === "success") {
    return {
      status,
      statusText: "Done · delivery sent",
      messageTag: "delivery:success",
    };
  }
  if (status === "partial_success") {
    return {
      status,
      statusText: "Done · delivery partial",
      messageTag: "delivery:partial_success",
    };
  }
  if (status === "failed") {
    return {
      status,
      statusText: "Done · delivery failed",
      messageTag: "delivery:failed",
    };
  }
  if (status === "duplicate_only") {
    return {
      status,
      statusText: "Done · delivery duplicate",
      messageTag: "delivery:duplicate_only",
    };
  }
  return {
    status,
    statusText: "Done · delivery skipped",
    messageTag: "delivery:skipped",
  };
}

async function finalizePMAResponse(
  responseText: string,
  options: { deliverySummary?: PMADeliverySummary | null } = {}
): Promise<void> {
  if (!pmaChat) return;

  const deliverySummary = options.deliverySummary ?? pendingDeliverySummary;
  const finalizedTurnToken = currentPMATurnToken;
  const outboxBaseline = currentOutboxBaseline
    ? new Set(currentOutboxBaseline)
    : null;

  currentOutboxBaseline = null;
  clearPMAPendingTurn();
  turnEventsCtrl.abort();
  latestPhase = null;
  latestGuidance = null;
  latestElapsed = null;

  const trimmed = (responseText || "").trim();
  const content = trimmed;

  const startTime = pmaChat.state.startTime;
  const duration = startTime ? (Date.now() - startTime) / 1000 : undefined;
  const steps = pmaChat.state.totalEvents || pmaChat.state.events.length;

  if (content) {
    pmaChat.addAssistantMessage(content, true, {
      steps,
      duration,
      tag: deliverySummary?.messageTag,
    });
  }
  pmaChat.state.streamText = "";
  pmaChat.state.status = "done";
  pmaChat.state.statusText = deliverySummary?.statusText || "Done";
  pmaChat.render();
  pmaChat.renderMessages();
  pmaChat.renderEvents();
  pendingDeliverySummary = null;

  void (async () => {
    let attachments = "";
    try {
      if (fileBoxCtrl) {
        const current = await fileBoxCtrl.refresh();
        attachments = buildOutboxAttachmentSummary(current, outboxBaseline);
      }
    } catch {
      attachments = "";
    }
    if (
      attachments &&
      pmaChat &&
      shouldAppendAsyncOutboxSummary(
        finalizedTurnToken,
        currentPMATurnToken,
        pmaChat.state.status
      )
    ) {
      pmaChat.addAssistantMessage(attachments, true);
      pmaChat.renderMessages();
    }
    void fileBoxCtrl?.refresh();
  })();
}

/**
 * Applies a walkthrough preset from sessionStorage (set before navigating to PMA).
 * We accept the legacy plain-string format for backward compatibility, but prefer
 * the structured preset so onboarding can seed both the PMA intro bubble and the
 * composer prefill without auto-sending.
 * Exported so the hub shell can run this after `showPMAView` when PMA was already initialized.
 */
function parsePendingPromptPreset(raw: string): PendingPromptPreset | null {
  const trimmed = raw.trim();
  if (!trimmed) return null;
  if (!trimmed.startsWith("{")) {
    return { assistantIntro: "", prompt: trimmed };
  }
  try {
    const parsed = JSON.parse(trimmed) as Partial<OnboardingPreset>;
    const assistantIntro =
      typeof parsed?.assistantIntro === "string" ? parsed.assistantIntro.trim() : "";
    const prompt = typeof parsed?.prompt === "string" ? parsed.prompt.trim() : "";
    if (!assistantIntro && !prompt) return null;
    return { assistantIntro, prompt };
  } catch {
    return { assistantIntro: "", prompt: trimmed };
  }
}

function drainPendingPrompt(): void {
  const raw = (() => {
    try {
      const stored = sessionStorage.getItem("car-pma-pending-prompt") || "";
      if (stored) sessionStorage.removeItem("car-pma-pending-prompt");
      return stored;
    } catch {
      return "";
    }
  })();
  const pending = parsePendingPromptPreset(raw);
  if (!pending) return;

  if (
    pending.assistantIntro &&
    pmaChat &&
    pmaChat.state.messages.length === 0
  ) {
    pmaChat.addAssistantMessage(pending.assistantIntro, true, {
      tag: "onboarding:intro",
    });
    pmaChat.render();
    pmaChat.renderMessages();
  }

  const elements = getElements();
  if (!elements.input) return;
  if (!pending.prompt) return;
  elements.input.value = pending.prompt;
  elements.input.dispatchEvent(new Event("input", { bubbles: true }));
  elements.input.focus();
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
    profileSelect: elements.profileSelect,
    modelSelect: elements.modelSelect,
    modelInput: elements.modelInput,
    reasoningSelect: elements.reasoningSelect,
  });

  await refreshAgentControls({ force: true, reason: "initial" });
  await loadPMAThreadInfo();
  await initFileBoxUI();
  await loadPMADocs();
  if (!currentDocName) {
    switchPMADoc("AGENTS.md");
  }
  attachHandlers();
  setPMAView(loadPMAView(), { persist: false });
  initNotificationBell();
  drainPendingPrompt();

  // If we refreshed mid-turn, recover the final output from the server.
  await resumePendingTurn();

  // If the page refreshes/navigates while a turn is running, avoid showing a noisy
  // "network error" and proactively interrupt the running turn on the server to
  // prevent the next request from receiving a stale/previous response.
  if (!unloadHandlerInstalled) {
    unloadHandlerInstalled = true;
    window.addEventListener("beforeunload", () => {
      isUnloading = true;
      stopPMARefreshLoop();
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

  startPMARefreshLoop();
}

function startPMARefreshLoop(): void {
  if (pmaRefreshCleanup) return;
  pmaRefreshCleanup = registerAutoRefresh("pma:refresh", {
    callback: async () => {
      const shell = document.getElementById("pma-shell");
      if (!shell || shell.classList.contains("hidden")) return;
      await loadPMAThreadInfo();
      await fileBoxCtrl?.refresh();
    },
    interval: CONSTANTS.UI.AUTO_REFRESH_INTERVAL,
    refreshOnActivation: true,
    immediate: false,
  });
}

function stopPMARefreshLoop(): void {
  if (pmaRefreshCleanup) {
    pmaRefreshCleanup();
    pmaRefreshCleanup = null;
  }
}

export function setPMARefreshActive(active: boolean): void {
  if (active) {
    startPMARefreshLoop();
  } else {
    stopPMARefreshLoop();
  }
}

type PMAQueueItem = {
  item_id: string;
  state: string;
  enqueued_at: string;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  dedupe_reason: string | null;
};

type PMAQueueResponse = {
  lane_id: string;
  items: PMAQueueItem[];
};

function deriveThreadPillState(
  active: boolean,
  currentStatus: string,
  queuePending: number
): { label: string; pillClass: string } {
  const status = currentStatus.trim().toLowerCase();
  if (status === "running" && active) return { label: "running", pillClass: "pill-running" };
  if (status === "interrupted") return { label: "interrupted", pillClass: "pill-warn" };
  if (status === "error" || status === "failed") return { label: "failed", pillClass: "pill-error" };
  if (status === "ok" || status === "completed") return { label: "completed", pillClass: "pill-completed" };
  if (active && queuePending > 0) return { label: "queued", pillClass: "pill-queued" };
  if (active) return { label: "accepted", pillClass: "pill-accepted" };
  if (status === "stalled") return { label: "stalled", pillClass: "pill-stalled" };
  return { label: "idle", pillClass: "pill-idle" };
}

function formatElapsedSeconds(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}m ${s}s`;
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

    let queuePending = 0;
    let queuedItems: PMAQueueItem[] = [];
    try {
      const queuePayload = (await api("/hub/pma/queue/pma:default", {
        method: "GET",
      })) as PMAQueueResponse;
      queuedItems = (queuePayload.items || []).filter(
        (item) => item.state === "pending" || item.state === "running"
      );
      queuePending = queuedItems.filter((item) => item.state === "pending").length;
    } catch {
      // queue endpoint may not be available
    }

    const turnStatus = String(
      info.status || (payload.active ? "running" : (last.status || "idle"))
    );

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

    const { label, pillClass } = deriveThreadPillState(
      !!payload.active,
      turnStatus,
      queuePending
    );
    if (elements.threadInfoStatus) {
      elements.threadInfoStatus.textContent = label;
      elements.threadInfoStatus.className = `pill pill-small ${pillClass}`;
    }

    const phaseEl = document.getElementById("pma-thread-info-phase");
    if (phaseEl) {
      const phase = latestPhase || String(info.phase || "");
      if (phase && payload.active) {
        phaseEl.textContent = phase;
        phaseEl.classList.remove("hidden");
      } else {
        phaseEl.classList.add("hidden");
      }
    }

    const queueRow = document.getElementById("pma-thread-info-queue-row");
    const queueValue = document.getElementById("pma-thread-info-queue");
    if (queueRow && queueValue) {
      if (queuePending > 0 || queuedItems.length > 0) {
        queueValue.textContent =
          queuePending > 0
            ? `${queuePending} waiting`
            : `${queuedItems.length} active`;
        queueRow.classList.remove("hidden");
      } else {
        queueRow.classList.add("hidden");
      }
    }

    const elapsedRow = document.getElementById("pma-thread-info-elapsed-row");
    const elapsedValue = document.getElementById("pma-thread-info-elapsed");
    if (elapsedRow && elapsedValue) {
      const elapsed = latestElapsed;
      if (elapsed != null && elapsed > 0 && payload.active) {
        elapsedValue.textContent = formatElapsedSeconds(elapsed);
        elapsedRow.classList.remove("hidden");
      } else {
        elapsedRow.classList.add("hidden");
      }
    }

    const guidanceEl = document.getElementById("pma-thread-info-guidance");
    if (guidanceEl) {
      const guidance = latestGuidance || String(info.guidance || "");
      if (guidance && payload.active) {
        guidanceEl.textContent = guidance;
        guidanceEl.classList.remove("hidden");
      } else {
        guidanceEl.classList.add("hidden");
      }
    }

    const queueListEl = document.getElementById("pma-thread-info-queue-list");
    if (queueListEl) {
      const pendingItems = queuedItems.filter((item) => item.state === "pending");
      if (pendingItems.length > 0) {
        queueListEl.innerHTML = pendingItems
          .slice(0, 3)
          .map(
            (item) =>
              `<div class="pma-thread-info-queue-list-item"><span class="pill pill-small pill-queued">${escapeHtml(item.state)}</span><span class="muted small">${escapeHtml(item.enqueued_at || "")}</span></div>`
          )
          .join("");
        queueListEl.classList.remove("hidden");
      } else {
        queueListEl.classList.add("hidden");
      }
    }

    elements.threadInfo.classList.remove("hidden");
  } catch {
    elements.threadInfo?.classList.add("hidden");
  }
}

function updateClearButtons(listing: FileBoxListing | null): void {
  const elements = getElements();
  if (!elements.inboxClear || !elements.outboxClear) return;
  const inboxCount = listing?.inbox?.length ?? 0;
  const outboxCount = listing?.outbox?.length ?? 0;
  elements.inboxClear.classList.toggle("hidden", inboxCount <= 1);
  elements.outboxClear.classList.toggle("hidden", outboxCount <= 1);
  if (elements.inboxHint) {
    const hasInbox = inboxCount > 0;
    elements.inboxHint.textContent = hasInbox ? "Next: Process uploaded files" : "";
    elements.inboxHint.classList.toggle("hidden", !hasInbox);
  }
}

async function clearPMABox(box: FileBoxBox): Promise<void> {
  const confirmed = window.confirm(`Clear ${box}? This will delete all files.`);
  if (!confirmed) return;
  await api(`/hub/pma/files/${box}`, { method: "DELETE" });
  flash(`Cleared ${box}`, "info");
  await fileBoxCtrl?.refresh();
}

async function sendMessage(): Promise<void> {
  const elements = getElements();
  if (!elements.input || !pmaChat) return;

  const message = elements.input.value?.trim() || "";
  if (!message) return;

  advancePMATurnToken();

  if (currentController) {
    await cancelActiveTurnAndWait({
      abortController() {
        if (currentController) {
          currentController.abort();
          currentController = null;
        }
        pmaChat!.state.controller = null;
      },
      turnEventsCtrl,
      interruptServer: () => interruptActiveTurn(),
      clearPending: clearPMAPendingTurn,
    });
  }

  // Ensure prior turn event streams are cleared so we don't render stale actions.
  turnEventsCtrl.abort();
  pendingDeliverySummary = null;
  latestPhase = null;
  latestGuidance = null;
  latestElapsed = null;

  elements.input.value = "";
  elements.input.style.height = "auto";

  const agent = elements.agentSelect?.value || getSelectedAgent();
  const profile = elements.profileSelect?.value || getSelectedProfile(agent);
  const model = elements.modelSelect?.value || getSelectedModel(agent);
  const reasoning = elements.reasoningSelect?.value || getSelectedReasoning(agent);
  const clientTurnId = newFileChatTurnId("pma");
  savePMAPendingTurn({ clientTurnId, message, startedAtMs: Date.now() });

  currentController = new AbortController();
  pmaChat.state.controller = currentController;
  pmaChat.state.status = "running";
  pmaChat.state.error = "";
  pmaChat.state.statusText = "";
  pmaChat.state.contextUsagePercent = null;
  pmaChat.state.streamText = "";
  pmaChat.state.contextUsagePercent = null;
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
    if (profile) payload.profile = profile;
    if (model) payload.model = model;
    if (reasoning) payload.reasoning = reasoning;

    const responsePromise = fetch(endpoint, {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
      signal: currentController.signal,
    });

    // Stream tool calls/events separately as soon as we have (thread_id, turn_id).
    // The main /hub/pma/chat stream only emits a final "update"/"done" today.
    void pollForTurnMeta(clientTurnId, { signal: currentController.signal });

    const res = await responsePromise;
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

    const contentType = res.headers.get("content-type") || "";
    if (contentType.includes("text/event-stream")) {
      await readPMAStream(res, true);
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
      pendingDeliverySummary = null;
      pmaChat.render();
      return;
    }
    const errorMsg = (err as Error).message || "Request failed";
    pmaChat.state.status = "error";
    pmaChat.state.error = errorMsg;
    pmaChat.addAssistantMessage(`Error: ${errorMsg}`, true);
    pmaChat.render();
    pmaChat.renderMessages();
    clearPMAPendingTurn();
    turnEventsCtrl.abort();
    pendingDeliverySummary = null;
  } finally {
    currentController = null;
    pmaChat.state.controller = null;
  }
}

async function readPMAStream(res: Response, finalizeOnClose = false): Promise<void> {
  await readEventStream(
    res,
    (event, data) => handleStreamEvent(event, data, pmaStreamHandlers),
    { handleEscapedNewlines: true }
  );

  if (finalizeOnClose && pmaChat && pmaChat.state.status === "running") {
    const responseText = pmaChat.state.streamText || "";
    if (responseText.trim()) {
      void finalizePMAResponse(responseText);
    }
  }
}

const pmaStreamHandlers: StreamEventHandler = {
  onStatus(status) {
    pmaChat!.state.statusText = status;
    latestPhase = status;
    pmaChat!.render();
    pmaChat!.renderEvents();
  },

  onToken(token) {
    pmaChat!.state.streamText = (pmaChat!.state.streamText || "") + token;
    if (!pmaChat!.state.statusText || pmaChat!.state.statusText === "starting") {
      pmaChat!.state.statusText = "responding";
      latestPhase = "responding";
    }
    if (pmaChat!.state.status !== "running") {
      pmaChat!.state.status = "running";
    }
    if (pmaChat!.state.startTime) {
      latestElapsed = Math.round((Date.now() - pmaChat!.state.startTime) / 1000);
    }
    pmaChat!.render();
  },

  onTokenUsage(percentRemaining) {
    if (pmaChat) {
      pmaChat.state.contextUsagePercent = percentRemaining;
      pmaChat.render();
    }
  },

  onUpdate(payload) {
    if (payload.client_turn_id) {
      clearPMAPendingTurn();
    }
    if (payload.message) {
      pmaChat!.state.streamText = payload.message as string;
    }
    if (payload.phase && typeof payload.phase === "string") {
      latestPhase = payload.phase;
    }
    if (payload.guidance && typeof payload.guidance === "string") {
      latestGuidance = payload.guidance;
    }
    if (payload.elapsed_seconds != null) {
      latestElapsed = typeof payload.elapsed_seconds === "number"
        ? payload.elapsed_seconds
        : null;
    }
    const summary = deriveDeliverySummary(payload);
    if (summary) {
      pendingDeliverySummary = summary;
    }
    pmaChat!.render();
  },

  onEvent(event) {
    if (pmaChat) {
      if (pmaChat.state.status !== "running") {
        pmaChat.state.status = "running";
      }
      if (!pmaChat.state.statusText || pmaChat.state.statusText === "starting") {
        pmaChat.state.statusText = "working";
      }
      pmaChat.applyAppEvent(event);
      pmaChat.renderEvents();
      pmaChat.render();
    }
    if (event && typeof event === "object") {
      const evt = event as Record<string, unknown>;
      const title = String(evt.title || evt.kind || "");
      const summary = String(evt.summary || "");
      if (title || summary) {
        latestPhase = title || latestPhase;
        if (!latestGuidance || summary) {
          latestGuidance = summary || latestGuidance;
        }
      }
    }
  },

  onError(message) {
    pendingDeliverySummary = null;
    latestPhase = null;
    latestGuidance = null;
    latestElapsed = null;
    pmaChat!.state.status = "error";
    pmaChat!.state.error = message;
    pmaChat!.addAssistantMessage(`Error: ${message}`, true);
    pmaChat!.render();
    pmaChat!.renderMessages();
    throw new Error(message);
  },

  onInterrupted(message) {
    pendingDeliverySummary = null;
    latestPhase = null;
    latestGuidance = null;
    latestElapsed = null;
    pmaChat!.state.status = "interrupted";
    pmaChat!.state.error = "";
    pmaChat!.state.statusText = message;
    pmaChat!.addAssistantMessage("Request interrupted", true);
    pmaChat!.render();
    pmaChat!.renderMessages();
  },

  onDone() {
    void finalizePMAResponse(pmaChat!.state.streamText || "");
  },
};

async function resumePendingTurn(): Promise<void> {
  const pending = loadPMAPendingTurn();
  if (!pending || !pmaChat) return;

  // Show a running indicator immediately.
  pmaChat.state.status = "running";
  pmaChat.state.statusText = "Recovering previous turn…";
  pmaChat.render();
  pmaChat.renderMessages();

  const tracker = createTurnRecoveryTracker();

  const onStale = (): void => {
    if (!pmaChat) return;
    pmaChat.state.status = "error";
    pmaChat.state.error = ACTIVE_TURN_RECOVERY_STALE_MESSAGE;
    clearPMAPendingTurn();
    turnEventsCtrl.abort();
    pmaChat.render();
    pmaChat.renderMessages();
  };

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
      if (
        threadId &&
        turnId &&
        !turnEventsCtrl.current &&
        agent.trim().toLowerCase() !== "opencode"
      ) {
        void startTurnEventsStream({ agent, threadId, turnId });
      }

      const last = (payload.last_result || {}) as Record<string, unknown>;
      const status = String(last.status || "");
      if (status === "ok" && typeof last.message === "string") {
        const summary = deriveDeliverySummary(last);
        pendingDeliverySummary = summary;
        await finalizePMAResponse(last.message, { deliverySummary: summary });
        return;
      }
      if (status === "error") {
        const detail = String(last.detail || "PMA chat failed");
        pendingDeliverySummary = null;
        pmaChat.state.status = "error";
        pmaChat.state.error = detail;
        pmaChat.addAssistantMessage(`Error: ${detail}`, true);
        pmaChat.render();
        pmaChat.renderMessages();
        clearPMAPendingTurn();
        turnEventsCtrl.abort();
        return;
      }
      if (status === "interrupted") {
        pendingDeliverySummary = null;
        pmaChat.state.status = "interrupted";
        pmaChat.state.error = "";
        pmaChat.addAssistantMessage("Request interrupted", true);
        pmaChat.render();
        pmaChat.renderMessages();
        clearPMAPendingTurn();
        turnEventsCtrl.abort();
        return;
      }

      // Still running; schedule bounded retry.
      pmaChat.state.status = "running";
      pmaChat.state.statusText = "Recovering previous turn…";
      pmaChat.render();
      scheduleRecoveryRetry({ tracker, retryFn: poll, onStale });
    } catch {
      scheduleRecoveryRetry({ tracker, retryFn: poll, onStale });
    }
  };

  await poll();
}

function applyPMAResult(payload: unknown): void {
  if (!payload || typeof payload !== "object") return;

  const result = payload as Record<string, unknown>;

  if (result.status === "interrupted") {
    pendingDeliverySummary = null;
    pmaChat!.state.status = "interrupted";
    pmaChat!.state.error = "";
    pmaChat!.addAssistantMessage("Request interrupted", true);
    pmaChat!.render();
    pmaChat!.renderMessages();
    return;
  }

  if (result.status === "error" || result.error) {
    pendingDeliverySummary = null;
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

  const summary = deriveDeliverySummary(result);
  pendingDeliverySummary = summary;
  const responseText = (pmaChat!.state.streamText || pmaChat!.state.statusText || "Done") as string;
  void finalizePMAResponse(responseText, { deliverySummary: summary });
}

type CancelRequestOptions = {
  clearPending?: boolean;
  interruptServer?: boolean;
  stopLane?: boolean;
  statusText?: string;
};

async function interruptActiveTurn(options: { stopLane?: boolean } = {}): Promise<void> {
  try {
    if (options.stopLane) {
      await api("/hub/pma/stop", {
        method: "POST",
        body: { lane_id: DEFAULT_PMA_LANE_ID },
      });
      return;
    }
    await api("/hub/pma/interrupt", { method: "POST" });
  } catch {
    // Best-effort; UI state already reflects cancellation.
  }
}

async function cancelRequest(options: CancelRequestOptions = {}): Promise<void> {
  const { clearPending = false, interruptServer = false, stopLane = false, statusText } = options;
  advancePMATurnToken();
  if (currentController) {
    currentController.abort();
    currentController = null;
  }
  pendingDeliverySummary = null;
  turnEventsCtrl.abort();
  latestPhase = null;
  latestGuidance = null;
  latestElapsed = null;
  if (interruptServer || stopLane) {
    await interruptActiveTurn({ stopLane });
  }
  if (clearPending) {
    clearPMAPendingTurn();
  }
  if (pmaChat) {
    pmaChat.state.controller = null;
    pmaChat.state.status = "interrupted";
    pmaChat.state.statusText = statusText || "Cancelled";
    pmaChat.state.contextUsagePercent = null;
    pmaChat.render();
  }
}

function resetThread(): void {
  advancePMATurnToken();
  clearPMAPendingTurn();
  pendingDeliverySummary = null;
  turnEventsCtrl.abort();
  latestPhase = null;
  latestGuidance = null;
  latestElapsed = null;
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

async function startNewThreadOnServer(): Promise<void> {
  const elements = getElements();
  const rawAgent = (elements.agentSelect?.value || getSelectedAgent() || "").trim().toLowerCase();
  const selectedAgent = rawAgent || undefined;
  const selectedProfile = elements.profileSelect?.value || getSelectedProfile(rawAgent);
  await api("/hub/pma/new", {
    method: "POST",
    body: {
      agent: selectedAgent,
      profile: selectedProfile || undefined,
      lane_id: DEFAULT_PMA_LANE_ID,
    },
  });
}

function attachHandlers(): void {
  const elements = getElements();
  document.addEventListener("click", (event) => {
    const target = event.target as HTMLElement | null;
    const btn = target?.closest?.(".pma-view-btn") as HTMLButtonElement | null;
    if (!btn) return;
    const value = (btn.dataset.view || "chat") as PMAView;
    setPMAView(value);
  });

  if (elements.sendBtn) {
    elements.sendBtn.addEventListener("click", () => {
      void sendMessage();
    });
  }

  document.addEventListener("pma:inject-prompt", (evt) => {
    const detail = (evt as CustomEvent<{ prompt?: string }>).detail;
    const prompt = typeof detail?.prompt === "string" ? detail.prompt : "";
    if (!prompt || !elements.input) return;
    try {
      sessionStorage.removeItem("car-pma-pending-prompt");
    } catch {
      // ignore
    }
    elements.input.value = prompt;
    elements.input.dispatchEvent(new Event("input", { bubbles: true }));
    elements.input.focus();
  });

  if (elements.cancelBtn) {
    elements.cancelBtn.addEventListener("click", () => {
      void cancelRequest({ clearPending: true, interruptServer: true });
    });
  }

  if (elements.newThreadBtn) {
    elements.newThreadBtn.addEventListener("click", () => {
      void (async () => {
        await cancelRequest({
          clearPending: true,
          interruptServer: true,
          stopLane: true,
          statusText: "Cancelled (new thread)",
        });
        try {
          await startNewThreadOnServer();
        } catch (err) {
          flash("Failed to start new session", "error");
          return;
        }
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
      box: DEFAULT_FILEBOX_BOX,
      insertStyle: "markdown",
      onUploaded: () => {
        void fileBoxCtrl?.refresh();
      },
    });
  }

  if (elements.outboxRefresh) {
    elements.outboxRefresh.addEventListener("click", () => {
      void fileBoxCtrl?.refresh();
    });
  }

  if (elements.inboxClear) {
    elements.inboxClear.addEventListener("click", () => {
      void clearPMABox(FILEBOX_BOXES[0]);
    });
  }

  if (elements.outboxClear) {
    elements.outboxClear.addEventListener("click", () => {
      void clearPMABox(FILEBOX_BOXES[1]);
    });
  }

  if (elements.scanReposBtn) {
    elements.scanReposBtn.addEventListener("click", async () => {
      const btn = elements.scanReposBtn as HTMLButtonElement;
      const originalText = btn.textContent || "";
      try {
        btn.disabled = true;
        await api("/hub/repos/scan", { method: "POST" });
        flash("Repositories scanned", "info");
      } catch (err) {
        flash("Failed to scan repos", "error");
      } finally {
        btn.disabled = false;
        btn.textContent = btn.textContent || originalText;
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

  document.querySelectorAll(".pma-docs-tab").forEach((tab) => {
    if (tab instanceof HTMLElement) {
      tab.addEventListener("click", () => {
        const docName = tab.dataset.doc;
        if (docName) switchPMADoc(docName);
      });
    }
  });

  if (elements.docsSaveBtn) {
    elements.docsSaveBtn.addEventListener("click", () => {
      const editor = elements.docsEditor;
      if (editor && currentDocName && EDITABLE_DOCS.includes(currentDocName)) {
        void savePMADoc(currentDocName, editor.value);
      }
    });
  }

  if (elements.docsResetBtn) {
    elements.docsResetBtn.addEventListener("click", resetActiveContext);
  }

  if (elements.docsSnapshotBtn) {
    elements.docsSnapshotBtn.addEventListener("click", () => {
      void snapshotActiveContext();
    });
  }

  if (elements.docsEditor) {
    elements.docsEditor.addEventListener("input", () => {
      elements.docsEditor.style.height = "auto";
      elements.docsEditor.style.height = `${elements.docsEditor.scrollHeight}px`;
    });
  }
}

const __pmaTest = {
  buildOutboxAttachmentSummary,
  parsePendingPromptPreset,
  shouldAppendAsyncOutboxSummary,
};

export { __pmaTest, drainPendingPrompt, initPMA };
