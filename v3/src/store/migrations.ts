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
    state TEXT NOT NULL DEFAULT 'pending',         -- legacy v1 values; later migrations use the closed outbox lifecycle
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
  // v2 — durable v3 identity, fencing, lifecycle, and human-authority tables.
  // This migration deliberately keeps the v2-compatible columns/tables alive:
  // existing readers can continue during the one-way migration window while new
  // code uses the typed repositories in db.ts.
  `
  PRAGMA foreign_keys = OFF;

  DROP INDEX IF EXISTS idx_events_triage;
  DROP INDEX IF EXISTS idx_events_session;
  ALTER TABLE events RENAME TO events_v1;
  CREATE TABLE events (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL,
    source_id TEXT NOT NULL DEFAULT 'legacy',
    payload_sha256 TEXT,
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
    actor TEXT NOT NULL DEFAULT 'external',
    triage_state TEXT NOT NULL DEFAULT 'pending',
    triage_lease_until TEXT,
    triage_claim_owner TEXT,
    triage_claim_token TEXT,
    route_state TEXT NOT NULL DEFAULT 'pending',
    route_claim_owner TEXT,
    route_claim_token TEXT,
    route_lease_until TEXT,
    incident_id TEXT,
    source_vendor TEXT NOT NULL,
    source_host TEXT NOT NULL,
    source_adapter TEXT NOT NULL
  );
  INSERT INTO events (
    id, idempotency_key, source_id, payload_sha256, car_session_id, type, severity,
    ts, received_at, requires_response, response_channel_json, title, body,
    payload_json, expires_at, actor, triage_state, triage_lease_until, incident_id,
    source_vendor, source_host, source_adapter
  )
  SELECT id, idempotency_key,
    source_vendor || char(0) || source_host || char(0) || source_adapter,
    NULL, car_session_id, type, severity, ts, received_at, requires_response,
    response_channel_json, title, body, payload_json, expires_at, actor,
    triage_state, triage_lease_until, incident_id, source_vendor, source_host,
    source_adapter
  FROM events_v1;
  DROP TABLE events_v1;
  CREATE UNIQUE INDEX events_source_key ON events(source_id, idempotency_key);
  CREATE INDEX idx_events_triage ON events(triage_state, received_at);
  CREATE INDEX idx_events_route ON events(route_state, received_at);
  CREATE INDEX idx_events_session ON events(car_session_id, received_at);

  CREATE TABLE idempotency (
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    payload_sha256 TEXT,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope, key)
  );
  CREATE INDEX idx_idempotency_object ON idempotency(object_type, object_id);
  INSERT INTO idempotency(scope, key, payload_sha256, object_type, object_id, created_at)
    SELECT source_id, idempotency_key, NULL, 'event', id, received_at FROM events;

  CREATE TABLE provider_invocations (
    id TEXT PRIMARY KEY,
    incident_id TEXT,
    provider_id TEXT NOT NULL,
    provider_instance TEXT NOT NULL,
    provider_version TEXT NOT NULL,
    capability TEXT NOT NULL,
    request_id TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    terminal_outcome TEXT,
    recovery_state TEXT,
    claim_owner TEXT,
    claim_token TEXT,
    lease_until TEXT,
    started_at TEXT,
    finished_at TEXT,
    response_ref TEXT,
    error_json TEXT,
    created_at TEXT NOT NULL DEFAULT ''
  );
  CREATE UNIQUE INDEX provider_invocations_request ON provider_invocations(provider_instance, request_id);
  CREATE INDEX provider_invocations_ready ON provider_invocations(state, lease_until, created_at);

  CREATE TABLE effects (
    id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL UNIQUE,
    decision_id TEXT,
    type TEXT NOT NULL,
    args_json TEXT NOT NULL DEFAULT '{}',
    args_sha256 TEXT NOT NULL,
    lineage_id TEXT NOT NULL,
    provider_policy_verdict TEXT,
    safety_verdict TEXT NOT NULL DEFAULT 'pending',
    grant_id TEXT,
    dedupe_hash TEXT,
    state TEXT NOT NULL DEFAULT 'proposed',
    terminal_outcome TEXT,
    claim_owner TEXT,
    claim_token TEXT,
    lease_until TEXT,
    recovery_state TEXT,
    started_at TEXT,
    finished_at TEXT,
    result_json TEXT,
    created_at TEXT NOT NULL DEFAULT ''
  );
  CREATE INDEX effects_ready ON effects(state, lease_until, created_at);

  CREATE TABLE interactions (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    kind TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'received',
    expires_at TEXT,
    terminal_outcome TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_id, idempotency_key)
  );
  CREATE INDEX interactions_state ON interactions(state, updated_at);

  CREATE TABLE human_facts (
    id TEXT PRIMARY KEY,
    interaction_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    body_json TEXT NOT NULL DEFAULT '{}',
    lineage_id TEXT,
    created_at TEXT NOT NULL
  );

  CREATE TABLE grants (
    id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL UNIQUE,
    lineage_id TEXT,
    scope_json TEXT NOT NULL DEFAULT '{}',
    effect_type TEXT NOT NULL,
    constraint_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',
    uses_remaining INTEGER,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    consumed_at TEXT,
    revoked_at TEXT,
    provenance_json TEXT NOT NULL DEFAULT '{}'
  );
  CREATE INDEX grants_active ON grants(status, effect_type, expires_at);

  ALTER TABLE outbox ADD COLUMN intent_id TEXT;
  ALTER TABLE outbox ADD COLUMN route_json TEXT NOT NULL DEFAULT '{}';
  ALTER TABLE outbox ADD COLUMN claim_owner TEXT;
  ALTER TABLE outbox ADD COLUMN claim_token TEXT;
  ALTER TABLE outbox ADD COLUMN lease_until TEXT;
  ALTER TABLE outbox ADD COLUMN remote_idempotency_key TEXT;
  ALTER TABLE outbox ADD COLUMN recovery_state TEXT;
  ALTER TABLE outbox ADD COLUMN result_json TEXT;
  CREATE UNIQUE INDEX outbox_intent_channel ON outbox(intent_id, channel) WHERE intent_id IS NOT NULL;
  CREATE INDEX outbox_claimable ON outbox(state, next_attempt_at, lease_until);

  PRAGMA foreign_keys = ON;
  `,
  // v3 — dedicated effect context columns. lineage_id remains populated for
  // compatibility with the first safety adapter; new repositories use the
  // typed JSON columns so scope/lineage cannot be mistaken for one another.
  `
  ALTER TABLE effects ADD COLUMN scope_json TEXT NOT NULL DEFAULT '{}';
  ALTER TABLE effects ADD COLUMN lineage_json TEXT;
  ALTER TABLE effects ADD COLUMN deadline_at TEXT;
  ALTER TABLE effects ADD COLUMN action_class TEXT;
  ALTER TABLE effects ADD COLUMN cost_usd REAL NOT NULL DEFAULT 0;
  CREATE INDEX effects_intent ON effects(intent_id);
  `,
  // v4 — display repo labels and authorization-grade repo identities are
  // deliberately distinct. Existing/derived repo strings are unverified.
  `
  ALTER TABLE sessions ADD COLUMN repo_verified INTEGER NOT NULL DEFAULT 0;
  `,
  // v5 — one canonical daemon owner per state store. Expired leases can be
  // reclaimed after a crash; live owners cannot be silently duplicated.
  `
  CREATE TABLE daemon_leases (
    name TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    token TEXT NOT NULL,
    lease_until TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    renewed_at TEXT NOT NULL
  );
  `,
  // v6 — digest delivery is linked to the canonical outbox receipt. A digest
  // row is an archive/projection while it is pending; `sent_at` is populated
  // only by receipt reconciliation, never when the outbox row is enqueued.
  // The held ids are a snapshot so alerts arriving after a digest is built are
  // not accidentally folded into an earlier delivery.
  `
  ALTER TABLE digests ADD COLUMN outbox_id INTEGER;
  ALTER TABLE digests ADD COLUMN held_outbox_ids_json TEXT NOT NULL DEFAULT '[]';
  -- Legacy timestamps were written before a canonical outbox receipt existed;
  -- preserve the archive text but do not carry an enqueue-time claim forward as
  -- delivery evidence.
  UPDATE digests SET sent_at = NULL WHERE outbox_id IS NULL;
  CREATE UNIQUE INDEX digests_outbox_id ON digests(outbox_id) WHERE outbox_id IS NOT NULL;
  `,
  // v7 — compact, durable agent-run projection. The agentctl journal remains
  // authority for transport execution; this table is the router-owned read
  // model used for recovery visibility and the operator console. It stores
  // semantic run transitions, never the high-volume native progress stream.
  `
  CREATE TABLE agent_runs (
    execution_id TEXT PRIMARY KEY,
    transport TEXT NOT NULL DEFAULT 'agentctl',
    agent TEXT NOT NULL,
    authority TEXT,
    mode TEXT,
    state TEXT NOT NULL,
    liveness TEXT,
    labels_json TEXT NOT NULL DEFAULT '[]',
    title TEXT,
    repo TEXT,
    cwd TEXT,
    profile TEXT,
    model TEXT,
    runtime TEXT,
    continuation_supported INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    updated_at TEXT NOT NULL,
    terminal_at TEXT,
    duration_seconds REAL,
    status_revision INTEGER NOT NULL DEFAULT 0,
    observation_state TEXT NOT NULL DEFAULT 'observed',
    last_meaningful_update TEXT,
    raw_json TEXT NOT NULL DEFAULT '{}',
    first_observed_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL
  );
  CREATE INDEX idx_agent_runs_state ON agent_runs(state, updated_at DESC);
  CREATE INDEX idx_agent_runs_observation ON agent_runs(observation_state, updated_at DESC);
  `,
];
