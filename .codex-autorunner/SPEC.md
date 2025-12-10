# Codex Autorunner – Hub/Supervisor Mode SPEC

## Overview
- Add a supervisor (“hub”) mode that runs from a parent directory and coordinates multiple child git repos. The home page lists repos and their statuses; selecting a repo drops into the existing per-repo UX.
- Keep the hub thin: orchestrate and present state; source of truth remains each repo’s `.codex-autorunner/`.
- Discovery is shallow (one level under the hub root) with a manifest to persist what we know; newly discovered repos are auto-initialized.

## Goals
- Operate across many repos from a single hub process (CLI + server/UI).
- Auto-discover child git repos (one directory level) and initialize missing `.codex-autorunner/*`, including a `.gitignore` that ignores everything by default.
- Allow repos to run independently and in parallel; hub can start/stop/resume per repo.
- Provide a hub home page (and API) summarizing repo status and linking to the existing per-repo UI.
- Add basic log rotation (~10 MB) for hub and per-repo logs.

## Non-goals
- Deep recursive discovery, exclude lists, or complex selection rules (future work).
- Auth/secrets management or sandbox guardrails; hub is allowed to traverse/run anywhere the user points it.
- Backward-compat shims for older config; we can refactor cleanly.

## Terminology
- Hub root: Directory where the supervisor runs; contains multiple child repos.
- Manifest: Hub-level file declaring known repos and metadata; lives under hub `.codex-autorunner/`.
- Repo entry: One child git repo; has its own `.codex-autorunner/` with config/state/logs/docs.
- Repo runner: The existing single-repo engine + loop, run per repo.

## Layout
```
<hub-root>/
  <repo-a>/.git/
  <repo-b>/.git/
  .codex-autorunner/
    config.yml            # hub config (mode: hub)
    manifest.yml          # list of repos + metadata
    hub_state.json        # minimal hub-only state (last scan, cached status snapshot)
    codex-autorunner-hub.log
    lock                  # hub-level lock (optional; single supervisor instance)
```
- Each repo continues to own its `.codex-autorunner/` (config/state/log/log rotation, docs, lock, prompt template). When auto-generated, drop `.codex-autorunner/.gitignore` with `*` (and `!/.gitignore`) to avoid accidental commits.

## Config and Manifest
- Single schema version (v2) covering both hub and repo configs; hub sets `mode: hub`, repos set `mode: repo`. Implement explicit discriminated types:
  - `HubConfig` (hub + server + logging).
  - `RepoConfig` (docs, codex, runner, git, server, logging).
- Config resolution rule: pick the nearest `.codex-autorunner/config.yml` walking upward from CWD. This prevents accidentally using hub config while inside a repo (and vice versa). Document this behavior.
- Hub config (`.codex-autorunner/config.yml`):
  ```
  version: 2
  mode: hub
  hub:
    repos_root: .                    # where to scan; default CWD
    manifest: .codex-autorunner/manifest.yml
    discover_depth: 1                # fixed to 1 for now
    auto_init_missing: true          # initialize when found
    log:
      path: .codex-autorunner/codex-autorunner-hub.log
      max_bytes: 10_000_000
      backup_count: 3
  server:
    host: 127.0.0.1
    port: 4173
  ```
- Manifest (`.codex-autorunner/manifest.yml`):
  ```
  version: 1
  repos:
    - id: repo-a            # slug (default: directory name)
      path: repo-a          # relative to hub root
      enabled: true         # include in scan/run
      auto_run: false       # whether hub tries to run/start it (default conservative)
    - id: repo-b
      path: repo-b
      enabled: true
      auto_run: false
  ```
  - Manifest is append-only (no excludes); directory name is the display name. Entries not found on disk surface as “missing”.
  - Keep manifest handling centralized (e.g., `manifest.py`) with load/save/update helpers; normalize paths relative to hub root; treat `id` as stable once written.
- Repo config (`mode: repo`) keeps today’s fields (docs, codex, prompt, runner, git, server) but bumps `version: 2` and adds logging rotation defaults.

## Discovery and Initialization Flow
1) Load hub config + manifest (create manifest if missing).
2) Enumerate immediate children under `repos_root`; treat any directory containing `.git` (file or dir) as a repo candidate.
3) For each manifest entry: verify on disk; mark missing if absent; add to listing even if `.git` is gone.
4) For each discovered repo not already in manifest: add manifest entry (`id` from directory name, enabled=true, auto_run=false by default), then initialize if `auto_init_missing` is true.
5) Initialization of a repo:
   - Create `.codex-autorunner/` with `.gitignore` (`*\n!/.gitignore\n`).
   - Write repo `config.yml` (mode: repo, version: 2, standard defaults).
   - Seed docs (`TODO.md`, `PROGRESS.md`, `OPINIONS.md`, `SPEC.md`) under `.codex-autorunner/`.
   - Create `state.json`, log file, lock (empty/unset), prompt template if applicable.
