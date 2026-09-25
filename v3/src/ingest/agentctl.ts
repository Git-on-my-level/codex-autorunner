/**
 * agentctl `subscribe` webhook normalizer.
 *
 * Grounded against agentctl v0.3.9 on this host:
 *   - `agentctl help subscribe` — "Create durable at-least-once callback delivery
 *     for execution events", default filter kinds = terminal, attention, artifact.
 *   - `agentctl events <exec-id>` — real journal rows, whose shape is:
 *       { schema_version, id: "event-…", origin_host_id: "host-…", execution_id: "exec-…",
 *         sequence, ordering: "observation"|"source", source_position?, kind, state,
 *         source_state?, authority, adapter, occurred_at, observed_at,
 *         dedupe_key, dedupe_version, payload }
 *   - observed kinds in the live journal: started | progress | health | terminal.
 *     `attention` and `artifact` are advertised by `subscribe`/`events --kind` but
 *     no sample exists in this host's history, so their payloads are read
 *     defensively (see ATTENTION/ARTIFACT sections).
 *
 * Delivery is at-least-once, so idempotency comes from the agentctl event id.
 */
import type { CarEvent, EventType, ResponseChannel, Severity } from "../contract/events.ts";
import { isAgentRunFailureState } from "../contract/lifecycle.ts";
import {
  buildEvent,
  clampString,
  isRecord,
  isoTs,
  minuteBucket,
  NormalizeError,
  num,
  obj,
  shortHash,
  str,
  type NormalizeContext,
} from "./normalize.ts";

export const AGENTCTL_ADAPTER = "agentctl-subscribe";

/**
 * agentctl may deliver a bare event, an array, or a callback envelope. The
 * envelope's exact field names are not published by `agentctl schema list`
 * (the JSON files ship inside the binary), so we unwrap structurally: any
 * object carrying `events`/`event`/`payload.events` is treated as an envelope
 * and its delivery metadata is preserved on each derived event's payload.
 */
export function unwrapAgentctlDelivery(raw: unknown): {
  events: unknown[];
  delivery: Record<string, unknown>;
} {
  if (Array.isArray(raw)) return { events: raw, delivery: {} };
  if (!isRecord(raw)) throw new NormalizeError("agentctl payload must be an object or array");

  // A bare journal event: has execution_id + kind and no nested carrier.
  if (typeof raw["kind"] === "string" && typeof raw["execution_id"] === "string") {
    return { events: [raw], delivery: {} };
  }

  const delivery: Record<string, unknown> = {};
  for (const key of [
    "delivery_id",
    "subscription_id",
    "attempt",
    "origin_host_id",
    "schema_version",
    "expires_at",
  ]) {
    if (raw[key] !== undefined) delivery[key] = raw[key];
  }

  const nested = raw["events"] ?? raw["event"] ?? obj(raw, "payload")?.["events"] ?? obj(raw, "payload")?.["event"];
  if (Array.isArray(nested)) return { events: nested, delivery };
  if (isRecord(nested)) return { events: [nested], delivery };

  throw new NormalizeError("agentctl payload carries no recognizable event", {
    keys: Object.keys(raw).slice(0, 20),
  });
}

/** Normalize one agentctl delivery into zero or more canonical events. */
export function normalizeAgentctl(raw: unknown, ctx: NormalizeContext): CarEvent[] {
  const { events, delivery } = unwrapAgentctlDelivery(raw);
  const out: CarEvent[] = [];
  for (const item of events) {
    const ev = normalizeAgentctlEvent(item, ctx, delivery);
    if (ev) out.push(ev);
  }
  if (out.length === 0) throw new NormalizeError("agentctl delivery contained no usable events");
  return out;
}

