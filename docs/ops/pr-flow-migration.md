# PR Flow migration cleanup

The refactored Flow Runtime does not read legacy PR flow state. You can remove old data safely.

## What changed

- Legacy PR flow state under `.codex-autorunner/pr_flow/` is ignored.
- Old `state.json` data is not imported; rerun flows if needed.

## Cleanup options

1) Run the helper script (dry-run by default):

```bash
./scripts/cleanup-legacy-pr-flow.sh
```

To delete the legacy directory and stale worktree folders:

```bash
./scripts/cleanup-legacy-pr-flow.sh --apply
```

2) Manual cleanup

- Delete `.codex-autorunner/pr_flow/` if it exists.
- Review `.codex-autorunner/worktrees/` and remove stale entries with `git worktree remove` or by deleting unregistered directories.

## Notes

- The cleanup script only removes worktree folders that are not registered in `git worktree list`.
- Use `git worktree list` to verify active worktrees before removing anything.
