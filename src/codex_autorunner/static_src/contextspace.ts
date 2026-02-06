import { api, confirmModal, flash, setButtonLoading } from "./utils.js";
import { initAgentControls, getSelectedAgent, getSelectedModel, getSelectedReasoning } from "./agentControls.js";
import {
  fetchContextspace,
  ingestSpecToTickets,
  listTickets,
  ContextspaceKind,
  ContextspaceNode,
  fetchContextspaceTree,
  uploadContextspaceFiles,
  downloadContextspaceZip,
  createContextspaceFolder,
  writeContextspace,
} from "./contextspaceApi.js";
import {
  applyDraft,
  discardDraft,
  fetchPendingDraft,
  FileDraft,
  sendFileChat,
  interruptFileChat,
  newClientTurnId,
  streamTurnEvents,
  type FileChatUpdate,
} from "./fileChat.js";
import { DocEditor } from "./docEditor.js";
import { ContextspaceFileBrowser } from "./contextspaceFileBrowser.js";
import { createDocChat, type ChatState } from "./docChatCore.js";
import { initChatPasteUpload } from "./chatUploads.js";
import { initDocChatVoice } from "./docChatVoice.js";
import { renderDiff } from "./diffRenderer.js";
import { createSmartRefresh, type SmartRefreshReason } from "./smartRefresh.js";
import { subscribe } from "./bus.js";
import { isRepoHealthy } from "./health.js";
import { loadPendingTurn, savePendingTurn, clearPendingTurn } from "./turnResume.js";
import { resumeFileChatTurn } from "./turnEvents.js";

type WorkspaceTarget = {
  path: string; // relative to workspace dir
  isPinned: boolean;
};

interface WorkspaceState {
  target: WorkspaceTarget | null;
  content: string;
  draft: FileDraft | null;
  loading: boolean;
  hasTickets: boolean;
  files: ContextspaceNode[];
  docEditor: DocEditor | null;
  browser: ContextspaceFileBrowser | null;
}

const state: WorkspaceState = {
  target: null,
  content: "",
  draft: null,
  loading: false,
  hasTickets: true,
  files: [],
  docEditor: null,
  browser: null,
};

const CONTEXTSPACE_CHAT_EVENT_LIMIT = 8;
const CONTEXTSPACE_CHAT_EVENT_MAX = 50;
const CONTEXTSPACE_PENDING_KEY = "car.contextspace.pendingTurn";

type WorkspaceTreePayload = {
  tree: ContextspaceNode[];
  defaultPath?: string | null;
};

type WorkspaceContentPayload = {
  path: string;
  content: string;
};

const workspaceChat = createDocChat({
  idPrefix: "contextspace-chat",
  storage: { keyPrefix: "car-contextspace-chat-", maxMessages: 50, version: 1 },
  limits: { eventVisible: CONTEXTSPACE_CHAT_EVENT_LIMIT, eventMax: CONTEXTSPACE_CHAT_EVENT_MAX },
  styling: {
    eventClass: "doc-chat-event",
    eventTitleClass: "doc-chat-event-title",
    eventSummaryClass: "doc-chat-event-summary",
    eventDetailClass: "doc-chat-event-detail",
    eventMetaClass: "doc-chat-event-meta",
    eventsEmptyClass: "doc-chat-events-empty",
    eventsHiddenClass: "hidden",
    messagesClass: "doc-chat-message",
    messageRoleClass: "doc-chat-message-role",
    messageContentClass: "doc-chat-message-content",
    messageMetaClass: "doc-chat-message-meta",
    messageUserClass: "user",
    messageAssistantClass: "assistant",
    messageAssistantThinkingClass: "streaming",
    messageAssistantFinalClass: "final",
  },
});

const CONTEXTSPACE_DOC_KINDS = new Set<ContextspaceKind>(["active_context", "decisions", "spec"]);
const CONTEXTSPACE_REFRESH_REASONS: SmartRefreshReason[] = ["initial", "background", "manual"];
let workspaceRefreshCount = 0;
let currentTurnEventsController: AbortController | null = null;

function hashString(value: string): string {
  let hash = 5381;
  for (let i = 0; i < value.length; i += 1) {
    hash = (hash * 33) ^ value.charCodeAt(i);
  }
  return (hash >>> 0).toString(36);
}

function workspaceTreeSignature(nodes: ContextspaceNode[]): string {
  const parts: string[] = [];
  const walk = (list: ContextspaceNode[]): void => {
    list.forEach((node) => {
      parts.push(
        [
          node.path || "",
          node.type || "",
          node.is_pinned ? "1" : "0",
          node.modified_at || "",
          node.size ?? "",
        ].join("|")
      );
      if (node.children?.length) walk(node.children);
    });
  };
  walk(nodes || []);
  return parts.join("::");
}

