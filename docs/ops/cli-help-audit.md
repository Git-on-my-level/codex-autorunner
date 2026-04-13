# CLI Help Quality Audit (Issue #849)

Date: 2026-03-03  
Scope: `car` CLI command tree (Typer), including aliases.  
Reviewed help pages: 131 command/group pages.

## Audit Criteria

- Purpose/intent is present for every command and command group.
- Option flags include help text.
- Destructive commands include explicit safety notes.
- Key workflow commands include concrete examples.
- Canonical aliases point users to canonical command paths.

## Command Group Matrix

| Group | Pages Reviewed | Status | Notes |
| --- | ---: | --- | --- |
| `car` | 12 | Complete | Root help text added; includes top-level lifecycle commands. |
| `car hub` | 27 | Complete | Group and subcommand summaries added across create/clone/serve/scan/snapshot/destination/dispatch/inbox/runs/worktree/tickets. |
| `car telegram` | 4 | Complete | Group help + command docstrings added. |
| `car discord` | 4 | Complete | Group help + command docstrings added. |
| `car templates` | 12 | Complete | Group help + examples on fetch/apply; repos subcommands documented. |
| `car template` (alias) | 12 | Complete | Alias help points to canonical `car templates ...`. |
| `car cleanup` | 3 | Complete | Group help present; command summaries already present. |
| `car chat` | 3 | Complete | Group help + `channels` subgroup help present. |
| `car doctor` | 3 | Complete | Group help + `versions` command summary added. |
| `car protocol` | 2 | Complete | Group help present; refresh command documented. |
| `car flow` | 9 | Complete | Group help present; telemetry/housekeep live under `flow` (ticket_flow commands are top-level `car ticket-flow`). |
| `car ticket-flow` | 7 | Complete | Canonical ticket_flow group help present; status/start/bootstrap breadcrumbs retained. |
| `car pma` | 33 | Complete | Group/subgroup help added (`docs`, `context`, `thread`). |

## Canonical Discoverability Changes

- `car template --help` now states canonical form: `car templates ...`.
- `car ticket-flow --help` now states canonical form: `car ticket-flow ...`.
- `car hub worktree list/scan` now emit canonical lifecycle hints:
  - Human output: `recommended: car hub worktree archive <repo_id>`
  - JSON payload: `recommended_command` + `recommended_actions`
- `car status` now includes `recommended_actions` in both human and JSON output.

## Docs Updates (`--help`-first behavior)

- `docs/car_constitution/61_AGENT_CHEATSHEET.md`
- `src/codex_autorunner/surfaces/cli/README.md`
- `src/codex_autorunner/bootstrap.py` (`pma_agents_content` defaults)

These now explicitly instruct operators/agents to run `car <group> --help` before acting in unfamiliar command areas.

## Regression Guard (CI/Lint-Style)

- New test suite: `tests/test_cli_help_quality.py`

Checks:
- No blank help summaries for any command/group in the full CLI tree.
- No missing option help text (excluding hidden/help options).
- Safety notes required for destructive commands.
- Examples required on selected key commands.
- Alias help must point to canonical command paths.

Run locally:

```bash
.venv/bin/pytest tests/test_cli_help_quality.py
```

