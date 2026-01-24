import { resolvePath, getAuthToken } from "./utils.js";
import {
  chatDecoder,
  getActiveDoc,
  getChatState,
  resetChatEvents,
  type ChatState,
  type ChatHistoryEntry,
} from "./docsState.js";
import {
  parseChatPayload,
  parseMaybeJson,
  recoverDraftMap,
  recoverPatchFromRaw,
  type DraftPayload,
} from "./docsParse.js";
import { applyDraftUpdates } from "./docsDrafts.js";
import { renderChat, updatePatchPreviewFromDraft } from "./docChatRender.js";
import { applyAppServerEvent, extractOutputDelta, renderChatEvents } from "./docChatEvents.js";
import {
  getSelectedAgent,
  getSelectedModel,
  getSelectedReasoning,
} from "./agentControls.js";

interface ChatRequestPayload {
  message: string;
  stream: boolean;
  agent?: string;
  model?: string;
  reasoning?: string;
  context_doc?: string;
}

interface ChatEntry extends ChatHistoryEntry {
  agent?: string;
  model?: string;
  reasoning?: string;
  drafts?: Record<string, unknown>;
  updated?: string[];
}

interface EventStreamPayload {
  thread_id?: string;
  threadId?: string;
  turn_id?: string;
  turnId?: string;
  agent?: string;
}

interface StreamParsedData {
  status?: string;
  token?: string;
  text?: string;
  detail?: string;
  error?: string;
}

export async function performDocChatRequest(entry: ChatEntry, state: ChatState): Promise<void> {
  const endpoint = resolvePath("/api/docs/chat");
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  const token = getAuthToken();
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  const payload: ChatRequestPayload = { message: entry.prompt, stream: true };
  payload.agent = entry.agent || getSelectedAgent();
  const selectedModel = entry.model || getSelectedModel(payload.agent);
  const selectedReasoning = entry.reasoning || getSelectedReasoning(payload.agent);
  if (selectedModel) {
    payload.model = selectedModel;
  }
  if (selectedReasoning) {
    payload.reasoning = selectedReasoning;
  }
  if (entry.viewing) {
    payload.context_doc = entry.viewing;
  }
  const res = await fetch(endpoint, {
    method: "POST",
    headers,
    body: JSON.stringify(payload),
    signal: state.controller.signal,
  });

  if (!res.ok) {
    const text = await res.text();
    let detail = text;
    try {
      const parsed = JSON.parse(text);
      const parsedObj = parsed as Record<string, unknown>;
      detail = (parsedObj.detail as string | undefined) || (parsedObj.error as string | undefined) || text;
    } catch (err) {
      // ignore parse errors
    }
    throw new Error(detail || `Request failed (${res.status})`);
  }

  const contentType = res.headers.get("content-type") || "";
  if (contentType.includes("text/event-stream")) {
    await readChatStream(res, state, entry);
    if (
      entry.status !== "error" &&
      entry.status !== "done" &&
      entry.status !== "interrupted"
    ) {
      entry.status = "done";
    }
  } else {
    const responsePayload = contentType.includes("application/json")
      ? await res.json()
      : await res.text();
    applyChatResult(responsePayload, state, entry);
  }
}

export async function startDocChatEventStream(payload: EventStreamPayload | undefined): Promise<void> {
  const threadId = payload?.thread_id || payload?.threadId;
  const turnId = payload?.turn_id || payload?.turnId;
  const agent = payload?.agent || getSelectedAgent();
  if (!threadId || !turnId) return;
  const state = getChatState();
  if (state.eventTurnId === turnId && state.eventThreadId === threadId) {
    return;
  }
  resetChatEvents(state);
  state.eventTurnId = turnId;
  state.eventThreadId = threadId;
  state.eventAgent = agent;
  state.eventController = new AbortController();
  renderChatEvents(state);

  const endpoint = resolvePath(
    `/api/agents/${encodeURIComponent(agent)}/turns/${encodeURIComponent(turnId)}/events`
  );
  const url = `${endpoint}?thread_id=${encodeURIComponent(threadId)}`;
  const headers: Record<string, string> = {};
  const token = getAuthToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  try {
    const res = await fetch(url, {
      method: "GET",
      headers,
      signal: state.eventController.signal,
    });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(text || `Event stream failed (${res.status})`);
    }
    const contentType = res.headers.get("content-type") || "";
    if (!contentType.includes("text/event-stream")) {
      throw new Error("Event stream unavailable");
    }
    await readAppServerEventStream(res, state);
  } catch (err) {
    const error = err as Error;
    if (error.name === "AbortError") return;
    state.eventError = error.message || "Failed to stream app-server events";
    renderChatEvents(state);
  }
}

