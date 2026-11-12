/**
 * The template executor: typed args, no shell, policy enforced by the executor
 * (never by a prompt), and every state transition on the record.
 */
import { afterEach, describe, expect, test } from "bun:test";
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { createActionBus, dedupeHash, DEFAULT_TEMPLATES_TOML } from "../../src/actions/index.ts";
import { parseTemplates, policyClassFor, validateArgs } from "../../src/actions/templates.ts";
import { FakeClock, memoryStore } from "../fakes.ts";
import { auditRows, makeRunner, StubPolicy, tempState } from "./helpers.ts";

const cleanups: (() => void)[] = [];
afterEach(() => {
  while (cleanups.length) cleanups.pop()!();
});

const MUTATING_TOML = `
[templates."svc.restart"]
argv = ["systemctl", "--user", "restart", "{service}"]
mutating = true
timeout_ms = 20000
[templates."svc.restart".args]
service = { type = "enum", required = true, values = ["multica", "forgejo"] }

[templates."git.status"]
argv = ["git", "-C", "{repo}", "status", "--short"]
mutating = false
[templates."git.status".args]
repo = { type = "path", required = true, max_len = 1024 }
`;

function harness(templatesToml?: string, respond?: Parameters<typeof makeRunner>[0]) {
  const state = tempState();
  cleanups.push(state.cleanup);
  if (templatesToml) writeFileSync(join(state.dir, "templates.toml"), templatesToml);
  const store = memoryStore(new FakeClock());
  const policy = new StubPolicy();
  const runner = makeRunner(respond);
  const bus = createActionBus(store, state.config, policy, { runner: runner.runner, env: {} });
  return { state, store, policy, runner, bus };
}

function actionRows(store: ReturnType<typeof memoryStore>) {
  return store.db
    .query("SELECT id, class, policy_verdict, dedupe_hash, state, args_json, result_json FROM actions ORDER BY id")
    .all() as {
    id: string;
    class: string;
    policy_verdict: string;
    dedupe_hash: string;
    state: string;
    args_json: string;
    result_json: string | null;
  }[];
}

describe("defaults", () => {
  test("the shipped defaults parse and are all read-only", () => {
    const templates = parseTemplates(DEFAULT_TEMPLATES_TOML);
    expect(Object.keys(templates).sort()).toEqual([
      "agentctl.recent",
      "agentctl.status",
      "git.log",
      "git.status",
    ]);
    for (const spec of Object.values(templates)) expect(spec.mutating).toBe(false);
  });

  test("are used when templates.toml is absent", () => {
    const h = harness();
    expect(h.bus.listTemplates()).toContain("git.status");
  });

  test("templates.toml replaces the defaults", () => {
    const h = harness(MUTATING_TOML);
    expect(h.bus.listTemplates().sort()).toEqual(["git.status", "svc.restart"]);
  });
});

