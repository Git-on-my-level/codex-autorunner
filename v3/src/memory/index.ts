/**
 * WS-C owns src/memory/: charter + rule/note/episode tiers, outcome recorder,
 * promotion loop, nightly consolidation, `card memory` support.
 *
 * Design invariants (DESIGN.md §6):
 *  - Autonomy is granted, never inferred: only `setAutonomy(..., by: "david")`
 *    can reach `granted`. `propose()` always lands `autonomy='none'`, and the
 *    consolidator never touches autonomy or charter.md.
 *  - One override demotes granted -> suggest (audit verb `memory.autonomy_demoted`).
 *  - The read path is deterministic: scope selectors + FTS5/BM25, no embeddings.
 *  - Everything that changes state audits.
 */
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";
import type { Store } from "../store/db.ts";
import type { CarConfig } from "../config/config.ts";
import { charterPath } from "../config/config.ts";
import type { MemoryHit, MemoryReader, MemoryWriter } from "../ports.ts";
import { memoryId, outcomeId } from "../contract/ids.ts";

/* ------------------------------------------------------------------ tuning */

/** Confidence half-life, in days, for memories that are not reinforced. */
export const DECAY_HALF_LIFE_DAYS = 45;
/** Below this confidence a decayed memory is archived (restorable). */
export const ARCHIVE_CONFIDENCE_FLOOR = 0.2;
/** Per-scope active-note cap; the lowest-confidence tail is archived. */
export const NOTE_CAP_PER_SCOPE = 40;
/** Consecutive confirmed outcomes required before a promotion is offered. */
export const PROMOTION_STREAK = 4;
/** confirmed: confidence += CONFIRM_STEP * (1 - confidence), capped. */
export const CONFIRM_STEP = 0.1;
export const CONFIRM_CEILING = 0.99;
/** overridden/corrected/flagged: confidence *= OVERRIDE_FACTOR (~3x harder than a confirm). */
export const OVERRIDE_FACTOR = 0.45;
/** Episodes sharing a dedupe class needed before distilling a pending rule. */
export const DISTILL_MIN_EPISODES = 3;
/** Token approximation used by the budget: 4 characters per token. */
export const CHARS_PER_TOKEN = 4;

const PROMOTION_OFFERED_KV = "memory:promotion_offered";
const OUTCOME_LINK_VERB = "memory.outcome_linked";

/* ------------------------------------------------------------------- types */

export interface MemoryScope {
  vendor?: string;
  repo?: string;
  host?: string;
  event_type?: string;
  dedupe_class?: string;
}

const SCOPE_KEYS = ["vendor", "repo", "host", "event_type", "dedupe_class"] as const;
type ScopeKey = (typeof SCOPE_KEYS)[number];

export interface PromotionOffer {
  memoryId: string;
  summary: string;
  scope: MemoryScope;
}

export interface MemoryApi {
  reader: MemoryReader;
  writer: MemoryWriter;
  consolidationJob: () => Promise<void>;
  /** Rules whose evidence has earned a digest promotion offer. */
  pendingPromotionOffers: () => PromotionOffer[];
  /** Record that the digest has offered promotion for this rule. */
  markPromotionOffered: (id: string) => void;
  /** Render the whole store to readable markdown (rules.md, notes.md, episodes.md). */
  exportMarkdown: (dir: string) => string[];
}

/** Denormalized scope of a decision: decisions -> incidents -> sessions/events. */
interface DecisionContext {
  action_class: string | null;
  incident_id: string;
  disposition: string;
  car_session_id: string | null;
  dedupe_class: string | null;
  summary: string | null;
  vendor: string | null;
  host: string | null;
  repo: string | null;
  event_type: string | null;
}

interface MemoryRow {
  id: string;
  tier: string;
  scope_json: string;
  kind: string;
  content_json: string;
  confidence: number;
  evidence_confirm: number;
  evidence_override: number;
  autonomy: string;
  status: string;
  authored_by: string;
  created_at: string;
  updated_at: string;
  last_reinforced_at: string | null;
  last_used_at: string | null;
  use_count: number;
  supersedes: string | null;
  provenance_json: string | null;
}

/* ----------------------------------------------------------------- helpers */

function parseJson<T>(text: string | null | undefined, fallback: T): T {
  if (!text) return fallback;
  try {
    return JSON.parse(text) as T;
  } catch {
    return fallback;
  }
}

/** `*` is a wildcard (crosses `/`); everything else is literal. */
export function globMatch(pattern: string, value: string): boolean {
  const rx = new RegExp(
    `^${pattern.replace(/[.+?^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*")}$`,
  );
  return rx.test(value);
}

export type ScopeInput = Partial<Record<ScopeKey, string | undefined>>;

/**
 * A scope selector matches an input when every key it *pins* matches.
 * Absent keys (and `*`) are wildcards; `repo` supports `*` globs, and any other
 * field containing `*` is globbed too. A pinned key with no input value never
 * matches — we cannot verify it.
 */
