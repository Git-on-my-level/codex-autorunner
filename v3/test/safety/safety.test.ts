import { describe, expect, test } from "bun:test";
import { FakeClock, memoryStore } from "../fakes.ts";
import {
  createSafetyKernel,
  MemorySafetyLedger,
  SqlSafetyLedger,
  normalizeArgs,
  normalizeEffect,
  type EffectProposal,
  type ImmutableLineage,
  type VerifiedScope,
} from "../../src/safety/index.ts";

const clock = () => new FakeClock(new Date("2026-08-27T12:00:00Z"));
const scope: VerifiedScope = { vendor: "claude-code", host: "test-host", repo: "github.com/acme/omi", repo_verified: true };
const lineage: ImmutableLineage = { source_id: "claude-hook", request_id: "perm-1", native_request_id: "toolu-1", verified_repo: scope.repo };

function proposal(overrides: Partial<EffectProposal> = {}): EffectProposal {
  return {
    intent_id: "intent-1",
    type: "run_template",
    args: { template_id: "git.status", repo: "/repo" },
    scope,
    lineage,
    action_class: "probe",
    ...overrides,
  };
}

function grant(kernel: ReturnType<typeof createSafetyKernel>, overrides: Record<string, unknown> = {}) {
  return kernel.createGrant({
    intent_id: "grant-intent-1",
    lineage,
    scope,
    effect_type: "run_template",
    constraints: { args: proposal().args, action_class: "probe" },
    uses_remaining: 1,
    ...overrides,
  });
}

describe("canonical effect identity", () => {
  test("normalizes object key order but preserves exact values and array order", () => {
    expect(normalizeArgs({ b: 2, a: { y: true, x: ["one", "two"] } }).sha256).toBe(
      normalizeArgs({ a: { x: ["one", "two"], y: true }, b: 2 }).sha256,
    );
    expect(normalizeArgs({ x: ["two", "one"] }).sha256).not.toBe(normalizeArgs({ x: ["one", "two"] }).sha256);
    expect(() => normalizeArgs({ x: Number.NaN })).toThrow();
  });

  test("effect normalization includes type, verified scope, and immutable lineage", () => {
    const a = normalizeEffect(proposal());
    const b = normalizeEffect(proposal({ scope: { ...scope, host: "other-host" } }));
    const c = normalizeEffect(proposal({ lineage: { ...lineage, request_id: "perm-2" } }));
    expect(a.dedupe_hash).not.toBe(b.dedupe_hash);
    expect(a.dedupe_hash).not.toBe(c.dedupe_hash);
  });

  test("repo scope cannot be fabricated from an unverified value", () => {
    expect(() => normalizeEffect(proposal({ scope: { repo: "omi" } }))).toThrow(/verified VCS/);
  });
});

