/**
 * Ingest auth: localhost is trusted, everything else needs a per-source bearer
 * token from `config.http.ingest_tokens`.
 *
 * Fails closed. If the peer address cannot be determined we do NOT assume
 * localhost — an ingest route is a write path into the attention control plane,
 * and "we could not tell who you are" must never mean "come in".
 */
import type { Context } from "hono";
import type { CarConfig } from "../config/config.ts";

/** Source ids used as bearer-token keys in config.http.ingest_tokens. */
export type SourceId = "generic" | "agentctl" | "claude" | "multica";

/** Token key honored for every source, so an operator can issue one shared token. */
export const WILDCARD_TOKEN_KEY = "*";

const LOOPBACK = new Set(["127.0.0.1", "::1", "::ffff:127.0.0.1", "0:0:0:0:0:0:0:1", "localhost"]);

/**
 * Bun passes its `Server` as the second fetch argument, which Hono surfaces as
 * `c.env`. That is the only trustworthy source of the peer address: proxy
 * headers (X-Forwarded-For &c.) are attacker-controlled and deliberately ignored.
 */
export interface PeerLookup {
  requestIP?(req: Request): { address: string; family: string; port: number } | null;
}

export function peerAddress(c: Context): string | null {
  const env = c.env as PeerLookup | undefined | null;
  if (!env || typeof env.requestIP !== "function") return null;
  try {
    return env.requestIP(c.req.raw)?.address ?? null;
  } catch {
    return null;
  }
}

export function isLoopback(address: string | null): boolean {
  if (address === null) return false;
  return LOOPBACK.has(address.toLowerCase());
}

export type AuthResult = { ok: true; via: "localhost" | "token" } | { ok: false; reason: string };

export function authorize(c: Context, config: CarConfig, source: SourceId): AuthResult {
  const address = peerAddress(c);
  if (isLoopback(address)) return { ok: true, via: "localhost" };

  const presented = bearerToken(c.req.header("authorization") ?? c.req.header("Authorization"));
  if (presented === null) {
    return { ok: false, reason: address === null ? "unidentified_peer" : "missing_bearer_token" };
  }

  const tokens = config.http.ingest_tokens ?? {};
  const candidates = [tokens[source], tokens[WILDCARD_TOKEN_KEY]].filter(
    (t): t is string => typeof t === "string" && t.length > 0,
  );
  if (candidates.length === 0) return { ok: false, reason: "no_token_configured_for_source" };

  for (const expected of candidates) {
    if (timingSafeEqual(presented, expected)) return { ok: true, via: "token" };
  }
  return { ok: false, reason: "invalid_token" };
}

export function bearerToken(header: string | undefined | null): string | null {
  if (!header) return null;
  const match = /^bearer\s+(.+)$/i.exec(header.trim());
  const token = match?.[1]?.trim();
  return token && token.length > 0 ? token : null;
}

/** Constant-time-ish comparison; length is not secret but content must not leak. */
export function timingSafeEqual(a: string, b: string): boolean {
  const ab = Buffer.from(a, "utf8");
  const bb = Buffer.from(b, "utf8");
  if (ab.length !== bb.length) return false;
  let diff = 0;
  for (let i = 0; i < ab.length; i++) diff |= ab[i]! ^ bb[i]!;
  return diff === 0;
}