export function scopeMatches(scope: MemoryScope, input: ScopeInput): boolean {
  for (const key of SCOPE_KEYS) {
    const want = scope[key];
    if (want === undefined || want === null || want === "" || want === "*") continue;
    const got = input[key];
    if (got === undefined || got === null || got === "") return false;
    if (key === "repo" || want.includes("*")) {
      if (!globMatch(want, got)) return false;
    } else if (want !== got) {
      return false;
    }
  }
  return true;
}

/** Stable key for "same scope" grouping (dedup + per-scope caps). */
export function canonicalScope(scope: MemoryScope): string {
  const out: Record<string, string> = {};
  for (const key of SCOPE_KEYS) {
    const value = scope[key];
    if (value !== undefined && value !== null && value !== "") out[key] = value;
  }
  return JSON.stringify(out, Object.keys(out).sort());
}

/** All string/number leaves of a content blob — what the FTS index actually sees. */
function contentText(content: unknown): string {
  const parts: string[] = [];
  const walk = (value: unknown, depth: number): void => {
    if (depth > 4 || value === null || value === undefined) return;
    if (typeof value === "string") parts.push(value);
    else if (typeof value === "number" || typeof value === "boolean") parts.push(String(value));
    else if (Array.isArray(value)) for (const item of value) walk(item, depth + 1);
    else if (typeof value === "object") for (const item of Object.values(value)) walk(item, depth + 1);
  };
  walk(content, 0);
  return parts.join(" ");
}

function scopeText(scope: MemoryScope): string {
  return SCOPE_KEYS.filter((k) => scope[k])
    .map((k) => `${k}:${scope[k]} ${scope[k]}`)
    .join(" ");
}

/** Build a safe FTS5 OR-query from free text; null when there is nothing to match. */
export function ftsQuery(text: string): string | null {
  const tokens = Array.from(
    new Set(
      text
        .toLowerCase()
        .split(/[^a-z0-9]+/i)
        .filter((t) => t.length >= 2),
    ),
  ).slice(0, 12);
  if (tokens.length === 0) return null;
  return tokens.map((t) => `"${t}"`).join(" OR ");
}

