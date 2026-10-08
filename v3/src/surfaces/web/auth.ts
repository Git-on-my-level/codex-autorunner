/** Human credentials are deliberately separate from agent credentials. */
import { createHmac } from "node:crypto";
import type { Context } from "hono";
import { getCookie, setCookie, deleteCookie } from "hono/cookie";
import type { CarConfig } from "../../config/config.ts";
import { authorize, configuredTokens, timingSafeEqual } from "../../ingest/auth.ts";

const COOKIE = "car_ui_session";
const SESSION_SECONDS = 8 * 60 * 60;
const sign = (token: string, expiry: number) => createHmac("sha256", token).update(`car-ui-v2:${expiry}`).digest("base64url");
/**
 * Presence of a web token opts a workspace into authenticated UI access. A
 * token is intentionally not generated for local setup by default, so trusted
 * workspaces can use the UI without a login screen.
 */
export function webAuthConfigured(config: CarConfig): boolean {
  const direct = config.http.ingest_tokens ?? {};
  const envs = config.http.ingest_token_envs ?? {};
  return configuredTokens(config, "web").length > 0 ||
    [direct.web, direct["*"], envs.web, envs["*"]].some((value) => typeof value === "string" && value.length > 0);
}
export function webAuthOptional(config: CarConfig): boolean {
  return config.http.web_auth === "optional" && !webAuthConfigured(config);
}
function validCookie(c: Context, config: CarConfig): boolean {
  const cookie = getCookie(c, COOKIE);
  const match = cookie?.match(/^v2\.(\d{10})\.([A-Za-z0-9_-]{43})$/);
  if (!match) return false;
  const expiry = Number(match[1]);
  const now = Math.floor(Date.now() / 1_000);
  if (expiry <= now || expiry > now + SESSION_SECONDS) return false;
  return configuredTokens(config, "web").some((token) => timingSafeEqual(match[2]!, sign(token, expiry)));
}
export function webOrigin(c: Context, config: CarConfig): string {
  // Never trust arbitrary X-Forwarded-* headers. TLS proxies configure the one
  // externally visible origin explicitly instead.
  return new URL(config.http.public_origin ?? c.req.url).origin;
}
export function sameWebOrigin(c: Context, config: CarConfig): boolean {
  const origin = c.req.header("origin");
  if (!origin) return false;
  try { return new URL(origin).origin === webOrigin(c, config); } catch { return false; }
}
export function authenticateWebWrite(c: Context, config: CarConfig): boolean {
  // With no configured human token, keep browser form writes same-origin even
  // in trusted mode. This preserves CSRF protection without inventing a
  // credential that the operator did not ask CAR to manage.
  if (webAuthOptional(config)) return sameWebOrigin(c, config);
  return authorize(c, config, "web").ok || (validCookie(c, config) && sameWebOrigin(c, config));
}
export function hasWebWriteSession(c: Context, config: CarConfig): boolean {
  if (webAuthOptional(config)) return true;
  return authorize(c, config, "web").ok || validCookie(c, config);
}
export function establishWebSession(c: Context, config: CarConfig, presented: string): boolean {
  if (c.req.header("sec-fetch-site") === "cross-site" || (c.req.header("origin") && !sameWebOrigin(c, config))) return false;
  const token = configuredTokens(config, "web").find((candidate) => timingSafeEqual(candidate, presented));
  if (!token) return false;
  const expiry = Math.floor(Date.now() / 1_000) + SESSION_SECONDS;
  setCookie(c, COOKIE, `v2.${expiry}.${sign(token, expiry)}`, {
    httpOnly: true, sameSite: "Strict", path: "/", maxAge: SESSION_SECONDS,
    secure: webOrigin(c, config).startsWith("https:"),
  });
  return true;
}
export function clearWebSession(c: Context): void { deleteCookie(c, COOKIE, { path: "/" }); }
