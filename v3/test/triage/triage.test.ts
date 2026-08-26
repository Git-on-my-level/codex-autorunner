/**
 * Triage tick: claiming, the rules pass end-to-end, the coalescer, incident
 * lineage, and the safety rails that must never spend a token.
 */
import { describe, expect, test } from "bun:test";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createPolicy } from "../../src/policy/index.ts";
import { harness, grantedRule, resolveScript, StubMemoryReader } from "./harness.ts";

/** A throwaway state dir holding one policy.toml. */
function policyDir(toml: string): string {
  const dir = mkdtempSync(join(tmpdir(), "car-triage-policy-"));
  writeFileSync(join(dir, "policy.toml"), toml);
  return dir;
}

describe("rules pass end to end", () => {
  test("trivial events resolve without an incident, an LLM call, or a page", async () => {
    const h = harness();
    const ids = [
      h.emit({ type: "heartbeat", severity: "info", requires_response: false }),
      h.emit({ type: "progress", severity: "info", requires_response: false }),
      h.emit({ type: "session.started", severity: "info", requires_response: false }),
      h.emit({ type: "artifact", severity: "info", requires_response: false }),
    ];
    await h.triage.tick();

    for (const id of ids) expect(h.eventRow(id).triage_state).toBe("rules_resolved");
    expect(h.incidents()).toHaveLength(0);
    expect(h.llm.calls).toBe(0);
    expect(h.channel.escalations).toHaveLength(0);
  });

  test("urgent escalates immediately and NO LLM is ever consulted", async () => {
    const h = harness();
    const id = h.emit({ type: "attention.error", severity: "urgent", title: "disk full on mac-studio" });

    const processed = await h.triage.tick();

    expect(processed).toBe(1);
    expect(h.llm.calls).toBe(0); // the guarantee: no model between David and a page
    expect(h.channel.escalations).toHaveLength(1);
    expect(h.channel.escalations[0]!.severity).toBe("urgent");
    expect(h.channel.escalations[0]!.question).toBe("disk full on mac-studio");
    expect(h.eventRow(id).triage_state).toBe("escalated");

    const decisions = h.decisions();
    expect(decisions).toHaveLength(1);
    expect(decisions[0]!.decided_by).toBe("rules");
    expect(decisions[0]!.disposition).toBe("escalate");
    expect(decisions[0]!.cost_usd).toBe(0);

    expect(h.escalations()).toHaveLength(1);
    expect(h.escalations()[0]!.state).toBe("pending");
  });

  test("urgent does not wait for the coalesce window", async () => {
    const h = harness();
    h.emit({ severity: "urgent", title: "prod down" });
    // No clock advance at all.
    expect(await h.triage.tick()).toBe(1);
    expect(h.channel.escalations).toHaveLength(1);
  });

  test("a note below attention is resolved silently", async () => {
    const h = harness();
    const id = h.emit({ type: "note", severity: "info", title: "fyi", requires_response: false });
    await h.triage.tick();
    expect(h.eventRow(id).triage_state).toBe("rules_resolved");
    expect(h.llm.calls).toBe(0);
  });

  test("an expired event is marked expired, not triaged", async () => {
    const h = harness();
    const id = h.emit({ expires_at: "2026-08-26T11:00:00Z" });
    await h.triage.tick();
    expect(h.eventRow(id).triage_state).toBe("expired");
    expect(h.llm.calls).toBe(0);
  });
});

