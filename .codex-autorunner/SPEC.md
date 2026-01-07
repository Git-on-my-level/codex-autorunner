# Static Tightening Plan (Opinionated)

This document defines the target lint/type-check baselines and the incremental path to reach them while keeping CI green. It is written to drive a sequence of agent runs that tighten quality gates without breaking existing workflows.

## Goals (Stopping Point)

- Ruff: full baseline for `pyproject.toml` with sane defaults for production Python (UP deferred unless a broad typing syntax pass is approved).
- Mypy: enabled for all Python source under `src/`, with targeted module‑local ignores only when justified and documented.
- ESLint: keep `eslint:recommended` clean; no extra JS rules unless clear value.
- Deadcode: maintain baseline hygiene; only update `.deadcode-baseline.json` if verified true dead code or agreed refactor.
- CI: `./scripts/check.sh` passes on each stage.

The “done” state is when the repo runs `./scripts/check.sh` cleanly with:
- ruff select includes E/F/W/I/B (UP deferred unless a broad typing syntax pass is approved),
- mypy has no global `ignore_errors` and no per‑module overrides (or only a very small, documented list),
- eslint clean, and
- deadcode baseline unchanged unless explicitly reviewed.

## Current Baseline (as of this plan)

- Ruff: `E/F/W/I/B` enabled; `E501` ignored; per‑file ignores for legacy `import *` shims and `B008` in Typer/FastAPI defaults; UP deferred (would require widespread typing syntax updates).
- Mypy: global `ignore_errors` removed; `warn_unused_ignores` enabled; explicit per-module ignores remain for bootstrap/cli/routes/web/voice/github/telegram/manifest while core and app_server are enforced.
- ESLint: `eslint:recommended` over static JS, vendor excluded.
- Deadcode: baseline file exists; scanner counts f-string references; no new findings.

## Strategy Overview

1) **Iterate in small steps**: tighten one component at a time, run `./scripts/check.sh`, and fix the new failures immediately.
2) **Prefer local type narrowing over global ignores**: use explicit guards, casts, and minimal refactors to satisfy type checkers.
3) **Keep doc‑only modules clean**: doc tooling should be repo‑only; add guards where needed.
4) **Avoid scope creep**: don’t change runtime logic unless it fixes a real safety issue; focus on typing correctness and lint hygiene.

## Stage Plan

### Stage 1: Solidify ruff hygiene (done)
- Enable W warnings; remove global ignores for E402/F403.
- Use per‑file ignores for intentional `import *` shims.

### Stage 2: Mypy core enforcement (done)
- Remove per‑module ignores for core; add explicit repo‑mode guards (RepoConfig) to modules that only work in repo mode.
- Add small casts after JSON/YAML loads.
- Add stubs (types‑PyYAML) for PyYAML.

### Stage 3: Mypy expand to integrations/app_server (done)
- Remove any remaining ignores for `src/codex_autorunner/integrations/app_server`.
- Fix typing locally in app_server submodules (prefer data classes + explicit types).
- If third‑party stubs are needed, add them to dev dependencies.

### Stage 4: Ruff rule expansion (done)
- Added ruff `I` (import sorting) and `B` (bugbear), applied fixes, and documented per‑file `B008` ignores for Typer/FastAPI defaults.
- Optionally add ruff `UP` (pyupgrade) and apply safe upgrades if it doesn’t change runtime semantics; currently deferred due to the large number of typing syntax changes required.
- If `ANN` (type annotation enforcement) is desired, scope it only to new code or specific folders; do not apply repo‑wide unless approved.

### Stage 5: Mypy strictness (done)
- Dropped global `ignore_errors = true` and enabled `warn_unused_ignores = true`.
- Core and app_server enforced; remaining ignores are limited to bootstrap/cli/routes/web/voice/github/telegram/manifest.
- Next focus: remove those ignores module-by-module, in small steps, keeping `./scripts/check.sh` green.

### Stage 6: Deadcode baseline hygiene (ongoing)
- Only update `.deadcode-baseline.json` if:
  - code is intentionally deprecated and will remain unused, or
  - a refactor removed usage and the change is verified.
- Add a short reason in PROGRESS if the baseline is updated.

## Known Best Practices (from current work)

- **Repo‑only guard**: If a module is repo‑mode only, add a small `RepoConfig` guard to avoid HubConfig unions.
- **Cast JSON/YAML outputs**: After `json.loads`/`yaml.safe_load`, cast to `Dict[str, Any]` once and work with it.
- **Avoid repeated `.get()` on unknowns**: store values in locals to let mypy narrow types.
- **Contextmanager annotations**: generator context managers should return `Iterator[None]` to satisfy mypy.
- **Sorting values**: sort by explicit numeric fields to avoid union ordering errors.

## Acceptance Criteria

A stage is accepted only if:
- `./scripts/check.sh` passes,
- no new global ignores are added (prefer local casts/guards),
- no behavior changes beyond safety guards or trivial typing fixes,
- CI remains green.

## Agent Workflow

1) Make one tightening change (e.g., enable a new ruff family or remove a mypy ignore).
2) Run `./scripts/check.sh`.
3) Fix all failures in small, local changes.
4) Summarize what was changed and any learnings.
5) Move to the next tightening step.
