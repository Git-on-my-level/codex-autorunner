/**
 * Rules pass — DESIGN §5 step 2. Pure functions over (event + policy + rule-tier
 * memories). No LLM, no I/O, $0. This is the layer that must stay boring: the
 * LLM is the exception path, and the two paths that must NEVER reach it are
 * `severity=urgent` (no model between David and a page) and `actor='car'`
 * (self-event suppression, the loop guard).
 */
import type { EventRow } from "../store/db.ts";
import type { MemoryHit } from "../ports.ts";

/** Zero-signal lifecycle noise: keep-informed, never triaged. */
export const TRIVIAL_TYPES = new Set(["heartbeat", "progress", "session.started", "artifact", "cost.report"]);

/** severity ordering for comparisons */
const SEVERITY_RANK: Record<string, number> = { info: 0, notice: 1, attention: 2, urgent: 3 };

export function severityRank(severity: string): number {
  return SEVERITY_RANK[severity] ?? 0;
}

/** `session.ended` payload outcomes that mean "nothing to see here". */
const OK_OUTCOMES = new Set(["ok", "success", "succeeded", "complete", "completed", "passed", "done", "0"]);

export type RuleOutcome =
  | { kind: "resolved"; reason: string }
  | { kind: "expired"; reason: string }
  /** Page David now; no model in the loop. */
  | { kind: "escalate"; reason: string }
  /** David granted autonomy for exactly this; execute it deterministically. */
  | { kind: "granted"; reason: string; rule: MemoryHit }
  /** CAR's own event: attach to the originating incident, never open LLM triage. */
  | { kind: "self_event"; reason: string }
  /** Nothing deterministic applies — coalesce and hand to the LLM. */
  | { kind: "llm"; reason: string };

export interface RulesContext {
  now: Date;
  /** memoryReader.grantedRules(...) for this event's scope. */
  grantedRules: MemoryHit[];
}

function parsePayload(row: EventRow): Record<string, unknown> {
  try {
    const parsed = JSON.parse(row.payload_json) as unknown;
    return typeof parsed === "object" && parsed !== null ? (parsed as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

/** `session.ended` is only auto-resolvable when it plainly succeeded. */
export function sessionEndedOk(row: EventRow): boolean {
  const payload = parsePayload(row);
  const raw = payload.outcome ?? payload.status ?? payload.result;
  const outcome = typeof raw === "string" || typeof raw === "number" ? String(raw).toLowerCase() : "";
  if (outcome) return OK_OUTCOMES.has(outcome);
  const exit = payload.exit_code ?? payload.exitCode;
  if (typeof exit === "number") return exit === 0;
  // No outcome reported: trust severity. attention/urgent means it ended badly.
  return severityRank(row.severity) < SEVERITY_RANK.attention!;
}

/**
 * Classify one claimed event. The caller owns all side effects; this function
 * only decides. Order is load-bearing and mirrors DESIGN §5.
 */
export function classifyEvent(row: EventRow, ctx: RulesContext): RuleOutcome {
  // 0. Expired events are moot by definition (contract `expires_at`).
  if (row.expires_at && new Date(row.expires_at).getTime() < ctx.now.getTime()) {
    return { kind: "expired", reason: `event expired at ${row.expires_at}` };
  }

  // 1. Trivial lifecycle types — $0, no incident, whoever the actor is.
  if (TRIVIAL_TYPES.has(row.type)) {
    return { kind: "resolved", reason: `trivial type ${row.type}` };
  }

  // 2. urgent → page immediately. NO LLM EVER between David and a page.
  //    This outranks self-event suppression: suppression exists to stop runaway
  //    *LLM triage*, and escalating costs no tokens and starts no loop.
  if (row.severity === "urgent") {
    return { kind: "escalate", reason: "severity=urgent: escalate immediately, no LLM" };
  }

  // 3. Self-event suppression (DESIGN §5(d)): events CAR's own actions caused
  //    attach to the originating incident and never open fresh LLM triage.
  if (row.actor === "car") {
    return { kind: "self_event", reason: "actor=car: self-event suppression" };
  }

  // 4. session.ended that plainly succeeded.
  if (row.type === "session.ended") {
    return sessionEndedOk(row)
      ? { kind: "resolved", reason: "session.ended ok" }
      : { kind: "llm", reason: "session.ended failed" };
  }

  // 5. FYI notes below the attention bar.
  if (row.type === "note") {
    return severityRank(row.severity) < SEVERITY_RANK.attention!
      ? { kind: "resolved", reason: `note below attention (severity=${row.severity})` }
      : { kind: "llm", reason: `note at severity=${row.severity}` };
  }

  // 6. attention.* matching an autonomy=granted rule → execute directly.
  if (row.type.startsWith("attention.")) {
    const rule = ctx.grantedRules.find((r) => r.autonomy === "granted" && r.status === "active");
    if (rule) {
      return { kind: "granted", reason: `granted rule ${rule.id}`, rule };
    }
  }

  // 7. attention.cleared with nothing outstanding is housekeeping.
  if (row.type === "attention.cleared") {
    return { kind: "resolved", reason: "attention.cleared" };
  }

  return { kind: "llm", reason: `no rule applies to ${row.type}/${row.severity}` };
}
