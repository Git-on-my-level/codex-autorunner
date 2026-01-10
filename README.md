# codex-autorunner

An opinionated autorunner that uses the Codex CLI to work on large tasks via a simple loop. On each loop we feed the Codex instance the last one's final output along with core documents.
1. TODO - Tracks long-horizon tasks
2. PROGRESS - High level overview of what's been done already that may be relevant for future agents
3. OPINIONS - Guidelines for how we should approach implementation
4. SPEC - Source-of-truth requirements and scope for large features/projects

## Sneak Peak
Run multiple agents on many repositories, with git worktree support
![Desktop hub](docs/screenshots/car-desktop-hub.png)

See the progress of your long running tasks with a high level overview
![Desktop repo dashboard](docs/screenshots/car-desktop-repo-dashboard.png)

Dive deep into specific agent execution with a rich but readable log
![Desktop logs](docs/screenshots/car-desktop-logs.png)

All memory and opinions are markdown files! Edit them directly or chat with the document!
![Desktop TODO](docs/screenshots/car-desktop-todo.png)

Use codex CLI directly for multi-shot problem solving or `/review`
![Desktop terminal](docs/screenshots/car-desktop-terminal.png)

Mobile-first experience, code on the go with Whisper support (BYOK)
![Mobile terminal](docs/screenshots/car-mobile-terminal.png)

## What it does
- Initializes a repo with Codex-friendly docs and config.
- Runs Codex in a loop against the repo, streaming logs.
- Tracks state, logs, and config under `.codex-autorunner/`.
- Exposes a power-user HTTP API and web UI for docs, logs, runner control, and a Codex TUI terminal.
- Optionally runs a Telegram bot for interactive, user-in-the-loop Codex sessions.
- Generates a pasteable repo snapshot (`.codex-autorunner/SNAPSHOT.md`) for sharing with other LLM chats.

CLI commands are available as `codex-autorunner` or the shorter `car`.

## Install
GitHub (pipx):
```
pipx install git+https://github.com/Git-on-my-level/codex-autorunner.git
```

From source (editable):
```
git clone https://github.com/Git-on-my-level/codex-autorunner.git
cd codex-autorunner
pip install -e .
```

### Optional extras
- Telegram bot support: `pip install codex-autorunner[telegram]`
- Voice transcription support: `pip install codex-autorunner[voice]`
- Dev tools (lint/test): `pip install codex-autorunner[dev]`
- Local dev alternative: `pip install -e .[extra]`

### Opinionated setup (macOS headless hub at `~/car-workspace`)
- One-shot setup (user scope): `scripts/install-local-mac-hub.sh`. It pipx-installs this repo, creates/initializes `~/car-workspace` as a hub, writes a launchd agent plist, and loads it. Defaults: host `0.0.0.0`, port `4173`, label `com.codex.autorunner`. Override via env (`WORKSPACE`, `HOST`, `PORT`, `LABEL`, `PLIST_PATH`, `PACKAGE_SRC`).
- Create/update the launchd agent plist and (re)load it: `scripts/launchd-hub.sh` (or `make launchd-hub`).
- Manual path if you prefer:
  - `pipx install .`
  - `car init --mode hub --path ~/car-workspace`
  - Copy `docs/ops/launchd-hub-example.plist` to `~/Library/LaunchAgents/com.codex.autorunner.plist`, replace `/Users/you` with your home, adjust host/port if desired, then `launchctl load -w ~/Library/LaunchAgents/com.codex.autorunner.plist`.
- The hub serves the UI/API from `http://<host>:<port>` and writes logs to `~/car-workspace/.codex-autorunner/codex-autorunner-hub.log`. Each repo under `~/car-workspace` should be a git repo with its own `.codex-autorunner/` (run `car init` in each).

#### Refresh a launchd hub to the current branch
When you change code in this repo and want the launchd-managed hub to run it:
1) Recommended: run the safe refresher, which installs into a new venv, flips `~/.local/pipx/venvs/codex-autorunner.current`, restarts launchd, health-checks, and auto-rolls back on failure:
```
make refresh-launchd
```

Important: avoid in-place pip/pipx installs against the live venv. During uninstall/reinstall, packaged static assets disappear and the UI can break while the server keeps running. Use the safe refresher or stop the service before manual installs.

2) Manual path (offline only; no rollback): stop launchd first, then reinstall into the launchd venv (pipx default paths shown; adjust if your label/paths differ):
```
$HOME/.local/pipx/venvs/codex-autorunner/bin/python -m pip install --force-reinstall /path/to/your/codex-autorunner
```
3) Restart the agent so it picks up the new bits (default label is `com.codex.autorunner`; default plist `~/Library/LaunchAgents/com.codex.autorunner.plist`):
```
launchctl unload ~/Library/LaunchAgents/com.codex.autorunner.plist 2>/dev/null || true
launchctl load -w ~/Library/LaunchAgents/com.codex.autorunner.plist
launchctl kickstart -k gui/$(id -u)/com.codex.autorunner
```
4) Tail the hub log to confirm it booted: `tail -n 50 ~/car-workspace/.codex-autorunner/codex-autorunner-hub.log`.

#### Health checks (recommended)
- `GET /health` returns 200 (verifies static assets are present).
- `GET /static/app.js` returns 200.
- Optional: `GET /` returns HTML (not a JSON error).
If you set a base path, prefix all checks with it.

