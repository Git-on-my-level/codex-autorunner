/**
 * WS-C memory layer tests. All against `:memory:` SQLite + FakeClock; no network.
 */
import { describe, expect, test, beforeEach, afterEach } from "bun:test";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { FakeClock, memoryStore, testConfig } from "../fakes.ts";
import type { Store } from "../../src/store/db.ts";
import {
  createMemory,
  globMatch,
  scopeMatches,
  canonicalScope,
  ftsQuery,
  estimateTokens,
  confirmStreak,
  ARCHIVE_CONFIDENCE_FLOOR,
  NOTE_CAP_PER_SCOPE,
  PROMOTION_STREAK,
  type MemoryApi,
} from "../../src/memory/index.ts";

const DAY_MS = 86_400_000;

let tmp: string;
let clock: FakeClock;
let store: Store;
let memory: MemoryApi;

function stateDir(): string {
  return tmp;
}

beforeEach(() => {
  tmp = mkdtempSync(join(tmpdir(), "car-memory-"));
  mkdirSync(join(tmp, "memory"), { recursive: true });
  clock = new FakeClock(new Date("2026-08-26T12:00:00Z"));
  store = memoryStore(clock);
  memory = createMemory(store, testConfig({ state_dir: stateDir() }));
});

afterEach(() => {
  store.db.close();
  rmSync(tmp, { recursive: true, force: true });
});

/* ------------------------------------------------------------- fixtures */

let seq = 0;

/** Build a decision with its incident/session/event so recordOutcome can scope it. */
function makeDecision(opts: {
  vendor?: string;
  repo?: string;
  host?: string;
  eventType?: string;
  dedupeClass?: string;
  actionClass?: string | null;
  disposition?: string;
  sessionId?: string;
}): { decisionId: string; carSessionId: string; incidentId: string } {
  seq++;
  const now = clock.now().toISOString();
  const carSessionId = opts.sessionId ?? `sess_test_${seq}`;
  const incidentId = `inc_test_${seq}`;
  const decisionId = `dec_test_${seq}`;
  const eventIdValue = `evt_test_${seq}`;

  const existing = store.db
    .query("SELECT car_session_id FROM sessions WHERE car_session_id = ?")
    .get(carSessionId);
  if (!existing) {
    store.db
      .query(
        `INSERT INTO sessions (car_session_id, vendor, host, repo, first_seen, last_event_at)
         VALUES (?, ?, ?, ?, ?, ?)`,
      )
      .run(
        carSessionId,
        opts.vendor ?? "claude-code",
        opts.host ?? "mac-studio",
        opts.repo ?? "github.com/x/omi",
        now,
        now,
      );
  }
  store.db
    .query(
      `INSERT INTO events (id, idempotency_key, car_session_id, type, severity, ts, received_at,
         source_vendor, source_host, source_adapter)
       VALUES (?, ?, ?, ?, 'attention', ?, ?, ?, ?, 'test')`,
    )
    .run(
      eventIdValue,
      `idem-${seq}`,
      carSessionId,
      opts.eventType ?? "attention.permission",
      now,
      now,
      opts.vendor ?? "claude-code",
      opts.host ?? "mac-studio",
    );
  store.db
    .query(
      `INSERT INTO incidents (id, car_session_id, opened_by_event, summary, dedupe_class, opened_at)
       VALUES (?, ?, ?, ?, ?, ?)`,
    )
    .run(
      incidentId,
      carSessionId,
      eventIdValue,
      "force-push to fix/telemetry",
      opts.dedupeClass ?? "force-push",
      now,
    );
  store.db
    .query(
      `INSERT INTO decisions (id, incident_id, decided_by, disposition, action_class, created_at)
       VALUES (?, ?, 'rules', ?, ?, ?)`,
    )
    .run(
      decisionId,
      incidentId,
      opts.disposition ?? "auto_resolve",
      opts.actionClass === undefined ? "reply" : opts.actionClass,
      now,
    );
  return { decisionId, carSessionId, incidentId };
}

function row(id: string): Record<string, unknown> {
  return store.db.query("SELECT * FROM memories WHERE id = ?").get(id) as Record<string, unknown>;
}

