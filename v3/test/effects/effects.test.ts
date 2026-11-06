import { describe, expect, test } from "bun:test";
import { createEffectExecutor, effectAdapter } from "../../src/effects/index.ts";
import { createSafetyKernel, type EffectProposal } from "../../src/safety/index.ts";

const base: EffectProposal = {
  intent_id: "effect-execute-1",
  type: "notify",
  args: { text: "hello" },
  scope: { source_id: "telegram", host: "test-host" },
  lineage: { source_id: "telegram", request_id: "tap-1" },
};

function grant(kernel: ReturnType<typeof createSafetyKernel>, proposal = base) {
  return kernel.createGrant({
    intent_id: `grant-${proposal.intent_id}`,
    lineage: proposal.lineage,
    scope: proposal.scope,
    effect_type: proposal.type,
    constraints: { args: proposal.args },
    uses_remaining: null,
  });
}

describe("core effect executor", () => {
  test("providers submit proposals; only a registered core adapter executes", async () => {
    const kernel = createSafetyKernel();
    const g = grant(kernel);
    let seen: unknown = null;
    const executor = createEffectExecutor({
      kernel,
      owner: "test-worker",
      metadata: { source: "test" },
      adapters: [effectAdapter("notify", (context) => {
        seen = context.effect.args;
        expect(context.metadata).toEqual({ source: "test" });
        return { ok: true, output: "sent" };
      })],
    });
    const out = await executor.execute(base, g.id);
    expect(out.ok).toBe(true);
    expect(out.state).toBe("terminal_recorded");
    expect(out.outcome).toBe("ok");
    expect(seen).toEqual({ text: "hello" });
  });

  test("missing adapter still records terminal failure before returning", async () => {
    const kernel = createSafetyKernel();
    const g = grant(kernel);
    const executor = createEffectExecutor({ kernel, owner: "test-worker" });
    const out = await executor.execute(base, g.id);
    expect(out.ok).toBe(false);
    expect(out.outcome).toBe("failed");
    expect(out.state).toBe("terminal_recorded");
    expect(out.output).toContain("no core adapter");
  });

  test("adapter uncertainty is durable and is never converted into success", async () => {
    const kernel = createSafetyKernel();
    const proposal = { ...base, intent_id: "effect-uncertain", lineage: { ...base.lineage, request_id: "tap-uncertain" } };
    const g = grant(kernel, proposal);
    const executor = createEffectExecutor({
      kernel,
      adapters: [effectAdapter("notify", () => ({ ok: false, outcome: "uncertain", output: "remote acknowledgement timed out" }))],
    });
    const out = await executor.execute(proposal, g.id);
    expect(out.ok).toBe(false);
    expect(out.outcome).toBe("uncertain");
    expect(out.effect.terminal_outcome).toBe("uncertain");
  });

  test("same intent replay is idempotent and conflicting reuse is rejected", async () => {
    const kernel = createSafetyKernel();
    const g = grant(kernel);
    const executor = createEffectExecutor({ kernel, adapters: [effectAdapter("notify", () => ({ ok: true }))] });
    const first = await executor.execute(base, g.id);
    const replay = await executor.execute(base, g.id);
    expect(replay.effect.intent_id).toBe(first.effect.intent_id);
    expect(replay.effect.terminal_outcome).toBe("ok");
    await expect(executor.execute({ ...base, args: { text: "different" } }, g.id)).rejects.toThrow(/intent_id/);
  });

  test("revocation between authorization and claim blocks the adapter", async () => {
    const kernel = createSafetyKernel();
    const proposal = { ...base, intent_id: "effect-revoked", lineage: { ...base.lineage, request_id: "tap-revoked" } };
    const g = grant(kernel, proposal);
    expect(kernel.authorize(proposal, g.id).verdict.allowed).toBe(true);
    expect(kernel.revokeGrant(g.id)).toBe(true);
    let calls = 0;
    const executor = createEffectExecutor({
      kernel,
      adapters: [effectAdapter("notify", () => { calls++; return { ok: true }; })],
    });
    const out = await executor.execute(proposal, g.id);
    expect(out.ok).toBe(false);
    expect(out.verdict.code).toBe("grant_revoked");
    expect(out.state).toBe("blocked");
    expect(calls).toBe(0);
  });

  test("long-running adapters renew their execution fence", async () => {
    const kernel = createSafetyKernel();
    const proposal = { ...base, intent_id: "effect-long", lineage: { ...base.lineage, request_id: "tap-long" } };
    const g = grant(kernel, proposal);
    const executor = createEffectExecutor({
      kernel,
      leaseMs: 30,
      adapters: [effectAdapter("notify", async () => {
        await Bun.sleep(80);
        return { ok: true };
      })],
    });
    const out = await executor.execute(proposal, g.id);
    expect(out.ok).toBe(true);
    expect(out.outcome).toBe("ok");
  });
});
