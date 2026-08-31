import { describe, expect, test } from "bun:test";
import { CarConfig } from "../../src/config/config.ts";
import { resolveProvider } from "../../src/config/provider_topology.ts";
import { descriptorFromResolved, type CapabilityProvider, type ContextBundle, type ContextQuery, type EffectProposalPacket, type IncidentPacket, type PolicyAdvice, type ProviderDecision, type ProviderDescriptor, type ProviderHealth, type ProviderPreflight } from "../../src/providers/types.ts";
import { ProviderRegistry } from "../../src/providers/registry.ts";
import { createSafetyKernel, SqlSafetyLedger } from "../../src/safety/index.ts";
import { createEffectExecutor, effectAdapter } from "../../src/effects/index.ts";
import { createRouter } from "../../src/router/index.ts";
import { IdempotencyConflictError, StaleClaimError, type Store } from "../../src/store/db.ts";
import { memoryStore, FakeChannel, FakeClock } from "../fakes.ts";
import { parseEvent } from "../../src/contract/events.ts";

function config(): CarConfig {
  return CarConfig.parse({
    state_dir: "/tmp/car-router-test",
    providers: {
      defaults: { operator: "test", policy: "test", memory: "test" },
      instances: { test: { adapter: "native", continuity: "global", scope: [] } },
    },
  });
}

function event(overrides: Record<string, unknown> = {}) {
  return parseEvent({
    contract: "car.event.v1",
    idempotency_key: "agentctl:exec-1:question-1",
    ts: "2026-08-26T12:00:00Z",
    source: { vendor: "agentctl", host: "mac", adapter: "subscribe-webhook" },
    session: { vendor: "agentctl", native_id: "exec-1", host: "mac", repo: "github.com/example/repo" },
    type: "attention.question",
    severity: "attention",
    requires_response: true,
    title: "Need a decision",
    body: "Choose a safe option",
    ...overrides,
  });
}

type FakeBehavior = {
  context?: (input: ContextQuery) => Promise<ContextBundle>;
  decide?: (input: IncidentPacket) => Promise<ProviderDecision>;
  evaluate?: (input: EffectProposalPacket) => Promise<PolicyAdvice>;
};

class FakeProvider implements CapabilityProvider {
  readonly descriptor: ProviderDescriptor;
  readonly calls: string[] = [];
  constructor(private readonly behavior: FakeBehavior = {}) {
    const resolved = resolveProvider(config(), "operator", { source: "agentctl", host: "mac" });
    this.descriptor = descriptorFromResolved(resolved, "fake-1.0.0", ["memory", "operator", "policy"]);
  }
  async preflight(): Promise<ProviderPreflight> {
    return { ready: true, checked_at: new Date().toISOString(), diagnostics: [] };
  }
  async health(): Promise<ProviderHealth> {
    return { healthy: true, checked_at: new Date().toISOString(), details: {} };
  }
  async context(input: ContextQuery): Promise<ContextBundle> {
    this.calls.push("memory");
    if (this.behavior.context) return this.behavior.context(input);
    return { charter: "", hits: [] };
  }
  async decide(input: IncidentPacket): Promise<ProviderDecision> {
    this.calls.push("operator");
    if (this.behavior.decide) return this.behavior.decide(input);
    return { contract: "car.operator.v1", request_id: input.request_id, disposition: "keep_informed", rationale: "quiet", effects: [] };
  }
  async evaluate(input: EffectProposalPacket): Promise<PolicyAdvice> {
    this.calls.push("policy");
    if (this.behavior.evaluate) return this.behavior.evaluate(input);
    return { contract: "car.policy.v1", request_id: input.request_id, verdict: "allow", rationale: "test" };
  }
}

async function setup(store: Store, provider: FakeProvider, options: { policyEnabled?: boolean } = {}) {
  const registry = new ProviderRegistry();
  registry.register(provider);
  await registry.preflight();
  await registry.health();
  const safety = createSafetyKernel({
    ledger: new SqlSafetyLedger(store),
    audit: (verb, objectId, detail) => store.audit("safety", verb, "effect", objectId, detail),
  });
  const channel = new FakeChannel();
  const router = createRouter({
    store,
    config: config(),
    registry,
    channel,
    safety,
    effects: createEffectExecutor({ kernel: safety, adapters: [effectAdapter("approve", () => ({ ok: true, output: "approved" }))] }),
    policyEnabled: options.policyEnabled,
    leaseSeconds: 60,
  });
  return { router, channel, safety, registry };
}

function ingest(store: Store, overrides: Record<string, unknown> = {}) {
  return store.ingestEvent(event(overrides), { sourceId: "auth:agentctl" });
}

