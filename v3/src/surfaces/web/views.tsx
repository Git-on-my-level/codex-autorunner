/** Pure server-rendered views. Data access and mutations remain outside this module. */
import { Layout, severityChip, autonomyChip, statusBadge, UI_ROOT } from "./layout.tsx";
import type { EventRow, IncidentRow, IncidentChain, MemoryRow, DigestRow, AgentRunViewRow, AgentObserverHealth } from "./queries.ts";
import type { AgentRunStateFilter, AgentRunSummary } from "./queries.ts";
import { isAgentRunFailureState, isAgentRunSuccessState, isAgentRunTerminalState, normalizeAgentRunState } from "../../contract/lifecycle.ts";

function fmtTs(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short" }).format(date);
}
function relativeTs(iso: string): string {
  const elapsed = Date.now() - new Date(iso).getTime();
  if (!Number.isFinite(elapsed)) return fmtTs(iso);
  const abs = Math.abs(elapsed);
  if (abs < 60_000) return "just now";
  if (abs < 3_600_000) return `${Math.round(abs / 60_000)}m ago`;
  if (abs < 86_400_000) return `${Math.round(abs / 3_600_000)}h ago`;
  return `${Math.round(abs / 86_400_000)}d ago`;
}
function truncate(s: string, n: number): string { return s.length > n ? `${s.slice(0, n)}…` : s; }
function pretty(json: string): string { try { return JSON.stringify(JSON.parse(json), null, 2); } catch { return json; } }
function titleCase(value: string): string {
  return value.replace(/[_.-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()).replace(/\bOmp\b/g, "OMP").replace(/\bCi\b/g, "CI").replace(/\bLlm\b/g, "LLM");
}
function eventTypeLabel(value: string): string { return titleCase(value.split(".").at(-1) || value); }
function sessionText(row: { session_title?: string | null; session_repo?: string | null; car_session_id: string | null }): string {
  return row.session_title || row.session_repo || row.car_session_id || "No session";
}
function sessionLabel(row: { session_title?: string | null; session_repo?: string | null; car_session_id: string | null }) {
  return row.car_session_id ? <span title={row.car_session_id}>{sessionText(row)}</span> : <span class="muted">No session</span>;
}
function sessionMeta(row: { session_title?: string | null; session_repo?: string | null; car_session_id: string | null }, primary: string): string | null {
  const session = sessionText(row);
  return session.trim().toLowerCase() === primary.trim().toLowerCase() ? null : session;
}
function compactRepo(repo: string | null | undefined): string | null {
  if (!repo) return null;
  const parts = repo.replace(/\/+$/, "").split("/").filter(Boolean);
  return parts.length >= 2 ? parts.slice(-2).join("/") : repo;
}
function incidentWorkState(incident: IncidentRow): { value: string; label: string } {
  if (incident.requires_operator_response) return { value: "unanswered", label: "Response required" };
  if (incident.state === "open" || incident.state === "escalated") return { value: "in_progress", label: "Monitoring" };
  return { value: incident.state, label: titleCase(incident.state) };
}
function PageHeader(props: { eyebrow?: string; title: string; description?: any; meta?: any }) {
  return <div class="page-header"><div class="page-header-copy">{props.eyebrow && <div class="page-eyebrow">{props.eyebrow}</div>}<h1>{props.title}</h1>{props.description && <p class="page-description">{props.description}</p>}</div>{props.meta && <div class="page-meta">{props.meta}</div>}</div>;
}
function EmptyState(props: { title: string; body: string }) { return <div class="empty-state"><h2>{props.title}</h2><p>{props.body}</p></div>; }
function fmtDuration(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return "—";
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}

/* ------------------------------------------------------------------ Inbox */
export interface InboxFiltersView { vendor?: string; severity?: string; state?: string; repo?: string; q?: string; }

export function InboxPage(props: { rows: EventRow[]; hasMore: boolean; filters: InboxFiltersView; vendors: string[]; severities: string[]; states: string[]; }) {
  const { rows, hasMore, filters, vendors, severities, states } = props;
  const qs = (extra: Record<string, string>) => { const merged = { ...filters, ...extra }; const p = new URLSearchParams(); for (const [k, v] of Object.entries(merged)) if (v) p.set(k, v); return p.size ? `?${p}` : ""; };
  const lastReceivedAt = rows.at(-1)?.received_at;
  const activeFilters = Object.values(filters).filter(Boolean).length;
  const escalated = rows.filter((row) => row.triage_state === "escalated").length;
  const urgent = rows.filter((row) => row.severity === "urgent").length;
  const filterForm = (prefix = "") => {
    const id = (name: string) => `${prefix}inbox-${name}`;
    return <form class="filter-bar" method="get" action={UI_ROOT} aria-label="Filter inbox">
      <div class="field"><label for={id("source")}>Source</label><select id={id("source")} name="vendor"><option value="">All sources</option>{vendors.map((v) => <option value={v} selected={filters.vendor === v}>{titleCase(v)}</option>)}</select></div>
      <div class="field"><label for={id("severity")}>Severity</label><select id={id("severity")} name="severity"><option value="">All severities</option>{severities.map((v) => <option value={v} selected={filters.severity === v}>{titleCase(v)}</option>)}</select></div>
      <div class="field"><label for={id("state")}>Triage state</label><select id={id("state")} name="state"><option value="">All states</option>{states.map((v) => <option value={v} selected={filters.state === v}>{titleCase(v)}</option>)}</select></div>
      <div class="field"><label for={id("repo")}>Repository</label><input id={id("repo")} type="text" name="repo" placeholder="owner/repository" value={filters.repo ?? ""} /></div>
      <div class="field grow"><label for={id("search")}>Search</label><input id={id("search")} type="search" name="q" placeholder="Title or event detail" value={filters.q ?? ""} /></div>
      <button class="button" type="submit">Apply{activeFilters ? ` (${activeFilters})` : ""}</button>
      {activeFilters > 0 && <a class="button ghost" href={UI_ROOT}>Clear all</a>}
    </form>;
  };
  return <Layout title="Inbox" active={UI_ROOT}>
    <PageHeader title="Inbox" description="Attention and meaningful outcomes, newest first." />
    <div class="overview-bar" aria-label="Inbox summary"><div class="overview-item"><span class="overview-value">{rows.length}</span><span class="overview-label">signals</span></div><div class="overview-item"><span class="overview-value">{escalated}</span><span class="overview-label">escalated</span></div><div class="overview-item"><span class="overview-value">{urgent}</span><span class="overview-label">urgent</span></div></div>
    <div class="desktop-filter">{filterForm()}</div>
    <details class="mobile-filter" open={activeFilters > 0}><summary>Filters{activeFilters ? ` · ${activeFilters} active` : ""}</summary>{filterForm("mobile-")}</details>
    {rows.length === 0 ? <EmptyState title={activeFilters ? "No matching events" : "Inbox is quiet"} body={activeFilters ? "No events match these filters. Clear them to return to the full attention stream." : "New provider signals will appear here after they are committed to the event journal."} /> : <>
      <div class="table-wrap desktop-table"><table><caption class="visually-hidden">Recent attention events</caption><thead><tr><th scope="col">Signal</th><th scope="col">Agent</th><th scope="col">State</th></tr></thead><tbody>{rows.map((r) => { const title = r.title || r.body; const session = sessionMeta(r, title); return <tr><td><a class="row-title" href={r.incident_id ? `${UI_ROOT}/incidents/${r.incident_id}` : `${UI_ROOT}?q=${encodeURIComponent(title)}`}>{truncate(title, 110)}</a><div class="row-meta"><time datetime={r.ts} title={fmtTs(r.ts)}>{relativeTs(r.ts)}</time> · {eventTypeLabel(r.type)}{session ? <> · {session}</> : null}</div></td><td>{titleCase(r.native_agent)}</td><td class="cell-status"><div class="actions">{statusBadge(r.triage_state)}{severityChip(r.severity)}</div></td></tr>; })}</tbody></table></div>
      <div class="record-list">{rows.map((r) => { const title = r.title || r.body; const session = sessionMeta(r, title); return <article class="record"><div class="record-top"><a class="row-title" href={r.incident_id ? `${UI_ROOT}/incidents/${r.incident_id}` : `${UI_ROOT}?q=${encodeURIComponent(title)}`}>{truncate(title, 95)}</a>{severityChip(r.severity)}</div><div class="record-meta">{statusBadge(r.triage_state)}<time datetime={r.ts} title={fmtTs(r.ts)}>{relativeTs(r.ts)}</time><span>{titleCase(r.native_agent)}</span><span>{eventTypeLabel(r.type)}</span>{session ? <span>{session}</span> : null}</div></article>; })}</div>
    </>}
    {hasMore && lastReceivedAt && <div class="pager"><a class="button" href={qs({ before: lastReceivedAt })}>Older events</a></div>}
  </Layout>;
}

/* -------------------------------------------------------------- Incidents */
export function IncidentsListPage(props: { rows: IncidentRow[]; state?: string }) {
  const { rows, state } = props; const label = state ? titleCase(state) : "Needs attention";
  return <Layout title="Incidents" active={`${UI_ROOT}/incidents`}>
    <PageHeader title="Incidents" description="Cases that need a decision or follow-through." />
    <nav class="tabs" aria-label="Incident state"><a href={`${UI_ROOT}/incidents`} class={!state ? "active" : ""} aria-current={!state ? "page" : undefined}>Needs attention{!state ? ` · ${rows.length}` : ""}</a>{["resolved", "snoozed", "expired"].map((s) => <a href={`${UI_ROOT}/incidents?state=${s}`} class={state === s ? "active" : ""} aria-current={state === s ? "page" : undefined}>{titleCase(s)}{state === s ? ` · ${rows.length}` : ""}</a>)}</nav>
    {rows.length === 0 ? <EmptyState title={`No ${label.toLowerCase()} incidents`} body={!state ? "Nothing currently requires operator attention. New escalations and open cases will appear here." : `There are no incidents in the ${label.toLowerCase()} state.`} /> : <>
      <div class="table-wrap desktop-table"><table><caption class="visually-hidden">{label} incidents</caption><thead><tr><th scope="col">Incident</th><th scope="col">Related work</th><th scope="col">State</th></tr></thead><tbody>{rows.map((i) => { const work = incidentWorkState(i); return <tr><td><a class="row-title" href={`${UI_ROOT}/incidents/${i.id}`}>{truncate(i.summary || i.id, 120)}</a><div class="row-meta"><time datetime={i.opened_at} title={fmtTs(i.opened_at)}>{relativeTs(i.opened_at)}</time>{i.llm_runs ? ` · ${i.llm_runs} provider run${i.llm_runs === 1 ? "" : "s"}` : ""}</div></td><td>{sessionLabel(i)}</td><td class="cell-status">{statusBadge(work.value, work.label)}</td></tr>; })}</tbody></table></div>
      <div class="record-list">{rows.map((i) => { const work = incidentWorkState(i); return <article class="record"><div class="record-top"><a class="row-title" href={`${UI_ROOT}/incidents/${i.id}`}>{truncate(i.summary || i.id, 100)}</a>{statusBadge(work.value, work.label)}</div><div class="record-meta"><time datetime={i.opened_at} title={fmtTs(i.opened_at)}>{relativeTs(i.opened_at)}</time><span>{sessionText(i)}</span>{i.llm_runs ? <span>{i.llm_runs} provider run{i.llm_runs === 1 ? "" : "s"}</span> : null}</div></article>; })}</div>
    </>}
  </Layout>;
}

export function IncidentDetailPage(props: { chain: IncidentChain }) {
  const { incident, events, decisions, actions, escalations, outcomes, audit } = props.chain;
  const latestDecision = decisions.at(-1); const latestEscalation = escalations.at(-1); const pendingEscalation = [...escalations].reverse().find((e) => !e.answer_json);
  const timeline = [
    ...events.map((e) => ({ ts: e.ts, kind: "Event", title: e.title || titleCase(e.type), body: e.body, state: e.severity, detail: `${titleCase(e.type)} · ${titleCase(e.native_agent)}` })),
    ...decisions.map((d) => ({ ts: d.created_at, kind: "Provider decision", title: titleCase(d.disposition), body: d.rationale, state: d.disposition, detail: [d.model, `${d.tokens_in}/${d.tokens_out} tokens`, `$${d.cost_usd.toFixed(4)}`].filter(Boolean).join(" · "), raw: d.action_args_json ? pretty(d.action_args_json) : "", decisionId: d.id })),
    ...escalations.map((e) => ({ ts: e.created_at, kind: "Escalation", title: e.question, body: e.answer_json ? `Answered by ${e.answered_by}: ${pretty(e.answer_json)}` : "Awaiting operator response", state: e.answer_json ? "confirmed" : "unanswered", detail: e.suggested_action_json ? `Suggested action: ${pretty(e.suggested_action_json)}` : "" })),
    ...outcomes.map((o) => ({ ts: o.created_at, kind: "Outcome", title: titleCase(o.verdict), body: o.note || (o.david_action_json ? pretty(o.david_action_json) : "Outcome recorded"), state: o.verdict, detail: "" })),
  ].sort((a, b) => a.ts.localeCompare(b.ts));
  const work = incidentWorkState(incident);
  const headerContext = [incident.session_vendor ? titleCase(incident.session_vendor) : null, compactRepo(incident.session_repo)]
    .filter(Boolean).join(" · ");
  const handoffCopy = incident.response_channel_kind === "agentctl-run" && incident.continuation_vendor
    ? "CAR will attempt native continuation; if it is unavailable, the answer is staged for handoff."
    : incident.response_channel_kind === "agentctl-run" && incident.response_agent
      ? `${titleCase(incident.response_agent)} has no verified continuation route. CAR records the answer and stages it for manual handoff; Telegram does not resume this agent.`
      : incident.response_channel_kind === "claude-hook-http"
        ? "CAR will return the decision to the held Claude Code request; if that hold expires, it falls back to continuation or staging."
        : incident.response_channel_kind === "claude-resume" || incident.response_channel_kind === "codex-exec-resume"
          ? "CAR will attempt native continuation; if it is unavailable, the answer is staged for handoff."
          : incident.response_channel_kind === "multica-api"
            ? "CAR will post the response to Multica; if delivery is unavailable, the answer is staged for handoff."
            : incident.response_agent
              ? `CAR records the answer and stages it for manual handoff; Telegram does not resume ${titleCase(incident.response_agent)}.`
              : "CAR records the answer, but this incident has no verified delivery route to an agent.";
  return <Layout title={incident.summary || "Incident"} active={`${UI_ROOT}/incidents`}>
    <p style="margin-bottom:10px"><a href={`${UI_ROOT}/incidents`}>Back to incidents</a></p>
    <PageHeader title={incident.summary || "Untitled incident"} description={<>Opened {relativeTs(incident.opened_at)}{headerContext ? ` · ${headerContext}` : ""}</>} />
    <div class="overview-bar incident-overview" aria-label="Incident summary"><div class="overview-item"><span class="overview-value">{statusBadge(work.value, work.label)}</span><span class="overview-label">work state</span></div><div class="overview-item"><span class="overview-value">{events.length}</span><span class="overview-label">signals</span></div><div class="overview-item"><span class="overview-value">{incident.llm_runs}</span><span class="overview-label">provider runs</span></div><div class="overview-item"><span class="overview-value">{timeline.length}</span><span class="overview-label">timeline items</span></div></div>
    {pendingEscalation ? <div class="decision-panel"><div class="actions">{severityChip(pendingEscalation.severity)}{statusBadge("unanswered", "Response required")}</div><div class="question">{pendingEscalation.question}</div>{pendingEscalation.sent_at ? <div class="handoff"><strong>Respond in Telegram</strong><span class="muted"> · Sent {fmtTs(pendingEscalation.sent_at)}</span><p class="row-meta">{handoffCopy}</p>{pendingEscalation.telegram_message_id ? <details style="margin-top:10px"><summary>Delivery details</summary><div class="details-body"><dl class="metadata"><dt>Message ID</dt><dd><code>{pendingEscalation.telegram_message_id}</code></dd><dt>Decision surface</dt><dd>Telegram</dd></dl></div></details> : null}</div> : <p class="handoff"><strong>Delivery pending.</strong> No Telegram receipt has been recorded.</p>}</div> : latestEscalation ? <div class="notice positive"><strong>Operator response recorded</strong><p>{latestEscalation.answer_json ? `Answered by ${latestEscalation.answered_by} at ${latestEscalation.answered_at ? fmtTs(latestEscalation.answered_at) : "an unknown time"}.` : "This escalation no longer requires a response."}</p></div> : <div class="notice"><strong>Monitoring</strong><p>No response is currently requested. CAR is waiting for more evidence or follow-up.</p></div>}
    <div class="detail-grid section">
      <section class="panel"><div class="panel-header"><h2>Why CAR is asking</h2></div><div class="panel-body"><p class="summary-copy">{latestDecision?.rationale || events.at(-1)?.body || "CAR grouped related signals into this incident for continued monitoring."}</p></div></section>
      <aside class="panel"><div class="panel-header"><h2>Context</h2></div><div class="panel-body"><dl class="metadata"><dt>State</dt><dd>{titleCase(incident.state)}</dd><dt>Opened</dt><dd>{fmtTs(incident.opened_at)}</dd><dt>Related work</dt><dd>{sessionLabel(incident)}</dd><dt>Last decision</dt><dd>{latestDecision ? `${titleCase(latestDecision.disposition)} · ${latestDecision.model}` : "None"}</dd></dl></div></aside>
    </div>
    <section class="section"><div class="section-heading"><h2>Timeline</h2></div><div class="panel"><div class="panel-body timeline">{timeline.length === 0 ? <p class="muted">No timeline entries recorded.</p> : timeline.map((item) => <article class="timeline-item"><time class="timeline-time" datetime={item.ts}>{fmtTs(item.ts)}</time><div class="timeline-body"><div class="actions"><span class="badge">{item.kind === "Event" ? "Signal" : item.kind === "Provider decision" ? "Decision" : item.kind === "Escalation" ? "Question" : item.kind}</span>{statusBadge(item.state)}</div><h3>{item.title}</h3>{item.body && <p>{item.body}</p>}{item.detail && <p class="muted">{item.detail}</p>}{"decisionId" in item && item.decisionId && actions.filter((a) => a.decision_id === item.decisionId).map((a) => <div class="notice" style="margin-top:10px"><strong>{titleCase(a.class)} · {titleCase(a.state)}</strong><p>Policy verdict: {a.policy_verdict}</p>{a.result_json && <pre>{pretty(a.result_json)}</pre>}</div>)}{"raw" in item && item.raw && <details style="margin-top:10px"><summary>Structured proposal</summary><div class="details-body"><pre>{item.raw}</pre></div></details>}</div></article>)}</div></div></section>
    <section class="section"><details><summary>Technical evidence · {audit.length} audit entries</summary><div class="details-body stack"><dl class="metadata"><dt>Incident ID</dt><dd><code>{incident.id}</code></dd>{incident.dedupe_class && <><dt>Grouping key</dt><dd><code>{incident.dedupe_class}</code></dd></>}</dl><div class="table-wrap"><table><caption class="visually-hidden">Incident audit log</caption><thead><tr><th scope="col">Time</th><th scope="col">Actor</th><th scope="col">Verb</th><th scope="col">Object</th><th scope="col">Detail</th></tr></thead><tbody>{audit.map((a) => <tr><td>{fmtTs(a.ts)}</td><td>{a.actor}</td><td>{a.verb}</td><td><code>{a.object_type}:{a.object_id}</code></td><td><code>{truncate(a.detail_json, 160)}</code></td></tr>)}</tbody></table></div></div></details></section>
  </Layout>;
}

/* -------------------------------------------------------------------- Runs */
function runWorkState(run: AgentRunViewRow, observerReliable: boolean): { value: string; label: string } {
  const state = normalizeAgentRunState(run.state);
  if (isAgentRunSuccessState(state)) return { value: "complete", label: "Completed" };
  if (isAgentRunFailureState(state) || (run.terminal_at && !isAgentRunSuccessState(state))) {
    return { value: "failed", label: titleCase(state || "failed") };
  }
  if (!observerReliable || run.observation_state === "stale" || run.observation_state === "unknown" || run.liveness === "unreachable") {
    return { value: "unanswered", label: `Last seen ${titleCase(state || "running")}` };
  }
  if (state === "attention") return { value: "unanswered", label: "Needs attention" };
  if (run.liveness === "blocked") return { value: "unanswered", label: "Blocked" };
  return { value: "in_progress", label: titleCase(state || "running") };
}
function runScope(run: AgentRunViewRow): string | null {
  if (run.repo) return run.repo;
  if (run.cwd) return run.cwd.split("/").filter(Boolean).at(-1) || run.cwd;
  return null;
}
function runLabels(run: AgentRunViewRow): string[] {
  try {
    const labels = JSON.parse(run.labels_json);
    return Array.isArray(labels) ? labels.filter((label): label is string => typeof label === "string") : [];
  } catch {
    return [];
  }
}
function runDuration(run: AgentRunViewRow, observerReliable: boolean): number | null {
  if (!isAgentRunTerminalState(run.state) && observerReliable && run.observation_state === "observed" && run.started_at) {
    const elapsed = (Date.now() - new Date(run.started_at).getTime()) / 1_000;
    if (Number.isFinite(elapsed)) return Math.max(0, elapsed, run.duration_seconds ?? 0);
  }
  return run.duration_seconds;
}
function runFreshness(run: AgentRunViewRow): { label: string; at: string } {
  if (isAgentRunTerminalState(run.state) && run.terminal_at) return { label: `Finished ${relativeTs(run.terminal_at)}`, at: run.terminal_at };
  return { label: `Seen ${relativeTs(run.last_observed_at)}`, at: run.last_observed_at };
}
function RunDetails({ run }: { run: AgentRunViewRow }) {
  const labels = runLabels(run);
  const scope = run.repo || run.cwd;
  return <details><summary>Technical details</summary><div class="details-body"><dl class="metadata">
    <dt>Execution</dt><dd><code>{run.execution_id}</code></dd>
    <dt>Liveness</dt><dd>{titleCase(run.liveness || "unknown")}</dd>
    <dt>Observed</dt><dd><time datetime={run.last_observed_at}>{fmtTs(run.last_observed_at)}</time></dd>
    <dt>Agent update</dt><dd><time datetime={run.updated_at}>{fmtTs(run.updated_at)}</time></dd>
    {scope ? <><dt>Scope</dt><dd><code>{scope}</code></dd></> : null}
    {labels.length ? <><dt>Labels</dt><dd>{labels.join(" · ")}</dd></> : null}
    {run.authority ? <><dt>Authority</dt><dd>{titleCase(run.authority)}</dd></> : null}
    {run.mode ? <><dt>Mode</dt><dd>{titleCase(run.mode)}</dd></> : null}
    {run.runtime ? <><dt>Runtime</dt><dd>{run.runtime}</dd></> : null}
    {run.model ? <><dt>Model</dt><dd>{run.model}</dd></> : null}
    {run.profile ? <><dt>Profile</dt><dd>{run.profile}</dd></> : null}
    {run.continuation_supported ? <><dt>Resume route</dt><dd>Available</dd></> : null}
  </dl></div></details>;
}
export function RunsPage(props: {
  runs: AgentRunViewRow[];
  health: AgentObserverHealth;
  observerEnabled: boolean;
  observerReliable: boolean;
  summary: AgentRunSummary;
  filters: { state: AgentRunStateFilter; agent?: string };
  agents: string[];
  page: number;
  hasNext: boolean;
  filteredTotal: number;
  refreshSeconds: number;
  scopeLabel: string;
}) {
  const healthNotice = !props.observerEnabled
    ? { cls: "", title: "Local observation is off", body: "Enable the agentctl observer with an exact label set, or explicitly choose observe all." }
    : props.health.state === "degraded"
      ? { cls: "critical", title: "Observation degraded", body: props.health.error || "CAR could not refresh local run metadata." }
      : props.health.state === "stale"
        ? { cls: "warning", title: "Run status may be stale", body: props.health.error || "The observer has not refreshed recently." }
      : props.health.state === "not_started"
        ? { cls: "", title: "Observation is starting", body: `Waiting for the first refresh. ${props.scopeLabel}.` }
      : props.health.coverage_degraded
        ? { cls: "warning", title: "Active coverage is incomplete", body: "More active runs exist than the configured discovery limit. Raise the limit up to 200 before treating the active count as complete." }
      : props.health.history_truncated
        ? { cls: "", title: "Older history is partial", body: "Active coverage is complete. Older agentctl runs remain available at the execution authority but may not be imported into this projection." }
        : null;
  const qs = (changes: Partial<{ state: AgentRunStateFilter; agent: string; page: number }>) => {
    const values = { state: props.filters.state, agent: props.filters.agent || "", page: props.page, ...changes };
    const query = new URLSearchParams();
    if (values.state !== "all") query.set("state", values.state);
    if (values.agent) query.set("agent", values.agent);
    if (values.page > 0) query.set("page", String(values.page));
    return `${UI_ROOT}/runs${query.size ? `?${query}` : ""}`;
  };
  const tabs: { state: AgentRunStateFilter; label: string; count: number }[] = [
    { state: "all", label: "All", count: props.summary.total },
    { state: "active", label: "Active", count: props.summary.active },
    { state: "attention", label: "Needs attention", count: props.summary.attention },
    { state: "finished", label: "Finished", count: props.summary.finished },
  ];
  return <Layout title="Runs" active={`${UI_ROOT}/runs`} refreshSeconds={props.refreshSeconds}>
    <PageHeader title="Runs" description={<>Local agent work without progress noise in the Inbox. <span class="faint">{props.scopeLabel}.</span></>} meta={props.health.observed_at ? `${props.observerReliable ? "Live · " : ""}refreshed ${relativeTs(props.health.observed_at)}` : undefined} />
    <div class="overview-bar" aria-label="Run summary"><div class="overview-item"><span class="overview-value">{props.summary.active}</span><span class="overview-label">active</span></div><div class="overview-item"><span class="overview-value">{props.summary.attention}</span><span class="overview-label">need attention</span></div><div class="overview-item"><span class="overview-value">{props.summary.finished}</span><span class="overview-label">finished</span></div></div>
    {healthNotice && <div class={`notice ${healthNotice.cls}`} style="margin-bottom:14px"><strong>{healthNotice.title}</strong><p>{healthNotice.body}</p></div>}
    <div class="tabs" aria-label="Run state filter">{tabs.map((tab) => <a class={props.filters.state === tab.state ? "active" : ""} href={qs({ state: tab.state, page: 0 })}>{tab.label} <span class="count">{tab.count}</span></a>)}</div>
    <form class="filter-bar compact-filter" method="get" action={`${UI_ROOT}/runs`} aria-label="Filter runs">
      {props.filters.state !== "all" ? <input type="hidden" name="state" value={props.filters.state} /> : null}
      <div class="field"><label for="runs-agent">Agent</label><select id="runs-agent" name="agent"><option value="">All agents</option>{props.agents.map((agent) => <option value={agent} selected={props.filters.agent === agent}>{titleCase(agent)}</option>)}</select></div>
      <button class="button" type="submit">Apply</button>
      {props.filters.agent ? <a class="button ghost" href={qs({ agent: "", page: 0 })}>Clear agent</a> : null}
    </form>
    <div class="section-heading runs-heading"><h2>{props.filters.state === "all" ? "Recent runs" : tabs.find((tab) => tab.state === props.filters.state)?.label} <span class="count">{props.filteredTotal}</span></h2>{props.page > 0 ? <span class="muted">Page {props.page + 1}</span> : null}</div>
    {props.runs.length === 0 ? <EmptyState title={props.summary.total ? "No matching runs" : "No observed runs"} body={props.summary.total ? "Try another state or agent filter." : `${props.scopeLabel} will appear here. Inbox remains reserved for meaningful attention signals.`} /> : <>
      <div class="table-wrap desktop-table"><table><caption class="visually-hidden">Observed local agent runs</caption><thead><tr><th scope="col">Work</th><th scope="col">Agent</th><th scope="col">Duration</th><th scope="col">State</th></tr></thead><tbody>{props.runs.map((run) => { const state = runWorkState(run, props.observerReliable); const freshness = runFreshness(run); const scope = runScope(run); return <tr><td><strong>{run.title || `${titleCase(run.agent)} run`}</strong><div class="row-meta">{scope ? `${scope} · ` : ""}<time datetime={freshness.at} title={fmtTs(freshness.at)}>{freshness.label}</time></div><div style="margin-top:8px"><RunDetails run={run} /></div></td><td>{titleCase(run.agent)}</td><td>{fmtDuration(runDuration(run, props.observerReliable))}</td><td class="cell-status">{statusBadge(state.value, state.label)}</td></tr>; })}</tbody></table></div>
      <div class="record-list">{props.runs.map((run) => { const state = runWorkState(run, props.observerReliable); const freshness = runFreshness(run); const scope = runScope(run); return <article class="record"><div class="record-top"><strong>{run.title || `${titleCase(run.agent)} run`}</strong>{statusBadge(state.value, state.label)}</div><div class="record-meta"><span>{titleCase(run.agent)}</span><span>{fmtDuration(runDuration(run, props.observerReliable))}</span><time datetime={freshness.at} title={fmtTs(freshness.at)}>{freshness.label}</time></div>{scope ? <p class="row-meta">{scope}</p> : null}<RunDetails run={run} /></article>; })}</div>
      {(props.page > 0 || props.hasNext) ? <nav class="pager actions" aria-label="Run pages">{props.page > 0 ? <a class="button" href={qs({ page: props.page - 1 })}>Newer</a> : null}{props.hasNext ? <a class="button" href={qs({ page: props.page + 1 })}>Older</a> : null}</nav> : null}
    </>}
  </Layout>;
}

/* ----------------------------------------------------------------- Memory */
function scopeLabel(row: MemoryRow): string { try { const scope = JSON.parse(row.scope_json) as Record<string, unknown>; const parts = Object.entries(scope).map(([k,v]) => { if (k === "repo" && typeof v === "string") return v.split("/").at(-1) || v; if (k === "provider") return titleCase(String(v)); return `${titleCase(k)}: ${v}`; }); return parts.length ? parts.join(", ") : "Everywhere"; } catch { return row.scope_json; } }
function authorLabel(value: string): string { return value === "outcome-learning" ? "Learned from outcomes" : titleCase(value); }
function memoryMeta(row: MemoryRow): string { return [...new Set([authorLabel(row.authored_by), scopeLabel(row)])].join(" · "); }
function contentAction(row: MemoryRow): string | null { try { const value = (JSON.parse(row.content_json) as Record<string, unknown>).action_class; return typeof value === "string" ? titleCase(value) : null; } catch { return null; } }
function contentSummary(row: MemoryRow): string { try { const c = JSON.parse(row.content_json) as Record<string, unknown>; const prose = ["text","statement","question"].map((k) => c[k]).find((v):v is string => typeof v === "string" && v.trim().length > 0); const action = typeof c.action_class === "string" ? c.action_class : null; if (prose) return truncate(prose, 180); const parts:string[]=[]; if(c.match!==undefined)parts.push(`If ${JSON.stringify(c.match)}`); if(typeof c.disposition==="string")parts.push(`then ${titleCase(c.disposition)}`); if(action)parts.push(`(${titleCase(action)})`); return truncate(parts.length ? parts.join(" ") : JSON.stringify(c),180); } catch { return truncate(row.content_json,180); } }
function confidenceLabel(value:number):string { return value >= .8 ? "High" : value >= .5 ? "Medium" : "Low"; }

export function MemoryPage(props: { rules: MemoryRow[]; notes: MemoryRow[]; pending: MemoryRow[]; charter: string; charterPath: string; noteAdded?: boolean; canWrite: boolean; }) {
  const { rules, notes, pending, charter, charterPath, noteAdded, canWrite } = props;
  return <Layout title="Legacy context" active={`${UI_ROOT}/memory`}>
    <PageHeader title="Legacy compatibility" description="V2-only context kept for migration and named legacy consumers." />
    <div class="notice warning"><strong>Does not affect v3</strong><p>Nothing here can influence provider proposals, create a grant, or bypass core safety.</p></div>
    <section class="section"><div class="section-heading"><h2>Review queue <span class="count">{pending.length}</span></h2></div>{pending.length === 0 ? <EmptyState title="Nothing needs review" body="No compatibility proposals are waiting." /> : <div class="review-list">{pending.map((p) => <article class="review-card"><div><h3>{contentSummary(p)}</h3><p class="row-meta">{memoryMeta(p)} · {relativeTs(p.created_at)}</p></div>{canWrite ? <div class="actions"><form class="inline" method="post" action={`${UI_ROOT}/memory/${p.id}/approve`}><button class="button primary" type="submit" aria-label={`Accept in legacy store: ${contentSummary(p)}`}>Accept in legacy store</button></form><form class="inline" method="post" action={`${UI_ROOT}/memory/${p.id}/reject`}><button class="button danger" type="submit" aria-label={`Dismiss legacy proposal: ${contentSummary(p)}`}>Dismiss proposal</button></form></div> : <a class="button" href={`${UI_ROOT}/login`}>Sign in to review</a>}</article>)}</div>}</section>
    <section class="section"><div class="section-heading"><h2>Accepted context <span class="count">{rules.length}</span></h2></div>{rules.length === 0 ? <EmptyState title="No accepted context" body="This compatibility store has no active rules." /> : <div class="table-wrap desktop-table"><table><caption class="visually-hidden">Accepted legacy context</caption><thead><tr><th scope="col">Context</th><th scope="col">Applies to</th><th scope="col">Confidence</th><th scope="col">Setting</th><th scope="col">Actions</th></tr></thead><tbody>{rules.map((r) => <tr><td><strong>{contentSummary(r)}</strong><div class="row-meta">{authorLabel(r.authored_by)} · updated {relativeTs(r.updated_at)}{contentAction(r) ? ` · ${contentAction(r)}` : ""}</div></td><td>{scopeLabel(r)}</td><td class="confidence">{confidenceLabel(r.confidence)} ({r.confidence.toFixed(2)})<div class="row-meta">{r.evidence_confirm} confirm · {r.evidence_override} contradict</div></td><td>{autonomyChip(r.autonomy)}</td><td class="cell-status">{canWrite ? <div class="actions">{r.autonomy !== "none" && <form class="inline" method="post" action={`${UI_ROOT}/memory/${r.id}/demote`}><button class="button" type="submit" aria-label={`Reduce legacy setting: ${contentSummary(r)}`}>Reduce legacy setting</button></form>}<form class="inline" method="post" action={`${UI_ROOT}/memory/${r.id}/archive`}><button class="button danger" type="submit" aria-label={`Archive legacy context: ${contentSummary(r)}`}>Archive context</button></form></div> : <a href={`${UI_ROOT}/login`}>Sign in</a>}</td></tr>)}</tbody></table></div>}
      {rules.length > 0 && <div class="record-list">{rules.map((r) => <article class="record"><div class="record-top"><strong>{contentSummary(r)}</strong>{autonomyChip(r.autonomy)}</div><div class="record-meta"><span>{scopeLabel(r)}</span><span>{confidenceLabel(r.confidence)} confidence</span>{contentAction(r) ? <span>{contentAction(r)}</span> : null}<span>{r.evidence_confirm} confirm · {r.evidence_override} contradict</span></div>{canWrite ? <div class="actions">{r.autonomy !== "none" && <form class="inline" method="post" action={`${UI_ROOT}/memory/${r.id}/demote`}><button class="button" type="submit" aria-label={`Reduce legacy setting: ${contentSummary(r)}`}>Reduce legacy setting</button></form>}<form class="inline" method="post" action={`${UI_ROOT}/memory/${r.id}/archive`}><button class="button danger" type="submit" aria-label={`Archive legacy context: ${contentSummary(r)}`}>Archive context</button></form></div> : <a class="button" href={`${UI_ROOT}/login`}>Sign in to manage</a>}</article>)}</div>}
    </section>
    <section class="section"><div class="section-heading"><h2>Human notes <span class="count">{notes.length}</span></h2></div>{noteAdded && <div class="notice positive" role="status"><strong>Context saved</strong><p>The note was written to the compatibility store.</p></div>}{canWrite ? <div class="panel" style="margin-top:10px"><div class="panel-body"><form method="post" action={`${UI_ROOT}/memory/notes`} class="composer"><div class="field"><label for="note-text">Context</label><input id="note-text" type="text" name="text" placeholder="What should a legacy consumer know?" required aria-describedby="note-help" /></div><div class="field"><label for="note-vendor">Provider</label><input id="note-vendor" type="text" name="vendor" placeholder="All providers" /></div><div class="field"><label for="note-repo">Repository</label><input id="note-repo" type="text" name="repo" placeholder="All repositories" /></div><button class="button primary" type="submit">Add context</button><p id="note-help" class="muted" style="grid-column:1/-1">Saved here only; this never grants permission to execute.</p></form></div></div> : <div class="notice" style="margin-top:10px"><strong>Read-only</strong><p><a href={`${UI_ROOT}/login`}>Sign in</a> to edit compatibility context.</p></div>}{notes.length === 0 ? <div style="margin-top:10px"><EmptyState title="No human notes" body="No operator-authored notes are stored here." /></div> : <><div class="table-wrap desktop-table" style="margin-top:10px"><table><caption class="visually-hidden">Legacy human context</caption><thead><tr><th scope="col">Context</th><th scope="col">Applies to</th><th scope="col">Source</th></tr></thead><tbody>{notes.map((n) => <tr><td>{contentSummary(n)}<div class="row-meta">Added {fmtTs(n.created_at)}</div></td><td>{scopeLabel(n)}</td><td>{authorLabel(n.authored_by)}</td></tr>)}</tbody></table></div><div class="record-list" style="margin-top:10px">{notes.map((n) => <article class="record"><strong>{contentSummary(n)}</strong><div class="record-meta"><span>{fmtTs(n.created_at)}</span><span>{authorLabel(n.authored_by)}</span><span>{scopeLabel(n)}</span></div></article>)}</div></>}</section>
    <section class="section"><details><summary>Compatibility charter</summary><div class="details-body">{charter ? <><p class="row-meta" style="margin-bottom:10px">Source: <code>{charterPath}</code></p><pre class="code-block">{charter}</pre></> : <p class="muted">No charter is configured.</p>}</div></details></section>
  </Layout>;
}

/* ----------------------------------------------------------------- Policy */
export function PolicyPage(props: { path: string; raw: string | null; parseOk: boolean; parseError?: string }) {
  const { path, raw, parseOk, parseError } = props;
  return <Layout title="Safety & policy" active={`${UI_ROOT}/policy`}>
    <PageHeader title="Safety & policy" description="Where proposals become authorized effects—or stop." />
    <div class="authority-strip" aria-label="Effect authority chain"><div class="authority-step"><strong>1 · Provider proposes</strong><span>Advice only</span></div><div class="authority-step"><strong>2 · Core authorizes</strong><span>Grant and safety checks</span></div><div class="authority-step"><strong>3 · Executor runs</strong><span>Claim-time enforcement</span></div></div>
    <section class="panel"><div class="panel-header"><div><h2>Core safety boundary</h2><p class="row-meta">Always authoritative</p></div>{statusBadge("pending", "Core-controlled")}</div><div class="panel-body"><p>Every effect needs a matching grant and must pass panic mode, budget, rate, circuit-breaker, deadline, and allowlist checks when claimed.</p></div></section>
    <section class="section"><div class="section-heading"><h2>V2 compatibility policy</h2></div><div class="panel"><div class="panel-header"><div><h2>policy.toml</h2><p class="row-meta">Diagnostic only · never grants v3 authority</p></div>{raw === null ? statusBadge("none", "Not configured") : parseOk ? statusBadge("ok") : statusBadge("error")}</div><div class="panel-body stack">{raw === null ? <p class="muted">No policy.toml found. V3 continues to fail closed.</p> : <details open={!parseOk}><summary>View file · {parseOk ? "ok" : "error"}</summary><div class="details-body stack">{!parseOk && parseError && <div class="notice critical"><strong>Parse error</strong><pre>{parseError}</pre></div>}<pre class="code-block">{raw}</pre></div></details>}<details><summary>Technical details</summary><div class="details-body"><dl class="metadata"><dt>Source</dt><dd><code>{path}</code></dd><dt>Parse status</dt><dd>{raw === null ? "Not present" : parseOk ? "Valid" : "Invalid"}</dd><dt>V3 authority</dt><dd>None</dd></dl></div></details></div></div></section>
  </Layout>;
}

/* ---------------------------------------------------------------- Digests */
function digestDelivery(digest: DigestRow): { state: string; label: string; detail: string } {
  if (digest.outbox_id === null) return { state: "none", label: "Generated only", detail: "No delivery job recorded" };
  switch (digest.outbox_state) {
    case "pending": return { state: "queued", label: "Queued", detail: "Waiting for the delivery worker" };
    case "sending": return { state: "in_progress", label: "Sending", detail: "A delivery claim is active" };
    case "delivered":
    case "sent": return digest.sent_at
      ? { state: "sent", label: "Delivered", detail: fmtTs(digest.sent_at) }
      : { state: "sent", label: "Receipt recorded", detail: "Archive reconciliation pending" };
    case "uncertain": return { state: "uncertain", label: "Delivery uncertain", detail: "Verify remotely before retrying" };
    case "failed":
    case "dead": return { state: "failed", label: "Delivery failed", detail: "Review the outbox failure before retrying" };
    case "deferred": return { state: "queued", label: "Deferred", detail: "Held for a later digest or delivery window" };
    case "suppressed": return { state: "none", label: "Suppressed", detail: "Delivery was intentionally suppressed" };
    case "expired": return { state: "expired", label: "Expired", detail: "The delivery window closed" };
    case "superseded": return { state: "none", label: "Superseded", detail: "A newer delivery intent replaced this one" };
    case "abandoned": return { state: "none", label: "Abandoned", detail: "Delivery was closed without a receipt" };
    case "no_target": return { state: "failed", label: "No target", detail: "No delivery destination could be resolved" };
    default: return { state: "none", label: "State unavailable", detail: "The linked outbox row has no recognized lifecycle state" };
  }
}
function renderDigest(markdown: string) {
  const lines = markdown.split(/\r?\n/); const firstContent = lines.findIndex((line) => line.trim().length > 0); if (firstContent >= 0 && /^#\s+CAR digest\b/i.test(lines[firstContent]!.trim())) lines.splice(firstContent, 1); const blocks: any[] = []; let bullets: string[] = [];
  const flush = () => { if (bullets.length) { blocks.push(<ul>{bullets.map((line) => <li>{line}</li>)}</ul>); bullets = []; } };
  for (const line of lines) { const text = line.trim(); if (!text) { flush(); continue; } if (text.startsWith("### ")) { flush(); blocks.push(<h3>{text.slice(4)}</h3>); } else if (text.startsWith("## ")) { flush(); blocks.push(<h3>{text.slice(3)}</h3>); } else if (text.startsWith("# ")) { flush(); blocks.push(<h3>{text.slice(2)}</h3>); } else if (/^[-*] /.test(text)) bullets.push(text.slice(2)); else { flush(); blocks.push(<p>{text}</p>); } } flush(); return blocks;
}
export function DigestsPage(props: { digests: DigestRow[] }) {
  return <Layout title="Digests" active={`${UI_ROOT}/digests`}><PageHeader title="Digests" description={`${props.digests.length} retained summaries with delivery status.`} />{props.digests.length === 0 ? <EmptyState title="No digests yet" body="Completed summaries will appear here with their delivery status." /> : <div class="digest-list">{props.digests.map((d) => { const delivery = digestDelivery(d); return <article class="digest"><header class="digest-header"><h2>{d.day}</h2><div class="actions">{statusBadge(delivery.state, delivery.label)}<span class="page-meta">{delivery.detail}</span></div></header><div class="digest-content">{renderDigest(d.rendered_md)}</div><details style="border-left:0;border-right:0;border-bottom:0;border-radius:0"><summary>Delivery & raw data</summary><div class="details-body stack"><dl class="metadata"><dt>State</dt><dd>{delivery.label}</dd><dt>Outbox ID</dt><dd>{d.outbox_id ?? "No delivery job"}</dd>{d.sent_message_id && <><dt>Remote receipt</dt><dd><code>{d.sent_message_id}</code></dd></>}{d.outbox_created_at && <><dt>Created</dt><dd>{fmtTs(d.outbox_created_at)}</dd></>}</dl>{d.outbox_result_json && <pre class="code-block">{pretty(d.outbox_result_json)}</pre>}<pre class="code-block">{d.rendered_md}</pre></div></details></article>; })}</div>}</Layout>;
}

export function LoginPage() { return <Layout title="Sign in"><div class="sign-in"><div class="page-header-copy" style="margin-bottom:18px"><div class="page-eyebrow">CAR v3</div><h1>Sign in</h1><p class="page-description">Authenticate to make governance changes. Read-only operator pages remain available without a session.</p></div><div class="panel"><div class="panel-body"><form class="stack" method="post" action={`${UI_ROOT}/login`}><div class="field"><label for="web-token">Web token</label><input id="web-token" name="token" type="password" autocomplete="current-password" required /></div><button class="button primary" type="submit">Sign in</button></form></div></div></div></Layout>; }
export function NotFoundPage(what: string) { return <Layout title="Not found"><PageHeader eyebrow="Navigation" title="Not found" description={what} /><a class="button" href={UI_ROOT}>Return to inbox</a></Layout>; }
