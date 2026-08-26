/**
 * Claude Code hook HTTP normalizer.
 *
 * Payload shapes verified against https://code.claude.com/docs/en/hooks:
 *   common: session_id, prompt_id?, transcript_path, cwd, permission_mode,
 *           hook_event_name, agent_id?, agent_type?, effort?
 *   SessionStart:      + reason: startup|resume|clear|compact|fork, model?
 *   SessionEnd:        + reason: clear|resume|logout|prompt_input_exit|other
 *   Stop:              + last_assistant_message, stop_reason: end_turn|max_tokens
 *   Notification:      + notification_type, notification_data
 *   PreToolUse:        + tool_name, tool_input, tool_use_id
 *   PermissionRequest: + tool_name, tool_input, tool_use_id
 *
 * Decision output (2xx JSON body) for PreToolUse/PermissionRequest:
 *   { hookSpecificOutput: { hookEventName, permissionDecision: allow|deny|escalate,
 *                           permissionDecisionReason } }
 * A 2xx with an empty body is "success, no decision" — that is CAR's timeout path.
 */
import type { CarEvent, EventType, ResponseChannel, Severity } from "../contract/events.ts";
import type { PermissionDecision } from "../permission_park.ts";
import {
  buildEvent,
  isoTs,
  isRecord,
  minuteBucket,
  NormalizeError,
  obj,
  shortHash,
  str,
  type NormalizeContext,
} from "./normalize.ts";

export const CLAUDE_ADAPTER = "hook-http";

/** Default Claude Code hook timeout for `http` hooks that ship no hint. */
export const DEFAULT_HOOK_TIMEOUT_MS = 60_000;
/** Margin subtracted from the hook timeout so CAR always answers before Claude gives up. */
export const PARK_MARGIN_MS = 5_000;
export const MIN_PARK_MS = 250;
export const MAX_PARK_MS = 600_000;

export interface ClaudeNormalizeResult {
  event: CarEvent;
  /** True for PermissionRequest: the HTTP response is parked awaiting a decision. */
  park: boolean;
  hookEventName: string;
}

export interface ClaudeNormalizeOptions {
  /**
   * Effective hook timeout for this request, already resolved from query/header/
   * payload hints by the caller. Passing it in keeps the `deadline_ms` recorded
   * on the event identical to the deadline the HTTP handler actually parks for.
   */
  hookTimeoutMs?: number;
}

export function normalizeClaude(
  raw: unknown,
  ctx: NormalizeContext,
  opts: ClaudeNormalizeOptions = {},
): ClaudeNormalizeResult {
  if (!isRecord(raw)) throw new NormalizeError("claude hook payload must be an object");

  const hookEventName = str(raw, "hook_event_name", "hookEventName");
  if (!hookEventName) throw new NormalizeError("claude hook payload is missing hook_event_name");

  const sessionId = str(raw, "session_id", "sessionId");
  if (!sessionId) throw new NormalizeError("claude hook payload is missing session_id");

  const cwd = str(raw, "cwd");
  const host = ctx.host;
  // Hook payloads carry no timestamp; the daemon clock is the authority.
  const ts = isoTs(raw["ts"] ?? raw["timestamp"], ctx.now);

  const mapped = mapHook(hookEventName, raw, ctx, sessionId, opts.hookTimeoutMs ?? hookTimeoutHintMs(raw));

  const event = buildEvent({
    idempotency_key: mapped.idempotencyKey,
    ts,
    vendor: "claude-code",
    adapter: CLAUDE_ADAPTER,
    host,
    session: {
      vendor: "claude-code",
      native_id: sessionId,
      host,
      ...(cwd ? { cwd } : {}),
      ...(mapped.sessionTitle ? { title: mapped.sessionTitle } : {}),
    },
    type: mapped.type,
    severity: mapped.severity,
    requires_response: mapped.requiresResponse,
    response_channel: mapped.responseChannel,
    title: mapped.title,
    body: mapped.body,
    payload: {
      hook_event_name: hookEventName,
      ...(str(raw, "prompt_id") ? { prompt_id: str(raw, "prompt_id") } : {}),
      ...(str(raw, "transcript_path") ? { transcript_path: str(raw, "transcript_path") } : {}),
      ...(str(raw, "permission_mode") ? { permission_mode: str(raw, "permission_mode") } : {}),
      ...(str(raw, "agent_id") ? { agent_id: str(raw, "agent_id") } : {}),
      ...(str(raw, "agent_type") ? { agent_type: str(raw, "agent_type") } : {}),
      ...(str(raw, "model") ? { model: str(raw, "model") } : {}),
      ...(str(raw, "reason") ? { reason: str(raw, "reason") } : {}),
      ...(str(raw, "stop_reason") ? { stop_reason: str(raw, "stop_reason") } : {}),
      ...(str(raw, "notification_type") ? { notification_type: str(raw, "notification_type") } : {}),
      ...(obj(raw, "notification_data") ? { notification_data: obj(raw, "notification_data") } : {}),
      ...(str(raw, "tool_name") ? { tool_name: str(raw, "tool_name") } : {}),
      ...(str(raw, "tool_use_id") ? { tool_use_id: str(raw, "tool_use_id") } : {}),
      ...(obj(raw, "tool_input") ? { tool_input: obj(raw, "tool_input") } : {}),
    },
  });

  return { event, park: hookEventName === "PermissionRequest", hookEventName };
}

