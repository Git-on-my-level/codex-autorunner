---
agent: opencode
done: false
title: "AutoOptimize iteration"
goal: "Run one bounded optimization hypothesis, measure the primary metric, and record the decision explicitly."
---

# AutoOptimize Iteration

## Tasks

1. Make one bounded change for one hypothesis.
2. Measure the primary metric.
3. Run guard commands.
4. Decide one of:
   - `keep`
   - `discard`
   - `pivot`
   - `blocked`
5. Record the outcome:
   - `car apps run blessed.autooptimize record-iteration -- --iteration <n> --ticket <ticket> --hypothesis "<hypothesis>" --value <value> --decision <decision> --guard-status <pass|fail|not_run> [--unit <unit>] [--commit-before <sha>] [--commit-after <sha>] [--milestone "<label>"] [--summary "<notes>"]`
6. Revert or preserve code changes according to the decision.
7. Ask the app which ticket should come next, then apply the recommended template:
   - `car apps run blessed.autooptimize plan-next-ticket`

## Constraints

- One ticket = one hypothesis = one measurable attempt.
- Keep/discard/pivot/blocked must be explicit.
- If the metric cannot be measured reliably, record `blocked` with a concrete summary of what prevented measurement.
- Stop conditions are interpreted by `plan-next-ticket`; hooks may render or attach artifacts, but they do not create hidden loop work.
