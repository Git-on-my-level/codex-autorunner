# AutoOptimize

AutoOptimize is a CAR app for metric-driven iterative improvement work. It
keeps all campaign state inside the app runtime, gives agents repeatable tools
for recording results, and renders deterministic summary artifacts for wrap-up.

## When to use it

Use AutoOptimize when you want a bounded optimization loop with:

- a clear primary metric
- one hypothesis per ticket
- explicit keep/discard/pivot/block decisions
- app-owned state instead of CAR core schema
- final summary artifacts for chat wrap-up

## Install and apply

```bash
car apps install blessed:apps/autooptimize
car apps apply blessed.autooptimize --set goal="Reduce p95 latency"
```

Named templates are supported for follow-on tickets:

```bash
car apps apply blessed.autooptimize --template baseline --set goal="Reduce p95 latency"
car apps apply blessed.autooptimize --template iteration --set goal="Reduce p95 latency"
car apps apply blessed.autooptimize --template closeout --set goal="Reduce p95 latency"
```

## Tool commands

Initialize the run:

```bash
car apps run blessed.autooptimize init-run -- \
  --goal "Reduce p95 latency" \
  --metric "p95 latency" \
  --direction lower \
  --unit ms \
  --guard-command "python3 -m pytest tests/core/apps" \
  --max-iterations 5
```

Record the baseline:

```bash
car apps run blessed.autooptimize record-baseline -- \
  --value 182.4 \
  --unit ms \
  --summary "Current main branch baseline"
```

Record one iteration:

```bash
car apps run blessed.autooptimize record-iteration -- \
  --iteration 1 \
  --ticket TICKET-301-autooptimize-iteration.md \
  --hypothesis "Cache repo config lookups" \
  --value 160.1 \
  --unit ms \
  --decision keep \
  --guard-status pass \
  --summary "Lowered p95 without regressions"
```

Check status and render artifacts:

```bash
car apps run blessed.autooptimize status -- --json
car apps run blessed.autooptimize plan-next-ticket -- --json
car apps run blessed.autooptimize validate-state
car apps run blessed.autooptimize render-summary-card
```

## State and artifacts

AutoOptimize writes only to app-owned runtime paths:

```text
$CAR_APP_STATE_DIR/run.json
$CAR_APP_STATE_DIR/iterations.jsonl
$CAR_APP_ARTIFACT_DIR/summary.md
$CAR_APP_ARTIFACT_DIR/summary.svg
$CAR_APP_ARTIFACT_DIR/summary.png
```

`summary.png` is optional. The renderer always emits `summary.md` and
`summary.svg`. PNG conversion is attempted only when a compatible local SVG
conversion dependency is available.

## Recommended workflow

1. Apply the bootstrap template and define the campaign goal.
2. Run `init-run` once the metric contract is clear.
3. Run `plan-next-ticket` and apply the recommended baseline template.
4. After every baseline or iteration ticket, run `plan-next-ticket` and apply
   its recommended template.
5. Use `status` frequently to review progress and stop-condition hints.
6. Finish with the closeout template, `validate-state`, and
   `render-summary-card`.

The loop is ticket-driven on purpose. Hooks render or attach artifacts at
lifecycle boundaries, while tickets remain the durable units that agents can
resume, review, and audit.

## Limitations

- AutoOptimize does not run hidden background loops. It recommends the next
  explicit ticket through `plan-next-ticket`.
- It does not add CAR-core schemas, background loops, or web UI.
- Guard commands are recorded in run state but must still be run explicitly by
  the agent.
- `before_chat_wrapup` currently attaches generated artifacts; rendering is
  performed by the explicit closeout step and the `after_flow_terminal` hook.