interface HookMapping {
  type: EventType;
  severity: Severity;
  requiresResponse: boolean;
  responseChannel: ResponseChannel | null;
  title: string;
  body: string;
  idempotencyKey: string;
  sessionTitle?: string;
}

function mapHook(
  hook: string,
  raw: Record<string, unknown>,
  ctx: NormalizeContext,
  sessionId: string,
  hookTimeoutMs: number,
): HookMapping {
  const cwd = str(raw, "cwd");
  const shortCwd = cwd ? cwd.split("/").filter(Boolean).slice(-1)[0] ?? cwd : undefined;

  switch (hook) {
    case "SessionStart": {
      const reason = str(raw, "reason") ?? "startup";
      return {
        type: "session.started",
        severity: "info",
        requiresResponse: false,
        responseChannel: null,
        title: `Claude Code session ${reason}${shortCwd ? ` in ${shortCwd}` : ""}`,
        body: "",
        idempotencyKey: key(sessionId, hook, reason),
        sessionTitle: shortCwd,
      };
    }

    case "SessionEnd": {
      const reason = str(raw, "reason") ?? "other";
      return {
        type: "session.ended",
        severity: "info",
        requiresResponse: false,
        responseChannel: null,
        title: `Claude Code session ended (${reason})`,
        body: "",
        idempotencyKey: key(sessionId, hook, reason),
        sessionTitle: shortCwd,
      };
    }

    case "Stop": {
      // The agent finished its turn and is now waiting on a human. That is
      // attention.idle in the contract, and a reply can still reach it by resume.
      const stopReason = str(raw, "stop_reason") ?? "end_turn";
      const last = str(raw, "last_assistant_message") ?? "";
      const discriminator = str(raw, "prompt_id") ?? `${stopReason}:${shortHash(last)}`;
      return {
        type: "attention.idle",
        severity: "notice",
        requiresResponse: false,
        responseChannel: resumeChannel(raw, sessionId),
        title: `Claude Code turn finished (${stopReason})`,
        body: last,
        idempotencyKey: key(sessionId, hook, discriminator),
        sessionTitle: shortCwd,
      };
    }

    case "Notification":
      return mapNotification(raw, ctx, sessionId, shortCwd);

    case "PreToolUse": {
      const toolName = str(raw, "tool_name") ?? "tool";
      const toolUseId = str(raw, "tool_use_id");
      return {
        type: "progress",
        severity: "info",
        requiresResponse: false,
        responseChannel: null,
        title: `${toolName}`,
        body: describeToolInput(obj(raw, "tool_input")),
        idempotencyKey: key(
          sessionId,
          hook,
          toolUseId ?? `${toolName}:${shortHash(obj(raw, "tool_input") ?? {}, minuteBucket(ctx.now))}`,
        ),
        sessionTitle: shortCwd,
      };
    }

    case "PermissionRequest": {
      const toolName = str(raw, "tool_name") ?? "tool";
      const toolUseId = str(raw, "tool_use_id");
      return {
        type: "attention.permission",
        severity: "attention",
        requiresResponse: true,
        responseChannel: {
          kind: "claude-hook-http",
          hint: {
            ...(toolUseId ? { tool_use_id: toolUseId } : {}),
            session_id: sessionId,
            tool_name: toolName,
            deadline_ms: parkDeadlineMs(hookTimeoutMs),
          },
        },
        title: `Permission: ${toolName}${describeToolInputShort(obj(raw, "tool_input"))}`,
        body: describeToolInput(obj(raw, "tool_input")),
        idempotencyKey: key(
          sessionId,
          hook,
          toolUseId ?? `${toolName}:${shortHash(obj(raw, "tool_input") ?? {}, minuteBucket(ctx.now))}`,
        ),
        sessionTitle: shortCwd,
      };
    }

    default:
      // Unknown/new hook events land as notes rather than being dropped.
      return {
        type: "note",
        severity: "info",
        requiresResponse: false,
        responseChannel: null,
        title: `Claude Code hook ${hook}`,
        body: "",
        idempotencyKey: key(sessionId, hook, shortHash(raw, minuteBucket(ctx.now))),
        sessionTitle: shortCwd,
      };
  }
}

