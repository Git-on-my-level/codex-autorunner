/**
 * Shared parsing helpers for agent (app-server) events.
 * Used by ticket chat and live agent output to render rich activity.
 */

export type AgentEventKind =
  | "thinking"
  | "tool"
  | "command"
  | "file"
  | "output"
  | "error"
  | "status"
  | "event";

export interface AgentEvent {
  id: string;
  title: string;
  summary: string;
  detail: string;
  kind: AgentEventKind;
  isSignificant: boolean;
  time: number;
  itemId: string | null;
  method: string;
}

export interface ParsedAgentEvent {
  event: AgentEvent;
  mergeStrategy?: "append" | "newline" | "replace";
}

interface CommandItem {
  command?: string | string[];
  type?: string;
  exitCode?: number | null;
  text?: string;
  message?: string;
  name?: string;
  tool?: string;
  id?: string;
  itemId?: string;
}

interface PayloadParams {
  command?: string | string[];
  error?: ErrorObject | string;
  delta?: string;
  text?: string;
  output?: string;
  status?: string;
  message?: string;
  files?: Array<string | { path?: string; file?: string; name?: string }>;
  fileChanges?: Array<string | { path?: string; file?: string; name?: string }>;
  paths?: Array<string | { path?: string; file?: string; name?: string }>;
  path?: string | { path?: string; file?: string; name?: string };
  file?: string | { path?: string; file?: string; name?: string };
  name?: string | { path?: string; file?: string; name?: string };
  item?: CommandItem;
  itemId?: string | null;
}

interface ErrorObject {
  message?: string;
  additionalDetails?: string;
  details?: string;
}

interface EventPayload {
  message?: EventMessage | unknown;
  received_at?: number;
  receivedAt?: number;
  id?: string;
}

interface EventMessage {
  method?: string;
  params?: PayloadParams;
}

const openCodeMessageRoles: Record<string, string> = {};
const openCodePendingTextByMessage: Record<string, string> = {};
const openCodePartTypes: Record<string, string> = {};
let openCodePendingTextNoId = "";
let openCodeMessageRolesSeen = false;

export function resetOpenCodeEventState(): void {
  Object.keys(openCodeMessageRoles).forEach((key) => delete openCodeMessageRoles[key]);
  Object.keys(openCodePendingTextByMessage).forEach((key) => delete openCodePendingTextByMessage[key]);
  Object.keys(openCodePartTypes).forEach((key) => delete openCodePartTypes[key]);
  openCodePendingTextNoId = "";
  openCodeMessageRolesSeen = false;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : null;
}

function extractOpenCodeProperties(params: PayloadParams | null | undefined): Record<string, unknown> {
  const properties = params ? asRecord((params as Record<string, unknown>).properties) : null;
  return properties || {};
}

function extractOpenCodePart(params: PayloadParams | null | undefined): Record<string, unknown> | null {
  const properties = extractOpenCodeProperties(params);
  const part = asRecord(properties.part);
  if (part) return part;
  return params ? asRecord((params as Record<string, unknown>).part) : null;
}

function extractOpenCodeInfo(params: PayloadParams | null | undefined): Record<string, unknown> | null {
  const properties = extractOpenCodeProperties(params);
  const info = asRecord(properties.info);
  if (info) return info;
  return params ? asRecord((params as Record<string, unknown>).info) : null;
}

function extractOpenCodeMessageId(params: PayloadParams | null | undefined): string | null {
  const info = extractOpenCodeInfo(params);
  const infoId = info?.id;
  if (typeof infoId === "string" && infoId.trim()) return infoId.trim();

  const part = extractOpenCodePart(params);
  const partMessageId = part?.messageID || part?.messageId || part?.message_id;
  if (typeof partMessageId === "string" && partMessageId.trim()) return partMessageId.trim();
  const properties = extractOpenCodeProperties(params);
  const directMessageId =
    properties.messageID ||
    properties.messageId ||
    properties.message_id ||
    (params ? (params as Record<string, unknown>).messageID : null) ||
    (params ? (params as Record<string, unknown>).messageId : null) ||
    (params ? (params as Record<string, unknown>).message_id : null);
  if (typeof directMessageId === "string" && directMessageId.trim()) return directMessageId.trim();

  return null;
}

