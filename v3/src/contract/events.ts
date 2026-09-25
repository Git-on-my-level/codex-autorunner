/**
 * car.event.v1 — the public wire contract.
 *
 * FROZEN: changes are additive-only within v1 and go through the coordinator.
 * This module must not import from any other v3 module.
 */
import { z } from "zod";

export const CONTRACT_VERSION = "car.event.v1";

/**
 * Field separator for every composite key and hash input in v3. A NUL byte is
 * deliberate: vendor names, hosts, native ids, titles and serialized args may
 * contain any printable character, so no printable separator is collision-proof
 * ("a:b" + "c" vs "a" + "b:c").
 *
 * It lives here as a named constant rather than as a literal in each call site
 * because a literal NUL is invisible in an editor — retyping one as a space
 * would silently change every dedupe key, idempotency key and action hash, and
 * nothing about the diff would look wrong.
 */
export const HASH_SEP = "\u0000";

export const Vendor = z.enum([
  "claude-code",
  "codex",
  "cursor",
  "hermes",
  "omp",
  "agentctl",
  "multica",
  "ci",
  "cron",
  "other",
]);
export type Vendor = z.infer<typeof Vendor>;

export const EventType = z.enum([
  "session.started",
  "session.ended",
  "attention.permission",
  "attention.question",
  "attention.idle",
  "attention.error",
  "attention.cleared",
  "progress",
  "artifact",
  "heartbeat",
  "cost.report",
  "note",
]);
export type EventType = z.infer<typeof EventType>;

export const Severity = z.enum(["info", "notice", "attention", "urgent"]);
export type Severity = z.infer<typeof Severity>;

export const ResponseChannelKind = z.enum([
  "claude-hook-http",
  "claude-resume",
  "codex-exec-resume",
  "agentctl-run",
  "multica-api",
  "file",
]);
export type ResponseChannelKind = z.infer<typeof ResponseChannelKind>;

export const SessionRef = z.object({
  vendor: Vendor,
  native_id: z.string().min(1).max(256),
  host: z.string().min(1).max(128),
  cwd: z.string().max(1024).optional(),
  repo: z.string().max(512).optional(),
  /** True only when the adapter resolved a canonical VCS identity, not a cwd label. */
  repo_verified: z.boolean().default(false),
  title: z.string().max(512).optional(),
});
export type SessionRef = z.infer<typeof SessionRef>;

export const ResponseChannel = z.object({
  kind: ResponseChannelKind,
  hint: z.record(z.string(), z.unknown()).optional(),
});
export type ResponseChannel = z.infer<typeof ResponseChannel>;

/** Maximum stored payload size; larger blobs are truncated with a marker. */
export const MAX_PAYLOAD_BYTES = 32 * 1024;
export const MAX_BODY_BYTES = 16 * 1024;

export const CarEvent = z.object({
  contract: z.literal(CONTRACT_VERSION),
  /** Source-scoped idempotency key. Required. Duplicate POSTs return the original id. */
  idempotency_key: z.string().min(1).max(512),
  ts: z.iso.datetime({ offset: true }),
  source: z.object({
    vendor: Vendor,
    host: z.string().min(1).max(128),
    adapter: z.string().min(1).max(64),
  }),
  /** Null for sessionless sources (cron, CI). */
  session: SessionRef.nullable().default(null),
  type: EventType,
  severity: Severity.default("info"),
  requires_response: z.boolean().default(false),
  response_channel: ResponseChannel.nullable().default(null),
  title: z.string().max(512).default(""),
  body: z.string().max(MAX_BODY_BYTES).default(""),
  payload: z.record(z.string(), z.unknown()).default({}),
  expires_at: z.iso.datetime({ offset: true }).optional(),
});
export type CarEvent = z.infer<typeof CarEvent>;

/** Parse one inbound event; throws ZodError with details on failure. */
export function parseEvent(input: unknown): CarEvent {
  return CarEvent.parse(input);
}

/**
 * Canonical session identity key, joined on {@link HASH_SEP}. Debug/audit use
 * only — the store's real identity is the (vendor, host, native_id) columns.
 */
export function sessionKey(ref: Pick<SessionRef, "vendor" | "host" | "native_id">): string {
  return [ref.vendor, ref.host, ref.native_id].join(HASH_SEP);
}

/**
 * Server-computed idempotency key for sources that cannot supply one
 * (bucketed to the minute so a repeating cron alert still re-fires later).
 */
export function computedIdempotencyKey(
  sourceVendor: string,
  type: string,
  payload: unknown,
  now: Date,
): string {
  const bucket = now.toISOString().slice(0, 16); // YYYY-MM-DDTHH:MM
  const hash = new Bun.CryptoHasher("sha256")
    .update([sourceVendor, type, JSON.stringify(payload), bucket].join(HASH_SEP))
    .digest("hex")
    .slice(0, 16);
  return `computed:${sourceVendor}:${type}:${hash}`;
}
