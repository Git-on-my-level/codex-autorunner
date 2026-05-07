# Ticket Flow

Ticket flow executes repo-local tickets in filename order.

Tickets live under:

`.codex-autorunner/tickets/`

The runtime repeatedly selects the first `TICKET-###*.md` where `done != true`, runs one agent turn, and then advances when the ticket is complete.

`depends_on` frontmatter is not an execution contract. If prerequisites matter, put prerequisite work in earlier ticket numbers.

Use ticket flow for structured multi-step work, cross-repo plans, explicit acceptance criteria, or work that needs pause/resume and user dispatches.

Use `car docs show repo/ticket-flow --repo <repo_path> --path <hub_root>` for a repo's generated quickstart when available.
