Codex Autorunner – Design

Single-repo autorunner that drives the Codex CLI using three markdown docs (TODO, PROGRESS, OPINIONS) as the control surface. Supports a CLI (V1) and a local web server/UI (V2) built on the same engine.

## Goals and Non-goals
- Goals: run Codex autonomously in a loop; keep TODO/PROGRESS/OPINIONS in sync; expose both CLI and local web UI; keep footprint small and repo-local.
- Non-goals: hosted/cloud service, multi-repo orchestration, SDK-based Codex integration (CLI only), complex plugin systems.

## Architecture
- Core engine: reads/writes docs, builds prompts, runs Codex subprocesses, logs output, manages state, implements main loop with backoff/stop rules.
- CLI (V1): thin Typer/Click wrapper exposing init/run/once/status/log/edit/doctor and uses engine directly.
- Web server + UI (V2): `codex-autorunner serve` starts FastAPI + RunnerManager (background loop thread) + static UI (SPA or simple JS) talking to the HTTP API.
- Single-process, single-repo; repo root detected from CWD or `--repo`.

## Repo Layout and Config
```
<repo>/
  TODO.md
  PROGRESS.md
  OPINIONS.md
  .codex-autorunner/
    config.yml
    state.json
    codex-autorunner.log
    lock
    prompt.txt   # optional template
```
- Config (`.codex-autorunner/config.yml`, versioned):
  - docs: paths for todo/progress/opinions.
  - codex: `binary`, `args` (default `["--yolo","exec","--sandbox","danger-full-access"]`).
  - prompt: `prev_run_max_chars`, optional `template` path.
  - runner: `sleep_seconds`, `stop_after_runs`, `max_wallclock_seconds`.
  - git: `auto_commit`, `commit_message_template`.
  - server: `host`, `port`.
- State (`state.json`): `last_run_id`, `status` (idle/running/error), `last_exit_code`, timestamps.
- Log: append-only `codex-autorunner.log` with run markers `=== run {id} start/end (code X) ===`.

## Engine Responsibilities
- Locking: `.codex-autorunner/lock` stores PID; refuse concurrent runs unless `--force`; clean stale locks via `kill`/`resume`.
- TODO parsing: `- [ ]` outstanding; `- [x/X]` done; order matters.
- Prompt: include docs, optional previous run block (clipped), stable instruction block; allow template placeholders `{{TODO}}`, `{{PROGRESS}}`, `{{OPINIONS}}`, `{{PREV_RUN_OUTPUT}}`.
- Previous output extraction: pull prior run block from log, trim to `prev_run_max_chars`.
- Codex execution: run subprocess with stdout/stderr streamed; log each line with timestamp/run id.
- Main loop: update state before/after runs; stop on empty TODOs, non-zero exit, stop_after_runs, wallclock budget, or external stop flag; optional git add/commit when enabled.
- Error handling: fail fast on missing config/docs/binary; set `status="error"` on non-zero exits.

## CLI Commands (V1)
- `init` — seed config/state/docs (optional `--git-init`, `--force`).
- `run` / `once` — continuous loop or single run; honors stop_after_runs/wallclock; uses lock.
- `status` — show state and outstanding TODO count.
- `log` — show specific run or tail lines.
- `edit` — open docs in `$EDITOR`.
- `doctor` — validate repo/config/docs/binary.
- `kill` / `resume` — terminate stuck loop and restart after clearing stale lock/state.

## Web Server + UI (V2)
- Process model: FastAPI HTTP server + RunnerManager thread controlling the loop; uses same engine/state files.
- API (defaults `127.0.0.1:4173`):
  - GET `/api/docs`, PUT `/api/docs/{todo|progress|opinions}` — atomic doc reads/writes.
  - GET `/api/state` — state plus derived fields (e.g., outstanding TODO count).
  - POST `/api/run/start` (optional `{once: true}`), `/api/run/stop`, `/api/run/kill`, `/api/run/resume` — manage loop.
  - GET `/api/logs` (by run_id or tail), GET `/api/logs/stream` — live log SSE.
  - POST `/api/chat` (blocking) and `/api/chat/stream` (SSE) — Codex chat with optional doc inclusion; chat does not auto-edit docs.
- UI views: dashboard (status, controls), docs editor (tabs for three docs with checkbox rendering), logs tail/stream, chat panel (include-doc checkboxes, streaming output, optional “add as TODO” helper).
- Mobile: simple responsive layout with top/bottom nav; keep bundle small and avoid heavy frameworks.

## Extensibility and Implementation Notes
- Config versioning with migrations or explicit failures on mismatch; preserve unknown fields.
- Optional extras section for future read-only docs; prompt template override via file.
- Backend abstraction kept minimal (CLI backend plus mock for tests); optional metrics hook stub.
- Suggested implementation order: config/state/TODO parser → IO helpers → Codex backend → engine prompt/run loop → CLI commands → locking/error handling → web server API + RunnerManager → UI.
