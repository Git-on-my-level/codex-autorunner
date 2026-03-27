# Worktree archives

## Overview
When a hub-managed worktree is cleaned up, CAR snapshots the worktree's
`.codex-autorunner/` artifacts into the base repo. The default cleanup archive
is a cleanup snapshot: tickets, contextspace docs, runs/dispatch history, flow
artifacts, the live `flows.db`, GitHub issue/PR context, and lightweight
metadata stay available for later review in the Archive UI without copying the
full runtime state unless you opt into a full archive profile.

Archives are local runtime data and are not meant to be committed. The
base repo's `.codex-autorunner/` folder is gitignored.

## Archive intents
CAR archive behavior is now modeled by explicit intents instead of profile
exceptions:

- `review_snapshot`: non-destructive snapshot for manual review
- `cleanup_snapshot`: destructive worktree-cleanup snapshot that always keeps
  reviewable flow state, including `flows.db`
- `reset_car_state`: destructive CAR-state archive that preserves every CAR
  path it is about to reset

The configured `portable` vs `full` profile only chooses how much additional
runtime-only state a review or cleanup snapshot keeps. It no longer decides
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
- Worktree cleanup archives by default (`archive=true`).
- Cleanup snapshots use the `portable` archive profile by default. Set
  `pma.worktree_archive_profile: full` when you intentionally want a forensic
  cleanup snapshot that also copies runner state and logs in addition to the
  default reviewable flow state.
- If archiving fails, cleanup stops unless `force_archive=true` is passed.
  Use force only when you accept losing the archive for that worktree.
- Partial snapshots can happen when some paths are missing. In that case
  the snapshot `status` is `partial` and `META.json` lists `missing_paths`.
- Failed staging directories are cleaned up instead of being published as
  visible snapshots.

## Viewing archives in the UI
Open the repo web UI and select the **Archive** tab. You can:
- browse snapshots by worktree ID and timestamp
- view snapshot metadata and `META.json`
- open archived files (tickets, contextspace, runs, flows, and any optional
  full-profile extras) in the archive file viewer

## Troubleshooting
- **Permissions**: ensure the base repo and `.codex-autorunner/archive/`
  are writable by the hub process.
- **Disk full**: archives can be large if runs include big attachments or
  long flow artifacts. Check free space on the base repo volume.
- **Partial snapshots**: inspect `META.json` for `missing_paths` or
  `skipped_symlinks`. Missing paths are often empty directories or
  artifacts that were never created in the worktree.
- **Logs**:
  - Hub-level failures: `.codex-autorunner/codex-autorunner-hub.log` in
    the hub root.
  - Snapshot copies: `logs/` inside the snapshot directory.

## Expected size and storage hygiene
Archive size depends on run history and attachments. CAR now prunes archive
history automatically using PMA retention settings:

- `pma.worktree_archive_max_snapshots_per_repo`
- `pma.worktree_archive_max_age_days`
- `pma.worktree_archive_max_total_bytes`
- `pma.run_archive_max_entries`
- `pma.run_archive_max_age_days`
- `pma.run_archive_max_total_bytes`

To run pruning on demand, use:

```bash
car cleanup archives --scope both
```

Add `--dry-run` to preview deletions without removing anything.

## Relation to State-Wide Cleanup

Worktree archives are one of several retention families managed by CAR. The
umbrella `car cleanup state` command orchestrates cleanup across all families:

- **worktree_archives**: `.codex-autorunner/archive/worktrees/` (reviewable)
- **run_archives**: `.codex-autorunner/archive/runs/` (reviewable)
- **filebox**: `.codex-autorunner/filebox/` (ephemeral)
- **reports**: `.codex-autorunner/reports/` (reviewable history)
- **workspaces**: repo-local `app_server_workspaces/` and global
  `~/.codex-autorunner/workspaces/` (ephemeral)

To preview state-wide cleanup:

```bash
car cleanup state --dry-run --scope repo
```

For the full retention contract and artifact taxonomy, see
[STATE_ROOTS.md](../STATE_ROOTS.md) and `.codex-autorunner/contextspace/spec.md`.
