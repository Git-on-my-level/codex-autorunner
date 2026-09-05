# Agent control surface: car.request.v1

Use the guided request interface for decisions. Existing native event adapters remain supported for observations and harness-specific permission requests. CLI and MCP use the same HTTP API; neither writes the server database directly.

## Calling-agent workflow

```bash
# Run from v3. The CLI can also be installed as card.
bun run src/cli.ts raise --key api-migration-compatibility-1 \
  --file examples/attention/migration-decision.json
bun run src/cli.ts request get req_RETURNED_ID
bun run src/cli.ts request context req_RETURNED_ID --revision 1 --file enriched-packet.json
bun run src/cli.ts wait req_RETURNED_ID --timeout 60
bun run src/cli.ts request receive req_RETURNED_ID
# Apply this answer only to this request. Confirm once the actual blocker is gone:
bun run src/cli.ts request ack req_RETURNED_ID --answer reply_RETURNED_ID \
  --outcome resolved --note 'Migration resumed under the compatibility decision'
```

Use actual IDs returned by CAR. The example packet's facts are illustrative, not evidence about your repository. `card schema` returns the current schema and instructions. JSON output includes `guidance.code`, `guidance.action` (tool, arguments and required input), `guidance.can_apply_answer`, and a human-readable `next_action`; errors include actionable messages. `wait` returns promptly for preparation requests so the source can add context instead of waiting on itself.

Minimum packet fields are `goal`, `blocker`, `question`. CAR returns concrete requests for `why_human`, facts with sources, attempted investigation, recommendation/rationale and impact. Supply what is true; use `cannot_investigate` to explain an access or knowledge limit rather than fabricating evidence. Optional alternatives contain a stable ID, label, exact answer and consequences. A deadline is the last time a fresh answer may be received, not the completion deadline for the entire task.

Normal incomplete packets remain `preparing` for at most 120 seconds or two enrichment rounds by default. Urgent, near-deadline, complete, and preparation-budget-exhausted requests surface immediately. Failure to return to CAR does not hide the request. Published packets cannot be silently edited beneath the human; cancel a superseded request and raise a new one.

## HTTP

Base path: `/v1/attention`. All request operations require `Authorization: Bearer <agent token>`. The server resolves client, host and workspace. Request JSON is strict and streaming-limited to 64 KiB; credentials never belong in URLs. The schema endpoint contains no account data and is public.

| Method/path | Input | Result |
|---|---|---|
| GET `/schema` | None | Public request schema and instructions |
| GET `/capabilities` | Agent credential | Deterministic workflow guide and state/authority boundaries |
| POST `/requests` | `{idempotency_key, packet}` | Durable request; same key and original packet replay safely |
| GET `/requests?before=...` | Cursor from prior page | Up to 100 summaries owned by this client; `next_cursor` |
| GET `/requests/:id` | None | Current packet, context guidance, optional proposal, answer and next action |
| POST `/requests/:id/context` | `{expected_revision, packet}` | Full replacement context while still preparing |
| POST `/requests/:id/ack` | `{answer_id, outcome: "received" | "resolved", note?}` | Exact-answer receipt or source-confirmed resolution |
| POST `/requests/:id/cancel` | `{expected_revision, reason}` | Explicit cancellation; stale answer becomes non-actionable |

There is deliberately no agent-side answer/grant endpoint. Human authentication is separate. A guessed request ID from another client returns not found. Response polling does not confer authority to another request.

## State and recovery

```
preparing -> needs_you -> answered -> received -> resolved
       \          \          \          \
        explicit cancellation, or expiry before receipt
```

GET does not acknowledge anything. `request receive` writes the answer to a private
local file **before** acknowledging receipt. The server rejects a direct transition
from answered to resolved. Standard CLI/MCP expose receipt only through `receive`;
custom HTTP integrations may send `outcome: "received"` only after persisting their
own inbox. An exact repeated receipt is idempotent. Report `resolved` only after the
blocker is actually gone.

`answer.eligible_for_receipt` is not execution permission: it permits entering that
receipt protocol. `guidance.can_apply_answer` is true only after receipt, for the
current request. A received request may finish after its decision deadline. A
cancelled/expired request must not use a cached answer; check current state before
acting. The human may withdraw obsolete work, but CAR cannot undo actions already
executed outside it. Missed guided decisions stay visible to the human until reviewed,
then remain in history as expired, never as successful resolutions.

While preparing, exact replay of an already committed context enrichment returns
the existing revision; it does not consume another round. Different stale content
is a conflict. Published context is immutable. Listing uses a chronological opaque
keyset cursor; always use the returned `next_cursor`, never construct one from an ID.

New submissions are spooled locally before HTTP. A dropped connection, server 5xx, or malformed response leaves the original payload and key available for replay. `accepted_locally` means only that this host saved it; it does not mean the CAR server or human saw it. `card flush` reports `accepted` (server acceptance), `rejected`, `pending`, and remaining work. A known 400/409/etc. rejection is preserved privately in `rejected/` rather than replayed indefinitely; authentication failure pauses the batch. `card flush` retries pending work; `card relay` retries while running. The MCP process includes the lightweight relay. A one-shot CLI cannot guarantee future retries after it exits.

Spools are scoped to server origin and credential fingerprint. Tokens are not embedded in request spool files. Rotating a token or switching servers intentionally does not deliver an old profile's requests to the new audience. Flush before planned rotation; after a lost credential, inspect old pending files and replay deliberately under the correct identity, checking for previously accepted duplicates. Local spool files contain potentially sensitive request content and must be backed up/protected accordingly.

Ambiguous native sends remain `uncertain`; staged file fallback remains `staged`. The web Watching view offers explicit native-source reconciliation, not blind retry. Confirm at the source before choosing retry. Source-confirmed clearance is matched to the exact native event and authenticated source; an unrelated lifecycle event is not clearance.

## MCP stdio

Example harness configuration (replace paths with absolute paths):

```json
{
  "mcpServers": {
    "car": {
      "command": "bun",
      "args": ["run", "/absolute/path/v3/src/cli.ts", "mcp"],
      "env": {"CAR_CONNECTION_FILE": "/private/path/agent.json"}
    }
  }
}
```

Tools: `car_raise`, `car_get`, `car_list`, `car_context`, `car_receive`, `car_ack`, `car_cancel`, `car_flush`, `car_guide`. Initialization and readiness are required. This is a small stdio tools adapter, not an MCP remote-auth server or implementation of every optional MCP capability. It negotiates the supported 2025-11-25 / 2025-06-18 versions, returns text plus structured JSON tool results, limits frames to 256 KiB and concurrent work to 16, validates tool inputs before local spooling, and exposes no human-authority tools. Cancellation suppresses a late response, not a remote operation already committed. Incomplete frames at EOF are never executed. Protocol reference: https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle
