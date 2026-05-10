# Troubleshooting

Start by identifying the hub root and runtime cwd.

Common checks:

- Hub config: `car hub endpoint --path <hub_root>`
- Hub scan: `car hub scan --path <hub_root>`
- Managed thread status: `car pma thread status --id <thread_id> --path <hub_root>`
- Ticket flow status: `car ticket-flow status --repo <repo_path> --path <hub_root>`
- Destination: `car hub destination show <repo_id> --path <hub_root>`
- Docs discovery: `car docs search <query> --path <hub_root>`

If a prompt references `.codex-autorunner/pma/docs/*` from inside a repo, treat those as hub-scoped docs and resolve them through `car docs path pma/about --path <hub_root>`.
