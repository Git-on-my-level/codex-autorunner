# Worktree Retirements

Terminology: **retire** is the operator-facing lifecycle action. **Archive** is
the retained-storage mechanism and on-disk path family under
`.codex-autorunner/archive/`. New user-facing commands and docs should say
retire/retirement unless they are describing literal archive storage,
retention internals, or archive file browsing.

## Overview
When a hub-managed worktree is cleaned up, CAR snapshots the worktree's
`.codex-autorunner/` artifacts into the base repo. The default cleanup retirement
is a retire snapshot: tickets, contextspace docs, runs/dispatch history, flow
artifacts, the live `flows.db`, GitHub issue/PR context, and lightweight
metadata stay available for later review in the Retirement UI without copying the
full runtime state unless you opt into a full retire snapshot profile.

Retirement snapshots are **reviewable retained output**, not live source of truth. The
canonical live stores (`flows.db` for run history, `contextspace/` for durable
context, `tickets/` for the ticket queue) remain authoritative. Retirement
snapshots exist for operator review and audit.

Retirement snapshots are local runtime data and are not meant to be committed. The
base repo's `.codex-autorunner/` folder is gitignored.

## Retirement intents
CAR retirement behavior is now modeled by explicit intents instead of profile
exceptions:

- `review_snapshot`: non-destructive snapshot for manual review
- `retire_snapshot`: destructive worktree-retire snapshot that always keeps
  reviewable flow state, including `flows.db`
- `reset_car_state`: destructive CAR-state archive that preserves every CAR
  path it is about to reset. This intent is storage terminology for CAR-state
  reset preservation, not a user-facing command name.

The configured `portable` vs `full` retire snapshot profile only chooses how much additional
runtime-only state a review or retire snapshot keeps. It no longer decides
whether destructive flows preserve required review artifacts.

## Storage layout
Snapshots are stored under the base repo:

```
<base_repo>/.codex-autorunner/archive/
  worktrees/
    <worktree_repo_id>/
      <snapshot_id>/
        META.json
        contextspace/
        tickets/
        runs/
        flows/
        github_context/
        config/
          config.yml
```

Snapshots are staged into a temporary directory first. `META.json` is written
in the staging directory, and the snapshot becomes visible only after an
atomic rename into the final snapshot path. This keeps in-progress snapshots
out of archive listings and retention pruning.

## Cleanup behavior
- Worktree cleanup retires by default (`retire=true`).
- Retire snapshots use the `portable` retire snapshot profile by default. Set
  `pma.worktree_archive_profile: full` when you intentionally want a forensic
  retire snapshot that also copies runner state and logs in addition to the
  default reviewable flow state.
- If retirement snapshotting fails, cleanup stops unless `force_retire=true` is passed.
  Use force only when you accept losing the retirement snapshot for that worktree.
- Partial snapshots can happen when some paths are missing. In that case
  the snapshot `status` is `partial` and `META.json` lists `missing_paths`.
- Failed staging directories are cleaned up instead of being published as
  visible snapshots.

## Viewing retirements in the UI
Retirement snapshots can be browsed in the Web Hub. You can:
- browse snapshots by worktree ID and timestamp
- view snapshot metadata and `META.json`
- open retained files (tickets, contextspace, runs, flows, and any optional
  full-profile extras) in the retirement snapshot file viewer

## Troubleshooting
- **Permissions**: ensure the base repo and `.codex-autorunner/archive/`
  are writable by the hub process.
- **Disk full**: retirement snapshots can be large if runs include big attachments or
  long flow artifacts. Check free space on the base repo volume.
- **Partial snapshots**: inspect `META.json` for `missing_paths` or
  `skipped_symlinks`. Missing paths are often empty directories or
  artifacts that were never created in the worktree.
- **Logs**:
  - Hub-level failures: `.codex-autorunner/codex-autorunner-hub.log` in
    the hub root.
  - Snapshot copies: `logs/` inside the snapshot directory.

## Expected size and storage hygiene
Retirement storage size depends on run history and attachments. CAR prunes
retirement snapshot history automatically using PMA retention settings:

- `pma.worktree_archive_max_snapshots_per_repo`
- `pma.worktree_archive_max_age_days`
- `pma.worktree_archive_max_total_bytes`
- `pma.run_archive_max_entries`
- `pma.run_archive_max_age_days`
- `pma.run_archive_max_total_bytes`

Retention pruning after retirement is **best-effort**: it must not fail the
retirement operation itself. If pruning encounters an error, the snapshot is still
published and the pruning failure is logged.

Snapshots without a `META.json` file (incomplete staging directories) are
intentionally invisible to retention pruning and will not appear in prune
planning or dry-run reports.

To run pruning on demand, use:

```bash
car cleanup retirements --scope both
```

Add `--dry-run` to preview deletions without removing anything.

## Relation to State-Wide Cleanup

Worktree retirements are one of several retention families managed by CAR. The
umbrella `car cleanup state` command orchestrates cleanup across all families.
Each family belongs to a single retention class that determines its cleanup
behavior:

| Family | Path | Class | Live store? |
|--------|------|-------|-------------|
| worktree_archives | `archive/worktrees/` | reviewable | No — retained output only |
| run_archives | `archive/runs/` | reviewable | No — retained output only; `flows.db` is canonical live store |
| filebox | `filebox/` | ephemeral | No — staging only |
| reports | `reports/` | reviewable | No — history files (stable outputs like `latest-*` are durable) |
| workspaces | `app_server_workspaces/`, global `workspaces/` | ephemeral | No — supervisor state only |

To preview state-wide cleanup:

```bash
car cleanup state --dry-run --scope repo
```

For the full retention contract and artifact taxonomy, see
[STATE_ROOTS.md](../STATE_ROOTS.md) and `.codex-autorunner/contextspace/spec.md`.
