Codex Autorunner - Design

Single-repo autorunner that drives the Codex app-server (with OpenCode support) using markdown files under `.codex-autorunner/` as the control surface. Ships a CLI and local web UI/API; hub mode supervises multiple repos/worktrees. The Codex CLI is primarily used for the interactive terminal surface (PTY).

## Goals / Non-goals
- Goals: autonomous loop, doc-driven control surface, small local footprint, repo-local state, UI + API for control.
- Non-goals: hosted service, plugin ecosystem, SDK-only integrations, multi-tenant infra.

## Architecture
- Engine: reads/writes tickets (and optional workspace docs), builds prompts, runs Codex subprocesses, logs output, manages state/locks, stop rules, optional git commits.
- CLI: Typer wrapper around engine for init/run/once/status/log/edit/doctor/etc.
- Server/UI: FastAPI with the same engine, static UI, workspace editor, unified file chat, terminal websocket, logs and runner control.
- Hub mode: supervises many repos and worktrees via a manifest; provides hub API for scan/run/stop/resume/init and usage.

## Layout and config
Repo root:
  codex-autorunner.yml (defaults, committed)
  codex-autorunner.override.yml (local overrides, gitignored)
  .codex-autorunner/
    tickets/ (required; `TICKET-###.md`)
    workspace/ (optional; `active_context.md`, `decisions.md`, `spec.md`)
    config.yml (generated)
    state.sqlite3, codex-autorunner.log, codex-server.log, lock
    prompt.txt (optional template)
Hub additions:
  .codex-autorunner/manifest.yml, hub_state.json, codex-autorunner-hub.log

Config sections (repo): docs, codex, prompt, runner, git, github, server, terminal, voice, log, server_log, app_server, opencode.
Precedence: built-ins < codex-autorunner.yml < override < .codex-autorunner/config.yml < env.

## Core loop
- Select the active ticket target under `.codex-autorunner/tickets/`.
- Build prompt from the ticket content, optional workspace doc excerpts, and bounded prior run output.
- Run Codex app-server with streaming logs via OpenCode runtime.
- Update state and stop on non-zero exit, stop_after_runs, wallclock limit, or external stop flag.

## API surface (repo)
- Workspace: /api/workspace, /api/workspace/{kind}, /api/workspace/spec/ingest.
- File chat: /api/file-chat, /api/file-chat/pending, /api/file-chat/apply, /api/file-chat/discard, /api/file-chat/interrupt.
- Ticket chat legacy wrappers: /api/tickets/{index}/chat, /api/tickets/{index}/chat/pending|apply|discard|interrupt.
- Runner/logs/state: /api/run/*, /api/state, /api/logs, /api/logs/stream.
- Terminal: /api/terminal websocket, /api/sessions.
- Voice: /api/voice/config, /api/voice/transcribe.
- GitHub: /api/github/* (issue->spec, PR sync, status).
- Usage: /api/usage, /api/usage/series.

## Hub API (high level)
- /hub/repos, /hub/repos/scan, /hub/repos/{id}/run|stop|resume|kill|init.
- /hub/worktrees/create|cleanup.
- /hub/usage, /hub/usage/series.
