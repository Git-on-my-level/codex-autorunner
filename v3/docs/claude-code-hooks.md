# Claude Code integration (HTTP hooks)

CAR ingests Claude Code sessions via Claude Code's built-in **`type: "http"` hooks** —
no wrapper scripts, no polling. Claude Code POSTs a JSON payload straight to CAR's
ingest endpoint whenever a hook fires; a small in-process normalizer on the CAR side
(`src/ingest/`) turns that payload into a `car.event.v1` envelope (see
[`generic-events.md`](./generic-events.md) for the envelope shape).

> Reference: https://code.claude.com/docs/en/hooks — this doc mirrors the current
> hook schema as of 2026-08. If Claude Code's hook shape has changed since, treat the
> official docs as authoritative and this file as the worked example.

## settings.json

Add (or merge into) `~/.claude/settings.json` — or the project-level
`.claude/settings.json` if you only want this wired for one repo:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "http",
            "url": "http://127.0.0.1:7171/v1/ingest/claude",
            "timeout": 10
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "http",
            "url": "http://127.0.0.1:7171/v1/ingest/claude",
            "timeout": 10
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "http",
            "url": "http://127.0.0.1:7171/v1/ingest/claude",
            "timeout": 10
          }
        ]
      }
    ],
    "Notification": [
      {
        "hooks": [
          {
            "type": "http",
            "url": "http://127.0.0.1:7171/v1/ingest/claude",
            "timeout": 10
          }
        ]
      }
    ],
    "PermissionRequest": [
      {
        "hooks": [
          {
            "type": "http",
            "url": "http://127.0.0.1:7171/v1/ingest/claude?timeout=115",
            "timeout": 115
          }
        ]
      }
    ]
  }
}
```

Notes on the shape:
- Each event name maps to an array of matcher groups; each group has a `matcher`
  (optional — omit it, as above, to match every tool/reason) and a `hooks` array.
  You can add a `matcher` (e.g. `"Bash"`, or a permission-mode-style matcher) to scope
  a hook to specific tools if you don't want every permission prompt going through CAR.
- `url` is CAR's ingest endpoint. All five events above can point at the same URL —
  CAR tells them apart via the `hook_event_name` field in the POST body.
- Localhost callers are trusted by default; no `headers`/`Authorization` needed for a
  same-machine daemon. If Claude Code and CAR run on different hosts, add a bearer
  token: `"headers": {"Authorization": "Bearer $CAR_INGEST_TOKEN"}, "allowedEnvVars":
  ["CAR_INGEST_TOKEN"]`, and set the matching value under `[http.ingest_tokens]` in
  `~/.car/config.toml` (see `../README.md`).
- **The `?timeout=115` query param on `PermissionRequest` matters, and it's not
  redundant with the `"timeout": 115` field next to it.** `timeout` in the hook config
  is Claude Code's own local "give up and cancel" clock (seconds) — it is *not* sent
  to CAR anywhere in the POST body or headers, so CAR has no way to know what it is
  unless you tell it. CAR's parking deadline (see "In-band permission decisions"
  below) is `hookTimeoutHintMs()` (`src/ingest/claude.ts`) reading, in priority order:
  a `timeout_ms` (ms) or `timeout` (seconds) query-string param on the URL, then an
  `X-Car-Hook-Timeout-Ms` / `X-Claude-Hook-Timeout` / `X-Hook-Timeout` header, then a
  same-named field in the payload — falling back to a 60s assumption if none of those
  is present. Keep the query param's number equal to the `timeout` field's number (as
  above) so CAR's park deadline and Claude Code's own cancel clock agree; CAR then
  parks for that value minus a 5s safety margin (clamped between 250ms and 10 minutes)
  so it always answers before Claude Code's own timeout fires. If you skip the query
  param, CAR still works — it just assumes 60s, parking for 55s regardless of what you
  set `"timeout"` to.

## What Claude Code sends

Common fields on every hook POST: `session_id`, `hook_event_name`, `cwd`,
`transcript_path`, `permission_mode`. `PermissionRequest` additionally carries
`tool_name`, `tool_input`, and `tool_use_id`. CAR's claude-code normalizer maps these
onto `car.event.v1`:

- `session.native_id` ← `session_id`, `session.host` ← the daemon's own hostname,
  `session.cwd` ← `cwd`.
- `idempotency_key` is built from `session_id` + `hook_event_name` (+ `tool_use_id`
  for `PermissionRequest`) so a retried hook delivery is a safe no-op — e.g.
  `claude-code:sess-abc:PermissionRequest:toolu_01X` (DESIGN.md §2).
- `SessionStart`/`SessionEnd` → `session.started` / `session.ended`.
- `Stop` → always `attention.idle`, severity `notice`, `requires_response: false`,
  `response_channel: {"kind": "claude-resume"}` — the agent yielded and a reply can
  still reach it via `claude -p --resume`, but CAR doesn't treat "turn ended" itself
  as needing you (that's what `Notification`/`PermissionRequest` are for).
- `Notification` → depends on `notification_type`: `permission_prompt` →
  `attention.permission`; `idle_prompt` → `attention.idle`; `agent_needs_input` /
  `elicitation_dialog` / `elicitation_url_dialog` → `attention.question` (all four
  `requires_response: true`, `response_channel: claude-resume` — a bare `Notification`
  can't carry an in-band decision the way `PermissionRequest` can); `agent_completed`
  → `progress`; anything else → `note`.
- `PermissionRequest` → `attention.permission`, `requires_response: true`,
  `response_channel: {"kind": "claude-hook-http", "hint": {"tool_use_id", "session_id",
  "tool_name", "deadline_ms"}}`.

## In-band permission decisions (DESIGN.md §8)

`PermissionRequest` is the one hook CAR answers **synchronously, in the same HTTP
response** — it does not just record the event and return 200 immediately. Instead:

1. The ingest handler persists the event, then **parks** the HTTP response (see
   `src/permission_park.ts`) for up to `deadline_ms` (hook timeout minus margin) while
   triage runs: a rules-pass autonomy grant, or a human tapping ✅/❌ on the Telegram
   escalation, can both resolve it.
2. If a decision lands in time, CAR responds 200 with:
   ```json
   {
     "hookSpecificOutput": {
       "hookEventName": "PermissionRequest",
       "permissionDecision": "allow",
       "permissionDecisionReason": "granted rule: dependency bumps on this repo"
     }
   }
   ```
   (`permissionDecision` is `"allow"` or `"deny"`; CAR does not use `"ask"` here — that
   would just re-open the exact prompt CAR exists to keep you out of.)
3. If nothing resolves before the deadline, CAR responds 200 with an **empty JSON
   object `{}`** — no decision fields at all — and Claude Code falls back to its own
   local permission prompt. **Degraded, never broken**: you still get asked, just by
   Claude Code directly instead of via CAR/Telegram, and the event + eventual local
   outcome are both still recorded once you answer it.
- A retried delivery of the same `PermissionRequest` (Claude Code or a proxy
  re-sending) while the first one is still parked also gets the empty `{}` body
  immediately, rather than parking a second time — the park registry is keyed by
  CAR's event id, and the first in-flight request owns the eventual decision.
- Parked requests do **not** survive a CAR restart — they're an in-process,
  best-effort optimization on top of the durable event row, not a queue. A restart
  mid-park just falls through to "no decision" for that one request.

If you'd rather CAR never sit in the hot path for permissions (e.g. you want a purely
observational deployment first), drop `PermissionRequest` from `settings.json` — CAR
works fine off the other four events; you just lose synchronous approve/deny from
Telegram for tool calls.
