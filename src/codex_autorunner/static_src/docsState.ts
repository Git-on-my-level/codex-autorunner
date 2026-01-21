import { CONSTANTS } from "./constants.js";
import { docButtons } from "./docsElements.js";

export const DOC_TYPES = ["todo", "progress", "opinions", "spec", "summary"] as const;
export type DocType = typeof DOC_TYPES[number];
export type DocKind = DocType | "snapshot";
export const CLEARABLE_DOCS = ["todo", "progress", "opinions"] as const;
export const COPYABLE_DOCS = ["spec", "summary"] as const;
export const PASTEABLE_DOCS = ["spec"] as const;
export const CHAT_HISTORY_LIMIT = 8;
export const CHAT_EVENT_LIMIT = CONSTANTS.UI?.DOC_CHAT_EVENT_LIMIT || 12;
export const CHAT_EVENT_MAX = Math.max(60, CHAT_EVENT_LIMIT * 8);

export const chatDecoder = new TextDecoder();

interface DocsCache {
  todo: string;
  progress: string;
  opinions: string;
  spec: string;
  summary: string;
}

interface SnapshotCache {
  exists: boolean;
  content: string;
  state: Record<string, unknown>;
}

export interface DraftData {
  [key: string]: unknown;
}

export interface DraftPreview {
  [key: string]: boolean;
}

export interface DraftState {
  data: DraftData;
  preview: DraftPreview;
}

interface SpecIngestState {
  status: string;
  patch: string;
  agentMessage: string;
  error: string;
  busy: boolean;
  controller: AbortController | null;
}

export interface ChatHistoryEntry {
  prompt?: string;
  response?: string;
  status: string;
  time?: string;
  error?: string;
  viewing?: string;
  targets?: string[];
  updated?: string[];
  drafts?: Record<string, unknown>;
}

export interface ChatState {
  history: unknown[];
  status: string;
  statusText: string;
  error: string;
  streamText: string;
  controller: AbortController | null;
  events: unknown[];
  eventsExpanded: boolean;
  eventController: AbortController | null;
  eventTurnId: string | null;
  eventThreadId: string | null;
  eventAgent: string | null;
  eventItemIndex: Record<string, unknown>;
  eventError: string;
}

export const docsState = {
  docsCache: { todo: "", progress: "", opinions: "", spec: "", summary: "" } as DocsCache,
  snapshotCache: { exists: false, content: "", state: {} } as SnapshotCache,
  snapshotBusy: false,
  activeDoc: "todo" as DocKind,
  chatState: createChatState(),
  draftState: {
    data: {},
    preview: {},
  } as DraftState,
  specIngestState: {
    status: "idle",
    patch: "",
    agentMessage: "",
    error: "",
    busy: false,
    controller: null,
  } as SpecIngestState,
  historyNavIndex: -1,
};

export const VOICE_TRANSCRIPT_DISCLAIMER_TEXT =
  CONSTANTS.PROMPTS?.VOICE_TRANSCRIPT_DISCLAIMER ||
  "Note: transcribed from user voice. If confusing or possibly inaccurate and you cannot infer the intention please clarify before proceeding.";

export function createChatState(): ChatState {
  return {
    history: [] as ChatHistoryEntry[],
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
    eventAgent: null,
    eventItemIndex: {},
    eventError: "",
  };
}

export function getChatState(): ChatState {
  return docsState.chatState;
}

export function getActiveDoc(): DocKind {
  return docsState.activeDoc;
}

export function setActiveDoc(kind: DocKind): void {
  docsState.activeDoc = kind;
}

export function getHistoryNavIndex(): number {
  return docsState.historyNavIndex;
}

export function setHistoryNavIndex(value: number): void {
  docsState.historyNavIndex = value;
}

export function setDraft(kind: DocKind, draft: unknown): void {
  if (!DOC_TYPES.includes(kind as DocType)) return;
  if (!draft) {
    delete docsState.draftState.data[kind];
    delete docsState.draftState.preview[kind];
  } else {
    docsState.draftState.data[kind] = draft;
  }
  updateDocDraftIndicators();
}

export function getDraft(kind: DocKind): unknown | null {
  return docsState.draftState.data[kind] || null;
}

export function hasDraft(kind: DocKind): boolean {
  return !!getDraft(kind);
}

export function isDraftPreview(kind: DocKind): boolean {
  return !!docsState.draftState.preview[kind];
}

export function setDraftPreview(kind: DocKind, value: boolean): void {
  if (!DOC_TYPES.includes(kind as DocType)) return;
  if (value) {
    docsState.draftState.preview[kind] = true;
  } else {
    delete docsState.draftState.preview[kind];
  }
  updateDocDraftIndicators();
}

export function updateDocDraftIndicators(): void {
  docButtons.forEach((btn: Element) => {
    const htmlBtn = btn as HTMLElement;
    const kind = htmlBtn.dataset.doc as DocKind;
    if (!DOC_TYPES.includes(kind as DocType)) return;
    htmlBtn.classList.toggle("has-draft", hasDraft(kind));
    htmlBtn.classList.toggle(
      "previewing",
      hasDraft(kind) && isDraftPreview(kind)
    );
  });
}

interface ResetChatEventsOptions {
  preserve?: boolean;
}

export function resetChatEvents(state: ChatState, { preserve = false }: ResetChatEventsOptions = {}): void {
  if (state.eventController) {
    state.eventController.abort();
  }
  state.eventController = null;
  state.eventTurnId = null;
  state.eventThreadId = null;
  state.eventAgent = null;
  state.eventItemIndex = {};
  state.eventError = "";
  if (!preserve) {
    state.events = [];
    state.eventsExpanded = false;
  }
}

export function getDocChatViewing(): DocKind {
  if (!DOC_TYPES.includes(docsState.activeDoc as DocType)) return "todo";
  return docsState.activeDoc;
}
