/** Machine-readable next actions. Deterministic and shared by every agent surface. */
import type { RequestRow } from "./contract.ts";

export const REQUEST_STATES = ["preparing", "needs_you", "answered", "received", "resolved", "cancelled", "expired"] as const;
export const terminalRequest = (state: string): boolean => ["resolved", "cancelled", "expired"].includes(state);
export const deadlineElapsed = (row: Pick<RequestRow, "state" | "due_at">, now: string): boolean =>
  ["preparing", "needs_you", "answered"].includes(row.state) && row.due_at !== null && row.due_at <= now;

export const REQUEST_GUIDE = {
  contract: "car.guide.v1",
  purpose: "Bring the human a grounded decision, not an unexplained alert. CAR owns the handoff, not your work.",
  writing: [
    "Write a short human-readable question. Put evidence in facts, the authority gap in why_human, and tradeoffs in options; avoid repeating the same explanation in every field.",
    "Write recommendation.answer as the actual proposed decision in plain language, never an internal option id. Keep exact conditions and meaningful uncertainty visible.",
    "Label synthetic evidence and historical snapshots honestly. Do not invent a deadline, current incident, missing access, or external verification.",
    "Routine waiting for another agent or prerequisite does not itself require a human decision. Raise only the unresolved judgment, not permission to wait.",
    "Invoke the CLI with literal argv and check its exit status before parsing output or retrying; a printed error is not a successful operation.",
  ],
  steps: [
    "Raise once with a stable idempotency_key, a short question, and the specific blocker.",
    "Fill context_requests from the source. Use why_human for a real authority gap; use cannot_investigate for routine access or dependency limits.",
    "Keep working on unrelated work while CAR waits. Local spooling is not server acceptance or human notification.",
    "Use receive to durably save and acknowledge the exact answer before applying it. A GET is not a receipt.",
    "Report resolved only when the actual blocker is gone. Cancel obsolete requests with a reason.",
  ],
  authority: {
    human_decision: "A human judgment is required only when existing instructions and source authority do not answer the question.",
    dependency_wait: "A missing file, service, or routine investigation is a dependency wait; record the limit and keep the request grounded.",
    no_inference: "Do not turn a recommendation, silence, timeout, or cached answer into permission.",
  },
  invariants: [
    "Silence, a timeout, memory and a recommendation never confer approval.",
    "An answer applies only to its immutable request, not to another task or standing permission.",
    "Published decisions cannot change under the human. Cancel and replace a changed question.",
    "A cancellation cannot roll back an action already taken. Check the current request before acting.",
  ],
  states: REQUEST_STATES,
} as const;

export function requestGuidance(row: RequestRow, answerId: string | null, now: string) {
  const packet = JSON.parse(row.packet_json) as { why_human?: string; cannot_investigate?: string };
  const state = deadlineElapsed(row, now) ? "expired" : row.state;
  const id = row.id;
  const code = state === "preparing" ? "add_context" : state === "needs_you" ? "wait_for_human" :
    state === "answered" ? "receive_answer" : state === "received" ? "report_resolution" : "stop";
  const action = state === "preparing" ? {
    tool: "car_context", cli: `card request context ${id} --revision ${row.revision} --file context.json`,
    arguments: { id, expected_revision: row.revision }, required: ["packet"],
  } : state === "needs_you" ? {
    tool: "car_get", cli: `card wait ${id} --timeout 60`, arguments: { id }, required: [],
  } : state === "answered" ? {
    tool: "car_receive", cli: `card request receive ${id}`, arguments: { id }, required: [],
  } : state === "received" ? {
    tool: "car_ack", cli: `card request ack ${id} --answer ${answerId} --outcome resolved --note 'How the blocker was cleared'`,
    arguments: { id, answer_id: answerId, outcome: "resolved" }, required: ["confirm_actual_blocker_cleared"],
  } : null;
  const authority = packet.why_human ? "human_judgment_gap" : packet.cannot_investigate ? "investigation_limit" : "dependency_or_context";
  const actionDetails = state === "preparing" ? { title: "Add context", summary: "Answer only the missing evidence questions; do not ask the human yet." }
    : state === "needs_you" ? { title: "Wait for the human", summary: "Keep unrelated work moving; a response is not approval until it is received." }
    : state === "answered" ? { title: "Receive the answer", summary: "Persist this exact answer locally, then acknowledge receipt before applying it." }
    : state === "received" ? { title: "Confirm the blocker is gone", summary: "Only report resolved after the source work actually resumed." }
    : { title: "No action", summary: "This request is closed; do not apply a cached answer." };
  return { contract: "car.guidance.v1" as const, code, action,
    action_details: actionDetails,
    authority,
    poll_after_ms: state === "needs_you" || state === "preparing" ? 2_000 : null,
    answer_scope: "this_request_only" as const, standing_permission: false,
    can_apply_answer: state === "received" && Boolean(answerId),
    deadline_at: row.due_at,
    cancel: terminalRequest(state) ? null : { tool: "car_cancel", arguments: { id, expected_revision: row.revision }, required: ["reason"] },
  };
}