function extractOpenCodePartId(params: PayloadParams | null | undefined): string | null {
  const part = extractOpenCodePart(params);
  const nestedPartId = part?.id || part?.partID || part?.partId || part?.part_id;
  if (typeof nestedPartId === "string" && nestedPartId.trim()) return nestedPartId.trim();
  const properties = extractOpenCodeProperties(params);
  const directPartId =
    properties.id ||
    properties.partID ||
    properties.partId ||
    properties.part_id ||
    (params ? (params as Record<string, unknown>).id : null) ||
    (params ? (params as Record<string, unknown>).partID : null) ||
    (params ? (params as Record<string, unknown>).partId : null) ||
    (params ? (params as Record<string, unknown>).part_id : null);
  if (typeof directPartId === "string" && directPartId.trim()) return directPartId.trim();
  return null;
}

function extractOpenCodePartType(params: PayloadParams | null | undefined): string {
  const part = extractOpenCodePart(params);
  const partType = part?.type;
  if (typeof partType === "string") return partType.trim().toLowerCase();
  return "";
}

function isOpenCodeMessagePartMethod(method: string): boolean {
  return method === "message.part.updated" || method === "message.part.delta";
}

function extractOpenCodeRole(params: PayloadParams | null | undefined): string {
  const info = extractOpenCodeInfo(params);
  const role = info?.role;
  if (typeof role === "string") return role.trim().toLowerCase();
  const directRole = params ? (params as Record<string, unknown>).role : null;
  if (typeof directRole === "string") return directRole.trim().toLowerCase();
  return "";
}

function extractOpenCodeDeltaText(params: PayloadParams | null | undefined): string {
  if (!params || typeof params !== "object") return "";
  if (typeof params.delta === "string") return params.delta;
  if (typeof params.text === "string") return params.text;
  if (typeof params.output === "string") return params.output;

  const properties = extractOpenCodeProperties(params);
  if (typeof properties.delta === "string" && properties.delta) return properties.delta;
  const delta = asRecord(properties.delta);
  const deltaText = delta?.text;
  if (typeof deltaText === "string" && deltaText) return deltaText;

  return "";
}

function extractOpenCodePartText(params: PayloadParams | null | undefined): string {
  const part = extractOpenCodePart(params);
  const text = part?.text;
  if (typeof text === "string" && text) return text;
  return "";
}

function extractOpenCodeToolInput(part: Record<string, unknown> | null): string {
  if (!part) return "";
  for (const key of ["input", "command", "cmd", "script"]) {
    const value = part[key];
    if (typeof value === "string" && value.trim()) {
      return value.trim();
    }
  }

  const args = asRecord(part.args) || asRecord(part.arguments) || asRecord(part.params);
  if (args) {
    for (const key of ["command", "cmd", "script", "input"]) {
      const value = args[key];
      if (typeof value === "string" && value.trim()) {
        return value.trim();
      }
    }
  }

  return "";
}

function mergeOpenCodeText(current: string, incoming: string): string {
  if (!incoming) return current;
  if (!current) return incoming;
  if (incoming === current) return current;
  if (incoming.length > current.length && incoming.startsWith(current)) return incoming;
  return `${current}${incoming}`;
}

function extractCommand(item: CommandItem | null | undefined, params: PayloadParams | null | undefined): string {
  const command = item?.command ?? params?.command;
  if (Array.isArray(command)) {
    return command
      .map((part) => String(part))
      .join(" ")
      .trim();
  }
  if (typeof command === "string") return command.trim();
  return "";
}