function els() {
  return {
    fileList: document.getElementById("contextspace-file-list") as HTMLElement | null,
    fileSelect: document.getElementById("contextspace-file-select") as HTMLSelectElement | null,
    breadcrumbs: document.getElementById("contextspace-breadcrumbs") as HTMLElement | null,
    status: document.getElementById("contextspace-status"),
    statusMobile: document.getElementById("contextspace-status-mobile"),
    uploadBtn: document.getElementById("contextspace-upload") as HTMLButtonElement | null,
    uploadInput: document.getElementById("contextspace-upload-input") as HTMLInputElement | null,
    mobileMenuToggle: document.getElementById("contextspace-mobile-menu-toggle") as HTMLButtonElement | null,
    mobileDropdown: document.getElementById("contextspace-mobile-dropdown") as HTMLElement | null,
    mobileUpload: document.getElementById("contextspace-mobile-upload") as HTMLButtonElement | null,
    mobileNewFolder: document.getElementById("contextspace-mobile-new-folder") as HTMLButtonElement | null,
    mobileNewFile: document.getElementById("contextspace-mobile-new-file") as HTMLButtonElement | null,
    mobileDownload: document.getElementById("contextspace-mobile-download") as HTMLButtonElement | null,
    mobileGenerate: document.getElementById("contextspace-mobile-generate") as HTMLButtonElement | null,
    newFolderBtn: document.getElementById("contextspace-new-folder") as HTMLButtonElement | null,
    newFileBtn: document.getElementById("contextspace-new-file") as HTMLButtonElement | null,
    downloadAllBtn: document.getElementById("contextspace-download-all") as HTMLButtonElement | null,
    generateBtn: document.getElementById("contextspace-generate-tickets") as HTMLButtonElement | null,
    textarea: document.getElementById("contextspace-content") as HTMLTextAreaElement | null,
    saveBtn: document.getElementById("contextspace-save") as HTMLButtonElement | null,
    saveBtnMobile: document.getElementById("contextspace-save-mobile") as HTMLButtonElement | null,
    reloadBtn: document.getElementById("contextspace-reload") as HTMLButtonElement | null,
    reloadBtnMobile: document.getElementById("contextspace-reload-mobile") as HTMLButtonElement | null,
    patchMain: document.getElementById("contextspace-patch-main") as HTMLElement | null,
    patchBody: document.getElementById("contextspace-patch-body") as HTMLElement | null,
    patchSummary: document.getElementById("contextspace-patch-summary") as HTMLElement | null,
    patchMeta: document.getElementById("contextspace-patch-meta") as HTMLElement | null,
    patchApply: document.getElementById("contextspace-patch-apply") as HTMLButtonElement | null,
    patchReload: document.getElementById("contextspace-patch-reload") as HTMLButtonElement | null,
    patchDiscard: document.getElementById("contextspace-patch-discard") as HTMLButtonElement | null,
    chatInput: document.getElementById("contextspace-chat-input") as HTMLTextAreaElement | null,
    chatSend: document.getElementById("contextspace-chat-send") as HTMLButtonElement | null,
    chatCancel: document.getElementById("contextspace-chat-cancel") as HTMLButtonElement | null,
    chatNewThread: document.getElementById("contextspace-chat-new-thread") as HTMLButtonElement | null,
    chatStatus: document.getElementById("contextspace-chat-status") as HTMLElement | null,
    chatError: document.getElementById("contextspace-chat-error") as HTMLElement | null,
    chatMessages: document.getElementById("contextspace-chat-history") as HTMLElement | null,
    chatEvents: document.getElementById("contextspace-chat-events") as HTMLElement | null,
    chatEventsList: document.getElementById("contextspace-chat-events-list") as HTMLElement | null,
    chatEventsToggle: document.getElementById("contextspace-chat-events-toggle") as HTMLButtonElement | null,
    agentSelect: document.getElementById("contextspace-chat-agent-select") as HTMLSelectElement | null,
    modelSelect: document.getElementById("contextspace-chat-model-select") as HTMLSelectElement | null,
    reasoningSelect: document.getElementById("contextspace-chat-reasoning-select") as HTMLSelectElement | null,
    createModal: document.getElementById("contextspace-create-modal") as HTMLElement | null,
    createTitle: document.getElementById("contextspace-create-title") as HTMLElement | null,
    createInput: document.getElementById("contextspace-create-name") as HTMLInputElement | null,
    createHint: document.getElementById("contextspace-create-hint") as HTMLElement | null,
    createPath: document.getElementById("contextspace-create-path") as HTMLSelectElement | null,
    createClose: document.getElementById("contextspace-create-close") as HTMLButtonElement | null,
    createCancel: document.getElementById("contextspace-create-cancel") as HTMLButtonElement | null,
    createSubmit: document.getElementById("contextspace-create-submit") as HTMLButtonElement | null,
  };
}

