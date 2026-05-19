# Release process

This repo uses tag-driven releases that publish to PyPI and create a GitHub Release.

## One-time setup
- Create the PyPI project and configure GitHub Actions as a trusted publisher.
- Confirm the repo has the `release.yml` workflow in `.github/workflows/`.

## Release steps
1) Update `pyproject.toml` to the new version.
2) Run the local release gates.
3) Commit the change and push to `main`.
4) Tag the commit with `vX.Y.Z` and push the tag.

## Local release gates

### Automation migration gate

Run the automation migration observability checks before tagging:

```bash
.venv/bin/python -m pytest -q tests/test_doctor_checks.py tests/core/orchestration/test_sqlite_migrations.py tests/test_migration_observability_docs.py
.venv/bin/python scripts/check_migration_observability_docs.py
tmp_root="$(mktemp -d)"; .venv/bin/python -m codex_autorunner.cli hub orchestration canary --path "$tmp_root" --json
```

The gate must show that `car doctor --json`, `car hub orchestration status --json`,
and `car pma automation migration-status --json` continue to expose pending
schema migrations, legacy PMA automation residue, malformed rows, mirror health,
and next steps as stable JSON.

Example:
```bash
git tag v0.1.1
git push origin v0.1.1
```

## What the workflow does
- Builds Web Hub static assets from `src/codex_autorunner/web_frontend/`.
- Builds sdist/wheel.
- Verifies the tag version matches `pyproject.toml`.
- Smoke-tests the wheel for static assets.
- Publishes to PyPI.
- Creates a GitHub Release with generated notes.

## Troubleshooting
- If the tag does not match `pyproject.toml`, the workflow will fail fast.
- If PyPI publish fails, confirm trusted publisher settings or use a PyPI API token.