## Quick start
1) Install (editable): `pip install -e .`
2) Initialize (per repo): `codex-autorunner init --git-init` (or `car init --git-init` if you prefer short). This creates `.codex-autorunner/config.yml`, state/log files, and the docs under `.codex-autorunner/`.
3) Run once: `codex-autorunner once` / `car once`
4) Continuous loop: `codex-autorunner run` / `car run`
5) If stuck: `codex-autorunner kill` then `codex-autorunner resume` (or the `car` equivalents)
6) Check status/logs: `codex-autorunner status`, `codex-autorunner log --tail 200` (or `car ...`)

## Configuration
- Root defaults live in `codex-autorunner.yml` (committed). These defaults are used when CAR generates `.codex-autorunner/config.yml`.
- Local overrides live in `codex-autorunner.override.yml` (gitignored). Use it for machine-specific tweaks; keep secrets in env vars.
- Repo config lives at `.codex-autorunner/config.yml` (generated). Edit it for repo-specific changes.

## Interfaces

CAR supports two interfaces with the same core engine. The web UI is the power
user control plane for multi-repo visibility and system control. The Telegram
bot is optimized for interactive back-and-forth, mirroring the Codex TUI
experience inside Telegram with user-in-the-loop approvals.

### Web UI (control plane)
1) Ensure the repo is initialized (`codex-autorunner init`) so `.codex-autorunner/config.yml` exists.
2) Start the API/UI backend: `codex-autorunner serve` (or `car serve`) — defaults to `127.0.0.1:4173`; override via `server.host`/`server.port` in `.codex-autorunner/config.yml`.
3) Open `http://127.0.0.1:4173` to use the UI, or call the FastAPI endpoints under `/api/*`.
   - The Terminal tab launches the configured Codex binary inside a PTY via websocket; it uses `codex.terminal_args` (defaults empty, so it runs `codex` bare unless you override). xterm.js assets are vendored under `static/vendor`.
   - If you need to serve under a proxy prefix (e.g., `/car`), set `server.base_path` in `.codex-autorunner/config.yml` or pass `--base-path` to `car serve/hub serve`; all HTTP/WS endpoints will be reachable under that prefix. Proxy must forward that prefix (e.g., Caddy `handle /car/* { reverse_proxy ... }` with a 404 fallback for everything else).
   - Chat composer shortcuts: desktop uses Cmd+Enter (or Ctrl+Enter) to send and Shift+Enter for newline; mobile uses Enter to send and Shift+Enter for newline.

### Telegram bot (interactive sessions)
- The interactive Telegram bot is separate from `notifications.telegram` (which is one-way notifications).
- Each operator should create their own Telegram bot token. Multi-user use requires explicit allowlists.
- Quickstart:
  1) Set env vars: `CAR_TELEGRAM_BOT_TOKEN` (and optionally `CAR_TELEGRAM_CHAT_ID`).
  2) In config, set `telegram_bot.enabled: true` and fill `allowed_user_ids` + `allowed_chat_ids`.
  3) Run `car telegram start --path <repo_or_hub>`.
  4) Use `/bind` (hub mode) and `/new` or `/resume` in Telegram.
- How it works (high level):
  - The bot polls the Telegram Bot API, allowlists chat/user IDs, and routes each topic to a workspace + Codex thread.
  - Messages and media are forwarded to the Codex app-server, streaming responses back to Telegram.
  - Approvals can be requested inline, giving a hands-on, TUI-like workflow without leaving Telegram.
- Details: `docs/telegram/architecture.md`, `docs/ops/telegram-bot-runbook.md`, and `docs/telegram/security.md`.

## Security and remote access
- The UI/API are effectively privileged access and can execute code on your machine (terminal + runner).
- Keep the server bound to `127.0.0.1` and use Tailscale (or another VPN) for remote access.
- If you must expose it, set `server.auth_token_env` and also put it behind an auth-enforcing reverse proxy (basic auth/SSO).
- Do not expose it publicly without protections. See `docs/web/security.md` for details.

### Auth token (optional)
If you set `server.auth_token_env`, the API requires `Authorization: Bearer <token>` on every request.
- Set the config: `server.auth_token_env: CAR_SERVER_TOKEN`.
- Export the token before starting the server: `export CAR_SERVER_TOKEN="..."`.
- Browser UI: visit `http://host:port/?token=...` once. The UI stores it in `sessionStorage` and removes it from the URL; websocket connections will include `token=...` automatically.
- CLI: requests automatically attach the token from `server.auth_token_env`; if the env var is missing, CLI commands will error.

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
- `sessions` — list terminal sessions (server-backed when available).
- `stop-session` — stop a terminal session by repo (`--repo`) or id (`--session`).
- `log` — view logs (tail or specific run).
- `edit` — open TODO/PROGRESS/OPINIONS/SPEC in `$EDITOR`.
- `ingest-spec` — generate TODO/PROGRESS/OPINIONS from SPEC using Codex (use `--force` to overwrite).
- `clear-docs` — reset TODO/PROGRESS/OPINIONS to empty templates (type CLEAR to confirm).
- `snapshot` — generate/update `.codex-autorunner/SNAPSHOT.md` (incremental by default when one exists; use `--from-scratch` to regenerate).
- `serve` — start the HTTP API (FastAPI) on host/port from config (defaults 127.0.0.1:4173).

## Snapshot (repo briefing)
- Web UI: open the Snapshot tab. If no snapshot exists, you’ll see “Generate snapshot”; otherwise you’ll see “Update snapshot (incremental)” and “Regenerate snapshot (from scratch)”, plus “Copy to clipboard”.
- CLI: `codex-autorunner snapshot` (or `car snapshot`) writes `.codex-autorunner/SNAPSHOT.md` and `.codex-autorunner/snapshot_state.json`.