function workspaceKindFromPath(path: string): ContextspaceKind | null {
  const normalized = (path || "").replace(/\\/g, "/").trim();
  if (!normalized) return null;
  const baseName = normalized.split("/").pop() || normalized;
  const match = baseName.match(/^([a-z_]+)\.md$/i);
  const kind = match ? match[1].toLowerCase() : "";
  if (CONTEXTSPACE_DOC_KINDS.has(kind as ContextspaceKind)) {
    return kind as ContextspaceKind;
  }
  return null;
}

async function readWorkspaceContent(path: string): Promise<string> {
  const kind = workspaceKindFromPath(path);
  if (kind) {
    const res = await fetchContextspace();
    return (res[kind] as string) || "";
  }
  return (await api(`/api/contextspace/file?path=${encodeURIComponent(path)}`)) as string;
}

async function writeContextspaceContent(path: string, content: string): Promise<string> {
  const kind = workspaceKindFromPath(path);
  if (kind) {
    try {
      const res = await writeContextspace(kind, content);
      return (res[kind] as string) || "";
    } catch (err) {
      const msg = (err as Error).message || "";
      if (!msg.toLowerCase().includes("invalid contextspace doc kind")) {
        throw err;
      }
      // Fallback to generic file write in case detection misfires
    }
  }
  return (await api(`/api/contextspace/file?path=${encodeURIComponent(path)}`, {
    method: "PUT",
    body: { content },
  })) as string;
}

function target(): string {
  if (!state.target) return "contextspace:active_context";
  return `contextspace:${state.target.path}`;
}

function contextspaceThreadKey(path: string): string {
  return `file_chat.contextspace_${(path || "").replace(/\//g, "_")}`;
}

function setStatus(text: string): void {
  const { status, statusMobile } = els();
  if (status) status.textContent = text;
  if (statusMobile) statusMobile.textContent = text;
}

function setWorkspaceRefreshing(active: boolean): void {
  const { reloadBtn, reloadBtnMobile } = els();
  workspaceRefreshCount = Math.max(0, workspaceRefreshCount + (active ? 1 : -1));
  const isRefreshing = workspaceRefreshCount > 0;
  setButtonLoading(reloadBtn, isRefreshing);
  setButtonLoading(reloadBtnMobile, isRefreshing);
}

function renderPatch(): void {
  const { patchMain, patchBody, patchSummary, patchMeta, textarea, saveBtn, reloadBtn } = els();
  if (!patchMain || !patchBody) return;
  const draft = state.draft;
  if (draft) {
    patchMain.classList.remove("hidden");
    patchMain.classList.toggle("stale", Boolean(draft.is_stale));
    renderDiff(draft.patch || "(no diff)", patchBody);
    if (patchSummary) {
      patchSummary.textContent = draft.is_stale
        ? "Stale draft — file changed since this draft was created."
        : draft.agent_message || "Changes ready";
      patchSummary.classList.toggle("warn", Boolean(draft.is_stale));
    }
    if (patchMeta) {
      const created = draft.created_at || "";
      patchMeta.textContent = draft.is_stale
        ? `${created} · base ${draft.base_hash || ""} vs current ${draft.current_hash || ""}`.trim()
        : created;
    }
    if (textarea) {
      textarea.classList.add("hidden");
      textarea.disabled = true;
    }
    const patchApply = els().patchApply;
    if (patchApply) patchApply.textContent = draft.is_stale ? "Force Apply" : "Apply Draft";
    saveBtn?.setAttribute("disabled", "true");
    reloadBtn?.setAttribute("disabled", "true");
  } else {
    patchMain.classList.add("hidden");
    if (textarea) {
      textarea.classList.remove("hidden");
      textarea.disabled = false;
    }
    saveBtn?.removeAttribute("disabled");
    reloadBtn?.removeAttribute("disabled");
  }
}

function renderChat(): void {
  workspaceChat.render();
}

function closeMobileMenu(): void {
  const dropdown = els().mobileDropdown;
  if (dropdown) dropdown.classList.add("hidden");
}

function toggleMobileMenu(): void {
  const dropdown = els().mobileDropdown;
  if (dropdown) dropdown.classList.toggle("hidden");
}

function updateDownloadButton(): void {
  const { downloadAllBtn, mobileDownload } = els();
  const currentPath = state.browser?.getCurrentPath() || "";
  const isRoot = !currentPath;
  const folderName = currentPath.split("/").pop() || "";

  const download = () => downloadContextspaceZip(isRoot ? undefined : currentPath);

  if (downloadAllBtn) {
    downloadAllBtn.title = isRoot ? "Download all as ZIP" : `Download ${folderName}/ as ZIP`;
    downloadAllBtn.onclick = download;
  }
  if (mobileDownload) {
    mobileDownload.textContent = isRoot ? "Download ZIP (all)" : `Download ${folderName || "folder"}`
    mobileDownload.onclick = () => {
      closeMobileMenu();
      download();
    };
  }
}


type CreateMode = "folder" | "file";
let createMode: CreateMode | null = null;

