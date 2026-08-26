/**
 * agentctl reply-back: a reply to agentctl-launched work is a NEW bounded
 * execution, not an injection into the old one (DESIGN §8).
 *
 *   agentctl run --background --label car-continuation -- <vendor resume argv>
 *
 * The resume argv is built from the vendor of the underlying session ref
 * (`session_refs` — a CAR session wrapping a codex process holds both the
 * agentctl exec id and the codex uuid). CAR then immediately subscribes to the
 * new execution so its lifecycle events come back through /v1/ingest/agentctl,
 * and links the new exec id as another ref of the same CAR session.
 *
 * Subscription is best-effort: a failure is audited, not fatal — the
 * continuation is already running and the `recent --unreconciled` sweep is the
 * belt-and-braces path.
 */
import { probeCapabilities, supports } from "../capabilities.ts";
import { getSession, pickRef } from "../sessions.ts";
import type { Adapter, AdapterOutcome, DeliveryContext } from "./types.ts";
import { AGENTCTL_LAUNCH_TIMEOUT_MS, timeoutFromHint } from "./types.ts";

export const CONTINUATION_LABEL = "car-continuation";

/** Vendors we know how to resume, in preference order. */
export const RESUMABLE_VENDORS = ["codex", "claude-code", "claude"];

/** Build the native resume argv for a vendor's session, to hand to `agentctl run --`. */
export function resumeArgvFor(vendor: string, nativeId: string, text: string): string[] | null {
  switch (vendor) {
    case "codex":
      return ["codex", "exec", "resume", nativeId, text];
    case "claude-code":
    case "claude":
      return ["claude", "-p", "--resume", nativeId, text];
    default:
      return null;
  }
}

/** agentctl speaks JSON envelopes; be liberal about where the execution id sits. */
export function parseExecutionId(out: string): string | null {
  const trimmed = out.trim();
  if (trimmed.startsWith("{")) {
    try {
      const parsed = JSON.parse(trimmed) as Record<string, unknown>;
      const found = findExecutionId(parsed, 0);
      if (found) return found;
    } catch {
      /* fall through to the regex */
    }
  }
  const m = /\bexec-[A-Za-z0-9._-]+/.exec(out);
  return m ? m[0] : null;
}

function findExecutionId(node: unknown, depth: number): string | null {
  if (depth > 6 || node === null || typeof node !== "object") return null;
  const obj = node as Record<string, unknown>;
  for (const key of ["execution_id", "executionId", "exec_id", "id"]) {
    const value = obj[key];
    if (typeof value === "string" && value.startsWith("exec-")) return value;
  }
  for (const value of Object.values(obj)) {
    const found = findExecutionId(value, depth + 1);
    if (found) return found;
  }
  return null;
}

export function ingestTarget(port: number): string {
  return `http://127.0.0.1:${port}/v1/ingest/agentctl`;
}

export const agentctlRunAdapter: Adapter = {
  kind: "agentctl-run",
  async deliver(ctx: DeliveryContext): Promise<AdapterOutcome> {
    const ref = pickRef(ctx.store, ctx.carSessionId, RESUMABLE_VENDORS);
    if (!ref) {
      return { status: "fallback", reason: "session has no resumable vendor ref (codex/claude)" };
    }
    const resumeArgv = resumeArgvFor(ref.vendor, ref.native_id, ctx.text);
    if (!resumeArgv) {
      return { status: "fallback", reason: `no resume argv for vendor '${ref.vendor}'` };
    }
    const caps = await probeCapabilities(ctx.store, ctx.runner, "agentctl");
    if (!supports(caps, "run")) {
      return { status: "fallback", reason: "installed agentctl does not advertise run" };
    }
    const session = getSession(ctx.store, ctx.carSessionId);
    const cwd = session?.cwd ?? undefined;
    const argv = [
      "agentctl",
      "run",
      "--background",
      "--label",
      CONTINUATION_LABEL,
      "--",
      ...resumeArgv,
    ];
    const res = await ctx.runner(argv, {
      ...(cwd ? { cwd } : {}),
      timeoutMs: timeoutFromHint(ctx.hint, AGENTCTL_LAUNCH_TIMEOUT_MS),
    });
    if (res.code !== 0) {
      return {
        status: "fallback",
        reason: `agentctl run exited ${res.code}`,
        detail: { stderr: res.stderr.slice(0, 2000) },
      };
    }

    const execId = parseExecutionId(res.stdout) ?? parseExecutionId(res.stderr);
    ctx.store.audit("adapter:agentctl", "agentctl.launched", "session", ctx.carSessionId, {
      label: CONTINUATION_LABEL,
      vendor: ref.vendor,
      native_id: ref.native_id,
      execution_id: execId,
    });

    if (execId) {
      // The continuation is another ref of the same CAR session, so its webhook
      // events attach here instead of minting a fresh session.
      ctx.store.linkSessionRef(ctx.carSessionId, {
        vendor: "agentctl",
        host: session?.host ?? ref.host,
        native_id: execId,
      });
      await subscribeBestEffort(ctx, execId);
    } else {
      ctx.store.audit("adapter:agentctl", "agentctl.subscribe_skipped", "session", ctx.carSessionId, {
        reason: "could not parse an execution id from agentctl run output",
      });
    }
    return { status: "delivered", detail: { execution_id: execId, label: CONTINUATION_LABEL } };
  },
};

async function subscribeBestEffort(ctx: DeliveryContext, execId: string): Promise<void> {
  const target = ingestTarget(ctx.config.http.port);
  const argv = [
    "agentctl",
    "subscribe",
    "create",
    "--execution",
    execId,
    "--destination",
    "webhook",
    "--target",
    target,
  ];
  let code: number;
  let stderr = "";
  try {
    const res = await ctx.runner(argv, { timeoutMs: 30_000 });
    code = res.code;
    stderr = res.stderr;
  } catch (err) {
    code = -1;
    stderr = String(err);
  }
  if (code === 0) {
    ctx.store.audit("adapter:agentctl", "agentctl.subscribed", "session", ctx.carSessionId, {
      execution_id: execId,
      target,
    });
  } else {
    ctx.store.audit("adapter:agentctl", "agentctl.subscribe_failed", "session", ctx.carSessionId, {
      execution_id: execId,
      target,
      code,
      stderr: stderr.slice(0, 2000),
    });
  }
}
