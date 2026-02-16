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
