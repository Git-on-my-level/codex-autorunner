# PMA UI QA

Use `make pma-ui-screens` for manual screenshot gates after PMA frontend changes that affect layout, routing, sparse states, or workspace ownership labels. The default screenshot pack seeds a disposable fixture hub and captures these routes:

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

The Python hub serves dynamic PMA routes through the SvelteKit SPA fallback, with removed top-level worktree/contextspace URLs redirecting into canonical PMA repo-scoped URLs. Keep this route pack aligned with `scripts/pma_ui_screens.py`, `tests/surfaces/web/test_pma_ui_screens.py`, and shell coverage in `tests/surfaces/web/test_pma_static_routes.py`.
