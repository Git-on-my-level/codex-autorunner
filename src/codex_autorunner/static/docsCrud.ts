import { api, confirmModal, flash, getUrlParams, updateUrlParams } from "./utils.js";
import { loadState } from "./state.js";
import { publish } from "./bus.js";
import { renderTodoPreview } from "./todoPreview.js";
import {
  docButtons,
  docActionsUI,
  specIssueUI,
  specIngestUI,
  chatUI,
} from "./docsElements.js";
import {
  CLEARABLE_DOCS,
  DOC_TYPES,
  docsState,
  getActiveDoc,
  getChatState,
  getDraft,
  hasDraft,
  isDraftPreview,
  setActiveDoc,
  type ChatHistoryEntry,
  type DocType,
  type DocKind,
} from "./docsState.js";
import { renderChat } from "./docChatRender.js";
import { reloadPatch, refreshAllDrafts } from "./docChatActions.js";
import { applyChatResult } from "./docChatStream.js";
import { renderSnapshotButtons } from "./docsSnapshot.js";
import { renderSpecIngestPatch, reloadSpecIngestPatch } from "./docsSpecIngest.js";
import { getDocTextarea, syncDocEditor, updateDocControls } from "./docsUi.js";

export function getDocFromUrl(): DocType | null {
  const params = getUrlParams();
  const kind = params.get("doc");
  if (!kind) return null;
  if (kind === "snapshot") return kind as DocType;
  return DOC_TYPES.includes(kind as DocType) ? (kind as DocType) : null;
}

export async function loadDocs(): Promise<void> {
  try {
    const data = await api("/api/docs");
    docsState.docsCache = { ...docsState.docsCache, ...(data as Partial<typeof docsState.docsCache>) };
    setDoc(getActiveDoc());
    renderTodoPreview(docsState.docsCache.todo);
    publish("docs:loaded", docsState.docsCache);
    refreshAllDrafts().catch(() => {});
  } catch (err) {
    const error = err as Error;
    flash(error.message);
  }
}

export async function safeLoadDocs(): Promise<void> {
  if (getActiveDoc() === "snapshot") {
    return;
  }
  const activeDoc = getActiveDoc();
  const textarea = getDocTextarea();
  const draft = getDraft(activeDoc);
  const previewing = !!draft && isDraftPreview(activeDoc);
  if (textarea) {
    const currentValue = textarea.value;
    const cachedValue = previewing ? (draft as { content: string }).content : (docsState.docsCache as unknown as Record<string, string>)[activeDoc] || "";
    if (currentValue !== cachedValue) {
      return;
    }
  }
  const state = getChatState();
  if (state.status === "running") {
    return;
  }
  try {
    const data = await api("/api/docs");
    if (
      textarea &&
      textarea.value !== (previewing ? (draft as { content: string }).content : (docsState.docsCache as unknown as Record<string, string>)[activeDoc] || "")
    ) {
      return;
    }
    docsState.docsCache = { ...docsState.docsCache, ...(data as Partial<typeof docsState.docsCache>) };
    setDoc(activeDoc);
    renderTodoPreview(docsState.docsCache.todo);
    publish("docs:loaded", docsState.docsCache);
  } catch (err) {
    console.error("Auto-refresh docs failed:", err);
  }
}

export function setDoc(kind: DocKind | null): void {
  setActiveDoc(kind);
  docButtons.forEach((btn) =>
    btn.classList.toggle("active", btn.dataset.doc === kind)
  );
  const isSnapshot = kind === "snapshot";

  syncDocEditor(kind, { force: true });

  if (specIssueUI.row) {
    specIssueUI.row.classList.toggle("hidden", kind !== "spec");
  }
  if (specIngestUI.panel) {
    specIngestUI.panel.classList.toggle("hidden", kind !== "spec");
  }

  if (docActionsUI.standard) {
    docActionsUI.standard.classList.toggle("hidden", isSnapshot);
  }
  if (docActionsUI.snapshot) {
    docActionsUI.snapshot.classList.toggle("hidden", !isSnapshot);
  }

  if (docActionsUI.ingest) {
    docActionsUI.ingest.classList.toggle("hidden", kind !== "spec");
  }
  if (docActionsUI.clear) {
    docActionsUI.clear.classList.toggle("hidden", !CLEARABLE_DOCS.includes(kind as any));
  }
  updateDocControls(kind);

  const chatPanel = document.querySelector(".doc-chat-panel");
  if (chatPanel) {
    chatPanel.classList.toggle("hidden", isSnapshot);
  }

  if (chatUI.patchMain) {
    if (isSnapshot) {
      chatUI.patchMain.classList.add("hidden");
    }
  }
  if (specIngestUI.patchMain) {
    if (isSnapshot) {
      specIngestUI.patchMain.classList.add("hidden");
    }
  }

  if (isSnapshot) {
    renderSnapshotButtons();
  } else {
    reloadPatch(kind as DocType, true);
    renderChat();
    if (kind === "spec") {
      reloadSpecIngestPatch(true);
    } else {
      renderSpecIngestPatch();
    }
  }

  updateUrlParams({ doc: kind });
}

