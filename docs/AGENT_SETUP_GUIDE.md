# CAR Setup Guide (for Agents)

This document is for an AI agent to walk the user through setting up CAR (codex-autorunner) based on your environment and preferences.

---

## Optional Add-On Guides

Use these only after base setup is complete:

- Telegram interactive bot onboarding: `docs/AGENT_SETUP_TELEGRAM_GUIDE.md`
- Discord interactive bot onboarding: `docs/AGENT_SETUP_DISCORD_GUIDE.md`

These are optional integrations, not required for baseline CAR setup.

---

## Instructions for the Agent

You are helping a user set up CAR (codex-autorunner), an agent coordination tool. Follow these steps interactively, asking clarifying questions when needed.

### Step 1: Understand the User's Goal

Ask the user which setup they want:

1. **Hub mode** (recommended) — Run a central hub that manages multiple repositories. This is the typical setup for most users.
2. **Single repo mode** — Use CAR to manage agents working on a single specific repository. This mode is primarily used for developing CAR itself and is not recommended for general use.

Recommend hub mode unless the user is explicitly working on CAR development or has a specific reason to use single repo mode.

### Step 2: Check Prerequisites

Verify the user has:

1. **Python 3.9+** installed
2. **At least one supported agent** installed and working:
   - [Codex CLI](https://github.com/openai/codex) — `codex --version`
   - [Opencode](https://github.com/opencode-ai/opencode) — `opencode --version`
3. **A directory for the hub** — This can be a new empty directory; repositories can be created or cloned through the hub later

If any prerequisites are missing, help them install what's needed.

### Step 3: Install CAR

CAR can be installed via pip or pipx:

```bash
# Using pipx (recommended for CLI tools)
pipx install codex-autorunner

# Or using pip
pip install codex-autorunner
```

Verify the installation:

```bash
car --version
```

### Step 4: Initialize the Hub

Create a directory for your hub and initialize it:

```bash
mkdir ~/car-hub  # or wherever you want your hub
cd ~/car-hub
car init --mode hub
```

This creates the hub structure with:
- `.codex-autorunner/manifest.yml` — Lists managed repositories
- `.codex-autorunner/config.yml` — Hub configuration
- `repos/` — Default directory where repositories will live

**Alternative: Single Repo Mode (development only)**

If the user is setting up single repo mode for CAR development:

```bash
cd /path/to/your/repo
car init --mode repo
```

### Step 5: Run the Doctor Check

Verify the setup is correct:

```bash
car doctor
```

This validates the configuration and checks that required agents are available.

### Step 6: Start the Hub Web UI

The web UI is the primary interface for managing CAR:

```bash
car serve
```

Open http://localhost:8765 in your browser. From here you can:
- Add existing repositories or clone new ones
- Create and manage tickets
- Monitor agent runs
- Use the built-in terminal for interactive agent sessions

### Step 7: Add a Repository

From the web UI, you can:
- **Scan** for existing git repositories in or near your hub directory
- **Clone** a repository from a URL
- **Create** a new repository from scratch

Alternatively, via CLI:

```bash
# Clone an existing repo
car hub clone https://github.com/your/repo.git

# Or create a new repo
car hub create my-new-project
```

### Step 8: Discover PMA and Destination Surfaces (Hub Mode)

Once hub setup is working, surface these two command families so users can find them quickly:

```bash
# PMA command surface (chat, threads, targets)
car pma --help
car pma chat "hello from setup" --path <hub_root>
car pma thread --help
car pma targets --help

# PMA docs/context/files surfaces
car pma docs show active --path <hub_root>
car pma context snapshot --path <hub_root>
car pma context compact --dry-run --path <hub_root>
car pma files --path <hub_root>
car pma upload inbox ./notes.txt --path <hub_root>
car pma download outbox response.txt --path <hub_root>
car pma delete inbox --all --path <hub_root>

# Repo/worktree destination controls
car hub destination --help
car hub destination show <repo_id> --path <hub_root>
car hub destination show <repo_id> --json --path <hub_root>
car hub destination set <repo_id> local --path <hub_root>
car hub destination set <repo_id> local --json --path <hub_root>
car hub destination set <repo_id> docker --image ghcr.io/example/my-codex-runner:latest --json --path <hub_root>
```

Use `car hub destination show ... --json` when validating destination changes; output includes `configured_destination`, `effective_destination`, `source`, and `issues` for quick scriptable checks.

Destination/reference links:
- API endpoints: `GET /hub/repos`, `POST /hub/repos/{repo_id}/destination`, and `POST /hub/repos/{repo_id}/settings`
- Web/API surface map: `src/codex_autorunner/surfaces/web/README.md`
- Docker destination examples + inheritance: `docs/configuration/destinations.md`

## Key Concepts to Explain

If the user asks, explain these concepts:

### Tickets
Tickets are the control plane for CAR. They are markdown files with frontmatter that define:
- What work needs to be done
- Which agent should do it
- Current status (pending, in_progress, done, blocked, etc.)

Agents can also create new tickets to break down complex work.

### Contextspace Documents
These are shared context files in each repo's `.codex-autorunner/contextspace/`:
- `active_context.md` — Short-lived context for the current effort
- `decisions.md` — Durable architectural/product decisions
- `spec.md` — Requirements specification

Both you and the agents can read and write these. They're accessible from the web UI.

### Hub vs Repo
- **Hub**: The central management layer. Contains a manifest of repositories and provides the web UI, terminal, and coordination features.
- **Repo**: An individual repository being managed. Each repo has its own `.codex-autorunner/` directory with tickets, contextspace docs, and state.

### Agents
CAR currently supports:
- **Codex** — OpenAI's coding agent
- **Opencode** — Open-source alternative

CAR passes tickets to agents along with relevant context, and agents execute the work.

### File System as Truth
CAR's philosophy is that the file system is the source of truth. Tickets, contextspace docs, and all state live on disk. This makes everything inspectable, versionable, and debuggable.

---

## Troubleshooting

### "Agent not found"
Make sure the agent is installed and available in your PATH. Run `codex --version` or `opencode --version` to verify.

Inside app-server-backed CAR sessions (web terminal/PMA and Telegram), `car` is
resolved from each workspace shim path (`<workspace>/.codex-autorunner/bin`, plus
`<workspace>` when a root `car` shim exists), so runtime resolution does not depend
on a global `car` installation.

### "No pending tickets"
Create a ticket file in `<repo>/.codex-autorunner/tickets/` with `status: pending`, or use the web UI to create one.

### "No repositories found"
Run `car hub scan` to discover existing git repositories, or use `car hub clone` / `car hub create` to add one.

### Configuration Issues
Check `.codex-autorunner/config.yml` or run `car doctor` for diagnostics.

### ticket_flow resume blocked or stale
If `car flow ticket_flow resume` fails because another run is active, inspect that active run before creating a new one.

If the conflicting run is healthy (`status: running`, `worker.status: alive`), resume or stop that run instead of using `--force-new`.

Use `--force-new` only when the conflicting active run is confirmed stale (`worker.status: absent` / `worker metadata missing`):

`car flow ticket_flow start --repo <path> --force-new`

Then verify the new run is live:

`car flow ticket_flow status --json --repo <path> [--run-id <uuid>]`

Only use `resume` by default when the target run metadata is healthy and there is no active-run conflict.

### Web UI not loading
Ensure `car serve` is running and check the terminal output for errors. The default port is 8765.

---

## Next Steps

Once basic setup is complete, suggest these next steps:

1. **Add more repositories** — Clone or create additional projects to manage from your hub
2. **Create a plan** — Chat with an AI to design a feature or fix, then convert it to tickets
3. **Add chat integrations (optional)** — Use `docs/AGENT_SETUP_TELEGRAM_GUIDE.md` or `docs/AGENT_SETUP_DISCORD_GUIDE.md`
4. **Explore the Web UI** — Browse the hub dashboard, try the built-in terminal, use voice input
5. **Read the docs** — Point to relevant docs in the `docs/` directory for advanced configuration

---

## Reference Commands

### Hub Commands

| Command | Description |
|---------|-------------|
| `car init --mode hub` | Initialize a hub |
| `car serve` | Start the hub web UI |
| `car hub scan` | Scan for repositories |
| `car hub clone <url>` | Clone a repo into the hub |
| `car hub create <name>` | Create a new repo in the hub |
| `car hub worktree create <base_repo_id> <branch>` | Create a hub-owned worktree (defaults to `origin/<default-branch>`) |
| `car hub destination show <repo_id>` | Show configured + effective destination for a repo/worktree |
| `car hub destination set <repo_id> <local|docker>` | Set destination execution mode for a repo/worktree |
| `car doctor` | Validate hub/repo setup |

### PMA Commands (hub-level)

| Command | Description |
|---------|-------------|
| `car pma --help` | Discover PMA subcommands |
| `car pma chat "<message>" --path <hub_root>` | Send a PMA turn via CLI |
| `car pma thread --help` | Discover managed PMA thread operations |
| `car pma targets --help` | Discover PMA delivery target controls |
| `car pma docs show <agents|active|log> --path <hub_root>` | Print PMA docs from `.codex-autorunner/pma/docs` |
| `car pma context snapshot --path <hub_root>` | Append current `active_context.md` into `context_log.md` |
| `car pma context compact --dry-run --path <hub_root>` | Preview deterministic active-context compaction |
| `car pma files --path <hub_root>` | List PMA inbox/outbox files |
| `car pma upload <inbox|outbox> <file...> --path <hub_root>` | Upload local files into PMA FileBox |
| `car pma download <inbox|outbox> <filename> --path <hub_root>` | Download a PMA file locally |

### Repo Commands (run from within a repo, or use `--repo`)

| Command | Description |
|---------|-------------|
| `car flow ticket_flow bootstrap` | Seed tickets (if needed) and start a ticket flow |
| `car flow ticket_flow start` | Start/resume the latest ticket flow (use `--force-new` when resume metadata is stale) |
| `car flow ticket_flow status` | Show ticket flow status |
| `car flow ticket_flow resume` | Resume a paused ticket flow |
| `car flow ticket_flow stop` | Stop a ticket flow |
| `car status` | Show autorunner status |
| `car log` | Show autorunner log output |
| `car edit <doc>` | Open a workspace doc in your editor |

---

*This guide is meant to be consumed by an AI agent helping you set up CAR. The agent will adapt these instructions to your specific environment and needs.*
