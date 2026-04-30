---
agent: codex
done: false
title: "AutoOptimize closeout"
goal: "Validate the AutoOptimize campaign state, render final artifacts, and summarize the best result plus follow-ups."
---

# AutoOptimize Closeout

## Tasks

1. Validate campaign state:
   - `car apps run blessed.autooptimize validate-state`
2. Confirm the app recommends closeout:
   - `car apps run blessed.autooptimize plan-next-ticket`
3. Render final artifacts:
   - `car apps run blessed.autooptimize render-summary-card`
4. Summarize:
   - best result
   - failed or discarded attempts
   - caveats
   - recommended follow-ups
5. Ensure the artifacts exist for chat wrap-up:
   - `.codex-autorunner/apps/blessed.autooptimize/artifacts/summary.md`
   - `.codex-autorunner/apps/blessed.autooptimize/artifacts/summary.png`

## Constraints

- If `validate-state` fails, fix state first rather than hand-waving past the inconsistency.
- Do not manually edit rendered artifacts unless you are correcting a deterministic renderer bug.
