---
agent: opencode
done: false
title: "AutoOptimize baseline"
goal: "Measure and record the baseline result for the AutoOptimize campaign."
---

# AutoOptimize Baseline

## Tasks

1. Run the agreed primary metric measurement.
2. Run guard commands if the campaign defines them.
3. Record the baseline:
   - `car apps run blessed.autooptimize record-baseline -- --value <value> [--unit <unit>] [--summary "<notes>"]`
4. Record any measurement caveats in contextspace.
5. Confirm the baseline is reflected in:
   - `car apps run blessed.autooptimize status`

## Constraints

- Use the same metric contract defined during bootstrap.
- If the campaign unit differs from the measured unit, stop and reconcile the metric contract before recording state.
