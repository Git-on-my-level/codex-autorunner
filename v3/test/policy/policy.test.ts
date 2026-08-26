/**
 * Policy engine: loader/hot-reload, class verdicts, guards, and the four safety
 * gates (dedupe, rate limits, circuit breaker, budget). Deliberately over-tested
 * — this is the layer that decides whether CAR is allowed to act at all.
 */
import { describe, expect, test, beforeEach, afterEach } from "bun:test";
import { mkdtempSync, rmSync, writeFileSync, utimesSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { FakeClock, memoryStore, testConfig } from "../fakes.ts";
import type { Store } from "../../src/store/db.ts";
import {
  BREAKER_FAILURES,
  ESCALATE_ONLY_KEY,
  clearBreaker,
  createPolicy,
  flattenClasses,
  globMatch,
  inQuietHours,
  policySummary,
  tripBreaker,
  type PolicyEngine,
} from "../../src/policy/index.ts";

let dir: string;
let clock: FakeClock;
let store: Store;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "car-policy-"));
  clock = new FakeClock();
  store = memoryStore(clock);
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

function engine(): PolicyEngine {
  return createPolicy(store, testConfig({ state_dir: dir }));
}

/** Write policy.toml with an mtime the hot-reloader is guaranteed to notice. */
let mtimeTick = 0;
function writePolicy(toml: string): void {
  const path = join(dir, "policy.toml");
  writeFileSync(path, toml);
  mtimeTick += 10;
  const t = new Date(Date.now() + mtimeTick * 1000);
  utimesSync(path, t, t);
}

/** Minimal action row: what every gate counts. */
function insertAction(opts: {
  cls: string;
  state?: string;
  dedupeHash?: string;
  startedAt?: string;
  decisionId?: string;
}): void {
  const id = `act_${Math.random().toString(36).slice(2)}`;
  const at = opts.startedAt ?? clock.now().toISOString();
  store.db
    .query(
      `INSERT INTO actions (id, decision_id, class, args_json, policy_verdict, dedupe_hash, state, started_at, finished_at)
       VALUES (?, ?, ?, '{}', 'auto', ?, ?, ?, ?)`,
    )
    .run(id, opts.decisionId ?? "dec_x", opts.cls, opts.dedupeHash ?? `h_${id}`, opts.state ?? "ok", at, at);
}

/** actions → decisions → incidents chain, needed for per-session rate limits. */
function insertSessionAction(cls: string, carSessionId: string): void {
  const suffix = Math.random().toString(36).slice(2);
  const inc = `inc_${suffix}`;
  const dec = `dec_${suffix}`;
  const now = clock.now().toISOString();
  store.db
    .query(
      "INSERT INTO incidents (id, car_session_id, opened_by_event, state, dedupe_class, opened_at) VALUES (?, ?, 'evt_x', 'open', 'c', ?)",
    )
    .run(inc, carSessionId, now);
  store.db
    .query(
      "INSERT INTO decisions (id, incident_id, decided_by, disposition, created_at) VALUES (?, ?, 'llm', 'auto_resolve', ?)",
    )
    .run(dec, inc, now);
  insertAction({ cls, decisionId: dec });
}

/* --------------------------------------------------------------- fail-safe */

describe("fail-safe defaults", () => {
  test("missing policy.toml means every class escalates", () => {
    const p = engine();
    expect(p.check("reply", {})).toBe("escalate");
    expect(p.check("probe", {})).toBe("escalate");
    expect(p.check("exec.restart_service", { target: "multica" })).toBe("escalate");
    expect(p.check("anything.at.all", {})).toBe("escalate");
    // Fail-safe is not the same as panic: nothing is *blocked*, it just escalates.
    expect(p.escalateOnly()).toBe(false);
    expect(p.gate("reply", "hash-1")).toBeNull();
  });

  test("missing policy.toml is announced in the summary", () => {
    expect(engine().summary()).toContain("none enabled");
  });

  test("an unparseable policy.toml runs fully closed rather than half-open", () => {
    writePolicy("[classes.reply\nenabled = true");
    const p = engine();
    expect(p.check("reply", {})).toBe("escalate");
    expect(p.snapshot().error).not.toBeNull();
    expect(p.summary()).toContain("INVALID");
  });

  test("a policy that fails validation does not widen permissions", () => {
    writePolicy(`[classes.reply]\nenabled = "yes"\n`);
    const p = engine();
    expect(p.check("reply", {})).toBe("escalate");
    expect(p.snapshot().error).not.toBeNull();
  });
});