describe("argument validation", () => {
  test("runs a read-only probe with substituted argv", async () => {
    const h = harness(undefined, () => ({ code: 0, stdout: "## main\n M src/x.ts" }));
    const res = await h.bus.runTemplate(
      "git.status",
      { repo: "/Users/dazheng/omi" },
      { decisionId: "dec_1", mutating: false },
    );
    expect(res.ok).toBe(true);
    expect(res.output).toContain("## main");
    expect(h.runner.calls[0]?.argv).toEqual([
      "git",
      "-C",
      "/Users/dazheng/omi",
      "status",
      "--short",
      "--branch",
    ]);
  });

  test("rejects shell metacharacters", async () => {
    const h = harness();
    for (const evil of [
      "/repo; rm -rf ~",
      "/repo && curl evil.test",
      "/repo`whoami`",
      "/repo$(id)",
      "/repo|tee x",
      "/repo\nrm x",
    ]) {
      const res = await h.bus.runTemplate("git.status", { repo: evil }, { decisionId: "dec_x", mutating: false });
      expect(res.ok).toBe(false);
      expect(res.output).toContain("shell_metacharacter");
    }
    expect(h.runner.calls).toHaveLength(0);
  });

  test("rejects unknown arguments", async () => {
    const h = harness();
    const res = await h.bus.runTemplate(
      "git.status",
      { repo: "/repo", extra: "--upload-pack=evil" },
      { decisionId: "dec_2", mutating: false },
    );
    expect(res.ok).toBe(false);
    expect(res.output).toContain("unknown_arg");
    expect(h.runner.calls).toHaveLength(0);
  });

  test("rejects missing required arguments", async () => {
    const h = harness();
    const res = await h.bus.runTemplate("git.status", {}, { decisionId: "dec_3", mutating: false });
    expect(res.ok).toBe(false);
    expect(res.output).toContain("missing_arg");
  });

  test("rejects unknown templates", async () => {
    const h = harness();
    const res = await h.bus.runTemplate("rm.rf", { path: "/" }, { decisionId: "dec_4", mutating: true });
    expect(res.ok).toBe(false);
    expect(res.output).toContain("unknown template");
  });

  test("rejects flag-shaped and traversing path values", async () => {
    const h = harness();
    const flag = await h.bus.runTemplate(
      "git.status",
      { repo: "--exec=evil" },
      { decisionId: "dec_5", mutating: false },
    );
    expect(flag.output).toContain("flag_injection");
    const traversal = await h.bus.runTemplate(
      "git.status",
      { repo: "/repo/../../etc" },
      { decisionId: "dec_6", mutating: false },
    );
    expect(traversal.output).toContain("path_traversal");
  });

  test("enum args must be one of the declared values", async () => {
    const h = harness(MUTATING_TOML);
    const bad = await h.bus.runTemplate(
      "svc.restart",
      { service: "prod-db" },
      { decisionId: "dec_7", mutating: true },
    );
    expect(bad.ok).toBe(false);
    expect(bad.output).toContain("not_allowed");
    const good = await h.bus.runTemplate(
      "svc.restart",
      { service: "multica" },
      { decisionId: "dec_8", mutating: true },
    );
    expect(good.ok).toBe(true);
    expect(h.runner.calls[0]?.argv).toEqual(["systemctl", "--user", "restart", "multica"]);
  });

  test("int args are type-checked", () => {
    const spec = parseTemplates(DEFAULT_TEMPLATES_TOML)["git.log"]!;
    expect(() => validateArgs("git.log", spec, { repo: "/r", count: "ten" })).toThrow(/integer/);
    expect(validateArgs("git.log", spec, { repo: "/r", count: 10 })).toEqual({ repo: "/r", count: "10" });
  });

  test("an omitted optional arg falls back to its declared default", async () => {
    const h = harness();
    await h.bus.runTemplate("agentctl.recent", {}, { decisionId: "dec_9", mutating: false });
    expect(h.runner.calls[0]?.argv).toEqual(["agentctl", "recent", "--limit", "20"]);
    await h.bus.runTemplate("agentctl.recent", { limit: 5 }, { decisionId: "dec_9b", mutating: false });
    expect(h.runner.calls[1]?.argv).toEqual(["agentctl", "recent", "--limit", "5"]);
  });

  test("an optional arg with no default and no value is refused, not silently dropped", async () => {
    const h = harness(`
[templates."probe.x"]
argv = ["tool", "--flag", "{maybe}"]
[templates."probe.x".args]
maybe = { type = "string", required = false }
`);
    const res = await h.bus.runTemplate("probe.x", {}, { decisionId: "dec_9c", mutating: false });
    expect(res.ok).toBe(false);
    expect(res.output).toContain("unbound_placeholder");
    expect(h.runner.calls).toHaveLength(0);
  });
});

describe("policy enforcement", () => {
  test("an 'escalate' verdict refuses execution", async () => {
    const h = harness();
    h.policy.verdict = "escalate";
    const res = await h.bus.runTemplate(
      "git.status",
      { repo: "/repo" },
      { decisionId: "dec_10", mutating: false },
    );
    expect(res.ok).toBe(false);
    expect(res.output).toContain("refused (escalate)");
    expect(h.runner.calls).toHaveLength(0);
    const rows = actionRows(h.store);
    expect(rows[0]?.policy_verdict).toBe("escalate");
    expect(rows[0]?.state).toBe("failed");
  });

  test("a 'forbid' verdict refuses execution", async () => {
    const h = harness();
    h.policy.verdict = "forbid";
    const res = await h.bus.runTemplate("git.status", { repo: "/r" }, { decisionId: "dec_11", mutating: false });
    expect(res.ok).toBe(false);
    expect(actionRows(h.store)[0]?.policy_verdict).toBe("forbid");
  });

  test("a gate block is refused with the gate's reason", async () => {
    const h = harness();
    h.policy.gateReason = "blocked_dedupe";
    const res = await h.bus.runTemplate("git.status", { repo: "/r" }, { decisionId: "dec_12", mutating: false });
    expect(res.ok).toBe(false);
    expect(actionRows(h.store)[0]?.policy_verdict).toBe("blocked_dedupe");
    expect(h.runner.calls).toHaveLength(0);
  });

  test("escalate-only mode suspends mutating templates but not probes", async () => {
    const h = harness(MUTATING_TOML);
    h.policy.escalateOnlyFlag = true;
    const mutating = await h.bus.runTemplate(
      "svc.restart",
      { service: "multica" },
      { decisionId: "dec_13", mutating: true },
    );
    expect(mutating.ok).toBe(false);
    expect(mutating.output).toContain("escalate-only");
    const probe = await h.bus.runTemplate(
      "git.status",
      { repo: "/r" },
      { decisionId: "dec_14", mutating: false },
    );
    expect(probe.ok).toBe(true);
  });

  test("a mutating template is unreachable from the read-only path", async () => {
    const h = harness(MUTATING_TOML);
    const res = await h.bus.runTemplate(
      "svc.restart",
      { service: "multica" },
      { decisionId: "dec_15", mutating: false },
    );
    expect(res.ok).toBe(false);
    expect(res.output).toContain("not reachable from the read-only path");
    expect(h.runner.calls).toHaveLength(0);
  });

  test("policy classes are derived from the template", () => {
    const templates = parseTemplates(MUTATING_TOML);
    expect(policyClassFor("git.status", templates["git.status"]!)).toBe("probe");
    expect(policyClassFor("svc.restart", templates["svc.restart"]!)).toBe("exec.svc.restart");
  });

  test("the derived class is what policy is consulted with", async () => {
    const h = harness(MUTATING_TOML);
    await h.bus.runTemplate("svc.restart", { service: "forgejo" }, { decisionId: "dec_16", mutating: true });
    expect(h.policy.checks[0]?.actionClass).toBe("exec.svc.restart");
    expect(h.policy.gates[0]?.actionClass).toBe("exec.svc.restart");
  });
});

