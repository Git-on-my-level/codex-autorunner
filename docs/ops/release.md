# Release process

This repo uses tag-driven releases that publish to PyPI and create a GitHub Release.

## One-time setup
- Create the PyPI project and configure GitHub Actions as a trusted publisher.
- Confirm the repo has the `release.yml` workflow in `.github/workflows/`.

## Release steps
1) Update `pyproject.toml` to the new version.
2) Commit the change and push to `main`.
3) Tag the commit with `vX.Y.Z` and push the tag.

Example:
```bash
git tag v0.1.1
git push origin v0.1.1
```

## What the workflow does
- Builds sdist/wheel.
- Verifies the tag version matches `pyproject.toml`.
- Smoke-tests the wheel for static assets.
- Publishes to PyPI.
- Creates a GitHub Release with generated notes.

## Troubleshooting
- If the tag does not match `pyproject.toml`, the workflow will fail fast.
- If PyPI publish fails, confirm trusted publisher settings or use a PyPI API token.