function normalizeAgentctlEvent(
  raw: unknown,
  ctx: NormalizeContext,
  delivery: Record<string, unknown>,
): CarEvent | null {
  if (!isRecord(raw)) throw new NormalizeError("agentctl event must be an object");

  const executionId = str(raw, "execution_id", "executionId");
  if (!executionId) throw new NormalizeError("agentctl event is missing execution_id");

  const kind = (str(raw, "kind") ?? "progress").toLowerCase();
  const state = (str(raw, "state") ?? "").toLowerCase();
  const ordering = str(raw, "ordering") ?? "";
  const sourceState = str(raw, "source_state", "sourceState");
  const adapter = str(raw, "adapter");
  const sequence = num(raw, "sequence");
  const eventId = str(raw, "id", "event_id");
  const host =
    str(raw, "origin_host_id", "originHostId") ??
    (typeof delivery["origin_host_id"] === "string" ? (delivery["origin_host_id"] as string) : undefined) ??
    ctx.host;

  const ts = isoTs(raw["occurred_at"] ?? raw["observed_at"] ?? raw["ts"], ctx.now);
  const payloadBlob = obj(raw, "payload") ?? {};

  const mapped = mapKind(kind, { state, ordering, sourceState, payload: payloadBlob });
  if (!mapped) return null;

  const responseChannel: ResponseChannel | null = mapped.responseChannel
    ? {
        kind: mapped.responseChannel,
        hint: {
          execution_id: executionId,
          ...(adapter ? { adapter } : {}),
          ...(mapped.hint ?? {}),
        },
      }
    : null;

  return buildEvent({
    idempotency_key: agentctlIdempotencyKey(eventId, executionId, sequence, kind, raw, ctx),
    ts,
    vendor: "agentctl",
    adapter: AGENTCTL_ADAPTER,
    host,
    session: {
      vendor: "agentctl",
      native_id: executionId,
      host,
      ...(str(raw, "cwd") ? { cwd: str(raw, "cwd")! } : {}),
      ...(str(raw, "repo") ? { repo: str(raw, "repo")! } : {}),
      ...(agentctlTitle(raw, adapter) ? { title: agentctlTitle(raw, adapter)! } : {}),
    },
    type: mapped.type,
    severity: mapped.severity,
    requires_response: mapped.requiresResponse,
    response_channel: responseChannel,
    title: mapped.title(executionId, adapter, state, sourceState, payloadBlob),
    body: mapped.body(payloadBlob),
    payload: {
      agentctl: {
        kind,
        state,
        ordering,
        ...(sourceState ? { source_state: sourceState } : {}),
        ...(adapter ? { adapter } : {}),
        ...(sequence !== undefined ? { sequence } : {}),
        ...(eventId ? { event_id: eventId } : {}),
        ...(str(raw, "authority") ? { authority: str(raw, "authority") } : {}),
        ...(Array.isArray(raw["labels"]) ? { labels: raw["labels"] } : {}),
        ...(str(raw, "backend_version", "runtime_version") ?? str(payloadBlob, "backend_version", "runtime_version")
          ? { runtime: str(raw, "backend_version", "runtime_version") ?? str(payloadBlob, "backend_version", "runtime_version") }
          : {}),
        ...(str(raw, "profile") ?? str(payloadBlob, "profile")
          ? { profile: str(raw, "profile") ?? str(payloadBlob, "profile") }
          : {}),
        ...(str(raw, "model") ?? str(payloadBlob, "model")
          ? { model: str(raw, "model") ?? str(payloadBlob, "model") }
          : {}),
        ...(str(raw, "config_fingerprint") ?? str(payloadBlob, "config_fingerprint")
          ? { config_fingerprint: str(raw, "config_fingerprint") ?? str(payloadBlob, "config_fingerprint") }
          : {}),
        ...(str(raw, "worktree") ?? str(payloadBlob, "worktree")
          ? { worktree: str(raw, "worktree") ?? str(payloadBlob, "worktree") }
          : {}),
        ...(str(raw, "dedupe_key") ? { dedupe_key: str(raw, "dedupe_key") } : {}),
        payload: payloadBlob,
      },
      ...(Object.keys(delivery).length > 0 ? { delivery } : {}),
    },
  });
}

interface KindMapping {
  type: EventType;
  severity: Severity;
  requiresResponse: boolean;
  responseChannel: ResponseChannel["kind"] | null;
  hint?: Record<string, unknown>;
  title: (
    execId: string,
    adapter: string | undefined,
    state: string,
    sourceState: string | undefined,
    payload: Record<string, unknown>,
  ) => string;
  body: (payload: Record<string, unknown>) => string;
}

