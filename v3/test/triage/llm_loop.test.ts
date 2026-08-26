/**
 * The bounded LLM tool loop. Every path here runs on ScriptedLlm — no network,
 * $0 — and asserts decisions and policy blocks, never prose.
 *
 * The contract under test: a run ends with EXACTLY ONE terminal tool, inside a
 * tool-call budget, inside a per-lineage run budget. Any violation escalates.
 */
import { describe, expect, test } from "bun:test";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { parkPermission, hasParkedPermission } from "../../src/permission_park.ts";
import { createPolicy } from "../../src/policy/index.ts";
import { TERMINAL_TOOLS, TOOL_SPECS } from "../../src/triage/index.ts";
import { harness, resolveScript, StubMemoryReader } from "./harness.ts";

/* --------------------------------------------------------------- toolset shape */

describe("closed toolset", () => {
  test("exactly the DESIGN §5 tools are offered", () => {
    expect(TOOL_SPECS.map((t) => t.name).sort()).toEqual(
      [
        "approve_permission",
        "defer",
        "deny_permission",
        "escalate",
        "keep_informed",
        "memory_get",
        "memory_propose",
        "memory_search",
        "read_session_tail",
        "reply_to_agent",
        "resolve",
        "run_action",
        "run_probe",
        "session_context",
      ].sort(),
    );
  });

  test("the four terminals are marked as such", () => {
    expect([...TERMINAL_TOOLS].sort()).toEqual(["defer", "escalate", "keep_informed", "resolve"]);
  });

  test("every tool ships a JSON schema", () => {
    for (const spec of TOOL_SPECS) {
      expect(spec.schema.type).toBe("object");
      expect(spec.description.length).toBeGreaterThan(10);
    }
  });
});

/* --------------------------------------------------------------- terminal rules */