describe("action rows and audit", () => {
  test("dedupe_hash is recorded and stable over (id, args)", async () => {
    const h = harness();
    await h.bus.runTemplate("git.status", { repo: "/r" }, { decisionId: "dec_17", mutating: false });
    const row = actionRows(h.store)[0]!;
    expect(row.dedupe_hash).toBe(dedupeHash("git.status", { repo: "/r" }));
    expect(row.dedupe_hash).toHaveLength(64);
    // Argument order must not change the hash.
    expect(dedupeHash("t", { a: 1, b: 2 })).toBe(dedupeHash("t", { b: 2, a: 1 }));
    expect(dedupeHash("t", { a: 1 })).not.toBe(dedupeHash("t", { a: 2 }));
    // Refused actions carry the hash too, so the breaker can see them.
    expect(h.policy.gates[0]?.hash).toBe(row.dedupe_hash);
  });

  test("a successful run transitions pending → running → ok and audits both ends", async () => {
    const h = harness(undefined, () => ({ code: 0, stdout: "clean" }));
    await h.bus.runTemplate("git.status", { repo: "/r" }, { decisionId: "dec_18", mutating: false });
    const row = actionRows(h.store)[0]!;
    expect(row.state).toBe("ok");
    expect(JSON.parse(row.result_json ?? "{}")).toEqual({ code: 0, stdout: "clean", stderr: "" });
    const started = auditRows(h.store, "action.started");
    expect(started[0]?.object_id).toBe(row.id);
    expect(started[0]?.detail.argv).toEqual(["git", "-C", "/r", "status", "--short", "--branch"]);
    expect(auditRows(h.store, "action.ok")[0]?.object_id).toBe(row.id);
  });

  test("a nonzero exit lands as state 'failed' with the stderr in the output", async () => {
    const h = harness(undefined, () => ({ code: 128, stderr: "not a git repository" }));
    const res = await h.bus.runTemplate("git.status", { repo: "/r" }, { decisionId: "dec_19", mutating: false });
    expect(res.ok).toBe(false);
    expect(res.output).toContain("not a git repository");
    expect(actionRows(h.store)[0]?.state).toBe("failed");
    expect(auditRows(h.store, "action.failed")).toHaveLength(1);
  });

  test("a refusal is recorded as an action row and an audit line", async () => {
    const h = harness();
    await h.bus.runTemplate("git.status", { repo: "/r; id" }, { decisionId: "dec_20", mutating: false });
    const refusals = auditRows(h.store, "action.refused");
    expect(refusals).toHaveLength(1);
    expect(refusals[0]?.detail.template_id).toBe("git.status");
    expect(actionRows(h.store)[0]?.state).toBe("failed");
  });

  test("the template timeout is handed to the runner", async () => {
    const h = harness(MUTATING_TOML);
    await h.bus.runTemplate("svc.restart", { service: "multica" }, { decisionId: "dec_21", mutating: true });
    expect(h.runner.calls[0]?.opts.timeoutMs).toBe(20000);
  });

  test("an invalid templates.toml refuses instead of throwing", async () => {
    const h = harness("this is not toml [[[");
    const res = await h.bus.runTemplate("git.status", { repo: "/r" }, { decisionId: "dec_22", mutating: false });
    expect(res.ok).toBe(false);
    expect(res.output).toContain("templates.toml is invalid");
  });
});
