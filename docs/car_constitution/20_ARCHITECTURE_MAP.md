# Architecture Map

Goal: allow a new agent to locate the correct seam for a change without relying on fragile file-level details.

## Mental model
```
[ Engine ]  →  [ Control Plane ]  →  [ Adapters ]  →  [ Surfaces ]
```
Left is most stable; right is most volatile.

## Engine (protocol-agnostic)
Responsibilities:
- run lifecycle + state transitions
- scheduling/locks/queues
- deterministic semantics

Non-responsibilities:
- no UI concepts
- no transport/protocol coupling
- no vendor SDK assumptions

## Control plane (filesystem-backed intent)
Responsibilities:
- canonical state + artifacts under `.codex-autorunner/`
- plans/snapshots/outputs/run metadata
- a durable bridge between humans, agents, and the engine

## Adapters (protocol translation)
Responsibilities:
- translate external events/requests into engine commands
- normalize streaming/logging into canonical run artifacts
- tolerate retries, restarts, partial failures

Non-responsibilities:
- avoid owning business logic; keep logic in engine/control plane

## Surfaces (UX)
Responsibilities:
- render state; collect inputs; support reconnects
- provide ergonomics (logs, terminal, dashboards)

Non-responsibilities:
- do not become state owners; never be the only place truth lives

## Cross-cutting constraints
- **One-way dependencies**: Surfaces → Adapters → Control Plane → Engine (never reverse).
- **Isolation is structural**: containment via workspaces/credentials, not interactive prompts.
- **Replaceability**: any adapter/surface can be rewritten; engine/control plane must remain stable.

## Component Implementation
Mapping the conceptual layers to the codebase:

- **Engine**: `src/codex_autorunner/core/`. Handles the core loop, state, and locking.
- **Control Plane**: `.codex-autorunner/` (files), `tickets/` (python).
- **Adapters**: `src/codex_autorunner/integrations/` (GitHub, Telegram, App Server).
- **Surfaces**:
  - **CLI**: `src/codex_autorunner/cli.py` (Typer wrapper).
  - **Server/UI**: `src/codex_autorunner/server.py` (FastAPI), `static/`.
  - **Hub**: Supervises multiple repos/worktrees.

## Data Layout & Config
- **Repo Root**:
  - `codex-autorunner.yml`: Defaults (committed).
  - `codex-autorunner.override.yml`: Local overrides (gitignored).
  - `.codex-autorunner/`: Canonical runtime state.
    - `tickets/`: Required (`TICKET-###.md`).
    - `workspace/`: Optional context (`active_context.md`, `decisions.md`, `spec.md`).
    - `config.yml`: Generated config.
    - `state.sqlite3`, logs, lock.
- **Global Root** (cross-repo only):
  - `~/.codex-autorunner/`: update cache, update status/lock, shared app-server workspaces.
- **Config Precedence**: Built-ins < `codex-autorunner.yml` < override < `.codex-autorunner/config.yml` < env.

## Execution Loop
1. **Select Ticket**: Active ticket target under `.codex-autorunner/tickets/`.
2. **Build Prompt**: From ticket content, workspace docs, and bounded prior run output.
3. **Run**: Execute Codex app-server with streaming logs via OpenCode runtime.
4. **Update State**: Handle stop rules (exit code, stop_after_runs, limits).

## Dispatch Model (Agent-Human Communication)
- **Dispatch**: Agent → Human (`tickets/models.py`).
  - `mode: "notify"`: Informational, agent continues.
  - `mode: "pause"`: Handoff, agent waits for Reply.
- **Reply**: Human → Agent response.
- **Storage**: `runs/<run_id>/dispatch/` (staging), `runs/<run_id>/dispatch_history/` (archive).

## API Surface
- **Workspace**: `/api/workspace/*`
- **File Chat**: `/api/file-chat/*`
- **Runner/Logs**: `/api/run/*`, `/api/logs/*`
- **Terminal**: `/api/terminal` (websocket), `/api/sessions`
- **Hub**: `/hub/*` (repos, worktrees, usage)
