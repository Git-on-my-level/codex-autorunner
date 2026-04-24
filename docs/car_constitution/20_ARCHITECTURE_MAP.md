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
- PMA delivery intent selection and durable notification bookkeeping before any transport adapter writes outbox state

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
- **Control Plane**: `.codex-autorunner/` (files, including the ticket queue under `tickets/`). Ticket queue behavior is implemented under `src/codex_autorunner/tickets/`.
- **Adapters**: `src/codex_autorunner/integrations/` (GitHub, Telegram, App Server).
  - PMA chat delivery follows the same split: `core/pma_chat_delivery.py` emits transport-agnostic attempts, `pma_chat_delivery_runtime.py` dispatches them, and Discord/Telegram adapters own formatting, topic parsing, and outbox writes.
  - SCM automation follows the same split: `core/scm_automation_service.py` owns routing, journaling, and retry semantics; provider adapters inject publish executors when wiring the service instead of core importing provider modules directly.
  - **Discord interaction runtime**: ingress (`ingress.py`) -> command runner (`command_runner.py`) -> interaction dispatch (`interaction_dispatch.py`).  Two-phase lifecycle: ingress acknowledges on the gateway hot path, then the runner executes the handler in the background with timeout enforcement.
- **Surfaces**:
  - **Hub**: Supervises multiple repos/worktrees (primary interface for users).
  - **Web/UI**: `src/codex_autorunner/surfaces/web/` (FastAPI routes, web services), `src/codex_autorunner/static/`.
  - **CLI**: `src/codex_autorunner/surfaces/cli/` (Typer command surface).

## Data Layout & Config
- **Hub Root** (primary):
  - `codex-autorunner.yml`: Defaults (committed).
  - `codex-autorunner.override.yml`: Local overrides (gitignored).
  - `.codex-autorunner/`: Hub state.
    - `manifest.yml`: Lists managed repositories.
    - `hub_state.json`, `config.yml`, logs.
    - `orchestration.sqlite3`: Orchestration metadata, bindings, executions, transcript mirrors.
    - `templates/`: Hub-scoped templates.
  - `repos/`: Managed repositories (default location).
- **Per-Repo** (under hub, or standalone for CAR development):
  - `.codex-autorunner/`: Canonical runtime state.
    - `tickets/`: Required (`TICKET-###.md`).
    - `contextspace/`: Optional context (`active_context.md`, `decisions.md`, `spec.md`).
    - `config.yml`: Generated config.
    - `state.sqlite3`, logs, lock.
    - `flows.db`: Flow engine internals (source of truth).
    - `discord_state.sqlite3`, `telegram_state.sqlite3`: Transport delivery/outbox state.
- **Global Root** (cross-repo caches):
  - `~/.codex-autorunner/`: update cache, update status/lock, shared app-server workspaces.
- **Config Precedence**: Built-ins < `codex-autorunner.yml` < override < `.codex-autorunner/config.yml` < env.

## Execution Loop
1. **Select Ticket**: Active ticket target under `.codex-autorunner/tickets/`.
2. **Build Prompt**: From ticket content, contextspace docs, and bounded prior run output.
3. **Run**: Execute the configured backend (for example Codex app-server with OpenCode runtime, or Hermes and other ACP-backed runtimes; see `docs/ops/hermes-acp.md`).
4. **Update State**: Handle stop rules (exit code, stop_after_runs, limits).

### Ticket-flow runner seam structure
The ticket-flow orchestration hot path is split across focused submodules:
- **`runner.py`** (`TicketRunner.step`): Step controller that sequences pre-turn planning, execution, and post-turn processing without reimplementing helper contracts inline.
- **`runner_selection.py`**: Ticket selection (`select_ticket`), validation (`validate_ticket_for_execution`), pre-turn planning (`plan_pre_turn`), reply-context loading (`build_reply_context`), and requested-context resolution (`load_ticket_context_block`).
- **`runner_prompt.py`**: Prompt assembly (`build_prompt`), budgeting (`_shrink_prompt`), and ticket-frontmatter preservation (`_preserve_ticket_structure`).
- **`runner_execution.py`**: Agent turn execution (`execute_turn`), git-state capture, loop-guard computation, and network-error classification.
- **`runner_post_turn.py`**: Dispatch archival (`archive_dispatch_and_create_summary`), turn-summary creation, frontmatter recheck, checkpoint/commit gating, pause-result construction, and completion-state cleanup.
- **`runner_commit.py`**: Commit-gating logic for done-but-dirty working trees.

Projections and summaries (read-only, consumed by hub/PMA/CLI surfaces):
- **`core/ticket_flow_projection.py`**: Canonical state projection merging ticket frontmatter, run-state, flow-store, freshness, and contradiction detection.
- **`core/ticket_flow_summary.py`**: Display-oriented summary (status icons, labels, PR URL) built from projection facts.

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
