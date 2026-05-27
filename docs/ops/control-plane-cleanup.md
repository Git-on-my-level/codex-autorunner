# Non-Authoritative Control-Plane Cleanup

CAR hubs own the authoritative runtime control plane under the hub root. Repo
and worktree `.codex-autorunner/` directories can still contain legacy files
that look authoritative, such as `manifest.yml`, `orchestration.sqlite3`,
`hub_projection.sqlite3`, compatibility metadata, and migration locks. In a
hub-owned workspace these files are not runtime authority; tickets,
contextspace, filebox, GitHub context, diagnostics, and logs remain valid
repo-local project data.

Preview cleanup before changing disk state:

```bash
car cleanup control-plane --hub /path/to/hub
car cleanup control-plane --hub /path/to/hub --json
```

The report lists each artifact, byte size, classification reason, workspace
role, and proposed archive path. It uses the hub workspace registry and
control-plane classification, so an explicitly launched standalone hub is not
cleaned. Nested standalone hubs under the selected hub root, and their managed
workspaces, are skipped as separate control planes.

Apply cleanup only after reviewing the dry run:

```bash
car cleanup control-plane --hub /path/to/hub --apply
```

The first implementation archives instead of deleting. Files move under:

```text
<hub>/.codex-autorunner/archive/control-plane-cleanup/<timestamp>/
```

To restore an archived file, move it back to the original relative path shown
in the dry-run or apply report. Run `car doctor --repo /path/to/hub` to see a
doctor hint that links nested control-plane artifacts back to this cleanup
command.
