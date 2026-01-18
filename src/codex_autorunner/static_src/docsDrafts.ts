import {
  getActiveDoc,
  hasDraft,
  isDraftPreview,
  setDraft,
} from "./docsState.js";
import { normalizeDraftPayload } from "./docsParse.js";
import { syncDocEditor } from "./docsUi.js";

export function applyDraftUpdates(drafts: Record<string, unknown>): void {
  if (!drafts || typeof drafts !== "object") return;
  Object.entries(drafts).forEach(([kind, entry]) => {
    const normalized = normalizeDraftPayload(entry);
    if (normalized) setDraft(kind as import("./docsState.js").DocKind, normalized);
  });
  const activeDoc = getActiveDoc();
  if (hasDraft(activeDoc) && isDraftPreview(activeDoc)) {
    syncDocEditor(activeDoc, { force: true });
  }
}
