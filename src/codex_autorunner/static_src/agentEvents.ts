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
  mergeStrategy?: "append" | "newline";
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

function extractFiles(payload: PayloadParams | null | undefined): string[] {
  const files: string[] = [];
  const addEntry = (entry: unknown): void => {
    if (typeof entry === "string" && entry.trim()) {
      files.push(entry.trim());
      return;
    }
    if (entry && typeof entry === "object") {
      const entryObj = entry as Record<string, unknown>;
      const path = entryObj.path || entryObj.file || entryObj.name;
      if (typeof path === "string" && path.trim()) {
        files.push(path.trim());
      }
    }
  };
  if (!payload || typeof payload !== "object") return files;
  for (const key of ["files", "fileChanges", "paths"] as Array<keyof PayloadParams>) {
    const value = payload[key];
    if (Array.isArray(value)) {
      value.forEach(addEntry);
    }
  }
  for (const key of ["path", "file", "name"]) {
    addEntry((payload as Record<string, unknown>)[key as string]);
  }
  return files;
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
  if (!method.includes("outputdelta")) return "";
  const params = (message as EventMessage).params || {};
  if (typeof params.delta === "string") return params.delta;
  if (typeof params.text === "string") return params.text;
  if (typeof params.output === "string") return params.output;
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
