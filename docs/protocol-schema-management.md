# Protocol Schema Management

This document describes how to manage protocol schema snapshots for Codex app-server and OpenCode server integration.

## Overview

Protocol schema snapshots are stored in `vendor/protocols/` and are used for:
- Detecting protocol drift between versions
- Validating that parsers handle expected schema structures
- CI validation that committed schemas are parseable and contain expected primitives

## Schema Locations

- **Codex app-server**: `vendor/protocols/codex.json`
- **OpenCode server**: `vendor/protocols/opencode_openapi.json`
- **Pinned CI tool versions**: `vendor/protocols/agent-compatibility.lock.json`

## Refreshing Schemas

When upgrading Codex or OpenCode, refresh the protocol schemas before updating parsers.

### CLI Command

```bash
# Refresh both schemas (requires binaries)
car protocol refresh
make protocol-schemas-refresh
make agent-compatibility-refresh

# Refresh only Codex schema
car protocol refresh --no-opencode

# Refresh only OpenCode schema  
car protocol refresh --no-codex

# Specify custom target directory
car protocol refresh --target-dir /path/to/schemas
```

### Requirements

- **Codex**: Binary must support `codex app-server generate-json-schema --out <dir>`
- **OpenCode**: Binary must start with `opencode serve` and expose `/doc` endpoint

### Environment Variables

If binaries are not in PATH, set:
- `CODEX_BIN` - Path to codex binary
- `OPENCODE_BIN` - Path to opencode binary

`make agent-compatibility-refresh` updates the pinned tool-version lock file and
the vendor schema snapshots together so CI and local drift checks stay aligned.

## Validation Tests

Run schema validation tests:

```bash
make agent-compatibility-check
make protocol-schemas-check
python -m pytest tests/test_protocol_schemas.py -v
```

These tests verify:
- Schemas are valid parseable JSON
- Schemas have expected top-level structure
- Codex schemas contain known type definitions (Thread, Turn, Message, Session)
- OpenAPI specs contain known endpoints (/global/health, /session)

## Updating Parsers

After refreshing schema snapshots:

1. Review schema changes
2. Update parser code to handle any new fields or methods
3. Run validation tests to ensure parser compatibility
4. Commit schema changes alongside parser updates

## CI Drift Detection

The `scripts/check_protocol_drift.py` script compares current binaries against
committed snapshots in `vendor/protocols`. The repo-level CI parity command is:

```bash
make agent-compatibility-check
python scripts/check_protocol_drift.py
```

CI installs Codex and OpenCode from `vendor/protocols/agent-compatibility.lock.json`
before running the check so drift results stay reproducible across environments.

## Runtime/Lifecycle Ownership Boundaries

Protocol parsing and lifecycle normalization are split across clearly scoped
modules. When schemas drift, use this map to identify which layer needs updates.

### Shared lifecycle classifier

**`src/codex_autorunner/core/acp_lifecycle.py`**

Authoritative ACP/shared lifecycle classifier. Owns:

- Terminal/progress/usage/approval classification for ACP methods
- Session-id, turn-id, and session-update extraction with alias support
- Text, output-delta, message-text, progress-message, error-message extraction
- Usage extraction (`usage`, `tokenUsage` keys)
- `ACPLifecycleSnapshot` as the canonical pre-computed lifecycle summary
- `runtime_terminal_status_for_lifecycle()` as the authoritative completion check

Every downstream consumer should prefer `ACPLifecycleSnapshot` fields over
re-extracting from raw payloads directly.

### Backend-specific runtime normalization

**`src/codex_autorunner/core/orchestration/runtime_thread_events.py`**

Consumes `ACPLifecycleSnapshot` from `acp_lifecycle.py` and adds backend-specific
normalization for web/Telegram/chat surfaces. Owns:

- OpenCode message-part handling (`message.part.updated`, `message.part.delta`)
- Codex app-server item events (`item/completed`, `item/toolCall/*`)
- OpenCode reasoning buffer accumulation
- OpenCode tool/patch/usage part normalization
- Streaming text merge and role-based message filtering
- `RuntimeThreadRunEventState` for accumulative event state

Must not duplicate terminal status or lifecycle classification logic that
`acp_lifecycle.py` already provides. Backend-specific branches (OpenCode part
types, Codex item types) are appropriate here only when the shared ACP contract
cannot express the behavior.

### OpenCode field extraction

**`src/codex_autorunner/core/orchestration/opencode_event_fields.py`**

Owns OpenCode-specific field extraction for message ids, part ids, roles,
part types, and output deltas from the nested `properties` envelope structure.

### App-server protocol helpers

**`src/codex_autorunner/integrations/app_server/protocol_helpers.py`**

Normalizes raw app-server JSON-RPC responses/requests/notifications. Owns
response envelope parsing and resume-payload snapshot recovery.

### Drift update workflow

When protocol schemas change:

1. Refresh schema snapshots (see [Refreshing Schemas](#refreshing-schemas)).
2. Check `acp_lifecycle.py` first: if the new/drifted fields affect terminal
   classification, session-update kinds, usage keys, or text extraction, update
   the shared classifier and `ACPLifecycleSnapshot`.
3. Then check `runtime_thread_events.py`: if the drift introduces new OpenCode
   part types, item types, or streaming methods not covered by the ACP contract,
   add backend-specific handling there.
4. Update the shared lifecycle corpus in
   `tests/fixtures/acp_lifecycle_corpus.json` with representative cases for
   any new or changed methods.
5. Run `tests/test_protocol_schemas.py`,
   `tests/agents/acp/test_lifecycle_corpus.py`, and
   `tests/core/orchestration/test_runtime_thread_events.py` to verify
   alignment.