function auditVerbs(objectId: string): string[] {
  return (
    store.db
      .query("SELECT verb FROM audit WHERE object_id = ? ORDER BY id ASC").all(objectId) as {
      verb: string;
    }[]
  ).map((r) => r.verb);
}

/* ---------------------------------------------------------- scope matching */

describe("scope matching", () => {
  test("an unscoped selector matches everything", () => {
    expect(scopeMatches({}, { vendor: "codex", repo: "github.com/x/y" })).toBe(true);
  });

  test("pinned keys must match exactly", () => {
    expect(scopeMatches({ vendor: "codex" }, { vendor: "codex" })).toBe(true);
    expect(scopeMatches({ vendor: "codex" }, { vendor: "claude-code" })).toBe(false);
    expect(
      scopeMatches({ event_type: "attention.permission" }, { event_type: "attention.error" }),
    ).toBe(false);
    expect(scopeMatches({ dedupe_class: "force-push" }, { dedupe_class: "force-push" })).toBe(true);
  });

  test("a pinned key with no input value never matches", () => {
    expect(scopeMatches({ repo: "github.com/x/y" }, { vendor: "codex" })).toBe(false);
  });

  test("repo supports '*' globs", () => {
    expect(globMatch("github.com/x/*", "github.com/x/omi")).toBe(true);
    expect(globMatch("*/prod-*", "github.com/prod-api")).toBe(true);
    expect(globMatch("github.com/x/*", "github.com/y/omi")).toBe(false);
    expect(scopeMatches({ repo: "github.com/x/*" }, { repo: "github.com/x/omi" })).toBe(true);
    expect(scopeMatches({ repo: "github.com/x/*" }, { repo: "github.com/z/omi" })).toBe(false);
    expect(scopeMatches({ repo: "*" }, { repo: "anything" })).toBe(true);
    expect(scopeMatches({ repo: "*" }, {})).toBe(true);
  });

  test("glob metacharacters in other fields are literal-safe", () => {
    expect(globMatch("a.b", "axb")).toBe(false);
    expect(canonicalScope({ repo: "r", vendor: "v" })).toBe(canonicalScope({ vendor: "v", repo: "r" }));
    expect(canonicalScope({ vendor: "v", host: undefined })).toBe(canonicalScope({ vendor: "v" }));
  });

  test("grantedRules returns only granted, active, scope-matching rules", () => {
    const granted = memory.writer.addFromDavid(
      "rule",
      "preference",
      { summary: "auto-approve dep bumps", disposition: "auto_resolve" },
      { repo: "github.com/x/*", event_type: "attention.permission" },
    );
    memory.writer.setAutonomy(granted, "granted", "david");
    const suggestOnly = memory.writer.addFromDavid(
      "rule",
      "preference",
      { summary: "suggest only" },
      { repo: "github.com/x/*" },
    );
    memory.writer.setAutonomy(suggestOnly, "suggest", "david");
    memory.writer.addFromDavid("rule", "preference", { summary: "other repo" }, { repo: "github.com/z/*" });

    const hits = memory.reader.grantedRules({
      repo: "github.com/x/omi",
      eventType: "attention.permission",
      vendor: "claude-code",
    });
    expect(hits.map((h) => h.id)).toEqual([granted]);

    expect(
      memory.reader.grantedRules({ repo: "github.com/z/thing", eventType: "attention.permission" }),
    ).toEqual([]);
  });
});

/* -------------------------------------------------------------- read path */

