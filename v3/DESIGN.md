# CAR v3 — Design

**CAR v3 is a cross-vendor attention & escalation control plane.** It is the layer
between you and all of your agents (Claude Code, Codex, Hermes, OMP, agentctl-launched
work, Multica autopilots, CI, cron). It ingests lifecycle and attention events from
everything, triages autonomously on your behalf — attempting to unblock agents itself —
keeps you informed, and escalates to you only when it cannot unblock. One bot in
Telegram (Discord later) is the primary UX; a minimal localhost web UI is the
power-user surface. Single source of truth for "what needs me."

v3 replaces v2's runner/tickets/chat-surfaces product (now commoditized by vendor-native
features). The one v2 primitive that survives, generalized, is **Dispatch**: an agent's
`notify` vs `pause` (yield-for-human with a durable reply inbox) becomes the
`requires_response` field of the event contract, and the file reply inbox becomes the
universal reply-back fallback.

Design lineage: this doc merges two independent Fable design proposals (systems-lead and
brain/UX-lead) plus prior analysis of v2, agentctl, fleetctl, and hostctl. Patterns
deliberately stolen: agentctl (journal + `subscribe` callbacks, capability probing,
typed JSON-first CLI), fleetctl (unconditional daily digest, `/brief.md` endpoint for
agents to curl, stateless localhost UI), hostctl (behavior/config split, closed
vocabulary of typed action gates), v2 PMA safety (dedupe, rate limits, circuit breaker).

## Non-negotiables

1. **Durable SoT**: SQLite, crash-only daemon. Kill -9 at any point; restart resumes
   from table state. No authoritative in-memory queues.
2. **Unconditional daily digest**: silence is never ambiguous. (v2's founding failure:
   repos sat "degraded" in Telegram for two months, unnoticed.)
3. **Silence watchdog**: absence of expected events is itself an event.
4. **Autonomy is granted, never inferred**: memory can raise CAR's *suggestion*
   confidence automatically; crossing from "suggest" to "act without asking" requires an
   explicit one-tap grant from David, and one overridden outcome demotes it back.
5. **Policy is enforced by the executor, not the prompt**: anything not matching an
   enabled action class + template is impossible, not discouraged.
6. **Failed delivery re-escalates**: a reply that can't reach its agent is loudly
   reported, never dropped.
7. **Side-by-side with v2**: own state dir (`~/.car/`), own port (7171), own bot token,
   CLI named `card`. The live v2 hub keeps running untouched.

## 1. Layout

Single Bun package at `v3/` (no workspace — one process, layered modules, an import
boundary keeps layers honest). Legacy Python/Svelte tree untouched this PR; root README
gains a v3 section. Root `package.json`/`pnpm-workspace.yaml` (legacy web UI) untouched.

```
v3/
  package.json  bunfig.toml  tsconfig.json  DESIGN.md (this file)
  schemas/                  # JSON Schema emitted from contract (generated, committed)
  src/
    contract/     # wire types + zod schemas + ids. FROZEN after scaffold. No internal imports.
    store/        # bun:sqlite, migrations, typed repositories. FROZEN interfaces after scaffold.
    config/       # TOML config + policy.toml load/validate/hot-reload
    ingest/       # Hono routes + per-source normalizers (agentctl, claude, multica, generic, telegram-note)
    triage/       # rules pass, coalescer, LLM loop, tools, safety (breaker/dedupe/spend)
    policy/       # action-class vocabulary, gates, budget accounting
    memory/       # charter + rules/notes/episodes, outcome recorder, consolidation
    actions/      # reply-back + exec adapters: claude-code, codex, agentctl, multica, file
    surfaces/telegram/   # grammY bot: escalations, buttons, replies, commands
    surfaces/web/        # Hono server-rendered JSX + htmx; /brief.md
    digest/       # digest builder + silence watchdog
    daemon.ts     # composition root (five loops, below)
    cli.ts        # `card` CLI: serve | emit | status | memory | policy | db | doctor
  test/           # mirrors src/; test/fixtures/ = golden wire events per source
  ops/            # launchd plist + systemd unit templates + install notes
```

**Stack** (all Bun-native or pure TS; no native modules, no ORM, no SPA framework):
`bun:sqlite` (WAL), `zod` (contract), `hono` (HTTP + server-rendered JSX), `grammy`
(Telegram, long-polling — no inbound port), `ai` + `@ai-sdk/anthropic` +
`@ai-sdk/openai` (provider-abstracted LLM tool loop), `smol-toml` (config).
Dependencies are frozen at scaffold time; implementation agents may not add packages.