/* -------------------------------------------------------------- class verdicts */

describe("class verdicts", () => {
  test("an enabled class is auto; a disabled one escalates", () => {
    writePolicy(`
[classes.reply]
enabled = true
max_per_hour = 10

[classes.approve_permission]
enabled = false
`);
    const p = engine();
    expect(p.check("reply", {})).toBe("auto");
    expect(p.check("approve_permission", {})).toBe("escalate");
  });

  test("nested exec classes flatten to dotted names", () => {
    writePolicy(`
[classes.exec.restart_service]
enabled = true
allowlist = ["multica", "forgejo"]
max_per_day = 4

[classes.exec.agentctl_continue]
enabled = true
repos_deny = ["*/prod-*"]
`);
    const p = engine();
    expect(Object.keys(p.snapshot().classes).sort()).toEqual([
      "exec.agentctl_continue",
      "exec.restart_service",
    ]);
    expect(p.check("exec.restart_service", { target: "multica" })).toBe("auto");
  });

  test("allowlist misses are forbidden, not merely escalated", () => {
    writePolicy(`
[classes.exec.restart_service]
enabled = true
allowlist = ["multica", "forgejo"]
`);
    const p = engine();
    expect(p.check("exec.restart_service", { target: "forgejo" })).toBe("auto");
    expect(p.check("exec.restart_service", { target: "postgres" })).toBe("forbid");
    // No target at all cannot satisfy an allowlist.
    expect(p.check("exec.restart_service", {})).toBe("forbid");
  });

  test("repos_deny globs forbid matching repos", () => {
    writePolicy(`
[classes.exec.agentctl_continue]
enabled = true
repos_deny = ["*/prod-*"]
`);
    const p = engine();
    expect(p.check("exec.agentctl_continue", { repo: "github.com/x/prod-api" })).toBe("forbid");
    expect(p.check("exec.agentctl_continue", { repo: "github.com/x/dev-api" })).toBe("auto");
  });

  test("never_touch_branches is a global guard across all classes", () => {
    writePolicy(`
[classes.exec.push]
enabled = true

[guards]
never_touch_branches = ["main", "master", "release/*"]
`);
    const p = engine();
    expect(p.check("exec.push", { branch: "main" })).toBe("forbid");
    expect(p.check("exec.push", { branch: "release/2026-08" })).toBe("forbid");
    expect(p.check("exec.push", { branch: "fix/telemetry" })).toBe("auto");
  });

  test("escalate-only mode overrides an enabled class", () => {
    writePolicy(`[classes.reply]\nenabled = true\n`);
    const p = engine();
    expect(p.check("reply", {})).toBe("auto");
    tripBreaker(store, "manual /panic");
    expect(p.check("reply", {})).toBe("escalate");
  });
});

/* ------------------------------------------------------------------ hot reload */

describe("hot reload", () => {
  test("a changed policy.toml takes effect without restarting", () => {
    writePolicy(`[classes.reply]\nenabled = true\n`);
    const p = engine();
    expect(p.check("reply", {})).toBe("auto");

    writePolicy(`[classes.reply]\nenabled = false\n`);
    expect(p.check("reply", {})).toBe("escalate");

    writePolicy(`[classes.reply]\nenabled = true\nmax_per_hour = 2\n`);
    expect(p.check("reply", {})).toBe("auto");
    expect(p.snapshot().classes.reply?.max_per_hour).toBe(2);
  });

  test("deleting policy.toml closes everything again", () => {
    writePolicy(`[classes.reply]\nenabled = true\n`);
    const p = engine();
    expect(p.check("reply", {})).toBe("auto");
    rmSync(join(dir, "policy.toml"));
    expect(p.check("reply", {})).toBe("escalate");
  });
});