describe("reader.assembleContext", () => {
  test("includes charter text verbatim and never truncates it", () => {
    const charter = "# Charter\nAlways ask before touching prod.\n";
    writeFileSync(join(tmp, "memory", "charter.md"), charter, "utf8");
    const ctx = memory.reader.assembleContext({ tokenBudget: 1 });
    expect(ctx.charter).toBe(charter);
    expect(ctx.hits).toEqual([]);
  });

  test("missing charter file degrades to empty string", () => {
    expect(memory.reader.assembleContext({ tokenBudget: 2500 }).charter).toBe("");
  });

  test("FTS retrieval returns at most 5 scope-matching notes, BM25-ranked", () => {
    for (let i = 0; i < 8; i++) {
      memory.writer.addFromDavid(
        "note",
        "fact",
        { text: `force push protection detail number ${i}` },
        { repo: "github.com/x/omi" },
      );
    }
    memory.writer.addFromDavid("note", "fact", { text: "unrelated kitchen recipe" }, {});
    memory.writer.addFromDavid("note", "fact", { text: "force push elsewhere" }, { repo: "github.com/z/other" });

    const ctx = memory.reader.assembleContext({
      repo: "github.com/x/omi",
      freeText: "force push protection",
      tokenBudget: 2500,
    });
    const notes = ctx.hits.filter((h) => h.tier === "note");
    expect(notes.length).toBe(5);
    for (const note of notes) {
      expect(String(note.content.text)).toContain("force push protection");
    }
  });

  test("pending notes surface as unverified; pending rules never do", () => {
    const pendingNote = memory.writer.propose("fact", { text: "force push needs a rebase first" }, {});
    const pendingRule = memory.writer.propose("preference", { summary: "force push auto-deny" }, {});
    const ctx = memory.reader.assembleContext({ freeText: "force push", tokenBudget: 100_000 });
    const note = ctx.hits.find((h) => h.id === pendingNote);
    expect(note?.status).toBe("pending");
    expect(ctx.hits.some((h) => h.id === pendingRule)).toBe(false);
  });

  test("search() and get() round-trip", () => {
    const id = memory.writer.addFromDavid(
      "note",
      "fact",
      { text: "codex cli has no queue subcommand" },
      { vendor: "codex" },
    );
    const found = memory.reader.search("queue subcommand");
    expect(found.map((h) => h.id)).toContain(id);
    expect(memory.reader.search("queue", { vendor: "claude-code" })).toEqual([]);
    expect(memory.reader.get(id)?.content.text).toBe("codex cli has no queue subcommand");
    expect(memory.reader.get("mem_nope")).toBeNull();
  });

  test("returns last 3 episodes sharing the dedupe class and last 5 session outcomes", () => {
    const { decisionId, carSessionId } = makeDecision({ dedupeClass: "force-push" });
    for (let i = 0; i < 7; i++) {
      memory.writer.recordOutcome({ decisionId, verdict: "confirmed" });
      clock.advance(1000);
    }
    const ctx = memory.reader.assembleContext({
      dedupeClass: "force-push",
      carSessionId,
      tokenBudget: 100_000,
    });
    expect(ctx.hits.filter((h) => h.tier === "episode").length).toBe(3);
    expect(ctx.hits.filter((h) => h.tier === "outcome").length).toBe(5);
  });

  test("token budget truncates episodes, then notes, then rules — charter survives", () => {
    writeFileSync(join(tmp, "memory", "charter.md"), "CHARTER", "utf8");
    const scope = { repo: "github.com/x/omi", dedupe_class: "force-push" };
    memory.writer.addFromDavid("rule", "preference", { summary: "R".repeat(200) }, scope);
    memory.writer.addFromDavid("note", "fact", { text: `N${"o".repeat(200)} force push` }, scope);
    memory.writer.addFromDavid("episode", "summary", { summary: "E".repeat(200) }, scope);

    const input = {
      repo: "github.com/x/omi",
      dedupeClass: "force-push",
      freeText: "force push",
      tokenBudget: 100_000,
    };
    const full = memory.reader.assembleContext(input);
    expect(full.hits.map((h) => h.tier)).toEqual(["rule", "note", "episode"]);

    // Each hit is ~55 tokens; shrink the budget one tier at a time.
    const tiersFor = (budget: number): string[] =>
      memory.reader.assembleContext({ ...input, tokenBudget: budget }).hits.map((h) => h.tier);
    expect(tiersFor(130)).toEqual(["rule", "note"]);
    expect(tiersFor(70)).toEqual(["rule"]);
    expect(tiersFor(1)).toEqual([]);
    expect(memory.reader.assembleContext({ ...input, tokenBudget: 1 }).charter).toBe("CHARTER");
    expect(estimateTokens("abcd")).toBe(1);
  });

  test("bumps last_used_at / use_count on returned hits only", () => {
    const used = memory.writer.addFromDavid("note", "fact", { text: "force push facts" }, {});
    const unused = memory.writer.addFromDavid("note", "fact", { text: "unrelated" }, {});
    memory.reader.assembleContext({ freeText: "force push facts", tokenBudget: 2500 });
    expect(row(used).use_count).toBe(1);
    expect(row(used).last_used_at).toBe(clock.now().toISOString());
    expect(row(unused).use_count).toBe(0);
    expect(row(unused).last_used_at).toBeNull();
  });

  test("ftsQuery sanitizes free text into a safe OR query", () => {
    expect(ftsQuery('force-push "main"')).toBe('"force" OR "push" OR "main"');
    expect(ftsQuery("!!! ??")).toBeNull();
  });
});