## 2. Event wire contract (`car.event.v1`)

`POST /v1/events` (single JSON object or NDJSON batch). Published as zod source in
`src/contract/` and emitted JSON Schema in `schemas/`; served at `GET /v1/schema`.
Additive-only within v1.

```jsonc
{
  "contract": "car.event.v1",
  "idempotency_key": "claude-code:sess-abc:PermissionRequest:toolu_01X", // REQUIRED, source-scoped
  "ts": "2026-08-26T18:04:11Z",
  "source": { "vendor": "claude-code", "host": "davids-mbp", "adapter": "hook-http" },
  "session": {                       // nullable for sessionless sources (cron, CI)
    "vendor": "claude-code",         // claude-code|codex|cursor|hermes|omp|agentctl|multica|ci|cron|other
    "native_id": "sess-abc123",
    "host": "davids-mbp",
    "cwd": "/Users/dazheng/omi",
    "repo": "github.com/x/y",        // optional
    "title": "fix BLE reconnect"
  },
  "type": "attention.permission",    // closed vocab, below
  "severity": "attention",           // info | notice | attention | urgent
  "requires_response": true,
  "response_channel": {              // how a reply can get back; null = file fallback only
    "kind": "claude-hook-http",      // | codex-exec-resume | claude-resume | agentctl-run | multica-api | file
    "hint": { "tool_use_id": "toolu_01X", "deadline_ms": 110000 }
  },
  "title": "Permission: gh pr merge 42",
  "body": "…free text ≤16KB…",
  "payload": { },                    // vendor blob ≤32KB, stored verbatim, truncated with marker
  "expires_at": "2026-08-26T18:06:00Z"  // optional; resolving after this is moot
}
```

**Types (closed enum v1):** `session.started`, `session.ended` (payload: outcome, cost
if known), `attention.permission` (approve/deny), `attention.question` (free-text
answer), `attention.idle` (waiting on input), `attention.error` (failed/crashed),
`attention.cleared`, `progress`, `artifact`, `heartbeat`, `cost.report`, `note`
(FYI / Telegram-forwarded / freeform).

**Identity & dedupe.** Server assigns ULID `event_id`. Unique index on
`idempotency_key` (at-least-once sources like agentctl subscribe are safe; duplicate
POST returns original id). Sources without native ids get server-computed
`sha256(source,type,payload)[..16]` bucketed to the minute. Ingest ACKs 200 only after
the insert commits: exactly-once processing on top of at-least-once delivery.

**Session identity.** Canonical key `(vendor, host, native_id)` → CAR mints
`car_session_id`. One CAR session may hold **multiple refs** (`session_refs` table): an
agentctl exec wrapping a codex process contributes both ids; adapters link refs when a
nested native id is learned. Escalations, Telegram threads, and memories hang off
`car_session_id`, surviving vendor id churn. `response_channel` tells the actions layer
which adapter can reach the session; adapters **probe vendor CLI capabilities at
runtime** (cached), never trust docs — e.g. installed codex-cli 0.145.0 has no
`codex queue`; the working path is `codex exec resume <uuid> "<text>"`.

**Ingest adapters (thin normalizers; anything that can `curl -d` is a source):**
- **agentctl**: `agentctl subscribe create --destination webhook --target
  http://127.0.0.1:7171/v1/ingest/agentctl` (CAR creates subscriptions for work it
  launches); plus a `recent --unreconciled` sweep as belt-and-braces.
- **Claude Code**: settings-level `type:"http"` hooks for `Notification`, `Stop`,
  `SessionStart/End`, `PermissionRequest` → `/v1/ingest/claude`. No wrapper scripts.
- **Multica**: webhook → `/v1/ingest/multica`.
- **Generic**: `POST /v1/events` raw envelope; `card emit --type attention.error
  --title "…" ` wraps it for CI/cron.
- **Telegram**: forwarded/plain messages not matching a reply flow become `note` events.

Auth: bearer token per source for non-localhost binds; localhost trusted by default.

## 3. Data model (SQLite, WAL, single file `~/.car/car.db`)