describe("explicit grants and core rails", () => {
  test("no grant is blocked and a matching human grant authorizes", () => {
    const c = clock();
    const kernel = createSafetyKernel({ clock: c, ledger: new MemorySafetyLedger() });
    expect(kernel.authorize(proposal()).verdict.code).toBe("grant_required");
    const g = grant(kernel);
    const out = kernel.authorize(proposal({ intent_id: "intent-2" }), g.id);
    expect(out.verdict.allowed).toBe(true);
    expect(out.effect.state).toBe("pending");
  });

  test("provider-shaped grant creation is rejected; only explicit human grants exist", () => {
    const kernel = createSafetyKernel({ clock: clock() });
    expect(() => kernel.createGrant({
      intent_id: "grant-intent",
      lineage,
      scope,
      effect_type: "approve",
      constraints: { args: { approval: true } },
      uses_remaining: null,
      created_by: "provider",
    })).toThrow(/human action/);
  });

  test("one-shot grant is consumed once and replay is rejected", () => {
    const kernel = createSafetyKernel({ clock: clock() });
    const g = grant(kernel);
    const authorized = kernel.authorize(proposal({ intent_id: "intent-2" }), g.id);
    expect(authorized.verdict.allowed).toBe(true);
    expect(kernel.getGrant(g.id)?.status).toBe("active");
    const claimed = kernel.claim(authorized.effect.intent_id, "worker")!;
    expect(kernel.getGrant(g.id)?.status).toBe("consumed");
    kernel.recordTerminal(authorized.effect.intent_id, "worker", claimed.token, "ok");
    const replay = kernel.authorize(proposal({ intent_id: "intent-3" }), g.id);
    expect(replay.verdict.code).toBe("grant_consumed");
  });

  test("bounded grants require exact lineage while reusable grants can explicitly cross requests", () => {
    const kernel = createSafetyKernel({ clock: clock() });
    expect(() => grant(kernel, { lineage: null, uses_remaining: 1 })).toThrow(/bounded grant.*lineage/);

    const reusable = grant(kernel, { lineage: null, uses_remaining: null });
    expect(kernel.authorize(proposal({ intent_id: "request-one" }), reusable.id).verdict.allowed).toBe(true);
    expect(kernel.authorize(proposal({
      intent_id: "request-two",
      lineage: { ...lineage, request_id: "perm-2", native_request_id: "toolu-2" },
    }), reusable.id).verdict.allowed).toBe(true);
  });

  test("reusable grant scope is an explicit subset constraint, not exact context equality", () => {
    const kernel = createSafetyKernel({ clock: clock() });
    const reusable = grant(kernel, {
      lineage: null,
      uses_remaining: null,
      scope: { vendor: "claude-code", event_type: "attention.permission" },
    });
    expect(kernel.authorize(proposal({
      intent_id: "scoped-match",
      scope: { ...scope, event_type: "attention.permission", source_id: "credential-principal" },
    }), reusable.id).verdict.allowed).toBe(true);
    expect(kernel.authorize(proposal({
      intent_id: "scoped-miss",
      scope: { ...scope, vendor: "codex", event_type: "attention.permission" },
    }), reusable.id).verdict.code).toBe("grant_mismatch");
  });

  test("core matching prefers exact-lineage and narrower human authority", () => {
    const kernel = createSafetyKernel({ clock: clock() });
    const broad = grant(kernel, { intent_id: "broad", lineage: null, scope: { vendor: "claude-code" }, uses_remaining: null });
    const narrow = grant(kernel, { intent_id: "narrow", lineage: null, scope, uses_remaining: null });
    const exact = grant(kernel, { intent_id: "exact", lineage, scope: { vendor: "claude-code" }, uses_remaining: 1 });

    expect(kernel.findMatchingGrant(proposal())?.id).toBe(exact.id);
    expect(kernel.authorize(proposal(), exact.id).verdict.allowed).toBe(true);
    expect(kernel.findMatchingGrant(proposal({ intent_id: "next", lineage: { ...lineage, request_id: "perm-2" } }))?.id).toBe(narrow.id);
    expect(kernel.getGrant(broad.id)?.status).toBe("active");
  });

  test("deadline ceilings are part of human grant matching", () => {
    const c = clock();
    const kernel = createSafetyKernel({ clock: c });
    grant(kernel, {
      intent_id: "deadline-bound",
      lineage: null,
      uses_remaining: null,
      constraints: { args: proposal().args, action_class: "probe", max_deadline_ms: 30_000 },
    });
    expect(kernel.findMatchingGrant(proposal({ deadline_at: new Date(c.now().getTime() + 20_000).toISOString() }))).not.toBeNull();
    expect(kernel.findMatchingGrant(proposal({ intent_id: "late", deadline_at: new Date(c.now().getTime() + 60_000).toISOString() }))).toBeNull();
  });

  test("wrong arguments, scope, and lineage never match a grant", () => {
    const kernel = createSafetyKernel({ clock: clock() });
    const g = grant(kernel, { uses_remaining: null });
    expect(kernel.authorize(proposal({ intent_id: "wrong-args", args: { template_id: "git.status", repo: "/other" } }), g.id).verdict.code).toBe("grant_mismatch");
    expect(kernel.authorize(proposal({ intent_id: "wrong-scope", scope: { ...scope, host: "other-host" } }), g.id).verdict.code).toBe("grant_mismatch");
    expect(kernel.authorize(proposal({ intent_id: "wrong-lineage", lineage: { ...lineage, request_id: "perm-2" } }), g.id).verdict.code).toBe("grant_mismatch");
  });

  test("dangerous content is blocked even with a matching grant", () => {
    const kernel = createSafetyKernel({ clock: clock() });
    const dangerous = proposal({ intent_id: "danger", args: { template_id: "run", command: "git push --force origin main" } });
    const g = grant(kernel, { constraints: { args: dangerous.args, action_class: "probe" }, uses_remaining: null });
    const out = kernel.authorize(dangerous, g.id);
    expect(out.verdict.code).toBe("dangerous_content");
    expect(out.effect.state).toBe("blocked");
  });

  test("panic outranks a grant and is reversible only by core", () => {
    const kernel = createSafetyKernel({ clock: clock() });
    const g = grant(kernel, { uses_remaining: null });
    const beforePanic = kernel.authorize(proposal({ intent_id: "before-panic" }), g.id);
    expect(beforePanic.effect.state).toBe("pending");
    kernel.panic("operator emergency");
    expect(kernel.ledger.getEffect("before-panic")?.state).toBe("blocked");
    expect(kernel.authorize(proposal(), g.id).verdict.code).toBe("panic");
    kernel.clearPanic();
    expect(kernel.authorize(proposal({ intent_id: "after-panic" }), g.id).verdict.allowed).toBe(true);
  });

  test("SQLite-backed grants, effect context, and panic survive kernel recreation", () => {
    const c = clock();
    const store = memoryStore(c);
    const first = createSafetyKernel({ clock: c, ledger: new SqlSafetyLedger(store) });
    const reusable = grant(first, { lineage: null, uses_remaining: null });
    const authorized = first.authorize(proposal({ intent_id: "sql-first" }), reusable.id);
    expect(authorized.effect.scope).toEqual(scope);
    expect(authorized.effect.lineage).toEqual(lineage);
    first.panic("restart-safe panic");

    const restarted = createSafetyKernel({ clock: c, ledger: new SqlSafetyLedger(store) });
    expect(restarted.snapshot()).toMatchObject({ panic: true, panic_reason: "restart-safe panic" });
    expect(restarted.getGrant(reusable.id)?.lineage).toBeNull();
    expect(restarted.authorize(proposal({
      intent_id: "sql-second",
      lineage: { ...lineage, request_id: "perm-2" },
    }), reusable.id).verdict.code).toBe("panic");
    restarted.clearPanic();
    expect(restarted.authorize(proposal({
      intent_id: "sql-third",
      lineage: { ...lineage, request_id: "perm-3" },
    }), reusable.id).verdict.allowed).toBe(true);
  });

  test("a crash after authorization does not burn one-shot authority", () => {
    const c = clock();
    const store = memoryStore(c);
    const first = createSafetyKernel({ clock: c, ledger: new SqlSafetyLedger(store) });
    const oneShot = grant(first);
    const authorized = first.authorize(proposal({ intent_id: "crash-gap" }), oneShot.id);
    expect(authorized.verdict.allowed).toBe(true);
    expect(first.getGrant(oneShot.id)?.status).toBe("active");

    const restarted = createSafetyKernel({ clock: c, ledger: new SqlSafetyLedger(store) });
    const claimed = restarted.claim(authorized.effect.intent_id, "restarted-worker")!;
    expect(restarted.getGrant(oneShot.id)?.status).toBe("consumed");
    expect(restarted.recordTerminal(authorized.effect.intent_id, "restarted-worker", claimed.token, "ok").terminal_outcome).toBe("ok");
  });
});

