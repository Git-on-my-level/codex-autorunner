/** Human-facing decisions. No model settings, host topology or queue mechanics on the home page. */
import { Hono, type Context } from "hono";
import type { AttentionService } from "../../attention/service.ts";
import type { RequestRow } from "../../attention/contract.ts";
import { HumanAnswer } from "../../attention/contract.ts";
import { recordAnswer, reconcileReply, type ReplyRow } from "../../attention/replies.ts";
import { AttentionError } from "../../attention/errors.ts";
import { RequestCard, NativeCard, PageHeader, decisionLabels as label, type NativeDecision } from "./decision_views.tsx";
import { hasWebWriteSession } from "./auth.ts";
import { Layout } from "./layout.tsx";

import { decisionCounts, REQUEST_CONDITION, NATIVE_CONDITION, NATIVE_JOIN, type DecisionTab as Tab } from "./decision_queries.ts";
const tabNames = { needs_you: "Needs you", watching: "Watching", handled: "Handled" };
export function installDecisionRoutes(app: Hono, service: AttentionService): void {
  const store = service.store;
  const workspace = service.config.attention.workspace_id;
  const counts = () => decisionCounts(store, workspace);
  const waitingWithoutContact = () => (store.db.query(`SELECT COUNT(*) AS n FROM attention_requests WHERE workspace_id=?
    AND state IN ('answered','received') AND last_seen_at < ?`).get(workspace,
      new Date(store.clock.now().getTime() - 15 * 60_000).toISOString()) as { n: number }).n;
  const show = (tab: Tab) => (c: Context) => {
    service.sweep();
    // Missed decisions stay visible until a human acknowledges the outcome.
    const condition = REQUEST_CONDITION[tab];
    const requestedPage = Number(c.req.query("page") ?? "0");
    const page = Number.isSafeInteger(requestedPage) && requestedPage >= 0 ? requestedPage : 0;
    const rows = store.db.query(`SELECT * FROM attention_requests WHERE workspace_id=? AND ${condition}
      ORDER BY CASE json_extract(packet_json,'$.urgency') WHEN 'urgent' THEN 0 ELSE 1 END,
      CASE WHEN ?='handled' THEN -strftime('%s',COALESCE(closed_at,created_at)) ELSE strftime('%s',COALESCE(due_at,created_at)) END, id
      LIMIT 51 OFFSET ?`).all(workspace, tab, page * 50) as RequestRow[];
    const nativeCondition = NATIVE_CONDITION[tab];
    const native = store.db.query(`SELECT s.*, e.type AS event_type, e.body, e.title, e.source_host, e.received_at AS created_at, e.obligation_state,
        h.state AS reply_state, h.last_error, h.id AS reply_id, h.revision AS reply_revision, i.snooze_until
      ${NATIVE_JOIN} AND (${nativeCondition})
      ORDER BY CASE e.severity WHEN 'urgent' THEN 0 ELSE 1 END, e.received_at DESC LIMIT 51 OFFSET ?`).all(page * 50) as NativeDecision[];
    const deliveries = tab === "watching" ? store.db.query("SELECT * FROM human_replies WHERE request_id IS NULL AND escalation_id IS NULL AND state IN ('failed','uncertain','staged','delivered') ORDER BY updated_at DESC LIMIT 51 OFFSET ?").all(page * 50) as ReplyRow[] : [];
    const canWrite = hasWebWriteSession(c, service.config);
    const attentionErrors = (store.db.query("SELECT COUNT(*) AS n FROM human_replies WHERE state IN ('failed','uncertain')").get() as { n: number }).n;
    return c.html(<Layout title={tabNames[tab]} active={tab === "needs_you" ? "/ui" : `/ui/${tab}`} refreshSeconds={15} navCounts={counts()}>
      <PageHeader title={tabNames[tab]} description={tab === "needs_you" ? "Grounded decisions and missed deadlines that still need your attention." : tab === "watching" ? "Context gathering, recorded answers and work awaiting confirmation." : "Explicit outcomes, including cancellations and missed deadlines."}/>
      {c.req.query("recorded") && <p class="notice" role="status">Answer recorded. CAR will keep watching until the source confirms it is unblocked.</p>}
      {attentionErrors > 0 && <p class="context-warning"><a href="/ui/watching">{attentionErrors} reply delivery issue(s) need review.</a> CAR has not assumed they succeeded.</p>}
      {tab === "needs_you" && waitingWithoutContact() > 0 && <p class="notice"><a href="/ui/watching">{waitingWithoutContact()} answered request(s) have no recent source check-in.</a> They remain in Watching, not marked successful.</p>}
      <div class="decision-list">{rows.slice(0, 50).map((row) => <RequestCard view={service.view(row)} row={row} canWrite={canWrite}/>)}{native.slice(0, 50).map((row) => <NativeCard row={row} canWrite={canWrite}/>)}</div>
      {deliveries.slice(0, 50).map((reply) => <details><summary>Delivery review · {label[reply.state] ?? reply.state}</summary><div class="details-body stack"><p>{JSON.parse(reply.payload_json).text ?? "Permission response"}</p><p>{reply.last_error ?? "Delivery is not proof the work is unblocked."}</p><p class="muted">{reply.id}. Reconcile only after checking the native source. Do not retry an uncertain send speculatively.</p>{canWrite && <form class="answer-form" method="post" action={`/ui/replies/${reply.id}/reconcile`}><input type="hidden" name="expected_revision" value={reply.revision}/><label for={`reconcile-${reply.id}`}>What did you verify at the source?</label><textarea id={`reconcile-${reply.id}`} name="note" rows={2} maxlength={2000} required/><div class="actions"><button class="button" name="outcome" value="source_confirmed">Source is unblocked</button><button class="button" name="outcome" value="not_received_retry">Confirmed not received: retry</button><button class="button danger" name="outcome" value="cancel">Cancel reply</button></div></form>}</div></details>)}
      {rows.length === 0 && native.length === 0 && deliveries.length === 0 && <div class="empty-state"><h2>{tab === "needs_you" ? "No decisions waiting here" : "Nothing in this view"}</h2><p>CAR only knows about connected sources and submitted requests. An empty queue is not a health check for every agent.</p></div>}
      <nav class="pager actions" aria-label="Decision pages">{page > 0 && <a class="button" href={`?page=${page - 1}`}>Previous</a>}{(rows.length > 50 || native.length > 50 || deliveries.length > 50) && <a class="button" href={`?page=${page + 1}`}>Next</a>}</nav>
      <p class="muted"><a href="/ui/settings">Connections and settings</a> · <a href="/ui/events">Inspect incoming events</a></p>
    </Layout>);
  };
  app.get("/", show("needs_you")); app.get("/watching", show("watching")); app.get("/handled", show("handled"));
  app.get("/decisions/:id", (c) => {
    const row = service.get(c.req.param("id"));
    if (!row) return c.text("Decision not found", 404);
    return c.html(<Layout title="Decision" active="/ui" refreshSeconds={15} navCounts={counts()}><a href="/ui">Back to decisions</a>{c.req.query("recorded") && <p role="status">Answer recorded. The current delivery and outcome are shown below; recording is not proof of receipt.</p>}<RequestCard view={service.view(row)} row={row} canWrite={hasWebWriteSession(c, service.config)} detail/></Layout>);
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
    return c.redirect("/ui/watching?recorded=1", 303);
  });
  app.post("/replies/:id/reconcile", async (c) => {
    const body = await c.req.parseBody();
    if (!["source_confirmed", "not_received_retry", "cancel"].includes(String(body.outcome))) throw new AttentionError("invalid_outcome", "Choose a reconciliation outcome", 400);
    reconcileReply(store, { id: c.req.param("id"), expectedRevision: Number(body.expected_revision), actor: "human:web",
      outcome: body.outcome as "source_confirmed" | "not_received_retry" | "cancel", note: String(body.note ?? "") });
    return c.redirect("/ui/watching", 303);
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
