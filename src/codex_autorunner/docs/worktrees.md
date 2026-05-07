# Worktrees

Hub-managed worktrees keep branch work visible to PMA and the hub UI.

Create a worktree through the hub when possible:

`car hub worktree create <base_repo_id> <branch> --path <hub_root>`

If a worktree was created manually, register it:

`car hub scan --path <hub_root>`

Never copy `.codex-autorunner/` between worktrees. Each worktree should own its own repo-local state.