function listFolderPaths(nodes: ContextspaceNode[], base = ""): string[] {
  const paths: string[] = [];
  nodes.forEach((node) => {
    if (node.type !== "folder") return;
    const current = base ? `${base}/${node.name}` : node.name;
    paths.push(current);
    if (node.children?.length) {
      paths.push(...listFolderPaths(node.children, current));
    }
  });
  return paths;
}

function openCreateModal(mode: CreateMode): void {
  const { createModal, createTitle, createInput, createHint, createPath } = els();
  if (!createModal || !createInput || !createTitle || !createHint || !createPath) return;
  createMode = mode;
  createTitle.textContent = mode === "folder" ? "New Folder" : "New Markdown File";
  createInput.value = "";
  createInput.placeholder = mode === "folder" ? "folder-name" : "note.md";
  createHint.textContent =
    mode === "folder"
      ? "Folder will be created under the current path"
      : "File will be created under the current path ('.md' appended if missing)";
  // Populate location selector with root + folders
  createPath.innerHTML = "";
  const rootOption = document.createElement("option");
  rootOption.value = "";
  rootOption.textContent = "Contextspace (root)";
  createPath.appendChild(rootOption);
  const folders = listFolderPaths(state.files);
  folders.forEach((path) => {
    const opt = document.createElement("option");
    opt.value = path;
    opt.textContent = path;
    createPath.appendChild(opt);
  });
  const currentPath = state.browser?.getCurrentPath() || "";
  createPath.value = currentPath;
  if (createPath.value !== currentPath) {
    createPath.value = "";
  }
  createModal.hidden = false;
  setTimeout(() => createInput.focus(), 10);
}

function closeCreateModal(): void {
  const { createModal } = els();
  createMode = null;
  if (createModal) createModal.hidden = true;
}

async function handleCreateSubmit(): Promise<void> {
  const { createInput, createPath } = els();
  if (!createMode || !createInput || !createPath) return;
  const rawName = (createInput.value || "").trim();
  if (!rawName) {
    flash("Name is required", "error");
    return;
  }
  const base = createPath.value ?? state.browser?.getCurrentPath() ?? "";
  const name = createMode === "file" && !rawName.toLowerCase().endsWith(".md") ? `${rawName}.md` : rawName;
  const path = base ? `${base}/${name}` : name;
  try {
    if (createMode === "folder") {
      await createContextspaceFolder(path);
      flash("Folder created", "success");
    } else {
      await writeContextspaceContent(path, "");
      flash("File created", "success");
    }
    closeCreateModal();
    await loadFiles(createMode === "file" ? path : state.target?.path || undefined, "manual");
    if (createMode === "file") {
      state.browser?.select(path);
    }
  } catch (err) {
    flash((err as Error).message || "Failed to create item", "error");
  }
}

const workspaceTreeRefresh = createSmartRefresh<WorkspaceTreePayload>({
  getSignature: (payload) => workspaceTreeSignature(payload.tree || []),
  render: (payload) => {
    state.files = payload.tree;
    const { fileList, fileSelect, breadcrumbs } = els();
    if (!fileList) return;

    if (!state.browser) {
      state.browser = new ContextspaceFileBrowser({
        container: fileList,
        selectEl: fileSelect,
        breadcrumbsEl: breadcrumbs,
        onSelect: (file) => {
          state.target = { path: file.path, isPinned: Boolean(file.is_pinned) };
          workspaceChat.setTarget(target());
          void refreshWorkspaceFile(file.path, "manual");
        },
        onPathChange: () => updateDownloadButton(),
        onRefresh: () => loadFiles(state.target?.path, "manual"),
        onConfirm: (message) =>
          (window as unknown as { contextspaceConfirm?: (msg: string) => Promise<boolean> }).contextspaceConfirm?.(
            message
          ) ?? confirmModal(message),
      });
    }

    const defaultPath = payload.defaultPath ?? state.target?.path ?? undefined;
    state.browser.setTree(payload.tree, defaultPath || undefined);
    updateDownloadButton();
    if (state.target) {
      workspaceChat.setTarget(target());
    }
  },
  onSkip: () => {
    updateDownloadButton();
  },
});

const workspaceContentRefresh = createSmartRefresh<WorkspaceContentPayload>({
  getSignature: (payload) => `${payload.path}::${hashString(payload.content || "")}`,
  render: async (payload, ctx) => {
    if (payload.path !== state.target?.path) return;
    state.content = payload.content as string;
    if (state.docEditor) {
      state.docEditor.destroy();
    }
    const { textarea, saveBtn, status } = els();
    if (!textarea) return;
    state.docEditor = new DocEditor({
      target: target(),
      textarea,
      saveButton: saveBtn,
      statusEl: status,
      onLoad: async () => payload.content as string,
      onSave: async (content) => {
        const saved = await writeContextspaceContent(payload.path, content);
        state.content = saved;
        if (saved !== content) {
          textarea.value = saved;
        }
      },
    });
    await loadPendingDraft();
    renderPatch();
    if (ctx.reason !== "background") {
      setStatus("Loaded");
    }
  },
});

