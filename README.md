# CAR (codex-autorunner)
[![PyPI](https://img.shields.io/pypi/v/codex-autorunner.svg)](https://pypi.org/project/codex-autorunner/)

CAR provides a set of low-opinion agent coordination tools for you to run long complex implementations using the agents you already love.

What this looks like in practice:
- You write a plan, or generate a plan by chatting with your favorite AI
- You convert the plan (or ask an AI to convert it for you) into CAR compatible tickets (markdown with some frontmatter)
- Go off and do something else, no need to babysit the agents, they will notify you if they need your input

![CAR Web Hub Screenshot](docs/screenshots/hub.png)

## How it works
CAR is very simple. At it's core, CAR is a state machine which checks to see if there are any incomplete tickets. If yes, pick the next one and run it against an agent. Tickets can be pre-populated by the user, but agents can also write tickets. _Tickets are the control plane for CAR_.

When each agent wakes up, it gets knowledge about CAR and how to operate within CAR, a pre-defined set of context (workspace files), the current ticket, and optionally the final output of the previous agent. This simple loop ensures that agents know enough to use CAR while also focusing them on the task at hand.

## Philosophy
The philosophy behind CAR is to let the agents do what they do best, and get out of their way. CAR is _very bitter-lesson-pilled_. As models and agents get more powerful, CAR should serve as a form of leverage, and avoid constraining models and their harnesses. This is why we treat the filesystem as the first class data plane and utilize tools and languages the models are already very familiar with (git, python).

CAR treats tickets as the control plane and models as the execution layer. This means that we rely on agents to follow the instructions written in the tickets. If you use a sufficiently weak model, CAR may not work well for you. CAR is an amplifier for agent capabilities. Agents who like to scope creep (create too many new tickets) or reward hack (mark a ticket as done despite it being incomplete) are not a good fit for CAR.

## Interaction patterns
CAR's core is a set of python functions surfaced as a CLI, operating on a file system. There are current 2 UIs built on top of this core.

### Web UI
The web UI is the main control plane for CAR. From here you can set up new repositories or clone existing ones, chat with agents using their TUI, and run the ticket autorunner. There are many quality-of-life features like Whisper integration, editing documents by chatting with AI (useful for mobile), viewing usage analytics, and much more. The Web UI is the most full featured user-facing interface and a good starting point for trying out CAR.

I recommend serving the web UI over Tailscale. There is an auth token option but the system is not very battle tested.

### Telegram
Telegram is the "on-the-go" and notification hub for CAR. From here you can kick off and monitor existing tickets, set up new tickets, and chat with agents. Your primary UX here is asking the agent to do things for you rather than you doing it yourself like you would on the web UI. This is great for on-the-go work, but it doesn't have full feature parity with the web UI.

### PMA durable docs
PMA keeps hub-scoped docs under `.codex-autorunner/pma/`. Put durable guidance in `AGENTS.md`, keep short-lived notes in `active_context.md`, and append timestamped snapshots to `context_log.md` when pruning. In the web UI, switch PMA to manual mode to edit these docs and trigger snapshots.

## Quickstart

The fastest way to get started is to pass [this setup guide](docs/AGENT_SETUP_GUIDE.md) to your favorite AI agent. The agent will walk you through installation and configuration interactively based on your environment.

**TL;DR for the impatient:**

# Install
```
pipx install codex-autorunner
```
# Initialize in your repo
```
cd /path/to/your/repo
car init
```
# Verify setup
```
car doctor
```
# Create a ticket and run
```
car flow ticket_flow bootstrap
```

## Supported models
CAR currently supports:
- Codex
- Opencode

CAR is built to easily integrate any reasonable agent built for Agent Client Protocol (ACP). If you would like to see your agent supported, please reach out or open a PR.

## Examples
Build out complex features and products by providing a series of tickets assigned to various agents.
![CAR Tickets in Progress Screenshot](docs/screenshots/tickets-in-progress.png)

Tickets are just markdown files that both you and the agent can edit.
![CAR Ticket Markdown Screenshot](docs/screenshots/ticket-markdown.png)

You don't have to babysit the agents, they inbox you or ping you on Telegram.
![CAR Inbox Screenshot](docs/screenshots/inbox.png)

You can collaborate with the agents in a shared workspace, independent of the codebase. Drop context there, extract artifacts, it's like a shared scratchpad.
![CAR Workspace Screenshot](docs/screenshots/workspace.png)

All core workspace documents are also just markdown files, so you and the agent can easily edit them.
![CAR Workspace New MD Screenshot](docs/screenshots/workspace-new-md.png)

If you need to do something more custom or granular, you can use your favorite agent TUIs in the built-in terminal.
![CAR Terminal Codex Screenshot](docs/screenshots/terminal-codex.png)
![CAR Terminal Opencode Screenshot](docs/screenshots/terminal-opencode.png)

On the go? The web UI is mobile responsive, or if you prefer you can type or voice chat with your agents on Telegram.
![CAR Telegram Media Voice Screenshot](docs/screenshots/telegram-media-voice.PNG)
![CAR Telegram Media Image Screenshot](docs/screenshots/telegram-media-image.PNG)

## Star history
[![Star History Chart](https://api.star-history.com/svg?repos=Git-on-my-level/codex-autorunner&type=Date)](https://star-history.com/#Git-on-my-level/codex-autorunner&Date)
