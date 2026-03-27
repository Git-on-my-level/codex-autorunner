# State Cleanup Operations

This document describes how to manage CAR state retention and disk reclamation
using `car cleanup state` and related commands.

## Overview

CAR manages state across several retention families:

| Family | Scope | Class | Description |
|--------|-------|-------|-------------|
| worktree_archives | repo | reviewable | Worktree cleanup snapshots |
| run_archives | repo | reviewable | Run archive entries |
| filebox | repo | ephemeral | Inbox/outbox staging |
| reports | repo | reviewable | Report history files |
| workspaces | repo/global | ephemeral | App-server workspace directories |

Retention classes determine cleanup behavior:

- **durable**: Never deleted (tickets, contextspace, flows.db, orchestration.sqlite3)
- **reviewable**: Bounded by age/count/byte policies
- **ephemeral**: Aggressively pruned when inactive
- **cache-only**: Deleted freely

For the full artifact mapping, see `.codex-autorunner/contextspace/spec.md`.

## Umbrella Command: `car cleanup state`

The `car cleanup state` command orchestrates cleanup across all retention
families.

### Basic Usage

```bash
# Preview what would be deleted (recommended first step)
car cleanup state --dry-run

# Clean repo-local state only
car cleanup state --scope repo

# Clean global state only (workspaces)
car cleanup state --scope global

# Clean all scopes
car cleanup state --scope all
```

### Dry-Run Report

The dry-run report shows:

- Cleanup results grouped by family
- Pruned counts and reclaimed bytes per family
- Blocked candidates that were skipped due to safety guards
- Total deleted count and bytes

Example output:

```
DRY RUN: CAR State Cleanup Report
==================================================
worktree_archives:
  pruned=2 bytes=15728640
run_archives:
  pruned=5 bytes=8388608
filebox:
  pruned=12 bytes=2097152
workspaces:
  pruned=3 bytes=10485760
  blocked=1

Total: deleted=22 bytes=36700160
```

### Scope Options

| Scope | Families Included |
|-------|-------------------|
| `repo` | worktree_archives, run_archives, filebox, reports, repo workspaces |
| `global` | global workspaces |
| `all` | All families |

## Safety Guards

`car cleanup state` will not delete:

### Active Run State

- Any `runs/<run_id>/` or `flows/<run_id>/` for non-terminal runs
- `state.sqlite3` or `app_server_threads.json` needed for live sessions

### Locked Workspaces

- Workspaces with a live lock (`lock_status: locked_alive`)
- Workspaces with an active runner/process

### Current/Live App-Server Workspaces

- Workspaces attached to active supervisors
- Workspaces with live handles or active threads
- Docker destination workspaces under repo-local `.codex-autorunner/`

### Canonical Source-of-Truth Stores

- `tickets/`
- `contextspace/` durable docs
- `context_log.md`
- `flows.db`
- Hub `orchestration.sqlite3`
- `manifest.yml`
- Stable reports (`reports/latest-*`, `final_report.md`)

Blocked candidates appear in the report with a `blocked=` count.

## Narrower Cleanup Commands

CAR provides targeted cleanup commands for specific families:

| Command | Description |
|---------|-------------|
| `car cleanup archives --scope both` | Prune worktree and run archives |
| `car cleanup filebox --scope both` | Prune FileBox inbox/outbox |
| `car cleanup reports` | Prune report history |
| `car cleanup processes` | Reap stale managed processes |
| `car hub runs cleanup` | Hub-specific run cleanup |
| `car hub worktree cleanup` | Hub-specific worktree cleanup |
| `car hub worktree archive` | Archive worktree before cleanup |

These commands remain available for targeted operations. `car cleanup state`
is the umbrella command for state-wide retention.

## Configuration

Retention policies are configured via `pma.*` settings in `codex-autorunner.yml`:

```yaml
pma:
  worktree_archive_max_snapshots_per_repo: 10
  worktree_archive_max_age_days: 30
  worktree_archive_max_total_bytes: 1073741824
  run_archive_max_entries: 100
  run_archive_max_age_days: 30
  run_archive_max_total_bytes: 536870912
  filebox_max_age_hours: 24
  report_max_history_files: 20
  report_max_total_bytes: 104857600
  workspace_max_age_days: 7
```

## Related Documentation

- [STATE_ROOTS.md](../STATE_ROOTS.md) — Canonical roots and retention taxonomy
- [worktree-archives.md](worktree-archives.md) — Worktree archive details
- `.codex-autorunner/contextspace/spec.md` — Full retention contract
