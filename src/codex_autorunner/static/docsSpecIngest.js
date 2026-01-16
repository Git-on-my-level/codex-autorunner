import { api, confirmModal, flash } from "./utils.js";
import { applySpecIngestDocs } from "./docsDocUpdates.js";
import { parseSpecIngestPayload } from "./docsParse.js";
import { getActiveDoc, docsState } from "./docsState.js";
import { specIngestUI } from "./docsElements.js";
import { renderDiffHtml, updateDocVisibility, autoResizeTextarea } from "./docsUi.js";

export function renderSpecIngestPatch() {
  if (!specIngestUI.patchMain) return;
  const isSpec = getActiveDoc() === "spec";
  const hasPatch = !!(docsState.specIngestState.patch || "").trim();
  if (specIngestUI.continueBtn)
    specIngestUI.continueBtn.disabled = docsState.specIngestState.busy;
  if (specIngestUI.cancelBtn) {
    specIngestUI.cancelBtn.disabled = !docsState.specIngestState.busy;
    specIngestUI.cancelBtn.classList.toggle(
      "hidden",
      !docsState.specIngestState.busy
    );
  }
  specIngestUI.patchMain.classList.toggle("hidden", !isSpec || !hasPatch);
  if (!isSpec || !hasPatch) {
    updateDocVisibility();
    return;
  }
  specIngestUI.patchBody.innerHTML = renderDiffHtml(docsState.specIngestState.patch);
  specIngestUI.patchSummary.textContent =
    docsState.specIngestState.agentMessage || "Spec ingest patch ready";
  if (specIngestUI.patchApply)
    specIngestUI.patchApply.disabled =
      docsState.specIngestState.busy || !hasPatch;
  if (specIngestUI.patchDiscard)
    specIngestUI.patchDiscard.disabled =
      docsState.specIngestState.busy || !hasPatch;
  if (specIngestUI.patchReload)
    specIngestUI.patchReload.disabled = docsState.specIngestState.busy;
  updateDocVisibility();
}

async function interruptSpecIngest() {
  try {
    await api("/api/ingest-spec/interrupt", { method: "POST" });
  } catch (err) {
    flash(err.message || "Failed to interrupt spec ingest", "error");
  }
}

export async function ingestSpec() {
  if (docsState.specIngestState.busy) return;
  const needsForce = ["todo", "progress", "opinions"].some(
    (k) => (docsState.docsCache[k] || "").trim().length > 0
  );
  if (needsForce) {
    const ok = await confirmModal(
      "Overwrite TODO, PROGRESS, and OPINIONS from SPEC? Existing content will be replaced."
    );
    if (!ok) return;
  }
  const button = /** @type {HTMLButtonElement} */ (
    document.getElementById("ingest-spec")
  );
  button.disabled = true;
  button.classList.add("loading");
  docsState.specIngestState.busy = true;
  docsState.specIngestState.controller = new AbortController();
  renderSpecIngestPatch();
  try {
    const data = await api("/api/ingest-spec", {
      method: "POST",
      body: { force: needsForce },
      signal: docsState.specIngestState.controller.signal,
    });
    const parsed = parseSpecIngestPayload(data);
    if (parsed.error) throw new Error(parsed.error);
    if (parsed.interrupted) {
      docsState.specIngestState.patch = "";
      docsState.specIngestState.agentMessage = parsed.agentMessage || "";
      applySpecIngestDocs(parsed);
      renderSpecIngestPatch();
      flash("Spec ingest interrupted");
      return;
    }
    docsState.specIngestState.patch = parsed.patch || "";
    docsState.specIngestState.agentMessage = parsed.agentMessage || "";
    applySpecIngestDocs(parsed);
    renderSpecIngestPatch();
    flash(parsed.patch ? "Spec ingest patch ready" : "Ingested SPEC into docs");
  } catch (err) {
    if (err.name === "AbortError") {
      return;
    } else {
      flash(err.message, "error");
    }
  } finally {
    button.disabled = false;
    button.classList.remove("loading");
    docsState.specIngestState.busy = false;
    docsState.specIngestState.controller = null;
    renderSpecIngestPatch();
  }
}

export function cancelSpecIngest() {
  if (!docsState.specIngestState.busy) return;
  interruptSpecIngest();
  if (docsState.specIngestState.controller)
    docsState.specIngestState.controller.abort();
  docsState.specIngestState.busy = false;
  docsState.specIngestState.controller = null;
  if (specIngestUI.continueBtn) specIngestUI.continueBtn.disabled = false;
  flash("Spec ingest interrupted");
  renderSpecIngestPatch();
}