describe("terminal tool enforcement", () => {
  test("a run with no terminal tool is forced to escalate", async () => {
    const h = harness({ script: [[{ tool: "memory_search", args: { query: "force push" } }]] });
    h.emit({ title: "remote diverged" });
    await h.settle();

    expect(h.channel.escalations).toHaveLength(1);
    expect(h.decisions()[0]!.disposition).toBe("escalate");
    expect(h.decisions()[0]!.decided_by).toBe("llm");
    expect(h.auditVerbs()).toContain("triage.terminal_violation");
    expect(h.incidents()[0]!.state).toBe("escalated");
  });

  test("two terminals in one turn is a violation, not a coin flip", async () => {
    const h = harness({
      script: [
        [
          { tool: "resolve", args: { summary: "fine" } },
          { tool: "escalate", args: { question: "actually, not fine" } },
        ],
      ],
    });
    h.emit({ title: "ambiguous" });
    await h.settle();

    expect(h.channel.escalations).toHaveLength(1);
    expect(h.channel.escalations[0]!.contextLines.join(" ")).toContain("2 terminal tools");
    // Crucially: the run did NOT auto-resolve.
    expect(h.incidents()[0]!.state).toBe("escalated");
  });

  test("an unknown tool name is a protocol violation", async () => {
    const h = harness({ script: [[{ tool: "rm_rf_slash", args: {} }]] });
    h.emit({ title: "nice try" });
    await h.settle();

    expect(h.channel.escalations).toHaveLength(1);
    expect(h.channel.escalations[0]!.contextLines.join(" ")).toContain("unknown tool");
    expect(h.actions.templates).toHaveLength(0);
  });

  test("an empty turn (no tool call at all) escalates", async () => {
    const h = harness({ script: [[]] });
    h.emit({ title: "silent model" });
    await h.settle();
    expect(h.channel.escalations[0]!.contextLines.join(" ")).toContain("no tool call");
  });

  test("an LLM error escalates rather than dropping the incident", async () => {
    const h = harness({ script: [] }); // ScriptedLlm throws "script exhausted"
    h.emit({ title: "provider down" });
    await h.settle();

    expect(h.channel.escalations).toHaveLength(1);
    expect(h.channel.escalations[0]!.contextLines.join(" ")).toContain("llm turn failed");
    expect(h.incidents()[0]!.state).toBe("escalated");
  });

  test("exceeding max_tool_calls escalates", async () => {
    const h = harness({
      config: { triage: { max_tool_calls: 2 } },
      script: [
        [{ tool: "memory_search", args: { query: "a" } }],
        [{ tool: "memory_search", args: { query: "b" } }],
        [{ tool: "memory_search", args: { query: "c" } }],
        [{ tool: "resolve", args: { summary: "too late" } }],
      ],
    });
    h.emit({ title: "chatty" });
    await h.settle();

    expect(h.channel.escalations).toHaveLength(1);
    expect(h.channel.escalations[0]!.contextLines.join(" ")).toContain("max_tool_calls=2");
    expect(h.incidents()[0]!.state).toBe("escalated");
  });

  test("exceeding run_token_cap escalates before the next turn", async () => {
    const h = harness({
      config: { triage: { run_token_cap: 200 } }, // ScriptedLlm burns 150/turn
      script: [
        [{ tool: "memory_search", args: { query: "a" } }],
        [{ tool: "memory_search", args: { query: "b" } }],
        [{ tool: "resolve", args: { summary: "too late" } }],
      ],
    });
    h.emit({ title: "verbose" });
    await h.settle();

    expect(h.channel.escalations).toHaveLength(1);
    expect(h.channel.escalations[0]!.contextLines.join(" ")).toContain("run_token_cap=200");
    expect(h.llm.calls).toBe(2); // stopped before the third turn
  });

  test("a terminal in the same turn as tool calls is honoured after them", async () => {
    const h = harness({
      script: [
        [
          { tool: "memory_search", args: { query: "prior" } },
          { tool: "resolve", args: { summary: "checked memory, all good" } },
        ],
      ],
    });
    h.emit({ title: "combined turn" });
    await h.settle();

    expect(h.incidents()[0]!.state).toBe("resolved");
    expect(h.decisions()[0]!.disposition).toBe("auto_resolve");
    expect(h.decisions()[0]!.rationale).toBe("checked memory, all good");
  });
});

/* ------------------------------------------------------------------- terminals */

describe("terminal dispositions", () => {
  test("resolve closes the incident", async () => {
    const h = harness({ script: resolveScript("replied to the agent") });
    const id = h.emit({ title: "question" });
    await h.settle();

    expect(h.incidents()[0]!.state).toBe("resolved");
    expect(h.incidents()[0]!.summary).toBe("replied to the agent");
    expect(h.incidents()[0]!.closed_at).not.toBeNull();
    expect(h.eventRow(id).triage_state).toBe("llm_resolved");
    expect(h.channel.escalations).toHaveLength(0);
  });

  test("keep_informed records without paging David", async () => {
    const h = harness({ script: [[{ tool: "keep_informed", args: { summary: "agent is retrying" } }]] });
    h.emit({ title: "transient" });
    await h.settle();

    expect(h.decisions()[0]!.disposition).toBe("keep_informed");
    expect(h.channel.escalations).toHaveLength(0);
    expect(h.channel.notifies).toHaveLength(0); // no push, by design
  });

  test("escalate writes an escalation row and sends it", async () => {
    const h = harness({
      script: [
        [
          {
            tool: "escalate",
            args: {
              severity: "attention",
              question: "force-push to fix/telemetry-cliff?",
              suggested_action: { class: "deny", label: "DENY — tell agent to rebase" },
            },
          },
        ],
      ],
    });
    const id = h.emit({ title: "remote diverged" });
    await h.settle();

    const esc = h.escalations()[0]!;
    expect(esc.severity).toBe("attention");
    expect(esc.question).toBe("force-push to fix/telemetry-cliff?");
    expect(esc.state).toBe("pending");
    expect(JSON.parse(esc.suggested_action_json as string).label).toContain("rebase");

    expect(h.channel.escalations).toHaveLength(1);
    expect(h.channel.escalations[0]!.suggestedActionLabel).toContain("rebase");
    expect(h.channel.escalations[0]!.escalationId).toBe(esc.id as string);
    expect(h.eventRow(id).triage_state).toBe("escalated");
  });

  test("defer snoozes the incident", async () => {
    const h = harness({
      script: [[{ tool: "defer", args: { until: "2026-08-27T09:00:00Z", reason: "waiting on CI" } }]],
    });
    h.emit({ title: "blocked on CI" });
    await h.settle();

    expect(h.incidents()[0]!.state).toBe("snoozed");
    expect(h.incidents()[0]!.snooze_until).toBe("2026-08-27T09:00:00Z");
    expect(h.decisions()[0]!.disposition).toBe("defer");
    expect(h.channel.escalations).toHaveLength(0);
  });
});

