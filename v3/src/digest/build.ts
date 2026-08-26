/**
 * Digest builder — non-negotiable #2: the daily digest is UNCONDITIONAL.
 * A day where nothing happened still produces a digest that says so, in words.
 * v2's founding failure was silence that looked like health.
 *
 * Pure over the store (+ an injected watchdog result), so the whole thing is
 * assertable from a seeded `:memory:` database.
 */
import type { Store } from "../store/db.ts";
import type { CarConfig } from "../config/config.ts";
import { OUTBOX_STATE } from "../surfaces/telegram/types.ts";
import { localDay } from "./time.ts";
import type { StuckLine } from "./watchdog.ts";

export interface HandledItem {
  decisionId: string;
  label: string;
}
export interface ResolvedItem {
  escalationId: string;
  label: string;
}
export interface HeldItem {
  outboxId: number;
  label: string;
}
export interface PromotionOffer {
  memoryId: string;
  label: string;
}
export interface SpendSection {
  triageUsd: number;
  triageCalls: number;
  agentsUsd: number;
  byModel: { model: string; costUsd: number; calls: number }[];
}
export interface MemorySection {
  pending: number;
  offers: PromotionOffer[];
}

export interface DigestData {
  /** Local calendar day this digest is filed under. */
  day: string;
  since: string;
  handled: HandledItem[];
  resolved: ResolvedItem[];
  stuck: StuckLine[];
  held: HeldItem[];
  spend: SpendSection;
  memory: MemorySection;
  escalateOnly: boolean;
  missedDays: string[];
}

const MAX_ITEMS = 12;

export async function buildDigest(
  store: Store,
  config: CarConfig,
  opts: { stuck?: StuckLine[]; windowHours?: number; missedDays?: string[] } = {},
): Promise<DigestData> {
  const now = store.clock.now();
  const windowHours = opts.windowHours ?? 24;
  const since = new Date(now.getTime() - windowHours * 3_600_000).toISOString();

  return {
    day: localDay(now),
    since,
    handled: handledAutonomously(store, since),
    resolved: davidResolved(store, since),
    stuck: opts.stuck ?? [],
    held: heldForDigest(store),
    spend: spendSection(store, since),
    memory: await memorySection(store, config),
    escalateOnly: store.kvGet<boolean>("escalate_only") === true,
    missedDays: opts.missedDays ?? [],
  };
}

/** Decisions CAR made on its own — each gets a 👍/👎 pair in the rendered digest. */
function handledAutonomously(store: Store, since: string): HandledItem[] {
  const rows = store.db
    .query(
      `SELECT d.id AS id, d.disposition AS disposition, d.action_class AS action_class,
              d.rationale AS rationale, d.decided_by AS decided_by,
              i.summary AS summary, i.car_session_id AS car_session_id,
              s.vendor AS vendor, s.title AS title, s.repo AS repo
       FROM decisions d
       JOIN incidents i ON i.id = d.incident_id
       LEFT JOIN sessions s ON s.car_session_id = i.car_session_id
       WHERE d.decided_by IN ('rules', 'llm') AND d.created_at >= ?
       ORDER BY d.created_at ASC
       LIMIT ?`,
    )
    .all(since, MAX_ITEMS) as {
    id: string;
    disposition: string;
    action_class: string | null;
    rationale: string;
    decided_by: string;
    summary: string;
    vendor: string | null;
    title: string | null;
    repo: string | null;
  }[];

  return rows.map((r) => ({
    decisionId: r.id,
    label: compact([
      r.rationale || r.action_class || r.disposition,
      r.title || lastSegment(r.repo) || r.vendor,
    ]),
  }));
}

function davidResolved(store: Store, since: string): ResolvedItem[] {
  const rows = store.db
    .query(
      `SELECT e.id AS id, e.question AS question, e.answer_json AS answer_json
       FROM escalations e
       WHERE e.answered_by = 'david' AND e.answered_at >= ?
       ORDER BY e.answered_at ASC LIMIT ?`,
    )
    .all(since, MAX_ITEMS) as { id: string; question: string; answer_json: string | null }[];

  return rows.map((r) => ({
    escalationId: r.id,
    label: compact([answerVerb(r.answer_json), truncate(r.question, 80)]),
  }));
}

function answerVerb(answerJson: string | null): string {
  if (!answerJson) return "resolved";
  try {
    const parsed = JSON.parse(answerJson) as { approval?: boolean; text?: string };
    if (parsed.approval === true) return "approved";
    if (parsed.approval === false) return "denied";
    if (parsed.text) return "answered";
  } catch {
    /* ignore */
  }
  return "resolved";
}

/** Messages parked by quiet hours / mute: the digest is where they land. */
function heldForDigest(store: Store): HeldItem[] {
  const rows = store.db
    .query(
      `SELECT id, body_json FROM outbox
       WHERE state = ? ORDER BY id ASC LIMIT ?`,
    )
    .all(OUTBOX_STATE.deferred, MAX_ITEMS) as { id: number; body_json: string }[];
  return rows.map((r) => ({ outboxId: r.id, label: truncate(bodyText(r.body_json), 120) }));
}

