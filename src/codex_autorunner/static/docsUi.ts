import { docActionsUI } from "./docsElements.js";
import {
  COPYABLE_DOCS,
  PASTEABLE_DOCS,
  docsState,
  getActiveDoc,
  getDraft,
  hasDraft,
  isDraftPreview,
  type DocKind,
} from "./docsState.js";
import type { DraftPayload } from "./docsParse.js";

export function renderDiffHtml(diffText: string): string {
  if (!diffText) return "";
  const lines = diffText.split("\n");
  let oldLineNum = 0;
  let newLineNum = 0;

  const htmlLines = lines.map((line) => {
    const escaped = line
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");

    if (line.startsWith("@@") && line.includes("@@")) {
      const match = line.match(/@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
      if (match) {
        oldLineNum = parseInt(match[1], 10);
        newLineNum = parseInt(match[2], 10);
      }
      return `<div class="diff-line diff-hunk"><span class="diff-gutter diff-gutter-hunk">···</span><span class="diff-content">${escaped}</span></div>`;
    }

    if (line.startsWith("+++") || line.startsWith("---")) {
      return `<div class="diff-line diff-file"><span class="diff-gutter"></span><span class="diff-content">${escaped}</span></div>`;
    }

    if (line.startsWith("+")) {
      const lineNum = newLineNum++;
      const content = escaped.substring(1);
      const isEmpty = content.trim() === "";
      const displayContent = isEmpty
        ? `<span class="diff-empty-marker">↵</span>`
        : content;
      return `<div class="diff-line diff-add"><span class="diff-gutter diff-gutter-add">${lineNum}</span><span class="diff-sign">+</span><span class="diff-content">${displayContent}</span></div>`;
    }

    if (line.startsWith("-")) {
      const lineNum = oldLineNum++;
      const content = escaped.substring(1);
      const isEmpty = content.trim() === "";
      const displayContent = isEmpty
        ? `<span class="diff-empty-marker">↵</span>`
        : content;
      return `<div class="diff-line diff-del"><span class="diff-gutter diff-gutter-del">${lineNum}</span><span class="diff-sign">−</span><span class="diff-content">${displayContent}</span></div>`;
    }

    if (
      line.startsWith(" ") ||
      (line.length > 0 && !line.startsWith("\\") && oldLineNum > 0)
    ) {
      const oLine = oldLineNum++;
      newLineNum += 1;
      const content = escaped.startsWith(" ") ? escaped.substring(1) : escaped;
      return `<div class="diff-line diff-ctx"><span class="diff-gutter diff-gutter-ctx">${oLine}</span><span class="diff-sign"> </span><span class="diff-content">${content}</span></div>`;
    }

    return `<div class="diff-line diff-meta"><span class="diff-gutter"></span><span class="diff-content diff-note">${escaped}</span></div>`;
  });

  return `<div class="diff-view">${htmlLines.join("")}</div>`;
}

export function autoResizeTextarea(textarea: HTMLTextAreaElement): void {
  textarea.style.height = "auto";
  textarea.style.height = textarea.scrollHeight + "px";
}

export function getDocTextarea(): HTMLTextAreaElement | null {
  return document.getElementById("doc-content") as HTMLTextAreaElement | null;
}

export function updateCopyButton(button: HTMLButtonElement | null, text: string, disabled = false): void {
  if (!button) return;
  const hasText = Boolean((text || "").trim());
  button.disabled = disabled || !hasText;
}

export function getDocCopyText(kind: DocKind = getActiveDoc()): string {
  const textarea = getDocTextarea();
  if (textarea && getActiveDoc() === kind) {
    return textarea.value || "";
  }
  if (kind === "snapshot") {
    return docsState.snapshotCache.content || "";
  }
  return docsState.docsCache[kind] || "";
}

export function updateStandardActionButtons(kind: DocKind = getActiveDoc()): void {
  if (docActionsUI.copy) {
    const canCopy = COPYABLE_DOCS.includes(kind as "spec" | "summary");
    docActionsUI.copy.classList.toggle("hidden", !canCopy);
    updateCopyButton(docActionsUI.copy, canCopy ? getDocCopyText(kind) : "");
  }
  if (docActionsUI.paste) {
    const canPaste = PASTEABLE_DOCS.includes(kind as "spec");
    docActionsUI.paste.classList.toggle("hidden", !canPaste);
  }
}

export function formatDraftTimestamp(value: string | undefined): string {
  if (!value) return "";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function updateDocVisibility(): void {
  const docContent = getDocTextarea();
  if (!docContent) return;
  const specHasPatch =
    getActiveDoc() === "spec" && !!(docsState.specIngestState.patch || "").trim();
  docContent.classList.toggle("hidden", specHasPatch);
}

export function updateDocStatus(kind: DocKind): void {
  const status = document.getElementById("doc-status");
  if (!status) return;
  if (kind === "snapshot") {
    status.textContent = docsState.snapshotBusy ? "Working…" : "Viewing SNAPSHOT";
    return;
  }
  const draft = getDraft(kind);
  if (draft && isDraftPreview(kind)) {
    status.textContent = `Previewing ${kind.toUpperCase()} draft`;
    return;
  }
  status.textContent = `Editing ${kind.toUpperCase()}`;
}

export function syncDocEditor(kind: DocKind, { force = false }: { force?: boolean } = {}): void {
  const textarea = getDocTextarea();
  if (!textarea) return;
  if (kind === "snapshot") {
    textarea.readOnly = true;
    textarea.classList.remove("doc-preview");
    textarea.value = docsState.snapshotCache.content || "";
    textarea.placeholder = "(snapshot will appear here)";
    updateDocStatus(kind);
    return;
  }
  const draft = getDraft(kind) as DraftPayload | null;
  const previewing = !!draft && isDraftPreview(kind);
  const nextValue = previewing ? (draft?.content || "") : docsState.docsCache[kind] || "";
  if (force || textarea.value !== nextValue) {
    textarea.value = nextValue;
  }
  textarea.readOnly = previewing;
  textarea.classList.toggle("doc-preview", previewing);
  textarea.placeholder = previewing ? "(draft preview)" : "";
  updateDocStatus(kind);
}

export function updateDocControls(kind: DocKind = getActiveDoc()): void {
  const saveBtn = document.getElementById("save-doc") as HTMLButtonElement | null;
  if (saveBtn) {
    const previewing = hasDraft(kind) && isDraftPreview(kind);
    saveBtn.disabled = kind === "snapshot" || previewing;
  }
  updateStandardActionButtons(kind);
}
