/**
 * GET /ui/brief.md — plain markdown for other agents to curl: open escalations,
 * stuck sessions, and yesterday's digest if present. Pure reads over deps.store.db.
 */
import type { Store } from "../../store/db.ts";
import { getDigest } from "./queries.ts";

function hoursSince(now: Date, iso: string): number {
  return Math.max(0, (now.getTime() - new Date(iso).getTime()) / 3_600_000);
}

interface OpenEscalation {
  id: string;
  question: string;
  created_at: string;
}

interface StuckSession {
  car_session_id: string;
  since: string;
  n: number;
  title: string | null;
  repo: string | null;
  vendor: string | null;
}

const RESOLVED_TRIAGE_STATES = new Set(["llm_resolved", "rules_resolved", "expired", "skipped"]);

export function buildBriefMarkdown(store: Store): string {
  const now = store.clock.now();

  const escalations = store.db
    .query(`SELECT id, question, created_at FROM escalations WHERE state = 'pending' ORDER BY created_at ASC`)
    .all() as OpenEscalation[];

  const placeholders = [...RESOLVED_TRIAGE_STATES].map(() => "?").join(",");
  const stuck = store.db
    .query(
      `SELECT e.car_session_id AS car_session_id, MIN(e.ts) AS since, COUNT(*) AS n,
              s.title AS title, s.repo AS repo, s.vendor AS vendor
       FROM events e
       LEFT JOIN sessions s ON e.car_session_id = s.car_session_id
       WHERE e.requires_response = 1 AND e.car_session_id IS NOT NULL
         AND e.triage_state NOT IN (${placeholders})
       GROUP BY e.car_session_id
       ORDER BY since ASC`,
    )
    .all(...RESOLVED_TRIAGE_STATES) as StuckSession[];

  const yesterday = new Date(now.getTime() - 24 * 3_600_000).toISOString().slice(0, 10);
  const yesterdayDigest = getDigest(store.db, yesterday);

  const lines: string[] = ["# CAR brief", ""];

  lines.push(`## Open escalations (${escalations.length})`, "");
  if (escalations.length === 0) {
    lines.push("_none_");
  } else {
    for (const e of escalations) {
      lines.push(`- \`${e.id}\` (${hoursSince(now, e.created_at).toFixed(1)}h): ${e.question}`);
    }
  }
  lines.push("");

  lines.push(`## Stuck sessions — pending response (${stuck.length})`, "");
  if (stuck.length === 0) {
    lines.push("_none_");
  } else {
    for (const s of stuck) {
      const label = s.title || s.repo || s.car_session_id;
      lines.push(
        `- \`${s.car_session_id}\` ${s.vendor ? `(${s.vendor}) ` : ""}${label} — pending ${hoursSince(now, s.since).toFixed(1)}h, ${s.n} event(s)`,
      );
    }
  }
  lines.push("");

  lines.push(`## Yesterday's digest (${yesterday})`, "");
  if (yesterdayDigest) {
    lines.push(yesterdayDigest.rendered_md);
  } else {
    lines.push("_no digest recorded for yesterday_");
  }
  lines.push("");

  return lines.join("\n");
}
