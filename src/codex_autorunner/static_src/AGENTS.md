# Static TypeScript - Agent Guide

This is the source of truth for web UI JavaScript. Edit files here, not in `../static/generated/`.

## Route Here When

- The task changes UI behavior, browser events, client-side state, or fetch/SSE handling.
- The generated JS changed and you need the real source file.

## Keep Straight

- Source: `src/codex_autorunner/static_src/*.ts`
- Hub **screenshot / QA mock data**: add scenarios in `uiMockScenarios.ts`, then load the hub with `?uiMock=<id>` (see `uiMock.ts`). `?uiMockStrip=1` removes the param from the URL after init for cleaner captures.
- Generated output: `src/codex_autorunner/static/generated/*.js`
- HTML/CSS shell: `../static/index.html`, `../static/styles.css`
- Web backend/static serving: `../surfaces/web/AGENTS.md`

## Validation

- Run `pnpm run build` after TS edits.
- Run `make frontend-check` if markup or DOM assumptions changed.
- Run `pnpm test:markdown` for browserless frontend tests in `tests/js/`.
- If a TS change affects route payloads or static asset serving, also check `tests/AGENTS.md`.
