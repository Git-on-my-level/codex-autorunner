# codex-autorunner

An autorunner that uses the Codex CLI to work on large tasks via a simple loop. On each loop we feed the Codex instance the last one's final output along with core documents.
1. TODO - Tracks long-horizon tasks
2. PROGRESS - High level overview of what's been done already that may be relevant for future agents
3. OPINIONS - Guidelines for how we should approach implementation
4. SPEC - Source-of-truth requirements and scope for large features/projects

## What it does
- Initializes a repo with Codex-friendly docs and config.
- Runs Codex in a loop against the repo, streaming logs.
- Tracks state, logs, and config under `.codex-autorunner/`.
- Exposes an HTTP API and web UI for docs, logs, runner control, and a Codex TUI terminal.
- Generates a pasteable repo snapshot (`.codex-autorunner/SNAPSHOT.md`) for sharing with other LLM chats.

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
   - If you need to serve under a proxy prefix (e.g., `/car`), set `server.base_path` in `.codex-autorunner/config.yml` or pass `--base-path` to `car serve/hub serve`; all HTTP/WS endpoints will be reachable under that prefix. Proxy must forward that prefix (e.g., Caddy `handle /car/* { reverse_proxy ... }` with a 404 fallback for everything else).

## Local install (macOS headless hub at `~/car-workspace`)
- One-shot setup (user scope): `scripts/install-local-mac-hub.sh`. It pipx-installs this repo, creates/initializes `~/car-workspace` as a hub, writes a launchd agent plist, and loads it. Defaults: host `0.0.0.0`, port `4173`, label `com.codex.autorunner`. Override via env (`WORKSPACE`, `HOST`, `PORT`, `LABEL`, `PLIST_PATH`, `PACKAGE_SRC`).
- Create/update the launchd agent plist and (re)load it: `scripts/launchd-hub.sh` (or `make launchd-hub`).
- Manual path if you prefer:
  - `pipx install .`
  - `car init --mode hub --path ~/car-workspace`
  - Copy `docs/ops/launchd-hub-example.plist` to `~/Library/LaunchAgents/com.codex.autorunner.plist`, replace `/Users/you` with your home, adjust host/port if desired, then `launchctl load -w ~/Library/LaunchAgents/com.codex.autorunner.plist`.
- The hub serves the UI/API from `http://<host>:<port>` and writes logs to `~/car-workspace/.codex-autorunner/codex-autorunner-hub.log`. Each repo under `~/car-workspace` should be a git repo with its own `.codex-autorunner/` (run `car init` in each).

## Refresh a launchd hub to the current branch
When you change code in this repo and want the launchd-managed hub to run it:
1) Reinstall into the launchd venv (pipx default paths shown; adjust if your label/paths differ):
```
$HOME/.local/pipx/venvs/codex-autorunner/bin/python -m pip install --force-reinstall /path/to/your/codex-autorunner
```
2) Restart the agent so it picks up the new bits (default label is `com.codex.autorunner`; default plist `~/Library/LaunchAgents/com.codex.autorunner.plist`):
```
launchctl unload ~/Library/LaunchAgents/com.codex.autorunner.plist 2>/dev/null || true
launchctl load -w ~/Library/LaunchAgents/com.codex.autorunner.plist
launchctl kickstart -k gui/$(id -u)/com.codex.autorunner
```
3) Tail the hub log to confirm it booted: `tail -n 50 ~/car-workspace/.codex-autorunner/codex-autorunner-hub.log`.
4) One-liner on this machine: `make refresh-launchd` (overrides available: `PIPX_PYTHON`, `LAUNCH_AGENT`, `LAUNCH_LABEL`).
5) Or use the script (similar to `scripts/install-local-mac-hub.sh`): `scripts/refresh-local-mac-hub.sh`.

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
- `edit` — open TODO/PROGRESS/OPINIONS/SPEC in `$EDITOR`.
- `ingest-spec` — generate TODO/PROGRESS/OPINIONS from SPEC using Codex (use `--force` to overwrite).
- `clear-docs` — reset TODO/PROGRESS/OPINIONS to empty templates (type CLEAR to confirm).
- `snapshot` — generate/update `.codex-autorunner/SNAPSHOT.md` (incremental by default when one exists; use `--from-scratch` to regenerate).
- `serve` — start the HTTP API (FastAPI) on host/port from config (defaults 127.0.0.1:4173).

## Snapshot (repo briefing)
- Web UI: open the Snapshot tab. If no snapshot exists, you’ll see “Generate snapshot”; otherwise you’ll see “Update snapshot (incremental)” and “Regenerate snapshot (from scratch)”, plus “Copy to clipboard”.
- CLI: `codex-autorunner snapshot` (or `car snapshot`) writes `.codex-autorunner/SNAPSHOT.md` and `.codex-autorunner/snapshot_state.json`.
