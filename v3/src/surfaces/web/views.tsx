/**
 * Page components. Pure rendering: each takes already-fetched data (from
 * queries.ts) and returns JSX. No DB access here.
 */
import { Layout, severityChip, autonomyChip, UI_ROOT } from "./layout.tsx";
import type {
  EventRow,
  IncidentRow,
  IncidentChain,
  MemoryRow,
  DigestRow,
} from "./queries.ts";

function fmtTs(iso: string): string {
  return iso.replace("T", " ").replace(/\.\d+Z?$/, "").replace("Z", "");
}

function truncate(s: string, n: number): string {
  return s.length > n ? `${s.slice(0, n)}…` : s;
}

function pretty(json: string): string {
  try {
    return JSON.stringify(JSON.parse(json), null, 2);
  } catch {
    return json;
  }
}

function sessionLabel(row: { session_title?: string | null; session_repo?: string | null; car_session_id: string | null }) {
  if (!row.car_session_id) return <span class="muted">—</span>;
  const label = row.session_title || row.session_repo || row.car_session_id;
  return <code title={row.car_session_id}>{label}</code>;
}

/* ------------------------------------------------------------------ Inbox */

export interface InboxFiltersView {
  vendor?: string;
  severity?: string;
  state?: string;
  repo?: string;
  q?: string;
}

