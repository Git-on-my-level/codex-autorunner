import { CONSTANTS } from "./constants.js";
import { docButtons } from "./docsElements.js";

export const DOC_TYPES = ["todo", "progress", "opinions", "spec", "summary"];
export const CLEARABLE_DOCS = ["todo", "progress", "opinions"];
export const COPYABLE_DOCS = ["spec", "summary"];
export const PASTEABLE_DOCS = ["spec"];
export const CHAT_HISTORY_LIMIT = 8;
export const CHAT_EVENT_LIMIT = CONSTANTS.UI?.DOC_CHAT_EVENT_LIMIT || 12;
export const CHAT_EVENT_MAX = Math.max(60, CHAT_EVENT_LIMIT * 8);

export const chatDecoder = new TextDecoder();

export const docsState = {
  docsCache: { todo: "", progress: "", opinions: "", spec: "", summary: "" },
  snapshotCache: { exists: false, content: "", state: {} },
  snapshotBusy: false,
  activeDoc: "todo",
  chatState: createChatState(),
  draftState: {
    data: {},
    preview: {},
  },
  specIngestState: {
    status: "idle",
    patch: "",
    agentMessage: "",
    error: "",
    busy: false,
    controller: null,
  },
  historyNavIndex: -1,
};

export const VOICE_TRANSCRIPT_DISCLAIMER_TEXT =
  CONSTANTS.PROMPTS?.VOICE_TRANSCRIPT_DISCLAIMER ||
  "Note: transcribed from user voice. If confusing or possibly inaccurate and you cannot infer the intention please clarify before proceeding.";

export function createChatState() {
  return {
    history: [],
    status: "idle",
    statusText: "",
    error: "",
    streamText: "",
    controller: null,
    events: [],
    eventsExpanded: false,
    eventController: null,
    eventTurnId: null,
    eventThreadId: null,
    eventItemIndex: {},
    eventError: "",
  };
}

export function getChatState() {
  return docsState.chatState;
}

export function getActiveDoc() {
  return docsState.activeDoc;
}

export function setActiveDoc(kind) {
  docsState.activeDoc = kind;
}

export function getHistoryNavIndex() {
  return docsState.historyNavIndex;
}

export function setHistoryNavIndex(value) {
  docsState.historyNavIndex = value;
}

export function setDraft(kind, draft) {
  if (!DOC_TYPES.includes(kind)) return;
  if (!draft) {
    delete docsState.draftState.data[kind];
    delete docsState.draftState.preview[kind];
  } else {
    docsState.draftState.data[kind] = draft;
  }
  updateDocDraftIndicators();
}

export function getDraft(kind) {
  return docsState.draftState.data[kind] || null;
}

export function hasDraft(kind) {
  return !!getDraft(kind);
}

export function isDraftPreview(kind) {
  return !!docsState.draftState.preview[kind];
}

export function setDraftPreview(kind, value) {
  if (!DOC_TYPES.includes(kind)) return;
  if (value) {
    docsState.draftState.preview[kind] = true;
  } else {
    delete docsState.draftState.preview[kind];
  }
  updateDocDraftIndicators();
}

export function updateDocDraftIndicators() {
  docButtons.forEach((btn) => {
    const kind = btn.dataset.doc;
    if (!DOC_TYPES.includes(kind)) return;
    btn.classList.toggle("has-draft", hasDraft(kind));
    btn.classList.toggle(
      "previewing",
      hasDraft(kind) && isDraftPreview(kind)
    );
  });
}

export function resetChatEvents(state, { preserve = false } = {}) {
  if (state.eventController) {
    state.eventController.abort();
  }
  state.eventController = null;
  state.eventTurnId = null;
  state.eventThreadId = null;
  state.eventItemIndex = {};
  state.eventError = "";
  if (!preserve) {
    state.events = [];
    state.eventsExpanded = false;
  }
}

export function getDocChatViewing() {
  if (!DOC_TYPES.includes(docsState.activeDoc)) return "";
  return docsState.activeDoc;
}
