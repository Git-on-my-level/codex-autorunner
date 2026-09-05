/** Machine-readable next actions. Deterministic and shared by every agent surface. */
import type { RequestRow } from "./contract.ts";

export const REQUEST_STATES = ["preparing", "needs_you", "answered", "received", "resolved", "cancelled", "expired"] as const;
export const terminalRequest = (state: string): boolean => ["resolved", "cancelled", "expired"].includes(state);
export const deadlineElapsed = (row: Pick<RequestRow, "state" | "due_at">, now: string): boolean =>
  ["preparing", "needs_you", "answered"].includes(row.state) && row.due_at !== null && row.due_at <= now;

export const REQUEST_GUIDE = {
  contract: "car.guide.v1",
  purpose: "Bring the human a grounded decision, not an unexplained alert. CAR owns the handoff, not your work.",
  steps: [
    "Raise once with a stable idempotency_key, goal, blocker and specific question.",
    "Follow guidance.code and context_requests. Investigate close to the source. State cannot_investigate instead of inventing evidence.",
    "Wait without blocking unrelated work. Local spooling is not server acceptance or human notification.",
    "Use receive to durably save and acknowledge the exact answer before applying it. A GET is not a receipt.",
    "Report resolved only when the actual blocker is gone. Cancel obsolete requests with a reason.",
  ],
  invariants: [
    "Silence, a timeout, memory and a recommendation never confer approval.",
    "An answer applies only to its immutable request, not to another task or standing permission.",
    "Published decisions cannot change under the human. Cancel and replace a changed question.",
    "A cancellation cannot roll back an action already taken. Check the current request before acting.",
  ],
  states: REQUEST_STATES,
} as const;

export function requestGuidance(row: RequestRow, answerId: string | null, now: string) {
  const state = deadlineElapsed(row, now) ? "expired" : row.state;
  const id = row.id;
  const code = state === "preparing" ? "add_context" : state === "needs_you" ? "wait_for_human" :
    state === "answered" ? "receive_answer" : state === "received" ? "report_resolution" : "stop";
  const action = state === "preparing" ? {
    tool: "car_context", cli: `card request context ${id} --revision ${row.revision} --file packet.json`,
    arguments: { id, expected_revision: row.revision }, required: ["packet"],
  } : state === "needs_you" ? {
    tool: "car_get", cli: `card wait ${id} --timeout 60`, arguments: { id }, required: [],
  } : state === "answered" ? {
    tool: "car_receive", cli: `card request receive ${id}`, arguments: { id }, required: [],
  } : state === "received" ? {
    tool: "car_ack", cli: `card request ack ${id} --answer ${answerId} --outcome resolved --note 'How the blocker was cleared'`,
    arguments: { id, answer_id: answerId, outcome: "resolved" }, required: ["confirm_actual_blocker_cleared"],
  } : null;
  return { contract: "car.guidance.v1" as const, code, action,
    poll_after_ms: state === "needs_you" || state === "preparing" ? 2_000 : null,
    answer_scope: "this_request_only" as const, standing_permission: false,
    can_apply_answer: state === "received" && Boolean(answerId),
    deadline_at: row.due_at,
    cancel: terminalRequest(state) ? null : { tool: "car_cancel", arguments: { id, expected_revision: row.revision }, required: ["reason"] },
  };
}
