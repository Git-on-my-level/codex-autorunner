# Managed Threads

Managed threads are PMA-owned conversations tied to a repo, worktree, or agent workspace. CAR tracks lifecycle, runtime binding, progress, and transcripts.

Default to managed threads for work that fits in one clear prompt and one managed resource.

Common operations:

- Spawn: `car pma thread spawn --agent codex --repo <repo_id> --path <hub_root>`
- Send: `car pma thread send --id <thread_id> --message "..." --path <hub_root>`
- Watch intentionally: `car pma thread send --id <thread_id> --message "..." --watch --path <hub_root>`
- Status: `car pma thread status --id <thread_id> --path <hub_root>`
- Compact: `car pma thread compact --id <thread_id> --summary "..." --path <hub_root>`
- Archive: `car pma thread archive --id <thread_id> --path <hub_root>`

Reuse a relevant active managed thread before spawning a new one.
