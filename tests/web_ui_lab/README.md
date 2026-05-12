# Web UI Lab

`tests.web_ui_lab` is the durable contract layer for Web Hub regression
scenarios. It defines screen-shaped scenarios that agents can inspect and
validate without launching a browser, then later feed into browser evidence and
journey runners.

## Test Pyramid

1. Fast scenario/contract checks validate route IDs, seed fixture kinds,
   read-model/API routes, viewport coverage, critical tags, artifact names, and
   fixture-backed normalized screen invariants.
2. Browser evidence packs should consume the same corpus and write screenshots,
   accessibility snapshots, DOM summaries, console logs, network failures, and
   layout diagnostics for selected routes and viewports.
3. Playwright journeys should stay small and cover only flows where real browser
   interaction matters.

## Scenario Shape

Scenarios live in `corpus.py` and use typed dataclasses from
`scenario_models.py`. Each scenario records:

- stable `scenario_id` and human-readable title
- route name/path from `scripts/web_ui_screens.py`
- desktop and mobile viewport coverage from that screenshot script
- seed fixture kind such as `empty_hub`, `seeded_repo_worktree_ticket`,
  `chat_list_detail`, `large_list_windowing`, `pma_pending`, `pma_running`, or
  `pma_final`
- expected visible landmarks and primary loading-marker behavior
- existing read-model/API routes and frontend mapper modules
- stable evidence artifact names
- selection tags: `fast`, `browser`, `critical`, and `large-state`

## Agent Usage

Run the fast agent gate before closing ordinary Web UI tickets:

```bash
make web-ui-fast
```

Each executed scenario writes:

- `fixture_payload.json`
- `report.json`

The default diagnostics root is `.codex-autorunner/diagnostics/web_ui_lab/`.
`latest.json` is the stable pointer for agents. If the command fails, inspect
`failures[0].inspect_first` first; it names the scenario report with the failed
invariant, route, fixture kind, and normalized screen model. To reprint the last
summary without rerunning scenarios:

```bash
.venv/bin/python scripts/web_ui_lab_check.py --summary
```

The direct corpus test remains useful while editing this package:

```bash
.venv/bin/python -m pytest tests/web_ui_lab/test_scenario_corpus.py -q
```

Tests pass a temporary diagnostics directory so the committed tree stays clean.

If a route is added to `scripts/web_ui_screens.py`, add a matching corpus entry.
If a screen stops depending on a listed endpoint or mapper, update the scenario
instead of creating a parallel UI fixture vocabulary.