describe("effect lifecycle fencing", () => {
  test("claim, renew, and terminal recording require the opaque claim token", () => {
    const c = clock();
    const kernel = createSafetyKernel({ clock: c, limits: { default_lease_ms: 1_000 } });
    const g = grant(kernel, { uses_remaining: null });
    const auth = kernel.authorize(proposal(), g.id);
    const claim = kernel.claim(auth.effect.intent_id, "worker-a");
    expect(claim).not.toBeNull();
    expect(kernel.renew(auth.effect.intent_id, "worker-b", claim!.token)).toBe(false);
    expect(() => kernel.recordTerminal(auth.effect.intent_id, "worker-b", claim!.token, "ok")).toThrow(/claim/);
    expect(kernel.recordTerminal(auth.effect.intent_id, "worker-a", claim!.token, "ok").terminal_outcome).toBe("ok");
    expect(kernel.recordTerminal(auth.effect.intent_id, "worker-a", claim!.token, "ok").state).toBe("terminal_recorded");
    expect(() => kernel.recordTerminal(auth.effect.intent_id, "worker-a", claim!.token, "failed")).toThrow(/different terminal/);
  });

  test("expired leases cannot record terminal truth", () => {
    const c = clock();
    const kernel = createSafetyKernel({ clock: c, limits: { default_lease_ms: 1_000 } });
    const g = grant(kernel, { uses_remaining: null });
    const auth = kernel.authorize(proposal(), g.id);
    const claim = kernel.claim(auth.effect.intent_id, "worker-a")!;
    c.advance(1_001);
    expect(() => kernel.recordTerminal(auth.effect.intent_id, "worker-a", claim.token, "ok")).toThrow(/expired/);
  });

  test("an expired running claim is never blindly reclaimed", () => {
    const c = clock();
    const kernel = createSafetyKernel({ clock: c, limits: { default_lease_ms: 1_000 } });
    const g = grant(kernel, { uses_remaining: null });
    const auth = kernel.authorize(proposal(), g.id);
    const first = kernel.claim(auth.effect.intent_id, "worker-a")!;
    c.advance(1_001);
    expect(kernel.claim(auth.effect.intent_id, "worker-b")).toBeNull();
    expect(() => kernel.recordTerminal(auth.effect.intent_id, "worker-a", first.token, "ok")).toThrow(/expired/);
  });

  test("failures open the breaker and rates/budget remain bounded", () => {
    const c = clock();
    const kernel = createSafetyKernel({ clock: c, limits: { max_failures_per_window: 2, max_attempts_per_window: 3, max_spend_usd: 2 } });
    for (let n = 1; n <= 2; n++) {
      const p = proposal({ intent_id: `failure-${n}`, args: { template_id: "git.status", repo: `/repo-${n}` }, cost_usd: 1 });
      const g = grant(kernel, { intent_id: `grant-failure-${n}`, constraints: { args: p.args, action_class: "probe" }, uses_remaining: null });
      const a = kernel.authorize(p, g.id);
      const claim = kernel.claim(a.effect.intent_id, `worker-${n}`)!;
      kernel.recordTerminal(a.effect.intent_id, `worker-${n}`, claim.token, "failed");
    }
    expect(kernel.snapshot().breaker_open).toBe(true);
    const g3 = grant(kernel, { intent_id: "grant-failure-3", constraints: { args: { template_id: "git.status", repo: "/repo-3" }, action_class: "probe" }, uses_remaining: null });
    expect(kernel.authorize(proposal({ intent_id: "failure-3", args: { template_id: "git.status", repo: "/repo-3" } }), g3.id).verdict.code).toBe("circuit_breaker");
  });

  test("effect spend is a rolling 24-hour budget rather than a permanent lock", () => {
    const c = clock();
    const kernel = createSafetyKernel({ clock: c, limits: { max_spend_usd: 1 } });
    const first = proposal({ intent_id: "spend-1", args: { template_id: "git.status", repo: "/repo-1" }, cost_usd: 1 });
    const firstGrant = grant(kernel, { intent_id: "spend-grant-1", constraints: { args: first.args, action_class: "probe" }, uses_remaining: null });
    expect(kernel.authorize(first, firstGrant.id).verdict.allowed).toBe(true);

    const second = proposal({ intent_id: "spend-2", args: { template_id: "git.status", repo: "/repo-2" }, cost_usd: 0.01 });
    const secondGrant = grant(kernel, { intent_id: "spend-grant-2", constraints: { args: second.args, action_class: "probe" }, uses_remaining: null });
    expect(kernel.authorize(second, secondGrant.id).verdict.code).toBe("budget");

    c.advance(24 * 60 * 60_000 + 1);
    expect(kernel.authorize({ ...second, intent_id: "spend-3" }, secondGrant.id).verdict.allowed).toBe(true);
  });

  test("SQLite claim transaction revalidates dedupe after delayed authorization", () => {
    const c = clock();
    const store = memoryStore(c);
    const authorizer = createSafetyKernel({ clock: c, ledger: new SqlSafetyLedger(store), limits: { dedupe_window_ms: 0 } });
    const reusable = grant(authorizer, { lineage: null, uses_remaining: null });
    const first = authorizer.authorize(proposal({ intent_id: "dedupe-pending-1" }), reusable.id);
    c.advance(1);
    const second = authorizer.authorize(proposal({ intent_id: "dedupe-pending-2" }), reusable.id);
    expect(first.verdict.allowed).toBe(true);
    expect(second.verdict.allowed).toBe(true);

    const claimer = createSafetyKernel({ clock: c, ledger: new SqlSafetyLedger(store) });
    expect(claimer.claim(first.effect.intent_id, "worker-1")).not.toBeNull();
    expect(claimer.claim(second.effect.intent_id, "worker-2")).toBeNull();
    expect(store.getEffectByIntent(second.effect.intent_id)).toMatchObject({ state: "blocked", safety_verdict: "dedupe" });
  });

  test("SQLite claim transaction serializes rolling rate and spend reservations", () => {
    const c = clock();
    const store = memoryStore(c);
    const authorizer = createSafetyKernel({ clock: c, ledger: new SqlSafetyLedger(store), limits: { dedupe_window_ms: 0 } });
    const pending: { intent: string; grantId: string }[] = [];
    for (let n = 1; n <= 2; n++) {
      const item = proposal({
        intent_id: `budget-pending-${n}`,
        args: { template_id: "git.status", repo: `/budget-${n}` },
        cost_usd: 0.6,
      });
      const authority = grant(authorizer, {
        intent_id: `budget-grant-${n}`,
        lineage: null,
        uses_remaining: null,
        constraints: { args: item.args, action_class: "probe" },
      });
      expect(authorizer.authorize(item, authority.id).verdict.allowed).toBe(true);
      pending.push({ intent: item.intent_id, grantId: authority.id });
    }

    const budgetClaimer = createSafetyKernel({
      clock: c,
      ledger: new SqlSafetyLedger(store),
      limits: { max_spend_usd: 1, max_attempts_per_window: 10 },
    });
    expect(budgetClaimer.claim(pending[0]!.intent, "budget-worker-1")).not.toBeNull();
    expect(budgetClaimer.claim(pending[1]!.intent, "budget-worker-2")).toBeNull();
    expect(store.getEffectByIntent(pending[1]!.intent)).toMatchObject({ state: "blocked", safety_verdict: "budget" });

    const rateStore = memoryStore(c);
    const rateAuthorizer = createSafetyKernel({ clock: c, ledger: new SqlSafetyLedger(rateStore), limits: { dedupe_window_ms: 0 } });
    const rateIntents: string[] = [];
    for (let n = 1; n <= 2; n++) {
      const item = proposal({ intent_id: `rate-pending-${n}`, args: { template_id: "git.status", repo: `/rate-${n}` } });
      const authority = grant(rateAuthorizer, {
        intent_id: `rate-grant-${n}`,
        lineage: null,
        uses_remaining: null,
        constraints: { args: item.args, action_class: "probe" },
      });
      rateAuthorizer.authorize(item, authority.id);
      rateIntents.push(item.intent_id);
    }
    const rateClaimer = createSafetyKernel({
      clock: c,
      ledger: new SqlSafetyLedger(rateStore),
      limits: { max_attempts_per_window: 1 },
    });
    expect(rateClaimer.claim(rateIntents[0]!, "rate-worker-1")).not.toBeNull();
    expect(rateClaimer.claim(rateIntents[1]!, "rate-worker-2")).toBeNull();
    expect(rateStore.getEffectByIntent(rateIntents[1]!)).toMatchObject({ state: "blocked", safety_verdict: "rate_limit" });
  });

  test("SQLite claim observes durable panic and breaker state created after authorization", () => {
    const c = clock();
    const store = memoryStore(c);
    const loose = createSafetyKernel({ clock: c, ledger: new SqlSafetyLedger(store), limits: { max_failures_per_window: 0, dedupe_window_ms: 0 } });
    const pending: string[] = [];
    for (let n = 1; n <= 3; n++) {
      const item = proposal({ intent_id: `breaker-${n}`, args: { template_id: "git.status", repo: `/breaker-${n}` } });
      const authority = grant(loose, {
        intent_id: `breaker-grant-${n}`,
        lineage: null,
        uses_remaining: null,
        constraints: { args: item.args, action_class: "probe" },
      });
      loose.authorize(item, authority.id);
      pending.push(item.intent_id);
    }
    for (let n = 0; n < 2; n++) {
      const claimed = loose.claim(pending[n]!, `failure-worker-${n}`)!;
      loose.recordTerminal(pending[n]!, `failure-worker-${n}`, claimed.token, "failed");
    }
    const strict = createSafetyKernel({ clock: c, ledger: new SqlSafetyLedger(store), limits: { max_failures_per_window: 2 } });
    expect(strict.claim(pending[2]!, "breaker-worker")).toBeNull();
    expect(store.getEffectByIntent(pending[2]!)).toMatchObject({ state: "blocked", safety_verdict: "circuit_breaker" });

    const panicItem = proposal({ intent_id: "external-panic", args: { template_id: "git.status", repo: "/panic" } });
    const panicGrant = grant(loose, {
      intent_id: "external-panic-grant",
      lineage: null,
      uses_remaining: null,
      constraints: { args: panicItem.args, action_class: "probe" },
    });
    loose.authorize(panicItem, panicGrant.id);
    store.setPanic("another process stopped execution");
    expect(loose.claim(panicItem.intent_id, "stale-kernel-worker")).toBeNull();
    expect(store.getEffectByIntent(panicItem.intent_id)).toMatchObject({ state: "blocked", safety_verdict: "panic" });
  });
});
