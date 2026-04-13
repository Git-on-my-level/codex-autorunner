# Managed Process Registry

CAR writes durable metadata for long-lived subprocesses under:

- `.codex-autorunner/processes/<kind>/<workspace_id>.json`
- `.codex-autorunner/processes/<kind>/<pid>.json` (for the same process, with
  `workspace_id` in `metadata.workspace_id`)

This registry is local state (not source code) and is intended for:

- auditability of spawned subprocesses
- crash recovery and reaping stale processes
- correlating parent CAR processes with child agent/app-server processes

## Record schema

Each JSON file stores one `ProcessRecord`:

- `kind` (`str`): process class (for example `opencode`, `codex_app_server`)
- `workspace_id` (`str | null`): optional workspace identifier
- `pid` (`int | null`): subprocess PID
- `pgid` (`int | null`): subprocess process-group id (preferred on Unix)
- `base_url` (`str | null`): optional service URL exposed by the process
- `command` (`list[str]`): full spawn command
- `owner_pid` (`int`): PID of the CAR process that spawned/owns this subprocess
- `started_at` (`str`): ISO timestamp when process started
- `metadata` (`object`): extra structured fields for feature-specific context

## Behavior

- Writes are atomic via temp-file replace.
- Workspace-keyed and pid-keyed records are written for OpenCode processes so either
  key can be used by cleanup paths.
- Reads and list operations validate schema.
- Delete removes only the target record file.
- All state is isolated under `.codex-autorunner/` (no shadow registry elsewhere).

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

Recommended global defaults for long-lived OpenCode processes:

- `opencode.server_scope`: default `workspace`
- `opencode.max_handles`: default `4`
- `opencode.idle_ttl_seconds`: default `900`

This keeps reuse on by default without letting long-running CAR hosts accumulate
dozens of idle workspace-scoped OpenCode servers. Operators that prefer fewer
processes across many repos can opt into `opencode.server_scope: global`; operators
that want hotter reuse can raise `max_handles` and `idle_ttl_seconds` explicitly.

If a process becomes orphaned or a previous run crashed before cleanup, these
commands let operators confirm ownership and recover without resorting to manual
`pkill`.

For the hub shared-state single-owner rollout checks, see
`docs/ops/hub-single-owner-verification.md`.
