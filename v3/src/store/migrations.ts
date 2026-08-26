/** Schema migrations, applied via PRAGMA user_version. FROZEN: schema changes go through the coordinator. */

export const MIGRATIONS: string[] = [
  // v1 — initial schema
  `
  CREATE TABLE events (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    car_session_id TEXT,
    type TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',
    ts TEXT NOT NULL,
    received_at TEXT NOT NULL,
    requires_response INTEGER NOT NULL DEFAULT 0,
    response_channel_json TEXT,
    title TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    expires_at TEXT,
    actor TEXT NOT NULL DEFAULT 'external',        -- external | car
    triage_state TEXT NOT NULL DEFAULT 'pending',  -- pending|coalescing|rules_resolved|llm_resolved|escalated|expired|skipped
    triage_lease_until TEXT,
    incident_id TEXT,
    source_vendor TEXT NOT NULL,
    source_host TEXT NOT NULL,
    source_adapter TEXT NOT NULL
  );
  CREATE INDEX idx_events_triage ON events(triage_state, received_at);
  CREATE INDEX idx_events_session ON events(car_session_id, received_at);

  CREATE TABLE sessions (
    car_session_id TEXT PRIMARY KEY,
    vendor TEXT NOT NULL,
    host TEXT NOT NULL,
    title TEXT,
    cwd TEXT,
    repo TEXT,
    state TEXT NOT NULL DEFAULT 'active',          -- active|ended|stale
    first_seen TEXT NOT NULL,
    last_event_at TEXT NOT NULL,
    last_heartbeat_at TEXT,
    expected_heartbeat_s INTEGER,
    telegram_thread_id TEXT,
    muted_until TEXT
  );

  CREATE TABLE session_refs (
    vendor TEXT NOT NULL,
    host TEXT NOT NULL,
    native_id TEXT NOT NULL,
    car_session_id TEXT NOT NULL,
    PRIMARY KEY (vendor, host, native_id)
  );
  CREATE INDEX idx_session_refs_car ON session_refs(car_session_id);

  CREATE TABLE incidents (
    id TEXT PRIMARY KEY,
    car_session_id TEXT,
    opened_by_event TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open',            -- open|resolved|escalated|snoozed|expired
    snooze_until TEXT,
    summary TEXT NOT NULL DEFAULT '',
    telegram_message_id TEXT,
    dedupe_class TEXT,
    llm_runs INTEGER NOT NULL DEFAULT 0,
    opened_at TEXT NOT NULL,
    closed_at TEXT
  );
  CREATE INDEX idx_incidents_state ON incidents(state, opened_at);
  CREATE INDEX idx_incidents_session ON incidents(car_session_id, opened_at);

  CREATE TABLE decisions (
    id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    decided_by TEXT NOT NULL,                      -- rules|llm|human
    disposition TEXT NOT NULL,                     -- auto_resolve|keep_informed|escalate|defer
    action_class TEXT,
    action_args_json TEXT,
    rationale TEXT NOT NULL DEFAULT '',
    model TEXT,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
  );
  CREATE INDEX idx_decisions_incident ON decisions(incident_id);

  CREATE TABLE escalations (
    id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    severity TEXT NOT NULL,
    question TEXT NOT NULL,
    suggested_action_json TEXT,
    state TEXT NOT NULL DEFAULT 'pending',         -- pending|answered|snoozed|expired|superseded
    telegram_message_id TEXT,
    sent_at TEXT,
    answered_by TEXT,
    answer_json TEXT,
    answered_at TEXT,
    created_at TEXT NOT NULL
  );
  CREATE INDEX idx_escalations_state ON escalations(state, created_at);

  CREATE TABLE actions (
    id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL,
    class TEXT NOT NULL,
    args_json TEXT NOT NULL DEFAULT '{}',
    policy_verdict TEXT NOT NULL,                  -- auto|escalate|forbid|blocked_dedupe|blocked_rate|blocked_breaker
    dedupe_hash TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',         -- pending|running|ok|failed
    started_at TEXT,
    finished_at TEXT,
    result_json TEXT
  );
  CREATE INDEX idx_actions_dedupe ON actions(dedupe_hash, started_at);
  CREATE INDEX idx_actions_state ON actions(state);

  CREATE TABLE outcomes (
    id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL,
    escalation_id TEXT,
    verdict TEXT NOT NULL,                         -- confirmed|overridden|corrected|flagged
    david_action_json TEXT,
    note TEXT,
    created_at TEXT NOT NULL
  );

  CREATE TABLE memories (
    id TEXT PRIMARY KEY,
    tier TEXT NOT NULL,                            -- rule|note|episode
    scope_json TEXT NOT NULL DEFAULT '{}',
    kind TEXT NOT NULL,
    content_json TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.5,
    evidence_confirm INTEGER NOT NULL DEFAULT 0,
    evidence_override INTEGER NOT NULL DEFAULT 0,
    autonomy TEXT NOT NULL DEFAULT 'none',         -- none|suggest|granted
    status TEXT NOT NULL DEFAULT 'active',         -- active|pending|archived|dormant
    authored_by TEXT NOT NULL,                     -- david|triage|consolidator|outcome
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_reinforced_at TEXT,
    last_used_at TEXT,
    use_count INTEGER NOT NULL DEFAULT 0,
    supersedes TEXT,
    provenance_json TEXT
  );
  CREATE INDEX idx_memories_status ON memories(status, tier);
  CREATE VIRTUAL TABLE memories_fts USING fts5(memory_id UNINDEXED, content, scope_text);

  CREATE TABLE outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,                         -- telegram|webhook
    target_json TEXT NOT NULL DEFAULT '{}',
    body_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',         -- pending|sent|failed|dead|deferred (held for the digest)
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    sent_message_id TEXT,
    created_at TEXT NOT NULL
  );
  CREATE INDEX idx_outbox_pending ON outbox(state, next_attempt_at);

  CREATE TABLE audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,                           -- daemon|rules|llm|david|adapter:<name>
    verb TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
  );
  CREATE INDEX idx_audit_object ON audit(object_type, object_id);
  CREATE INDEX idx_audit_ts ON audit(ts);

  CREATE TABLE spend (
    day TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (day, provider, model)
  );

  CREATE TABLE digests (
    day TEXT PRIMARY KEY,
    rendered_md TEXT NOT NULL,
    sent_at TEXT
  );

  CREATE TABLE kv (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
  );
  `,
];
