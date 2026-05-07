# CAR Path Model

CAR uses explicit roots because PMA and managed threads can run from different working directories.

- Hub root: the directory containing `.codex-autorunner/config.yml`.
- Repo root: a git checkout registered in the hub manifest.
- Worktree root: a hub-managed or registered worktree for a repo.
- Runtime cwd: where the agent process is actually executing.

Hub-scoped PMA docs live under:

`.codex-autorunner/pma/docs/`

Repo-scoped docs live under each repo root:

`.codex-autorunner/ABOUT_CAR.md`
`.codex-autorunner/contextspace/`
`.codex-autorunner/tickets/`

When an agent is running inside a repo, relative PMA doc paths can be misleading. Prefer absolute paths or `car docs path <doc_id> --path <hub_root>`.