function mapNotification(
  raw: Record<string, unknown>,
  ctx: NormalizeContext,
  sessionId: string,
  shortCwd: string | undefined,
): HookMapping {
  const notificationType = str(raw, "notification_type") ?? "unknown";
  const data = obj(raw, "notification_data") ?? {};
  const message = str(data, "message", "text", "prompt", "question") ?? "";

  // Notifications carry no stable id, so the key is content + minute bucket:
  // an identical idle_prompt an hour later is legitimately a new event.
  const discriminator = `${notificationType}:${shortHash(data)}:${minuteBucket(ctx.now)}`;
  const idempotencyKey = key(sessionId, "Notification", discriminator);

  const base = {
    requiresResponse: false,
    responseChannel: null as ResponseChannel | null,
    body: message,
    idempotencyKey,
    sessionTitle: shortCwd,
  };

  switch (notificationType) {
    case "permission_prompt":
      return {
        ...base,
        type: "attention.permission",
        severity: "attention",
        requiresResponse: true,
        // A Notification hook cannot return a decision — only PermissionRequest
        // can. So the reply route is resume/file, never claude-hook-http.
        responseChannel: resumeChannel(raw, sessionId),
        title: message || "Claude Code is asking for permission",
      };

    case "idle_prompt":
      return {
        ...base,
        type: "attention.idle",
        severity: "attention",
        requiresResponse: true,
        responseChannel: resumeChannel(raw, sessionId),
        title: message || "Claude Code is waiting for input",
      };

    case "agent_needs_input":
    case "elicitation_dialog":
    case "elicitation_url_dialog":
      return {
        ...base,
        type: "attention.question",
        severity: "attention",
        requiresResponse: true,
        responseChannel: resumeChannel(raw, sessionId),
        title: message || `Claude Code needs input (${notificationType})`,
      };

    case "agent_completed":
      return {
        ...base,
        type: "progress",
        severity: "info",
        title: message || "Claude Code subagent completed",
      };

    default:
      return {
        ...base,
        type: "note",
        severity: "notice",
        title: message || `Claude Code notification (${notificationType})`,
      };
  }
}

function resumeChannel(raw: Record<string, unknown>, sessionId: string): ResponseChannel {
  const cwd = str(raw, "cwd");
  const transcript = str(raw, "transcript_path");
  return {
    kind: "claude-resume",
    hint: {
      session_id: sessionId,
      ...(cwd ? { cwd } : {}),
      ...(transcript ? { transcript_path: transcript } : {}),
    },
  };
}