/* ----------------------------------------------------------------- rate limits */

describe("rate limits", () => {
  test("max_per_hour blocks once the window is full", () => {
    writePolicy(`[classes.reply]\nenabled = true\nmax_per_hour = 2\n`);
    const p = engine();
    insertAction({ cls: "reply" });
    expect(p.gate("reply", "h1")).toBeNull();
    insertAction({ cls: "reply" });
    const reason = p.gate("reply", "h2");
    expect(reason).toContain("rate_limit");
    expect(reason).toContain("max_per_hour=2");
  });

  test("actions older than the window do not count", () => {
    writePolicy(`[classes.reply]\nenabled = true\nmax_per_hour = 1\n`);
    const p = engine();
    const old = new Date(clock.now().getTime() - 2 * 3600_000).toISOString();
    insertAction({ cls: "reply", startedAt: old });
    expect(p.gate("reply", "h1")).toBeNull();
  });

  test("max_per_day counts a rolling 24h", () => {
    writePolicy(`
[classes.exec.restart_service]
enabled = true
max_per_day = 2
`);
    const p = engine();
    insertAction({ cls: "exec.restart_service" });
    insertAction({ cls: "exec.restart_service" });
    expect(p.gate("exec.restart_service", "h9")).toContain("max_per_day=2");
  });

  test("max_per_session_per_hour is scoped to one session", () => {
    writePolicy(`
[classes.reply]
enabled = true
max_per_hour = 100
max_per_session_per_hour = 2
`);
    const p = engine();
    insertSessionAction("reply", "sess_A");
    insertSessionAction("reply", "sess_A");
    insertSessionAction("reply", "sess_B");
    expect(p.gate("reply", "h1", "sess_A")).toContain("max_per_session_per_hour=2");
    expect(p.gate("reply", "h2", "sess_B")).toBeNull();
  });

  test("a class with no limits configured is never rate-blocked", () => {
    writePolicy(`[classes.probe]\nenabled = true\n`);
    const p = engine();
    for (let i = 0; i < 50; i++) insertAction({ cls: "probe" });
    expect(p.gate("probe", "h-fresh")).toBeNull();
  });
});

/* ---------------------------------------------------------------------- dedupe */

describe("dedupe", () => {
  test("an identical action inside 30 minutes is blocked", () => {
    writePolicy(`[classes.reply]\nenabled = true\n`);
    const p = engine();
    expect(p.gate("reply", "same-hash")).toBeNull();
    insertAction({ cls: "reply", dedupeHash: "same-hash" });
    expect(p.gate("reply", "same-hash")).toContain("dedupe");
  });

  test("the same action outside the window is allowed again", () => {
    writePolicy(`[classes.reply]\nenabled = true\n`);
    const p = engine();
    const old = new Date(clock.now().getTime() - 31 * 60_000).toISOString();
    insertAction({ cls: "reply", dedupeHash: "same-hash", startedAt: old });
    expect(p.gate("reply", "same-hash")).toBeNull();
  });

  test("a failed attempt still counts — retry storms are the thing being stopped", () => {
    writePolicy(`[classes.reply]\nenabled = true\n`);
    const p = engine();
    insertAction({ cls: "reply", dedupeHash: "same-hash", state: "failed" });
    expect(p.gate("reply", "same-hash")).toContain("dedupe");
  });
});

/* ------------------------------------------------------------- circuit breaker */

