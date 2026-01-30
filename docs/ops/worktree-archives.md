# Worktree archives

## Overview
When a hub-managed worktree is cleaned up, CAR snapshots the worktree's
`.codex-autorunner/` artifacts into the base repo. This keeps tickets,
workspace docs, runs/dispatch history, flow artifacts, and logs available
for later review in the Archive UI.

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
        workspace/
        tickets/
        runs/
        flows/
        flows.db
        logs/
          codex-autorunner.log
          codex-server.log
        config/
          config.yml
        state/
          state.sqlite3
```

`META.json` is written last and contains the snapshot status plus summary
fields such as `file_count`, `total_bytes`, `flow_run_count`, and
`latest_flow_run_id`.

## Cleanup behavior
- Worktree cleanup archives by default (`archive=true`).
- If archiving fails, cleanup stops unless `force_archive=true` is passed.
  Use force only when you accept losing the archive for that worktree.
- Partial snapshots can happen when some paths are missing. In that case
  the snapshot `status` is `partial` and `META.json` lists `missing_paths`.
- Failures still write `META.json` with `status=failed` and an `error`.

## Viewing archives in the UI
Open the repo web UI and select the **Archive** tab. You can:
- browse snapshots by worktree ID and timestamp
- view snapshot metadata and `META.json`
- open archived files (tickets, workspace, runs, flows, logs) in the
  archive file viewer

## Troubleshooting
- **Permissions**: ensure the base repo and `.codex-autorunner/archive/`
  are writable by the hub process.
- **Disk full**: archives can be large if runs include big attachments or
  long logs. Check free space on the base repo volume.
- **Partial snapshots**: inspect `META.json` for `missing_paths` or
  `skipped_symlinks`. Missing paths are often empty directories or
  artifacts that were never created in the worktree.
- **Logs**:
  - Hub-level failures: `.codex-autorunner/codex-autorunner-hub.log` in
    the hub root.
  - Snapshot copies: `logs/` inside the snapshot directory.

## Expected size and storage hygiene
Archive size depends on run history, attachments, and logs. Expect small
snapshots for short-lived worktrees and larger ones for long-lived runs.
If storage grows quickly, consider a retention rule (for example, keep the
latest N snapshots per worktree or delete snapshots older than X days).