```sql
events(id PK, idempotency_key UNIQUE, car_session_id NULL, type, severity, ts, received_at,
       requires_response, response_channel_json, title, body, payload_json, expires_at,
       actor,             -- 'external' | 'car'  (loop guard: CAR tags events its own actions caused)
       triage_state,      -- pending|coalescing|rules_resolved|llm_resolved|escalated|expired|skipped
       incident_id NULL);
sessions(car_session_id PK, vendor, host, title, cwd, repo, state, first_seen,
         last_event_at, last_heartbeat_at, expected_heartbeat_s NULL, telegram_thread_id NULL);
session_refs(vendor, host, native_id, car_session_id, PRIMARY KEY(vendor,host,native_id));
incidents(id PK, car_session_id, opened_by_event, state,  -- open|resolved|escalated|snoozed|expired
          snooze_until NULL, summary, telegram_message_id NULL, opened_at, closed_at NULL);
decisions(id PK, incident_id, decided_by,   -- rules|llm|human
          disposition,                      -- auto_resolve|keep_informed|escalate|defer
          action_class NULL, action_args_json NULL, rationale, model NULL,
          tokens_in, tokens_out, cost_usd, created_at);
escalations(id PK, incident_id, severity, question, suggested_action_json NULL,
            state,                          -- pending|answered|snoozed|expired|superseded
            telegram_message_id NULL, sent_at, answered_by NULL, answer_json NULL, answered_at NULL);
actions(id PK, decision_id, class, args_json, policy_verdict, dedupe_hash,
        state,                              -- pending|running|ok|failed
        started_at, finished_at, result_json NULL);
outcomes(id PK, decision_id, escalation_id NULL, verdict,  -- confirmed|overridden|corrected|flagged
         david_action_json NULL, note NULL, created_at);
memories(id PK, tier,                       -- rule|note|episode
         scope_json,                        -- {vendor?, repo?, host?, event_type?, dedupe_class?}
         kind,                              -- rule: preference|autonomy ; note: fact ; episode: summary
         content_json, confidence REAL, evidence_confirm INT, evidence_override INT,
         autonomy,                          -- none|suggest|granted
         status,                            -- active|pending|archived|dormant
         authored_by,                       -- david|triage|consolidator|outcome
         created_at, updated_at, last_reinforced_at NULL, last_used_at NULL, use_count INT,
         supersedes NULL, provenance_json NULL);
memories_fts(FTS5 over content, scope_text);
outbox(id PK, channel, target_json, body_json, state, attempts, next_attempt_at,
       sent_message_id NULL);               -- all outbound delivery (Telegram, webhooks) goes through here
audit(id PK, ts, actor, verb, object_type, object_id, detail_json);  -- append-only; EVERYTHING writes here
spend(day, provider, model, calls, tokens_in, tokens_out, cost_usd, PRIMARY KEY(day,provider,model));
```

Triage claims events via `UPDATE … SET triage_state='coalescing', lease=… WHERE
triage_state='pending'`; leases expire so a crashed worker's claim is reclaimed.

## 4. Daemon architecture

One Bun process (`card serve`), five in-process loops over the shared store, supervised
by launchd/systemd KeepAlive:

1. **HTTP** (Hono, `127.0.0.1:7171`): ingest routes, web UI, `/brief.md`, `/healthz`,
   `/v1/schema`. The Claude `PermissionRequest` handler **parks the HTTP response**
   (deadline = hook timeout − margin) while triage/escalation races; an instant policy
   verdict or a Telegram button tap answers in-band (`{"decision":"allow"|"deny"}`);
   on deadline it returns no decision so Claude falls back to its local prompt —
   degraded, never broken.
2. **Telegram** (grammY long-poll): buttons/commands/replies → decision rows. Buttons
   only ever write rows; all real state lives in SQLite (the v2 40k-LOC bot lesson).
3. **Triage worker**: polls pending events, coalesces per session (20s debounce — an
   agent bursting error+attention+notify gets ONE triage run), runs rules then (if
   needed) the LLM loop, writes decisions/actions/escalations.
4. **Scheduler**: daily digest; snooze expiry; nightly memory consolidation; retention;
   **silence watchdog** — session with pending `requires_response` > N hours, or any
   source/session silent past 2× its expected cadence (config or learned) → synthetic
   `attention.idle` event; 4× → real escalation.
5. **Outbox deliverer**: drains `outbox` with exponential backoff; records
   `sent_message_id` for edit-in-place and reply routing.

## 5. Triage

**Deterministic first; the LLM is the exception path.** Pipeline per event:

