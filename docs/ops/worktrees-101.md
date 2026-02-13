# Worktrees 101 (Hub-managed)

This doc explains how CAR expects git worktrees to be created and registered when you are operating in **hub mode**.

## Terms

- **Hub**: the CAR “home” directory containing `.codex-autorunner/` + a manifest (`.codex-autorunner/manifest.yml`).
- **Base repo**: a normal git clone registered in the hub (manifest `kind: base`).
- **Worktree**: a git worktree directory registered in the hub (manifest `kind: worktree`), grouped under a base repo via `worktree_of`.

## Canonical creation paths (preferred)

### A) Hub Web UI
Use the hub UI action **“New Worktree”** for a base repo. This creates the worktree under the hub’s configured worktrees directory and registers it in the manifest.

### B) CLI
From the hub root, run:

- `car hub worktree create <base_repo_id> <branch> [--start-point <ref>]`

Examples:
- `car hub worktree create myrepo feature/pma-worktree-ux`
- `car hub worktree create myrepo feature/pma-worktree-ux --start-point origin/main`

Notes:
- If `--start-point` is omitted, CAR creates the worktree from `origin/<default-branch>`.
- The worktree directory is created under: `<hub_root>/<hub.worktrees_root>/<worktree_repo_id>/`
- Worktrees are treated as full repos and get their own `.codex-autorunner/` state/docs.

## When to use `car hub scan`

Use `car hub scan` when:
- you cloned/created repos under the hub’s repos/worktrees roots outside of CAR, OR
- you created a worktree manually via `git worktree add`, OR
- the hub UI does not show a repo/worktree you expect to be managed.

Command:
- `car hub scan --path <hub_root>`

Important:
- Scan is shallow (depth=1). Only immediate child directories of `hub.repos_root` and `hub.worktrees_root` are discovered.
- If you created a worktree outside `hub.worktrees_root`, either move it into that directory or update hub config so scan can discover it.

## Naming conventions and `worktree_of` grouping

CAR uses a simple convention to group worktrees under a base repo:

- Worktree repo_id / directory name:
  - `<base_repo_id>--<branch>`
  - branch is sanitized for filesystem safety (e.g. `/` may be replaced)

When CAR creates a worktree, it sets:
- `kind: worktree`
- `worktree_of: <base_repo_id>`
- `branch: <branch>`

When CAR scans for worktrees, it can *infer* `worktree_of` if the worktree directory name matches:
- `<base_repo_id>--<branch>`

If you created a worktree manually, prefer naming the directory like:
- `myrepo--feature-pma-worktree-ux`
so scan can infer grouping.

## Critical warning: do not copy `.codex-autorunner/` between worktrees

Each base repo and each worktree has its own `.codex-autorunner/` directory.

Do NOT copy `.codex-autorunner/` from a base repo into a worktree.
Instead:
- create/register the worktree via hub UI / CLI, or
- create worktree manually *then run* `car hub scan` so CAR can initialize it as needed.

Copying `.codex-autorunner/` can introduce stale locks, stale run metadata, and confusing state.

## Troubleshooting checklist

- Worktree not visible in hub UI:
  1) Confirm the directory is under `hub.worktrees_root`.
  2) Run: `car hub scan --path <hub_root>`
  3) Confirm the worktree directory has a `.git` entry.
- Worktree is visible but not grouped:
  - Rename directory to `<base_repo_id>--<branch>` and re-scan.
