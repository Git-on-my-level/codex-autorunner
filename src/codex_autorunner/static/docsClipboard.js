import { flash } from "./utils.js";
import { PASTEABLE_DOCS, getActiveDoc, hasDraft, isDraftPreview } from "./docsState.js";
import { getDocTextarea, getDocCopyText, updateDocControls } from "./docsUi.js";

export async function copyDocToClipboard(kind = getActiveDoc()) {
  const text = getDocCopyText(kind);
  if (!text.trim()) return;
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      flash("Copied to clipboard");
      return;
    }
  } catch {
    // fall through
  }

  let temp = null;
  try {
    temp = document.createElement("textarea");
    temp.value = text;
    temp.setAttribute("readonly", "");
    temp.style.position = "fixed";
    temp.style.top = "-9999px";
    temp.style.opacity = "0";
    document.body.appendChild(temp);
    temp.select();
    const ok = document.execCommand("copy");
    flash(ok ? "Copied to clipboard" : "Copy failed");
  } catch {
    flash("Copy failed");
  } finally {
    if (temp && temp.parentNode) {
      temp.parentNode.removeChild(temp);
    }
  }
}

export async function pasteSpecFromClipboard() {
  const activeDoc = getActiveDoc();
  if (!PASTEABLE_DOCS.includes(activeDoc)) return;
  if (hasDraft(activeDoc) && isDraftPreview(activeDoc)) {
    flash("Exit draft preview before pasting.", "error");
    return;
  }
  const textarea = getDocTextarea();
  if (!textarea) return;
  try {
    if (!navigator.clipboard?.readText) {
      flash("Paste not supported in this browser", "error");
      return;
    }
    const text = await navigator.clipboard.readText();
    if (!text) {
      flash("Clipboard is empty", "error");
      return;
    }
    textarea.value = text;
    textarea.focus();
    updateDocControls("spec");
    flash("SPEC replaced from clipboard");
  } catch {
    flash("Paste failed", "error");
  }
}
