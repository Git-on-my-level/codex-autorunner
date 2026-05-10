# Templates and Apps

Ticket templates are reusable ticket packs or ticket files from configured template repos.

Useful commands:

- `car templates list --repo <repo_path> --path <hub_root>`
- `car templates search <query> --repo <repo_path> --path <hub_root>`
- `car templates show <id> --repo <repo_path> --path <hub_root>`
- `car templates apply <id> --repo <repo_path> --path <hub_root>`

CAR apps are installable bundles with entrypoints and optional tools.

Useful commands:

- `car apps list --repo <repo_path> --path <hub_root>`
- `car apps show <ref> --repo <repo_path> --path <hub_root>`
- `car apps install <ref> --repo <repo_path> --path <hub_root>`
- `car apps apply <app_id> --repo <repo_path> --path <hub_root>`
- `car apps tools <app_id> --repo <repo_path> --path <hub_root>`
