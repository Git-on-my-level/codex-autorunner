# State Roots Contract

This document defines the canonical locations for all durable state and artifacts
in Codex Autorunner (CAR), the retention taxonomy, and the "no shadow state" contract.

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
- `filebox/` - Shared inbox/outbox attachment root (`filebox/inbox`, `filebox/outbox`)

**Notable repo-local artifacts**:
- `flows/<run_id>/chat/inbound.jsonl` - Mirrored inbound chat events for a flow run
- `flows/<run_id>/chat/outbound.jsonl` - Mirrored outbound chat events for a flow run
- `tickets/ingest_state.json` - Canonical ticket-ingest receipt (`ingested`, `ingested_at`, `source`)
- `filebox/outbox/` - Agent-produced artifacts, including `car render` screenshot/observe/demo outputs

**Resolution**: `resolve_repo_state_root(repo_root)` in `core/state_roots.py`

### 2. Hub Root

**Location**: `<hub_root>/.codex-autorunner/`

**Purpose**: Hub-level state for typed managed resources (`repo` plus
`agent_workspace`), orchestration metadata, and cross-resource coordination.

**Contents**:
- `manifest.yml` - Managed hub resources list (`repos:` plus `agent_workspaces:` and their `destination` config)
- `hub_state.json` - Hub state
- `config.yml` - Hub config
- `codex-autorunner-hub.log` - Hub logs
- `templates/` - Hub-scoped templates
- `runtimes/` - CAR-managed runtime workspace roots under `<runtime>/<workspace_id>/`
- `chat/channel_directory.json` - Cross-platform channel directory used for lightweight routing context
- **`orchestration.sqlite3`** - Hub SQLite store for orchestration metadata, bindings, executions, transcript mirrors, and event projections

**Resolution**: Hub root is typically the hub's repo root, using repo-local patterns.

#### Orchestration SQLite Database

The `orchestration.sqlite3` database is the canonical store for:

- **Thread/binding metadata**: Durable CAR thread registrations plus external
  channel bindings (Discord channel IDs, Telegram chat IDs) to consistent thread
  targets under repos or agent workspaces
- **Execution state**: Active/running/queued orchestration work items
- **Transcript mirrors**: Plain-text user/assistant message copies for search, previews, and debugging
- **Event projections**: Aggregated views of automation events, flow executions

**What orchestration.sqlite3 does NOT store** (remains authoritative elsewhere):
- **Flow engine internals**: Still stored in repo-local `flows.db`
- **Delivery/outbox state**: Discord and Telegram state databases remain authoritative for message delivery
- **Runtime conversation history**: Downstream runtimes (Codex, OpenCode) own their canonical conversation state
- **Reasoning traces/tool payloads**: Kept in runtime-native stores

**Resolution**: `resolve_hub_orchestration_db_path(hub_root)` in `core/state_roots.py`

#### Managed Agent Workspace Roots

CAR-managed agent workspaces live under:

- `<hub_root>/.codex-autorunner/runtimes/<runtime>/<workspace_id>/`

This root is reserved for first-class `agent_workspace` hub resources. In v1,
CAR allocates these paths itself and does not accept arbitrary external runtime
paths for managed agent workspaces.

The durable identity hierarchy is:

- `runtime binary -> agent workspace -> CAR thread -> live process`

The runtime binary is detected/configured separately. CAR does not install the
runtime as part of the state-root contract.

**Resolution**:
- `resolve_hub_runtimes_root(hub_root)`
- `resolve_hub_runtime_root(hub_root, runtime=...)`
- `resolve_hub_agent_workspace_root(hub_root, runtime=..., workspace_id=...)`

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

## Retention Taxonomy

Canonical roots define where CAR is allowed to store state. Retention classes
define how long that state should be kept.

A path can be canonical without being retained forever. For example,
`state.sqlite3`, `lock`, and workspace directories are canonical locations but
are runtime state, not durable artifacts.

### Retention Classes

| Class | Description | Cleanup Behavior |
|-------|-------------|------------------|
| `durable` | Source-of-truth or long-lived artifacts | Never deleted; may be compacted in place |
| `reviewable` | Bounded review artifacts | Pruned by explicit age/count/byte policy |
| `ephemeral` | Runtime, staging, or transient artifacts | Aggressively pruned when inactive |
| `cache-only` | Rebuildable caches | Deleted freely when not in active use |

### Per-Artifact Retention Mapping

The canonical retention contract is defined in
`.codex-autorunner/contextspace/spec.md` with detailed per-path mappings.

