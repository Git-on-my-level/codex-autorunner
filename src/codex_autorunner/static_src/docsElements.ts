export const docButtons: NodeListOf<HTMLElement> = document.querySelectorAll(
  ".chip[data-doc]"
);

export const chatUI = {
  status: document.getElementById("doc-chat-status") as HTMLElement | null,
  eventsMain: document.getElementById("doc-chat-events") as HTMLElement | null,
  eventsList: document.getElementById("doc-chat-events-list") as HTMLElement | null,
  eventsCount: document.getElementById("doc-chat-events-count") as HTMLElement | null,
  eventsToggle: document.getElementById("doc-chat-events-toggle") as HTMLButtonElement | null,
  patchMain: document.getElementById("doc-patch-main") as HTMLElement | null,
  patchSummary: document.getElementById("doc-patch-summary") as HTMLElement | null,
  patchMeta: document.getElementById("doc-patch-meta") as HTMLElement | null,
  patchBody: document.getElementById("doc-patch-body") as HTMLElement | null,
  patchApply: document.getElementById("doc-patch-apply") as HTMLButtonElement | null,
  patchPreview: document.getElementById("doc-patch-preview") as HTMLButtonElement | null,
  patchDiscard: document.getElementById("doc-patch-discard") as HTMLButtonElement | null,
  patchReload: document.getElementById("doc-patch-reload") as HTMLButtonElement | null,
  history: document.getElementById("doc-chat-history") as HTMLElement | null,
  historyCount: document.getElementById("doc-chat-history-count") as HTMLElement | null,
  error: document.getElementById("doc-chat-error") as HTMLElement | null,
  input: document.getElementById("doc-chat-input") as HTMLTextAreaElement | null,
  send: document.getElementById("doc-chat-send") as HTMLButtonElement | null,
  cancel: document.getElementById("doc-chat-cancel") as HTMLButtonElement | null,
  newThread: document.getElementById("doc-chat-new-thread") as HTMLButtonElement | null,
  voiceBtn: document.getElementById("doc-chat-voice") as HTMLButtonElement | null,
  voiceStatus: document.getElementById("doc-chat-voice-status") as HTMLElement | null,
  hint: document.getElementById("doc-chat-hint") as HTMLElement | null,
  agentSelect: document.getElementById("doc-agent-select") as HTMLSelectElement | null,
  modelSelect: document.getElementById("doc-model-select") as HTMLSelectElement | null,
  reasoningSelect: document.getElementById("doc-reasoning-select") as HTMLSelectElement | null,
};

export const specIssueUI = {
  row: document.getElementById("spec-issue-import") as HTMLElement | null,
  toggle: document.getElementById("spec-issue-import-toggle") as HTMLButtonElement | null,
  inputRow: document.getElementById("spec-issue-input-row") as HTMLElement | null,
  input: document.getElementById("spec-issue-input") as HTMLInputElement | null,
  button: document.getElementById("spec-issue-import-btn") as HTMLButtonElement | null,
};

export const snapshotUI = {
  generate: document.getElementById("snapshot-generate") as HTMLButtonElement | null,
  update: document.getElementById("snapshot-update") as HTMLButtonElement | null,
  regenerate: document.getElementById("snapshot-regenerate") as HTMLButtonElement | null,
  copy: document.getElementById("snapshot-copy") as HTMLButtonElement | null,
  refresh: document.getElementById("snapshot-refresh") as HTMLButtonElement | null,
};

export const docActionsUI = {
  standard: document.getElementById("doc-actions-standard") as HTMLElement | null,
  snapshot: document.getElementById("doc-actions-snapshot") as HTMLElement | null,
  ingest: document.getElementById("ingest-spec") as HTMLButtonElement | null,
  clear: document.getElementById("clear-docs") as HTMLButtonElement | null,
  copy: document.getElementById("doc-copy") as HTMLButtonElement | null,
  paste: document.getElementById("spec-paste") as HTMLButtonElement | null,
};

export const specIngestUI = {
  panel: document.getElementById("spec-ingest-followup") as HTMLElement | null,
  input: document.getElementById("spec-ingest-input") as HTMLTextAreaElement | null,
  continueBtn: document.getElementById("spec-ingest-continue") as HTMLButtonElement | null,
  cancelBtn: document.getElementById("spec-ingest-cancel") as HTMLButtonElement | null,
  patchMain: document.getElementById("spec-ingest-patch-main") as HTMLElement | null,
  patchSummary: document.getElementById("spec-ingest-patch-summary") as HTMLElement | null,
  patchBody: document.getElementById("spec-ingest-patch-body") as HTMLElement | null,
  patchApply: document.getElementById("spec-ingest-patch-apply") as HTMLButtonElement | null,
  patchDiscard: document.getElementById("spec-ingest-patch-discard") as HTMLButtonElement | null,
  patchReload: document.getElementById("spec-ingest-patch-reload") as HTMLButtonElement | null,
};

export const threadRegistryUI = {
  banner: document.getElementById("doc-thread-registry-banner") as HTMLElement | null,
  detail: document.getElementById("doc-thread-registry-detail") as HTMLElement | null,
  reset: document.getElementById("doc-thread-registry-reset") as HTMLButtonElement | null,
  download: document.getElementById("doc-thread-registry-download") as HTMLButtonElement | null,
};