/* ------------------------------------------------------------- write path */

describe("writer", () => {
  test("addFromDavid is active, confidence 1.0 and decay-exempt", () => {
    const id = memory.writer.addFromDavid("rule", "preference", { summary: "never force push" }, {});
    const r = row(id);
    expect(r.status).toBe("active");
    expect(r.confidence).toBe(1);
    expect(r.authored_by).toBe("david");
    expect(r.autonomy).toBe("none");
    expect(JSON.parse(String(r.provenance_json)).decay_exempt).toBe(true);
    expect(auditVerbs(id)).toContain("memory.created");
  });

  test("propose lands pending and NEVER grants autonomy", () => {
    const ruleId = memory.writer.propose("preference", { summary: "maybe auto-approve" }, { vendor: "codex" });
    const noteId = memory.writer.propose("fact", { text: "a fact" }, {});
    expect(row(ruleId).status).toBe("pending");
    expect(row(ruleId).autonomy).toBe("none");
    expect(row(ruleId).tier).toBe("rule");
    expect(row(ruleId).authored_by).toBe("triage");
    expect(row(noteId).tier).toBe("note");
    // A proposal is invisible to the granted-rules fast path.
    expect(memory.reader.grantedRules({ vendor: "codex" })).toEqual([]);
  });

  test("setAutonomy refuses non-David actors and audits the transition", () => {
    const id = memory.writer.addFromDavid("rule", "preference", { summary: "x" }, {});
    expect(() => memory.writer.setAutonomy(id, "granted", "triage" as "david")).toThrow(/only David/);
    expect(row(id).autonomy).toBe("none");
    memory.writer.setAutonomy(id, "granted", "david");
    expect(row(id).autonomy).toBe("granted");
    expect(auditVerbs(id)).toContain("memory.autonomy_set");
  });
});

/* --------------------------------------------------------- outcome recorder */

