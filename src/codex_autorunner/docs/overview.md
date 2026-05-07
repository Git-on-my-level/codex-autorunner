# CAR Overview

Codex Autorunner (CAR) coordinates agent work across a hub, repos, worktrees, and chat surfaces.

The hub is the control plane. It owns the manifest, PMA state, PMA docs, managed thread records, filebox, automation state, and global web UI.

Repos and worktrees are execution workspaces. They own ticket queues, repo contextspace docs, repo FileBox state, and ticket-flow runtime artifacts.

PMA is the hub-level coordinator. It should delegate normal implementation to managed threads or ticket flows, keep hub-level memory in PMA docs, and use the filesystem as the source of truth.

Use `car docs list --path <hub_root>` to discover available docs and `car docs search <query> --path <hub_root>` when you need the right operational guide.