describe("circuit breaker", () => {
  test(`${BREAKER_FAILURES} failed actions in 10 minutes flips escalate-only`, () => {
    writePolicy(`[classes.reply]\nenabled = true\n`);
    const p = engine();
    for (let i = 0; i < BREAKER_FAILURES - 1; i++) insertAction({ cls: "reply", state: "failed" });
    expect(p.escalateOnly()).toBe(false);

    insertAction({ cls: "reply", state: "failed" });
    expect(p.escalateOnly()).toBe(true);
    expect(store.kvGet<boolean>(ESCALATE_ONLY_KEY)).toBe(true);
    expect(p.gate("reply", "any")).toContain("circuit_breaker");
    expect(p.check("reply", {})).toBe("escalate");
  });

  test("failures spread outside the 10-minute window do not flip it", () => {
    writePolicy(`[classes.reply]\nenabled = true\n`);
    const p = engine();
    for (let i = 0; i < BREAKER_FAILURES + 2; i++) {
      insertAction({
        cls: "reply",
        state: "failed",
        startedAt: new Date(clock.now().getTime() - (11 + i) * 60_000).toISOString(),
      });
    }
    expect(p.escalateOnly()).toBe(false);
  });

  test("the flag is sticky: it survives the failures ageing out", () => {
    writePolicy(`[classes.reply]\nenabled = true\n`);
    const p = engine();
    for (let i = 0; i < BREAKER_FAILURES; i++) insertAction({ cls: "reply", state: "failed" });
    expect(p.escalateOnly()).toBe(true);
    clock.advance(60 * 60_000);
    expect(p.escalateOnly()).toBe(true);
  });

  test("clearBreaker lifts it and does not instantly re-trip on the same rows", () => {
    writePolicy(`[classes.reply]\nenabled = true\n`);
    const p = engine();
    for (let i = 0; i < BREAKER_FAILURES; i++) insertAction({ cls: "reply", state: "failed" });
    expect(p.escalateOnly()).toBe(true);

    clearBreaker(store); // the /panic-clear button
    expect(p.escalateOnly()).toBe(false);
    expect(p.check("reply", {})).toBe("auto");

    // ...but a fresh burst re-trips it.
    clock.advance(1000);
    for (let i = 0; i < BREAKER_FAILURES; i++) insertAction({ cls: "reply", state: "failed" });
    expect(p.escalateOnly()).toBe(true);
  });

  test("the engine method and the free function are the same flow", () => {
    const p = engine();
    tripBreaker(store, "manual");
    expect(p.escalateOnly()).toBe(true);
    p.clearBreaker();
    expect(p.escalateOnly()).toBe(false);
  });
});

/* ---------------------------------------------------------------------- budget */

describe("budget", () => {
  test("100% of the daily triage budget flips escalate-only", () => {
    writePolicy(`[budget]\ntriage_daily_usd = 2.00\n`);
    const p = engine();
    store.recordSpend("anthropic", "claude-haiku-4-5", 1000, 500, 1.5); // 75%
    expect(p.escalateOnly()).toBe(false);
    expect(p.budgetStatus().warn).toBe(false);

    store.recordSpend("anthropic", "claude-haiku-4-5", 1000, 500, 0.1); // 80% → warn
    expect(p.budgetStatus().warn).toBe(true);
    expect(p.escalateOnly()).toBe(false);

    store.recordSpend("anthropic", "claude-haiku-4-5", 1000, 500, 0.5); // 100% → stop
    expect(p.budgetStatus().exhausted).toBe(true);
    expect(p.escalateOnly()).toBe(true);
    expect(p.gate("reply", "h")).toContain("budget");
  });

  test("the 80% warning fires before the stop", () => {
    writePolicy(`[budget]\ntriage_daily_usd = 1.00\n`);
    const p = engine();
    store.recordSpend("anthropic", "m", 1, 1, 0.5);
    expect(p.budgetStatus().warn).toBe(false);
    store.recordSpend("anthropic", "m", 1, 1, 0.31);
    expect(p.budgetStatus().warn).toBe(true);
    expect(p.budgetStatus().exhausted).toBe(false);
  });

  test("no configured budget means no budget stop", () => {
    const p = engine();
    store.recordSpend("anthropic", "m", 1, 1, 999);
    expect(p.escalateOnly()).toBe(false);
  });

  test("spend resets with the day", () => {
    writePolicy(`[budget]\ntriage_daily_usd = 1.00\n`);
    const p = engine();
    store.recordSpend("anthropic", "m", 1, 1, 2);
    expect(p.escalateOnly()).toBe(true);
    clock.advance(24 * 3600_000);
    // A new day's spend row is empty; only the sticky kv flag would remain.
    clearBreaker(store);
    expect(p.budgetStatus().spent).toBe(0);
    expect(p.escalateOnly()).toBe(false);
  });
});