describe("recordOutcome", () => {
  test("confirmed raises confidence toward the ceiling and reinforces", () => {
    const id = memory.writer.addFromDavid("rule", "preference", { summary: "deny force push" }, {
      repo: "github.com/x/*",
    });
    store.db.query("UPDATE memories SET confidence = 0.5 WHERE id = ?").run(id);
    const { decisionId } = makeDecision({ repo: "github.com/x/omi" });

    memory.writer.recordOutcome({ decisionId, verdict: "confirmed" });
    expect(row(id).confidence).toBeCloseTo(0.55, 10); // 0.5 + 0.1*(1-0.5)
    expect(row(id).evidence_confirm).toBe(1);
    expect(row(id).last_reinforced_at).toBe(clock.now().toISOString());

    memory.writer.recordOutcome({ decisionId, verdict: "confirmed" });
    expect(row(id).confidence).toBeCloseTo(0.595, 10);
    expect(
      (store.db.query("SELECT COUNT(*) c FROM outcomes").get() as { c: number }).c,
    ).toBe(2);
  });

  test("confidence is capped at 0.99", () => {
    const id = memory.writer.addFromDavid("rule", "preference", { summary: "x" }, {});
    store.db.query("UPDATE memories SET confidence = 0.995 WHERE id = ?").run(id);
    const { decisionId } = makeDecision({});
    memory.writer.recordOutcome({ decisionId, verdict: "confirmed" });
    expect(row(id).confidence).toBe(0.99);
  });

  test("override multiplies confidence by 0.45 and counts evidence", () => {
    const id = memory.writer.addFromDavid("rule", "preference", { summary: "x" }, {});
    store.db.query("UPDATE memories SET confidence = 0.8 WHERE id = ?").run(id);
    const { decisionId } = makeDecision({});
    memory.writer.recordOutcome({ decisionId, verdict: "overridden" });
    expect(row(id).confidence).toBeCloseTo(0.36, 10);
    expect(row(id).evidence_override).toBe(1);
  });

  for (const verdict of ["overridden", "corrected", "flagged"] as const) {
    test(`a single '${verdict}' outcome demotes granted -> suggest`, () => {
      const id = memory.writer.addFromDavid("rule", "preference", { summary: "auto approve" }, {
        repo: "github.com/x/*",
      });
      memory.writer.setAutonomy(id, "granted", "david");
      const { decisionId } = makeDecision({ repo: "github.com/x/omi" });
      memory.writer.recordOutcome({ decisionId, verdict });
      expect(row(id).autonomy).toBe("suggest");
      expect(auditVerbs(id)).toContain("memory.autonomy_demoted");
      expect(memory.reader.grantedRules({ repo: "github.com/x/omi" })).toEqual([]);
    });
  }

  test("only scope-matching and action-class-matching rules are touched", () => {
    const matching = memory.writer.addFromDavid("rule", "preference", { summary: "m", action_class: "reply" }, {
      repo: "github.com/x/*",
    });
    const wrongRepo = memory.writer.addFromDavid("rule", "preference", { summary: "w" }, {
      repo: "github.com/z/*",
    });
    const wrongAction = memory.writer.addFromDavid(
      "rule",
      "preference",
      { summary: "a", action_class: "exec.restart_service" },
      { repo: "github.com/x/*" },
    );
    const { decisionId } = makeDecision({ repo: "github.com/x/omi", actionClass: "reply" });
    memory.writer.recordOutcome({ decisionId, verdict: "confirmed" });
    expect(row(matching).evidence_confirm).toBe(1);
    expect(row(wrongRepo).evidence_confirm).toBe(0);
    expect(row(wrongAction).evidence_confirm).toBe(0);
  });

  test("an outcome for an unknown decision is still recorded, harmlessly", () => {
    memory.writer.recordOutcome({ decisionId: "dec_missing", verdict: "confirmed" });
    expect((store.db.query("SELECT COUNT(*) c FROM outcomes").get() as { c: number }).c).toBe(1);
    expect(
      (store.db.query("SELECT COUNT(*) c FROM memories").get() as { c: number }).c,
    ).toBe(0);
  });

  test("writes an episode summarizing decision + outcome", () => {
    const { decisionId } = makeDecision({ dedupeClass: "force-push", disposition: "escalate" });
    memory.writer.recordOutcome({ decisionId, verdict: "confirmed", davidAction: { tap: "deny" } });
    const episode = store.db
      .query("SELECT * FROM memories WHERE tier = 'episode'")
      .get() as Record<string, unknown>;
    expect(episode.authored_by).toBe("outcome");
    expect(episode.status).toBe("active");
    const content = JSON.parse(String(episode.content_json));
    expect(content.verdict).toBe("confirmed");
    expect(content.disposition).toBe("escalate");
    expect(JSON.parse(String(episode.scope_json)).dedupe_class).toBe("force-push");
  });
});

/* ------------------------------------------------------ promotion detection */

