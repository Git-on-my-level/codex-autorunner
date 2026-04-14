Codex Autorunner - Design

Agent coordination hub that drives Codex app-server and OpenCode using markdown docs in .codex-autorunner/ as the control surface. Ships a CLI and local web UI/API; hub mode supervises multiple repos/worktrees and is the primary interface. Single-repo mode exists for CAR development but is not recommended for general use. The Codex CLI is primarily used for the interactive terminal surface (PTY).

## Goals / Non-goals
- Goals: autonomous loop, doc-driven control surface, small local footprint, repo-local state, UI + API for control.
- Non-goals: hosted service, plugin ecosystem, SDK-only integrations, multi-tenant infra.

## Architecture
- Engine: protocol-agnostic control layer that reads/writes docs, builds prompts, manages state/locks, stop rules, optional git commits, and delegates all backend execution to adapters.
- Hub: supervises many repos and worktrees via a manifest; provides hub API for scan/run/stop/resume/init and usage. This is the primary mode of operation.
- Server/UI: FastAPI with the same engine, static UI, doc chat, terminal websocket, logs and runner control.
- CLI: Typer wrapper around engine for init/run/once/status/log/edit/doctor/snapshot/etc.

> Constitution alignment: transport/vendor-specific logic (Codex subprocesses, app-server/OpenCode runtime, Telegram, etc.) lives in adapters/surfaces; the Engine consumes a protocol-neutral backend interface. This preserves the one-way dependency rule in `docs/car_constitution/20_ARCHITECTURE_MAP.md`.

## Layout and config
Hub root:
  codex-autorunner.yml (defaults, committed)
  codex-autorunner.override.yml (local overrides, gitignored)
  .codex-autorunner/
    manifest.yml, hub_state.json, codex-autorunner-hub.log
    config.yml (generated)
  repos/ (managed repositories)

Per-repo (under hub or standalone for development):
  .codex-autorunner/
    tickets/ (TICKET-###.md files)
    contextspace/ (active_context.md, decisions.md, spec.md)
    config.yml (generated)
    state.sqlite3, codex-autorunner.log, codex-server.log, lock
    prompt.txt (optional template)

Config sections (repo): docs, codex, prompt, runner, git, github, server, terminal, voice, log, server_log, app_server, opencode.
Precedence: built-ins < codex-autorunner.yml < override < .codex-autorunner/config.yml < env.

## Core loop
- Parse TODO checkboxes and preserve ordering.
- Build prompt from docs plus bounded prior run output.
- Run the configured execution backend (for example Codex app-server with OpenCode runtime, or an ACP-backed runtime such as Hermes).
- Update state and stop on empty TODOs, non-zero exit, stop_after_runs, wallclock limit, or external stop flag.

If `.codex-autorunner/workspace/` still exists with content from older installs, migrate to `contextspace/` using `docs/migrations/workspace-to-contextspace.md`.

## API surface (repo)
- Docs: /api/docs, /api/docs/{kind}, doc chat endpoints, /api/ingest-spec, /api/docs/clear.
- Snapshot: /api/snapshot.
- Runner/logs/state: /api/run/*, /api/state, /api/logs, /api/logs/stream.
- Terminal: /api/terminal websocket, /api/sessions.
- Voice: /api/voice/config, /api/voice/transcribe.
- GitHub: /api/github/* (issue->spec, PR sync, status).
- Usage: /api/usage, /api/usage/series.

## Hub API (high level)
- /hub/repos, /hub/repos/scan, /hub/repos/{id}/run|stop|resume|kill|init.
- /hub/worktrees/create|cleanup.
- /hub/usage, /hub/usage/series.
