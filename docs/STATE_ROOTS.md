# State Roots Contract

This document defines the canonical locations for all durable state and artifacts
in Codex Autorunner (CAR), and the "no shadow state" contract.

## Canonical State Roots

All durable artifacts must live under one of these roots:

### 1. Repo-Local Root

**Location**: `<repo_root>/.codex-autorunner/`

**Purpose**: Per-repository runtime state, tickets, context, and configuration.

**Contents**:
- `tickets/` - Ticket queue (required)
- `contextspace/` - Durable context (active_context.md, decisions.md, spec.md)
- `config.yml` - Generated repo config
- `state.sqlite3` - Run state database
- `codex-autorunner.log` - Runner logs
- `lock` - Lock file for exclusive access
- `runs/` - Run artifacts and dispatch
- `flows/` - Flow artifacts
- `flows.db` - Flow store database
- `pma/` - PMA state and queue
- `archive/` - Worktree snapshots
- `bin/` - Generated helper scripts
- `workspace/` - Workspace directory
- `app_server_workspaces/` - App-server supervisor/workspace state when the effective destination is `docker`

**Notable repo-local artifacts**:
- `flows/<run_id>/chat/inbound.jsonl` - Mirrored inbound chat events for a flow run
- `flows/<run_id>/chat/outbound.jsonl` - Mirrored outbound chat events for a flow run
- `tickets/ingest_state.json` - Canonical ticket-ingest receipt (`ingested`, `ingested_at`, `source`)

**Resolution**: `resolve_repo_state_root(repo_root)` in `core/state_roots.py`

### 2. Hub Root

**Location**: `<hub_root>/.codex-autorunner/`

**Purpose**: Hub-level state for multi-repo management.

**Contents**:
- `manifest.yml` - Managed repositories list (including repo/worktree `destination` config)
- `hub_state.json` - Hub state
- `config.yml` - Hub config
- `codex-autorunner-hub.log` - Hub logs
- `templates/` - Hub-scoped templates
- `chat/channel_directory.json` - Cross-platform channel directory used for lightweight routing context

**Resolution**: Hub root is typically the hub's repo root, using repo-local patterns.

### 3. Global Root

**Location**: `~/.codex-autorunner/` (configurable via `CAR_GLOBAL_STATE_ROOT`)

**Purpose**: Cross-repo caches, shared resources, update state.

**Contents**:
- `update_cache/` - Cached update artifacts
- `update_status.json` - Update status
- `locks/` - Cross-repo locks (e.g., telegram bot lock)
- `workspaces/` - App-server workspaces (default for non-docker destinations)

**Docker destination override**:
- When a repo/worktree runs with effective destination `docker`, supervisor state root is forced to:
  - `<repo_root>/.codex-autorunner/app_server_workspaces`
- Rationale: docker-wrapped commands execute inside the repo bind mount, so state must be writable and visible from that mount.
- This still satisfies the canonical state contract because the override remains under repo-local `.codex-autorunner/`.

**Resolution**: `resolve_global_state_root()` in `core/state_roots.py`

**Config Override**: Set `state_roots.global` in config or `CAR_GLOBAL_STATE_ROOT` env var.

## Non-Canonical Locations (Caches Only)

These locations are explicitly **non-canonical** (ephemeral, disposable):

| Location | Purpose | Notes |
|----------|---------|-------|
| `/tmp/`, `$TMPDIR` | Temporary files | Never durable |
| `$XDG_CACHE_HOME` or `~/.cache` | Optional caches | Must be rebuildable |
| `__pycache__/` | Python bytecode | Auto-generated |

## Legacy PMA Target Artifacts (Deprecated)

- `<hub_root>/.codex-autorunner/pma/delivery_targets.json`
- `<hub_root>/.codex-autorunner/pma/active_sink.json`

These files are kept only for compatibility and are not part of current PMA dispatch behavior. Delivery routing now treats legacy targets as deprecated/no-op and ignores them.

**Rule**: Any location outside the canonical roots must be:
1. A true cache (data is derivable from canonical sources)
2. Explicitly documented here
3. Safe to delete without data loss

## No Shadow State Contract

**Invariant**: All durable state and artifacts must be representable under a
canonical root. No "shadow" state directories outside these roots.

### What This Means

1. **No ad-hoc roots**: Don't create new state directories outside the roots
2. **Single source of truth**: Canonical roots are the source of truth
3. **Portable state**: All durable state can be moved by relocating the root
4. **Testable boundaries**: Tests verify no writes outside allowed roots

### Enforcement

- `core/state_roots.py` provides the single authority for root resolution
- Call sites should use `resolve_repo_state_root()` and `resolve_global_state_root()`
- Tests verify boundary enforcement (see `tests/core/test_state_roots.py`)

## Path Authority Module

The `core/state_roots.py` module provides:

```python
def resolve_repo_state_root(repo_root: Path) -> Path:
    """Return the repo-local state root (.codex-autorunner)."""

def resolve_global_state_root(*, config=None, repo_root=None) -> Path:
    """Resolve the global state root for cross-repo caches and locks."""

def resolve_hub_templates_root(hub_root: Path) -> Path:
    """Return the hub-scoped templates root."""
```

### Usage Pattern

```python
from codex_autorunner.core.state_roots import resolve_repo_state_root

state_root = resolve_repo_state_root(repo_root)
db_path = state_root / "state.sqlite3"
log_path = state_root / "codex-autorunner.log"
```

## Migration Notes

When adding new durable artifacts:
1. Determine the appropriate root (repo-local, hub, or global)
2. Use `resolve_*_state_root()` functions, not ad-hoc path construction
3. Document the artifact in this file
4. Ensure tests cover the boundary