function extractFiles(payload: PayloadParams | null | undefined, limit = Number.POSITIVE_INFINITY): string[] {
  const files: string[] = [];
  const addEntry = (entry: unknown): boolean => {
    if (files.length >= limit) return true;
    if (typeof entry === "string" && entry.trim()) {
      files.push(entry.trim());
      return files.length >= limit;
    }
    if (entry && typeof entry === "object") {
      const entryObj = entry as Record<string, unknown>;
      const path = entryObj.path || entryObj.file || entryObj.name;
      if (typeof path === "string" && path.trim()) {
        files.push(path.trim());
        return files.length >= limit;
      }
    }
    return false;
  };
  if (!payload || typeof payload !== "object") return files;
  for (const key of ["files", "fileChanges", "paths"] as Array<keyof PayloadParams>) {
    const value = payload[key];
    if (!Array.isArray(value)) continue;
    for (const entry of value) {
      if (addEntry(entry)) return files;
    }
  }
  for (const key of ["path", "file", "name"]) {
    if (addEntry((payload as Record<string, unknown>)[key as string])) return files;
  }
  return files;
}

function extractCompactPreview(payload: unknown): string {
  if (!payload || typeof payload !== "object") return "";
  const preview = (payload as Record<string, unknown>).preview;
  return typeof preview === "string" ? preview.trim() : "";
}

function summarizeFileChanges(files: string[], totalCount?: number | null): { summary: string; detail: string } {
  const hasTotalCount = typeof totalCount === "number" && Number.isFinite(totalCount);
  const visibleCount = hasTotalCount && totalCount > 4 ? 4 : Math.min(files.length, 4);
  const visible = files.slice(0, visibleCount).filter((value) => value.trim());
  if (visible.length) {
    const effectiveTotal = hasTotalCount ? totalCount : files.length;
    const remaining = Math.max(effectiveTotal - visible.length, 0);
    return {
      summary: remaining > 0 ? `${visible.join(", ")} +${remaining} more` : visible.join(", "),
      detail: hasTotalCount && totalCount > visible.length ? `${totalCount} file changes` : "",
    };
  }
  if (hasTotalCount) {
    return {
      summary: totalCount === 1 ? "1 file change" : `${totalCount} file changes`,
      detail: "",
    };
  }
  return { summary: "File changes", detail: "" };
}

function parseLegacyDiffEntryCount(value: string): number | null {
  const match = value.match(/^(\d+)\s+diff entries?$/i);
  if (!match) return null;
  return Number.parseInt(match[1] || "", 10);
}

function extractErrorMessage(params: PayloadParams | null | undefined): string {
  if (!params || typeof params !== "object") return "";
  const err = params.error;
  if (err && typeof err === "object") {
    const errObj = err as ErrorObject;
    const message = typeof errObj.message === "string" ? errObj.message : "";
    const details =
      typeof errObj.additionalDetails === "string"
        ? errObj.additionalDetails
        : typeof errObj.details === "string"
          ? errObj.details
          : "";
    if (message && details && message !== details) {
      return `${message} (${details})`;
    }
    return message || details;
  }
  if (typeof err === "string") return err;
  if (typeof params.message === "string") return params.message;
  return "";
}

function hasMeaningfulText(summary: string, detail: string): boolean {
  return Boolean(summary.trim() || detail.trim());
}

function inferSignificance(kind: AgentEventKind, method: string): boolean {
  if (kind === "thinking") return true;
  if (kind === "error") return true;
  if (["tool", "command", "file", "output"].includes(kind)) return true;
  if (method.includes("requestApproval")) return true;
  return false;
}

/**
 * Extract output delta text from an event payload.
 */
export function extractOutputDelta(payload: unknown): string {
  const message = payload && typeof payload === "object" ? (payload as EventPayload).message || payload : payload;
  if (!message || typeof message !== "object") return "";
  const method = String((message as EventMessage).method || "").toLowerCase();
  const params = (message as EventMessage).params || {};
  if (method.includes("outputdelta")) {
    const delta = extractOpenCodeDeltaText(params);
    if (delta) return delta;
  }
  if (isOpenCodeMessagePartMethod(method)) {
    return extractOpenCodeDeltaText(params) || extractOpenCodePartText(params);
  }
  return "";
}

/**
 * Parse an app-server event payload into a normalized AgentEvent plus merge hints.
 */
