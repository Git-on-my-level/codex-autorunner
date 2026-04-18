# Codex Autorunner - Agent Guide

This repo dogfoods codex-autorunner. Start here, then prefer the nearest nested `AGENTS.md` over repeating repo-wide rules.

## Start Here

- Read `docs/car_constitution/61_AGENT_CHEATSHEET.md` first.
- If the task mentions tickets, contextspace, dispatch, or `.codex-autorunner/`, also read `.codex-autorunner/ABOUT_CAR.md`.
- If you need runtime setup details, read `.codex-autorunner/DESTINATION_QUICKSTART.md`.

## Hard Invariants

- Filesystem state is the source of truth.
- Durable runtime state lives under `.codex-autorunner/`.
- Keep the layered split intact: protocol-agnostic engine, adapters, surfaces.
- Prefer deterministic behavior, explicit config, small diffs, and observable failures.
- Agents can propose and execute, but files decide.

## Fast Routing

- Core engine and config: `src/codex_autorunner/core/`, `docs/car_constitution/10_CODEBASE_CONSTITUTION.md`, `20_ARCHITECTURE_MAP.md`, `30_ENGINEERING_STANDARDS.md`.
- Web UI frontend: `src/codex_autorunner/static_src/AGENTS.md`, then `src/codex_autorunner/static/AGENTS.md`.
- Web backend and static asset serving: `src/codex_autorunner/surfaces/web/AGENTS.md`.
- Frontend and web tests: `tests/AGENTS.md`.
- Agent protocol work: `src/codex_autorunner/agents/acp/AGENTS.md`.
- Debugging and ops: `docs/car_constitution/50_OBSERVABILITY_OPERATIONS.md`, `docs/ops/`.

## CAR Files

- Update tickets only under `.codex-autorunner/tickets/`.
- Update context docs only under `.codex-autorunner/contextspace/`.
- Do not create duplicate ticket or context files elsewhere in the repo.
- Treat `.codex-autorunner/` as intentional structure even when generated or gitignored.

## Repo Defaults

- Python: use the project venv, usually via `.venv/bin/python`; run `make setup` if needed.
- Frontend JS: edit `src/codex_autorunner/static_src/*.ts`, not `src/codex_autorunner/static/generated/*.js`; run `pnpm run build` after TS changes.
- Prefer `make` targets for common checks.
- Use `gh` for GitHub operations when possible.
- Use a 300 second timeout for `git commit`; avoid `--no-verify` unless the user approves.
- Do not restart launchd services or run refresh scripts yourself; ask the user to do that. Preferred update path is `scripts/safe-refresh-local-mac-hub.sh`.

## Useful Pointers

- Release flow: `docs/ops/release.md`
- Telegram debugging: `docs/ops/telegram-debugging.md`
- VM/cloud caveats: `docs/ops/running-in-a-vm.md`
- Subagent model configuration: `docs/adding-an-agent.md`
