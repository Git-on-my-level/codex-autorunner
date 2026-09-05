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
  return <article class="decision-card decision-message" id={row.id} aria-labelledby={`question-${row.id}`}>
    <header class="message-sender"><div><strong>{packet.project ?? row.client_id}</strong><span>{row.client_id} · {row.host}</span></div><Timestamp value={row.created_at}/></header>
    <div class="decision-meta">{statusBadge(row.state, decisionLabels[row.state])}{packet.urgency === "urgent" && statusBadge("urgent", "Urgent")}</div>
    <h2 id={`question-${row.id}`}><a href={`/ui/decisions/${row.id}`}>{packet.question}</a></h2>
    <p class="message-intro">{packet.why_human ?? "The source did not explain which judgment it needs from you."}</p>
    {rec ? <div class="recommendation"><span class="eyebrow">{recSource}</span><p class="recommended-answer">{rec.answer}</p><p>{rec.rationale}</p></div> : <p class="muted">No grounded recommendation supplied.</p>}
    <p><strong>Impact:</strong> {packet.impact ?? packet.blocker}</p>
    {uncertainty.length > 0 && <div class="decision-uncertainty"><strong>Before deciding</strong>{uncertainty.map((item) => <p>{item}</p>)}</div>}
    {row.due_at && <p><strong>{row.state === "expired" ? "Deadline was:" : "Decision deadline:"}</strong> <Timestamp value={row.due_at}/></p>}
    {missing.length > 0 && <p class="context-warning">Context incomplete: {missing.map((m) => m.field.replaceAll("_", " ")).join(", ")}.</p>}
    {row.state === "preparing" && <p>Waiting for the source to gather context. This will surface by <Timestamp value={row.prepare_by}/>, even if the agent stops responding.</p>}
    {view.answer && <div class="answer-record"><span class="eyebrow">Your reply</span><p class="preserve-lines">{view.answer.payload.text ?? (view.answer.payload.approval ? "Approved" : "Denied")}</p><p class="muted">{decisionLabels[view.answer.delivery] ?? view.answer.delivery}.</p></div>}
    {row.close_reason && <p><strong>Outcome:</strong> {row.close_reason}</p>}
    {row.reviewed_at && <p><strong>Missed deadline reviewed:</strong> {row.review_note}</p>}
    <PacketEvidence packet={packet}/>
    {row.state === "needs_you" && canWrite && <section class="decision-composer" aria-label="Reply to this decision">
      <form class="answer-form reply-form" method="post" action={`/ui/decisions/${row.id}/answer`}>
        {continueTo && <input type="hidden" name="continue_to" value={continueTo}/>}
        <input type="hidden" name="expected_revision" value={row.revision}/>
        <fieldset class="reply-choices"><legend class="reply-heading">Your reply</legend>
          <p class="reply-hint muted">{packet.options.length ? "Choose an answer below, or write your own. Nothing is sent until you reply." : "Tell the agent how you’d like to proceed."}</p>
          {packet.options.map((option) => <label class="reply-choice">
            <input type="radio" name="option_id" value={option.id} required/>
            <span class="reply-choice-copy"><strong>{option.label}</strong><span class="preserve-lines">{option.answer}</span><span class="muted reply-tradeoff">{option.consequences}</span></span>
          </label>)}
          {packet.options.length > 0 && <label class="reply-choice custom-choice"><input type="radio" name="option_id" value="" required/><span class="reply-choice-copy"><strong>Write my own answer</strong><span class="muted">Give a different direction or include specific conditions.</span></span></label>}
          <div class={packet.options.length ? "custom-reply" : "custom-reply always-visible"}>
            <label for={`answer-${row.id}`}>Your answer</label>
            <textarea id={`answer-${row.id}`} name="text" rows={3} maxlength={8000} required={!packet.options.length} placeholder="Tell the agent what to do, including any conditions…"/>
          </div>
        </fieldset>
        <div class="reply-send"><button type="submit" class="button primary">{continueTo ? "Send & next" : "Send reply"} <span aria-hidden="true">↗</span></button><p class="muted">{continueTo ? "Continue triaging. Track this reply in Watching." : "Moves to Watching while the agent picks up your reply."}</p></div>
        <small class="muted reply-scope">Applies to this request only.</small>
      </form>
    </section>}
    {row.state === "expired" && !row.reviewed_at && canWrite && <form class="answer-form outcome-review" method="post" action={`/ui/decisions/${row.id}/review-expiry`}>
      {continueTo && <input type="hidden" name="continue_to" value={continueTo}/>}
      <input type="hidden" name="expected_revision" value={row.revision}/>
      <label for={`review-${row.id}`}>This decision was missed. What should be recorded?</label>
      <textarea id={`review-${row.id}`} name="note" rows={2} maxlength={2000} required placeholder="For example: asked the source for a fresh decision, or this work is no longer needed."/>
      <button type="submit" class="button">Acknowledge missed decision</button><small class="muted">Moves to history as expired, never as approved or resolved.</small>
    </form>}
    {detail && !terminalRequest(row.state) && canWrite && <details class="withdraw-request"><summary>No longer needed?</summary><div class="details-body stack">
      <p>Withdraw this request when a decision is no longer needed. This stops further use of the reply through CAR, but does not undo work the agent has already performed.</p>
      <form class="answer-form" method="post" action={`/ui/decisions/${row.id}/withdraw`}><input type="hidden" name="expected_revision" value={row.revision}/>
        {continueTo && <input type="hidden" name="continue_to" value={continueTo}/>}
        <label for={`withdraw-${row.id}`}>Why is this request no longer needed?</label><textarea id={`withdraw-${row.id}`} name="reason" rows={2} maxlength={2000} required/>
        <button class="button danger" type="submit">Withdraw request</button>
      </form></div></details>}
    <details class="message-record"><summary>Request details</summary><div class="details-body"><p class="muted"><small>Source: {row.client_id} · revision {row.revision} · last API contact <Timestamp value={row.last_seen_at}/>. This is not a liveness guarantee.</small></p></div></details>
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
    <header class="message-sender"><div><strong>{row.source_host}</strong><span>Native integration</span></div><Timestamp value={row.created_at}/></header>
    <div class="decision-meta">{statusBadge(state, decisionLabels[state] ?? state)}</div>
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
