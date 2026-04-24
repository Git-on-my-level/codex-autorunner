# Static TypeScript - Agent Guide

This is the source of truth for web UI JavaScript. Edit files here, not in `../static/generated/`.

## Route Here When

- The task changes UI behavior, browser events, client-side state, or fetch/SSE handling.
- The generated JS changed and you need the real source file.

## Keep Straight

- Source: `src/codex_autorunner/static_src/*.ts`
- Hub **screenshot / QA mock data**: add scenarios in `uiMockScenarios.ts`, then load the hub with `?uiMock=<id>` (see `uiMock.ts`). `?uiMockStrip=1` removes the param from the URL after init for cleaner captures. **`make ui-qa-screens`** runs Playwright over **every** `uiMock` scenario (see `scripts/ui_qa/generate_manifest.py`); set `UI_QA_UI_MOCKS=0` to capture only a single unmocked hub page.
- **Onboarding / first run**: `walkthrough.ts` — `?carOnboarding=1` clears the walkthrough-dismissed key, **PMA `localStorage` chat** (`car.pma.pma`), the pending-prompt key, and reopens the top strip. Call **`consumeOnboardingUrlReset()`** at the start of `initHubShell` so this runs *before* PMA loads (otherwise an old local chat reappears). **`make serve-onboarding`** creates a clean temp hub and prints URLs; use with `&carOnboarding=1` for a true empty slate in the same browser.
- Generated output: `src/codex_autorunner/static/generated/*.js`
- HTML/CSS shell: `../static/index.html`, `../static/styles.css`
- Web backend/static serving: `../surfaces/web/AGENTS.md`

## Validation

- Run `pnpm run build` after TS edits.
- Run `make frontend-check` if markup or DOM assumptions changed.
- Run `pnpm test:markdown` for browserless frontend tests in `tests/js/`.
- If a TS change affects route payloads or static asset serving, also check `tests/AGENTS.md`.
