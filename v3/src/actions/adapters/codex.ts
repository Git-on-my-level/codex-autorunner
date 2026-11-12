/**
 * Codex reply-back: `codex exec resume <uuid> "<text>"`.
 *
 * DESIGN §2/§8: the installed codex-cli has NO `codex queue`; the working path
 * is `exec resume`. Capabilities are probed at runtime before every attempt
 * (cached 24h) — a successful `--help` that no longer advertises `resume` sends
 * the reply to the file inbox instead of silently dropping it.
 */
import { probeCapabilities, supports } from "../capabilities.ts";
import { pickRef, sessionCwd } from "../sessions.ts";
import type { Adapter, AdapterOutcome, DeliveryContext } from "./types.ts";
import { REPLY_TIMEOUT_MS, timeoutFromHint } from "./types.ts";

export const codexExecResumeAdapter: Adapter = {
  kind: "codex-exec-resume",
  async deliver(ctx: DeliveryContext): Promise<AdapterOutcome> {
    const ref = pickRef(ctx.store, ctx.carSessionId, ["codex"]);
    if (!ref) {
      return { status: "fallback", reason: "session has no codex native ref" };
    }
    const caps = await probeCapabilities(ctx.store, ctx.runner, "codex");
    if (!supports(caps, "resume")) {
      return {
        status: "fallback",
        reason: "installed codex CLI does not advertise resume",
        detail: { probed_at: caps.probed_at, verbs: caps.verbs },
      };
    }
    const cwd = sessionCwd(ctx.store, ctx.carSessionId);
    const argv = ["codex", "exec", "resume", ref.native_id, ctx.text];
    const res = await ctx.runner(argv, {
      ...(cwd ? { cwd } : {}),
      timeoutMs: timeoutFromHint(ctx.hint, REPLY_TIMEOUT_MS),
    });
    if (res.code !== 0) {
      return {
        status: "fallback",
        reason: `codex exec resume exited ${res.code}`,
        detail: { native_id: ref.native_id, stderr: res.stderr.slice(0, 2000) },
      };
    }
    return { status: "delivered", detail: { native_id: ref.native_id, cwd: cwd ?? null } };
  },
};