describe("promotion offers", () => {
  function suggestRule(): string {
    const id = memory.writer.addFromDavid("rule", "preference", { summary: "auto-approve dep bumps" }, {
      repo: "github.com/x/*",
    });
    memory.writer.setAutonomy(id, "suggest", "david");
    return id;
  }

  test("offers after 4 consecutive confirmed outcomes, not before", () => {
    const id = suggestRule();
    const { decisionId } = makeDecision({ repo: "github.com/x/omi" });
    for (let i = 0; i < PROMOTION_STREAK - 1; i++) {
      memory.writer.recordOutcome({ decisionId, verdict: "confirmed" });
      clock.advance(1000);
      expect(memory.pendingPromotionOffers()).toEqual([]);
    }
    memory.writer.recordOutcome({ decisionId, verdict: "confirmed" });
    const offers = memory.pendingPromotionOffers();
    expect(offers.length).toBe(1);
    expect(offers[0]!.memoryId).toBe(id);
    expect(offers[0]!.summary).toBe("auto-approve dep bumps");
    expect(offers[0]!.scope).toEqual({ repo: "github.com/x/*" });
    expect(confirmStreak(store, id)).toBe(4);
  });

  test("an override resets the streak", () => {
    const id = suggestRule();
    const { decisionId } = makeDecision({ repo: "github.com/x/omi" });
    for (let i = 0; i < 3; i++) memory.writer.recordOutcome({ decisionId, verdict: "confirmed" });
    memory.writer.recordOutcome({ decisionId, verdict: "overridden" });
    expect(confirmStreak(store, id)).toBe(0);
    for (let i = 0; i < 3; i++) memory.writer.recordOutcome({ decisionId, verdict: "confirmed" });
    expect(memory.pendingPromotionOffers()).toEqual([]);
    memory.writer.recordOutcome({ decisionId, verdict: "confirmed" });
    expect(memory.pendingPromotionOffers().map((o) => o.memoryId)).toEqual([id]);
  });

  test("markPromotionOffered suppresses a repeat offer, an override re-arms it", () => {
    const id = suggestRule();
    const { decisionId } = makeDecision({ repo: "github.com/x/omi" });
    for (let i = 0; i < PROMOTION_STREAK; i++) {
      memory.writer.recordOutcome({ decisionId, verdict: "confirmed" });
    }
    memory.markPromotionOffered(id);
    expect(memory.pendingPromotionOffers()).toEqual([]);
    expect(auditVerbs(id)).toContain("memory.promotion_offered");

    memory.writer.recordOutcome({ decisionId, verdict: "overridden" });
    for (let i = 0; i < PROMOTION_STREAK; i++) {
      memory.writer.recordOutcome({ decisionId, verdict: "confirmed" });
    }
    expect(memory.pendingPromotionOffers().map((o) => o.memoryId)).toEqual([id]);
  });

  test("granted and none rules are never offered", () => {
    const granted = memory.writer.addFromDavid("rule", "preference", { summary: "g" }, {});
    memory.writer.setAutonomy(granted, "granted", "david");
    memory.writer.addFromDavid("rule", "preference", { summary: "n" }, {});
    const { decisionId } = makeDecision({});
    for (let i = 0; i < PROMOTION_STREAK + 2; i++) {
      memory.writer.recordOutcome({ decisionId, verdict: "confirmed" });
    }
    expect(memory.pendingPromotionOffers()).toEqual([]);
  });
});

/* ---------------------------------------------------------- consolidation */

