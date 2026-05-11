# Web Read Models Operations

Use this runbook for the Web Hub read-model architecture documented in
`docs/architecture/web-ui-read-model-contracts.md`.

## What To Inspect

- Contracts: `src/codex_autorunner/surfaces/web/read_model_contracts.py` and
  `src/codex_autorunner/web_frontend/src/lib/api/readModelContracts.ts`.
- Shared projection storage/service:
  `src/codex_autorunner/core/hub_projection_store.py` and
  `src/codex_autorunner/core/hub_projection_service.py`.
- Chat read model: `src/codex_autorunner/core/orchestration/chat_surface_read_model.py`.
- Repo/worktree/ticket read-model routes:
  `src/codex_autorunner/surfaces/web/routes/hub_repo_routes/read_models.py`.
- Frontend store and streams:
  `src/codex_autorunner/web_frontend/src/lib/data/`.
- Budget smoke: `scripts/web_responsiveness_budgets.py` and
  `tests/chat_surface_lab/web_responsiveness_budgets.py`.

## Rebuild Projections

Shared hub UI projections validate on startup. To force the shared durable
projection rebuild for a hub root:

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
from codex_autorunner.core.hub_projection_service import HubProjectionService

hub_root = Path(".").resolve()
service = HubProjectionService(hub_root)
service.validate_or_rebuild_stale_state()
print(service.rebuild_all_projections())
PY
```

Use the actual hub root for `hub_root` when running outside that directory.
Projection rebuilds must be deterministic from canonical files, orchestration
SQLite tables, runtime journals, and durable event journals. Do not repair a bad
projection by editing projection rows by hand; rebuild from canonical state.

## Snapshot And Stream Checks

Start or use an existing hub, then inspect scoped snapshots:

```bash
BASE="$(car hub endpoint)"
curl -s "$BASE/hub/read-models/repo-worktree/topology?kind=all&limit=50" | jq .
curl -s "$BASE/hub/read-models/repo-worktree/runtime?kind=all&limit=50" | jq .
curl -s "$BASE/hub/read-models/tickets/<ticket_id>?owner_kind=repo&owner_id=<repo_id>" | jq .
curl -s "$BASE/hub/chat/index?view=all&limit=50" | jq .
curl -s "$BASE/hub/chat/threads/<thread_id>/detail?timeline_limit=50" | jq .
```

For streams, verify the response is `text/event-stream`, cursors increase
within one source, and reconnect with the last cursor replays no duplicate
visible state:

```bash
curl -N "$BASE/hub/chat/patches?cursor=<last_cursor>&limit=100"
```

When a stream reports `projection.cursor_gap`, request the snapshot route from
the event `repair.snapshotRoute` with the last applied cursor. The client should
replace only the affected window or entity family.

## Responsiveness Budget Smoke

Run the deterministic large-hub smoke without touching live hub state:

```bash
.venv/bin/python scripts/web_responsiveness_budgets.py
```

The default writes diagnostics under
`.codex-autorunner/diagnostics/web-responsiveness-budgets/`. Use smaller counts
for a quick local check:

```bash
.venv/bin/python scripts/web_responsiveness_budgets.py \
  --chat-count 120 \
  --repo-count 20 \
  --worktree-count 40 \
  --ticket-run-group-count 20 \
  --timeline-event-count 10 \
  --journal-event-count 30
```

Budget failures are grouped by projection lag, backend snapshot latency, stream
latency, frontend render work, or payload/window size. Fix the named family
instead of raising a global timeout.

## Migration Guardrails

These patterns are forbidden for migrated screens:

- Page-local broad fetch choreography as a primary data source.
- Normal polling loops for screen updates.
- Unbounded DOM lists for high-cardinality data.
- Frontend transcript composition from runtime fragments.
- Projection invalidation without a documented repair behavior.

Remaining legacy broad clients are diagnostics/tests-only. If a UI route needs a
new data shape, add a screen read model, a typed event, a selector, and a scale
test instead of reviving broad list loads.
