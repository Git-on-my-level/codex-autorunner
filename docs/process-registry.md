# Managed Process Registry

CAR writes durable metadata for long-lived subprocesses under the hub root when
one is available:

- `.codex-autorunner/processes/<kind>/<handle_id>.json`
- repo/workspace fallback locations are used only for non-hub contexts

This registry is local state (not source code) and is intended for:

- auditability of spawned subprocesses
- crash recovery and reaping stale processes
- correlating parent CAR processes with child agent/app-server processes

## Record schema

Each JSON file stores one `ProcessRecord`:

- `kind` (`str`): process class (for example `opencode`, `codex_app_server`)
- `handle_id` / `workspace_id` (`str | null`): runtime process handle identifier
- `pid` (`int | null`): subprocess PID
- `pgid` (`int | null`): subprocess process-group id (preferred on Unix)
- `base_url` (`str | null`): optional service URL exposed by the process
- `command` (`list[str]`): full spawn command
- `owner_pid` (`int`): PID of the CAR process that spawned/owns this subprocess
- `started_at` (`str`): ISO timestamp when process started
- `metadata` (`object`): extra structured fields for feature-specific context

New records also include top-level `handle_id`. Older records that only have
`workspace_id` remain readable and reapable; CAR derives the record key from
`handle_id`, then `workspace_id`, then PID-only records.

Required metadata includes the server scope, runtime profile when present,
initial cwd, state directory, and runtime-specific fields such as `base_url`.

## Behavior

- Writes are atomic via temp-file replace.
- Runtime handles are keyed by process ownership scope, not by surface routing
  state. A Discord channel, Telegram chat, web tab, PMA thread, Codex thread,
  OpenCode session, or ticket id must not become a process registry ownership key.
- Reads and list operations validate schema.
- Delete removes only the target record file.
- All state is isolated under `.codex-autorunner/` (no shadow registry elsewhere).
- Surface/service startup runs the managed-process reaper before accepting runtime
  work. Reaping skips live CAR owners, terminates stale process groups, and
  deletes stale records.

## Diagnostic Workflow

Use these checks to answer: which process records exist, who owns them, and which
OS processes are still running?

- `car doctor processes --repo <repo_root>`: counts live `ps` snapshot entries and
  records under `.codex-autorunner/processes`.
- `car cleanup processes --repo <repo_root>`: attempts normal reaping (skips active
  owners).
- `car cleanup processes --repo <repo_root> --force`: force reaps all records,
  including when owners are still running (useful for stuck cleanup situations).

Example output from `car cleanup processes --force` should show whether each record
was killed and whether process records were removed.

Recommended defaults for long-lived runtime processes:

- `app_server.server_scope`: default `global`
- `app_server.max_handles`: default `1`
- `app_server.idle_ttl_seconds`: default `3600`
- `opencode.server_scope`: default `global`
- `opencode.max_handles`: default `1`
- `opencode.idle_ttl_seconds`: default `900`

This keeps one governed Codex app-server and one governed OpenCode server per
hub/runtime profile by default. Operators that need workspace isolation can opt
into `server_scope: workspace`; operators that want hotter reuse can raise
`max_handles` and `idle_ttl_seconds` explicitly.

If a process becomes orphaned or a previous run crashed before cleanup, these
commands let operators confirm ownership and recover without resorting to manual
`pkill`.

For the hub shared-state single-owner rollout checks, see
`docs/ops/hub-single-owner-verification.md`.
