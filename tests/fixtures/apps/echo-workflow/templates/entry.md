---
agent: codex
done: false
title: "Echo Workflow entry"
goal: "Run the echo-workflow fixture app tools and verify state and artifacts."
---

# Echo Workflow

This ticket was created by the `fixture.echo-workflow` app entrypoint.

## Tasks

- Run `car apps run fixture.echo-workflow record-state -- "<message>"`
- Run `car apps run fixture.echo-workflow render-summary`
- Inspect state under `.codex-autorunner/apps/fixture.echo-workflow/state/`
- Inspect artifacts under `.codex-autorunner/apps/fixture.echo-workflow/artifacts/`