describe("consolidationJob", () => {
  test("decays unreinforced confidence on the 45-day half-life", async () => {
    const id = memory.writer.propose("preference", { summary: "decays" }, {});
    store.db.query("UPDATE memories SET status = 'active', confidence = 0.8 WHERE id = ?").run(id);
    clock.advance(45 * DAY_MS);
    await memory.consolidationJob();
    expect(row(id).confidence).toBeCloseTo(0.4, 6);

    clock.advance(45 * DAY_MS);
    await memory.consolidationJob();
    expect(row(id).confidence).toBeCloseTo(0.2, 6); // compounding is not double-counted
  });

  test("archives below the 0.2 floor", async () => {
    const id = memory.writer.propose("preference", { summary: "fades away" }, {});
    store.db.query("UPDATE memories SET status = 'active', confidence = 0.5 WHERE id = ?").run(id);
    clock.advance(120 * DAY_MS);
    await memory.consolidationJob();
    expect(Number(row(id).confidence)).toBeLessThan(ARCHIVE_CONFIDENCE_FLOOR);
    expect(row(id).status).toBe("archived");
    expect(auditVerbs(id)).toContain("memory.archived");
  });

  test("David-authored memories are exempt from decay", async () => {
    const id = memory.writer.addFromDavid("rule", "preference", { summary: "never force push" }, {});
    clock.advance(400 * DAY_MS);
    await memory.consolidationJob();
    expect(row(id).confidence).toBe(1);
    expect(row(id).status).toBe("active");
  });

  test("reinforcement resets the decay clock", async () => {
    const id = memory.writer.propose("preference", { summary: "reinforced" }, {});
    store.db.query("UPDATE memories SET status = 'active', confidence = 0.8 WHERE id = ?").run(id);
    clock.advance(45 * DAY_MS);
    const { decisionId } = makeDecision({});
    memory.writer.recordOutcome({ decisionId, verdict: "confirmed" });
    const reinforced = Number(row(id).confidence);
    await memory.consolidationJob(); // same instant as the reinforcement
    expect(Number(row(id).confidence)).toBeCloseTo(reinforced, 10);
  });

  test("merges duplicate notes in the same scope, keeping the newest", async () => {
    const scope = { repo: "github.com/x/omi" };
    const older = memory.writer.addFromDavid("note", "fact", { text: "codex has no queue subcommand" }, scope);
    clock.advance(1000);
    const newer = memory.writer.addFromDavid("note", "fact", { text: "Codex has no queue subcommand!" }, scope);
    clock.advance(1000);
    const otherScope = memory.writer.addFromDavid(
      "note",
      "fact",
      { text: "codex has no queue subcommand" },
      { repo: "github.com/z/other" },
    );
    const distinct = memory.writer.addFromDavid("note", "fact", { text: "hermes uses tailscale" }, scope);

    await memory.consolidationJob();

    expect(row(newer).status).toBe("active");
    expect(row(older).status).toBe("archived");
    expect(row(newer).supersedes).toBe(older);
    expect(JSON.parse(String(row(older).provenance_json)).superseded_by).toBe(newer);
    expect(row(otherScope).status).toBe("active"); // different scope, not a duplicate
    expect(row(distinct).status).toBe("active");
    expect(auditVerbs(newer)).toContain("memory.merged");
  });

  test("distills a consistent episode cluster into a pending rule proposal", async () => {
    for (let i = 0; i < 3; i++) {
      const { decisionId } = makeDecision({ dedupeClass: "dep-bump", disposition: "auto_resolve" });
      memory.writer.recordOutcome({ decisionId, verdict: "confirmed" });
      clock.advance(1000);
    }
    await memory.consolidationJob();

    const rules = store.db
      .query("SELECT * FROM memories WHERE tier = 'rule'")
      .all() as Record<string, unknown>[];
    expect(rules.length).toBe(1);
    const rule = rules[0]!;
    expect(rule.status).toBe("pending");
    expect(rule.autonomy).toBe("none");
    expect(rule.authored_by).toBe("consolidator");
    const content = JSON.parse(String(rule.content_json));
    expect(content.disposition).toBe("auto_resolve");
    expect(content.distilled_from.length).toBe(3);
    expect(JSON.parse(String(rule.scope_json)).dedupe_class).toBe("dep-bump");

    // Idempotent: a rule already covers this dedupe class.
    await memory.consolidationJob();
    expect(
      (store.db.query("SELECT COUNT(*) c FROM memories WHERE tier = 'rule'").get() as { c: number }).c,
    ).toBe(1);
  });

  test("does not distill from too few, repeated, or inconsistent episodes", async () => {
    // two distinct decisions: below the threshold
    for (let i = 0; i < 2; i++) {
      const d = makeDecision({ dedupeClass: "sparse", disposition: "auto_resolve" });
      memory.writer.recordOutcome({ decisionId: d.decisionId, verdict: "confirmed" });
    }
    // one decision confirmed three times is still ONE piece of evidence
    const repeated = makeDecision({ dedupeClass: "repeated", disposition: "auto_resolve" });
    for (let i = 0; i < 3; i++) {
      memory.writer.recordOutcome({ decisionId: repeated.decisionId, verdict: "confirmed" });
    }
    // three decisions, but the dispositions disagree
    for (const disposition of ["auto_resolve", "escalate", "defer"]) {
      const d = makeDecision({ dedupeClass: "mixed", disposition });
      memory.writer.recordOutcome({ decisionId: d.decisionId, verdict: "confirmed" });
    }

    await memory.consolidationJob();
    expect(
      (store.db.query("SELECT COUNT(*) c FROM memories WHERE tier = 'rule'").get() as { c: number }).c,
    ).toBe(0);
  });

  test("caps notes per scope at 40, archiving the lowest-confidence tail", async () => {
    const scope = { repo: "github.com/x/capped" };
    const ids: string[] = [];
    for (let i = 0; i < NOTE_CAP_PER_SCOPE + 3; i++) {
      const id = memory.writer.addFromDavid("note", "fact", { text: `distinct note ${i}` }, scope);
      store.db.query("UPDATE memories SET confidence = ? WHERE id = ?").run(0.1 + i / 100, id);
      ids.push(id);
    }
    await memory.consolidationJob();
    const active = store.db
      .query("SELECT COUNT(*) c FROM memories WHERE tier = 'note' AND status = 'active'")
      .get() as { c: number };
    expect(active.c).toBe(NOTE_CAP_PER_SCOPE);
    // the three lowest-confidence notes went
    expect(row(ids[0]!).status).toBe("archived");
    expect(row(ids[1]!).status).toBe("archived");
    expect(row(ids[2]!).status).toBe("archived");
    expect(row(ids[3]!).status).toBe("active");
  });

  test("never touches autonomy or the charter file", async () => {
    const charterFile = join(tmp, "memory", "charter.md");
    writeFileSync(charterFile, "# Charter\nsacred\n", "utf8");
    const granted = memory.writer.addFromDavid("rule", "preference", { summary: "granted rule" }, {});
    memory.writer.setAutonomy(granted, "granted", "david");
    const proposal = memory.writer.propose("preference", { summary: "pending" }, {});
    clock.advance(365 * DAY_MS);
    await memory.consolidationJob();
    expect(row(granted).autonomy).toBe("granted");
    expect(row(proposal).autonomy).toBe("none");
    expect(readFileSync(charterFile, "utf8")).toBe("# Charter\nsacred\n");
    const verbs = (
      store.db.query("SELECT verb FROM audit WHERE actor = 'consolidator'").all() as { verb: string }[]
    ).map((r) => r.verb);
    expect(verbs).not.toContain("memory.autonomy_set");
    expect(verbs).toContain("memory.consolidated");
  });
});

