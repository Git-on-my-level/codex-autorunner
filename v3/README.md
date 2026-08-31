# CAR v3 — the attention router

CAR v3 is a cross-vendor attention router. It is the durable layer
between you and all of your agents (Claude Code, Codex, Hermes, OMP, agentctl-launched
work, Multica autopilots, CI, cron). It ingests and normalizes attention, groups it into
durable incidents, routes replies back to the right source, and records every decision
and delivery. Autonomous operation, policy judgment, and learning are replaceable
capability providers. Providers propose typed effects; CAR core authorizes and executes
them through one safety and audit path.

Full design (binding spec): [`DESIGN.md`](./DESIGN.md). The accepted product boundary
and provider decisions are recorded in
[`ADR 0001`](./docs/architecture/0001-attention-router-capability-providers.md).
The v2 failures and fixes that constrain the rewrite are indexed in
[`Historical scars`](./docs/architecture/HISTORICAL_SCARS.md).

This is alpha software. It runs side-by-side with CAR v2
(the ticket/runner product one directory up) — separate state dir (`~/.car/`), separate
port (`7171`), separate bot token, separate CLI (`card` vs `car`). v2 is deprecated
immediately and remains runnable only as a migration bridge. The production composition
now follows ADR 0001: the daemon runs the durable attention router and capability-provider
host; legacy policy/memory modules are compatibility projections for the current web and
digest views and have no routing, grant, or execution authority.

## Install

