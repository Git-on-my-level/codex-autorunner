# Generic events: `car.event.v1`

Anything that can `curl -d` is a valid CAR source (DESIGN.md §2). This is the wire
contract every ingest adapter (Claude Code hooks, agentctl webhooks, Multica, this
generic path) ultimately produces. The canonical zod schema lives in
[`../src/contract/events.ts`](../src/contract/events.ts) — that file is the source of
truth if this doc and the code ever disagree.

## Endpoint

```
POST http://127.0.0.1:7171/v1/events
Content-Type: application/json
```

Body is a single JSON object, a JSON array of event objects, or newline-delimited JSON
(NDJSON, one event per line — send `Content-Type: application/x-ndjson` or
`application/jsonlines`, or just don't wrap it in `{}`/`[]`, and CAR sniffs it). A
single object gets a single result back:

```json
{ "inserted": true, "event_id": "01H...", "car_session_id": null }
```

A JSON array or NDJSON body gets an envelope with one result per line, in order,
`ok:false` entries for lines that failed to parse or didn't validate, and a `count`/
`accepted`/`rejected` summary — one bad line never discards the good ones in the same
batch:

```json
{
  "contract": "car.event.v1",
  "count": 3,
  "accepted": 2,
  "rejected": 1,
  "results": [
    { "index": 0, "ok": true, "inserted": true, "event_id": "01H...", "car_session_id": null },
    { "index": 1, "ok": false, "error": "invalid_event", "detail": "..." },
    { "index": 2, "ok": true, "inserted": false, "event_id": "01H...", "car_session_id": "01J..." }
  ]
}
```

HTTP status is `200` if at least one item in the batch was accepted, `400` if every
item failed (or the whole body failed to parse at all — a single malformed object also
returns `400` with `{"error": "invalid_event", "detail": "..."}`).

## Envelope

```jsonc
{
  "contract": "car.event.v1",
  // REQUIRED. Source-scoped. Duplicate POSTs with the same key return the
  // original event id instead of inserting a second row (at-least-once
  // delivery -> exactly-once processing).
  "idempotency_key": "ci:nightly-build:2026-08-26",
  "ts": "2026-08-26T18:04:11Z",            // ISO-8601 with offset
  "source": {
    "vendor": "ci",                        // claude-code|codex|cursor|hermes|omp|
                                            // agentctl|multica|ci|cron|other
    "host": "davids-mbp",
    "adapter": "generic-curl"
  },
  "session": null,                         // null for sessionless sources (CI, cron);
                                            // see "Sessionful example" below otherwise
  "type": "attention.error",               // closed vocabulary, see below
  "severity": "urgent",                    // info | notice | attention | urgent
  "requires_response": false,
  "response_channel": null,                // null = file-fallback-only reply-back
  "title": "nightly build failed",
  "body": "free text, <=16KB",
  "payload": {},                           // vendor blob, <=32KB, stored verbatim
                                            // (truncated with a marker past the cap)
  "expires_at": null                       // optional; resolving after this is moot
}
```

**Event types** (closed enum, additive-only within v1): `session.started`,
`session.ended`, `attention.permission`, `attention.question`, `attention.idle`,
`attention.error`, `attention.cleared`, `progress`, `artifact`, `heartbeat`,
`cost.report`, `note`.

**Severity**: `info` < `notice` < `attention` < `urgent`. `urgent` skips the LLM
triage step entirely and escalates immediately (DESIGN.md §5) — reserve it for things
that actually need David awake right now.

Fetch the live JSON Schema (generated from the zod source) at any time:
```bash
curl -s http://127.0.0.1:7171/v1/schema
```

## curl example

```bash
curl -s http://127.0.0.1:7171/v1/events \
  -H 'content-type: application/json' \
  -d '{
    "contract": "car.event.v1",
    "idempotency_key": "cron:disk-check:2026-08-26T06:00",
    "ts": "2026-08-26T06:00:03Z",
    "source": { "vendor": "cron", "host": "davids-mbp", "adapter": "disk-check.sh" },
    "session": null,
    "type": "attention.error",
    "severity": "attention",
    "title": "disk usage above 90% on /",
    "body": "df -h reports 92% used on /Users/david",
    "payload": { "used_pct": 92, "mount": "/" }
  }'
```

## Sessionful example

Give an event a `session` block to attach it to a CAR session (created on first sight,
matched thereafter by `vendor+host+native_id` — DESIGN.md §2):

```jsonc
{
  "contract": "car.event.v1",
  "idempotency_key": "hermes:sess-42:question:1",
  "ts": "2026-08-26T18:04:11Z",
  "source": { "vendor": "hermes", "host": "davids-mbp", "adapter": "hermes-webhook" },
  "session": {
    "vendor": "hermes",
    "native_id": "sess-42",
    "host": "davids-mbp",
    "cwd": "/Users/david/omi",
    "repo": "github.com/x/omi",
    "title": "fix BLE reconnect"
  },
  "type": "attention.question",
  "severity": "attention",
  "requires_response": true,
  "response_channel": { "kind": "file" },
  "title": "Which retry backoff?",
  "body": "Exponential (1s/2s/4s) or fixed 2s?",
  "payload": {}
}
```

## `card emit` — for CI and cron

The daemon's CLI wraps the envelope for you (fills `contract`, `ts`, `source`, and a
bucketed idempotency key) so a one-liner in a script or crontab is enough:

```bash
# from v3/, against a locally running `card serve`
bun run src/cli.ts emit --type note --title "backup finished" --body "12.4GB, 4m02s"
bun run src/cli.ts emit --type attention.error --severity urgent \
  --title "nightly build failed" --body "see CI log"
bun run src/cli.ts emit --type attention.error --idempotency-key "ci:job-882" \
  --title "flaky test re-run exhausted retries"
# --port lets a script target a non-default daemon port
bun run src/cli.ts emit --type heartbeat --title "watchdog check-in" --port 7171
```

`--severity` defaults to `notice`; omit `--idempotency-key` and one is computed for
you from source+type+payload, bucketed to the minute (so a repeating alert still
re-fires on the next minute rather than deduping forever — DESIGN.md §2). Crontab
example:

```cron
0 6 * * * /path/to/bun run /path/to/v3/src/cli.ts emit --type note --title "daily backup ran" >/dev/null 2>&1
```

## Idempotency & dedupe

The unique index is on `idempotency_key` alone. Reusing a key is always safe — you get
the original `event_id` back, never a duplicate row. Sources that can't supply a
natural key (this generic path, cron) should still pass an explicit one when they can
(as above) rather than relying on the server-computed fallback, since the fallback
buckets to the minute and can coalesce two genuinely-different same-minute events from
an identical source+type+payload combination.