/* ------------------------------------------------------------ LLM run ceiling */

describe("max LLM runs per incident lineage", () => {
  test("the third recurrence of a dedupe class escalates without an LLM call", async () => {
    const h = harness({
      script: [resolveScript("run 1")[0]!, resolveScript("run 2")[0]!, resolveScript("run 3")[0]!],
    });

    // Same idempotency-key prefix ⇒ same dedupe class ⇒ one lineage.
    for (const n of [1, 2]) {
      h.emit({ idempotency_key: `claude-code:sess-1:PermissionRequest:${n}`, title: `attempt ${n}` });
      expect(await h.settle()).toBe(1);
    }
    expect(h.llm.calls).toBe(2);
    expect(h.incidents()).toHaveLength(1);
    expect(h.incidents()[0]!.llm_runs).toBe(2);

    h.emit({ idempotency_key: "claude-code:sess-1:PermissionRequest:3", title: "attempt 3" });
    expect(await h.settle()).toBe(1);

    expect(h.llm.calls).toBe(2); // the ceiling held
    expect(h.channel.escalations).toHaveLength(1);
    expect(h.channel.escalations[0]!.contextLines.join(" ")).toContain("exhausted its 2 LLM triage runs");
    expect(h.incidents()[0]!.state).toBe("escalated");
  });

  test("the ceiling is configurable", async () => {
    const h = harness({
      config: { triage: { max_llm_runs_per_incident: 1 } },
      script: [resolveScript("run 1")[0]!],
    });
    h.emit({ idempotency_key: "claude-code:sess-1:Err:1" });
    await h.settle();
    expect(h.llm.calls).toBe(1);

    h.emit({ idempotency_key: "claude-code:sess-1:Err:2" });
    await h.settle();
    expect(h.llm.calls).toBe(1);
    expect(h.channel.escalations).toHaveLength(1);
  });

  test("a different dedupe class gets its own budget", async () => {
    const h = harness({ script: [resolveScript()[0]!, resolveScript()[0]!, resolveScript()[0]!] });
    for (const cls of ["PermissionRequest", "Notification", "Stop"]) {
      h.emit({ idempotency_key: `claude-code:sess-1:${cls}:1`, title: cls });
      await h.settle();
    }
    expect(h.llm.calls).toBe(3);
    expect(h.incidents()).toHaveLength(3);
    expect(h.channel.escalations).toHaveLength(0);
  });
});

/* ------------------------------------------------------------------ tool calls */

