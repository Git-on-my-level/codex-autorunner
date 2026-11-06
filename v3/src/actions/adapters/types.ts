/**
 * Shared adapter shapes. One reply-back adapter = one rung of a delivery chain:
 * it either delivers, asks to fall through to the next rung, or hard-fails.
 */
import type { Store } from "../../store/db.ts";
import type { CarConfig } from "../../config/config.ts";
import type { ReplyPayload } from "../../ports.ts";
import type { ResponseChannel } from "../../contract/events.ts";
import type { Runner } from "../runner.ts";

/** Minimal structural HTTP seam so tests inject a plain function, not a Response. */
export interface HttpResponseLike {
  ok: boolean;
  status: number;
  text(): Promise<string>;
}
export interface HttpRequestInit {
  method?: string;
  headers?: Record<string, string>;
  body?: string;
  signal?: AbortSignal;
}
export type FetchLike = (url: string, init?: HttpRequestInit) => Promise<HttpResponseLike>;

export function defaultFetch(): FetchLike {
  return (url, init) => fetch(url, init as RequestInit) as unknown as Promise<HttpResponseLike>;
}

export type EnvLike = Record<string, string | undefined>;

export type AdapterOutcome =
  | { status: "delivered"; detail?: Record<string, unknown> }
  /** This rung cannot reach the agent; try the next rung (ultimately the file inbox). */
  | { status: "fallback"; reason: string; detail?: Record<string, unknown> }
  /** This rung failed in a way that must not be silently downgraded. */
  | { status: "failed"; reason: string; detail?: Record<string, unknown> };

export interface DeliveryContext {
  store: Store;
  config: CarConfig;
  runner: Runner;
  httpFetch: FetchLike;
  env: EnvLike;
  carSessionId: string;
  /** The channel the event advertised; null means "file inbox only". */
  channel: ResponseChannel | null;
  payload: ReplyPayload;
  /** Rendered plain-text form of the payload (approval verdict + free text). */
  text: string;
  hint: Record<string, unknown>;
}

export interface Adapter {
  kind: string;
  deliver(ctx: DeliveryContext): Promise<AdapterOutcome>;
}

/** Default wall-clock for a reply-back subprocess (a resumed agent turn is slow). */
export const REPLY_TIMEOUT_MS = 120_000;
/** Launching a background agentctl execution should return promptly. */
export const AGENTCTL_LAUNCH_TIMEOUT_MS = 60_000;
export const MULTICA_TIMEOUT_MS = 20_000;

/** Read a string field out of `response_channel.hint`, trying several key spellings. */
export function hintString(hint: Record<string, unknown>, ...keys: string[]): string | null {
  for (const key of keys) {
    const value = hint[key];
    if (typeof value === "string" && value.length > 0) return value;
  }
  return null;
}

export function hintNumber(hint: Record<string, unknown>, ...keys: string[]): number | null {
  for (const key of keys) {
    const value = hint[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
    if (typeof value === "string" && value.trim() !== "" && Number.isFinite(Number(value))) {
      return Number(value);
    }
  }
  return null;
}

/** Per-delivery timeout: honour a caller-supplied hint, else the default. */
export function timeoutFromHint(hint: Record<string, unknown>, fallbackMs: number): number {
  const hinted = hintNumber(hint, "timeout_ms", "timeoutMs");
  if (hinted === null || hinted <= 0) return fallbackMs;
  return Math.min(hinted, 600_000);
}