1. Normalize + persist (always).
2. **Rules pass** (pure functions over event + policy + rule-tier memories; no LLM):
   - `heartbeat` / `progress` / `session.started` / `artifact` → keep-informed, $0.
   - `session.ended` ok → keep-informed; failed → LLM queue.
   - `note` → keep-informed unless severity ≥ attention.
   - `attention.*` matching an **autonomy=granted** rule → execute directly
     (`decided_by=rules`), one-time notify on first use of a fresh grant.
   - `severity=urgent` → escalate immediately, **no LLM between David and a page**.
   - Everything else → LLM queue.
3. **Coalesce** 20s per session.
4. **LLM triage run**: bounded tool loop (max 8 tool calls, ~60s, per-run token cap),
   must end with exactly one terminal tool.

**Tool set (closed):** `memory_search` / `memory_get` (read-only), `session_context`,
`read_session_tail` (bounded journal/transcript read), `run_probe(template_id, args)`
(read-only allowlisted templates with typed args — hostctl gate pattern),
`reply_to_agent(session_id, text)`, `approve_permission` / `deny_permission`,
`run_action(template_id, args)` (mutating allowlisted templates, executed via
`agentctl run --label car-triage` so they're journaled and observable),
`memory_propose(kind, content, scope)` (lands `status=pending`, never grants autonomy),
and terminals: `resolve(summary)`, `keep_informed(summary)`,
`escalate(severity, question, suggested_action?)`, `defer(until, reason)`.

**Policy** (`~/.car/policy.toml`; hot-reload; code defines classes, config enables):

```toml
[classes.reply]                  enabled = true,  max_per_hour = 10, max_per_session_per_hour = 3
[classes.probe]                  enabled = true,  max_per_hour = 30
[classes.approve_permission]     enabled = false  # per-rule grants only, via memory promotion
[classes.exec.restart_service]   enabled = true,  allowlist = ["multica", "forgejo"], max_per_day = 4
[classes.exec.agentctl_continue] enabled = true,  repos_deny = ["*/prod-*"]
[guards]
never_touch_branches = ["main", "master"]
quiet_hours = "23:00-08:00"      # only 'urgent' pushes; rest queue for digest
never_auto_approve = []          # extra regexes, ADDED to the built-in rail below
[budget]
triage_daily_usd = 2.00          # 80% → digest warning; 100% → escalate-only mode
[providers]
triage = "anthropic/claude-haiku-4-5"
consolidation = "anthropic/claude-sonnet-5"
```

**Runaway/spend protection** (v2 PmaSafetyChecker concepts, kept):
(a) action dedupe hash — identical `(class,args)` within 30 min → blocked, escalate;
(b) per-class rate limits; (c) **circuit breaker** — 5 failed actions in 10 min flips
escalate-only mode until David clears it via button; (d) **self-event suppression** —
events with `actor='car'` (CAR's own labeled actions) attach to the originating
incident and never open fresh LLM triage; (e) budget caps; (f) max 2 LLM runs per
incident lineage — recurrence of the same dedupe class thereafter escalates
unconditionally; (g) **the never-auto-approve rail** — an approval is matched
against `NEVER_AUTO_APPROVE` (force push, `reset --hard`, `rm -rf`, sudo,
curl-piped-to-shell, `gh pr merge`, package publish, terraform apply, kubectl
delete, DROP TABLE, prod changes, credential paths) plus any `[guards]
never_auto_approve` extras, and escalates instead. Autonomy is scoped to a repo
or a request lineage, but what makes a request dangerous is inside its text,
which no class verdict can see; the built-ins cannot be disabled from config,
because the rail costs a notification and never an outcome. All enforced in the
executor, tested explicitly.

**Providers** via Vercel AI SDK; `fake` provider (record/replay canned tool-call
scripts) is a first-class registry entry — all triage tests run against it, $0.

## 6. Memory

Charter file + three DB tiers. Everything inspectable; every memory links to the
outcomes that shaped it.

- **Charter** (`~/.car/memory/charter.md`): David-authored standing prose, always in
  the prompt (~500 tokens), never machine-edited.
- **Rules** (structured): scope selectors + `{match, disposition, action_class?,
  args_template?}` + confidence + evidence counters + **autonomy: none|suggest|granted**.
- **Notes**: scoped free-text facts.
- **Episodes**: compact decision+outcome summaries, auto-written; raw material for
  consolidation and "have we seen this before."

**Write paths (three writers, different trust):**
1. **David** (`/remember`, escalation "🧠 Always…" button, web editor): active
   immediately, decay-exempt. **Only David's taps set `autonomy=granted`.**
2. **Outcome recorder** (deterministic, no LLM): every escalation answer and digest
   👍/👎 writes `outcomes` and updates matching rules — confirm bumps confidence;
   override multiplies it down ~3× harder, and **one override demotes granted →
   suggest** with a notice to David.
3. **Triage** via `memory_propose` → `status=pending`; visible to future runs (marked
   unverified), surfaced in the digest for review.

**Promotion loop:** when a `suggest` rule matches David's actual choice N times running
(default 4, zero overrides), the next **digest** (not a page) offers:
"Auto-approve these? [Yes] [Yes, this repo only] [Keep asking]". First autonomous use
after a grant sends a one-time notify with a 👎-to-revoke. *CAR learns to suggest;
David promotes suggestions to autonomy; one bad outcome demotes.*

**Read path (deterministic, ≤2.5k tokens):** charter + scope-matched active rules
(exact vendor/repo/type match + glob — rules are few and structured, no vector search)
+ top-5 FTS notes + last 3 episodes sharing the dedupe class + last 5 outcomes for the
session. No embeddings in v1 (David-scale = thousands of rows; FTS5 + scoping wins on
inspectability; `sqlite-vec` slots behind `memory_search` later).

**Consolidation** (nightly, before digest, budget-capped): merge duplicate notes;
distill consistent episode clusters into `pending` rule proposals; exponential
confidence decay (half-life 45d) unless reinforced, < 0.2 → archived (restorable);
per-scope caps. The consolidator may merge/archive; it may **never** grant autonomy or
touch charter.md.

`card memory export` renders the store to markdown under `~/.car/memory/export/`
(a view, not a second store).

## 7. Telegram UX

grammY. Forum-supergroup mode: one topic per session; flat-chat fallback: one anchor
message per session, all traffic replies to it.

**Escalate:**
```
🔴 needs you · claude-code · omi-desktop @ mac-studio
Agent asks: force-push to fix/telemetry-cliff? Remote diverged.
CAR probed: `git status` — remote has 2 CI-authored commits.
Memory: you've denied force-push twice on this repo.
Suggests: DENY — tell agent to rebase instead.
[✅ Approve] [❌ Deny] [💬 Reply] [😴 ▾] [🧠 Always…]
```
✅/❌ execute via reply-back, edit the message in place to the resolution, and record
the outcome (the tap IS the learning signal). 💬 or any plain reply to the
anchor/topic routes verbatim to the agent — the v2 reply inbox, generalized. 😴 snooze
(1h / tonight / next digest), resurfaces bold in digest. Quiet hours: only `urgent`
breaks through.

**Keep-informed:** no push. One pinned, edited-in-place status line per active session.
Optional `/ticker on`.

**Auto-resolved:** silent, except first-use-after-grant notify.

**Daily digest (unconditional):**
```
☀️ CAR digest — Tue Aug 26
🤖 Handled (3): approved dep bump ×2 [👍/👎] · restarted forgejo [👍/👎]
🙋 You resolved (2): denied force-push · answered hermes planning q
⚠️ Stuck/silent: multica autopilot #12 — no heartbeat 26h [🔍 probe] [escalate]
💸 Spend: triage $0.41 (34 runs) · agents ~$12.30 (reported)
🧠 Memory: 2 pending learnings [review] · 1 promotion offer
```

Commands: `/status`, `/digest`, `/remember <text>`, `/mute <session> <dur>`,
`/policy`, `/panic` (escalate-only mode + cancel in-flight actions), `/ticker`.

## 8. Reply-back adapters

One interface: `deliver(session, payload: {text}|{approval:boolean}) →
delivered|degraded|queued|failed`, dispatched on `response_channel.kind`, capability-
probed at runtime, every attempt audited. Failure re-escalates, never drops.

- **claude-hook-http**: answer the parked `PermissionRequest` response in-band —
  race-free, verified.
- **claude-resume**: `claude -p --resume <native_id> "<text>"` in the session's cwd
  (headless/ended sessions). Live interactive terminal session: no safe injection —
  stage the file reply AND say so honestly in-thread ("reply staged; paste or it's
  picked up next hook fire"); a shipped `UserPromptSubmit` hook snippet slurps staged
  replies into context.
- **codex-exec-resume**: `codex exec resume <uuid> "<text>"` (verified against
  codex-cli 0.145.0; there is no `codex queue` in the installed CLI).
- **agentctl-run**: a reply to agentctl-launched work is a new bounded execution
  (`agentctl run --label car-continuation -- <vendor resume argv>` from
  `session_refs`); CAR immediately `subscribe create`s on it.
- **multica-api**: comment/approve on the card via the self-hosted REST API.
- **file (universal fallback)**: `~/.car/replies/<car_session_id>/reply-<seq>.md` —
  v2's `tickets/replies.py` inbox generalized and documented in the public contract;
  consumed replies archived to `reply_history/`.

## 9. Web UI

Hono server-rendered JSX + htmx. No build step, no SPA. Localhost bind (tailnet
exposure via tailscale serve is deliberately out of scope). Stateless views over
SQLite. It is a power-user viewport onto the same tables the bot uses — no chat.

v1 pages: **Inbox** (event stream; filters vendor/repo/severity/state; FTS),
**Incident detail** (full context → decision → outcome → audit rows; the "why did CAR
do that" page), **Memory browser/editor** (rules by confidence with evidence counts and
autonomy badges; promote/demote/archive; notes + charter editor; pending queue),
**Policy** (policy.toml rendered as validated form; writes file; daemon hot-reloads),
**Digest archive**, **`GET /brief.md`** (open escalations + stuck sessions +
yesterday's digest as markdown — for other agents to curl).

## 10. Implementation plan

**Phase 0 (serial, this session, before fan-out):** scaffold `v3/` — package.json with
ALL dependencies (frozen; no agent adds packages), tsconfig/bunfig, complete
`contract/` (zod + fixtures + emitted schema), complete `store/` migrations + typed
repository interfaces, `config/` loader, `daemon.ts` five-loop skeleton with no-op
modules, cross-module TS interfaces (`TriagePort`, `ActionBus`, `ChannelPort`,
`MemoryReader/Writer`, `Clock`), fake providers, `bun test` green.

**Then 7 parallel workstreams, disjoint directory ownership:**

| WS | Scope (owned paths) | Model |
|----|---------------------|-------|
| A | Ingest routes + all source normalizers + golden fixtures (`src/ingest/`, `test/fixtures/`) | Opus |
| B | Triage: rules pass, coalescer, LLM loop, tools, safety + policy engine (`src/triage/`, `src/policy/`) | Opus |
| C | Memory: tiers, outcome recorder, promotion, consolidation (`src/memory/`) | Opus |
| D | Telegram surface + digest builder + watchdog (`src/surfaces/telegram/`, `src/digest/`) | Opus |
| E | Actions/reply-back adapters + capability probes (`src/actions/`) | Opus |
| F | Web UI (`src/surfaces/web/`) | Sonnet |
| G | Ops units, docs, hook/adapter snippets, README repoint, e2e smoke (`ops/`, `docs/`, root README) | Sonnet |

Rules: agents code against the frozen scaffold interfaces and fakes; no package.json
edits; no edits outside owned paths; every stream ships tests. Integration, cross-wiring
in `daemon.ts`, and the e2e pass are the coordinator's job.

**Tests:** golden wire fixtures per source (adapters must normalize samples to
identical canonical events); `:memory:` SQLite everywhere; triage tests via the
scripted fake provider asserting decisions and policy blocks (not prose); the safety
suite (dedupe, breaker, self-event suppression, budget stop) is deliberately
over-tested; e2e smoke boots the daemon, replays fixtures, asserts outbox rows,
incident states, audit completeness, and a rendered digest.

## 11. Cut order (if scope must shrink) & risks

Cut first → last: Discord (stub only, by design); web memory *editing* (read-only
browser + bot suffice); mutating `run_action` templates (probe+reply+escalate is a
defensible v1 posture that halves the safety surface); Multica adapter (generic
webhook covers); LLM consolidation (decay + manual review). **Never cut:** ingest
durability, escalation with reply-back, unconditional digest, silence watchdog, audit.

Risks: (1) reply-back into live interactive sessions is structurally weak across all
vendors — mitigated by the file fallback being first-class and the UX being honest
about "staged" vs "delivered"; (2) one bad autonomous action poisons trust — mitigated
by grant-only autonomy, demote-on-one-override, escalate-on-doubt defaults; (3) vendor
CLI drift — mitigated by runtime capability probes; (4) Telegram bot state complexity
— mitigated by buttons-only-write-rows and the web UI as overflow surface.
