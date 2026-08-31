import { describe, expect, test } from "bun:test";
import { memoryStore, FakeClock } from "./fakes.ts";
import { IdempotencyConflictError, StaleClaimError, openDb } from "../src/store/db.ts";
import { parseEvent, CONTRACT_VERSION } from "../src/contract/events.ts";
import { MIGRATIONS } from "../src/store/migrations.ts";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Database } from "bun:sqlite";

function ev(overrides: Record<string, unknown> = {}) {
  return parseEvent({
    contract: CONTRACT_VERSION,
    idempotency_key: "agentctl:exec-1:terminal",
    ts: "2026-08-26T12:00:00Z",
    source: { vendor: "agentctl", host: "mac", adapter: "subscribe-webhook" },
    session: { vendor: "codex", native_id: "uuid-1", host: "mac", repo: "github.com/x/y" },
    type: "attention.question",
    severity: "attention",
    requires_response: true,
    title: "Which migration strategy?",
    ...overrides,
  });
}

describe("store", () => {
  test("additive migration preserves existing v1 events and makes them source-scoped", () => {
    const dir = mkdtempSync(join(tmpdir(), "car-v3-migration-"));
    const path = join(dir, "car.db");
    try {
      const v1 = new Database(path, { create: true });
      v1.exec(MIGRATIONS[0]!);
      v1.exec("PRAGMA user_version = 1");
      v1.query(
        `INSERT INTO events
         (id, idempotency_key, type, severity, ts, received_at, requires_response,
          title, body, payload_json, actor, source_vendor, source_host, source_adapter)
         VALUES ('evt_old', 'old-key', 'note', 'info', '2026-08-26T12:00:00Z',
          '2026-08-26T12:00:00Z', 0, 'old', '', '{}', 'external', 'cron', 'mac', 'curl')`,
      ).run();
      v1.close();

      const migrated = openDb(path);
      expect((migrated.query("PRAGMA user_version").get() as { user_version: number }).user_version).toBe(
        MIGRATIONS.length,
      );
      expect(migrated.query("SELECT id, source_id FROM events WHERE id = 'evt_old'").get()).toMatchObject({
        id: "evt_old",
      });
      expect(
        (migrated.query("SELECT COUNT(*) AS n FROM idempotency WHERE object_id = 'evt_old'").get() as { n: number })
          .n,
      ).toBe(1);
      const effectColumns = (migrated.query("PRAGMA table_info(effects)").all() as { name: string }[]).map((row) => row.name);
      expect(effectColumns).toEqual(expect.arrayContaining(["scope_json", "lineage_json", "deadline_at", "action_class", "cost_usd"]));
      migrated.close();
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  test("ingest is idempotent on idempotency_key", () => {
    const store = memoryStore();
    const first = store.ingestEvent(ev());
    const second = store.ingestEvent(ev());
    expect(first.inserted).toBe(true);
    expect(second.inserted).toBe(false);
    expect(second.event_id).toBe(first.event_id);
    const n = (store.db.query("SELECT COUNT(*) n FROM events").get() as { n: number }).n;
    expect(n).toBe(1);
  });

  test("same session ref maps to one car session; distinct refs can be linked", () => {
    const store = memoryStore();
    const a = store.ingestEvent(ev());
    const b = store.ingestEvent(ev({ idempotency_key: "agentctl:exec-1:artifact", type: "artifact" }));
    expect(a.car_session_id).toBe(b.car_session_id);

    store.linkSessionRef(a.car_session_id!, { vendor: "agentctl", host: "mac", native_id: "exec-1" });
    const c = store.ingestEvent(
      ev({
        idempotency_key: "agentctl:exec-1:heartbeat",
        type: "heartbeat",
        session: { vendor: "agentctl", native_id: "exec-1", host: "mac" },
      }),
    );
    expect(c.car_session_id).toBe(a.car_session_id);
  });

  test("an unverified repo label cannot replace a verified session identity", () => {
    const store = memoryStore();
    const first = store.ingestEvent(ev({
      session: {
        vendor: "codex",
        native_id: "uuid-verified",
        host: "mac",
        repo: "github.com/acme/canonical",
        repo_verified: true,
      },
    }), { verifiedRepo: "github.com/acme/canonical" });
    store.ingestEvent(ev({
      idempotency_key: "agentctl:exec-1:second",
      session: {
        vendor: "codex",
        native_id: "uuid-verified",
        host: "mac",
        repo: "cwd-basename-label",
      },
    }));

    expect(
      store.db.query("SELECT repo, repo_verified FROM sessions WHERE car_session_id = ?").get(first.car_session_id!) as {
        repo: string;
        repo_verified: number;
      },
    ).toEqual({ repo: "github.com/acme/canonical", repo_verified: 1 });
  });

  test("claim leases events and reclaims expired leases", () => {
    const clock = new FakeClock();
    const store = memoryStore(clock);
    store.ingestEvent(ev());
    const claimed = store.claimPendingEvents(10, 120);
    expect(claimed.length).toBe(1);
    // still leased: nothing to claim
    expect(store.claimPendingEvents(10, 120).length).toBe(0);
    // lease expires: reclaimable
    clock.advance(121 * 1000);
    expect(store.claimPendingEvents(10, 120).length).toBe(1);
  });

  test("every ingest writes audit", () => {
    const store = memoryStore();
    store.ingestEvent(ev());
    const n = (store.db.query("SELECT COUNT(*) n FROM audit WHERE verb = 'event.ingested'").get() as { n: number }).n;
    expect(n).toBe(1);
  });

  test("agent run projection is durable, quiet on refresh, and rejects stale snapshots", () => {
    const store = memoryStore();
    store.upsertAgentRun({ executionId: "exec-run-1", agent: "cursor", state: "running", liveness: "healthy", updatedAt: "2026-08-26T12:00:00Z", statusRevision: 2 });
    store.upsertAgentRun({ executionId: "exec-run-1", agent: "cursor", state: "running", liveness: "healthy", updatedAt: "2026-08-26T12:00:00Z", statusRevision: 2 });
    expect((store.db.query("SELECT COUNT(*) n FROM audit WHERE object_type = 'agent_run'").get() as { n: number }).n).toBe(1);
    store.upsertAgentRun({ executionId: "exec-run-1", agent: "cursor", state: "completed", liveness: "exited", updatedAt: "2026-08-26T12:01:00Z", terminalAt: "2026-08-26T12:01:00Z", statusRevision: 3 });
    store.upsertAgentRun({ executionId: "exec-run-1", agent: "cursor", state: "running", liveness: "healthy", updatedAt: "2026-08-26T11:59:00Z", statusRevision: 1 });
    expect(store.getAgentRun("exec-run-1")?.state).toBe("completed");
    // Terminal truth is monotonic even when a later malformed snapshot tries
    // to regress it, and timestamp comparison handles RFC3339 fraction width.
    store.upsertAgentRun({ executionId: "exec-run-1", agent: "cursor", state: "running", liveness: "alive", updatedAt: "2026-08-26T12:01:01.1Z" });
    expect(store.getAgentRun("exec-run-1")?.state).toBe("completed");
    store.upsertAgentRun({ executionId: "exec-fraction", agent: "omp", state: "running", liveness: "alive", updatedAt: "2026-08-26T12:00:00.900Z" });
    store.upsertAgentRun({ executionId: "exec-fraction", agent: "omp", state: "starting", liveness: "unknown", updatedAt: "2026-08-26T12:00:00Z" });
    expect(store.getAgentRun("exec-fraction")?.state).toBe("running");
    expect((store.db.query("SELECT COUNT(*) n FROM audit WHERE object_type = 'agent_run'").get() as { n: number }).n).toBe(3);
  });

  test("spend accumulates by day/provider/model", () => {
    const store = memoryStore();
    store.recordSpend("anthropic", "claude-haiku-4-5", 100, 50, 0.01);
    store.recordSpend("anthropic", "claude-haiku-4-5", 200, 80, 0.02);
    const today = store.spendToday();
    expect(today.calls).toBe(2);
    expect(today.cost_usd).toBeCloseTo(0.03);
  });

  test("idempotency is source scoped and rejects a same-source payload conflict", () => {
    const store = memoryStore();
    const first = store.ingestEvent(ev());
    const otherSource = store.ingestEvent(
      ev({
        source: { vendor: "codex", host: "mac", adapter: "subscribe-webhook" },
        title: "same producer key, different source",
      }),
    );
    expect(otherSource.inserted).toBe(true);
    expect(otherSource.event_id).not.toBe(first.event_id);
    expect(() => store.ingestEvent(ev({ title: "changed payload" }))).toThrow(IdempotencyConflictError);
    expect(store.reserveIdempotency("producer", "k", "hash")).toBe("inserted");
    expect(store.reserveIdempotency("producer", "k", "hash")).toBe("duplicate");
    expect(store.reserveIdempotency("producer", "k", "other")).toBe("conflict");
  });

  test("event claims are fenced so a stale worker cannot finish a reclaimed row", () => {
    const clock = new FakeClock();
    const store = memoryStore(clock);
    const inserted = store.ingestEvent(ev());
    const first = store.claimEvents(1, 10, "worker-a")[0]!;
    clock.advance(11_000);
    const second = store.claimEvents(1, 10, "worker-b")[0]!;
    expect(second.route_claim_token).not.toBe(first.route_claim_token);
    expect(() => store.completeEventClaim(inserted.event_id, {
      owner: "worker-a",
      token: first.route_claim_token!,
    }, "rules_resolved")).toThrow(StaleClaimError);
    store.completeEventClaim(inserted.event_id, {
      owner: "worker-b",
      token: second.route_claim_token!,
    }, "rules_resolved");
    expect(store.getEvent(inserted.event_id)?.triage_state).toBe("rules_resolved");
  });

  test("provider terminal state is durable, replayable, and conflict detecting", () => {
    const store = memoryStore();
    const created = store.createProviderInvocation({
      providerId: "native",
      providerInstance: "native:default",
      providerVersion: "1",
      capability: "operator",
      requestId: "req-1",
    });
    const claimed = store.claimProviderInvocation(created.id, "provider-worker", 120)!;
    store.recordProviderTerminal(created.id, {
      owner: "provider-worker",
      token: claimed.claim_token!,
    }, "succeeded", { responseRef: "response-1" });
    expect(store.getProviderInvocation(created.id)?.terminal_outcome).toBe("succeeded");
    expect(() => store.recordProviderTerminal(created.id, {
      owner: "provider-worker",
      token: claimed.claim_token!,
    }, "failed")).toThrow(IdempotencyConflictError);
  });

  test("effects, interactions, one-shot grants, and uncertain outbox receipts survive replay", () => {
    const clock = new FakeClock();
    const store = memoryStore(clock);
    const effect = store.createEffect({
      intentId: "intent-effect-1",
      type: "notify",
      args: { text: "hello" },
      lineageId: "lineage-1",
      state: "pending",
    });
    const claimedEffect = store.claimPendingEffects(1, "effect-worker", 10)[0]!;
    store.recordEffectTerminal(effect.id, {
      owner: "effect-worker",
      token: claimedEffect.claim_token!,
    }, "ok", { delivered: true });
    const interaction = store.recordInteraction({
      sourceId: "telegram:user-1",
      idempotencyKey: "tap-1",
      kind: "feedback",
      targetType: "effect",
      targetId: effect.id,
      actorId: "david",
      body: { verdict: "confirmed" },
    });
    expect(store.recordInteraction({
      sourceId: "telegram:user-1",
      idempotencyKey: "tap-1",
      kind: "feedback",
      targetType: "effect",
      targetId: effect.id,
      actorId: "david",
      body: { verdict: "confirmed" },
    }).factId).toBe(interaction.factId);
    const grant = store.createGrant({
      intentId: "grant-intent-1",
      scope: { repo: "github.com/acme/car" },
      effectType: "notify",
      usesRemaining: 1,
      createdBy: "david",
    });
    expect(store.getGrant(grant.id)?.effect_type).toBe("notify");
    expect(store.listGrants().map((row) => row.id)).toContain(grant.id);
    expect(store.upsertGrant({
      intentId: "grant-intent-1",
      scope: { repo: "github.com/acme/car" },
      effectType: "notify",
      usesRemaining: 1,
      createdBy: "david",
      status: "active",
    }).inserted).toBe(false);
    expect(store.consumeGrant(grant.id)).toBe(true);
    expect(store.consumeGrant(grant.id)).toBe(false);
    const outbox = store.enqueueOutboxIntent({ intentId: "intent-message-1", channel: "telegram", target: { chat: 1 }, body: { text: "hello" } });
    const claimedOutbox = store.claimPendingOutbox(1, "outbox-worker", 10)[0]!;
    clock.advance(11_000);
    expect(store.recoverExpiredClaims().outbox).toBe(1);
    expect(store.db.query("SELECT state, intent_id FROM outbox WHERE id = ?").get(outbox.outboxId)).toMatchObject({
      state: "uncertain",
      intent_id: "intent-message-1",
    });
    expect(claimedOutbox.claim_token).toBeTruthy();

    const uncertainEffect = store.createEffect({
      intentId: "intent-effect-crash",
      type: "notify",
      args: { text: "possibly sent" },
      lineageId: "lineage-crash",
      state: "pending",
    });
    store.claimPendingEffects(1, "crashed-effect-worker", 10);
    clock.advance(11_000);
    expect(store.recoverExpiredClaims().effects).toBe(1);
    expect(store.getEffect(uncertainEffect.id)).toMatchObject({
      state: "terminal_recorded",
      terminal_outcome: "uncertain",
      recovery_state: "execution_outcome_unknown",
    });
    expect(store.claimPendingEffects(1, "replacement-effect-worker", 10)).toHaveLength(0);
  });

  test("intent-keyed safety APIs preserve effect context and fence terminal writes", () => {
    const clock = new FakeClock();
    const store = memoryStore(clock);
    const created = store.upsertEffect({
      intentId: "intent-safety-1",
      type: "run_template",
      args: { template: "git.status" },
      lineageId: "legacy-lineage",
      scope: { repo: "github.com/acme/car", repo_verified: true },
      lineage: { source_id: "claude", request_id: "req-1" },
      deadlineAt: "2026-08-26T12:05:00.000Z",
      actionClass: "probe",
      costUsd: 0.02,
    });
    expect(created.inserted).toBe(true);
    expect(store.getEffect("intent-safety-1")?.scope_json).toContain("repo_verified");
    expect(store.getEffectByIntent("intent-safety-1")?.lineage_json).toContain("request_id");
    store.createGrant({
      id: "grant-1",
      intentId: "grant-intent-safety-1",
      lineageId: null,
      scope: { repo: "github.com/acme/car", repo_verified: true },
      effectType: "run_template",
      constraints: { args: { template: "git.status" }, action_class: "probe" },
      usesRemaining: null,
      createdBy: "human",
    });
    const authorized = store.authorizeEffect("intent-safety-1", "grant-1", "allowed");
    expect(authorized.state).toBe("pending");
    const token = "opaque-token";
    expect(store.claimEffect(
      "intent-safety-1",
      "grant-1",
      "safety-worker",
      token,
      "2026-08-26T12:02:00.000Z",
      clock.now().toISOString(),
      clock.now().toISOString(),
      {
        maxAttemptsPerWindow: 0,
        attemptWindowMs: 60_000,
        maxFailuresPerWindow: 0,
        failureWindowMs: 60_000,
        spendWindowMs: 60_000,
        maxSpendUsd: 0,
        dedupeWindowMs: 60_000,
        dangerousArgsSha256: null,
      },
    )).toEqual({ claimed: true });
    expect(store.recordTerminalEffect("intent-safety-1", "safety-worker", token, "ok", clock.now().toISOString(), { output: "ok" }, clock.now().toISOString())).toBe(true);
    expect(store.recordTerminalEffect("intent-safety-1", "safety-worker", token, "ok", clock.now().toISOString(), { output: "replay" }, clock.now().toISOString())).toBe(true);
    expect(() => store.recordTerminalEffect("intent-safety-1", "safety-worker", token, "failed", clock.now().toISOString(), null, clock.now().toISOString())).toThrow(IdempotencyConflictError);
  });

  test("daemon ownership is single-flight, fenced, and crash-reclaimable", () => {
    const clock = new FakeClock();
    const store = memoryStore(clock);
    const first = store.claimDaemonOwner("daemon-a", 30);
    expect(first).not.toBeNull();
    expect(store.claimDaemonOwner("daemon-b", 30)).toBeNull();
    expect(store.renewDaemonOwner({ owner: first!.owner, token: "stale" }, 30)).toBe(false);
    expect(store.renewDaemonOwner(first!, 30)).toBe(true);
    clock.advance(31_000);
    const replacement = store.claimDaemonOwner("daemon-b", 30);
    expect(replacement).not.toBeNull();
    expect(store.releaseDaemonOwner(first!)).toBe(false);
    expect(store.releaseDaemonOwner(replacement!)).toBe(true);
  });

  test("panic state is a typed durable record", () => {
    const store = memoryStore();
    expect(store.getPanicState()).toMatchObject({ active: false, reason: null });
    expect(store.setPanic("operator requested stop")).toMatchObject({ active: true, reason: "operator requested stop" });
    expect(store.getPanicState()).toMatchObject({ active: true, reason: "operator requested stop" });
    expect(store.clearPanic()).toMatchObject({ active: false, reason: null });
  });
});
