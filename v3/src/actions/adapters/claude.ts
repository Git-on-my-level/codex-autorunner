/**
 * Claude Code reply-back rungs.
 *
 * - `claude-hook-http`: answer the parked `PermissionRequest` HTTP response
 *   in-band (DESIGN §4/§8). Race-free and verified: `answerPermission` returns
 *   false if the park already timed out, and then we fall through.
 * - `claude-resume`: `claude -p --resume <native_id> "<text>"` in the session's
 *   cwd, for headless/ended sessions. A live interactive terminal session has no
 *   safe injection path, so a failure here falls through to the staged file
 *   inbox — and the caller reports "staged", not "delivered".
 */
import { answerPermission, hasParkedPermission } from "../../permission_park.ts";
import type { Store } from "../../store/db.ts";
import { probeCapabilities, supports } from "../capabilities.ts";
import { pickRef, sessionCwd } from "../sessions.ts";
import type { Adapter, AdapterOutcome, DeliveryContext } from "./types.ts";
import { hintString, REPLY_TIMEOUT_MS, timeoutFromHint } from "./types.ts";

export const CLAUDE_VENDORS = ["claude-code", "claude"];

/**
 * Which parked request does this reply answer? The hint is authoritative when
 * ingest recorded the CAR event id in it; otherwise fall back to the durable
 * record — the newest permission event of this session that is still parked.
 * Parks are in-process and short-lived, so "still parked" is an exact test.
 */
export function resolveParkedEventId(
  store: Store,
  carSessionId: string,
  hint: Record<string, unknown>,
): string | null {
  const hinted = hintString(hint, "event_id", "eventId");
  if (hinted) return hinted;
  const rows = store.db
    .query(
      `SELECT id FROM events
       WHERE car_session_id = ? AND type = 'attention.permission'
       ORDER BY received_at DESC LIMIT 20`,
    )
    .all(carSessionId) as { id: string }[];
  for (const row of rows) {
    if (hasParkedPermission(row.id)) return row.id;
  }
  return null;
}

export const claudeHookHttpAdapter: Adapter = {
  kind: "claude-hook-http",
  async deliver(ctx: DeliveryContext): Promise<AdapterOutcome> {
    const eventId = resolveParkedEventId(ctx.store, ctx.carSessionId, ctx.hint);
    if (!eventId) {
      return { status: "fallback", reason: "no parked permission request for this session" };
    }
    if (ctx.payload.approval === undefined) {
      // A parked PermissionRequest can only be answered allow/deny; prose has to
      // reach the session some other way.
      return { status: "fallback", reason: "payload is not an approval" };
    }
    const decision = ctx.payload.approval ? ("allow" as const) : ("deny" as const);
    const answered = answerPermission(eventId, {
      decision,
      ...(ctx.payload.text ? { reason: ctx.payload.text } : {}),
    });
    if (!answered) {
      return { status: "fallback", reason: "parked permission already timed out or unknown" };
    }
    return { status: "delivered", detail: { event_id: eventId, decision } };
  },
};

export const claudeResumeAdapter: Adapter = {
  kind: "claude-resume",
  async deliver(ctx: DeliveryContext): Promise<AdapterOutcome> {
    const ref = pickRef(ctx.store, ctx.carSessionId, CLAUDE_VENDORS);
    if (!ref) {
      return { status: "fallback", reason: "session has no claude native ref" };
    }
    const caps = await probeCapabilities(ctx.store, ctx.runner, "claude-code");
    if (!supports(caps, "resume")) {
      return {
        status: "fallback",
        reason: "installed claude CLI does not advertise --resume",
        detail: { probed_at: caps.probed_at },
      };
    }
    const cwd = sessionCwd(ctx.store, ctx.carSessionId);
    const argv = ["claude", "-p", "--resume", ref.native_id, ctx.text];
    const res = await ctx.runner(argv, {
      ...(cwd ? { cwd } : {}),
      timeoutMs: timeoutFromHint(ctx.hint, REPLY_TIMEOUT_MS),
    });
    if (res.code !== 0) {
      return {
        status: "fallback",
        reason: `claude -p --resume exited ${res.code}`,
        detail: { native_id: ref.native_id, stderr: res.stderr.slice(0, 2000) },
      };
    }
    return { status: "delivered", detail: { native_id: ref.native_id, cwd: cwd ?? null } };
  },
};
