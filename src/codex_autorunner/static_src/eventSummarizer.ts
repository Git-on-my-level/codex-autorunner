import type { AgentEvent } from "./agentEvents.js";

export const COMPACT_MAX_ACTIONS = 10;
export const COMPACT_MAX_TEXT_LENGTH = 80;

export type CompactActionStatus = "done" | "fail" | "warn" | "running" | "update";

export interface CompactAction {
  icon: string;
  label: string;
  text: string;
  status: CompactActionStatus;
}

export interface EventSummary {
  header: string;
  actions: CompactAction[];
  thinkingText?: string;
}

const STATUS_ICONS: Record<string, string> = {
  done: "✓",
  fail: "✗",
  warn: "⚠",
  running: "▸",
  update: "↻",
  thinking: "🧠",
};

function normalizeText(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}

function truncateText(value: string, maxLength: number): string {
  if (value.length <= maxLength) return value;
  return `${value.slice(0, maxLength).trim()}…`;
}

function formatElapsed(seconds: number): string {
  const total = Math.max(Math.floor(seconds), 0);
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  const secs = total % 60;
  if (minutes < 60) return `${minutes}m ${secs}s`;
  const hours = Math.floor(minutes / 60);
  const remaining = minutes % 60;
  return `${hours}h ${remaining}m`;
}

function deriveHeaderLabel(events: AgentEvent[]): string {
  const lastError = [...events].reverse().find((evt) => evt.kind === "error");
  if (lastError) return "error";
  const lastStatus = [...events].reverse().find((evt) => evt.kind === "status" && evt.summary);
  if (lastStatus?.summary) return lastStatus.summary;
  return "working";
}

function eventToAction(event: AgentEvent, maxTextLength: number): CompactAction | null {
  if (event.isSignificant === false && event.kind !== "thinking") {
    return null;
  }
  const rawText = normalizeText(event.summary || event.detail || "");
  if (!rawText) return null;

  let label: string = event.kind;
  let status: CompactActionStatus = "update";

  if (event.kind === "command") {
    label = "command";
    status = event.method.includes("requestApproval") ? "warn" : "done";
  } else if (event.kind === "tool") {
    label = "tool";
    status = "done";
  } else if (event.kind === "file") {
    label = "files";
    status = event.method.includes("requestApproval") ? "warn" : "done";
  } else if (event.kind === "output") {
    label = "output";
    status = "update";
  } else if (event.kind === "thinking") {
    label = "thinking";
    status = "update";
  } else if (event.kind === "error") {
    label = "error";
    status = "fail";
  } else if (event.kind === "status") {
    label = "status";
    status = "running";
  } else {
    label = "event";
    status = "update";
  }

  const text = truncateText(rawText, maxTextLength);
  const icon = label === "thinking" ? STATUS_ICONS.thinking : STATUS_ICONS[status] || STATUS_ICONS.running;

  return { icon, label, text, status };
}

export function summarizeEvents(
  events: AgentEvent[],
  options: { maxActions?: number; maxTextLength?: number; now?: number; contextUsagePercent?: number; startTime?: number } = {}
): EventSummary {
  const maxActions = options.maxActions ?? COMPACT_MAX_ACTIONS;
  const maxTextLength = options.maxTextLength ?? COMPACT_MAX_TEXT_LENGTH;
  const actions: CompactAction[] = [];
  let lastOutputIndex: number | null = null;
  let lastThinkingIndex: number | null = null;

  events.forEach((event) => {
    const action = eventToAction(event, maxTextLength);
    if (!action) return;
    if (action.label === "output") {
      if (lastOutputIndex != null) {
        actions[lastOutputIndex] = action;
      } else {
        actions.push(action);
        lastOutputIndex = actions.length - 1;
      }
      return;
    }
    if (action.label === "thinking") {
      if (lastThinkingIndex != null) {
        actions[lastThinkingIndex] = action;
      } else {
        actions.push(action);
        lastThinkingIndex = actions.length - 1;
      }
      return;
    }
    actions.push(action);
    lastOutputIndex = null;
  });

  let thinkingAction: CompactAction | null = null;
  if (lastThinkingIndex != null && actions[lastThinkingIndex]) {
    thinkingAction = actions[lastThinkingIndex];
  }

  let trimmedActions = actions.slice(-maxActions);
  if (thinkingAction) {
    trimmedActions = trimmedActions.filter((action) => action !== thinkingAction);
    trimmedActions.push(thinkingAction);
    if (maxActions > 0 && trimmedActions.length > maxActions) {
      trimmedActions = trimmedActions.slice(-maxActions);
    }
  }

  const step = actions.length;
  const headerParts: string[] = [deriveHeaderLabel(events)];
  const now = options.now ?? Date.now();
  const startTime = options.startTime ?? (events.length ? Math.min(...events.map((evt) => evt.time || now)) : null);
  if (startTime != null) {
    const elapsed = (now - startTime) / 1000;
    headerParts.push(formatElapsed(elapsed));
  }
  if (step) {
    headerParts.push(`step ${step}`);
  }
  if (options.contextUsagePercent != null) {
    headerParts.push(`ctx ${options.contextUsagePercent}%`);
  }

  return {
    header: headerParts.join(" · "),
    actions: trimmedActions,
    thinkingText: thinkingAction?.text,
  };
}

export function renderCompactSummary(summary: EventSummary): string {
  const lines: string[] = [];
  if (summary.header) {
    lines.push(summary.header);
  }
  summary.actions.forEach((action) => {
    if (!action.text) return;
    if (action.label === "thinking") {
      lines.push(`${STATUS_ICONS.thinking} ${action.text}`);
      return;
    }
    lines.push(`${action.icon} ${action.label}: ${action.text}`);
  });
  return lines.join("\n");
}
