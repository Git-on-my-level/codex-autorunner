/** Human-facing decisions. No model settings, host topology or queue mechanics on the home page. */
import { Hono, type Context } from "hono";
import type { AttentionService } from "../../attention/service.ts";
import type { RequestRow } from "../../attention/contract.ts";
import { HumanAnswer } from "../../attention/contract.ts";
import { recordAnswer, reconcileReply, type ReplyRow } from "../../attention/replies.ts";
import { AttentionError } from "../../attention/errors.ts";
import { RequestCard, NativeCard, PageHeader, decisionLabels as label, type NativeDecision } from "./decision_views.tsx";
import { Mailbox, type MailboxItem } from "./mailbox.tsx";
import { hasWebWriteSession } from "./auth.ts";
import { Layout } from "./layout.tsx";

import { decisionCounts, REQUEST_CONDITION, NATIVE_CONDITION, NATIVE_JOIN, type DecisionTab as Tab } from "./decision_queries.ts";
const tabNames = { needs_you: "Needs you", watching: "Watching", handled: "Handled" };
const tabPaths: Record<Tab, string> = { needs_you: "/ui", watching: "/ui/watching", handled: "/ui/handled" };

type Selection = { kind: "request" | "native" | "delivery"; id: string };
type MailboxEntry = { key: string; item: MailboxItem; value: RequestRow | NativeDecision | ReplyRow; sortTime: string };
const mailboxStateLabels: Record<string, string> = {
  needs_you: "", pending: "", open: "", preparing: "Gathering context", answered: "Awaiting receipt",
  received: "Awaiting outcome", resolved: "Unblocked", expired: "Missed deadline", cancelled: "Withdrawn",
  failed: "Delivery failed", uncertain: "Delivery uncertain", staged: "Saved · receipt not confirmed",
  delivered: "Awaiting clearance", acknowledged: "Awaiting clearance", delivering: "Sending",
};

function tabHref(tab: Tab): string { return tabPaths[tab]; }
function tabForRequest(row: RequestRow): Tab {
  if (row.state === "needs_you" || (row.state === "expired" && !row.reviewed_at)) return "needs_you";
  if (row.state === "preparing" || row.state === "answered" || row.state === "received") return "watching";
  return "handled";
}
function selectionFromQuery(raw: string | undefined): Selection | null {
  if (!raw) return null;
  const separator = raw.indexOf(":");
  if (separator > 0) {
    const kind = raw.slice(0, separator);
    const id = raw.slice(separator + 1);
    if ((kind === "native" || kind === "delivery") && id) return { kind, id };
  }
  return { kind: "request", id: raw };
}
function selectionKey(selection: Selection): string { return `${selection.kind}:${selection.id}`; }
function displayTime(value: string): string {
  return Number.isFinite(Date.parse(value))
    ? new Date(value).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" })
    : value;
}
function packetSummary(row: RequestRow): { project: string; question: string; preview: string; urgency: string } {
  try {
    const packet = JSON.parse(row.packet_json) as { project?: string; question?: string; blocker?: string; goal?: string; urgency?: string };
    return {
      project: packet.project ?? row.client_id,
      question: packet.question ?? "Decision request",
      preview: packet.blocker ?? packet.goal ?? "No additional context supplied.",
      urgency: packet.urgency ?? "normal",
    };
  } catch {
    return { project: row.client_id, question: "Decision request", preview: "The request packet could not be read.", urgency: "normal" };
  }
}
function deliverySummary(row: ReplyRow): string {
  try {
    const payload = JSON.parse(row.payload_json) as Record<string, unknown>;
    if (typeof payload.text === "string") return payload.text;
    if (payload.approval === true) return "Approved";
    if (payload.approval === false) return "Denied";
    return "Permission response unavailable.";
  } catch {
    return "Reply payload unavailable.";
  }
}

function nativeState(row: NativeDecision): string {
  const terminal = ["resolved", "cancelled", "expired"].includes(row.obligation_state);
  return terminal ? row.obligation_state : row.reply_state ?? row.state;
}
function nativeUrgent(row: NativeDecision): boolean {
  return row.severity === "urgent" || (row as NativeDecision & { event_severity?: string }).event_severity === "urgent";
}

function selectionHref(tab: Tab, key: string, page: number): string {
  const query = `selected=${encodeURIComponent(key)}`;
  return `${tabHref(tab)}?${query}${page > 0 ? `&page=${page}` : ""}`;
}

