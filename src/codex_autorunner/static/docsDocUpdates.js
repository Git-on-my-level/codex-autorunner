import { confirmModal, flash } from "./utils.js";
import { loadState } from "./state.js";
import { publish } from "./bus.js";
import { renderTodoPreview } from "./todoPreview.js";
import {
  getActiveDoc,
  docsState,
  hasDraft,
  isDraftPreview,
} from "./docsState.js";
import { getDocTextarea, updateDocStatus, updateDocControls, syncDocEditor } from "./docsUi.js";

export async function applyDocUpdateFromChat(kind, content, { force = false } = {}) {
  if (!content) return false;
  const textarea = getDocTextarea();
  const activeDoc = getActiveDoc();
  const viewingSameDoc = activeDoc === kind;
  const previewing = hasDraft(kind) && isDraftPreview(kind);
  if (viewingSameDoc && textarea) {
    const cached = docsState.docsCache[kind] || "";
    if (!force && !previewing && textarea.value !== cached) {
      const ok = await confirmModal(
        `You have unsaved ${kind.toUpperCase()} edits. Overwrite with chat result?`
      );
      if (!ok) {
        flash(
          `Kept your unsaved ${kind.toUpperCase()} edits; chat result not applied.`
        );
        return false;
      }
    }
  }

  docsState.docsCache[kind] = content;
  if (viewingSameDoc && textarea && !previewing) {
    textarea.value = content;
    updateDocStatus(kind);
  }
  if (viewingSameDoc) {
    updateDocControls(kind);
  }
  publish("docs:updated", { kind, content });
  if (kind === "todo") {
    renderTodoPreview(content);
    loadState({ notify: false }).catch(() => {});
  }
  return true;
}

export function applySpecIngestDocs(payload) {
  if (!payload) return;
  docsState.docsCache = {
    ...docsState.docsCache,
    todo: payload.todo ?? docsState.docsCache.todo,
    progress: payload.progress ?? docsState.docsCache.progress,
    opinions: payload.opinions ?? docsState.docsCache.opinions,
    spec: payload.spec ?? docsState.docsCache.spec,
    summary: payload.summary ?? docsState.docsCache.summary,
  };
  const activeDoc = getActiveDoc();
  if (activeDoc !== "snapshot") {
    syncDocEditor(activeDoc, { force: true });
  }
  updateDocControls(activeDoc);
  renderTodoPreview(docsState.docsCache.todo);
  publish("docs:updated", { kind: "todo", content: docsState.docsCache.todo });
  publish("docs:updated", { kind: "progress", content: docsState.docsCache.progress });
  publish("docs:updated", { kind: "opinions", content: docsState.docsCache.opinions });
  publish("docs:updated", { kind: "spec", content: docsState.docsCache.spec });
  publish("docs:updated", { kind: "summary", content: docsState.docsCache.summary });
  loadState({ notify: false }).catch(() => {});
}