Key durable artifacts (never deleted):
- `tickets/` — ticket queue
- `contextspace/` — durable context docs (active_context.md, decisions.md, spec.md)
- `context_log.md` — context log snapshots
- `runs/<run_id>/dispatch_history/` and `reply_history/` — turn history
- `flows.db` — flow engine store
- Hub `orchestration.sqlite3` — orchestration store
- Stable reports (`reports/latest-*`, `final_report.md`)

Key reviewable artifacts (bounded pruning):
- `archive/worktrees/**` — worktree snapshots
- `archive/runs/**` — run archives
- `flows/<run_id>/` — flow artifacts
- `reports/` history files
- `github_context/`

Key ephemeral artifacts (aggressive pruning):
- `state.sqlite3` — runtime state database
- `app_server_workspaces/` — repo-local workspace state
- `filebox/inbox/`, `filebox/outbox/` — attachment staging
- `*.log` files
- `uploads/**`
- `lock` files

Key cache-only artifacts:
- `update_cache/` — update artifacts cache

### Cleanup Command

Use `car cleanup state` to reclaim disk space across all retention families:

```bash
# Preview what would be deleted
car cleanup state --dry-run

# Clean repo-local state only
car cleanup state --scope repo

# Clean global state only
car cleanup state --scope global

# Clean all scopes
car cleanup state --scope all
```

For details on the cleanup contract and safety guards, see
[State Cleanup Operations](ops/state-cleanup.md).

## Non-Canonical Locations (Caches Only)

These locations are explicitly **non-canonical** (ephemeral, disposable):

| Location | Purpose | Notes |
|----------|---------|-------|
| `/tmp/`, `$TMPDIR` | Temporary files | Never durable |
| `$XDG_CACHE_HOME` or `~/.cache` | Optional caches | Must be rebuildable |
| `__pycache__/` | Python bytecode | Auto-generated |

**Rule**: Any location outside the canonical roots must be:
1. A true cache (data is derivable from canonical sources)
2. Explicitly documented here
3. Safe to delete without data loss

## Authority Boundaries

This section defines which store is authoritative for which data domain.

### Repo-Local `flows.db` (Flow Engine Internals)

**Location**: `<repo_root>/.codex-autorunner/flows.db`

**Remains authoritative for**:
- Flow definition storage and versioning
- Flow execution state machine (pending, running, completed, failed)
- Flow step orchestration and dependency resolution
- Flow variable and context persistence

The hub `orchestration.sqlite3` may contain **projections** of flow execution status, but `flows.db` is the source of truth for flow-engine internals.

### Transport State Databases (Discord, Telegram)

**Discord**: `<repo_root>/.codex-autorunner/discord_state.sqlite3`
**Telegram**: `<repo_root>/.codex-autorunner/telegram_state.sqlite3`

These transport-specific databases remain authoritative for:
- Per-channel/topic delivery state and outbox queues
- Platform-specific message metadata
- Webhook state and callback tracking

After the orchestration SQLite cutover, **binding metadata** (which agent thread is bound to which Discord channel/Telegram chat) moves to hub `orchestration.sqlite3`, but **delivery state** remains in the transport databases.

### Legacy PMA Artifacts (Migration/Compatibility)

**Locations**:
- `<repo_root>/.codex-autorunner/pma/deliveries.jsonl`
- `<repo_root>/.codex-autorunner/pma/thread_*.json`
- `<repo_root>/.codex-autorunner/pma/queue.json`

These artifacts are now **compatibility inputs** rather than canonical stores:
- Used only for migration to hub SQLite and orchestration bindings
- No new data should be written to these paths
- Over time, these will become read-only archives

The canonical thread/binding state is now in `orchestration.sqlite3`.

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

def resolve_hub_state_root(hub_root: Path) -> Path:
    """Return the hub-scoped state root."""

def resolve_hub_orchestration_db_path(hub_root: Path) -> Path:
    """Return the canonical orchestration SQLite path under the hub state root."""

def resolve_hub_templates_root(hub_root: Path) -> Path:
    """Return the hub-scoped templates root."""

def resolve_hub_runtimes_root(hub_root: Path) -> Path:
    """Return the hub-scoped root for CAR-managed runtime workspaces."""

def resolve_hub_runtime_root(hub_root: Path, *, runtime: str) -> Path:
    """Return the managed root for one runtime under the hub."""

def resolve_hub_agent_workspace_root(
    hub_root: Path, *, runtime: str, workspace_id: str
) -> Path:
    """Return the canonical root for a managed agent workspace."""
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