export async function readAppServerEventStream(res: Response, state: ChatState): Promise<void> {
  if (!res.body) throw new Error("Streaming not supported in this browser");
  const reader = res.body.getReader();
  let buffer = "";
  let escapedNewlines = false;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    const decoded = chatDecoder.decode(value, { stream: true });
    if (!escapedNewlines) {
      const combined = buffer + decoded;
      if (!combined.includes("\n") && combined.includes("\\n")) {
        escapedNewlines = true;
        buffer = buffer.replace(/\\n(?=event:|data:|\\n)/g, "\n");
      }
    }
    buffer += escapedNewlines
      ? decoded.replace(/\\n(?=event:|data:|\\n)/g, "\n")
      : decoded;
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() || "";
    for (const chunk of chunks) {
      if (!chunk.trim()) continue;
      let event = "message";
      const dataLines: string[] = [];
      chunk.split("\n").forEach((line) => {
        if (line.startsWith("event:")) {
          event = line.slice(6).trim();
        } else if (line.startsWith("data:")) {
          dataLines.push(line.slice(5).trimStart());
        } else if (line.trim()) {
          dataLines.push(line);
        }
      });
      if (dataLines.length === 0) continue;
      const data = dataLines.join("\n");
      await handleAppServerStreamEvent(event || "message", data, state);
    }
  }
}

async function handleAppServerStreamEvent(_event: string, rawData: string, state: ChatState): Promise<void> {
  if (!rawData) return;
  const parsed = parseMaybeJson(rawData);
  applyAppServerEvent(state, parsed);
  const delta = extractOutputDelta(parsed);
  if (delta) {
    const entry = state.history[0] as ChatHistoryEntry | undefined;
    if (entry && entry.status === "running") {
      entry.response = (entry.response || "") + delta;
      state.streamText = entry.response;
      renderChat();
    }
  }
  renderChatEvents(state);
}

export async function readChatStream(res: Response, state: ChatState, entry: ChatEntry): Promise<void> {
  if (!res.body) throw new Error("Streaming not supported in this browser");
  const reader = res.body.getReader();
  let buffer = "";
  let escapedNewlines = false;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    const decoded = chatDecoder.decode(value, { stream: true });
    if (!escapedNewlines) {
      const combined = buffer + decoded;
      if (!combined.includes("\n") && combined.includes("\\n")) {
        escapedNewlines = true;
        buffer = buffer.replace(/\\n(?=event:|data:|\\n)/g, "\n");
      }
    }
    buffer += escapedNewlines
      ? decoded.replace(/\\n(?=event:|data:|\\n)/g, "\n")
      : decoded;
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() || "";
    for (const chunk of chunks) {
      if (!chunk.trim()) continue;
      let event = "message";
      const dataLines: string[] = [];
      chunk.split("\n").forEach((line) => {
        if (line.startsWith("event:")) {
          event = line.slice(6).trim();
        } else if (line.startsWith("data:")) {
          dataLines.push(line.slice(5).trimStart());
        } else if (line.trim()) {
          dataLines.push(line);
        }
      });
      const data = dataLines.join("\n");
      const sanitizedData = data.includes("\n")
        ? data.replace(/\n/g, "\\n")
        : data;
      await handleStreamEvent(event || "message", sanitizedData, state, entry);
    }
  }
}