/* ----------------------------------------------------------------- gate order */

describe("gate precedence", () => {
  test("the breaker outranks rate limits and dedupe", () => {
    writePolicy(`[classes.reply]\nenabled = true\nmax_per_hour = 0\n`);
    const p = engine();
    tripBreaker(store, "manual");
    expect(p.gate("reply", "h")).toContain("circuit_breaker");
  });

  test("budget outranks rate limits", () => {
    writePolicy(`
[classes.reply]
enabled = true
max_per_hour = 0

[budget]
triage_daily_usd = 0.01
`);
    const p = engine();
    store.recordSpend("anthropic", "m", 1, 1, 5);
    expect(p.gate("reply", "h")).toContain("budget");
  });
});

/* ----------------------------------------------------------------- pure helpers */

describe("helpers", () => {
  test("globMatch handles the documented patterns", () => {
    expect(globMatch("*/prod-*", "github.com/x/prod-api")).toBe(true);
    expect(globMatch("*/prod-*", "github.com/x/staging")).toBe(false);
    expect(globMatch("main", "main")).toBe(true);
    expect(globMatch("main", "maintenance")).toBe(false);
    // Regex metacharacters in patterns are literal, not operators.
    expect(globMatch("a.b", "axb")).toBe(false);
  });

  test("quiet hours spanning midnight", () => {
    const at = (h: number, m = 0) => new Date(2026, 7, 26, h, m);
    expect(inQuietHours("23:00-08:00", at(23, 30))).toBe(true);
    expect(inQuietHours("23:00-08:00", at(3))).toBe(true);
    expect(inQuietHours("23:00-08:00", at(9))).toBe(false);
    expect(inQuietHours("09:00-17:00", at(12))).toBe(true);
    expect(inQuietHours("09:00-17:00", at(20))).toBe(false);
    expect(inQuietHours("", at(3))).toBe(false);
    expect(inQuietHours("nonsense", at(3))).toBe(false);
  });

  test("flattenClasses keeps leaf tables and recurses namespaces", () => {
    const flat = flattenClasses({
      reply: { enabled: true, max_per_hour: 10 },
      exec: { restart_service: { enabled: true }, agentctl_continue: { enabled: false } },
    });
    expect(Object.keys(flat).sort()).toEqual(["exec.agentctl_continue", "exec.restart_service", "reply"]);
    expect(flat.reply?.max_per_hour).toBe(10);
    expect(flat["exec.agentctl_continue"]?.enabled).toBe(false);
  });
});

/* --------------------------------------------------------------- prompt summary */

describe("policySummary", () => {
  test("renders enabled classes, guards and budget for the triage prompt", () => {
    writePolicy(`
[classes.reply]
enabled = true
max_per_hour = 10
max_per_session_per_hour = 3

[classes.approve_permission]
enabled = false

[guards]
never_touch_branches = ["main"]
quiet_hours = "23:00-08:00"

[budget]
triage_daily_usd = 2.00
`);
    const p = engine();
    const s = policySummary(p);
    expect(s).toContain("reply=on");
    expect(s).toContain("10/h");
    expect(s).toContain("3/session/h");
    expect(s).toContain("approve_permission=off");
    expect(s).toContain("main");
    expect(s).toContain("$2.00");
  });

  test("escalate-only mode is stated loudly", () => {
    const p = engine();
    tripBreaker(store, "manual");
    expect(policySummary(p)).toContain("ESCALATE-ONLY");
  });

  test("falls back gracefully on a bare PolicyPort", () => {
    const bare = { check: () => "escalate" as const, gate: () => null, escalateOnly: () => true };
    expect(policySummary(bare)).toContain("ESCALATE-ONLY");
  });
});
