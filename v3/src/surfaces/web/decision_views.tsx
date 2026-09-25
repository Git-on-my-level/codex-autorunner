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
function requestAnswerStatus(delivery: string): string {
  switch (delivery) {
    case "staged": return "Waiting for the agent to pick up your reply.";
    case "acknowledged": return "The agent received your reply; waiting for work to resume.";
    case "resolved": return "The source confirmed the request is unblocked.";
    case "uncertain": return "Delivery is uncertain; check the source before retrying.";
    case "failed": return "Delivery failed; check the source before retrying.";
    default: return `${decisionLabels[delivery] ?? delivery}.`;
  }
}
export interface NativeDecision {
  event_type: string; id: string; incident_id: string; question: string; severity: string; state: string;
  body: string; title: string; source_host: string; created_at: string; obligation_state: string;
  reply_state: string | null; last_error: string | null; snooze_until: string | null; reply_id?: string | null; reply_revision?: number;
  reply_payload_json?: string | null;
}
export function PageHeader({ title, description }: { title: string; description: string }) {
  return <header class="page-header"><div><h1>{title}</h1><p class="page-description">{description}</p></div><a class="button ghost" href="" data-manual-refresh>Refresh</a></header>;
}
export function Timestamp({ value }: { value: string }) {
  const valid = Number.isFinite(Date.parse(value));
  return <time datetime={value} title={value}>{valid ? new Date(value).toISOString().replace("T", " ").replace(/:\d\d\.\d{3}Z$/, " UTC") : value}</time>;
}
function PacketEvidence({ packet }: { packet: DecisionPacket }) {
  return <details class="message-evidence"><summary>Evidence and investigation</summary><div class="details-body stack">
    <div><h3>Goal</h3><p>{packet.goal}</p><h3>Blocker</h3><p>{packet.blocker}</p></div>
    <div><h3>Reported facts</h3><p class="muted">These are source claims, not independent verification by CAR.</p>
      {packet.facts.length ? packet.facts.map((fact) => <p>{fact.statement}<br/><span class="muted">Source: {fact.source ?? "Not supplied"}</span></p>) : <p>No evidence supplied.</p>}</div>
    <div><h3>What the agent tried</h3>{packet.attempts.length ? packet.attempts.map((attempt) => <p>{attempt}</p>) : <p>No investigation supplied.</p>}
      {packet.cannot_investigate && <p><strong>Investigation limit:</strong> {packet.cannot_investigate}</p>}</div>
  </div></details>;
}
export function RequestCard({ view, row, canWrite, detail = false, continueTo }: { view: DecisionView; row: RequestRow; canWrite: boolean; detail?: boolean; continueTo?: string }) {
  const packet = view.packet;
  const missing = assessPacket(packet);
  const reviewer = view.preparation.triage?.proposal as { recommendation?: { answer: string; rationale: string }; uncertainty?: string[] } | null;
  const rec = packet.recommendation ?? reviewer?.recommendation;
  const recSource = packet.recommendation ? "Source agent recommends" : "CAR reviewer suggests · not independently verified";
  const uncertainty = [...packet.uncertainty, ...(reviewer?.uncertainty ?? []).map((item) => `Reviewer: ${item}`)];
  const impact = packet.impact ?? packet.blocker;
  const decisionContext = <>
    <p class="message-intro decision-context">{packet.why_human ?? "The source did not explain which judgment it needs from you."}</p>
    {rec ? <div class="recommendation"><span class="eyebrow">{recSource}</span><p class="recommended-answer">{rec.answer}</p><p>{rec.rationale}</p></div> : <p class="muted">No grounded recommendation supplied.</p>}
    {impact && <p class="decision-impact"><strong>Impact:</strong> {impact}</p>}
    {uncertainty.length > 0 && <div class="decision-uncertainty"><strong>Before deciding</strong>{uncertainty.map((item) => <p>{item}</p>)}</div>}
  </>;
  return <article class="decision-card decision-message" id={row.id} aria-labelledby={`question-${row.id}`}>
    <header class="message-sender"><strong>{packet.project ?? row.client_id}</strong><div class="decision-meta">{statusBadge(row.state, decisionLabels[row.state])}{packet.urgency === "urgent" && row.state !== "expired" && statusBadge("urgent", "Urgent")}</div></header>
    <h2 id={`question-${row.id}`}><a href={`/ui/decisions/${row.id}`}>{packet.question}</a></h2>
    {row.state === "expired" && !row.reviewed_at && canWrite && <section class="expiry-review" aria-label="Review missed decision">
      <form class="answer-form outcome-review" method="post" action={`/ui/decisions/${row.id}/review-expiry`}>
        {continueTo && <input type="hidden" name="continue_to" value={continueTo}/>}
        <input type="hidden" name="expected_revision" value={row.revision}/>
        <fieldset class="reply-choices"><legend class="reply-heading">What next?</legend>
          <p class="expiry-explanation">The deadline passed. Close this request without approving it.</p>
          <label class="reply-choice review-choice"><input type="radio" name="note" value="I will follow up with the source for a fresh decision." required/><span class="reply-choice-copy"><strong>I'll follow up with the source</strong></span></label>
          <label class="reply-choice review-choice"><input type="radio" name="note" value="This decision is no longer needed." required/><span class="reply-choice-copy"><strong>No longer needed</strong></span></label>
        </fieldset>
        <div class="reply-send"><button type="submit" class="button primary">{continueTo ? "Mark reviewed & next" : "Mark reviewed"}</button></div>
      </form>
      <details class="custom-review"><summary>Add a different note</summary><form class="answer-form details-body" method="post" action={`/ui/decisions/${row.id}/review-expiry`}>
        {continueTo && <input type="hidden" name="continue_to" value={continueTo}/>}
        <input type="hidden" name="expected_revision" value={row.revision}/>
        <label for={`review-${row.id}`}>Review note</label><textarea id={`review-${row.id}`} name="note" rows={2} maxlength={2000} required/>
        <button type="submit" class="button primary">{continueTo ? "Mark reviewed & next" : "Mark reviewed"}</button>
      </form></details>
    </section>}
    {view.answer && row.state !== "expired" && <div class="answer-record answer-record-primary"><span class="eyebrow">Your reply</span><p class="preserve-lines">{view.answer.payload.text ?? (view.answer.payload.approval ? "Approved" : "Denied")}</p><p class="muted">{requestAnswerStatus(view.answer.delivery)}</p></div>}
    {view.answer || row.state === "expired" ? <details class="original-context"><summary>{row.state === "expired" ? "Original decision & history" : "Original request context"}</summary><div class="details-body stack">
      {row.state === "expired" && view.answer && <div class="answer-record"><span class="eyebrow">Previous reply · expired</span><p class="preserve-lines">{view.answer.payload.text ?? (view.answer.payload.approval ? "Approved" : "Denied")}</p></div>}
      {decisionContext}
      {row.state === "expired" && <><h3>Original options · no longer available</h3>{packet.options.map(option => <div><strong>{option.label}</strong><p>{option.answer}</p><p class="muted">{option.consequences}</p></div>)}<PacketEvidence packet={packet}/></>}
    </div></details> : decisionContext}
    {row.due_at && row.state !== "expired" && <p><strong>Decision deadline:</strong> <Timestamp value={row.due_at}/></p>}
    {missing.length > 0 && <p class="context-warning">Context incomplete: {missing.map((m) => m.field.replaceAll("_", " ")).join(", ")}.</p>}
    {row.state === "preparing" && <p>Waiting for the source to gather context. This will surface by <Timestamp value={row.prepare_by}/>, even if the agent stops responding.</p>}
    {row.close_reason && row.state !== "expired" && <p><strong>Outcome:</strong> {row.close_reason}</p>}
    {row.reviewed_at && <p><strong>Missed deadline reviewed:</strong> {row.review_note}</p>}
    {row.state !== "expired" && <PacketEvidence packet={packet}/>}
    {row.state === "needs_you" && canWrite && <section class="decision-composer" aria-label="Reply to this decision">
      <form class="answer-form reply-form" method="post" action={`/ui/decisions/${row.id}/answer`}>
        {continueTo && <input type="hidden" name="continue_to" value={continueTo}/>}
        <input type="hidden" name="expected_revision" value={row.revision}/>
        <fieldset class="reply-choices"><legend class="reply-heading">Your reply</legend>
          {packet.options.map((option) => <div class="reply-choice-row">
            <label class="reply-choice">
              <input type="radio" name="option_id" value={option.id} required/>
              <span class="reply-choice-copy"><strong>{option.label}</strong><span class="preserve-lines">{option.answer}</span><span class="muted reply-tradeoff">{option.consequences}</span></span>
            </label>
            <button type="button" class="button ghost customize-answer" data-customize-answer={option.answer} aria-label={`Customize ${option.label}`}>Customize this answer</button>
          </div>)}
          {packet.options.length > 0 && <label class="reply-choice custom-choice"><input type="radio" name="option_id" value="" required/><span class="reply-choice-copy"><strong>Write my own answer</strong><span class="muted">Give a different direction or include specific conditions.</span></span></label>}
          <div class={packet.options.length ? "custom-reply" : "custom-reply always-visible"}>
            <label for={`answer-${row.id}`}>Your answer</label>
            <textarea id={`answer-${row.id}`} name="text" rows={3} maxlength={8000} required={!packet.options.length} placeholder="Tell the agent what to do, including any conditions…"/>
            <span class="visually-hidden" role="status" aria-live="polite" data-customize-status/>
          </div>
        </fieldset>
        <div class="reply-send"><button type="submit" class="button primary" title="Send this reply for this request only">{continueTo ? "Send & next" : "Send reply"}</button></div>
      </form>
    </section>}
    {detail && !terminalRequest(row.state) && canWrite && <details class="withdraw-request"><summary>No longer needed?</summary><div class="details-body stack">
      <p>Withdraw this request when a decision is no longer needed. This stops further use of the reply through CAR, but does not undo work the agent has already performed.</p>
      <form class="answer-form" method="post" action={`/ui/decisions/${row.id}/withdraw`}><input type="hidden" name="expected_revision" value={row.revision}/>
        {continueTo && <input type="hidden" name="continue_to" value={continueTo}/>}
        <label for={`withdraw-${row.id}`}>Why is this request no longer needed?</label><textarea id={`withdraw-${row.id}`} name="reason" rows={2} maxlength={2000} required/>
        <button class="button danger" type="submit">Withdraw request</button>
      </form></div></details>}
    <details class="message-record"><summary>Request details</summary><div class="details-body"><p>Created <Timestamp value={row.created_at}/> · {row.host}</p>{row.state === "expired" && <p>Deadline: <Timestamp value={row.due_at ?? row.updated_at}/> · {row.close_reason}</p>}<p class="muted">Source: {row.client_id} · revision {row.revision} · last API contact <Timestamp value={row.last_seen_at}/>. This is not a liveness guarantee.</p><p>Replies apply to this request only.</p></div></details>
    {!detail && <a class="decision-record-link" href={`/ui/decisions/${row.id}`}>Open decision record</a>}
  </article>;
}
export function NativeCard({ row, canWrite, continueTo }: { row: NativeDecision; canWrite: boolean; continueTo?: string }) {
  // The obligation is the source-of-truth outcome. Delivery is separate
  // evidence: a reply may have been delivered before the source's deadline
  // elapsed, but that does not turn a missed obligation into success.
  const terminalObligation = ["resolved", "cancelled", "expired"].includes(row.obligation_state);
  const state = terminalObligation ? row.obligation_state : row.reply_state ?? row.state;
  let replyText: string | undefined;
  if (row.reply_payload_json) {
    try {
      const payload = JSON.parse(row.reply_payload_json) as { text?: string; approval?: boolean };
      replyText = typeof payload.text === "string" ? payload.text : typeof payload.approval === "boolean" ? (payload.approval ? "Approved" : "Denied") : "Recorded answer unavailable.";
    } catch { replyText = "Recorded answer could not be read."; }
  }
  return <article class="decision-card decision-message">
    <header class="message-sender"><div><strong>{row.source_host}</strong><span>Native integration</span></div><div class="message-sender-side"><div class="decision-meta">{statusBadge(state, decisionLabels[state] ?? state)}</div><Timestamp value={row.created_at}/></div></header>
    <h2>{row.question}</h2><p class="summary-copy preserve-lines">{row.body || "No additional context was supplied by this integration."}</p>
    {replyText && <div class="answer-record"><span class="eyebrow">Your reply</span><p class="preserve-lines">{replyText}</p><p class="muted">{decisionLabels[row.reply_state ?? ""] ?? row.reply_state}.</p></div>}
    {terminalObligation && row.reply_state && <p class="muted">Delivery record: {decisionLabels[row.reply_state] ?? row.reply_state}. This evidence does not change the source obligation outcome.</p>}
    {row.last_error && <p class="context-warning">{row.last_error}</p>}
    {row.snooze_until && <p>Returns to Needs you at <Timestamp value={row.snooze_until}/>.</p>}
    {row.state === "pending" && !terminalObligation && !row.reply_state && row.event_type === "attention.permission" && canWrite && <div class="actions">{[true, false].map((approval) => <form method="post" action={`/ui/escalations/${row.id}/answer`}>{continueTo && <input type="hidden" name="continue_to" value={continueTo}/>}<input type="hidden" name="approval" value={String(approval)}/><button class="button" type="submit">{approval ? "Approve this request" : "Deny this request"}</button></form>)}</div>}
    {row.state === "pending" && !terminalObligation && !row.reply_state && canWrite && <form class="answer-form" method="post" action={`/ui/escalations/${row.id}/answer`}>
      {continueTo && <input type="hidden" name="continue_to" value={continueTo}/>}
      <label for={`native-${row.id}`}>Reply to this request</label><textarea id={`native-${row.id}`} name="text" rows={3} maxlength={8000} required/>
      <button class="button primary" type="submit">{continueTo ? "Send & next" : "Send reply"}</button><small class="muted">Track this reply in Watching. Applies to this request only.</small>
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
