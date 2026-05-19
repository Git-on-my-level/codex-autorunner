# Managed Threads

Managed threads are PMA-owned conversations tied to a repo, worktree, or agent workspace. CAR tracks lifecycle, runtime binding, progress, and transcripts.

Default to managed threads for work that fits in one clear prompt and one managed resource.

Common operations:

- Spawn: `car pma thread spawn --agent codex --repo <repo_id> --path <hub_root>`
- Send: `car pma thread send --id <thread_id> --message "..." --path <hub_root>`
- Watch intentionally: `car pma thread send --id <thread_id> --message "..." --watch --path <hub_root>`
- Status: `car pma thread status --id <thread_id> --path <hub_root>`
- Compact: `car pma thread compact --id <thread_id> --summary "..." --path <hub_root>`
- Retire: `car pma thread retire --id <thread_id> --path <hub_root>`

Reuse a relevant active managed thread before spawning a new one.

## Bound Chat Generations

Managed threads own chat lifecycle. A row is active or archived from the
managed thread target's `lifecycle_status`; Discord and Telegram surface
bindings are pointers to the current channel or topic generation.

`/newt` on a bound Discord channel or Telegram topic starts a new active
managed thread generation and rebinds the surface key to that thread. The old
generation may be retired, but it remains addressable through Retirement and the
direct chat detail route.

Stale surface archive events are history for a previous binding generation.
They must not own active row membership after a later `surface.rebound` points
the same surface key at a fresh active managed thread.