Requires [Bun](https://bun.sh) (no Node, no native modules).

```bash
cd v3
bun install
bun run src/cli.ts serve      # or: bun run dev
```

`card serve` starts one canonical owner process: authenticated HTTP ingest + web UI on
`127.0.0.1:7171`, Telegram (if configured), the attention router, provider host,
human/outcome observation delivery, digest/watchdog, provider health, and optional
external dead-man heartbeat. An opt-in agentctl observer powers the compact Runs view
without making agentctl a dependency of the native setup. Core state lives in SQLite at
`~/.car/car.db` (WAL mode).
Claims are fenced and restart-replayed; a second daemon cannot own the same store.

Useful commands while developing:

```bash
bunx tsc --noEmit   # typecheck
bun test            # unit tests ("bun run check" / "bun run test" via package.json)
bun run src/cli.ts status   # event/session/escalation counts from the db
bun run src/cli.ts doctor   # topology, sqlite, credentials, Hermes, Telegram, dead-man
bun run src/cli.ts migration-audit --v2-root /path/to/quiesced/v2-state
bun run scripts/ui-preview.ts   # seeded in-memory UI at http://127.0.0.1:7194/ui
```

The UI preview is seeded and isolated: it never opens the user's CAR database or
delivery channels. Run timestamps are relative to preview launch so freshness states
stay realistic. Its write token is `preview-token`; signing in changes only the
in-memory fixture and is useful for exercising the legacy compatibility controls.

## Configuration: `~/.car/config.toml`

The daemon runs localhost-only, without Telegram, with the no-dependency native provider
by default. No write caller is trusted merely for being on localhost: configure at least
one credential before ingest or web mutation can succeed. See
[`src/config/config.ts`](./src/config/config.ts) for the authoritative schema.

```toml
# ~/.car/config.toml
state_dir = "/Users/david/.car"     # default: ~/.car

[http]
host = "127.0.0.1"                  # default; only change if you know what you're doing
port = 7171                         # default
# Every ingest/mutation caller needs an authenticated source identity, even localhost.
[http.ingest_tokens]
generic = "replace-me"
# agentctl = "a-different-source-credential"

[telegram]
enabled = true                      # default: false
token_env = "CAR_TELEGRAM_TOKEN"    # default; env var name holding the bot token
chat_id = "-1001234567890"          # your chat or supergroup id
allowed_user_ids = ["123456789"]    # Telegram user ids; chat_id is not identity
forum_mode = false                  # true = topic-per-session in a forum supergroup
digest_time = "08:30"               # local HH:MM for the unconditional daily digest

[triage]
lease_seconds = 120                 # default; claimed-event lease before a crashed worker's claim is reclaimed

[safety]
max_effects_per_hour = 60
max_failures_per_10m = 5
max_effect_spend_usd = 25             # rolling 24-hour core effect budget
effect_lease_seconds = 120
dedupe_minutes = 30

[watchdog]
pending_response_hours = 4          # default; a pending requires_response event silent this long -> escalate
default_heartbeat_multiple_warn = 2      # default
default_heartbeat_multiple_escalate = 4  # default

# Optional, read-only local run discovery. Off by default. Keep an exact label
# scope, or set observe_all=true deliberately. CAR never collects agentctl results.
[agentctl_observer]
enabled = false
required_labels = ["car-observe"]
observe_all = false
interval_seconds = 15
discovery_limit = 100
retention_days = 30
retention_max_terminal = 2000

[providers.defaults]
operator = "native"
policy = "native"
memory = "native"

[providers.instances.native]
adapter = "native"
continuity = "global"
scope = []

# First supported non-native provider. Hermes profile selection uses its public
# `-p <profile> acp` interface; CAR never reads Hermes-private state.
# [providers.instances.hermes_work]
# adapter = "hermes"
# profile = "work"
# continuity = "scoped"             # or global / incident
# scope = ["repo"]
# executable = "hermes"

[deadman]
enabled = false
# url = "https://independent-observer.example/car"
# token_env = "CAR_DEADMAN_TOKEN"
```

`agentctl_observer.discovery_limit` accepts `1..200`, matching agentctl's
public query ceiling. The Runs page reports incomplete active coverage
separately from partial older history.

Provider policy judgment is advisory. Human grants, canonical argument/scope matching,
dangerous-content rails, limits, budgets, leases, and panic remain core-owned. The
universal reply-back fallback
writes to `~/.car/replies/<car_session_id>/` — see
[`docs/replies-file-contract.md`](./docs/replies-file-contract.md).

## One-way v2 cutover evidence

Stop v2 writers before qualification, then run `card migration-audit --v2-root <root>`.
The command reads but never mutates the supplied v2 tree, hashes the exact inventory,
inspects SQLite lifecycle columns, records active and unclassified rows, writes a
mode-0600 immutable JSON report beneath `~/.car/migration/`, and audits the report in
the v3 database. It exits 2 when active/live/error evidence blocks cutover, 3 when
nonempty tables still require human classification, and 0 only when the source is
ready for explicit human approval and read-only archival. It does not invent v3 events
from ambiguous legacy rows and is not a dual-write bridge.

## Send your first event

With the daemon running (`card serve`), from another shell:

```bash
curl -s http://127.0.0.1:7171/v1/events \
  -H 'content-type: application/json' \
  -H 'authorization: Bearer replace-me' \
  -d '{
    "contract": "car.event.v1",
    "idempotency_key": "manual:hello-1",
    "ts": "'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'",
    "source": { "vendor": "other", "host": "'"$(hostname)"'", "adapter": "manual-curl" },
    "session": null,
    "type": "note",
    "severity": "info",
    "title": "Hello CAR",
    "body": "First manual event.",
    "payload": {}
  }'
```

Or use the CLI wrapper, meant for CI/cron scripts (it fills in `contract`, `ts`,
`source`, and a computed idempotency key for you):

```bash
CAR_INGEST_TOKEN=replace-me bun run src/cli.ts emit --type note --title "Hello CAR" --body "First event from card emit"
bun run src/cli.ts emit --type attention.error --severity urgent --title "nightly build failed"
```

Check it landed:

```bash
curl -s http://127.0.0.1:7171/healthz
curl -s http://127.0.0.1:7171/ui/brief.md   # open escalations + stuck sessions, markdown
bun run src/cli.ts status
```

See [`docs/generic-events.md`](./docs/generic-events.md) for the full envelope and more
`card emit` examples, and [`docs/`](./docs/) for per-source integration guides
(Claude Code hooks, agentctl subscribe, Multica webhook).

## Telegram bot setup

1. Talk to [@BotFather](https://t.me/BotFather) on Telegram, `/newbot`, follow the
   prompts. You get a bot token like `123456:ABC-DEF...`.
2. Export it in the environment the daemon runs under — do **not** put it in
   `config.toml`:
   ```bash
   export CAR_TELEGRAM_TOKEN="123456:ABC-DEF..."
   ```
   (The env var name is configurable via `telegram.token_env` if you want something
   other than `CAR_TELEGRAM_TOKEN`.)
3. Add the bot to the chat or supergroup you want escalations in, and get its
   `chat_id` (e.g. forward a message to `@userinfobot`, or check the `getUpdates`
   response after messaging the bot).
4. Set `telegram.enabled = true`, `telegram.chat_id`, and at least one explicit
   `telegram.allowed_user_ids` entry in `config.toml` (above), then restart
   `card serve`. Telegram user IDs are the identity allowlist; a permitted chat
   alone never authorizes an actor. CAR rejects missing or unauthorized actors
   before they reach any command, message, or callback handler.

**Forum mode.** If your chat is a Telegram **forum-enabled supergroup**, set
`forum_mode = true`: CAR opens one topic per session and threads all of that
session's escalations, status, and replies into it. In a flat (non-forum) chat,
CAR instead keeps one pinned, edited-in-place anchor message per session and
replies to it — set `forum_mode = false` (default) or omit it. Getting this wrong
just means messages land in the main chat instead of a topic; it isn't destructive,
so it's safe to flip and restart if you're not sure which kind of chat you made.

## Running the end-to-end smoke test

`v3/scripts/e2e-smoke.ts` boots a full daemon against a throwaway config (random
free port, temp `state_dir`), replays a small battery of events over HTTP, and
asserts against `/healthz`, `/ui/brief.md`, and SQLite directly (authentication,
event/session durability, router/provider terminals, escalation, audit, and daemon-owner
release). It's not wired into
`package.json` (`package.json` is frozen for this build) — run it directly:

```bash
bun run scripts/e2e-smoke.ts
```

Exits non-zero on any assertion failure.

## More

- [`DESIGN.md`](./DESIGN.md) — the binding spec: event contract, data model, router
  loops, capability providers, grants and safety, Telegram UX, reply-back adapters,
  web UI.
- [`docs/architecture/0001-attention-router-capability-providers.md`](./docs/architecture/0001-attention-router-capability-providers.md)
  — the accepted product and core/provider boundary.
- [`docs/`](./docs/) — integration guides per event source.
- [`ops/`](./ops/) — launchd (macOS) / systemd (Linux) unit templates to run
  `card serve` as a supervised background service.
