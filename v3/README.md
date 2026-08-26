# CAR v3 — the attention control plane

CAR v3 is a cross-vendor attention & escalation control plane. It is the layer
between you and all of your agents (Claude Code, Codex, Hermes, OMP, agentctl-launched
work, Multica autopilots, CI, cron). It ingests lifecycle and attention events from
everything, triages autonomously on your behalf — attempting to unblock agents itself —
keeps you informed, and escalates to you only when it cannot unblock. One bot in
Telegram (Discord later) is the primary UX; a minimal localhost web UI is the
power-user surface. Single source of truth for "what needs me."

Full design (binding spec): [`DESIGN.md`](./DESIGN.md).

This is alpha software, actively under construction. It runs side-by-side with CAR v2
(the ticket/runner product one directory up) — separate state dir (`~/.car/`), separate
port (`7171`), separate bot token, separate CLI (`card` vs `car`). v2 keeps running
untouched.

## Install

Requires [Bun](https://bun.sh) (no Node, no native modules).

```bash
cd v3
bun install
bun run src/cli.ts serve      # or: bun run dev
```

`card serve` starts one process: HTTP ingest + web UI on `127.0.0.1:7171`, the
Telegram long-poller (if configured), the triage loop, and the digest/watchdog
scheduler. State lives entirely in SQLite at `~/.car/car.db` (WAL mode) — the daemon
is crash-only; `kill -9` and restart resumes from table state.

Useful commands while developing:

```bash
bunx tsc --noEmit   # typecheck
bun test            # unit tests ("bun run check" / "bun run test" via package.json)
bun run src/cli.ts status   # event/session/escalation counts from the db
bun run src/cli.ts doctor   # sanity-checks state dir, sqlite, telegram token
```

## Configure: `~/.car/config.toml`

All keys are optional — the daemon runs with safe defaults (localhost-only, no
Telegram, escalate-only until policy enables action classes). See
[`src/config/config.ts`](./src/config/config.ts) for the authoritative schema.

```toml
# ~/.car/config.toml
state_dir = "/Users/david/.car"     # default: ~/.car

[http]
host = "127.0.0.1"                  # default; only change if you know what you're doing
port = 7171                         # default
# bearer tokens required per source id ONLY for non-localhost binds;
# localhost callers are trusted by default.
[http.ingest_tokens]
# agentctl = "shh-a-secret-token"

[telegram]
enabled = true                      # default: false
token_env = "CAR_TELEGRAM_TOKEN"    # default; env var name holding the bot token
chat_id = "-1001234567890"          # your chat or supergroup id
forum_mode = false                  # true = topic-per-session in a forum supergroup
digest_time = "08:30"               # local HH:MM for the unconditional daily digest

[triage]
coalesce_seconds = 20               # default; debounce window per session before triage runs
lease_seconds = 120                 # default; claimed-event lease before a crashed worker's claim is reclaimed
max_tool_calls = 8                  # default; bound on the LLM triage tool loop
max_llm_runs_per_incident = 2       # default; recurrence beyond this escalates unconditionally
run_token_cap = 16000               # default; per-run token cap

[watchdog]
pending_response_hours = 4          # default; a pending requires_response event silent this long -> escalate
default_heartbeat_multiple_warn = 2      # default
default_heartbeat_multiple_escalate = 4  # default

[providers]
triage = "anthropic/claude-haiku-4-5"        # default
consolidation = "anthropic/claude-sonnet-5"  # default
```

Policy (action-class enablement, rate limits, guards, budget) is separate:
`~/.car/policy.toml`, hot-reloaded — see DESIGN.md §5 for the schema and defaults.
Memory charter lives at `~/.car/memory/charter.md`. The universal reply-back fallback
writes to `~/.car/replies/<car_session_id>/` — see
[`docs/replies-file-contract.md`](./docs/replies-file-contract.md).

## Send your first event

With the daemon running (`card serve`), from another shell:

```bash
curl -s http://127.0.0.1:7171/v1/events \
  -H 'content-type: application/json' \
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
bun run src/cli.ts emit --type note --title "Hello CAR" --body "First event from card emit"
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
4. Set `telegram.enabled = true` and `telegram.chat_id` in `config.toml` (above),
   restart `card serve`.

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
asserts against `/healthz`, `/ui/brief.md`, and the SQLite file directly (event
count, idempotency dedupe, session creation, audit rows). It's not wired into
`package.json` (`package.json` is frozen for this build) — run it directly:

```bash
bun run scripts/e2e-smoke.ts
```

Exits non-zero on any assertion failure. It only asserts scaffold-guaranteed
behavior, so it stays green whether the other workstreams' modules are still stubs
or fully implemented.

## More

- [`DESIGN.md`](./DESIGN.md) — the binding spec: event contract, data model, daemon
  loops, triage pipeline, policy, memory, Telegram UX, reply-back adapters, web UI.
- [`docs/`](./docs/) — integration guides per event source.
- [`ops/`](./ops/) — launchd (macOS) / systemd (Linux) unit templates to run
  `card serve` as a supervised background service.