describe("tool execution", () => {
  test("memory tools are wired to the reader and writer", async () => {
    const memory = new StubMemoryReader(
      [],
      [
        {
          id: "mem_1",
          tier: "note",
          kind: "fact",
          content: { text: "you denied force-push twice on this repo" },
          confidence: 0.9,
          autonomy: "none",
          status: "active",
        },
      ],
    );
    const h = harness({
      memory,
      script: [
        [{ tool: "memory_search", args: { query: "force push" } }],
        [{ tool: "memory_propose", args: { kind: "fact", content: { text: "learned something" } } }],
        [{ tool: "resolve", args: { summary: "done" } }],
      ],
    });
    h.emit({ title: "force push?" });
    await h.settle();

    expect(h.writer.proposals).toHaveLength(1);
    expect(h.writer.proposals[0]!.kind).toBe("fact");
    expect(h.incidents()[0]!.state).toBe("resolved");
  });

  test("session_context and read_session_tail read the real tables", async () => {
    const h = harness({
      script: [
        [{ tool: "session_context", args: {} }, { tool: "read_session_tail", args: { limit: 5 } }],
        [{ tool: "resolve", args: { summary: "context gathered" } }],
      ],
    });
    h.emit({ title: "first" });
    h.emit({ title: "second" });
    await h.settle();

    expect(h.incidents()[0]!.state).toBe("resolved");
    const toolAudits = h
      .rows<{ verb: string; detail_json: string }>("SELECT verb, detail_json FROM audit WHERE verb = 'triage.tool_call'")
      .map((r) => JSON.parse(r.detail_json) as { tool: string; ok: boolean });
    expect(toolAudits.map((a) => a.tool)).toEqual(["session_context", "read_session_tail"]);
    expect(toolAudits.every((a) => a.ok)).toBe(true);
  });

  test("run_probe goes through the action bus and records an action row", async () => {
    const dir = policyDir("[classes.probe]\nenabled = true\nmax_per_hour = 30\n");
    const h = harness({
      config: { state_dir: dir },
      policyFactory: createPolicy,
      script: [
        [{ tool: "run_probe", args: { template_id: "git_status", args: { repo: "github.com/x/omi" } } }],
        [{ tool: "resolve", args: { summary: "probed, all clear" } }],
      ],
    });
    h.emit({ title: "diverged?" });
    await h.settle();

    expect(h.actions.templates).toHaveLength(1);
    expect(h.actions.templates[0]!.templateId).toBe("git_status");
    const row = h.actionRows().find((r) => r.class === "probe")!;
    expect(row.policy_verdict).toBe("auto");
    expect(row.state).toBe("ok");
    rmSync(dir, { recursive: true, force: true });
  });

  test("a disabled class blocks the tool and the block is visible to the model", async () => {
    // No policy.toml at all ⇒ fail-safe ⇒ every class escalates.
    const dir = mkdtempSync(join(tmpdir(), "car-noplicy-"));
    const h = harness({
      config: { state_dir: dir },
      policyFactory: createPolicy,
      script: [
        [{ tool: "run_action", args: { template_id: "restart_service", args: { target: "forgejo" } } }],
        [{ tool: "escalate", args: { question: "may I restart forgejo?" } }],
      ],
    });
    h.emit({ title: "forgejo unresponsive" });
    await h.settle();

    expect(h.actions.templates).toHaveLength(0); // never reached the bus
    const blocked = h.actionRows().find((r) => r.class === "exec.restart_service")!;
    expect(blocked.policy_verdict).toBe("blocked");
    expect(blocked.state).toBe("failed");
    expect(h.channel.escalations).toHaveLength(1);
    rmSync(dir, { recursive: true, force: true });
  });

  test("an allowlist miss blocks a mutating template", async () => {
    const dir = policyDir(
      `[classes.exec.restart_service]\nenabled = true\nallowlist = ["multica", "forgejo"]\n`,
    );
    const h = harness({
      config: { state_dir: dir },
      policyFactory: createPolicy,
      script: [
        [{ tool: "run_action", args: { template_id: "restart_service", args: { target: "postgres" } } }],
        [{ tool: "escalate", args: { question: "restart postgres?" } }],
      ],
    });
    h.emit({ title: "db down" });
    await h.settle();

    expect(h.actions.templates).toHaveLength(0);
    expect(h.channel.escalations).toHaveLength(1);
    rmSync(dir, { recursive: true, force: true });
  });

  test("reply_to_agent delivers through the action bus when the class is enabled", async () => {
    const dir = policyDir("[classes.reply]\nenabled = true\nmax_per_hour = 10\n");
    const h = harness({
      config: { state_dir: dir },
      policyFactory: createPolicy,
      script: [
        [{ tool: "reply_to_agent", args: { text: "use the rebase, not force-push" } }],
        [{ tool: "resolve", args: { summary: "agent unblocked" } }],
      ],
    });
    h.emit({ title: "which approach?" });
    await h.settle();

    expect(h.actions.delivered).toHaveLength(1);
    expect(h.actions.delivered[0]!.payload).toEqual({ text: "use the rebase, not force-push" });
    expect(h.incidents()[0]!.state).toBe("resolved");
    rmSync(dir, { recursive: true, force: true });
  });

  test("the reply rate limit blocks a second reply in the same window", async () => {
    const dir = policyDir("[classes.reply]\nenabled = true\nmax_per_hour = 1\n");
    const h = harness({
      config: { state_dir: dir },
      policyFactory: createPolicy,
      script: [
        [{ tool: "reply_to_agent", args: { text: "first" } }],
        [{ tool: "reply_to_agent", args: { text: "second" } }],
        [{ tool: "escalate", args: { question: "I am rate limited; over to you" } }],
      ],
    });
    h.emit({ title: "chatty incident" });
    await h.settle();

    expect(h.actions.delivered).toHaveLength(1); // only the first got through
    const replies = h.actionRows().filter((r) => r.class === "reply");
    expect(replies).toHaveLength(2);
    expect(replies.filter((r) => r.policy_verdict === "blocked")).toHaveLength(1);
    expect(h.channel.escalations).toHaveLength(1);
    rmSync(dir, { recursive: true, force: true });
  });

  test("dedupe blocks an identical repeated action inside the window", async () => {
    const dir = policyDir("[classes.reply]\nenabled = true\nmax_per_hour = 50\n");
    const h = harness({
      config: { state_dir: dir },
      policyFactory: createPolicy,
      script: [
        [{ tool: "reply_to_agent", args: { text: "same text" } }],
        [{ tool: "reply_to_agent", args: { text: "same text" } }],
        [{ tool: "escalate", args: { question: "stuck in a loop" } }],
      ],
    });
    h.emit({ title: "loopy" });
    await h.settle();

    expect(h.actions.delivered).toHaveLength(1);
    const blocked = h.actionRows().filter((r) => r.policy_verdict === "blocked");
    expect(blocked).toHaveLength(1);
    expect(JSON.parse(blocked[0]!.result_json as string).blocked).toContain("dedupe");
    rmSync(dir, { recursive: true, force: true });
  });
});