describe("self-event suppression", () => {
  test("actor=car attaches to the originating incident and never opens LLM triage", async () => {
    const h = harness({ script: [[{ tool: "escalate", args: { question: "need a human" } }]] });

    // 1. A real event opens an incident and escalates.
    h.emit({ title: "agent blocked" });
    await h.settle();
    const incident = h.incidents()[0]!;
    expect(h.llm.calls).toBe(1);

    // 2. CAR's own labelled follow-up must not start a second triage run.
    const selfId = h.emit(
      { idempotency_key: "car:sess-1:action:1", type: "attention.error", title: "CAR reply delivery report" },
      { actor: "car" },
    );
    const processed = await h.triage.tick();

    expect(processed).toBe(0);
    expect(h.llm.calls).toBe(1); // unchanged — no fresh LLM triage
    expect(h.eventRow(selfId).triage_state).toBe("rules_resolved");
    expect(h.eventRow(selfId).incident_id).toBe(incident.id);
    expect(h.incidents()).toHaveLength(1);
  });

  test("a self-event with no open incident is still suppressed", async () => {
    const h = harness();
    const id = h.emit({ type: "attention.error", title: "CAR probe output" }, { actor: "car" });
    expect(await h.triage.tick()).toBe(0);
    expect(h.eventRow(id).triage_state).toBe("rules_resolved");
    expect(h.eventRow(id).incident_id).toBeNull();
    expect(h.llm.calls).toBe(0);
  });

  test("an urgent CAR event still pages David, on the originating incident", async () => {
    const h = harness({ script: [[{ tool: "escalate", args: { question: "need a human" } }]] });
    h.emit({ title: "agent blocked" });
    await h.settle();
    const incident = h.incidents()[0]!;

    const urgentId = h.emit(
      {
        idempotency_key: "car:sess-1:action:fatal",
        type: "attention.error",
        severity: "urgent",
        title: "CAR could not deliver the reply",
      },
      { actor: "car" },
    );
    expect(await h.triage.tick()).toBe(1);

    expect(h.llm.calls).toBe(1); // no second LLM run
    expect(h.channel.escalations).toHaveLength(2);
    expect(h.channel.escalations[1]!.severity).toBe("urgent");
    expect(h.incidents()).toHaveLength(1); // attached, not a parallel story
    expect(h.eventRow(urgentId).incident_id).toBe(incident.id);
  });

  test("a storm of CAR self-events cannot run up a bill", async () => {
    const h = harness();
    for (let i = 0; i < 25; i++) {
      h.emit({ idempotency_key: `car:sess-1:action:${i}`, type: "attention.error" }, { actor: "car" });
    }
    await h.settle();
    expect(h.llm.calls).toBe(0);
    expect(h.store.spendToday().cost_usd).toBe(0);
  });
});

describe("coalescer", () => {
  test("a burst on one session becomes ONE incident and ONE LLM run", async () => {
    const h = harness({ script: resolveScript("unblocked the agent") });
    const ids = [
      h.emit({ type: "attention.error", title: "build failed" }),
      h.emit({ type: "attention.idle", title: "waiting on input" }),
      h.emit({ type: "attention.question", title: "which branch?" }),
    ];

    // Inside the window nothing is released.
    expect(await h.triage.tick()).toBe(0);
    expect(h.llm.calls).toBe(0);
    expect(h.incidents()).toHaveLength(0);
    for (const id of ids) expect(h.eventRow(id).triage_state).toBe("coalescing");

    h.clock.advance(21_000);
    expect(await h.triage.tick()).toBe(1);

    expect(h.llm.calls).toBe(1);
    expect(h.incidents()).toHaveLength(1);
    const incident = h.incidents()[0]!;
    for (const id of ids) {
      expect(h.eventRow(id).triage_state).toBe("llm_resolved");
      expect(h.eventRow(id).incident_id).toBe(incident.id);
    }
    expect(incident.state).toBe("resolved");
  });

  test("the window restarts when a newer event lands", async () => {
    const h = harness({ script: resolveScript() });
    h.emit({ title: "first" });

    h.clock.advance(15_000);
    expect(await h.triage.tick()).toBe(0);

    h.emit({ title: "second" }); // resets the debounce
    h.clock.advance(15_000);
    expect(await h.triage.tick()).toBe(0);
    expect(h.llm.calls).toBe(0);

    h.clock.advance(10_000);
    expect(await h.triage.tick()).toBe(1);
    expect(h.llm.calls).toBe(1);
  });

  test("different sessions coalesce independently", async () => {
    const h = harness({ script: [resolveScript()[0]!, resolveScript()[0]!] });
    h.emit({ title: "session one" });
    h.emit({
      idempotency_key: "codex:sess-2:Question:1",
      title: "session two",
      source: { vendor: "codex", host: "mac-studio", adapter: "webhook" },
      session: { vendor: "codex", native_id: "sess-2", host: "mac-studio", title: "other work" },
    });

    expect(await h.settle()).toBe(2);
    expect(h.llm.calls).toBe(2);
    expect(h.incidents()).toHaveLength(2);
  });

  test("sessionless events (cron/CI) each get their own batch", async () => {
    const h = harness({ script: [resolveScript()[0]!, resolveScript()[0]!] });
    h.emit({
      idempotency_key: "ci:build:1",
      session: null,
      source: { vendor: "ci", host: "runner-1", adapter: "generic" },
      title: "nightly build failed",
    });
    h.emit({
      idempotency_key: "cron:backup:1",
      session: null,
      source: { vendor: "cron", host: "mac-studio", adapter: "generic" },
      title: "backup skipped",
    });

    expect(await h.settle()).toBe(2);
    expect(h.incidents()).toHaveLength(2);
  });

  test("coalescing survives a tick with nothing new (state is durable, not in-memory)", async () => {
    const h = harness({ script: resolveScript() });
    const id = h.emit({ title: "held" });
    await h.triage.tick();
    await h.triage.tick();
    await h.triage.tick();
    expect(h.eventRow(id).triage_state).toBe("coalescing");
    expect(h.llm.calls).toBe(0);

    h.clock.advance(21_000);
    await h.triage.tick();
    expect(h.eventRow(id).triage_state).toBe("llm_resolved");
  });
});

