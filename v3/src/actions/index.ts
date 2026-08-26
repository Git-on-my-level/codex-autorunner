/**
 * WS-E owns src/actions/: reply-back adapters (claude-hook-http parked responses,
 * claude-resume, codex-exec-resume, agentctl-run, multica-api, file fallback),
 * runtime capability probes, and the policy-enforcing template executor.
 * Scaffold stub: file fallback only; templates refuse.
 */
import { mkdirSync } from "node:fs";
import { join } from "node:path";
import type { Store } from "../store/db.ts";
import type { CarConfig } from "../config/config.ts";
import { repliesDir } from "../config/config.ts";
import type { ActionBus, DeliveryResult, PolicyPort, ReplyPayload } from "../ports.ts";
import type { ResponseChannel } from "../contract/events.ts";

export function createActionBus(store: Store, config: CarConfig, _policy: PolicyPort): ActionBus {
  return {
    async deliver(
      carSessionId: string,
      _channel: ResponseChannel | null,
      payload: ReplyPayload,
    ): Promise<DeliveryResult> {
      // Universal file fallback (v2 replies.py inbox, generalized).
      const dir = join(repliesDir(config), carSessionId);
      mkdirSync(dir, { recursive: true });
      const seqRow = store.db
        .query("SELECT COUNT(*) n FROM audit WHERE verb = 'reply.staged' AND object_id = ?")
        .get(carSessionId) as { n: number };
      const path = join(dir, `reply-${String(seqRow.n + 1).padStart(4, "0")}.md`);
      const text = payload.approval !== undefined ? (payload.approval ? "APPROVED" : "DENIED") : (payload.text ?? "");
      await Bun.write(path, `${text}\n`);
      store.audit("adapter:file", "reply.staged", "session", carSessionId, { path });
      return "queued";
    },
    async runTemplate(templateId, _args, opts) {
      store.audit("adapter:exec", "action.refused", "decision", opts.decisionId, {
        templateId,
        reason: "scaffold stub: no templates registered",
      });
      return { ok: false, output: "no templates registered" };
    },
  };
}
