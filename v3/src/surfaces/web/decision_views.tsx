/** Pure presentation: source claims stay text; authority and transitions stay in Core. */
import type { AttentionService } from "../../attention/service.ts";
import type { DecisionPacket, RequestRow } from "../../attention/contract.ts";
import { assessPacket } from "../../attention/quality.ts";
import { terminalRequest } from "../../attention/guidance.ts";
import { statusBadge } from "./layout.tsx";

export type DecisionView = ReturnType<AttentionService["view"]>;
export const decisionLabels: Record<string, string> = {
  preparing: "Gathering context", needs_you: "Decision needed", answered: "Answer recorded · waiting for receipt",
  received: "Received · waiting for work to resume", resolved: "Source confirmed unblocked",
  cancelled: "Withdrawn · not a successful resolution", expired: "Deadline missed · not approved",
  pending: "Decision needed", delivered: "Delivered · awaiting clearance", staged: "Saved · receipt not confirmed",
  uncertain: "Delivery uncertain", failed: "Delivery failed", acknowledged: "Received · awaiting clearance", delivering: "Sending",
};
export interface NativeDecision {
  event_type: string; id: string; incident_id: string; question: string; severity: string; state: string;
  body: string; title: string; source_host: string; created_at: string; obligation_state: string;
  reply_state: string | null; last_error: string | null; snooze_until: string | null; reply_id?: string | null; reply_revision?: number;
}
export function PageHeader({ title, description }: { title: string; description: string }) {
  return <header class="page-header"><div><h1>{title}</h1><p class="page-description">{description}</p></div><a class="button ghost" href="" data-manual-refresh>Refresh</a></header>;
}
export function Timestamp({ value }: { value: string }) {
  const valid = Number.isFinite(Date.parse(value));
  return <time datetime={value} title={value}>{valid ? new Date(value).toISOString().replace("T", " ").replace(/:\d\d\.\d{3}Z$/, " UTC") : value}</time>;
}
function PacketEvidence({ packet }: { packet: DecisionPacket }) {
  return <details><summary>Evidence and investigation</summary><div class="details-body stack">
    <div><h3>Goal</h3><p>{packet.goal}</p><h3>Blocker</h3><p>{packet.blocker}</p></div>
    <div><h3>Reported facts</h3><p class="muted">These are source claims, not independent verification by CAR.</p>
      {packet.facts.length ? packet.facts.map((fact) => <p>{fact.statement}<br/><span class="muted">Source: {fact.source ?? "Not supplied"}</span></p>) : <p>No evidence supplied.</p>}</div>
    <div><h3>What the agent tried</h3>{packet.attempts.length ? packet.attempts.map((attempt) => <p>{attempt}</p>) : <p>No investigation supplied.</p>}
      {packet.cannot_investigate && <p><strong>Investigation limit:</strong> {packet.cannot_investigate}</p>}</div>
  </div></details>;
}
export function RequestCard({ view, row, canWrite, detail = false }: { view: DecisionView; row: RequestRow; canWrite: boolean; detail?: boolean }) {
  const packet = view.packet;
  const missing = assessPacket(packet);
  const reviewer = view.preparation.triage?.proposal as { recommendation?: { answer: string; rationale: string }; uncertainty?: string[] } | null;
  const rec = packet.recommendation ?? reviewer?.recommendation;
  const recSource = packet.recommendation ? "Source agent recommends" : "CAR reviewer suggests · not independently verified";
  const uncertainty = [...packet.uncertainty, ...(reviewer?.uncertainty ?? []).map((item) => `Reviewer: ${item}`)];
  return <article class="decision-card" id={row.id} aria-labelledby={`question-${row.id}`}>
    <div class="decision-meta">{statusBadge(row.state, decisionLabels[row.state])}<span>{packet.project ?? row.client_id} · {row.host}</span>{packet.urgency === "urgent" && statusBadge("urgent", "Urgent")}</div>
    <h2 id={`question-${row.id}`}><a href={`/ui/decisions/${row.id}`}>{packet.question}</a></h2>
    <p><strong>Why you:</strong> {packet.why_human ?? "The source did not explain which judgment it needs from you."}</p>
    {rec ? <div class="recommendation"><span class="eyebrow">{recSource}</span><p class="recommended-answer">{rec.answer}</p><p>{rec.rationale}</p></div> : <p class="muted">No grounded recommendation supplied. This is not an implied recommendation to approve.</p>}
    <p><strong>Unblocks:</strong> {packet.impact ?? packet.blocker}</p>
    {uncertainty.length > 0 && <div class="decision-uncertainty"><strong>Before deciding</strong>{uncertainty.map((item) => <p>{item}</p>)}</div>}
    {row.due_at && <p><strong>{row.state === "expired" ? "Deadline was:" : "Decision deadline:"}</strong> <Timestamp value={row.due_at}/></p>}
    {missing.length > 0 && <p class="context-warning">Context incomplete: {missing.map((m) => m.field.replaceAll("_", " ")).join(", ")}. CAR will not hide the blocker while waiting for a better report.</p>}
    {row.state === "preparing" && <p>Waiting for the source to gather context. This will surface by <Timestamp value={row.prepare_by}/>, even if the agent stops responding.</p>}
    {view.answer && <div class="answer-record"><strong>Recorded answer</strong><p class="preserve-lines">{view.answer.payload.text ?? (view.answer.payload.approval ? "Approved" : "Denied")}</p><p class="muted">{decisionLabels[view.answer.delivery] ?? view.answer.delivery}. Receipt and actual unblocking are separate confirmations.</p></div>}
    {row.close_reason && <p><strong>Outcome:</strong> {row.close_reason}</p>}
    {row.reviewed_at && <p><strong>Missed deadline reviewed:</strong> {row.review_note}</p>}
    <PacketEvidence packet={packet}/>
    {row.state === "needs_you" && canWrite && <details class="decision-composer" open={detail}>
      <summary>Choose an answer or give a different instruction</summary><div class="details-body stack">
      <p class="muted">Every answer is scoped to this request. Neither a recommendation nor your response creates standing permission.</p>
      <div class="decision-options">{packet.options.map((option) => <form class="option-card" method="post" action={`/ui/decisions/${row.id}/answer`}>
        <input type="hidden" name="expected_revision" value={row.revision}/><input type="hidden" name="option_id" value={option.id}/>
        <h3>{option.label}</h3><p class="option-answer preserve-lines">{option.answer}</p>
        <p class="muted"><strong>Consequence:</strong> {option.consequences}</p>
        <button type="submit" class="button">Choose: {option.label}</button>
      </form>)}</div>
      <form class="answer-form" method="post" action={`/ui/decisions/${row.id}/answer`}>
        <input type="hidden" name="expected_revision" value={row.revision}/>
        <label for={`answer-${row.id}`}>Your answer or a different instruction</label>
        <textarea id={`answer-${row.id}`} name="text" rows={3} maxlength={8000} required placeholder="State the decision and any constraints for this request."/>
        <button type="submit" class="button primary">Record decision</button>
        <small class="muted">CAR records this first, then waits for the agent to confirm receipt and resolution.</small>
      </form></div>
    </details>}
    {row.state === "expired" && !row.reviewed_at && canWrite && <form class="answer-form outcome-review" method="post" action={`/ui/decisions/${row.id}/review-expiry`}>
      <input type="hidden" name="expected_revision" value={row.revision}/>
      <label for={`review-${row.id}`}>This decision was missed. What should be recorded?</label>
      <textarea id={`review-${row.id}`} name="note" rows={2} maxlength={2000} required placeholder="For example: asked the source for a fresh decision, or this work is no longer needed."/>
      <button type="submit" class="button">Acknowledge missed decision</button><small class="muted">Moves to history as expired, never as approved or resolved.</small>
    </form>}
    {detail && !terminalRequest(row.state) && canWrite && <details><summary>Withdraw an obsolete request</summary><div class="details-body stack">
      <p>This prevents further use of this answer through CAR. It cannot undo work the agent has already performed. Check the source when necessary.</p>
      <form class="answer-form" method="post" action={`/ui/decisions/${row.id}/withdraw`}><input type="hidden" name="expected_revision" value={row.revision}/>
        <label for={`withdraw-${row.id}`}>Why is this request no longer needed?</label><textarea id={`withdraw-${row.id}`} name="reason" rows={2} maxlength={2000} required/>
        <button class="button danger" type="submit">Withdraw request</button>
      </form></div></details>}
    <p class="muted"><small>Source: {row.client_id} · revision {row.revision} · last API contact <Timestamp value={row.last_seen_at}/>. This is not a liveness guarantee.</small></p>
    {!detail && <a class="decision-record-link" href={`/ui/decisions/${row.id}`}>Open decision record</a>}
  </article>;
}
export function NativeCard({ row, canWrite }: { row: NativeDecision; canWrite: boolean }) {
  // The obligation is the source-of-truth outcome. Delivery is separate
  // evidence: a reply may have been delivered before the source's deadline
  // elapsed, but that does not turn a missed obligation into success.
  const terminalObligation = ["resolved", "cancelled", "expired"].includes(row.obligation_state);
  const state = terminalObligation ? row.obligation_state : row.reply_state ?? row.state;
  return <article class="decision-card">
    <div class="decision-meta">{statusBadge(state, decisionLabels[state] ?? state)}<span>{row.source_host} · native integration</span></div>
    <h2>{row.question}</h2><p class="summary-copy preserve-lines">{row.body || "No additional context was supplied by this integration."}</p>
    {terminalObligation && row.reply_state && <p class="muted">Delivery record: {decisionLabels[row.reply_state] ?? row.reply_state}. This evidence does not change the source obligation outcome.</p>}
    {row.last_error && <p class="context-warning">{row.last_error}</p>}
    {row.snooze_until && <p>Returns to Needs you at <Timestamp value={row.snooze_until}/>.</p>}
    {row.state === "pending" && !terminalObligation && !row.reply_state && row.event_type === "attention.permission" && canWrite && <div class="actions">{[true, false].map((approval) => <form method="post" action={`/ui/escalations/${row.id}/answer`}><input type="hidden" name="approval" value={String(approval)}/><button class="button" type="submit">{approval ? "Approve this request" : "Deny this request"}</button></form>)}</div>}
    {row.state === "pending" && !terminalObligation && !row.reply_state && canWrite && <form class="answer-form" method="post" action={`/ui/escalations/${row.id}/answer`}>
      <label for={`native-${row.id}`}>Reply to this request</label><textarea id={`native-${row.id}`} name="text" rows={3} maxlength={8000} required/>
      <button class="button primary" type="submit">Record answer</button><small class="muted">Delivery is tracked separately. This creates no standing permission.</small>
    </form>}
    {row.reply_id && !terminalObligation && ["failed","uncertain","staged","delivered"].includes(row.reply_state ?? "") && canWrite && <details><summary>Check delivery at the source</summary><div class="details-body stack">
      <p>Do not resend an uncertain answer without checking whether it arrived.</p>
      <form class="answer-form" method="post" action={`/ui/replies/${row.reply_id}/reconcile`}>
        <input type="hidden" name="expected_revision" value={row.reply_revision}/>
        <label for={`reconcile-${row.reply_id}`}>What did you verify at the source?</label><textarea id={`reconcile-${row.reply_id}`} name="note" rows={2} maxlength={2000} required/>
        <div class="actions"><button class="button" name="outcome" value="source_confirmed">Source is unblocked</button><button class="button" name="outcome" value="not_received_retry">Confirmed not received: retry</button><button class="button danger" name="outcome" value="cancel">Cancel reply</button></div>
      </form></div></details>}
    <a href={`/ui/incidents/${row.incident_id}`}>Inspect source record</a>
  </article>;
}

/** A failed POST must not turn the user's draft into an unlabelled success or discard it. */
export function DecisionError({ message, draft, href }: { message: string; draft?: string; href: string }) {
  return <section class="decision-card" aria-labelledby="decision-error"><h1 id="decision-error">Action not confirmed</h1>
    <p role="alert">{message}</p><p>Check the current decision before trying again. A timeout or an error is not approval, and the source may already have received an earlier answer.</p>
    {draft && <div class="answer-record"><h2>Your submitted text</h2><p class="muted">Retained here for copying. This is not confirmation that CAR recorded it.</p><pre class="preserve-lines">{draft}</pre></div>}
    <a class="button" href={href}>Check current decision</a></section>;
}