function normalizeText(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

function tokenSet(text: string): Set<string> {
  return new Set(normalizeText(text).split(" ").filter(Boolean));
}

/** Jaccard similarity over token sets; 1 = identical bag of words. */
function similarity(a: string, b: string): number {
  const sa = tokenSet(a);
  const sb = tokenSet(b);
  if (sa.size === 0 && sb.size === 0) return 1;
  let shared = 0;
  for (const t of sa) if (sb.has(t)) shared++;
  const union = sa.size + sb.size - shared;
  return union === 0 ? 1 : shared / union;
}

function rowToHit(row: MemoryRow): MemoryHit {
  return {
    id: row.id,
    tier: row.tier,
    kind: row.kind,
    content: parseJson<Record<string, unknown>>(row.content_json, {}),
    confidence: row.confidence,
    autonomy: row.autonomy,
    status: row.status,
  };
}

function summarize(content: Record<string, unknown>): string {
  for (const key of ["summary", "text", "match", "title", "content"]) {
    const value = content[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  const text = contentText(content).trim();
  return text || JSON.stringify(content);
}

export function estimateTokens(text: string): number {
  return Math.ceil(text.length / CHARS_PER_TOKEN);
}

function hitTokens(hit: MemoryHit): number {
  return estimateTokens(`${hit.tier}:${hit.kind} ${JSON.stringify(hit.content)}`);
}

function maxIso(...values: (string | null | undefined)[]): string | null {
  let best: string | null = null;
  for (const value of values) {
    if (!value) continue;
    if (best === null || value > best) best = value;
  }
  return best;
}

/* ------------------------------------------------------- standalone queries */
/* Exported as free functions so other workstreams (digest, CLI) can call them
   without a port change — DaemonDeps only carries MemoryReader/MemoryWriter. */

/**
 * Rules with autonomy='suggest' that have >= PROMOTION_STREAK consecutive
 * confirmed outcomes since their last override, and have not been offered yet.
 * The streak is derived from the per-rule outcome links written by
 * recordOutcome (audit rows, insertion-ordered = outcome created_at order).
 */
export function pendingPromotionOffers(store: Store): PromotionOffer[] {
  const offered = store.kvGet<Record<string, string>>(PROMOTION_OFFERED_KV) ?? {};
  const rules = store.db
    .query(
      "SELECT * FROM memories WHERE tier = 'rule' AND status = 'active' AND autonomy = 'suggest' ORDER BY confidence DESC",
    )
    .all() as MemoryRow[];
  const offers: PromotionOffer[] = [];
  for (const rule of rules) {
    if (offered[rule.id]) continue;
    if (confirmStreak(store, rule.id) >= PROMOTION_STREAK) {
      offers.push({
        memoryId: rule.id,
        summary: summarize(parseJson<Record<string, unknown>>(rule.content_json, {})),
        scope: parseJson<MemoryScope>(rule.scope_json, {}),
      });
    }
  }
  return offers;
}

/** Trailing run of confirmed outcomes linked to a rule (resets on any override). */
export function confirmStreak(store: Store, ruleId: string): number {
  const links = store.db
    .query(
      "SELECT detail_json FROM audit WHERE object_type = 'memory' AND object_id = ? AND verb = ? ORDER BY id ASC",
    )
    .all(ruleId, OUTCOME_LINK_VERB) as { detail_json: string }[];
  let streak = 0;
  for (const link of links) {
    const verdict = parseJson<{ verdict?: string }>(link.detail_json, {}).verdict;
    if (verdict === "confirmed") streak++;
    else streak = 0;
  }
  return streak;
}

export function markPromotionOffered(store: Store, id: string): void {
  const offered = store.kvGet<Record<string, string>>(PROMOTION_OFFERED_KV) ?? {};
  offered[id] = store.clock.now().toISOString();
  store.kvSet(PROMOTION_OFFERED_KV, offered);
  store.audit("daemon", "memory.promotion_offered", "memory", id, {});
}

function clearPromotionOffered(store: Store, id: string): void {
  const offered = store.kvGet<Record<string, string>>(PROMOTION_OFFERED_KV) ?? {};
  if (offered[id] === undefined) return;
  delete offered[id];
  store.kvSet(PROMOTION_OFFERED_KV, offered);
}

/** Render the store to markdown files under `dir`. A view, never a second store. */
export function exportMarkdown(store: Store, dir: string): string[] {
  mkdirSync(dir, { recursive: true });
  const written: string[] = [];
  const sections: { file: string; tier: string; title: string }[] = [
    { file: "rules.md", tier: "rule", title: "Rules" },
    { file: "notes.md", tier: "note", title: "Notes" },
    { file: "episodes.md", tier: "episode", title: "Episodes" },
  ];
  for (const section of sections) {
    const rows = store.db
      .query(
        "SELECT * FROM memories WHERE tier = ? ORDER BY status ASC, confidence DESC, created_at DESC",
      )
      .all(section.tier) as MemoryRow[];
    const lines: string[] = [`# ${section.title}`, "", `_${rows.length} entr${rows.length === 1 ? "y" : "ies"}, exported ${store.clock.now().toISOString()}_`, ""];
    if (rows.length === 0) lines.push("_(none)_", "");
    for (const row of rows) {
      const scope = parseJson<MemoryScope>(row.scope_json, {});
      const content = parseJson<Record<string, unknown>>(row.content_json, {});
      const scopeLabel = SCOPE_KEYS.filter((k) => scope[k]).map((k) => `${k}=${scope[k]}`).join(", ") || "global";
      lines.push(`## ${summarize(content)}`, "");
      lines.push(`- id: \`${row.id}\``);
      lines.push(`- kind: ${row.kind} · status: ${row.status} · autonomy: ${row.autonomy}`);
      lines.push(`- scope: ${scopeLabel}`);
      lines.push(
        `- confidence: ${row.confidence.toFixed(3)} (confirm ${row.evidence_confirm} / override ${row.evidence_override})`,
      );
      lines.push(`- authored_by: ${row.authored_by} · created ${row.created_at} · updated ${row.updated_at}`);
      if (row.last_reinforced_at) lines.push(`- last reinforced: ${row.last_reinforced_at}`);
      if (row.last_used_at) lines.push(`- last used: ${row.last_used_at} (${row.use_count}x)`);
      if (row.supersedes) lines.push(`- supersedes: \`${row.supersedes}\``);
      lines.push("", "```json", JSON.stringify(content, null, 2), "```", "");
    }
    const path = join(dir, section.file);
    writeFileSync(path, lines.join("\n"), "utf8");
    written.push(path);
  }
  store.audit("daemon", "memory.exported", "memory", "export", { dir, files: written.length });
  return written;
}

/* ------------------------------------------------------------------ factory */

export function createMemory(store: Store, config: CarConfig): MemoryApi {
  const nowIso = (): string => store.clock.now().toISOString();

  const readCharter = (): string => {
    try {
      return readFileSync(charterPath(config), "utf8");
    } catch {
      return "";
    }
  };

  /* ------------------------------------------------------------ primitives */

  const syncFts = (id: string, content: unknown, scope: MemoryScope): void => {
    store.db.query("DELETE FROM memories_fts WHERE memory_id = ?").run(id);
    store.db
      .query("INSERT INTO memories_fts (memory_id, content, scope_text) VALUES (?, ?, ?)")
      .run(id, contentText(content), scopeText(scope));
  };

  const insert = (input: {
    tier: string;
    kind: string;
    content: Record<string, unknown>;
    scope: MemoryScope;
    authoredBy: string;
    status: string;
    confidence: number;
    autonomy?: string;
    evidenceConfirm?: number;
    provenance?: Record<string, unknown>;
    reinforced?: boolean;
  }): string => {
    const id = memoryId();
    const now = nowIso();
    store.db
      .query(
        `INSERT INTO memories (id, tier, scope_json, kind, content_json, confidence, evidence_confirm,
           autonomy, status, authored_by, created_at, updated_at, last_reinforced_at, provenance_json)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      )
      .run(
        id,
        input.tier,
        JSON.stringify(input.scope),
        input.kind,
        JSON.stringify(input.content),
        input.confidence,
        input.evidenceConfirm ?? 0,
        input.autonomy ?? "none",
        input.status,
        input.authoredBy,
        now,
        now,
        input.reinforced ? now : null,
        input.provenance ? JSON.stringify(input.provenance) : null,
      );
    syncFts(id, input.content, input.scope);
    store.audit(input.authoredBy, "memory.created", "memory", id, {
      tier: input.tier,
      kind: input.kind,
      status: input.status,
      autonomy: input.autonomy ?? "none",
      scope: input.scope,
    });
    return id;
  };

  const archive = (row: MemoryRow, reason: string, detail: Record<string, unknown> = {}): void => {
    store.db
      .query("UPDATE memories SET status = 'archived', updated_at = ? WHERE id = ?")
      .run(nowIso(), row.id);
    store.audit("consolidator", "memory.archived", "memory", row.id, { reason, ...detail });
  };

  const bumpUsage = (ids: string[]): void => {
    if (ids.length === 0) return;
    const now = nowIso();
    const stmt = store.db.query(
      "UPDATE memories SET last_used_at = ?, use_count = use_count + 1 WHERE id = ?",
    );
    store.db.transaction(() => {
      for (const id of ids) stmt.run(now, id);
    })();
  };

  const activeRules = (): MemoryRow[] =>
    store.db
      .query(
        "SELECT * FROM memories WHERE tier = 'rule' AND status = 'active' ORDER BY confidence DESC, updated_at DESC",
      )
      .all() as MemoryRow[];

  /** FTS/BM25 over one tier; falls back to recency when there is nothing to match. */
  const ftsRows = (query: string | null, tier: string | null, limit: number): MemoryRow[] => {
    const tierClause = tier ? " AND m.tier = ?" : "";
    if (query === null) {
      const sql = `SELECT m.* FROM memories m WHERE m.status IN ('active','pending')${tier ? " AND m.tier = ?" : ""} ORDER BY m.confidence DESC, m.created_at DESC LIMIT ?`;
      return (tier ? store.db.query(sql).all(tier, limit) : store.db.query(sql).all(limit)) as MemoryRow[];
    }
    const sql = `SELECT m.*, bm25(memories_fts) AS rank
       FROM memories_fts JOIN memories m ON m.id = memories_fts.memory_id
       WHERE memories_fts MATCH ? AND m.status IN ('active','pending')${tierClause}
       ORDER BY rank ASC LIMIT ?`;
    try {
      return (
        tier ? store.db.query(sql).all(query, tier, limit) : store.db.query(sql).all(query, limit)
      ) as MemoryRow[];
    } catch {
      return [];
    }
  };

  /* ---------------------------------------------------------------- reader */

  const reader: MemoryReader = {
    assembleContext(input) {
      const scopeInput: ScopeInput = {
        vendor: input.vendor,
        repo: input.repo,
        event_type: input.eventType,
        dedupe_class: input.dedupeClass,
      };

      // Priority order, highest first. Truncation drops from the tail:
      // outcomes, then episodes, then notes, then rules. Charter is never cut.
      const rules = activeRules()
        .filter((row) => scopeMatches(parseJson<MemoryScope>(row.scope_json, {}), scopeInput))
        .map(rowToHit);

      // Notes may be pending (an unverified triage proposal, DESIGN §6): the hit
      // carries status='pending' so the prompt can mark it unverified. Rules are
      // active-only — a proposal must never influence a disposition.
      const noteTerms = [input.freeText ?? "", input.repo ?? ""].join(" ");
      const notes = ftsRows(ftsQuery(noteTerms), "note", 25)
        .filter((row) => scopeMatches(parseJson<MemoryScope>(row.scope_json, {}), scopeInput))
        .slice(0, 5)
        .map(rowToHit);

      const episodes = input.dedupeClass
        ? (
            store.db
              .query(
                `SELECT * FROM memories
                 WHERE tier = 'episode' AND status = 'active'
                   AND json_extract(scope_json, '$.dedupe_class') = ?
                 ORDER BY created_at DESC, id DESC LIMIT 3`,
              )
              .all(input.dedupeClass) as MemoryRow[]
          ).map(rowToHit)
        : [];

      const outcomeHits: MemoryHit[] = input.carSessionId
        ? (
            store.db
              .query(
                `SELECT o.id, o.verdict, o.decision_id, o.david_action_json, o.note, o.created_at
                 FROM outcomes o
                 JOIN decisions d ON d.id = o.decision_id
                 JOIN incidents i ON i.id = d.incident_id
                 WHERE i.car_session_id = ?
                 ORDER BY o.created_at DESC, o.id DESC LIMIT 5`,
              )
              .all(input.carSessionId) as {
              id: string;
              verdict: string;
              decision_id: string;
              david_action_json: string | null;
              note: string | null;
              created_at: string;
            }[]
          ).map((row) => ({
            id: row.id,
            tier: "outcome",
            kind: row.verdict,
            content: {
              verdict: row.verdict,
              decision_id: row.decision_id,
              david_action: parseJson<Record<string, unknown> | null>(row.david_action_json, null),
              note: row.note,
              created_at: row.created_at,
            },
            confidence: 1,
            autonomy: "none",
            status: "recorded",
          }))
        : [];

      const charter = readCharter();
      const ordered = [...rules, ...notes, ...episodes, ...outcomeHits];
      let used = estimateTokens(charter) + ordered.reduce((sum, hit) => sum + hitTokens(hit), 0);
      while (used > input.tokenBudget && ordered.length > 0) {
        const dropped = ordered.pop()!;
        used -= hitTokens(dropped);
      }

      bumpUsage(ordered.filter((hit) => hit.tier !== "outcome").map((hit) => hit.id));
      return { charter, hits: ordered };
    },

    search(query, scope) {
      const rows = ftsRows(ftsQuery(query ?? ""), null, 50);
      const scopeInput = (scope ?? {}) as ScopeInput;
      const filtered =
        scope && Object.keys(scope).length > 0
          ? rows.filter((row) => scopeMatches(parseJson<MemoryScope>(row.scope_json, {}), scopeInput))
          : rows;
      return filtered.slice(0, 20).map(rowToHit);
    },

    get(id) {
      const row = store.db.query("SELECT * FROM memories WHERE id = ?").get(id) as MemoryRow | null;
      return row ? rowToHit(row) : null;
    },

    grantedRules(input) {
      const scopeInput: ScopeInput = {
        vendor: input.vendor,
        repo: input.repo,
        event_type: input.eventType,
        dedupe_class: input.dedupeClass,
      };
      return (
        store.db
          .query(
            "SELECT * FROM memories WHERE tier = 'rule' AND status = 'active' AND autonomy = 'granted' ORDER BY confidence DESC",
          )
          .all() as MemoryRow[]
      )
        .filter((row) => scopeMatches(parseJson<MemoryScope>(row.scope_json, {}), scopeInput))
        .map(rowToHit);
    },
  };

  /* ---------------------------------------------------------------- writer */

  /** Resolve the scope a decision happened in, for outcome-driven rule matching. */
  const decisionContext = (decisionId: string): DecisionContext | null => {
    return store.db
      .query(
        `SELECT d.action_class, d.incident_id, d.disposition,
                i.car_session_id, i.dedupe_class, i.summary,
                s.vendor, s.host, s.repo, e.type AS event_type
         FROM decisions d
         LEFT JOIN incidents i ON i.id = d.incident_id
         LEFT JOIN sessions s ON s.car_session_id = i.car_session_id
         LEFT JOIN events e ON e.id = i.opened_by_event
         WHERE d.id = ?`,
      )
      .get(decisionId) as DecisionContext | null;
  };

  const writer: MemoryWriter = {
    addFromDavid(tier, kind, content, scope) {
      // David's memories are active immediately, fully confident, and decay-exempt.
      return insert({
        tier,
        kind,
        content,
        scope: scope as MemoryScope,
        authoredBy: "david",
        status: "active",
        confidence: 1.0,
        autonomy: "none",
        reinforced: true,
        provenance: { source: "david", decay_exempt: true },
      });
    },

    propose(kind, content, scope) {
      // Triage proposals are unverified: pending, and NEVER autonomous.
      return insert({
        tier: kind === "fact" ? "note" : kind === "summary" ? "episode" : "rule",
        kind,
        content,
        scope: scope as MemoryScope,
        authoredBy: "triage",
        status: "pending",
        confidence: 0.5,
        autonomy: "none",
        provenance: { source: "triage", unverified: true },
      });
    },

    recordOutcome(input) {
      const now = nowIso();
      const id = outcomeId();
      store.db
        .query(
          "INSERT INTO outcomes (id, decision_id, escalation_id, verdict, david_action_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        )
        .run(
          id,
          input.decisionId,
          input.escalationId ?? null,
          input.verdict,
          input.davidAction ? JSON.stringify(input.davidAction) : null,
          now,
        );
      store.audit("outcome", "outcome.recorded", "outcome", id, {
        verdict: input.verdict,
        decision: input.decisionId,
      });

      const ctx = decisionContext(input.decisionId);
      if (!ctx) return; // orphan outcome: recorded, but nothing to learn from.

      const scopeInput: ScopeInput = {
        vendor: ctx.vendor ?? undefined,
        repo: ctx.repo ?? undefined,
        host: ctx.host ?? undefined,
        event_type: ctx.event_type ?? undefined,
        dedupe_class: ctx.dedupe_class ?? undefined,
      };
      const confirmed = input.verdict === "confirmed";

      // 1. Deterministic evidence update on every rule this decision exercised.
      const candidates = store.db
        .query("SELECT * FROM memories WHERE tier = 'rule' AND status IN ('active','pending')")
        .all() as MemoryRow[];
      for (const rule of candidates) {
        const scope = parseJson<MemoryScope>(rule.scope_json, {});
        if (!scopeMatches(scope, scopeInput)) continue;
        const content = parseJson<Record<string, unknown>>(rule.content_json, {});
        const match = (content.match ?? {}) as Record<string, unknown>;
        const ruleAction = (content.action_class ?? match.action_class) as string | undefined;
        if (ruleAction !== undefined && ruleAction !== (ctx.action_class ?? undefined)) continue;

        if (confirmed) {
          const next = Math.min(
            CONFIRM_CEILING,
            rule.confidence + CONFIRM_STEP * (1 - rule.confidence),
          );
          store.db
            .query(
              `UPDATE memories SET confidence = ?, evidence_confirm = evidence_confirm + 1,
                 last_reinforced_at = ?, updated_at = ?,
                 provenance_json = json_set(COALESCE(provenance_json, '{}'), '$.last_decay_at', ?)
               WHERE id = ?`,
            )
            .run(next, now, now, now, rule.id);
          store.audit("outcome", "memory.reinforced", "memory", rule.id, {
            outcome: id,
            confidence: next,
          });
        } else {
          const next = rule.confidence * OVERRIDE_FACTOR;
          store.db
            .query(
              "UPDATE memories SET confidence = ?, evidence_override = evidence_override + 1, updated_at = ? WHERE id = ?",
            )
            .run(next, now, rule.id);
          store.audit("outcome", "memory.evidence_override", "memory", rule.id, {
            outcome: id,
            verdict: input.verdict,
            confidence: next,
          });
          // Non-negotiable #4: one overridden outcome demotes an autonomous rule.
          if (rule.autonomy === "granted") {
            store.db
              .query("UPDATE memories SET autonomy = 'suggest', updated_at = ? WHERE id = ?")
              .run(now, rule.id);
            store.audit("outcome", "memory.autonomy_demoted", "memory", rule.id, {
              from: "granted",
              to: "suggest",
              outcome: id,
              verdict: input.verdict,
            });
          }
          // A fresh streak has to be earned before offering promotion again.
          clearPromotionOffered(store, rule.id);
        }

        // Per-rule outcome ledger: the promotion streak is derived from these.
        store.audit("outcome", OUTCOME_LINK_VERB, "memory", rule.id, {
          outcome: id,
          verdict: input.verdict,
          decision: input.decisionId,
        });
      }

      // 2. Episodes are auto-written decision+outcome summaries (DESIGN §6).
      insert({
        tier: "episode",
        kind: "summary",
        content: {
          summary: `${ctx.disposition}${ctx.action_class ? ` ${ctx.action_class}` : ""} → ${input.verdict}${ctx.summary ? `: ${ctx.summary}` : ""}`,
          disposition: ctx.disposition,
          action_class: ctx.action_class,
          verdict: input.verdict,
          decision_id: input.decisionId,
          incident_id: ctx.incident_id,
          outcome_id: id,
          david_action: input.davidAction ?? null,
        },
        scope: {
          vendor: ctx.vendor ?? undefined,
          repo: ctx.repo ?? undefined,
          host: ctx.host ?? undefined,
          event_type: ctx.event_type ?? undefined,
          dedupe_class: ctx.dedupe_class ?? undefined,
        },
        authoredBy: "outcome",
        status: "active",
        confidence: confirmed ? 0.8 : 0.6,
        reinforced: true,
      });
    },

    setAutonomy(id, autonomy, by) {
      // Autonomy is granted, never inferred: only David's own taps reach here.
      if (by !== "david") {
        throw new Error(`memory.setAutonomy: only David may change autonomy (got by='${by}')`);
      }
      const row = store.db.query("SELECT * FROM memories WHERE id = ?").get(id) as MemoryRow | null;
      if (!row) throw new Error(`memory.setAutonomy: unknown memory ${id}`);
      store.db
        .query("UPDATE memories SET autonomy = ?, updated_at = ? WHERE id = ?")
        .run(autonomy, nowIso(), id);
      store.audit(by, "memory.autonomy_set", "memory", id, { from: row.autonomy, to: autonomy });
    },
  };

  /* --------------------------------------------------------- consolidation */

  /** Exponential decay from the later of last reinforcement and last decay pass. */
  const decayAndArchive = (): { decayed: number; archived: number } => {
    const now = store.clock.now();
    const nowIsoStr = now.toISOString();
    const rows = store.db
      .query("SELECT * FROM memories WHERE status = 'active'")
      .all() as MemoryRow[];
    let decayed = 0;
    let archived = 0;
    for (const row of rows) {
      const provenance = parseJson<Record<string, unknown>>(row.provenance_json, {});
      if (row.authored_by === "david" || provenance.decay_exempt === true) continue;
      const base = maxIso(
        row.last_reinforced_at ?? row.created_at,
        typeof provenance.last_decay_at === "string" ? provenance.last_decay_at : null,
      );
      const elapsedMs = now.getTime() - new Date(base ?? row.created_at).getTime();
      if (elapsedMs <= 0) continue;
      const elapsedDays = elapsedMs / 86_400_000;
      const next = row.confidence * Math.pow(0.5, elapsedDays / DECAY_HALF_LIFE_DAYS);
      provenance.last_decay_at = nowIsoStr;
      store.db
        .query("UPDATE memories SET confidence = ?, provenance_json = ?, updated_at = ? WHERE id = ?")
        .run(next, JSON.stringify(provenance), nowIsoStr, row.id);
      decayed++;
      store.audit("consolidator", "memory.decayed", "memory", row.id, {
        from: row.confidence,
        to: next,
        elapsed_days: Number(elapsedDays.toFixed(3)),
      });
      if (next < ARCHIVE_CONFIDENCE_FLOOR) {
        archive({ ...row, confidence: next }, "confidence_below_floor", { confidence: next });
        archived++;
      }
    }
    return { decayed, archived };
  };

  /** Merge exact-duplicate notes within a scope; newest survives, older archived. */
  const mergeDuplicateNotes = (): number => {
    const notes = store.db
      .query(
        "SELECT * FROM memories WHERE tier = 'note' AND status = 'active' ORDER BY created_at DESC, id DESC",
      )
      .all() as MemoryRow[];
    const groups = new Map<string, MemoryRow[]>();
    for (const note of notes) {
      const key = canonicalScope(parseJson<MemoryScope>(note.scope_json, {}));
      const bucket = groups.get(key);
      if (bucket) bucket.push(note);
      else groups.set(key, [note]);
    }

    let merged = 0;
    for (const bucket of groups.values()) {
      if (bucket.length < 2) continue;
      const survivors: { row: MemoryRow; text: string; mergedFrom: string[] }[] = [];
      for (const note of bucket) {
        // newest-first, so the first occurrence of a cluster is the survivor
        const text = contentText(parseJson<unknown>(note.content_json, {}));
        const query = ftsQuery(text);
        const candidates = query
          ? new Set(
              (
                store.db
                  .query(
                    `SELECT memories_fts.memory_id AS id FROM memories_fts
                     JOIN memories m ON m.id = memories_fts.memory_id
                     WHERE memories_fts MATCH ? AND m.tier = 'note' AND m.status = 'active'`,
                  )
                  .all(query) as { id: string }[]
              ).map((r) => r.id),
            )
          : null;
        const survivor = survivors.find(
          (s) =>
            (candidates === null || candidates.has(s.row.id)) && similarity(s.text, text) >= 0.9,
        );
        if (!survivor) {
          survivors.push({ row: note, text, mergedFrom: [] });
          continue;
        }
        const now = nowIso();
        const provenance = parseJson<Record<string, unknown>>(note.provenance_json, {});
        provenance.superseded_by = survivor.row.id;
        store.db
          .query(
            "UPDATE memories SET status = 'archived', provenance_json = ?, updated_at = ? WHERE id = ?",
          )
          .run(JSON.stringify(provenance), now, note.id);
        survivor.mergedFrom.push(note.id);
        const survivorProvenance = parseJson<Record<string, unknown>>(
          survivor.row.provenance_json,
          {},
        );
        survivorProvenance.merged_from = survivor.mergedFrom;
        store.db
          .query("UPDATE memories SET supersedes = ?, provenance_json = ?, updated_at = ? WHERE id = ?")
          .run(note.id, JSON.stringify(survivorProvenance), now, survivor.row.id);
        survivor.row.provenance_json = JSON.stringify(survivorProvenance);
        store.audit("consolidator", "memory.merged", "memory", survivor.row.id, {
          archived: note.id,
          reason: "duplicate_note",
        });
        merged++;
      }
    }
    return merged;
  };

  /**
   * Distill episode clusters (same dedupe_class, >= DISTILL_MIN_EPISODES
   * consistent confirmed outcomes) into a *pending* rule proposal. Never active,
   * never autonomous — David or the digest review promotes it.
   */
  const distillEpisodes = (): number => {
    const episodes = store.db
      .query(
        "SELECT * FROM memories WHERE tier = 'episode' AND status = 'active' ORDER BY created_at ASC, id ASC",
      )
      .all() as MemoryRow[];
    const clusters = new Map<string, MemoryRow[]>();
    for (const episode of episodes) {
      const dedupeClass = parseJson<MemoryScope>(episode.scope_json, {}).dedupe_class;
      if (!dedupeClass) continue;
      const bucket = clusters.get(dedupeClass);
      if (bucket) bucket.push(episode);
      else clusters.set(dedupeClass, [episode]);
    }

    let distilled = 0;
    for (const [dedupeClass, bucket] of clusters) {
      const existing = store.db
        .query("SELECT id, scope_json FROM memories WHERE tier = 'rule' AND status != 'archived'")
        .all() as { id: string; scope_json: string }[];
      if (
        existing.some((r) => parseJson<MemoryScope>(r.scope_json, {}).dedupe_class === dedupeClass)
      ) {
        continue;
      }

      // "Consistent" = same disposition, confirmed by David, across *distinct*
      // decisions — repeated outcomes on one decision are one piece of evidence.
      const byDisposition = new Map<string, MemoryRow[]>();
      const seenDecisions = new Map<string, Set<string>>();
      for (const episode of bucket) {
        const content = parseJson<Record<string, unknown>>(episode.content_json, {});
        if (content.verdict !== "confirmed") continue;
        const disposition = typeof content.disposition === "string" ? content.disposition : null;
        if (!disposition) continue;
        const decision = typeof content.decision_id === "string" ? content.decision_id : episode.id;
        const seen = seenDecisions.get(disposition) ?? new Set<string>();
        if (seen.has(decision)) continue;
        seen.add(decision);
        seenDecisions.set(disposition, seen);
        const list = byDisposition.get(disposition);
        if (list) list.push(episode);
        else byDisposition.set(disposition, [episode]);
      }
      let best: { disposition: string; rows: MemoryRow[] } | null = null;
      for (const [disposition, rows] of byDisposition) {
        if (rows.length >= DISTILL_MIN_EPISODES && (!best || rows.length > best.rows.length)) {
          best = { disposition, rows };
        }
      }
      if (!best) continue;

      const contents = best.rows.map((r) => parseJson<Record<string, unknown>>(r.content_json, {}));
      const scopes = best.rows.map((r) => parseJson<MemoryScope>(r.scope_json, {}));
      const agreed = (key: ScopeKey): string | undefined => {
        const first = scopes[0]?.[key];
        return first && scopes.every((s) => s[key] === first) ? first : undefined;
      };
      const firstAction = contents[0]?.action_class;
      const actionClass =
        typeof firstAction === "string" && contents.every((c) => c.action_class === firstAction)
          ? firstAction
          : undefined;

      insert({
        tier: "rule",
        kind: "preference",
        content: {
          summary: `Distilled: ${best.disposition}${actionClass ? ` (${actionClass})` : ""} for ${dedupeClass}`,
          match: { dedupe_class: dedupeClass },
          disposition: best.disposition,
          ...(actionClass ? { action_class: actionClass } : {}),
          distilled_from: best.rows.map((r) => r.id),
        },
        scope: {
          dedupe_class: dedupeClass,
          vendor: agreed("vendor"),
          repo: agreed("repo"),
          event_type: agreed("event_type"),
        },
        authoredBy: "consolidator",
        status: "pending",
        confidence: 0.5,
        autonomy: "none",
        evidenceConfirm: best.rows.length,
        provenance: { source: "consolidator", distilled_from: best.rows.map((r) => r.id) },
      });
      distilled++;
    }
    return distilled;
  };

  /** Per-scope active-note cap: archive the lowest-confidence tail. */
  const capNotesPerScope = (): number => {
    const notes = store.db
      .query("SELECT * FROM memories WHERE tier = 'note' AND status = 'active'")
      .all() as MemoryRow[];
    const groups = new Map<string, MemoryRow[]>();
    for (const note of notes) {
      const key = canonicalScope(parseJson<MemoryScope>(note.scope_json, {}));
      const bucket = groups.get(key);
      if (bucket) bucket.push(note);
      else groups.set(key, [note]);
    }
    let capped = 0;
    for (const bucket of groups.values()) {
      if (bucket.length <= NOTE_CAP_PER_SCOPE) continue;
      bucket.sort(
        (a, b) =>
          b.confidence - a.confidence ||
          (a.created_at < b.created_at ? 1 : a.created_at > b.created_at ? -1 : 0),
      );
      for (const row of bucket.slice(NOTE_CAP_PER_SCOPE)) {
        archive(row, "scope_note_cap", { cap: NOTE_CAP_PER_SCOPE, confidence: row.confidence });
        capped++;
      }
    }
    return capped;
  };

  /**
   * Nightly consolidation (no LLM in v1). Never touches autonomy or charter.md.
   */
  const consolidationJob = async (): Promise<void> => {
    const startedAt = nowIso();
    const { decayed, archived } = decayAndArchive();
    const merged = mergeDuplicateNotes();
    const distilled = distillEpisodes();
    const capped = capNotesPerScope();
    store.audit("consolidator", "memory.consolidated", "memory", "consolidation", {
      started_at: startedAt,
      decayed,
      archived,
      merged,
      distilled,
      capped,
    });
  };

  return {
    reader,
    writer,
    consolidationJob,
    pendingPromotionOffers: () => pendingPromotionOffers(store),
    markPromotionOffered: (id: string) => markPromotionOffered(store, id),
    exportMarkdown: (dir: string) => exportMarkdown(store, dir),
  };
}
