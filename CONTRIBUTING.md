# Contributing

Thanks for helping improve codex-autorunner.

## Ground rules
- Keep changes small and focused.
- Keep docs in sync with behavior changes.
- Avoid unnecessary dependencies.

## Proposing changes
- Open an issue for bugs or larger changes so we can align first.
- For small fixes, a focused PR is fine without prior discussion.

## Development
- Bootstrap dev env (venv, dev deps, npm deps, hooks): `make setup`
- Install dev deps: `pip install -e .[dev]`
- Run tests: `python -m pytest` (or `make test`)
- JS lint (UI): `npm run lint:js`
- Format: `python -m black src tests`
- Build static assets: `pnpm run build` (source is `src/codex_autorunner/static_src/`, output is `src/codex_autorunner/static/`)

## Pull requests
- Explain the user-facing impact.
- Include tests when behavior changes.
- Update relevant docs if you touch config or UX.
