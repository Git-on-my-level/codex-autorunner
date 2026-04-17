# State Cleanup Operations

This document describes how to manage CAR state retention and disk reclamation
using `car cleanup state` and related commands.

## Overview

CAR manages state across several retention families:

| Family | Scope | Class | Description |
|--------|-------|-------|-------------|
| worktree_archives | repo | reviewable | Worktree cleanup snapshots |
| run_archives | repo | reviewable | Run archive entries |
| logs | repo/global | ephemeral | Run logs and update log |
| uploads | repo | ephemeral | Terminal and Telegram upload staging |
| github_context | repo | reviewable | GitHub context bundles |
| review_runs | repo | reviewable | Review run artifacts |
| filebox | repo | ephemeral | Inbox/outbox staging |
| reports | repo | reviewable | Report history files |
| update_cache | global | cache-only | Update artifacts cache |
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

# Clean global state only (update cache/logs, workspaces)
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
| `repo` | worktree_archives, run_archives, logs, uploads, github_context, review_runs, filebox, reports, repo workspaces |
| `global` | update_cache, logs, global workspaces |
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

- Workspaces with live `codex_app_server` process records in the current repo
- Workspaces carrying `lock` or `run.json` guard markers
- The current repo workspace when `app_server_threads.json` is present

### Canonical Source-of-Truth Stores

- `tickets/`
- `contextspace/` durable docs
- `context_log.md`
- `flows.db`
- Hub `orchestration.sqlite3`
- `manifest.yml`
- Stable reports (`reports/latest-*`, `final_report.md`)

Blocked candidates appear in the report with a `blocked=` count and a
human-readable reason.

### Workspace Retention Guards

Workspace retention planning (`plan_workspace_retention`) blocks pruning when:

- The workspace root violates a canonical-root boundary
- The workspace is marked active (running process)
- The workspace has a live lock file
- The workspace contains a `run.json` current-run marker

Global workspace cleanup skips entirely when hub manifest visibility is too
weak to prove which shared workspaces are active.

## Dry-Run And Execute Fidelity

The dry-run and execute paths must classify the **same candidates** with the
**same keep/prune/block reasons** for the same inputs. The only difference is
that the execute path mutates the filesystem; dry-run does not.

When a candidate is blocked, kept, or pruned, the reason must be visible in
tests, summaries, or logs. This attributability contract lets operators compare
dry-run output against actual cleanup results and trust that the dry-run report
accurately predicts what will happen.

## Best-Effort And Skip Contracts

Several cleanup behaviors are intentionally best-effort:

- **Post-archive retention pruning**: must not fail the archive operation
  itself. Pruning errors are logged but do not propagate.
- **Incomplete archive snapshots**: snapshots without `META.json` are
  intentionally invisible to retention planning and will not appear in prune
  candidate lists.
- **Run-archive policy fallback**: when repo config cannot be loaded, run
  archive retention falls back to built-in defaults.
- **Stable report preservation**: `prune_report_directory()` always preserves
  stable-prefix outputs (`latest-*`, `final_report.md`) regardless of the
  history budget.
- **Filebox symlinks**: `prune_filebox_root()` ignores symlinks and non-files.
- **Global workspace skip**: when hub manifest visibility is insufficient to
  determine which shared workspaces are active, global workspace cleanup
  returns zero candidates rather than risking active-workspace deletion.

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
  run_archive_max_entries: 200
  run_archive_max_age_days: 30
  run_archive_max_total_bytes: 1073741824
  filebox_inbox_max_age_days: 7
  filebox_outbox_max_age_days: 7
  report_max_history_files: 20
  report_max_total_bytes: 5242880
  app_server_workspace_max_age_days: 7
```

## Related Documentation

- [STATE_ROOTS.md](../STATE_ROOTS.md) — Canonical roots and retention taxonomy
- [worktree-archives.md](worktree-archives.md) — Worktree archive details
- `.codex-autorunner/contextspace/spec.md` — Full retention contract
