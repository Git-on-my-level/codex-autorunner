# PMA UI QA

Use these routes for manual screenshot gates after PMA frontend changes that affect layout, routing, sparse states, or workspace ownership labels:

- `/pma`
- `/dashboard`
- `/repos`
- `/repos/example`
- `/worktrees`
- `/worktrees/example`
- `/tickets`
- `/tickets/TICKET-100`
- `/contextspace/local`
- `/contextspace/example`
- `/settings`

The Python hub serves dynamic PMA routes through the SvelteKit SPA fallback. Keep route smoke coverage aligned with this list in `tests/surfaces/web/test_pma_static_routes.py`.