describe("attention router", () => {
  test("native-shaped provider happy path invokes memory before operator and resolves", async () => {
    const store = memoryStore();
    const provider = new FakeProvider();
    const { router } = await setup(store, provider);
    const inserted = ingest(store);
    const result = await router.tick();
    expect(result.processed).toBe(1);
    expect(result.resolved).toBe(1);
    expect(provider.calls).toEqual(["memory", "operator"]);
    expect(store.getEvent(inserted.event_id)?.route_state).toBe("provider_resolved");
    expect((store.db.query("SELECT COUNT(*) AS n FROM provider_invocations WHERE state = 'terminal_recorded'").get() as { n: number }).n).toBe(2);
  });

  test("provider effect without a human grant is durably blocked and escalated", async () => {
    const store = memoryStore();
    const provider = new FakeProvider({
      decide: async (input) => ({
        contract: "car.operator.v1",
        request_id: input.request_id,
        disposition: "resolve",
        rationale: "approve the requested operation",
        effects: [{
          contract: "car.effect-proposal.v1",
          intent_id: "provider-effect-1",
          effect_type: "approve",
          args: { command: "echo safe" },
          lineage_id: "provider-lineage",
          rationale: "requested by the provider",
        }],
      }),
    });
    const { router, channel } = await setup(store, provider);
    const inserted = ingest(store);
    const result = await router.tick();
    expect(result.blockedEffects).toBe(1);
    expect(result.escalated).toBe(1);
    expect(channel.escalations).toHaveLength(1);
    const effect = store.listEffects()[0]!;
    expect(effect.state).toBe("blocked");
    expect(effect.safety_verdict).toBe("grant_required");
    expect(effect.action_class).toBe("approve");
    expect(JSON.parse((store.db.query("SELECT suggested_action_json FROM escalations").get() as { suggested_action_json: string }).suggested_action_json)).toMatchObject({
      effect_intent_id: effect.intent_id,
      effect_type: "approve",
      action_class: "approve",
    });
    expect(store.getEvent(inserted.event_id)?.route_state).toBe("escalated");
  });

  test("one operator usage charge is conservatively partitioned across all proposed effects", async () => {
    const store = memoryStore();
    const provider = new FakeProvider({
      decide: async (input) => ({
        contract: "car.operator.v1",
        request_id: input.request_id,
        disposition: "resolve",
        rationale: "three proposals from one paid decision",
        usage: { tokens_in: 100, tokens_out: 50, cost_usd: 1.0000001 },
        effects: [1, 2, 3].map((n) => ({
          contract: "car.effect-proposal.v1" as const,
          intent_id: `cost-effect-${n}`,
          effect_type: "approve" as const,
          args: { command: `echo ${n}` },
          lineage_id: `provider-lineage-${n}`,
          rationale: "cost attribution test",
        })),
      }),
    });
    const { router } = await setup(store, provider);
    ingest(store);
    expect((await router.tick()).effects).toBe(3);
    const costs = store.listEffects().map((effect) => effect.cost_usd ?? 0).sort((left, right) => right - left);
    expect(costs).toEqual([0.333334, 0.333334, 0.333333]);
    expect(costs.reduce((sum, cost) => sum + cost, 0)).toBeCloseTo(1.000001, 9);
  });

  test("provider failure is typed, terminal, audited, and escalated without swapping providers", async () => {
    const store = memoryStore();
    const provider = new FakeProvider({ context: async () => { throw new Error("memory unavailable"); } });
    const { router, channel } = await setup(store, provider);
    ingest(store);
    const result = await router.tick();
    expect(result.providerFailures).toBe(1);
    expect(channel.escalations).toHaveLength(1);
    const invocation = store.db.query("SELECT state, terminal_outcome, error_json FROM provider_invocations").get() as { state: string; terminal_outcome: string; error_json: string };
    expect(invocation.state).toBe("terminal_recorded");
    expect(invocation.terminal_outcome).toBe("unknown");
    expect(JSON.parse(invocation.error_json)).toMatchObject({ code: "unknown" });
    expect((store.db.query("SELECT COUNT(*) AS n FROM provider_invocations").get() as { n: number }).n).toBe(1);
  });

  test("policy failure leaves the proposed effect durably blocked", async () => {
    const store = memoryStore();
    const provider = new FakeProvider({
      decide: async (input) => ({
        contract: "car.operator.v1",
        request_id: input.request_id,
        disposition: "resolve",
        rationale: "proposal needs policy advice",
        effects: [{
          contract: "car.effect-proposal.v1",
          intent_id: "policy-failure-effect",
          effect_type: "approve",
          args: { command: "echo safe" },
          lineage_id: "provider-lineage",
          rationale: "test",
        }],
      }),
      evaluate: async () => { throw new Error("policy unavailable"); },
    });
    const { router } = await setup(store, provider);
    ingest(store);
    const result = await router.tick();
    expect(result.providerFailures).toBe(1);
    expect(result.blockedEffects).toBe(1);
    expect(store.listEffects()[0]).toMatchObject({ state: "blocked", safety_verdict: "grant_required" });
  });

  test("a reusable human grant is core-matched on the next exact provider effect", async () => {
    const store = memoryStore();
    const provider = new FakeProvider({
      decide: async (input) => ({
        contract: "car.operator.v1",
        request_id: input.request_id,
        disposition: "resolve",
        rationale: "safe approval",
        effects: [{
          contract: "car.effect-proposal.v1",
          intent_id: "provider-reused-id",
          effect_type: "approve",
          args: { command: "echo safe" },
          lineage_id: "provider-lineage",
          rationale: "exact repeat",
        }],
      }),
    });
    const { router, safety } = await setup(store, provider);
    ingest(store);
    expect((await router.tick()).blockedEffects).toBe(1);
    safety.createGrant({
      intent_id: "human-always-approve",
      lineage: null,
      scope: { vendor: "agentctl", event_type: "attention.question" },
      effect_type: "approve",
      constraints: { args: { command: "echo safe" }, action_class: "approve" },
      uses_remaining: null,
      created_by: "human",
    });

    ingest(store, { idempotency_key: "agentctl:exec-1:question-2", ts: "2026-08-26T12:01:00Z" });
    const next = await router.tick();
    expect(next.blockedEffects).toBe(0);
    expect(next.resolved).toBe(1);
    expect(store.listEffects().find((effect) => effect.state === "terminal_recorded")).toMatchObject({ state: "terminal_recorded", terminal_outcome: "ok" });
  });

  test("stale event claim cannot complete after reclaim", () => {
    const clock = new FakeClock();
    const store = memoryStore(clock);
    const inserted = ingest(store);
    const first = store.claimEvents(1, 1, "worker-a")[0]!;
    clock.advance(2_000);
    const second = store.claimEvents(1, 1, "worker-b")[0]!;
    expect(() => store.completeEventClaim(inserted.event_id, { owner: "worker-a", token: first.route_claim_token! }, "resolved")).toThrow(StaleClaimError);
    expect(() => store.completeEventClaim(inserted.event_id, { owner: "worker-b", token: second.route_claim_token! }, "resolved")).not.toThrow();
  });

  test("provider terminal is written before safety effect proposal", async () => {
    const store = memoryStore();
    const provider = new FakeProvider({
      decide: async (input) => ({
        contract: "car.operator.v1",
        request_id: input.request_id,
        disposition: "resolve",
        rationale: "effect requires review",
        effects: [{ contract: "car.effect-proposal.v1", intent_id: "effect-order", effect_type: "approve", args: { command: "echo safe" }, lineage_id: "lineage", rationale: "review" }],
      }),
    });
    const { router } = await setup(store, provider);
    ingest(store);
    await router.tick();
    const terminal = store.db.query("SELECT MIN(rowid) AS rowid FROM audit WHERE verb = 'provider_invocation.terminal_recorded'").get() as { rowid: number };
    const proposed = store.db.query("SELECT MIN(rowid) AS rowid FROM audit WHERE verb = 'effect.proposed'").get() as { rowid: number };
    expect(terminal.rowid).toBeLessThan(proposed.rowid);
  });

  test("durable provider results replay even when the runtime is unavailable", async () => {
    const store = memoryStore();
    const provider = new FakeProvider();
    const first = await setup(store, provider);
    const inserted = ingest(store);
    expect((await first.router.tick()).resolved).toBe(1);

    store.db.query(
      `UPDATE events SET triage_state = 'pending', route_state = 'pending',
         route_claim_owner = NULL, route_claim_token = NULL, route_lease_until = NULL
       WHERE id = ?`,
    ).run(inserted.event_id);
    const registry = new ProviderRegistry();
    const safety = createSafetyKernel({ ledger: new SqlSafetyLedger(store) });
    const replay = createRouter({
      store,
      config: config(),
      registry,
      channel: new FakeChannel(),
      safety,
      effects: createEffectExecutor({ kernel: safety }),
      leaseSeconds: 60,
    });
    const result = await replay.tick();
    expect(result.processed).toBe(1);
    expect(result.providerFailures).toBe(0);
    expect(store.getEvent(inserted.event_id)?.route_state).toBe("provider_resolved");
  });
});