function DeliveryReader({ row, canWrite }: { row: ReplyRow; canWrite: boolean }) {
  return <article class="decision-card decision-message">
    <div class="message-sender"><div><strong>CAR reply delivery</strong><span>{label[row.state] ?? row.state}</span></div><time datetime={row.updated_at} data-format="compact">{displayTime(row.updated_at)}</time></div>
    <h2>Check whether this reply reached the source</h2>
    <p class="message-intro preserve-lines">{deliverySummary(row)}</p>
    <p>{row.last_error ?? "Delivery is not proof that the source is unblocked."}</p>
    <p class="muted">{row.id}. Reconcile only after checking the native source. Do not retry an uncertain send speculatively.</p>
    {canWrite && <form class="answer-form" method="post" action={`/ui/replies/${row.id}/reconcile`}>
      <input type="hidden" name="expected_revision" value={row.revision}/>
      <label for={`reconcile-${row.id}`}>What did you verify at the source?</label>
      <textarea id={`reconcile-${row.id}`} name="note" rows={2} maxlength={2000} required/>
      <div class="actions"><button class="button" name="outcome" value="source_confirmed">Source is unblocked</button><button class="button" name="outcome" value="not_received_retry">Confirmed not received: retry</button><button class="button danger" name="outcome" value="cancel">Cancel reply</button></div>
    </form>}
  </article>;
}
export function installDecisionRoutes(app: Hono, service: AttentionService): void {
  const store = service.store;
  const workspace = service.config.attention.workspace_id;
  const counts = () => decisionCounts(store, workspace);
  const waitingWithoutContact = () => (store.db.query(`SELECT COUNT(*) AS n FROM attention_requests WHERE workspace_id=?
    AND state IN ('answered','received') AND last_seen_at < ?`).get(workspace,
      new Date(store.clock.now().getTime() - 15 * 60_000).toISOString()) as { n: number }).n;
  const renderMailbox = (tab: Tab, c: Context, forcedSelection: Selection | null = null, forcedRequest?: RequestRow) => {
    service.sweep();
    // Missed decisions stay visible until a human acknowledges the outcome.
    const condition = REQUEST_CONDITION[tab];
    const requestedPage = Number(c.req.query("page") ?? "0");
    const page = Number.isSafeInteger(requestedPage) && requestedPage >= 0 ? requestedPage : 0;
    const rows = store.db.query(`SELECT * FROM attention_requests WHERE workspace_id=? AND ${condition}
      ORDER BY CASE json_extract(packet_json,'$.urgency') WHEN 'urgent' THEN 0 ELSE 1 END,
      CASE WHEN ?='handled' THEN -strftime('%s',COALESCE(closed_at,created_at)) ELSE strftime('%s',COALESCE(due_at,created_at)) END, id
      LIMIT 51 OFFSET ?`).all(workspace, tab, page * 50) as RequestRow[];
    const selection = forcedSelection ?? selectionFromQuery(c.req.query("selected"));
    const nativeCondition = NATIVE_CONDITION[tab];
    const native = store.db.query(`SELECT s.*, e.type AS event_type, e.severity AS event_severity, e.body, e.title, e.source_host, e.received_at AS created_at, e.obligation_state,
        h.state AS reply_state, h.payload_json AS reply_payload_json, h.last_error, h.id AS reply_id, h.revision AS reply_revision, i.snooze_until
      ${NATIVE_JOIN} AND (${nativeCondition})
      ORDER BY CASE e.severity WHEN 'urgent' THEN 0 ELSE 1 END, e.received_at DESC LIMIT 51 OFFSET ?`).all(page * 50) as NativeDecision[];
    const deliveries = tab === "watching" ? store.db.query("SELECT * FROM human_replies WHERE request_id IS NULL AND escalation_id IS NULL AND state IN ('failed','uncertain','staged','delivered') ORDER BY updated_at DESC LIMIT 51 OFFSET ?").all(page * 50) as ReplyRow[] : [];
    const canWrite = hasWebWriteSession(c, service.config);
    const attentionErrors = (store.db.query("SELECT COUNT(*) AS n FROM human_replies WHERE state IN ('failed','uncertain')").get() as { n: number }).n;
    const navCounts = counts();
    const selectedNative = selection?.kind === "native"
      ? store.db.query(`SELECT s.*, e.type AS event_type, e.severity AS event_severity, e.body, e.title, e.source_host, e.received_at AS created_at, e.obligation_state,
          h.state AS reply_state, h.payload_json AS reply_payload_json, h.last_error, h.id AS reply_id, h.revision AS reply_revision, i.snooze_until
        ${NATIVE_JOIN} AND (${nativeCondition}) AND s.id=?`).get(selection.id) as NativeDecision | null
      : null;
    const selectedDelivery = tab === "watching" && selection?.kind === "delivery"
      ? store.db.query("SELECT * FROM human_replies WHERE id=? AND request_id IS NULL AND escalation_id IS NULL AND state IN ('failed','uncertain','staged','delivered')").get(selection.id) as ReplyRow | null
      : null;
    const requestRows = forcedRequest && !rows.slice(0, 50).some((row) => row.id === forcedRequest.id) ? [...rows.slice(0, 50), forcedRequest] : rows.slice(0, 50);
    const nativeRows = selectedNative && !native.slice(0, 50).some((row) => row.id === selectedNative.id) ? [...native.slice(0, 50), selectedNative] : native.slice(0, 50);
    const deliveryRows = selectedDelivery && !deliveries.slice(0, 50).some((row) => row.id === selectedDelivery.id) ? [...deliveries.slice(0, 50), selectedDelivery] : deliveries.slice(0, 50);
    const entries: MailboxEntry[] = [
      ...requestRows.map((row): MailboxEntry => {
        const summary = packetSummary(row);
        return { key: `request:${row.id}`, value: row, item: {
          id: `request:${row.id}`, href: `/ui/decisions/${encodeURIComponent(row.id)}${page > 0 ? `?page=${page}` : ""}`, source: summary.project,
          time: displayTime(row.updated_at), datetime: row.updated_at, subject: summary.question, preview: summary.preview,
          state: mailboxStateLabels[row.state] ?? row.state, urgency: summary.urgency as "normal" | "urgent",
        }, sortTime: tab === "handled" ? row.closed_at ?? row.created_at : row.due_at ?? row.created_at };
      }),
      ...nativeRows.map((row): MailboxEntry => ({
        key: `native:${row.id}`, value: row, item: {
          id: `native:${row.id}`, href: selectionHref(tab, `native:${row.id}`, page),
          source: `${row.source_host} · native`, time: displayTime(row.created_at), datetime: row.created_at, subject: row.question || row.title || "Native request",
          preview: row.body || "No additional context was supplied by this integration.",
          state: mailboxStateLabels[nativeState(row)] ?? nativeState(row), urgency: nativeUrgent(row) ? "urgent" : "normal",
        }, sortTime: row.created_at,
      })),
      ...deliveryRows.map((row): MailboxEntry => ({
        key: `delivery:${row.id}`, value: row, item: {
          id: `delivery:${row.id}`, href: selectionHref(tab, `delivery:${row.id}`, page),
          source: "CAR reply", time: displayTime(row.updated_at), datetime: row.updated_at, subject: `Reply delivery · ${mailboxStateLabels[row.state] ?? row.state}`,
          preview: deliverySummary(row), state: mailboxStateLabels[row.state] ?? row.state,
        }, sortTime: row.updated_at,
      })),
    ];
    entries.sort((a, b) => {
      const urgency = Number(b.item.urgency === "urgent") - Number(a.item.urgency === "urgent");
      if (urgency) return urgency;
      const direction = tab === "handled" ? -1 : 1;
      return direction * (Date.parse(a.sortTime) - Date.parse(b.sortTime)) || a.key.localeCompare(b.key);
    });
    const readerEntry = selection
      ? entries.find((entry) => entry.key === selectionKey(selection))
      : entries[0];
    const readerValue = forcedRequest ?? (readerEntry?.value as RequestRow | undefined);
    const reader = selection?.kind === "request" && readerValue && "packet_json" in readerValue
      ? <RequestCard view={service.view(readerValue)} row={readerValue} canWrite={canWrite} detail/>
      : readerEntry?.value && selection?.kind === "native"
        ? <NativeCard row={readerEntry.value as NativeDecision} canWrite={canWrite}/>
        : readerEntry?.value && selection?.kind === "delivery"
          ? <DeliveryReader row={readerEntry.value as ReplyRow} canWrite={canWrite}/>
          : !selection && readerEntry?.value && "packet_json" in readerEntry.value
            ? <RequestCard view={service.view(readerEntry.value as RequestRow)} row={readerEntry.value as RequestRow} canWrite={canWrite} detail/>
            : !selection && readerEntry?.value && "event_type" in readerEntry.value
              ? <NativeCard row={readerEntry.value as NativeDecision} canWrite={canWrite}/>
              : !selection && readerEntry?.value
              ? <DeliveryReader row={readerEntry.value as ReplyRow} canWrite={canWrite}/>
              : null;
    const recovery = selection && !reader ? <div class="mailbox-empty" role="status">
      This item is no longer in this mailbox. <a href={tabHref(tab)}>Return to {tabNames[tab].toLowerCase()}</a>.
    </div> : null;
    const explicitKey = selection ? selectionKey(selection) : null;
    const mailboxItems = entries.map((entry, index) => ({ ...entry.item, selected: explicitKey === entry.key || (!selection && index === 0) }));
    const recordedRow = selection?.kind === "request" && readerValue && "state" in readerValue ? readerValue as RequestRow : undefined;
    const recordedNotice = c.req.query("recorded") ? <p class="notice" role="status">
      {recordedRow
        ? (recordedRow.state === "resolved" ? "Source confirmed this request is unblocked." : recordedRow.state === "received" ? "Source received the answer. Waiting for work to resume." : recordedRow.state === "answered" ? "Answer recorded. Waiting for the source to receive it." : "Answer recorded. CAR is still tracking this request.")
        : selection?.kind === "native" ? "Answer recorded. Delivery and source outcome remain separate and are shown below."
          : selection?.kind === "delivery" ? "Delivery record updated. Check the source before treating this as resolved."
            : "Action recorded. CAR is still tracking this item."} {navCounts.needs_you > 0 ? <a href="/ui">Next decision</a> : <a href={tabHref(tab)}>Back to inbox</a>}.
    </p> : null;
    const waiting = tab === "needs_you" ? waitingWithoutContact() : 0;
    return c.html(<Layout mailbox title={tabNames[tab]} active={tabHref(tab)} refreshSeconds={15} navCounts={navCounts}>
      <Mailbox title={tabNames[tab]} refreshHref={page > 0 ? `?page=${page}` : ""} items={mailboxItems} explicitSelection={Boolean(selection)}
        backHref={`${tabHref(tab)}${page > 0 ? `?page=${page}` : ""}`} backLabel={`Back to ${tabNames[tab]}`} reader={(reader || recovery) && <>{recordedNotice}{reader ?? recovery}</>}
        beforeList={<>{attentionErrors > 0 && <p class="context-warning"><a href="/ui/watching">{attentionErrors} reply delivery issue(s) need review.</a> CAR has not assumed they succeeded.</p>}{waiting > 0 && <p class="notice"><a href="/ui/watching">{waiting} answered request(s) have no recent source check-in.</a> They remain in Watching, not marked successful.</p>}</>}
        afterList={<nav class="pager actions" aria-label="Decision pages">{page > 0 && <a class="button" href={`?page=${page - 1}`}>Previous</a>}{(rows.length > 50 || native.length > 50 || deliveries.length > 50) && <a class="button" href={`?page=${page + 1}`}>Next</a>}</nav>}
        emptyMessage={tab === "needs_you" ? "No decisions waiting here." : "Nothing in this view."}/>
    </Layout>);
  };
  const show = (tab: Tab) => (c: Context) => renderMailbox(tab, c);
  app.get("/", show("needs_you")); app.get("/watching", show("watching")); app.get("/handled", show("handled"));
  app.get("/decisions/:id", (c) => {
    service.sweep();
    const row = service.get(c.req.param("id"));
    if (!row) return c.text("Decision not found", 404);
    return renderMailbox(tabForRequest(row), c, { kind: "request", id: row.id }, row);
  });
  app.post("/decisions/:id/answer", async (c) => {
    const body = await c.req.parseBody();
    const input = HumanAnswer.parse({ expected_revision: Number(body.expected_revision),
      ...(typeof body.option_id === "string" && body.option_id ? { option_id: body.option_id } : { text: body.text }) });
    service.answer(c.req.param("id"), input.expected_revision, "human:web", input);
    return c.redirect(`/ui/decisions/${encodeURIComponent(c.req.param("id"))}?recorded=1`, 303);
  });
  app.post("/decisions/:id/withdraw", async (c) => {
    const body = await c.req.parseBody();
    service.withdraw(c.req.param("id"), Number(body.expected_revision), String(body.reason ?? ""), "human:web");
    return c.redirect(`/ui/decisions/${encodeURIComponent(c.req.param("id"))}`, 303);
  });
  app.post("/decisions/:id/review-expiry", async (c) => {
    const body = await c.req.parseBody();
    service.reviewExpiry(c.req.param("id"), Number(body.expected_revision), "human:web", String(body.note ?? ""));
    return c.redirect(`/ui/decisions/${encodeURIComponent(c.req.param("id"))}`, 303);
  });
  app.post("/escalations/:id/answer", async (c) => {
    const body = await c.req.parseBody();
    const text = typeof body.text === "string" ? body.text.trim() : "";
    // Guided cards require an immutable packet revision; the native endpoint
    // cannot be used to bypass their optimistic concurrency check.
    if (store.db.query("SELECT id FROM attention_requests WHERE escalation_id=?").get(c.req.param("id"))) throw new AttentionError("revision_required", "Answer the current decision card");
    const approval = body.approval === "true" ? true : body.approval === "false" ? false : undefined;
    if (approval !== undefined) {
      const event = store.db.query("SELECT e.type FROM escalations s JOIN incidents i ON i.id=s.incident_id JOIN events e ON e.id=COALESCE(s.origin_event_id,i.opened_by_event) WHERE s.id=?").get(c.req.param("id")) as { type: string } | null;
      if (event?.type !== "attention.permission") throw new AttentionError("answer_type", "This question requires a text answer", 400);
    }
    recordAnswer(store, { escalationId: c.req.param("id"), actor: "human:web", payload: approval === undefined ? { text } : { approval } });
    return c.redirect(`/ui/watching?selected=${encodeURIComponent(`native:${c.req.param("id")}`)}&recorded=1`, 303);
  });
  app.post("/replies/:id/reconcile", async (c) => {
    const body = await c.req.parseBody();
    if (!["source_confirmed", "not_received_retry", "cancel"].includes(String(body.outcome))) throw new AttentionError("invalid_outcome", "Choose a reconciliation outcome", 400);
    const reply = reconcileReply(store, { id: c.req.param("id"), expectedRevision: Number(body.expected_revision), actor: "human:web",
      outcome: body.outcome as "source_confirmed" | "not_received_retry" | "cancel", note: String(body.note ?? "") });
    if (reply.escalation_id) {
      const destination = reply.state === "resolved" || reply.state === "cancelled" ? "/ui/handled" : "/ui/watching";
      return c.redirect(`${destination}?selected=${encodeURIComponent(`native:${reply.escalation_id}`)}`, 303);
    }
    return c.redirect(`/ui/watching?selected=${encodeURIComponent(`delivery:${c.req.param("id")}`)}&recorded=1`, 303);
  });
  app.get("/settings", (c) => {
    const grants = store.db.query("SELECT id,effect_type,scope_json,expires_at,uses_remaining FROM grants WHERE status='active' ORDER BY created_at DESC LIMIT 100").all() as { id: string; effect_type: string; scope_json: string; expires_at: string | null; uses_remaining: number | null }[];
    const clients = Object.entries(service.config.attention.clients);
    return c.html(<Layout title="Settings" active="/ui/settings"><PageHeader title="Settings" description="Your workspace, connected sources and explicit permissions."/>
      <section class="decision-card"><h2>Workspace: {workspace}</h2><p>{service.config.attention.triage_enabled ? "An optional CAR reviewer helps prepare incomplete decisions. It cannot answer or approve them." : "Deterministic routing. No additional model is required."}</p>
        <p>Telegram: {service.config.telegram.enabled ? "configured; delivery receipts tracked separately" : "not enabled; decisions remain available here"}.</p>
        <h3>Allowed clients</h3>{clients.length ? clients.map(([id, config]) => <p>{id} · {config.host}</p>) : <p>No request clients configured. An operator agent can use <code>card init</code> for a new installation.</p>}
        <p class="muted">Configured access is not proof a client is online. One server owns this workspace. Keep human credentials out of agent environments.</p>
      </section>
      <section class="decision-card"><h2>Explicit standing permissions</h2><p>Past answers and remembered preferences do not create permission.</p>{grants.length ? grants.map((grant) => <details><summary>{grant.effect_type} · {grant.expires_at ? `expires ${grant.expires_at}` : "no expiry"}</summary><div class="details-body"><pre>{grant.scope_json}</pre><p>Uses remaining: {grant.uses_remaining ?? "not count-limited"}</p>{hasWebWriteSession(c, service.config) && <form method="post" action={`/ui/grants/${grant.id}/revoke`}><button class="button danger" type="submit">Revoke permission</button></form>}</div></details>) : <p>No active grants.</p>}</section>
      <details><summary>Advanced inspection</summary><div class="details-body actions"><a href="/ui/events">Events</a><a href="/ui/incidents">Incidents</a><a href="/ui/runs">Runs</a><a href="/ui/digests">Digests</a><a href="/ui/policy">Safety and policy</a><a href="/ui/memory">Context notes</a></div></details>
      <form method="post" action="/ui/logout"><button class="button" type="submit">Sign out</button></form>
    </Layout>);
  });
  app.post("/grants/:id/revoke", (c) => { service.store.revokeGrant(c.req.param("id"), "human:web"); return c.redirect("/ui/settings", 303); });
}
