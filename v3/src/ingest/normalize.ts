/**
 * Shared normalizer helpers (WS-A).
 *
 * Every per-source adapter is a pure function `(raw, ctx) => CarEvent[]`. Purity is
 * the whole point: the golden fixtures in test/fixtures/<source>/ can assert the
 * exact canonical event without a server, a socket, or a database.
 */
import { hostname } from "node:os";
import {
  CONTRACT_VERSION,
  MAX_BODY_BYTES,
  MAX_PAYLOAD_BYTES,
  parseEvent,
  type CarEvent,
  type ResponseChannel,
  type Severity,
  type EventType,
  type Vendor,
} from "../contract/events.ts";
import { HASH_SEP } from "../contract/events.ts";

/** Ambient facts a normalizer needs but cannot read off the wire. */
export interface NormalizeContext {
  /** Wall clock for sources that ship no timestamp (Claude Code hooks). */
  now: Date;
  /** Fallback host when the payload carries none. */
  host: string;
}

let cachedHost: string | null = null;
export function defaultHost(): string {
  if (cachedHost === null) {
    try {
      cachedHost = hostname() || "unknown-host";
    } catch {
      cachedHost = "unknown-host";
    }
  }
  return cachedHost;
}

export function makeContext(now: Date, host?: string): NormalizeContext {
  return { now, host: clampString(host || defaultHost(), 128) };
}

/* -------------------------------------------------------------- primitives */

export function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** Read a string field, tolerating numbers (vendor ids are sometimes numeric). */
export function str(source: unknown, ...keys: string[]): string | undefined {
  if (!isRecord(source)) return undefined;
  for (const key of keys) {
    const v = source[key];
    if (typeof v === "string" && v.length > 0) return v;
    if (typeof v === "number" && Number.isFinite(v)) return String(v);
  }
  return undefined;
}

export function num(source: unknown, ...keys: string[]): number | undefined {
  if (!isRecord(source)) return undefined;
  for (const key of keys) {
    const v = source[key];
    if (typeof v === "number" && Number.isFinite(v)) return v;
    if (typeof v === "string" && v.trim() !== "" && Number.isFinite(Number(v))) return Number(v);
  }
  return undefined;
}

export function obj(source: unknown, ...keys: string[]): Record<string, unknown> | undefined {
  if (!isRecord(source)) return undefined;
  for (const key of keys) {
    const v = source[key];
    if (isRecord(v)) return v;
  }
  return undefined;
}

export function clampString(v: string, max: number): string {
  return v.length <= max ? v : v.slice(0, Math.max(0, max - 1)) + "…";
}

/** Bodies are capped by the contract; truncate loudly rather than failing validation. */
export function clampBody(v: string): string {
  if (v.length <= MAX_BODY_BYTES) return v;
  const marker = "\n…[truncated by CAR ingest]";
  return v.slice(0, MAX_BODY_BYTES - marker.length) + marker;
}

/**
 * Vendor blobs are stored verbatim up to MAX_PAYLOAD_BYTES, then replaced by a
 * self-describing marker so nothing downstream mistakes a truncation for data.
 */
export function clampPayload(payload: Record<string, unknown>): Record<string, unknown> {
  let encoded: string;
  try {
    encoded = JSON.stringify(payload) ?? "{}";
  } catch {
    return { _car_truncated: true, _car_reason: "unserializable" };
  }
  if (encoded.length <= MAX_PAYLOAD_BYTES) return payload;
  return {
    _car_truncated: true,
    _car_original_bytes: encoded.length,
    _car_preview: encoded.slice(0, 2048),
  };
}

/** Short stable digest used to synthesize idempotency keys. */
export function shortHash(...parts: unknown[]): string {
  const material = parts
    .map((p) => (typeof p === "string" ? p : JSON.stringify(p) ?? "null"))
    .join(HASH_SEP);
  return new Bun.CryptoHasher("sha256").update(material).digest("hex").slice(0, 16);
}

/** Minute bucket, so a repeating alert with no native id still re-fires later. */
export function minuteBucket(d: Date): string {
  return d.toISOString().slice(0, 16);
}

/** Normalize any vendor timestamp to a contract-legal ISO-8601 string with offset. */
export function isoTs(raw: unknown, fallback: Date): string {
  if (typeof raw === "string" && raw.length > 0) {
    const parsed = new Date(raw);
    if (!Number.isNaN(parsed.getTime())) return parsed.toISOString();
  }
  if (typeof raw === "number" && Number.isFinite(raw)) {
    // Heuristic: seconds vs milliseconds since epoch.
    const ms = raw > 1e12 ? raw : raw * 1000;
    const parsed = new Date(ms);
    if (!Number.isNaN(parsed.getTime())) return parsed.toISOString();
  }
  return fallback.toISOString();
}

/* ------------------------------------------------------------ construction */

export interface DraftEvent {
  idempotency_key: string;
  ts: string;
  vendor: Vendor;
  adapter: string;
  host: string;
  session: {
    vendor: Vendor;
    native_id: string;
    host: string;
    cwd?: string;
    repo?: string;
    repo_verified?: boolean;
    title?: string;
  } | null;
  type: EventType;
  severity: Severity;
  requires_response?: boolean;
  response_channel?: ResponseChannel | null;
  title?: string;
  body?: string;
  payload?: Record<string, unknown>;
  expires_at?: string;
}

/**
 * Build + validate a canonical event. Every adapter funnels through here, so the
 * contract is enforced once and truncation rules cannot drift per source.
 */
export function buildEvent(draft: DraftEvent): CarEvent {
  return parseEvent({
    contract: CONTRACT_VERSION,
    idempotency_key: clampString(draft.idempotency_key, 512),
    ts: draft.ts,
    source: {
      vendor: draft.vendor,
      host: clampString(draft.host, 128),
      adapter: clampString(draft.adapter, 64),
    },
    session: draft.session
      ? {
          vendor: draft.session.vendor,
          native_id: clampString(draft.session.native_id, 256),
          host: clampString(draft.session.host, 128),
          ...(draft.session.cwd ? { cwd: clampString(draft.session.cwd, 1024) } : {}),
          ...(draft.session.repo ? { repo: clampString(draft.session.repo, 512) } : {}),
          repo_verified: draft.session.repo_verified === true,
          ...(draft.session.title ? { title: clampString(draft.session.title, 512) } : {}),
        }
      : null,
    type: draft.type,
    severity: draft.severity,
    requires_response: draft.requires_response ?? false,
    response_channel: draft.response_channel ?? null,
    title: clampString(draft.title ?? "", 512),
    body: clampBody(draft.body ?? ""),
    payload: clampPayload(draft.payload ?? {}),
    ...(draft.expires_at ? { expires_at: draft.expires_at } : {}),
  });
}

/** Raised by an adapter when a payload is structurally unusable. */
export class NormalizeError extends Error {
  constructor(
    message: string,
    readonly detail: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = "NormalizeError";
  }
}