export async function importIssueToSpec(): Promise<void> {
  if (!specIssueUI.input || !specIssueUI.button) return;
  const issue = (specIssueUI.input.value || "").trim();
  if (!issue) {
    flash("Enter a GitHub issue number or URL", "error");
    return;
  }
  const state = getChatState();
  if (state.status === "running") {
    flash("SPEC chat is running; try again shortly", "error");
    return;
  }

  specIssueUI.button.disabled = true;
  specIssueUI.button.classList.add("loading");
  try {
    const entry = {
      id: `${Date.now()}`,
      prompt: `Import issue → SPEC: ${issue}`,
      targets: ["spec"],
      response: "",
      status: "running",
      time: String(Date.now()),
      drafts: {},
      updated: [],
    } as ChatHistoryEntry;
    state.history.unshift(entry);
    state.status = "running";
    state.error = "";
    state.streamText = "";
    state.statusText = "importing issue";
    renderChat();

    const res = await api("/api/github/spec/from-issue", {
      method: "POST",
      body: { issue },
    });
    applyChatResult(res, state, entry);
    entry.status = "done";
    state.status = "idle";
    if (specIssueUI.inputRow) {
      specIssueUI.inputRow.classList.add("hidden");
    }
    if (specIssueUI.toggle) {
      specIssueUI.toggle.textContent = "Import Issue → SPEC";
    }
    if (specIssueUI.input) {
      specIssueUI.input.value = "";
    }
    flash("Imported issue into pending SPEC draft");
  } catch (err) {
    const error = err as Error;
    const message = error?.message || "Issue import failed";
    const entry = state.history[0] as ChatHistoryEntry | undefined;
    if (entry) {
      entry.status = "error";
      entry.error = message;
    }
    state.status = "idle";
    state.error = message;
    flash(message, "error");
  } finally {
    specIssueUI.button.disabled = false;
    specIssueUI.button.classList.remove("loading");
    renderChat();
  }
}

export async function saveDoc(): Promise<void> {
  if (getActiveDoc() === "snapshot") {
    flash("Snapshot is read-only. Use Generate to update.", "error");
    return;
  }
  if (hasDraft(getActiveDoc()) && isDraftPreview(getActiveDoc())) {
    flash("Exit draft preview before saving.", "error");
    return;
  }
  const textarea = document.getElementById("doc-content") as HTMLTextAreaElement | null;
  if (!textarea) return;
  const content = textarea.value;
  const saveBtn = document.getElementById("save-doc") as HTMLButtonElement | null;
  if (!saveBtn) return;
  saveBtn.disabled = true;
  saveBtn.classList.add("loading");
  try {
    await api(`/api/docs/${getActiveDoc()}`, { method: "PUT", body: { content } });
    docsState.docsCache[getActiveDoc()] = content;
    flash(`${getActiveDoc().toUpperCase()} saved`);
    publish("docs:updated", { kind: getActiveDoc(), content });
    if (getActiveDoc() === "todo") {
      renderTodoPreview(content);
      await loadState({ notify: false });
    }
  } catch (err) {
    const error = err as Error;
    flash(error.message);
  } finally {
    saveBtn.disabled = false;
    saveBtn.classList.remove("loading");
  }
}

export async function clearDocs(): Promise<void> {
  const confirmed = await confirmModal(
    "Clear TODO, PROGRESS, and OPINIONS? This action cannot be undone."
  );
  if (!confirmed) {
    flash("Clear cancelled");
    return;
  }
  const button = document.getElementById("clear-docs") as HTMLButtonElement | null;
  if (!button) return;
  button.disabled = true;
  button.classList.add("loading");
  try {
    const data = await api("/api/docs/clear", { method: "POST" });
    docsState.docsCache = { ...docsState.docsCache, ...(data as Partial<typeof docsState.docsCache>) };
    setDoc(getActiveDoc());
    renderTodoPreview(docsState.docsCache.todo);
    publish("docs:updated", { kind: "todo", content: docsState.docsCache.todo });
    publish("docs:updated", { kind: "progress", content: docsState.docsCache.progress });
    publish("docs:updated", { kind: "opinions", content: docsState.docsCache.opinions });
    flash("Cleared TODO/PROGRESS/OPINIONS");
  } catch (err) {
    const error = err as Error;
    flash(error.message, "error");
  } finally {
    button.disabled = false;
    button.classList.remove("loading");
  }
}