async function refreshWorkspaceFile(path: string, reason: SmartRefreshReason = "manual"): Promise<void> {
  if (!CONTEXTSPACE_REFRESH_REASONS.includes(reason)) {
    reason = "manual";
  }
  const isInitial = reason === "initial";
  if (isInitial) {
    state.loading = true;
    setStatus("Loading…");
  } else {
    setWorkspaceRefreshing(true);
  }
  try {
    await workspaceContentRefresh.refresh(
      async () => ({ path, content: await readWorkspaceContent(path) }),
      { reason }
    );
  } catch (err) {
    const message = (err as Error).message || "Failed to load contextspace file";
    flash(message, "error");
    setStatus(message);
  } finally {
    state.loading = false;
    if (!isInitial) {
      setWorkspaceRefreshing(false);
    }
  }
}

async function loadPendingDraft(): Promise<void> {
  state.draft = await fetchPendingDraft(target());
  renderPatch();
}

async function reloadWorkspace(): Promise<void> {
  if (!state.target) return;
  await refreshWorkspaceFile(state.target.path, "manual");
}

async function maybeShowGenerate(): Promise<void> {
  try {
    const res = await listTickets();
    const tickets = Array.isArray((res as { tickets?: unknown[] }).tickets)
      ? ((res as { tickets?: unknown[] }).tickets as unknown[])
      : [];
    state.hasTickets = tickets.length > 0;
  } catch {
    state.hasTickets = true;
  }
  const { generateBtn, mobileGenerate } = els();
  const hidden = state.hasTickets;
  if (generateBtn) generateBtn.classList.toggle("hidden", hidden);
  if (mobileGenerate) mobileGenerate.classList.toggle("hidden", hidden);
}

async function generateTickets(): Promise<void> {
  try {
    const res = await ingestSpecToTickets();
    flash(
      res.created > 0
        ? `Created ${res.created} ticket${res.created === 1 ? "" : "s"}`
        : "No tickets created",
      "success"
    );
    await maybeShowGenerate();
  } catch (err) {
    flash((err as Error).message || "Failed to generate tickets", "error");
  }
}

async function applyWorkspaceDraft(): Promise<void> {
  try {
    const isStale = Boolean(state.draft?.is_stale);
    if (isStale) {
      const confirmForce = await confirmModal(
        "This draft is stale because the file changed after it was created. Force apply anyway?"
      );
      if (!confirmForce) return;
    }
    const res = await applyDraft(target(), { force: isStale });
    const textarea = els().textarea;
    if (textarea) {
      textarea.value = res.content || "";
    }
    state.content = res.content || "";
    state.draft = null;
    renderPatch();
    flash(res.agent_message || "Draft applied", "success");
  } catch (err) {
    flash((err as Error).message || "Failed to apply draft", "error");
  }
}

async function discardWorkspaceDraft(): Promise<void> {
  try {
    const res = await discardDraft(target());
    const textarea = els().textarea;
    if (textarea) textarea.value = res.content || "";
    state.content = res.content || "";
    state.draft = null;
    renderPatch();
    flash("Draft discarded", "success");
  } catch (err) {
    flash((err as Error).message || "Failed to discard draft", "error");
  }
}

function clearTurnEventsStream(): void {
  if (currentTurnEventsController) {
    try {
      currentTurnEventsController.abort();
    } catch {
      // ignore
    }
    currentTurnEventsController = null;
  }
}

function clearPendingTurnState(): void {
  clearTurnEventsStream();
  clearPendingTurn(CONTEXTSPACE_PENDING_KEY);
}

function maybeStartTurnEventsFromUpdate(update: FileChatUpdate): void {
  const meta = update as Record<string, unknown>;
  const threadId = typeof meta.thread_id === "string" ? meta.thread_id : "";
  const turnId = typeof meta.turn_id === "string" ? meta.turn_id : "";
  const agent = typeof meta.agent === "string" ? meta.agent : undefined;
  if (!threadId || !turnId) return;
  clearTurnEventsStream();
  currentTurnEventsController = streamTurnEvents(
    { agent, threadId, turnId },
    {
      onEvent: (event) => {
        workspaceChat.applyAppEvent(event);
        workspaceChat.renderEvents();
        workspaceChat.render();
      },
    }
  );
}

function applyChatUpdate(update: FileChatUpdate): void {
  const hasDraft =
    (update.has_draft as boolean | undefined) ?? (update.hasDraft as boolean | undefined);
  if (hasDraft === false) {
    state.draft = null;
    if (typeof update.content === "string") {
      state.content = update.content as string;
      const textarea = els().textarea;
      if (textarea) textarea.value = state.content;
    }
    renderPatch();
  } else if (hasDraft === true || update.patch || update.content) {
    state.draft = {
      target: target(),
      content: (update.content as string) || "",
      patch: (update.patch as string) || "",
      agent_message: update.agent_message,
      created_at: update.created_at,
      base_hash: update.base_hash,
      current_hash: update.current_hash,
      is_stale: Boolean(update.is_stale),
    };
    renderPatch();
  }
  if (update.message || update.agent_message) {
    const text = (update.message as string) || (update.agent_message as string) || "";
    if (text) workspaceChat.addAssistantMessage(text);
  }
  workspaceChat.render();
}

