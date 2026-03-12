# Hotspot Budget Guardrails

This repo now carries a narrow architectural budget check for the specific seams
that were extracted in the refactor pack behind tickets `100/110`, `120/130`,
`200/210`, and `300/310`.

It is intentionally not a repo-wide complexity policy.

## What the check covers

- Extracted web-route seams and their remaining legacy composition owners
- The shared Discord command slices plus the still-centralized command monoliths
- Shared OpenCode progress/token helper ownership
- Extracted ticket-runner seams plus the remaining `TicketRunner.step()` hotspot

The budgets live in `tests/test_hotspot_budgets.py`.

The file is split into:

- `STANDARD_FILE_BUDGETS`
- `STANDARD_FUNCTION_BUDGETS`
- `LEGACY_FILE_CAPS`
- `LEGACY_FUNCTION_CAPS`
- `HELPER_OWNERSHIP_RULES`

Standard budgets protect seams that are already extracted and should stay small.
Legacy caps are explicit exceptions for hotspots that still exist but are not
allowed to grow unchecked.

## How to run

```bash
.venv/bin/python -m pytest tests/test_hotspot_budgets.py -q
```

## Updating a budget intentionally

1. Change the relevant budget or legacy cap in `tests/test_hotspot_budgets.py`.
2. Keep the change narrow to the seam you actually touched.
3. Update the adjacent `reason` text if the exception or ceiling changed.
4. Run the hotspot budget test and the focused regression suite for that seam.

## Escape hatch

If a hotspot cannot be reduced yet, move it into a legacy cap or raise the
existing legacy cap with a concrete reason. Do not loosen unrelated budgets.

When a legacy hotspot is finally extracted, tighten or remove the legacy cap in
the same change so the check reflects the new shape instead of preserving stale
exceptions.
