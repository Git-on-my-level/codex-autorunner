export const docButtons = /** @type {NodeListOf<HTMLElement>} */ (
  document.querySelectorAll(".chip[data-doc]")
);

export const chatUI = {
  status: /** @type {HTMLElement|null} */ (
    document.getElementById("doc-chat-status")
  ),
  eventsMain: /** @type {HTMLElement|null} */ (
    document.getElementById("doc-chat-events")
  ),
  eventsList: /** @type {HTMLElement|null} */ (
    document.getElementById("doc-chat-events-list")
  ),
  eventsCount: /** @type {HTMLElement|null} */ (
    document.getElementById("doc-chat-events-count")
  ),
  eventsToggle: /** @type {HTMLButtonElement|null} */ (
    document.getElementById("doc-chat-events-toggle")
  ),
  patchMain: /** @type {HTMLElement|null} */ (
    document.getElementById("doc-patch-main")
  ),
  patchSummary: /** @type {HTMLElement|null} */ (
    document.getElementById("doc-patch-summary")
  ),
  patchMeta: /** @type {HTMLElement|null} */ (
    document.getElementById("doc-patch-meta")
  ),
  patchBody: /** @type {HTMLElement|null} */ (
    document.getElementById("doc-patch-body")
  ),
  patchApply: /** @type {HTMLButtonElement|null} */ (
    document.getElementById("doc-patch-apply")
  ),
  patchPreview: /** @type {HTMLButtonElement|null} */ (
    document.getElementById("doc-patch-preview")
  ),
  patchDiscard: /** @type {HTMLButtonElement|null} */ (
    document.getElementById("doc-patch-discard")
  ),
  patchReload: /** @type {HTMLButtonElement|null} */ (
    document.getElementById("doc-patch-reload")
  ),
  history: /** @type {HTMLElement|null} */ (
    document.getElementById("doc-chat-history")
  ),
  historyCount: /** @type {HTMLElement|null} */ (
    document.getElementById("doc-chat-history-count")
  ),
  error: /** @type {HTMLElement|null} */ (
    document.getElementById("doc-chat-error")
  ),
  input: /** @type {HTMLTextAreaElement|null} */ (
    document.getElementById("doc-chat-input")
  ),
  send: /** @type {HTMLButtonElement|null} */ (
    document.getElementById("doc-chat-send")
  ),
  cancel: /** @type {HTMLButtonElement|null} */ (
    document.getElementById("doc-chat-cancel")
  ),
  newThread: /** @type {HTMLButtonElement|null} */ (
    document.getElementById("doc-chat-new-thread")
  ),
  voiceBtn: /** @type {HTMLButtonElement|null} */ (
    document.getElementById("doc-chat-voice")
  ),
  voiceStatus: /** @type {HTMLElement|null} */ (
    document.getElementById("doc-chat-voice-status")
  ),
  hint: /** @type {HTMLElement|null} */ (
    document.getElementById("doc-chat-hint")
  ),
};

export const specIssueUI = {
  row: /** @type {HTMLElement|null} */ (
    document.getElementById("spec-issue-import")
  ),
  toggle: /** @type {HTMLButtonElement|null} */ (
    document.getElementById("spec-issue-import-toggle")
  ),
  inputRow: /** @type {HTMLElement|null} */ (
    document.getElementById("spec-issue-input-row")
  ),
  input: /** @type {HTMLInputElement|null} */ (
    document.getElementById("spec-issue-input")
  ),
  button: /** @type {HTMLButtonElement|null} */ (
    document.getElementById("spec-issue-import-btn")
  ),
};

export const snapshotUI = {
  generate: /** @type {HTMLButtonElement|null} */ (
    document.getElementById("snapshot-generate")
  ),
  update: /** @type {HTMLButtonElement|null} */ (
    document.getElementById("snapshot-update")
  ),
  regenerate: /** @type {HTMLButtonElement|null} */ (
    document.getElementById("snapshot-regenerate")
  ),
  copy: /** @type {HTMLButtonElement|null} */ (
    document.getElementById("snapshot-copy")
  ),
  refresh: /** @type {HTMLButtonElement|null} */ (
    document.getElementById("snapshot-refresh")
  ),
};

export const docActionsUI = {
  standard: /** @type {HTMLElement|null} */ (
    document.getElementById("doc-actions-standard")
  ),
  snapshot: /** @type {HTMLElement|null} */ (
    document.getElementById("doc-actions-snapshot")
  ),
  ingest: /** @type {HTMLButtonElement|null} */ (
    document.getElementById("ingest-spec")
  ),
  clear: /** @type {HTMLButtonElement|null} */ (
    document.getElementById("clear-docs")
  ),
  copy: /** @type {HTMLButtonElement|null} */ (
    document.getElementById("doc-copy")
  ),
  paste: /** @type {HTMLButtonElement|null} */ (
    document.getElementById("spec-paste")
  ),
};

export const specIngestUI = {
  panel: /** @type {HTMLElement|null} */ (
    document.getElementById("spec-ingest-followup")
  ),
  input: /** @type {HTMLTextAreaElement|null} */ (
    document.getElementById("spec-ingest-input")
  ),
  continueBtn: /** @type {HTMLButtonElement|null} */ (
    document.getElementById("spec-ingest-continue")
  ),
  cancelBtn: /** @type {HTMLButtonElement|null} */ (
    document.getElementById("spec-ingest-cancel")
  ),
  patchMain: /** @type {HTMLElement|null} */ (
    document.getElementById("spec-ingest-patch-main")
  ),
  patchSummary: /** @type {HTMLElement|null} */ (
    document.getElementById("spec-ingest-patch-summary")
  ),
  patchBody: /** @type {HTMLElement|null} */ (
    document.getElementById("spec-ingest-patch-body")
  ),
  patchApply: /** @type {HTMLButtonElement|null} */ (
    document.getElementById("spec-ingest-patch-apply")
  ),
  patchDiscard: /** @type {HTMLButtonElement|null} */ (
    document.getElementById("spec-ingest-patch-discard")
  ),
  patchReload: /** @type {HTMLButtonElement|null} */ (
    document.getElementById("spec-ingest-patch-reload")
  ),
};

export const threadRegistryUI = {
  banner: /** @type {HTMLElement|null} */ (
    document.getElementById("doc-thread-registry-banner")
  ),
  detail: /** @type {HTMLElement|null} */ (
    document.getElementById("doc-thread-registry-detail")
  ),
  reset: /** @type {HTMLButtonElement|null} */ (
    document.getElementById("doc-thread-registry-reset")
  ),
  download: /** @type {HTMLButtonElement|null} */ (
    document.getElementById("doc-thread-registry-download")
  ),
};