function applyFinalResult(result: Record<string, unknown>): void {
  const chatState = workspaceChat.state as ChatState;
  const status = String(result.status || "");
  if (status === "ok") {
    applyChatUpdate(result as FileChatUpdate);
    chatState.status = "done";
    chatState.error = "";
    chatState.streamText = "";
    clearPendingTurnState();
    renderChat();
    return;
  }
  if (status === "error") {
    const detail = String(result.detail || "Chat failed");
    chatState.status = "error";
    chatState.error = detail;
    renderChat();
    flash(detail, "error");
    clearPendingTurnState();
    return;
  }
  if (status === "interrupted") {
    chatState.status = "interrupted";
    chatState.error = "";
    chatState.streamText = "";
    renderChat();
    clearPendingTurnState();
  }
}

async function resumePendingWorkspaceTurn(): Promise<void> {
  const pending = loadPendingTurn(CONTEXTSPACE_PENDING_KEY);
  if (!pending) return;
  const chatState = workspaceChat.state as ChatState;
  chatState.status = "running";
  chatState.statusText = "Recovering previous turn…";
  workspaceChat.render();
  workspaceChat.renderMessages();

  try {
    const outcome = await resumeFileChatTurn(pending.clientTurnId, {
      onEvent: (event) => {
        workspaceChat.applyAppEvent(event);
        workspaceChat.renderEvents();
        workspaceChat.render();
      },
      onResult: (result) => applyFinalResult(result as Record<string, unknown>),
      onError: (msg) => {
        chatState.statusText = msg;
        renderChat();
      },
    });
    currentTurnEventsController = outcome.controller;
    if (outcome.lastResult && (outcome.lastResult as Record<string, unknown>).status) {
      applyFinalResult(outcome.lastResult as Record<string, unknown>);
      return;
    }
    // If still running but no event stream yet, poll again shortly.
    if (!outcome.controller) {
      window.setTimeout(() => {
        void resumePendingWorkspaceTurn();
      }, 1000);
    }
  } catch (err) {
    const msg = (err as Error).message || "Failed to resume turn";
    chatState.statusText = msg;
    renderChat();
  }
}

async function sendChat(): Promise<void> {
  const { chatInput, chatSend, chatCancel } = els();
  const message = (chatInput?.value || "").trim();
  if (!message) return;

  const chatState = workspaceChat.state as ChatState;

  // Abort any in-flight chat first
  if (chatState.controller) chatState.controller.abort();

  chatState.controller = new AbortController();
  chatState.status = "running";
  chatState.error = "";
  chatState.statusText = "queued";
  chatState.streamText = "";
  chatState.contextUsagePercent = null;
  workspaceChat.clearEvents();
  workspaceChat.addUserMessage(message);
  renderChat();
  if (chatInput) chatInput.value = "";
  chatSend?.setAttribute("disabled", "true");
  chatCancel?.classList.remove("hidden");
  clearTurnEventsStream();

  const clientTurnId = newClientTurnId("contextspace");
  savePendingTurn(CONTEXTSPACE_PENDING_KEY, {
    clientTurnId,
    message,
    startedAtMs: Date.now(),
    target: target(),
  });

  const agent = getSelectedAgent();
  const model = getSelectedModel(agent) || undefined;
  const reasoning = getSelectedReasoning(agent) || undefined;

  try {
    await sendFileChat(
      target(),
      message,
      chatState.controller,
      {
        onStatus: (status) => {
          chatState.statusText = status;
          setStatus(status || "Running…");
          renderChat();
        },
        onToken: (token) => {
          chatState.streamText = (chatState.streamText || "") + token;
          workspaceChat.renderMessages();
        },
        onEvent: (event) => {
          workspaceChat.applyAppEvent(event);
          workspaceChat.renderEvents();
        },
        onTokenUsage: (percent) => {
          chatState.contextUsagePercent = percent;
          renderChat();
        },
        onUpdate: (update) => {
          applyChatUpdate(update);
          maybeStartTurnEventsFromUpdate(update);
        },
        onError: (msg) => {
          chatState.status = "error";
          chatState.error = msg;
          renderChat();
          flash(msg, "error");
          clearPendingTurnState();
        },
        onInterrupted: (msg) => {
          chatState.status = "interrupted";
          chatState.error = "";
          chatState.streamText = "";
          renderChat();
          flash(msg, "info");
          clearPendingTurnState();
        },
        onDone: () => {
          if (chatState.streamText) {
            workspaceChat.addAssistantMessage(chatState.streamText);
            chatState.streamText = "";
          }
          chatState.status = "done";
          renderChat();
          clearPendingTurnState();
        },
      },
      { agent, model, reasoning, clientTurnId }
    );
  } catch (err) {
    const msg = (err as Error).message || "Chat failed";
    const chatStateLocal = workspaceChat.state as ChatState;
    chatStateLocal.status = "error";
    chatStateLocal.error = msg;
    renderChat();
    flash(msg, "error");
    clearPendingTurnState();
  } finally {
    chatSend?.removeAttribute("disabled");
    chatCancel?.classList.add("hidden");
    const chatStateLocal = workspaceChat.state as ChatState;
    chatStateLocal.controller = null;
  }
}