/* ------------------------------------------------------------ permission park */

describe("permission answering", () => {
  test("approve_permission answers the parked hook response AND the fallback adapter", async () => {
    const dir = policyDir("[classes.approve_permission]\nenabled = true\n");
    const h = harness({
      config: { state_dir: dir },
      policyFactory: createPolicy,
      script: [
        [{ tool: "approve_permission", args: { reason: "memory says this is routine" } }],
        [{ tool: "resolve", args: { summary: "approved bun install" } }],
      ],
    });
    const eventId = h.emit({
      type: "attention.permission",
      title: "Permission: bun install",
      response_channel: { kind: "claude-hook-http", hint: { tool_use_id: "toolu_1" } },
    });

    const parked = parkPermission(eventId, 30_000);
    expect(hasParkedPermission(eventId)).toBe(true);

    await h.settle();

    await expect(parked).resolves.toEqual({
      decision: "allow",
      reason: "memory says this is routine",
    });
    // Fallback delivery is attempted too — a park is best-effort by design.
    expect(h.actions.delivered).toHaveLength(1);
    expect(h.actions.delivered[0]!.payload).toEqual({ approval: true });
    expect(h.incidents()[0]!.state).toBe("resolved");
    rmSync(dir, { recursive: true, force: true });
  });

  test("deny_permission resolves the park with deny", async () => {
    const dir = policyDir("[classes.approve_permission]\nenabled = true\n");
    const h = harness({
      config: { state_dir: dir },
      policyFactory: createPolicy,
      script: [
        [{ tool: "deny_permission", args: { reason: "never force-push main" } }],
        [{ tool: "resolve", args: { summary: "denied" } }],
      ],
    });
    const eventId = h.emit({ type: "attention.permission", title: "Permission: git push --force" });
    const parked = parkPermission(eventId, 30_000);

    await h.settle();

    await expect(parked).resolves.toEqual({ decision: "deny", reason: "never force-push main" });
    expect(h.actions.delivered[0]!.payload).toEqual({ approval: false });
    rmSync(dir, { recursive: true, force: true });
  });

  test("with no park (restarted daemon) the fallback adapter still carries the answer", async () => {
    const dir = policyDir("[classes.approve_permission]\nenabled = true\n");
    const h = harness({
      config: { state_dir: dir },
      policyFactory: createPolicy,
      script: [
        [{ tool: "approve_permission", args: {} }],
        [{ tool: "resolve", args: { summary: "approved" } }],
      ],
    });
    h.emit({ type: "attention.permission", title: "Permission: bun install" });
    await h.settle();

    expect(h.actions.delivered).toHaveLength(1);
    expect(h.incidents()[0]!.state).toBe("resolved");
    rmSync(dir, { recursive: true, force: true });
  });
});