export function parseAppServerEvent(payload: unknown): ParsedAgentEvent | null {
  const message = payload && typeof payload === "object" ? (payload as EventPayload).message || payload : payload;
  if (!message || typeof message !== "object") return null;
  const messageObj = message as EventMessage;
  const method = messageObj.method || "app-server";
  const params = messageObj.params || {};
  const item = (params.item as CommandItem) || {};
  const itemId = params.itemId || item.id || item.itemId || null;
  const receivedAt =
    payload && typeof payload === "object"
      ? (payload as EventPayload).received_at || (payload as EventPayload).receivedAt || Date.now()
      : Date.now();

  if (method === "session.diff") {
    const properties = asRecord((params as Record<string, unknown>).properties) || {};
    const diffEntries = Array.isArray(properties.diff) ? properties.diff : null;
    const rawDiffCount = properties.diff_count;
    let diffCount = typeof rawDiffCount === "number" ? rawDiffCount : null;
    if (diffCount == null) {
      const compactFiles = Array.isArray(properties.files) ? properties.files.length : null;
      diffCount = diffEntries?.length ?? compactFiles;
    }
    const filePayload = diffEntries ? ({ files: diffEntries } as PayloadParams) : (properties as PayloadParams);
    const files = extractFiles(filePayload, 10);
    const fallbackPreview = extractCompactPreview(payload) ||
      (typeof params.message === "string" ? params.message.trim() : "") ||
      (typeof params.status === "string" ? params.status.trim() : "");
    const { summary: fileSummary, detail } = summarizeFileChanges(files, diffCount);
    const diffCountLabel = diffCount === null ? "" : (diffCount === 1 ? "1 file change" : `${diffCount} file changes`);
    let summary = fileSummary;
    let detailText = detail;
    if (!files.length && fallbackPreview) {
      const legacyDiffCount = parseLegacyDiffEntryCount(fallbackPreview);
      const effectiveDiffCount = diffCount ?? legacyDiffCount;
      if (effectiveDiffCount !== null && legacyDiffCount !== null) {
        summary = effectiveDiffCount === 1 ? "1 file change" : `${effectiveDiffCount} file changes`;
      } else if (fallbackPreview !== "diff updated") {
        summary = fallbackPreview;
        if (!detailText && diffCountLabel && fallbackPreview !== diffCountLabel) {
          detailText = diffCountLabel;
        }
      }
    }
    return {
      event: {
        id: (payload as EventPayload)?.id || `${Date.now()}`,
        title: "File change",
        summary,
        detail: detailText,
        kind: "file",
        isSignificant: true,
        time: receivedAt,
        itemId,
        method,
      },
    };
  }

  // Handle reasoning/thinking deltas - accumulate into existing event
  if (method === "item/reasoning/summaryTextDelta") {
    const delta = params.delta || "";
    if (!delta) return null;
    const event: AgentEvent = {
      id: (payload as EventPayload)?.id || `${Date.now()}`,
      title: "Thinking",
      summary: delta,
      detail: "",
      kind: "thinking",
      isSignificant: true,
      time: receivedAt,
      itemId,
      method,
    };
    return { event, mergeStrategy: "append" };
  }

  // Handle reasoning part added (paragraph break)
  if (method === "item/reasoning/summaryPartAdded") {
    const event: AgentEvent = {
      id: (payload as EventPayload)?.id || `${Date.now()}`,
      title: "Thinking",
      summary: "",
      detail: "",
      kind: "thinking",
      isSignificant: true,
      time: receivedAt,
      itemId,
      method,
    };
    return { event, mergeStrategy: "newline" };
  }

  if (method === "message.updated" || method === "message.completed") {
    const role = extractOpenCodeRole(params);
    const messageId = extractOpenCodeMessageId(params);
    if (messageId && role) {
      openCodeMessageRoles[messageId] = role;
      openCodeMessageRolesSeen = true;
    }

    if (role === "user") {
      if (messageId) {
        delete openCodePendingTextByMessage[messageId];
      }
      openCodePendingTextNoId = "";
      return null;
    }

    if (role === "assistant" && messageId && openCodePendingTextByMessage[messageId]) {
      const pendingText = openCodePendingTextByMessage[messageId];
      delete openCodePendingTextByMessage[messageId];
      return {
        event: {
          id: (payload as EventPayload)?.id || `${Date.now()}`,
          title: "Agent",
          summary: pendingText,
          detail: "",
          kind: "output",
          isSignificant: true,
          time: receivedAt,
          itemId: messageId,
          method,
        },
      };
    }

    if (role === "assistant" && openCodePendingTextNoId) {
      const pendingText = openCodePendingTextNoId;
      openCodePendingTextNoId = "";
      return {
        event: {
          id: (payload as EventPayload)?.id || `${Date.now()}`,
          title: "Agent",
          summary: pendingText,
          detail: "",
          kind: "output",
          isSignificant: true,
          time: receivedAt,
          itemId: messageId,
          method,
        },
      };
    }

    const text = extractOpenCodePartText(params) || extractOpenCodeDeltaText(params);
    if (!text) {
      return null;
    }

    const event: AgentEvent = {
      id: (payload as EventPayload)?.id || `${Date.now()}`,
      title: "Agent",
      summary: text,
      detail: "",
      kind: "output",
      isSignificant: true,
      time: receivedAt,
      itemId: messageId,
      method,
    };
    return { event };
  }

  if (isOpenCodeMessagePartMethod(method)) {
    const part = extractOpenCodePart(params);
    const partId = extractOpenCodePartId(params);
    let partType = extractOpenCodePartType(params);
    if (partId && partType) {
      openCodePartTypes[partId] = partType;
    } else if (partId && !partType) {
      partType = openCodePartTypes[partId] || "";
    }
    const openCodeMessageId = extractOpenCodeMessageId(params);
    const knownRole = openCodeMessageId ? openCodeMessageRoles[openCodeMessageId] : "";
    if (knownRole === "user") {
      return null;
    }

    if (partType === "" || partType === "text") {
      const delta = extractOpenCodeDeltaText(params);
      const text = delta || extractOpenCodePartText(params);
      if (!text) return null;
      if (knownRole !== "assistant") {
        if (openCodeMessageId) {
          openCodePendingTextByMessage[openCodeMessageId] = mergeOpenCodeText(
            openCodePendingTextByMessage[openCodeMessageId] || "",
            text
          );
          return null;
        }
        if (openCodeMessageRolesSeen) {
          openCodePendingTextNoId = mergeOpenCodeText(openCodePendingTextNoId, text);
          return null;
        }
      }
      const parsedItemId =
        openCodeMessageId ||
        partId ||
        itemId;
      const event: AgentEvent = {
        id: (payload as EventPayload)?.id || `${Date.now()}`,
        title: "Agent",
        summary: text,
        detail: "",
        kind: "output",
        isSignificant: true,
        time: receivedAt,
        itemId: parsedItemId || null,
        method,
      };
      if (!parsedItemId) return { event };
      if (delta) return { event, mergeStrategy: "append" };
      return { event, mergeStrategy: "replace" };
    }

    if (partType === "reasoning") {
      const delta = extractOpenCodeDeltaText(params);
      const text = delta || extractOpenCodePartText(params);
      if (!text) return null;
      const reasoningItemId =
        partId ||
        openCodeMessageId ||
        itemId;
      const event: AgentEvent = {
        id: (payload as EventPayload)?.id || `${Date.now()}`,
        title: "Thinking",
        summary: text,
        detail: "",
        kind: "thinking",
        isSignificant: true,
        time: receivedAt,
        itemId: reasoningItemId || null,
        method,
      };
      return delta && reasoningItemId ? { event, mergeStrategy: "append" } : { event };
    }

    if (partType === "tool") {
      if (!part) return null;
      const toolName =
        (typeof part.tool === "string" && part.tool) ||
        (typeof part.name === "string" && part.name) ||
        "Tool call";
      const input = extractOpenCodeToolInput(part);
      const state = asRecord(part.state);
      const status = typeof state?.status === "string" ? state.status.trim() : "";
      const exitCode = state?.exitCode;
      const error = asRecord(state?.error)?.message || state?.error;
      const detailParts: string[] = [];
      if (input) detailParts.push(input);
      if (status) detailParts.push(status);
      if (exitCode !== undefined && exitCode !== null) detailParts.push(`exit ${exitCode}`);
      if (typeof error === "string" && error.trim()) detailParts.push(error.trim());

      return {
        event: {
          id: (payload as EventPayload)?.id || `${Date.now()}`,
          title: "Tool",
          summary: toolName,
          detail: detailParts.join(" · "),
          kind: "tool",
          isSignificant: true,
          time: receivedAt,
          itemId:
            ((typeof part.callID === "string" && part.callID) ||
              partId ||
              null),
          method,
        },
      };
    }

    if (partType === "patch") {
      if (!part) return null;
      const files = extractFiles(part as unknown as PayloadParams);
      if (!files.length) return null;
      return {
        event: {
          id: (payload as EventPayload)?.id || `${Date.now()}`,
          title: "File change",
          summary: files.join(", "),
          detail: "",
          kind: "file",
          isSignificant: true,
          time: receivedAt,
          itemId: partId || null,
          method,
        },
      };
    }

    if (partType === "usage") {
      if (!part) return null;
      const totalTokens = part.totalTokens;
      const summaryText =
        typeof totalTokens === "number" ? `tokens used: ${totalTokens}` : "";
      if (!summaryText) return null;
      return {
        event: {
          id: (payload as EventPayload)?.id || `${Date.now()}`,
          title: "Status",
          summary: summaryText,
          detail: "",
          kind: "status",
          isSignificant: false,
          time: receivedAt,
          itemId: partId || null,
          method,
        },
      };
    }
  }

  let title = method;
  let summary = "";
  let detail = "";
  let kind: AgentEventKind = "event";

  // Handle generic status updates
  if (method === "status" || params.status) {
    title = "Status";
    summary = params.status || "Processing";
    kind = "status";
  } else if (method === "item/completed") {
    const itemType = (item as CommandItem).type;
    if (itemType === "commandExecution") {
      title = "Command";
      summary = extractCommand(item as CommandItem, params);
      kind = "command";
      if ((item as CommandItem).exitCode !== undefined && (item as CommandItem).exitCode !== null) {
        detail = `exit ${(item as CommandItem).exitCode}`;
      }
    } else if (itemType === "fileChange") {
      title = "File change";
      const files = extractFiles(item as PayloadParams);
      summary = files.join(", ") || "Updated files";
      kind = "file";
    } else if (itemType === "tool") {
      title = "Tool";
      summary =
        (item as CommandItem).name ||
        (item as CommandItem).tool ||
        (item as CommandItem).id ||
        "Tool call";
      kind = "tool";
    } else if (itemType === "agentMessage") {
      title = "Agent";
      summary = (item as CommandItem).text || "Agent message";
      kind = "output";
    } else {
      title = itemType ? `Item ${itemType}` : "Item completed";
      summary = (item as CommandItem).text || (item as CommandItem).message || "";
    }
  } else if (method === "item/commandExecution/requestApproval") {
    title = "Command approval";
    summary = extractCommand(item as CommandItem, params) || "Approval requested";
    kind = "command";
  } else if (method === "item/fileChange/requestApproval") {
    title = "File approval";
    const files = extractFiles(params);
    summary = files.join(", ") || "Approval requested";
    kind = "file";
  } else if (method === "turn/completed") {
    title = "Turn completed";
    summary = params.status || "completed";
    kind = "status";
  } else if (method === "error") {
    title = "Error";
    summary = extractErrorMessage(params) || "App-server error";
    kind = "error";
  } else if (method.includes("outputDelta")) {
    title = "Output";
    summary = params.delta || params.text || "";
    kind = "output";
  } else if (params.delta) {
    title = "Delta";
    summary = params.delta;
  }

  const summaryText = typeof summary === "string" ? summary : String(summary ?? "");
  const detailText = typeof detail === "string" ? detail : String(detail ?? "");
  const meaningful = hasMeaningfulText(summaryText, detailText);
  const isStarted = method.includes("item/started");
  if (!meaningful && isStarted) {
    return null;
  }
  if (!meaningful) {
    return null;
  }
  const isSignificant = inferSignificance(kind, method);

  const event: AgentEvent = {
    id: (payload as EventPayload)?.id || `${Date.now()}`,
    title,
    summary: summaryText,
    detail: detailText,
    kind,
    isSignificant,
    time: receivedAt,
    itemId,
    method,
  };
  return { event };
}
