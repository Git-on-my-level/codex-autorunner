/**
 * Multica reply-back: comment / approve on the card via the self-hosted REST API.
 *
 * Base URL and token come from the environment (CAR_MULTICA_URL,
 * CAR_MULTICA_TOKEN) because src/config is frozen for this workstream — see the
 * config-addition request in the workstream report. Missing credentials are not
 * an error: the reply falls through to the file inbox like any other
 * unreachable channel.
 *
 * HTTP goes through the injectable `FetchLike` seam; tests never touch a socket.
 */
import type { Adapter, AdapterOutcome, DeliveryContext } from "./types.ts";
import { hintString, MULTICA_TIMEOUT_MS, timeoutFromHint } from "./types.ts";

export const MULTICA_URL_ENV = "CAR_MULTICA_URL";
export const MULTICA_TOKEN_ENV = "CAR_MULTICA_TOKEN";

export function commentPath(cardId: string): string {
  return `/api/v1/cards/${encodeURIComponent(cardId)}/comments`;
}
export function approvalPath(cardId: string): string {
  return `/api/v1/cards/${encodeURIComponent(cardId)}/approvals`;
}

/** Join a base URL and a path without ever letting the path escape the base host. */
export function joinUrl(base: string, path: string): string {
  const trimmed = base.replace(/\/+$/, "");
  const suffix = path.startsWith("/") ? path : `/${path}`;
  return `${trimmed}${suffix}`;
}

/** A hint-supplied path is untrusted event data; it may only be a relative path. */
export function isSafePath(path: string): boolean {
  return path.startsWith("/") && !path.startsWith("//") && !path.includes("://");
}

export const multicaApiAdapter: Adapter = {
  kind: "multica-api",
  async deliver(ctx: DeliveryContext): Promise<AdapterOutcome> {
    const base = ctx.env[MULTICA_URL_ENV];
    const token = ctx.env[MULTICA_TOKEN_ENV];
    if (!base) {
      return { status: "fallback", reason: `${MULTICA_URL_ENV} is not set` };
    }
    const cardId = hintString(ctx.hint, "card_id", "cardId", "card", "id");
    if (!cardId) {
      return { status: "fallback", reason: "response_channel.hint has no card_id" };
    }

    const isApproval = ctx.payload.approval !== undefined;
    const hinted = hintString(ctx.hint, "path");
    if (hinted && !isSafePath(hinted)) {
      return { status: "fallback", reason: "response_channel.hint.path is not a relative path" };
    }
    const path = hinted ?? (isApproval ? approvalPath(cardId) : commentPath(cardId));
    const url = joinUrl(base, path);
    const body = isApproval
      ? JSON.stringify({ approved: ctx.payload.approval, comment: ctx.text })
      : JSON.stringify({ body: ctx.text });

    const headers: Record<string, string> = { "content-type": "application/json" };
    if (token) headers["authorization"] = `Bearer ${token}`;

    let status: number;
    let ok: boolean;
    let responseText = "";
    try {
      const res = await ctx.httpFetch(url, {
        method: "POST",
        headers,
        body,
        signal: AbortSignal.timeout(timeoutFromHint(ctx.hint, MULTICA_TIMEOUT_MS)),
      });
      status = res.status;
      ok = res.ok;
      if (!ok) responseText = (await res.text()).slice(0, 2000);
    } catch (err) {
      return {
        status: "fallback",
        reason: `multica request failed: ${String(err)}`,
        detail: { card_id: cardId },
      };
    }
    if (!ok) {
      return {
        status: "fallback",
        reason: `multica responded ${status}`,
        detail: { card_id: cardId, response: responseText },
      };
    }
    return { status: "delivered", detail: { card_id: cardId, status, approval: isApproval } };
  },
};
