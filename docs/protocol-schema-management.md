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

## Refreshing Schemas

When upgrading Codex or OpenCode, refresh the protocol schemas before updating parsers.

### CLI Command

```bash
# Refresh both schemas (requires binaries)
car protocol refresh
make protocol-schemas-refresh

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

## Validation Tests

Run schema validation tests:

```bash
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
committed snapshots in `vendor/protocols`:

```bash
python scripts/check_protocol_drift.py
```

This is used in CI to detect when upstream protocols have changed.