export async function continueSpecIngest() {
  if (docsState.specIngestState.busy) return;
  if (!specIngestUI.input) return;
  const message = (specIngestUI.input.value || "").trim();
  if (!message) {
    flash("Enter a follow-up prompt to continue", "error");
    return;
  }
  const needsForce = ["todo", "progress", "opinions"].some(
    (k) => (docsState.docsCache[k] || "").trim().length > 0
  );
  docsState.specIngestState.busy = true;
  if (specIngestUI.continueBtn) specIngestUI.continueBtn.disabled = true;
  docsState.specIngestState.controller = new AbortController();
  renderSpecIngestPatch();
  try {
    const data = await api("/api/ingest-spec", {
      method: "POST",
      body: { force: needsForce, message },
      signal: docsState.specIngestState.controller.signal,
    });
    const parsed = parseSpecIngestPayload(data);
    if (parsed.error) throw new Error(parsed.error);
    if (parsed.interrupted) {
      docsState.specIngestState.patch = "";
      docsState.specIngestState.agentMessage = parsed.agentMessage || "";
      applySpecIngestDocs(parsed);
      renderSpecIngestPatch();
      flash("Spec ingest interrupted");
      return;
    }
    docsState.specIngestState.patch = parsed.patch || "";
    docsState.specIngestState.agentMessage = parsed.agentMessage || "";
    applySpecIngestDocs(parsed);
    renderSpecIngestPatch();
    specIngestUI.input.value = "";
    autoResizeTextarea(specIngestUI.input);
    flash(parsed.patch ? "Spec ingest patch updated" : "Spec ingest updated docs");
  } catch (err) {
    if (err.name === "AbortError") {
      return;
    } else {
      flash(err.message, "error");
    }
  } finally {
    docsState.specIngestState.busy = false;
    if (specIngestUI.continueBtn) specIngestUI.continueBtn.disabled = false;
    docsState.specIngestState.controller = null;
    renderSpecIngestPatch();
  }
}

export async function applySpecIngestPatch() {
  if (!docsState.specIngestState.patch) {
    flash("No spec ingest patch to apply", "error");
    return;
  }
  docsState.specIngestState.busy = true;
  renderSpecIngestPatch();
  try {
    const res = await api("/api/ingest-spec/apply", { method: "POST" });
    const parsed = parseSpecIngestPayload(res);
    if (parsed.error) throw new Error(parsed.error);
    docsState.specIngestState.patch = "";
    docsState.specIngestState.agentMessage = "";
    applySpecIngestDocs(parsed);
    flash("Spec ingest patch applied");
  } catch (err) {
    flash(err.message || "Failed to apply spec ingest patch", "error");
  } finally {
    docsState.specIngestState.busy = false;
    renderSpecIngestPatch();
  }
}

export async function discardSpecIngestPatch() {
  if (!docsState.specIngestState.patch) return;
  docsState.specIngestState.busy = true;
  renderSpecIngestPatch();
  try {
    const res = await api("/api/ingest-spec/discard", { method: "POST" });
    const parsed = parseSpecIngestPayload(res);
    if (parsed.error) throw new Error(parsed.error);
    docsState.specIngestState.patch = "";
    docsState.specIngestState.agentMessage = "";
    applySpecIngestDocs(parsed);
    flash("Spec ingest patch discarded");
  } catch (err) {
    flash(err.message || "Failed to discard spec ingest patch", "error");
  } finally {
    docsState.specIngestState.busy = false;
    renderSpecIngestPatch();
  }
}

export async function reloadSpecIngestPatch(silent = false) {
  try {
    const res = await api("/api/ingest-spec/pending", { method: "GET" });
    const parsed = parseSpecIngestPayload(res);
    if (parsed.error) throw new Error(parsed.error);
    if (parsed.patch) {
      docsState.specIngestState.patch = parsed.patch;
      docsState.specIngestState.agentMessage = parsed.agentMessage || "";
      applySpecIngestDocs(parsed);
      renderSpecIngestPatch();
      if (!silent) flash("Loaded spec ingest patch");
      return;
    }
  } catch (err) {
    const message = err?.message || "";
    if (message.includes("No pending spec ingest patch")) {
      docsState.specIngestState.patch = "";
      docsState.specIngestState.agentMessage = "";
      renderSpecIngestPatch();
      return;
    }
    if (!silent) {
      flash(message || "Failed to load spec ingest patch", "error");
    }
  }
  if (!docsState.specIngestState.patch) {
    renderSpecIngestPatch();
  }
}