function key(sessionId: string, hook: string, discriminator: string): string {
  return `claude-code:${sessionId}:${hook}:${discriminator}`;
}

/* ------------------------------------------------------------- park timing */

/**
 * Claude Code's hook payload does not include the configured timeout, so the
 * hint is read (in priority order) from an explicit query param, a header the
 * hook config can set, or a payload field — then the margin is applied.
 * ASSUMPTION: none of these is guaranteed present; 60s is the documented default
 * for `http` hooks in most settings, which yields the 55s park in the design.
 */
export function readHookTimeoutHint(
  raw: Record<string, unknown>,
  headers?: { get(name: string): string | null },
  url?: URL,
): number | undefined {
  const fromUrl = url
    ? readMs(url.searchParams.get("timeout_ms"), url.searchParams.get("timeout"))
    : undefined;
  if (fromUrl !== undefined) return fromUrl;

  const fromHeaders = headers
    ? readMs(
        headers.get("x-car-hook-timeout-ms"),
        headers.get("x-claude-hook-timeout") ?? headers.get("x-hook-timeout"),
      )
    : undefined;
  if (fromHeaders !== undefined) return fromHeaders;

  return readMs(
    valueOf(raw["timeout_ms"]),
    valueOf(raw["timeout"] ?? raw["hook_timeout"] ?? raw["timeout_seconds"]),
  );
}

/** As above, but falls back to the documented default rather than undefined. */
export function hookTimeoutHintMs(
  raw: Record<string, unknown>,
  headers?: { get(name: string): string | null },
  url?: URL,
): number {
  return readHookTimeoutHint(raw, headers, url) ?? DEFAULT_HOOK_TIMEOUT_MS;
}

function valueOf(v: unknown): string | null {
  if (typeof v === "number" && Number.isFinite(v)) return String(v);
  if (typeof v === "string" && v.trim() !== "") return v;
  return null;
}

function readMs(millis: string | null, seconds: string | null): number | undefined {
  if (millis !== null && Number.isFinite(Number(millis))) return Number(millis);
  if (seconds !== null && Number.isFinite(Number(seconds))) return Number(seconds) * 1000;
  return undefined;
}

/** Hook timeout minus the safety margin, clamped to a sane window. */
export function parkDeadlineMs(hookTimeoutMs: number): number {
  const raw = hookTimeoutMs - PARK_MARGIN_MS;
  return Math.min(MAX_PARK_MS, Math.max(MIN_PARK_MS, raw));
}

/* ---------------------------------------------------------- hook responses */

/** The JSON body Claude Code expects when CAR resolves a parked PermissionRequest. */
export function hookDecisionBody(decision: PermissionDecision): Record<string, unknown> {
  const allow = decision.decision === "allow";
  return {
    hookSpecificOutput: {
      hookEventName: "PermissionRequest",
      permissionDecision: allow ? "allow" : "deny",
      // permissionDecisionReason is required for deny; always sending it is harmless.
      permissionDecisionReason:
        decision.reason ?? (allow ? "Approved via CAR" : "Denied via CAR"),
    },
  };
}

/** "No decision": a 2xx empty object, so Claude falls back to its local prompt. */
export const NO_DECISION_BODY: Record<string, never> = {};

/* --------------------------------------------------------------- summaries */

function describeToolInput(input: Record<string, unknown> | undefined): string {
  if (!input) return "";
  const command = str(input, "command");
  if (command) {
    const description = str(input, "description");
    return description ? `${command}\n\n${description}` : command;
  }
  const file = str(input, "file_path", "path", "notebook_path");
  if (file) return file;
  const url = str(input, "url");
  if (url) return url;
  try {
    return JSON.stringify(input, null, 2);
  } catch {
    return "";
  }
}

function describeToolInputShort(input: Record<string, unknown> | undefined): string {
  if (!input) return "";
  const summary = str(input, "command") ?? str(input, "file_path", "path") ?? str(input, "url");
  return summary ? `: ${summary.split("\n")[0]}` : "";
}
