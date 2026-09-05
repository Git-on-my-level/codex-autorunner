/** One set of predicates for board rows and counts. Pagination must not hide obligations. */
import type { Store } from "../../store/db.ts";
import type { NavigationCounts } from "./layout.tsx";
export type DecisionTab = "needs_you" | "watching" | "handled";
export const REQUEST_CONDITION: Record<DecisionTab, string> = {
  needs_you: "(state='needs_you' OR (state='expired' AND reviewed_at IS NULL))",
  watching: "state IN ('preparing','answered','received')",
  handled: "(state IN ('resolved','cancelled') OR (state='expired' AND reviewed_at IS NOT NULL))",
};
export const NATIVE_CONDITION: Record<DecisionTab, string> = {
  needs_you: "s.state='pending' AND h.id IS NULL AND e.obligation_state NOT IN ('resolved','cancelled','expired')",
  watching: "e.obligation_state NOT IN ('resolved','cancelled','expired') AND (s.state='snoozed' OR h.id IS NOT NULL)",
  handled: "e.obligation_state IN ('resolved','cancelled','expired')",
};
export const NATIVE_JOIN = `FROM escalations s JOIN incidents i ON i.id=s.incident_id
  JOIN events e ON e.id=COALESCE(s.origin_event_id,i.opened_by_event)
  LEFT JOIN human_replies h ON h.escalation_id=s.id
  WHERE NOT EXISTS (SELECT 1 FROM attention_requests r WHERE r.escalation_id=s.id)`;
export function decisionCounts(store: Store, workspace: string): NavigationCounts {
  const result: NavigationCounts = { needs_you: 0, watching: 0, handled: 0 };
  for (const tab of Object.keys(result) as DecisionTab[]) {
    const requestCount = (store.db.query(`SELECT COUNT(*) AS n FROM attention_requests WHERE workspace_id=? AND ${REQUEST_CONDITION[tab]}`).get(workspace) as { n: number }).n;
    const nativeCount = (store.db.query(`SELECT COUNT(*) AS n ${NATIVE_JOIN} AND (${NATIVE_CONDITION[tab]})`).get() as { n: number }).n;
    result[tab] = requestCount + nativeCount;
  }
  result.watching += (store.db.query(`SELECT COUNT(*) AS n FROM human_replies WHERE escalation_id IS NULL AND request_id IS NULL
    AND state IN ('failed','uncertain','staged','delivered')`).get() as { n: number }).n;
  return result;
}
