# codex-autorunner

An autorunner that uses the Codex CLI to work on large tasks via a simple loop. On each loop we feed the Codex instance the last one's final output along with 3 documents.
1. TODO - Tracks long-horizon tasks
2. PROGRESS - High level overview of what's been done already that may be relevant for future agents
3. OPINIONS - Guidelines for how we should approach implementation

## What it does
- Initializes a repo with Codex-friendly docs and config.
- Runs Codex in a loop against the repo, streaming logs.
- Tracks state, logs, and config under `.codex-autorunner/`.
- Exposes an HTTP API and web UI for docs, logs, runner control, and a Codex TUI terminal.

CLI commands are available as `codex-autorunner` or the shorter `car`.

## Quick start
1) Install (editable): `pip install -e .`
2) Initialize (per repo): `codex-autorunner init --git-init` (or `car init --git-init` if you prefer short). This creates `.codex-autorunner/config.yml`, state/log files, and the docs under `.codex-autorunner/`.
3) Run once: `codex-autorunner once` / `car once`
4) Continuous loop: `codex-autorunner run` / `car run`
5) If stuck: `codex-autorunner kill` then `codex-autorunner resume` (or the `car` equivalents)
6) Check status/logs: `codex-autorunner status`, `codex-autorunner log --tail 200` (or `car ...`)

## Run the web server/UI
1) Ensure the repo is initialized (`codex-autorunner init`) so `.codex-autorunner/config.yml` exists.
2) Start the API/UI backend: `codex-autorunner serve` (or `car serve`) — defaults to `127.0.0.1:4173`; override via `server.host`/`server.port` in `.codex-autorunner/config.yml`.
3) Open `http://127.0.0.1:4173` to use the UI, or call the FastAPI endpoints under `/api/*`.
   - The Terminal tab launches the configured Codex binary inside a PTY via websocket; it uses `codex.terminal_args` (defaults empty, so it runs `codex` bare unless you override). xterm.js assets are vendored under `static/vendor`.

## Git hooks
- Install dev tools: `pip install -e .[dev]`
- Point Git to the repo hooks: `git config core.hooksPath .githooks`
- The `pre-commit` hook runs `scripts/check.sh` (Black formatting check + pytest). Run it manually with `./scripts/check.sh` before committing or in CI.

## Commands (CLI)
- `init` — seed config/state/docs.
- `run` / `once` — run the loop (continuous or single iteration).
- `resume` — clear stale lock/state and restart; `--once` for a single run.
- `kill` — SIGTERM the running loop and mark state error.
- `status` — show current state and outstanding TODO count.
- `log` — view logs (tail or specific run).
- `edit` — open TODO/PROGRESS/OPINIONS in `$EDITOR`.
- `serve` — start the HTTP API (FastAPI) on host/port from config (defaults 127.0.0.1:4173).