async function cancelChat(): Promise<void> {
  const chatState = workspaceChat.state as ChatState;
  if (chatState.controller) {
    chatState.controller.abort();
  }
  try {
    await interruptFileChat(target());
  } catch {
    // ignore
  }
  chatState.status = "interrupted";
  chatState.streamText = "";
  chatState.contextUsagePercent = null;
  renderChat();
  clearPendingTurnState();
}

async function resetThread(): Promise<void> {
  if (!state.target) return;
  try {
    await api("/api/app-server/threads/reset", {
      method: "POST",
      body: { key: contextspaceThreadKey(state.target.path) },
    });
    const chatState = workspaceChat.state as ChatState;
    chatState.messages = [];
    chatState.streamText = "";
    chatState.contextUsagePercent = null;
    workspaceChat.clearEvents();
    clearPendingTurnState();
    renderChat();
    flash("New contextspace chat thread", "success");
  } catch (err) {
    flash((err as Error).message || "Failed to reset thread", "error");
  }
}

async function loadFiles(defaultPath?: string, reason: SmartRefreshReason = "manual"): Promise<void> {
  if (!CONTEXTSPACE_REFRESH_REASONS.includes(reason)) {
    reason = "manual";
  }
  const isInitial = reason === "initial";
  if (!isInitial) {
    setWorkspaceRefreshing(true);
  }
  try {
    await workspaceTreeRefresh.refresh(
      async () => ({ tree: await fetchContextspaceTree(), defaultPath }),
      { reason }
    );
  } finally {
    if (!isInitial) {
      setWorkspaceRefreshing(false);
    }
  }
}

