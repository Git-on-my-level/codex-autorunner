import { describe, expect, test } from "bun:test";
import type { CarActionBus } from "../../src/actions/index.ts";
import { CONTRACT_VERSION, parseEvent } from "../../src/contract/events.ts";
import { createCoreEffectAdapters } from "../../src/effects/adapters.ts";
import { createEffectExecutor } from "../../src/effects/index.ts";
import { createSafetyKernel, SqlSafetyLedger, type EffectProposal } from "../../src/safety/index.ts";
import { FakeChannel, memoryStore } from "../fakes.ts";

class Actions implements CarActionBus {
  deliveries: unknown[] = [];
  async deliver(carSessionId: string, channel: unknown, payload: unknown) {
    this.deliveries.push({ carSessionId, channel, payload });
    return "delivered" as const;
  }
  async runTemplate() { return { ok: true, output: "ok" }; }
  async probeCapabilities() { return { vendor: "test", cli: "test", ok: true, verbs: [], resume: false, queue: false, probed_at: new Date().toISOString() }; }
  listTemplates() { return []; }
}

describe("core effect adapters", () => {
  test("an authorized reply resolves its destination only from durable event lineage", async () => {
    const store = memoryStore();
    const inserted = store.ingestEvent(parseEvent({
      contract: CONTRACT_VERSION,
      idempotency_key: "reply-lineage",
      ts: "2026-08-26T12:00:00Z",
      source: { vendor: "codex", host: "mac", adapter: "test" },
      session: { vendor: "codex", native_id: "thread-1", host: "mac" },
      type: "attention.question",
      requires_response: true,
      response_channel: { kind: "codex-exec-resume", hint: { thread_id: "thread-1" } },
      title: "Proceed?",
    }), { sourceId: "auth:test" });
    const actions = new Actions();
    const channel = new FakeChannel();
    const kernel = createSafetyKernel({ ledger: new SqlSafetyLedger(store), clock: store.clock });
    const proposal: EffectProposal = {
      intent_id: "reply-effect",
      type: "reply",
      args: { text: "Use option A" },
      scope: { vendor: "codex", event_type: "attention.question" },
      lineage: { source_id: "auth:test", request_id: "request-1", event_id: inserted.event_id },
      action_class: "reply",
    };
    const grant = kernel.createGrant({
      intent_id: "reply-grant",
      lineage: proposal.lineage,
      scope: proposal.scope,
      effect_type: "reply",
      constraints: { args: proposal.args, action_class: "reply" },
      uses_remaining: 1,
      created_by: "human",
    });
    const executor = createEffectExecutor({ kernel, adapters: createCoreEffectAdapters(store, actions, channel) });

    expect(await executor.execute(proposal, grant.id)).toMatchObject({ ok: true, outcome: "ok" });
    expect(actions.deliveries).toEqual([{
      carSessionId: inserted.car_session_id,
      channel: { kind: "codex-exec-resume", hint: { thread_id: "thread-1" } },
      payload: { text: "Use option A" },
    }]);
  });
  for (const delivery of ["queued", "degraded"] as const) {
    test(`${delivery} fallback cannot complete a native reply effect`, async () => {
      const store = memoryStore();
      try {
        const inserted = store.ingestEvent(parseEvent({
          contract: CONTRACT_VERSION, idempotency_key: `fallback-${delivery}`,
          ts: "2026-09-04T12:00:00Z", source: {vendor: "codex", host: "mac", adapter: "test"},
          session: {vendor: "codex", native_id: "thread-1", host: "mac"},
          type: "attention.question", requires_response: true, title: "Proceed?",
        }), {sourceId: "auth:test"});
        const actions = new Actions();
        const adapter = createCoreEffectAdapters(store, {...actions,
          deliver: async () => delivery,
          runTemplate: actions.runTemplate.bind(actions),
          probeCapabilities: actions.probeCapabilities.bind(actions),
          listTemplates: actions.listTemplates.bind(actions),
        }, new FakeChannel()).find(a => a.type === "reply")!;
        const kernel = createSafetyKernel({ledger: new SqlSafetyLedger(store), clock: store.clock});
        const proposal: EffectProposal = {intent_id: `fallback-${delivery}`, type: "reply", args: {text: "Keep"},
          scope: {vendor: "codex"}, lineage: {source_id: "auth:test", request_id: "r", event_id: inserted.event_id}};
        const grant = kernel.createGrant({intent_id: `g-${delivery}`, lineage: proposal.lineage,
          scope: proposal.scope, effect_type: "reply", constraints: {args: proposal.args}, created_by: "human"});
        const result = await createEffectExecutor({kernel, adapters: [adapter]}).execute(proposal, grant.id);
        expect(result).toMatchObject({ok: false, outcome: "uncertain"});
        expect(store.db.query("SELECT obligation_state FROM events WHERE id=?").get(inserted.event_id))
          .toEqual({obligation_state: "staged"});
      } finally { store.db.close(); }
    });
  }

});