export async function handleStreamEvent(event: string, rawData: string, state: ChatState, entry: ChatEntry): Promise<void> {
  const parsed = parseMaybeJson(rawData);
  if (event === "turn") {
    void startDocChatEventStream(parsed as EventStreamPayload | undefined);
    return;
  }
  if (event === "status") {
    state.statusText =
      typeof parsed === "string" ? parsed : (parsed as StreamParsedData).status || "";
    renderChat();
    return;
  }
  if (event === "token") {
    const token =
      typeof parsed === "string"
        ? parsed
        : (parsed as StreamParsedData).token || (parsed as StreamParsedData).text || rawData || "";
    entry.response = (entry.response || "") + token;
    state.streamText = entry.response || "";
    if (!state.statusText || state.statusText === "queued") {
      state.statusText = "responding";
    }
    renderChat();
    return;
  }
  if (event === "update") {
    const payload = parseChatPayload(parsed);
    const fallbackPatch = recoverPatchFromRaw(rawData);
    if (fallbackPatch) {
      updatePatchPreviewFromDraft({
        patch: fallbackPatch,
        content: "",
        agentMessage: "",
        createdAt: "",
        baseHash: "",
      });
    }
    if (payload.response) {
      entry.response = payload.response;
    }
    state.streamText = entry.response;
    let updated =
      (payload.updated && payload.updated.length
        ? payload.updated
        : Object.keys(payload.drafts || {})) || [];
    if (!updated.length) {
      const recoveredDrafts = recoverDraftMap(rawData);
      if (recoveredDrafts) {
        updated = Object.keys(recoveredDrafts);
        entry.updated = updated;
        entry.drafts = recoveredDrafts;
        applyDraftUpdates(recoveredDrafts);
        updatePatchPreviewFromDraft(recoveredDrafts[getActiveDoc()] as DraftPayload);
        entry.status = "done";
        renderChat();
        return;
      }
      const recoveredPatch = recoverPatchFromRaw(rawData);
      if (recoveredPatch) {
        const recoveredDraft: DraftPayload = {
          patch: recoveredPatch,
          content: "",
          agentMessage: "",
          createdAt: "",
          baseHash: "",
        };
        entry.updated = [getActiveDoc()];
        entry.drafts = { [getActiveDoc()]: recoveredDraft };
        applyDraftUpdates(entry.drafts);
        updatePatchPreviewFromDraft(recoveredDraft);
        entry.status = "done";
        renderChat();
        return;
      }
    }
    if (updated.length) {
      entry.updated = updated;
      entry.drafts = payload.drafts || {};
      applyDraftUpdates(payload.drafts);
      updatePatchPreviewFromDraft(payload.drafts?.[getActiveDoc()] as DraftPayload);
      entry.status = "done";
    }
    renderChat();
    return;
  }
  if (event === "error") {
    const message =
      (parsed && (parsed as Record<string, unknown>).detail) ||
      (parsed && (parsed as Record<string, unknown>).error) ||
      rawData ||
      "Doc chat failed";
    entry.status = "error";
    entry.error = String(message);
    state.error = String(message);
    state.status = "error";
    renderChat();
    resetChatEvents(state, { preserve: true });
    throw new Error(String(message));
  }
  if (event === "interrupted") {
    const message =
      (parsed && (parsed as Record<string, unknown>).detail) || rawData || "Doc chat interrupted";
    entry.status = "interrupted";
    entry.error = String(message);
    state.error = "";
    state.status = "interrupted";
    state.streamText = entry.response || "";
    resetChatEvents(state, { preserve: true });
    renderChat();
    return;
  }
  if (event === "done" || event === "finish") {
    entry.status = "done";
    resetChatEvents(state, { preserve: true });
    return;
  }
}

export function applyChatResult(payload: unknown, state: ChatState, entry: ChatEntry): void {
  const parsed = parseChatPayload(payload);
  if (parsed.interrupted) {
    entry.status = "interrupted";
    entry.error = (parsed as Record<string, unknown>).detail as string || "Doc chat interrupted";
    state.status = "interrupted";
    state.error = "";
    return;
  }
  if (parsed.error) {
    entry.status = "error";
    entry.error = parsed.error;
    state.error = parsed.error;
    state.status = "error";
    renderChat();
    return;
  }
  entry.status = "done";
  entry.response = parsed.response || "(no response)";
  state.streamText = parsed.response || "";
  const updated =
    (parsed.updated && parsed.updated.length
      ? parsed.updated
      : Object.keys(parsed.drafts || {})) || [];
  if (updated.length) {
    entry.updated = updated;
    entry.drafts = parsed.drafts || {};
    applyDraftUpdates(parsed.drafts);
  }
}