/* ------------------------------------------------------------------ accounting */

describe("spend accounting", () => {
  test("every turn's tokens and cost land on the decision and in spend", async () => {
    const h = harness({
      script: [
        [{ tool: "memory_search", args: { query: "x" } }],
        [{ tool: "resolve", args: { summary: "done" } }],
      ],
    });
    h.emit({ title: "two turns" });
    await h.settle();

    // ScriptedLlm reports 100 in / 50 out / $0.001 per turn.
    const decision = h.decisions()[0]!;
    expect(decision.tokens_in).toBe(200);
    expect(decision.tokens_out).toBe(100);
    expect(decision.cost_usd).toBeCloseTo(0.002, 6);
    expect(decision.model).toBe("fake/scripted");

    const spend = h.store.spendToday();
    expect(spend.calls).toBe(2);
    expect(spend.cost_usd).toBeCloseTo(0.002, 6);
  });

  test("a forced escalate still attributes the tokens it burned", async () => {
    const h = harness({ script: [[{ tool: "memory_search", args: { query: "x" } }]] });
    h.emit({ title: "no terminal" });
    await h.settle();

    expect(h.decisions()[0]!.disposition).toBe("escalate");
    expect(h.decisions()[0]!.tokens_in).toBe(100);
    expect(h.store.spendToday().cost_usd).toBeCloseTo(0.001, 6);
  });
});

/* ------------------------------------------------------------------- injection */

describe("LlmRunner injection", () => {
  test("setLlmRunner swaps the seam after construction", async () => {
    const h = harness({ script: [] });
    let called = 0;
    h.triage.setLlmRunner({
      async turn() {
        called++;
        return {
          toolCalls: [{ tool: "resolve", args: { summary: "from the injected runner" } }],
          tokensIn: 7,
          tokensOut: 3,
          costUsd: 0.0005,
          model: "injected/model",
        };
      },
    });
    h.emit({ title: "swap me" });
    await h.settle();

    expect(called).toBe(1);
    expect(h.llm.calls).toBe(0);
    expect(h.decisions()[0]!.model).toBe("injected/model");
    expect(h.incidents()[0]!.state).toBe("resolved");
  });
});

/** A throwaway state dir holding one policy.toml. */
function policyDir(toml: string): string {
  const dir = mkdtempSync(join(tmpdir(), "car-llm-policy-"));
  writeFileSync(join(dir, "policy.toml"), toml);
  return dir;
}
