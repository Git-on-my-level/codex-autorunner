/**
 * Write-path auth: every caller, including localhost, needs a per-source bearer
 * token from config or its configured environment variable.
 *
 * Fails closed. If the peer address cannot be determined we do NOT assume
 * localhost — an ingest route is a write path into the attention control plane,
 * and "we could not tell who you are" must never mean "come in".
 */
import type { Context } from "hono";
import type { CarConfig } from "../config/config.ts";
import { createHmac } from "node:crypto";

/** Source ids used as bearer-token keys in config.http.ingest_tokens. */
export type SourceId = "generic" | "agentctl" | "claude" | "multica" | "web" | "provider";

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

export type AuthResult =
  | { ok: true; via: "token"; source: SourceId; principal: string }
  | { ok: false; reason: string };

export function authorize(c: Context, config: CarConfig, source: SourceId): AuthResult {
  const address = peerAddress(c);
  const presented = bearerToken(c.req.header("authorization") ?? c.req.header("Authorization"));
  if (presented === null) {
    return { ok: false, reason: address === null ? "unidentified_peer_and_missing_token" : "missing_bearer_token" };
  }

  const candidates = configuredTokens(config, source);
  if (candidates.length === 0) return { ok: false, reason: "no_token_configured_for_source" };

  for (const expected of candidates) {
    if (timingSafeEqual(presented, expected)) {
      const credential = new Bun.CryptoHasher("sha256").update(presented).digest("hex").slice(0, 16);
      return { ok: true, via: "token", source, principal: `${source}:${credential}` };
    }
  }
  return { ok: false, reason: "invalid_token" };
}

/** Resolved tokens for a source. Values are never logged or returned by diagnostics. */
export function configuredTokens(config: CarConfig, source: SourceId): string[] {
  const tokens = config.http.ingest_tokens ?? {};
  const tokenEnvs = config.http.ingest_token_envs ?? {};
  const envTokens = [tokenEnvs[source], tokenEnvs[WILDCARD_TOKEN_KEY]]
    .filter((name): name is string => typeof name === "string" && name.length > 0)
    .map((name) => process.env[name]);
  return Array.from(
    new Set(
      [tokens[source], tokens[WILDCARD_TOKEN_KEY], ...envTokens].filter(
        (t): t is string => typeof t === "string" && t.length > 0,
      ),
    ),
  );
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

/** Scoped URL capability for agentctl, whose webhook CLI cannot set headers. */
export function agentctlCallbackCapability(secret: string, executionId: string): string {
  return createHmac("sha256", secret)
    .update(`car-agentctl-callback-v1\0${executionId}`)
    .digest("base64url");
}

export function authorizeAgentctlCallback(
  config: CarConfig,
  executionId: string,
  presented: string,
): { principal: string } | null {
  for (const secret of configuredTokens(config, "agentctl")) {
    if (timingSafeEqual(presented, agentctlCallbackCapability(secret, executionId))) {
      const credential = new Bun.CryptoHasher("sha256").update(secret).digest("hex").slice(0, 16);
      return { principal: `agentctl:${credential}:execution:${executionId}` };
    }
  }
  return null;
}