/* ---------------------------------------------------------------- export */

describe("exportMarkdown", () => {
  test("renders rules, notes and episodes to readable markdown", () => {
    memory.writer.addFromDavid("rule", "preference", { summary: "never force push main" }, {
      repo: "github.com/x/*",
    });
    memory.writer.addFromDavid("note", "fact", { text: "codex 0.145 has no queue" }, { vendor: "codex" });
    const { decisionId } = makeDecision({ dedupeClass: "force-push" });
    memory.writer.recordOutcome({ decisionId, verdict: "confirmed" });

    const dir = join(tmp, "export");
    const files = memory.exportMarkdown(dir);
    expect(files.map((f) => f.split("/").pop())).toEqual(["rules.md", "notes.md", "episodes.md"]);

    const rules = readFileSync(join(dir, "rules.md"), "utf8");
    expect(rules).toContain("# Rules");
    expect(rules).toContain("never force push main");
    expect(rules).toContain("scope: repo=github.com/x/*");
    expect(rules).toContain("autonomy: none");

    expect(readFileSync(join(dir, "notes.md"), "utf8")).toContain("codex 0.145 has no queue");
    expect(readFileSync(join(dir, "episodes.md"), "utf8")).toContain("confirmed");
    expect(
      (store.db.query("SELECT COUNT(*) c FROM audit WHERE verb = 'memory.exported'").get() as {
        c: number;
      }).c,
    ).toBe(1);
  });

  test("empty tiers render an explicit '(none)'", () => {
    const dir = join(tmp, "export-empty");
    memory.exportMarkdown(dir);
    expect(readFileSync(join(dir, "rules.md"), "utf8")).toContain("_(none)_");
  });
});