export function InboxPage(props: {
  rows: EventRow[];
  hasMore: boolean;
  filters: InboxFiltersView;
  vendors: string[];
  severities: string[];
  states: string[];
}) {
  const { rows, hasMore, filters, vendors, severities, states } = props;
  const qs = (extra: Record<string, string>) => {
    const merged = { ...filters, ...extra };
    const params = new URLSearchParams();
    for (const [k, v] of Object.entries(merged)) if (v) params.set(k, v);
    const s = params.toString();
    return s ? `?${s}` : "";
  };
  const lastReceivedAt = rows.length ? rows[rows.length - 1]!.received_at : undefined;
  return (
    <Layout title="Inbox" active={UI_ROOT}>
      <h1>Inbox</h1>
      <form class="filters" method="get" action={UI_ROOT}>
        <select name="vendor">
          <option value="">any vendor</option>
          {vendors.map((v) => (
            <option value={v} selected={filters.vendor === v}>
              {v}
            </option>
          ))}
        </select>
        <select name="severity">
          <option value="">any severity</option>
          {severities.map((v) => (
            <option value={v} selected={filters.severity === v}>
              {v}
            </option>
          ))}
        </select>
        <select name="state">
          <option value="">any state</option>
          {states.map((v) => (
            <option value={v} selected={filters.state === v}>
              {v}
            </option>
          ))}
        </select>
        <input type="text" name="repo" placeholder="repo" value={filters.repo ?? ""} />
        <input type="text" name="q" placeholder="search title/body…" value={filters.q ?? ""} />
        <button type="submit">Filter</button>
        {(filters.vendor || filters.severity || filters.state || filters.repo || filters.q) && (
          <a href={UI_ROOT}>clear</a>
        )}
      </form>

      {rows.length === 0 ? (
        <p class="empty">No events match.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>ts</th>
              <th>type</th>
              <th>severity</th>
              <th>source</th>
              <th>session</th>
              <th>triage state</th>
              <th>title</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr>
                <td>{fmtTs(r.ts)}</td>
                <td>{r.type}</td>
                <td>{severityChip(r.severity)}</td>
                <td>
                  {r.source_vendor}
                  <span class="muted"> @ {r.source_host}</span>
                </td>
                <td>{sessionLabel(r)}</td>
                <td>
                  {r.incident_id ? (
                    <a href={`${UI_ROOT}/incidents/${r.incident_id}`}>{r.triage_state}</a>
                  ) : (
                    r.triage_state
                  )}
                </td>
                <td>{truncate(r.title || r.body, 80)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {hasMore && lastReceivedAt && (
        <div class="pager">
          <a href={qs({ before: lastReceivedAt })}>next page →</a>
        </div>
      )}
    </Layout>
  );
}

/* -------------------------------------------------------------- Incidents */

export function IncidentsListPage(props: { rows: IncidentRow[]; state?: string }) {
  const { rows, state } = props;
  return (
    <Layout title="Incidents" active={`${UI_ROOT}/incidents`}>
      <h1>Incidents</h1>
      <div class="filters">
        <a href={`${UI_ROOT}/incidents`} class={!state ? "active" : ""}>
          open + escalated
        </a>
        {["resolved", "snoozed", "expired"].map((s) => (
          <a href={`${UI_ROOT}/incidents?state=${s}`} class={state === s ? "active" : ""}>
            {s}
          </a>
        ))}
      </div>
      {rows.length === 0 ? (
        <p class="empty">No incidents in this view.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>opened</th>
              <th>state</th>
              <th>session</th>
              <th>dedupe class</th>
              <th>llm runs</th>
              <th>summary</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((i) => (
              <tr>
                <td>{fmtTs(i.opened_at)}</td>
                <td>
                  <span class="chip">{i.state}</span>
                </td>
                <td>{sessionLabel(i)}</td>
                <td class="muted">{i.dedupe_class || "—"}</td>
                <td>{i.llm_runs}</td>
                <td>
                  <a href={`${UI_ROOT}/incidents/${i.id}`}>{truncate(i.summary || i.id, 100)}</a>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Layout>
  );
}

export function IncidentDetailPage(props: { chain: IncidentChain }) {
  const { chain } = props;
  const { incident, events, decisions, actions, escalations, outcomes, audit } = chain;
  return (
    <Layout title={`Incident ${incident.id}`} active={`${UI_ROOT}/incidents`}>
      <h1>
        Incident <code>{incident.id}</code> <span class="chip">{incident.state}</span>
      </h1>
      <p class="muted">
        opened {fmtTs(incident.opened_at)} · session {sessionLabel(incident)}
        {incident.dedupe_class ? ` · dedupe ${incident.dedupe_class}` : ""} · {incident.llm_runs} LLM run(s)
      </p>
      {incident.summary && <p>{incident.summary}</p>}

      <div class="section">
        <h2>1. Events ({events.length})</h2>
        {events.length === 0 ? (
          <p class="empty">No events attached.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>ts</th>
                <th>type</th>
                <th>severity</th>
                <th>requires response</th>
                <th>title / body</th>
              </tr>
            </thead>
            <tbody>
              {events.map((e) => (
                <tr>
                  <td>{fmtTs(e.ts)}</td>
                  <td>{e.type}</td>
                  <td>{severityChip(e.severity)}</td>
                  <td>{e.requires_response ? "yes" : "no"}</td>
                  <td>
                    <strong>{e.title}</strong>
                    {e.body && <div class="muted">{truncate(e.body, 200)}</div>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div class="section">
        <h2>2. Decisions ({decisions.length})</h2>
        {decisions.length === 0 ? (
          <p class="empty">No decisions yet.</p>
        ) : (
          decisions.map((d) => (
            <div class="section">
              <p>
                <span class="chip">{d.decided_by}</span> → <strong>{d.disposition}</strong>
                {d.action_class && <> ({d.action_class})</>} <span class="muted">{fmtTs(d.created_at)}</span>
              </p>
              <p>{d.rationale}</p>
              <p class="muted">
                {d.model && <>model {d.model} · </>}
                {d.tokens_in}/{d.tokens_out} tokens · ${d.cost_usd.toFixed(4)}
              </p>
              {d.action_args_json && <pre>{pretty(d.action_args_json)}</pre>}
              {actions
                .filter((a) => a.decision_id === d.id)
                .map((a) => (
                  <p class="muted">
                    action <code>{a.class}</code> → verdict <span class="chip">{a.policy_verdict}</span>, state{" "}
                    <span class="chip">{a.state}</span>
                    {a.result_json && <pre>{pretty(a.result_json)}</pre>}
                  </p>
                ))}
            </div>
          ))
        )}
      </div>

      <div class="section">
        <h2>3. Escalations ({escalations.length})</h2>
        {escalations.length === 0 ? (
          <p class="empty">No escalations.</p>
        ) : (
          escalations.map((e) => (
            <div class="section">
              <p>
                {severityChip(e.severity)} <span class="chip">{e.state}</span>{" "}
                <span class="muted">{fmtTs(e.created_at)}</span>
              </p>
              <p>
                <strong>Q:</strong> {e.question}
              </p>
              {e.suggested_action_json && (
                <p class="muted">
                  suggested: <code>{pretty(e.suggested_action_json)}</code>
                </p>
              )}
              {e.answer_json ? (
                <p>
                  <strong>A</strong> ({e.answered_by}, {e.answered_at && fmtTs(e.answered_at)}):{" "}
                  {pretty(e.answer_json)}
                </p>
              ) : (
                <p class="muted">unanswered</p>
              )}
            </div>
          ))
        )}
      </div>

      <div class="section">
        <h2>4. Outcomes ({outcomes.length})</h2>
        {outcomes.length === 0 ? (
          <p class="empty">No outcomes recorded.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>ts</th>
                <th>verdict</th>
                <th>note</th>
              </tr>
            </thead>
            <tbody>
              {outcomes.map((o) => (
                <tr>
                  <td>{fmtTs(o.created_at)}</td>
                  <td>
                    <span class="chip">{o.verdict}</span>
                  </td>
                  <td>{o.note || (o.david_action_json && pretty(o.david_action_json)) || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div class="section">
        <h2>5. Audit trail ({audit.length})</h2>
        {audit.length === 0 ? (
          <p class="empty">No audit rows.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>ts</th>
                <th>actor</th>
                <th>verb</th>
                <th>object</th>
                <th>detail</th>
              </tr>
            </thead>
            <tbody>
              {audit.map((a) => (
                <tr>
                  <td>{fmtTs(a.ts)}</td>
                  <td>{a.actor}</td>
                  <td>{a.verb}</td>
                  <td>
                    <code>
                      {a.object_type}:{a.object_id}
                    </code>
                  </td>
                  <td>
                    <code>{truncate(a.detail_json, 120)}</code>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </Layout>
  );
}

/* ----------------------------------------------------------------- Memory */

function scopeLabel(row: MemoryRow): string {
  try {
    const scope = JSON.parse(row.scope_json) as Record<string, unknown>;
    const parts = Object.entries(scope).map(([k, v]) => `${k}=${v}`);
    return parts.length ? parts.join(", ") : "(any)";
  } catch {
    return row.scope_json;
  }
}

/**
 * One human-readable line per memory. This is the page David audits his grants
 * on, so it leads with the sentence a person wrote or was asked — a rule minted
 * from a Telegram tap carries the original question, and showing only its match
 * key (a dedupe-class hash) would make a granted rule unreviewable.
 */
function contentSummary(row: MemoryRow): string {
  try {
    const content = JSON.parse(row.content_json) as Record<string, unknown>;
    const prose = ["text", "statement", "question"]
      .map((key) => content[key])
      .find((v): v is string => typeof v === "string" && v.trim().length > 0);
    const actionClass = typeof content.action_class === "string" ? content.action_class : null;

    if (prose) return truncate(actionClass ? `${prose} → ${actionClass}` : prose, 160);

    const parts: string[] = [];
    if (content.match !== undefined) parts.push(`if ${JSON.stringify(content.match)}`);
    if (typeof content.disposition === "string") parts.push(`then ${content.disposition}`);
    if (actionClass) parts.push(`(${actionClass})`);
    if (parts.length) return truncate(parts.join(" "), 160);
    return truncate(JSON.stringify(content), 160);
  } catch {
    return truncate(row.content_json, 160);
  }
}

export function MemoryPage(props: {
  rules: MemoryRow[];
  notes: MemoryRow[];
  pending: MemoryRow[];
  charter: string;
  charterPath: string;
  noteAdded?: boolean;
}) {
  const { rules, notes, pending, charter, charterPath, noteAdded } = props;
  return (
    <Layout title="Memory" active={`${UI_ROOT}/memory`}>
      <h1>Memory</h1>

      <div class="section">
        <h2>Rules ({rules.length})</h2>
        {rules.length === 0 ? (
          <p class="empty">No active rules.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>scope</th>
                <th>content</th>
                <th>confidence</th>
                <th>evidence +/-</th>
                <th>autonomy</th>
                <th>status</th>
                <th>actions</th>
              </tr>
            </thead>
            <tbody>
              {rules.map((r) => (
                <tr>
                  <td class="muted">{scopeLabel(r)}</td>
                  <td>{contentSummary(r)}</td>
                  <td>{r.confidence.toFixed(2)}</td>
                  <td>
                    +{r.evidence_confirm} / -{r.evidence_override}
                  </td>
                  <td>{autonomyChip(r.autonomy)}</td>
                  <td class="muted">{r.status}</td>
                  <td class="actions-cell">
                    {r.autonomy !== "none" && (
                      <form class="inline" method="post" action={`${UI_ROOT}/memory/${r.id}/demote`}>
                        <button type="submit" title="demote one step: granted→suggest→none">
                          demote
                        </button>
                      </form>
                    )}{" "}
                    <form class="inline" method="post" action={`${UI_ROOT}/memory/${r.id}/archive`}>
                      <button type="submit" class="danger">
                        archive
                      </button>
                    </form>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <p class="muted">
          Promotion to <span class="chip autonomy-granted">granted</span> is a one-tap Telegram flow only; it is not
          offered here.
        </p>
      </div>

      <div class="section">
        <h2>Pending review ({pending.length})</h2>
        {pending.length === 0 ? (
          <p class="empty">Nothing pending.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>tier</th>
                <th>scope</th>
                <th>content</th>
                <th>authored by</th>
                <th>actions</th>
              </tr>
            </thead>
            <tbody>
              {pending.map((p) => (
                <tr>
                  <td>{p.tier}</td>
                  <td class="muted">{scopeLabel(p)}</td>
                  <td>{contentSummary(p)}</td>
                  <td>{p.authored_by}</td>
                  <td class="actions-cell">
                    <form class="inline" method="post" action={`${UI_ROOT}/memory/${p.id}/approve`}>
                      <button type="submit">approve</button>
                    </form>{" "}
                    <form class="inline" method="post" action={`${UI_ROOT}/memory/${p.id}/reject`}>
                      <button type="submit" class="danger">
                        reject
                      </button>
                    </form>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div class="section">
        <h2>Notes ({notes.length})</h2>
        {noteAdded && <p class="muted">Note added.</p>}
        <form method="post" action={`${UI_ROOT}/memory/notes`} class="filters">
          <input type="text" name="text" placeholder="new note…" required style="flex: 1 1 320px;" />
          <input type="text" name="vendor" placeholder="vendor (optional scope)" />
          <input type="text" name="repo" placeholder="repo (optional scope)" />
          <button type="submit">Add note</button>
        </form>
        {notes.length === 0 ? (
          <p class="empty">No notes.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>created</th>
                <th>scope</th>
                <th>content</th>
                <th>by</th>
              </tr>
            </thead>
            <tbody>
              {notes.map((n) => (
                <tr>
                  <td>{fmtTs(n.created_at)}</td>
                  <td class="muted">{scopeLabel(n)}</td>
                  <td>{contentSummary(n)}</td>
                  <td>{n.authored_by}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div class="section">
        <h2>Charter</h2>
        <p class="muted">{charterPath} (read-only here; edit the file to change it)</p>
        <textarea rows={12} readonly>
          {charter || "(no charter file found)"}
        </textarea>
      </div>
    </Layout>
  );
}

/* ----------------------------------------------------------------- Policy */

export function PolicyPage(props: { path: string; raw: string | null; parseOk: boolean; parseError?: string }) {
  const { path, raw, parseOk, parseError } = props;
  return (
    <Layout title="Policy" active={`${UI_ROOT}/policy`}>
      <h1>Policy</h1>
      <p class="muted">{path}</p>
      {raw === null ? (
        <p class="empty">No policy.toml found at this path. The daemon runs escalate-only until one exists.</p>
      ) : (
        <>
          <p>
            parse status:{" "}
            {parseOk ? <span class="chip autonomy-granted">ok</span> : <span class="chip severity-urgent">error</span>}
          </p>
          {!parseOk && parseError && <pre>{parseError}</pre>}
          <p class="muted">Read-only in v1 — edit the file on disk; the daemon hot-reloads it.</p>
          <pre>{raw}</pre>
        </>
      )}
    </Layout>
  );
}

/* ---------------------------------------------------------------- Digests */

export function DigestsPage(props: { digests: DigestRow[] }) {
  const { digests } = props;
  return (
    <Layout title="Digests" active={`${UI_ROOT}/digests`}>
      <h1>Digest archive</h1>
      {digests.length === 0 ? (
        <p class="empty">No digests recorded yet.</p>
      ) : (
        digests.map((d) => (
          <div class="section">
            <h3>
              {d.day} {d.sent_at ? <span class="muted">sent {fmtTs(d.sent_at)}</span> : <span class="chip">unsent</span>}
            </h3>
            <pre>{d.rendered_md}</pre>
          </div>
        ))
      )}
    </Layout>
  );
}

/* --------------------------------------------------------------- NotFound */

export function NotFoundPage(what: string) {
  return (
    <Layout title="Not found" active="">
      <h1>Not found</h1>
      <p>{what}</p>
    </Layout>
  );
}