describe("granted rules", () => {
  test("a granted rule executes directly, with decided_by=rules and no LLM", async () => {
    const h = harness({ memory: new StubMemoryReader([grantedRule()]) });
    const id = h.emit({
      type: "attention.permission",
      title: "Permission: bun install",
      response_channel: { kind: "claude-hook-http", hint: { tool_use_id: "toolu_1" } },
    });

    expect(await h.triage.tick()).toBe(1);

    expect(h.llm.calls).toBe(0);
    expect(h.actions.delivered).toHaveLength(1);
    expect(h.actions.delivered[0]!.payload).toEqual({ approval: true });
    expect(h.channel.escalations).toHaveLength(0);

    const decision = h.decisions()[0]!;
    expect(decision.decided_by).toBe("rules");
    expect(decision.disposition).toBe("auto_resolve");
    expect(decision.action_class).toBe("approve_permission");
    expect(decision.cost_usd).toBe(0);

    expect(h.incidents()[0]!.state).toBe("resolved");
    expect(h.eventRow(id).triage_state).toBe("rules_resolved");
    expect(h.actionRows()).toHaveLength(1);
    expect(h.actionRows()[0]!.state).toBe("ok");
  });

  test("first use of a fresh grant notifies once, then goes quiet", async () => {
    const h = harness({ memory: new StubMemoryReader([grantedRule()]) });
    h.emit({ type: "attention.permission", idempotency_key: "claude-code:sess-1:Perm:1" });
    await h.triage.tick();
    expect(h.channel.notifies).toHaveLength(1);
    expect(h.channel.notifies[0]!.text).toContain("granted rule");

    h.clock.advance(60_000);
    h.emit({ type: "attention.permission", idempotency_key: "claude-code:sess-1:Perm:2" });
    await h.triage.tick();
    expect(h.channel.notifies).toHaveLength(1); // still one
  });

  test("a granted rule bypasses class enablement but not an explicit forbid", async () => {
    const dir = policyDir(`[classes.approve_permission]\nenabled = true\nallowlist = ["never-matches"]\n`);
    const h = harness({
      memory: new StubMemoryReader([grantedRule()]),
      config: { state_dir: dir },
      policyFactory: createPolicy,
    });
    h.emit({ type: "attention.permission", title: "Permission: rm -rf" });

    expect(await h.triage.tick()).toBe(1);
    expect(h.actions.delivered).toHaveLength(0);
    expect(h.channel.escalations).toHaveLength(1);
    expect(h.channel.escalations[0]!.contextLines.join(" ")).toContain("blocked");
    rmSync(dir, { recursive: true, force: true });
  });

  test("a granted rule still runs when its class is globally disabled (memory promotion path)", async () => {
    // DESIGN §5: approve_permission is `enabled = false` on purpose — per-rule
    // grants via memory promotion are the only way it ever fires.
    const dir = policyDir("[classes.approve_permission]\nenabled = false\n");
    const h = harness({
      memory: new StubMemoryReader([grantedRule()]),
      config: { state_dir: dir },
      policyFactory: createPolicy,
    });
    h.emit({ type: "attention.permission", title: "Permission: bun install" });

    await h.triage.tick();
    expect(h.actions.delivered).toHaveLength(1);
    expect(h.channel.escalations).toHaveLength(0);
    rmSync(dir, { recursive: true, force: true });
  });

  test("a granted rule blocked by a gate escalates instead of acting", async () => {
    const blocking = {
      check: () => "auto" as const,
      gate: () => "rate_limit: reply max_per_hour=10",
      escalateOnly: () => false,
    };
    const h = harness({ memory: new StubMemoryReader([grantedRule()]), policy: blocking });
    h.emit({ type: "attention.permission" });

    expect(await h.triage.tick()).toBe(1);
    expect(h.actions.delivered).toHaveLength(0);
    expect(h.channel.escalations).toHaveLength(1);
    expect(h.channel.escalations[0]!.contextLines.join(" ")).toContain("rate_limit");
    // The blocked attempt is still recorded — blocked attempts are evidence.
    expect(h.actionRows()[0]!.policy_verdict).toBe("blocked");
  });

  test("escalate-only mode suspends granted rules", async () => {
    const panicked = {
      check: () => "auto" as const,
      gate: () => null,
      escalateOnly: () => true,
    };
    const h = harness({ memory: new StubMemoryReader([grantedRule()]), policy: panicked });
    h.emit({ type: "attention.permission" });

    await h.triage.tick();
    expect(h.actions.delivered).toHaveLength(0);
    expect(h.channel.escalations).toHaveLength(1);
  });

  test("a granted rule whose delivery fails escalates rather than going quiet", async () => {
    const h = harness({ memory: new StubMemoryReader([grantedRule()]) });
    h.actions.deliverResult = "failed";
    h.emit({ type: "attention.permission" });

    await h.triage.tick();
    expect(h.channel.escalations).toHaveLength(1);
    expect(h.channel.escalations[0]!.contextLines.join(" ")).toContain("failed to execute");
  });
});

