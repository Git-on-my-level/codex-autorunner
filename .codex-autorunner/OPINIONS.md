# Opinions

- Prefer small, well-tested changes; favor minimal new dependencies.
- Keep TODO/PROGRESS/OPINIONS in sync with each run.
- Ignore runtime artifacts in git (logs, lock, state files) to keep diffs clean.
- Favor Typer-based CLI patterns and readable prompts.
- For the web UI: keep the bundle simple (no heavy design systems), mobile-friendly tabs for Dashboard/Docs/Logs/Chat, and use the existing API shape (serve static assets from the same FastAPI app).
- Surface kill/resume controls in the UI and reflect runner pid/status clearly.
- Clean, modern UI that is easy to understand and elegant. Think dark theme and futuristic
