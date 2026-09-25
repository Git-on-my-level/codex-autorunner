/**
 * Pre-release bootstrap, not a data-conversion history.
 * No v3 deployment has been released. Unknown/nonempty schemas fail closed;
 * startup never guesses a migration, imports v2 authority, or deletes state.
 * See docs/architecture/0003-pre-release-foundation.md before changing this.
 */
export const SCHEMA_VERSION = 1;
export const APPLICATION_ID = 0x43415233; // CAR3
export const SCHEMA_SQL = `
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

CREATE TABLE attention_requests (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    client_id TEXT NOT NULL,
    host TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    initial_hash TEXT NOT NULL,
    packet_json TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    state TEXT NOT NULL CHECK (state IN ('preparing','needs_you','answered','received','resolved','cancelled','expired')),
    preparation_rounds INTEGER NOT NULL DEFAULT 0 CHECK (preparation_rounds >= 0),
    prepare_by TEXT NOT NULL,
    due_at TEXT,
    event_id TEXT UNIQUE,
    incident_id TEXT,
    escalation_id TEXT UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    closed_at TEXT,
    close_reason TEXT,
    reviewed_at TEXT,
    review_note TEXT,
    UNIQUE(workspace_id, client_id, idempotency_key)
  );

CREATE TABLE attention_triage_runs (
    request_id TEXT NOT NULL REFERENCES attention_requests(id),
    revision INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('running','complete','failed','unknown')),
    proposal_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    PRIMARY KEY(request_id, revision)
  );

CREATE TABLE audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,                           -- daemon|rules|llm|david|adapter:<name>
    verb TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
  );

CREATE TABLE daemon_leases (
    name TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    token TEXT NOT NULL,
    lease_until TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    renewed_at TEXT NOT NULL
  );

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

CREATE TABLE digests (
    day TEXT PRIMARY KEY,
    rendered_md TEXT NOT NULL,
    sent_at TEXT,
    outbox_id INTEGER,
    held_outbox_ids_json TEXT NOT NULL DEFAULT '[]'
  );

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
    created_at TEXT NOT NULL DEFAULT '',
    scope_json TEXT NOT NULL DEFAULT '{}',
    lineage_json TEXT,
    deadline_at TEXT,
    action_class TEXT,
    cost_usd REAL NOT NULL DEFAULT 0
  );

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
    created_at TEXT NOT NULL,
    origin_event_id TEXT
  );

CREATE TABLE events (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL,
    source_id TEXT NOT NULL DEFAULT 'native',
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
    source_adapter TEXT NOT NULL,
    obligation_state TEXT NOT NULL DEFAULT 'open'
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

CREATE TABLE human_replies (
    id TEXT PRIMARY KEY,
    escalation_id TEXT UNIQUE,
    request_id TEXT UNIQUE REFERENCES attention_requests(id),
    incident_id TEXT,
    event_id TEXT,
    car_session_id TEXT,
    response_channel_json TEXT,
    payload_json TEXT NOT NULL,
    actor TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending','delivering','delivered','staged','uncertain','failed','acknowledged','resolved','cancelled','expired')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    acknowledged_at TEXT,
    resolved_at TEXT
  );

CREATE TABLE idempotency (
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    payload_sha256 TEXT,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope, key)
  );

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

CREATE TABLE kv (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
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

CREATE VIRTUAL TABLE memories_fts USING fts5(memory_id UNINDEXED, content, scope_text);

CREATE TABLE outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,                         -- telegram|webhook
    target_json TEXT NOT NULL DEFAULT '{}',
    body_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',         -- delivery state; closed lifecycle transitions are enforced by Store
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    sent_message_id TEXT,
    created_at TEXT NOT NULL,
    intent_id TEXT,
    route_json TEXT NOT NULL DEFAULT '{}',
    claim_owner TEXT,
    claim_token TEXT,
    lease_until TEXT,
    remote_idempotency_key TEXT,
    recovery_state TEXT,
    result_json TEXT
  );

CREATE TABLE outcomes (
    id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL,
    escalation_id TEXT,
    verdict TEXT NOT NULL,                         -- confirmed|overridden|corrected|flagged
    david_action_json TEXT,
    note TEXT,
    created_at TEXT NOT NULL
  );

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

CREATE TABLE session_refs (
    vendor TEXT NOT NULL,
    host TEXT NOT NULL,
    native_id TEXT NOT NULL,
    car_session_id TEXT NOT NULL,
    PRIMARY KEY (vendor, host, native_id)
  );

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
    muted_until TEXT,
    repo_verified INTEGER NOT NULL DEFAULT 0
  );

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

CREATE UNIQUE INDEX digests_outbox_id ON digests(outbox_id) WHERE outbox_id IS NOT NULL;

CREATE INDEX effects_intent ON effects(intent_id);

CREATE INDEX effects_ready ON effects(state, lease_until, created_at);

CREATE UNIQUE INDEX events_source_key ON events(source_id, idempotency_key);

CREATE INDEX grants_active ON grants(status, effect_type, expires_at);

CREATE INDEX idx_actions_dedupe ON actions(dedupe_hash, started_at);

CREATE INDEX idx_actions_state ON actions(state);

CREATE INDEX idx_agent_runs_observation ON agent_runs(observation_state, updated_at DESC);

CREATE INDEX idx_agent_runs_state ON agent_runs(state, updated_at DESC);

CREATE INDEX idx_attention_client ON attention_requests(workspace_id, client_id, updated_at);

CREATE INDEX idx_attention_state ON attention_requests(workspace_id, state, updated_at);

CREATE INDEX idx_audit_object ON audit(object_type, object_id);

CREATE INDEX idx_audit_ts ON audit(ts);

CREATE INDEX idx_decisions_incident ON decisions(incident_id);

CREATE INDEX idx_escalation_origin ON escalations(origin_event_id, state);

CREATE INDEX idx_escalations_state ON escalations(state, created_at);

CREATE INDEX idx_events_obligation ON events(requires_response, obligation_state, received_at);

CREATE INDEX idx_events_route ON events(route_state, received_at);

CREATE INDEX idx_events_session ON events(car_session_id, received_at);

CREATE INDEX idx_events_triage ON events(triage_state, received_at);

CREATE INDEX idx_human_replies_event ON human_replies(event_id);

CREATE INDEX idx_human_replies_pending ON human_replies(state, created_at);

CREATE INDEX idx_idempotency_object ON idempotency(object_type, object_id);

CREATE INDEX idx_incidents_session ON incidents(car_session_id, opened_at);

CREATE INDEX idx_incidents_state ON incidents(state, opened_at);

CREATE INDEX idx_memories_status ON memories(status, tier);

CREATE INDEX idx_outbox_pending ON outbox(state, next_attempt_at);

CREATE INDEX idx_session_refs_car ON session_refs(car_session_id);

CREATE INDEX interactions_state ON interactions(state, updated_at);

CREATE INDEX outbox_claimable ON outbox(state, next_attempt_at, lease_until);

CREATE UNIQUE INDEX outbox_intent_channel ON outbox(intent_id, channel) WHERE intent_id IS NOT NULL;

CREATE INDEX provider_invocations_ready ON provider_invocations(state, lease_until, created_at);

CREATE UNIQUE INDEX provider_invocations_request ON provider_invocations(provider_instance, request_id);

CREATE INDEX idx_attention_chronology ON attention_requests(workspace_id, client_id, host, created_at DESC, id DESC);
CREATE INDEX idx_attention_review ON attention_requests(workspace_id, state, reviewed_at, due_at);

`;
