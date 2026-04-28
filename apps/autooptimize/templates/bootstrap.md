---
agent: codex
done: false
title: "AutoOptimize bootstrap"
goal: "Bootstrap an AutoOptimize campaign, define the metric contract, and queue the baseline plus the first bounded iteration tickets."
---

# AutoOptimize Bootstrap

This ticket was created by the `blessed.autooptimize` app.

## Tasks

1. Clarify or infer the primary metric contract:
   - metric name
   - direction (`higher` or `lower`)
   - unit
   - target if one exists
2. Run `car apps run blessed.autooptimize init-run -- ...` with the agreed run settings.
3. Create or ensure one baseline ticket:
   - `car apps apply blessed.autooptimize --template baseline --set goal="<goal>"`
4. Create the first one or two iteration tickets:
   - `car apps apply blessed.autooptimize --template iteration --set goal="<goal>"`
5. Keep the workflow strict:
   - one ticket = one hypothesis = one measurable attempt
6. Record any metric caveats or environmental assumptions in contextspace.

## Constraints

- Do not measure multiple hypotheses in a single ticket.
- Do not reference raw script paths from this workflow; use `car apps run ...`.
- Prefer explicit guard commands if reliability matters.
