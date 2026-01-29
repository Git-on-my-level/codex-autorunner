# Import Boundary Check

CAR enforces layer boundaries (Surfaces → Adapters → Control Plane → Engine).
The import-boundary check prevents new violations while allowing existing ones via an allowlist.

## What the check does
- Scans `src/codex_autorunner/**` for Python imports.
- Applies boundary rules to core and integrations modules.
- Fails only when **new** violations appear compared to the allowlist.

## How to run
```
.venv/bin/python scripts/check_import_boundaries.py
```
(or `make check`, which runs the full check suite).

## Interpreting failures
If the script reports:
```
New import boundary violations detected:
- src/.../file.py:LINE imports codex_autorunner....
```
then a new boundary violation was introduced.

## Fix options
1. **Fix the import** by moving logic to a lower layer or introducing a port/interface.
2. **Allowlist temporarily** if the coupling is intentional and tracked for later cleanup.

## Updating the allowlist
- File: `scripts/import_boundaries_allowlist.json`
- Each entry must include `importer`, `imported`, and a short `reason`.
- Prefer narrow, explicit entries rather than wildcards.

Remove stale entries when the violation is fixed (the checker will report them as no longer needed).