describe("escalate-only mode", () => {
  test("the breaker skips LLM triage entirely for a released batch", async () => {
    const panicked = {
      check: () => "auto" as const,
      gate: () => null,
      escalateOnly: () => true,
    };
    const h = harness({ script: resolveScript(), policy: panicked });
    h.emit({ type: "attention.error", title: "build failed" });

    expect(await h.settle()).toBe(1);
    expect(h.llm.calls).toBe(0);
    expect(h.store.spendToday().cost_usd).toBe(0);
    expect(h.channel.escalations).toHaveLength(1);
    expect(h.channel.escalations[0]!.contextLines.join(" ")).toContain("escalate-only");
  });

  test("budget exhaustion stops triage spend (real policy engine)", async () => {
    const dir = policyDir("[budget]\ntriage_daily_usd = 0.50\n");
    const h = harness({ script: resolveScript(), config: { state_dir: dir }, policyFactory: createPolicy });

    // Under budget: the LLM runs normally.
    h.emit({ type: "attention.error", idempotency_key: "claude-code:sess-1:Err:1", title: "build failed" });
    await h.settle();
    expect(h.llm.calls).toBe(1);

    // Blow the budget; the next batch escalates without a single token.
    h.store.recordSpend("anthropic", "claude-haiku-4-5", 10_000, 5_000, 0.75);
    h.emit({ type: "attention.error", idempotency_key: "claude-code:sess-1:Other:1", title: "tests failed" });
    expect(await h.settle()).toBe(1);

    expect(h.llm.calls).toBe(1); // unchanged
    expect(h.channel.escalations).toHaveLength(1);
    expect(h.channel.escalations[0]!.contextLines.join(" ")).toContain("escalate-only");
    rmSync(dir, { recursive: true, force: true });
  });

  test("the circuit breaker stops triage spend (real policy engine)", async () => {
    const dir = policyDir("[classes.reply]\nenabled = true\n");
    const h = harness({ script: resolveScript(), config: { state_dir: dir }, policyFactory: createPolicy });

    // Five failed actions in the window trip the breaker.
    for (let i = 0; i < 5; i++) {
      h.store.db
        .query(
          `INSERT INTO actions (id, decision_id, class, args_json, policy_verdict, dedupe_hash, state, started_at, finished_at)
           VALUES (?, 'dec_x', 'reply', '{}', 'auto', ?, 'failed', ?, ?)`,
        )
        .run(`act_${i}`, `h${i}`, h.clock.now().toISOString(), h.clock.now().toISOString());
    }

    h.emit({ type: "attention.error", title: "build failed" });
    expect(await h.settle()).toBe(1);
    expect(h.llm.calls).toBe(0);
    expect(h.channel.escalations).toHaveLength(1);
    rmSync(dir, { recursive: true, force: true });
  });
});

describe("audit completeness", () => {
  test("every decision and escalation leaves an audit trail", async () => {
    const h = harness({ script: [[{ tool: "escalate", args: { question: "force-push?" } }]] });
    h.emit({ title: "remote diverged" });
    await h.settle();

    const verbs = h.auditVerbs();
    expect(verbs).toContain("incident.opened");
    expect(verbs).toContain("triage.batch_released");
    expect(verbs).toContain("decision.recorded");
    expect(verbs).toContain("escalation.created");
    expect(verbs).toContain("triage.escalated");
    expect(verbs).toContain("incident.state");
  });

  test("rules resolutions are audited too", async () => {
    const h = harness();
    h.emit({ type: "heartbeat", severity: "info", requires_response: false });
    await h.triage.tick();
    expect(h.auditVerbs()).toContain("triage.rules_resolved");
  });
});
