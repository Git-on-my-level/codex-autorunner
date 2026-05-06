# Static TypeScript - Agent Guide

This is the source of truth for the legacy/debug CAR UI JavaScript. The default
hub UI is the Svelte PMA Hub under `../pma_frontend/`; use this tree only when
benchmarking or comparing against the old UI.

## Route Here When

- The task changes UI behavior, browser events, client-side state, or fetch/SSE handling.
- The generated JS changed and you need the real source file.

## Keep Straight

- Source: `src/codex_autorunner/static_src/*.ts`
- Default app source: `src/codex_autorunner/pma_frontend/`
- Runtime opt-in for the old UI: set `CAR_ENABLE_LEGACY_UI=1`, or use
  `make serve-legacy-ui`.
- Hub **screenshot / QA mock data**: add scenarios in `uiMockScenarios.ts`, then load the hub with `?uiMock=<id>` (see `uiMock.ts`). `?uiMockStrip=1` removes the param from the URL after init for cleaner captures. **`make ui-qa-screens`** runs Playwright over **every** `uiMock` scenario (see `scripts/ui_qa/generate_manifest.py`); set `UI_QA_UI_MOCKS=0` to capture only a single unmocked hub page.
- **Onboarding / first run**: `walkthrough.ts` — `?carOnboarding=1` clears the walkthrough-dismissed key, **PMA `localStorage` chat** (`car.pma.pma`), the pending-prompt key, and reopens the top strip. Call **`consumeOnboardingUrlReset()`** at the start of `initHubShell` so this runs *before* PMA loads (otherwise an old local chat reappears). **`make serve-onboarding`** creates a clean temp hub and prints URLs; use with `&carOnboarding=1` for a true empty slate in the same browser.
- Generated output: `src/codex_autorunner/static/generated/*.js`
- HTML/CSS shell: `../static/index.html`, `../static/styles.css`
- Web backend/static serving: `../surfaces/web/AGENTS.md`

## Validation

- Run `make legacy-ui-build` after TS edits.
- Run `make frontend-check` if legacy markup or DOM assumptions changed.
- Run `pnpm legacy:test` for browserless legacy frontend tests in `tests/js/`.
- If a TS change affects route payloads or static asset serving, also check `tests/AGENTS.md`.