function bodyText(bodyJson: string): string {
  try {
    const parsed = JSON.parse(bodyJson) as { text?: string };
    return (parsed.text ?? "").split("\n").join(" · ");
  } catch {
    return "(unreadable message)";
  }
}

/** Mark held rows as folded into the digest, so they are not re-listed tomorrow. */
export function markHeldDelivered(store: Store, ids: number[]): void {
  for (const id of ids) {
    store.db.query("UPDATE outbox SET state = ? WHERE id = ?").run(OUTBOX_STATE.sent, id);
    store.audit("daemon", "outbox.folded_into_digest", "outbox", String(id), {});
  }
}

function spendSection(store: Store, since: string): SpendSection {
  const today = store.spendToday();
  const byModel = store.db
    .query(
      `SELECT model, SUM(cost_usd) AS cost_usd, SUM(calls) AS calls
       FROM spend WHERE day = ? GROUP BY model ORDER BY cost_usd DESC`,
    )
    .all(store.clock.now().toISOString().slice(0, 10)) as {
    model: string;
    cost_usd: number;
    calls: number;
  }[];

  const agents = store.db
    .query(
      `SELECT COALESCE(SUM(CAST(json_extract(payload_json, '$.cost_usd') AS REAL)), 0) AS total
       FROM events WHERE type = 'cost.report' AND received_at >= ?`,
    )
    .get(since) as { total: number } | null;

  return {
    triageUsd: today.cost_usd ?? 0,
    triageCalls: today.calls ?? 0,
    agentsUsd: agents?.total ?? 0,
    byModel: byModel.map((r) => ({ model: r.model, costUsd: r.cost_usd, calls: r.calls })),
  };
}

/**
 * Memory section. `pendingPromotionOffers` belongs to WS-C; when it is not
 * exported yet we fall back to the DESIGN §6 rule directly (a `suggest` rule
 * that matched David N times with zero overrides is offer-worthy).
 */
async function memorySection(store: Store, _config: CarConfig): Promise<MemorySection> {
  const pendingRow = store.db
    .query("SELECT COUNT(*) AS n FROM memories WHERE status = 'pending'")
    .get() as { n: number } | null;

  let offers: PromotionOffer[] | null = null;
  try {
    const mod = (await import("../memory/index.ts")) as Record<string, unknown>;
    const fn = mod.pendingPromotionOffers;
    if (typeof fn === "function") {
      offers = normalizeOffers(await (fn as (s: Store) => unknown)(store));
    }
  } catch {
    /* memory module may not export promotion offers yet — degrade, never fail */
  }

  // An empty array from the memory module is an ANSWER ("nothing to offer",
  // e.g. these were already offered), not a gap. Only a missing export falls back.
  return { pending: pendingRow?.n ?? 0, offers: offers ?? fallbackOffers(store) };
}

function normalizeOffers(raw: unknown): PromotionOffer[] {
  if (!Array.isArray(raw)) return [];
  const out: PromotionOffer[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const rec = item as Record<string, unknown>;
    const id = (rec.memoryId ?? rec.memory_id ?? rec.id) as string | undefined;
    if (typeof id !== "string") continue;
    const label =
      (typeof rec.label === "string" && rec.label) ||
      (typeof rec.summary === "string" && rec.summary) ||
      (typeof rec.question === "string" && rec.question) ||
      "Auto-handle this from now on?";
    out.push({ memoryId: id, label });
  }
  return out;
}

const PROMOTION_THRESHOLD = 4;

/** Used only when the memory module exposes no promotion API at all. */
export function fallbackOffers(store: Store): PromotionOffer[] {
  const rows = store.db
    .query(
      `SELECT id, content_json, scope_json, evidence_confirm
       FROM memories
       WHERE tier = 'rule' AND status = 'active' AND autonomy = 'suggest'
         AND evidence_confirm >= ? AND evidence_override = 0
       ORDER BY evidence_confirm DESC LIMIT 3`,
    )
    .all(PROMOTION_THRESHOLD) as {
    id: string;
    content_json: string;
    scope_json: string;
    evidence_confirm: number;
  }[];

  return rows.map((r) => {
    let match = "this";
    let scopeLabel = "";
    try {
      const content = JSON.parse(r.content_json) as { match?: string; question?: string };
      match = content.question || content.match || match;
      const scope = JSON.parse(r.scope_json) as { repo?: string; vendor?: string };
      scopeLabel = scope.repo ? ` on ${lastSegment(scope.repo)}` : scope.vendor ? ` for ${scope.vendor}` : "";
    } catch {
      /* ignore */
    }
    return {
      memoryId: r.id,
      label: `${truncate(match, 60)}${scopeLabel} (matched you ${r.evidence_confirm}×)`,
    };
  });
}

/* ------------------------------------------------------------------ utils */

function compact(parts: (string | null | undefined)[]): string {
  return parts.filter((p): p is string => Boolean(p && p.trim())).join(" — ");
}

function truncate(value: string, max: number): string {
  const flat = value.replace(/\s+/g, " ").trim();
  return flat.length <= max ? flat : `${flat.slice(0, max - 1)}…`;
}

function lastSegment(value: string | null | undefined): string | null {
  if (!value) return null;
  const seg = value.split("/").filter(Boolean);
  return seg.length ? seg[seg.length - 1]! : value;
}