6) Discovery runs at hub startup and can be re-triggered via API/CLI; it is idempotent.

## Orchestration Model
- The hub hosts multiple repo runners concurrently; each repo keeps its own lock/state/logs.
- Hub respects per-repo locks; it does not auto-clear or override. Resume/kill are forwarded to the per-repo engine.
- Run/stop/resume requests are per repo; no global throttling. Multiple repos may run in parallel.
- Hub state (`hub_state.json`): minimal snapshot of last scan (timestamp, list of repos with path/id/display name, and last-known status derived from per-repo `state.json`), used for fast UI loads. Source of truth remains per-repo files; hub also maintains in-memory status updated by each runner and periodically flushed to `hub_state.json`.
- If a repo is missing or init fails, surface that status in the hub list; do not block others.
- Abstractions:
  - `RepoRunner`: start_loop(once), request_stop, resume (clear stale lock), kill, status.
  - `HubSupervisor`: manages RepoRunners keyed by repo id, coordinates discovery/init/start/stop.
- Status modeling:
  - `RepoStatus` enum: UNINITIALIZED, INITIALIZING, IDLE, RUNNING, ERROR, LOCKED, MISSING, INIT_ERROR.
  - `LockStatus` enum: UNLOCKED, LOCKED_ALIVE, LOCKED_STALE.

## API Additions (namespace `/hub`)
- `GET /hub/repos` → list repos with id, path, display_name (dir name), enabled/auto_run, discovered/init status, last-known state (idle/running/error/locked/missing/uninitialized), last_run_id/time if available.
- `POST /hub/repos/scan` → rerun discovery/init; returns updated list.
- `POST /hub/repos/{id}/run` → start/continue loop for that repo (optionally `{once: bool}`); respects repo lock.
- `POST /hub/repos/{id}/stop` → request stop for that repo’s loop.
- `POST /hub/repos/{id}/resume` → forward resume (clear stale lock) for that repo.
- `POST /hub/repos/{id}/kill` → forward kill to that repo.
- `POST /hub/repos/{id}/init` → force (re)init if missing `.codex-autorunner/`.
- Existing per-repo routes remain unchanged; hub uses them to render the detail view.
- Reuse per-repo routes via a router factory; in hub mode mount under `/repos/{id}/...` so the UI can call per-repo APIs without duplication.

## UI Changes
- Home page lists repos with status badge (idle/running/error/locked/missing/uninitialized), last run summary, and quick actions (run/stop/resume/init). Selecting a row opens the existing repo dashboard (same UI, now parameterized by repo path/id).
- Empty states: no repos discovered, init failures, or missing repos surfaced inline with guidance.
- Keep UX lightweight; no additional multi-level nesting beyond the home page and existing per-repo view.

## Logging and Rotation
- Per-repo logs gain rotation defaults: max_bytes 10 MB, backup_count 3. Preserve run markers.
- Hub log at `.codex-autorunner/codex-autorunner-hub.log` uses the same rotation policy.
- Rotation happens when writing; prefer rotating between runs (or otherwise avoid splitting a single run’s markers across files).
- Centralize logging setup so each repo/hub logger has its own rotating file handler; no shared handlers across repos.

## CLI Surface
- `codex-autorunner hub serve` (or `serve` in hub mode) starts the supervisor server/UI from the hub root (detect `mode: hub` in config).
- `codex-autorunner hub scan` triggers discovery/init and prints repo table.
- Per-repo commands (`run`, `once`, `status`, etc.) continue to work when executed inside a repo directory (mode: repo). Commands run in the wrong mode should error clearly (e.g., running `run` from hub root).

## Failure Modes and Handling
- Missing repo on disk → mark as missing; do not auto-remove from manifest.
- Init failure → mark repo as `init_error` with message; allow retry via init/scan.
- Locked repo → surface as locked; hub does not clear unless user calls resume. Resume is allowed only when lock is stale (process not alive); otherwise start is blocked.
- Hub lock prevents concurrent hub instances; additional instances fail fast (no read-only secondaries).
- If manifest and discovery disagree (e.g., renamed dir), show both entries; user can fix manifest manually.

## Non-functional
- No sandbox or network restrictions enforced by hub.
- Prefer fast, in-process orchestration; avoid heavy dependencies.
- Tests to add: manifest load/save, discovery one-level, auto-init (including `.gitignore` contents), hub API endpoints, UI home list rendering, log rotation behavior, parallel run smoke tests.
