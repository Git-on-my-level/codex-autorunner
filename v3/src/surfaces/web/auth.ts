import { createHmac } from "node:crypto";
import type { Context } from "hono";
import { getCookie, setCookie } from "hono/cookie";
import type { CarConfig } from "../../config/config.ts";
import { authorize, configuredTokens, timingSafeEqual } from "../../ingest/auth.ts";

const COOKIE = "car_ui_session";
const COOKIE_PAYLOAD = "car-ui-session-v1";
const SESSION_SECONDS = 8 * 60 * 60;

function sessionValue(token: string): string {
  return `v1.${createHmac("sha256", token).update(COOKIE_PAYLOAD).digest("base64url")}`;
}

export function authenticateWebWrite(c: Context, config: CarConfig): boolean {
  if (authorize(c, config, "web").ok) return true;
  const cookie = getCookie(c, COOKIE);
  if (!cookie || !sameOrigin(c)) return false;
  return configuredTokens(config, "web").some((token) => timingSafeEqual(cookie, sessionValue(token)));
}

/** Read-side capability check used to avoid rendering controls that can only 401. */
export function hasWebWriteSession(c: Context, config: CarConfig): boolean {
  if (authorize(c, config, "web").ok) return true;
  const cookie = getCookie(c, COOKIE);
  return Boolean(cookie && configuredTokens(config, "web").some((token) => timingSafeEqual(cookie, sessionValue(token))));
}

export function establishWebSession(c: Context, config: CarConfig, presented: string): boolean {
  const token = configuredTokens(config, "web").find((candidate) => timingSafeEqual(candidate, presented));
  if (!token) return false;
  setCookie(c, COOKIE, sessionValue(token), {
    httpOnly: true,
    sameSite: "Strict",
    path: "/ui",
    maxAge: SESSION_SECONDS,
  });
  return true;
}

/** Cookie-authenticated writes must originate from the same browser origin. */
function sameOrigin(c: Context): boolean {
  const origin = c.req.header("origin");
  const host = c.req.header("host");
  if (!origin || !host) return false;
  try {
    return new URL(origin).host === host;
  } catch {
    return false;
  }
}
