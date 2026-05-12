# Web UI QA

Use `make web-ui-fast` before closing ordinary Web UI tickets. It runs the
fixture-backed Web UI lab scenarios without launching a browser and writes
`.codex-autorunner/diagnostics/web_ui_lab/latest.json`. On failure, start with
`failures[0].inspect_first`; it points to the scenario `report.json` with the
failed invariant, route, fixture, and normalized screen model. To print the last
summary without rerunning the scenarios, use:

```bash
.venv/bin/python scripts/web_ui_lab_check.py --summary
```

Use `make web-ui-screens` for explicit visual/layout evidence after Web frontend
changes that affect layout, routing, sparse states, or workspace ownership
labels. The default screenshot pack seeds a disposable fixture hub and captures
these routes:

- `/chats`
- `/hub`
- `/repos`
- `/repos/smoke-repo`
- `/repos/smoke-repo/tickets`
- `/repos/smoke-repo/tickets/TICKET-350-smoke-fixture`
- `/repos/smoke-repo/worktrees/smoke-repo--review`
- `/repos/smoke-repo/worktrees/smoke-repo--review/contextspace`
- `/repos/smoke-repo/worktrees/smoke-repo--review/tickets`
- `/tickets`
- `/tickets/TICKET-350-smoke-fixture`
- `/worktrees`
- `/contextspace/local`
- `/settings`

The Python hub serves dynamic Web routes through the SvelteKit SPA fallback, with removed top-level worktree/contextspace URLs redirecting into canonical Web repo-scoped URLs. Keep this route pack aligned with `scripts/web_ui_screens.py`, `tests/surfaces/web/test_web_ui_screens.py`, and shell coverage in `tests/surfaces/web/test_web_static_routes.py`.

Before opening screenshots, inspect `.codex-autorunner/render/web_ui_samples/latest/latest.json`. Each route/viewport record names the final URL, capture duration, loading marker status, console warnings/errors, failed requests, layout overflow or clipping diagnostics, and artifact paths for the screenshot, accessibility snapshot, DOM summary, and layout diagnostics. Start with records whose `failure_subsystems` is non-empty; the value identifies whether the failure came from navigation, console, network, loading, layout, screenshot, or accessibility capture.

Use `make web-ui-smoke` for the tiny Playwright journey layer. It rebuilds the Web static bundle, starts the same seeded disposable hub, navigates the primary routes, opens seeded PMA chat states, checks queued/running/final turn affordances, and writes `.codex-autorunner/render/web_ui_smoke/latest/manifest.json`. Trace, video, and failure screenshots are retained only when the smoke run fails; the manifest points at those artifacts.

## When To Run What

- `make web-ui-fast`: default agent gate for route contracts, fixture-backed
  read-model shape, mapper vocabulary, sparse states, and large-list scenarios.
- `make web-ui-screens`: opt-in browser evidence pack for visual, responsive,
  layout, loading-marker, console, or network-risk changes.
- `make web-ui-smoke`: opt-in Playwright journey check for critical browser
  interactions, especially PMA chat queued/running/final affordances.