function mapKind(
  kind: string,
  info: {
    state: string;
    ordering: string;
    sourceState: string | undefined;
    payload: Record<string, unknown>;
  },
): KindMapping | null {
  switch (kind) {
    /**
     * Exec lifecycle. The journal emits `started` twice over: once with
     * ordering="observation" (sequence 1, payload {accepted:true}) when agentctl
     * actually launched the argv, and repeatedly with ordering="source" as the
     * native adapter reports thread.started/turn.started. Only the observation
     * row is a real session start; the rest are progress, or CAR would mint a
     * dozen session.started events per run.
     */
    case "started":
    case "launched":
      if (info.ordering === "source") return progressMapping();
      return {
        type: "session.started",
        severity: "info",
        requiresResponse: false,
        responseChannel: null,
        title: (execId, adapter) => `agentctl started ${adapter ?? "execution"} ${execId}`,
        body: () => "",
      };

    case "terminal": {
      const bad = isAgentRunFailureState(info.state);
      return {
        type: "session.ended",
        severity: bad ? "attention" : "info",
        requiresResponse: false,
        // A reply to agentctl work is a fresh bounded execution, so keep the
        // channel on ended events: that is how triage continues failed work.
        responseChannel: "agentctl-run",
        title: (execId, adapter, state) =>
          `agentctl ${adapter ?? "execution"} ${state || "ended"}: ${execId}`,
        body: (payload) => {
          const code = str(payload, "failure_code", "error_code");
          const reason = str(payload, "reason", "message", "error");
          return [code ? `failure_code: ${code}` : "", reason ?? ""].filter(Boolean).join("\n");
        },
      };
    }

    case "attention": {
      const flavor = attentionFlavor(info.payload, info.sourceState);
      return {
        type: flavor.type,
        severity: flavor.severity,
        requiresResponse: true,
        responseChannel: "agentctl-run",
        hint: flavor.hint,
        title: (execId, adapter, _state, _sourceState, payload) =>
          str(payload, "title", "question", "prompt", "summary", "message") ??
          `agentctl ${adapter ?? "execution"} needs attention: ${execId}`,
        body: (payload) =>
          str(payload, "body", "detail", "details", "question", "prompt", "message") ?? "",
      };
    }

    case "artifact":
      return {
        type: "artifact",
        severity: "info",
        requiresResponse: false,
        responseChannel: null,
        title: (execId, adapter, _state, _sourceState, payload) =>
          str(payload, "path", "name", "title", "uri", "url") ??
          `agentctl ${adapter ?? "execution"} artifact: ${execId}`,
        body: (payload) => str(payload, "description", "summary", "body") ?? "",
      };

    case "health":
      return {
        type: "heartbeat",
        severity: "info",
        requiresResponse: false,
        responseChannel: null,
        title: () => "",
        body: () => "",
      };

    case "progress":
      return progressMapping();

    default:
      // Unknown kinds are kept as progress rather than dropped: agentctl's event
      // vocabulary is additive, and silently discarding is worse than a low-value row.
      return progressMapping();
  }
}

function progressMapping(): KindMapping {
  return {
    type: "progress",
    severity: "info",
    requiresResponse: false,
    responseChannel: null,
    title: (_execId, _adapter, _state, sourceState) => sourceState ?? "",
    body: () => "",
  };
}

/**
 * agentctl's `attention` payload is not in this host's journal history, so the
 * discriminator is read defensively across the plausible field names; anything
 * unrecognized degrades to attention.question, which is the safe default (a
 * free-text answer always works, a mis-typed permission would not).
 */
function attentionFlavor(
  payload: Record<string, unknown>,
  sourceState: string | undefined,
): { type: EventType; severity: Severity; hint: Record<string, unknown> } {
  const marker = [
    str(payload, "attention_kind", "attention_type", "type", "reason", "category"),
    sourceState,
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();

  const hint: Record<string, unknown> = {};
  const toolUseId = str(payload, "tool_use_id", "toolUseId", "call_id");
  if (toolUseId) hint["tool_use_id"] = toolUseId;

  if (/permission|approval|approve|consent|authoriz/.test(marker)) {
    return { type: "attention.permission", severity: "attention", hint };
  }
  if (/idle|waiting|stalled|input_needed/.test(marker)) {
    return { type: "attention.idle", severity: "attention", hint };
  }
  if (/error|failure|crash|fault/.test(marker)) {
    return { type: "attention.error", severity: "urgent", hint };
  }
  return { type: "attention.question", severity: "attention", hint };
}

function agentctlTitle(raw: Record<string, unknown>, adapter: string | undefined): string | undefined {
  const labels = raw["labels"];
  if (Array.isArray(labels) && labels.length > 0 && typeof labels[0] === "string") {
    return clampString(labels.filter((l) => typeof l === "string").join(" "), 512);
  }
  return undefined;
}

/**
 * agentctl event ids are unique and stable across redelivery, which is exactly
 * what at-least-once webhook delivery needs. Falls back to (execution, sequence),
 * then to a content hash bucketed to the minute.
 */
export function agentctlIdempotencyKey(
  eventId: string | undefined,
  executionId: string,
  sequence: number | undefined,
  kind: string,
  raw: Record<string, unknown>,
  ctx: NormalizeContext,
): string {
  if (eventId) return `agentctl:${eventId}`;
  if (sequence !== undefined) return `agentctl:${executionId}:${sequence}`;
  return `agentctl:${executionId}:${kind}:${shortHash(raw, minuteBucket(ctx.now))}`;
}
