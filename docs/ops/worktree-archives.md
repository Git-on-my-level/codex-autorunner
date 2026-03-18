# Worktree archives

## Overview
When a hub-managed worktree is cleaned up, CAR snapshots the worktree's
`.codex-autorunner/` artifacts into the base repo. The default cleanup archive
is a portable review snapshot: tickets, contextspace docs, runs/dispatch
history, flow artifacts, GitHub issue/PR context, and lightweight metadata stay
available for later review in the Archive UI without copying the live flow DB
or other bulky runtime state.

Archives are local runtime data and are not meant to be committed. The
base repo's `.codex-autorunner/` folder is gitignored.

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

`META.json` is written last and contains the snapshot status plus summary
fields such as `file_count`, `total_bytes`, `flow_run_count`, and
`latest_flow_run_id`.

## Cleanup behavior
- Worktree cleanup archives by default (`archive=true`).
- Cleanup snapshots use the `portable` archive profile by default. Set
  `pma.worktree_archive_profile: full` when you intentionally want a forensic
  snapshot that also copies `flows.db`, runner state, and logs.
- If archiving fails, cleanup stops unless `force_archive=true` is passed.
  Use force only when you accept losing the archive for that worktree.
- Partial snapshots can happen when some paths are missing. In that case
  the snapshot `status` is `partial` and `META.json` lists `missing_paths`.
- Failures still write `META.json` with `status=failed` and an `error`.

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