export async function initContextspace(): Promise<void> {
  const {
    generateBtn,
    uploadBtn,
    uploadInput,
    mobileMenuToggle,
    mobileDropdown,
    mobileUpload,
    mobileNewFolder,
    mobileNewFile,
    mobileDownload,
    mobileGenerate,
    newFolderBtn,
    saveBtn,
    saveBtnMobile,
    reloadBtn,
    reloadBtnMobile,
    patchApply,
    patchDiscard,
    patchReload,
    chatSend,
    chatCancel,
    chatNewThread,
  } = els();

  if (!document.getElementById("contextspace")) return;

  initAgentControls({
    agentSelect: els().agentSelect,
    modelSelect: els().modelSelect,
    reasoningSelect: els().reasoningSelect,
  });
  await initDocChatVoice({
    buttonId: "contextspace-chat-voice",
    inputId: "contextspace-chat-input",
  });

  await maybeShowGenerate();
  await loadFiles(undefined, "initial");
  workspaceChat.setTarget(target());
  void resumePendingWorkspaceTurn();

  const reloadEverything = async () => {
    await loadFiles(state.target?.path, "manual");
    await reloadWorkspace();
  };

  saveBtn?.addEventListener("click", () => void state.docEditor?.save(true));
  saveBtnMobile?.addEventListener("click", () => void state.docEditor?.save(true));
  reloadBtn?.addEventListener("click", () => void reloadEverything());
  reloadBtnMobile?.addEventListener("click", () => void reloadEverything());

  uploadBtn?.addEventListener("click", () => uploadInput?.click());
  uploadInput?.addEventListener("change", async () => {
    const files = uploadInput.files;
    if (!files || !files.length) return;
    const subdir = state.browser?.getCurrentPath() || "";
    try {
      await uploadContextspaceFiles(files, subdir || undefined);
      flash(`Uploaded ${files.length} file${files.length === 1 ? "" : "s"}`, "success");
      await loadFiles(state.target?.path, "manual");
    } catch (err) {
      flash((err as Error).message || "Upload failed", "error");
    } finally {
      uploadInput.value = "";
    }
  });

  // Mobile action sheet
  const handleMobileToggle = (evt: Event) => {
    evt.preventDefault();
    evt.stopPropagation();
    toggleMobileMenu();
  };

  mobileMenuToggle?.addEventListener("pointerdown", handleMobileToggle);
  mobileMenuToggle?.addEventListener("click", (evt) => {
    evt.preventDefault(); // swallow synthetic click after pointerdown
  });
  mobileMenuToggle?.addEventListener("keydown", (evt) => {
    if (evt.key === "Enter" || evt.key === " ") {
      handleMobileToggle(evt);
    }
  });

  document.addEventListener("pointerdown", (evt) => {
    if (!mobileDropdown || mobileDropdown.classList.contains("hidden")) return;
    if (evt.target instanceof Node && mobileDropdown.contains(evt.target)) return;
    closeMobileMenu();
  });

  document.addEventListener("keydown", (evt) => {
    if (evt.key === "Escape" && mobileDropdown && !mobileDropdown.classList.contains("hidden")) {
      closeMobileMenu();
    }
  });

  mobileUpload?.addEventListener("click", () => {
    closeMobileMenu();
    uploadInput?.click();
  });
  mobileNewFolder?.addEventListener("click", () => {
    closeMobileMenu();
    openCreateModal("folder");
  });
  mobileNewFile?.addEventListener("click", () => {
    closeMobileMenu();
    openCreateModal("file");
  });
  mobileDownload?.addEventListener("click", () => {
    closeMobileMenu();
    const currentPath = state.browser?.getCurrentPath() || "";
    downloadContextspaceZip(currentPath || undefined);
  });
  mobileGenerate?.addEventListener("click", () => {
    closeMobileMenu();
    void generateTickets();
  });

  newFolderBtn?.addEventListener("click", () => openCreateModal("folder"));
  els().newFileBtn?.addEventListener("click", () => openCreateModal("file"));

  generateBtn?.addEventListener("click", () => void generateTickets());
  patchApply?.addEventListener("click", () => void applyWorkspaceDraft());
  patchDiscard?.addEventListener("click", () => void discardWorkspaceDraft());
  patchReload?.addEventListener("click", () => void loadPendingDraft());
  chatSend?.addEventListener("click", () => void sendChat());
  chatCancel?.addEventListener("click", () => void cancelChat());
  chatNewThread?.addEventListener("click", () => void resetThread());
  const chatInput = els().chatInput;
  if (chatInput) {
    chatInput.addEventListener("keydown", (evt) => {
      if ((evt.metaKey || evt.ctrlKey) && evt.key === "Enter") {
        evt.preventDefault();
        void sendChat();
      }
    });

    initChatPasteUpload({
      textarea: chatInput,
      basePath: "/api/filebox",
      box: "inbox",
      insertStyle: "both",
      pathPrefix: ".codex-autorunner/filebox",
    });
  }

  const { createModal, createClose, createCancel, createSubmit } = els();
  createClose?.addEventListener("click", () => closeCreateModal());
  createCancel?.addEventListener("click", () => closeCreateModal());
  createSubmit?.addEventListener("click", () => void handleCreateSubmit());
  els().createInput?.addEventListener("keydown", (evt) => {
    if (evt.key === "Enter") {
      evt.preventDefault();
      void handleCreateSubmit();
    }
  });
  createModal?.addEventListener("click", (evt) => {
    if (evt.target === createModal) closeCreateModal();
  });
  document.addEventListener("keydown", (evt) => {
    if (evt.key === "Escape" && createModal && !createModal.hidden) {
      closeCreateModal();
    }
  });

  // Confirm modal wiring
  const confirmModal = document.getElementById("contextspace-confirm-modal");
  const confirmText = document.getElementById("contextspace-confirm-text");
  const confirmYes = document.getElementById("contextspace-confirm-yes") as HTMLButtonElement | null;
  const confirmCancel = document.getElementById("contextspace-confirm-cancel") as HTMLButtonElement | null;
  let confirmResolver: ((val: boolean) => void) | null = null;
  const closeConfirm = (result: boolean) => {
    if (confirmModal) confirmModal.hidden = true;
    confirmResolver?.(result);
    confirmResolver = null;
  };
  (window as unknown as { contextspaceConfirm?: (message: string) => Promise<boolean> }).contextspaceConfirm = (message) =>
    new Promise<boolean>((resolve) => {
      confirmResolver = resolve;
      if (confirmText) confirmText.textContent = message;
      if (confirmModal) confirmModal.hidden = false;
      confirmYes?.focus();
    });
  confirmYes?.addEventListener("click", () => closeConfirm(true));
  confirmCancel?.addEventListener("click", () => closeConfirm(false));
  confirmModal?.addEventListener("click", (evt) => {
    if (evt.target === confirmModal) closeConfirm(false);
  });

  subscribe("repo:health", (payload: unknown) => {
    const status = (payload as { status?: string } | null)?.status || "";
    if (status !== "ok" && status !== "degraded") return;
    if (!isRepoHealthy()) return;
    void loadFiles(state.target?.path, "background");
    const textarea = els().textarea;
    const hasLocalEdits = textarea ? textarea.value !== state.content : false;
    if (state.target && !state.draft && !hasLocalEdits) {
      void refreshWorkspaceFile(state.target.path, "background");
    }
  });
}
