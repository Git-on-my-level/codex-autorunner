# Progress

- Initialized codex-autorunner project structure and CLI.
- Added default docs (TODO, PROGRESS, OPINIONS) and config for dogfooding.
- Installed package locally via `pip install -e .` for immediate CLI availability.
- Implemented V2 backend pieces: FastAPI server, RunnerManager threading, chat endpoint, log streaming, and serve command.
- Added server config defaults (host/port/auth token) and updated deps for FastAPI/Uvicorn.
- Pinned Click to <8.2 to keep Typer help output working.
- Prepared backlog for building the V2 web UI and wiring SSE/WebSocket streams.
- Added kill/resume support (CLI + API), runner PID tracking, and status reporting for headless control.
- Relocated TODO/PROGRESS/OPINIONS into .codex-autorunner/ and set config defaults accordingly.
- Built the V2 static web UI (Dashboard/Docs/Logs/Chat) with dark theme, API wiring for state/docs/logs/chat, and served via FastAPI static assets.
- Added streaming: new `/api/chat/stream` SSE endpoint plus UI hooks for live chat responses and log tailing via `/api/logs/stream`, including toggle controls and auth-aware streaming fetchers.
- Exposed kill/resume runner controls in the dashboard UI, wiring buttons to the new API endpoints, refreshing state after actions, and styling the kill button distinctly.
- Added a git `pre-commit` hook that runs a repo check script (`scripts/check.sh`) to enforce Black formatting and pytest before commits, documented hook setup, and added dev extras plus a smoke test for static assets.
