# CAR (codex-autorunner)
[![PyPI](https://img.shields.io/pypi/v/codex-autorunner.svg)](https://pypi.org/project/codex-autorunner/)

**Plan once, then let your favorite coding agents grind through tickets while you sleep — and ping you on Telegram or Discord when they're stuck.**

CAR is a meta-harness for coding agents (Codex, Hermes, OpenCode, anything ACP). It is _not_ a coding agent itself. You bring the plan and the agents; CAR coordinates the long-running execution.

> 🧪 Built for developers comfortable running long-horizon agent workflows on their own machine. If you babysit every agent turn, this isn't the tool for you.

![CAR Hub](docs/screenshots/ticket-3-column.png)

**What you actually do with CAR:**
- ✍️ Write a plan, or chat with an AI to generate one
- 🎫 Convert it into CAR tickets (markdown + frontmatter) — use the [CAR Ticket Skill](docs/car-ticket-skill.md) with any assistant
- 🚶 Walk away. Agents work the queue and notify you (Telegram, Discord, inbox) only when they need input

---

## 🚀 Quickstart

### Option 1 — Let your agent install it (recommended)

Paste this to Codex, Cursor, Hermes, OpenClaw, or whichever assistant you trust on your machine:

> Please walk me through setting up CAR (codex-autorunner) using this guide:
> https://github.com/Git-on-my-level/codex-autorunner/blob/main/docs/AGENT_SETUP_GUIDE.md

The agent will check prerequisites, install CAR, initialize a hub, and configure your first repo interactively.

### Option 2 — Install it yourself

```bash
pipx install codex-autorunner   # or: pip install codex-autorunner
car --version
mkdir ~/car-hub && cd ~/car-hub
car init --mode hub
```

Then open the web UI and add a repo. Full walkthrough: [AGENT_SETUP_GUIDE.md](docs/AGENT_SETUP_GUIDE.md).

### Recommended add-ons

- 💬 [Telegram setup](docs/AGENT_SETUP_TELEGRAM_GUIDE.md) · [Discord setup](docs/AGENT_SETUP_DISCORD_GUIDE.md) - Pick one
- 🤖 [Hermes setup](docs/ops/hermes-acp.md) - Recommended PMA, keeps memory across all your CAR projects
- 🐳 [Docker runtime per repo/worktree](docs/configuration/destinations.md) - For running agents in a containerized environment

---

## 🧠 How it works

At its core, CAR is a state machine: while there are incomplete tickets, pick the next one and run it against an agent. Tickets can be pre-written by you, by agents, or on the fly.

> _Tickets are the control plane. Agents are the execution layer._

When an agent wakes up it gets: knowledge of CAR, a pre-defined `contextspace`, the current ticket, and optionally the previous agent's output. That's it.

📸 [See it in action — full screenshot gallery](docs/screenshots/GALLERY.md)

---

## 🎛️ Ways to interact

| Surface | When to use it |
|---|---|
| **Web UI** | Main control plane. Set up repos, chat with agents, run the autorunner, view usage. Start here. ([security notes](docs/web/security.md)) |
| **CLI** | The agent-friendly surface. Not really made for human use. |
| **Telegram / Discord** | Persistent multi-device chat without exposing your hub to the internet. |
| **Project Manager Agent (PMA)** | Conversational interface to CAR itself. Available in the web UI and chat apps. Hermes makes [an excellent PMA](docs/ops/hermes-acp.md) thanks to its global memory. |

---

## 🤖 Supported agents

- **Codex**
- **Hermes**
- **OpenCode**

CAR integrates any reasonable [ACP](https://github.com/zed-industries/agent-client-protocol) agent. Want yours added? Open an issue or PR.

---

## 🧭 Philosophy

CAR is _very bitter-lesson-pilled_. As models and agents get stronger, CAR should serve as leverage and stay out of their way. We treat the filesystem as the first-class data plane and lean on tools agents already know cold (git, python, markdown).

Because tickets are the control plane and agents are the execution layer, **CAR is an amplifier**. With a strong model it sings; with a model that scope-creeps or reward-hacks (marks tickets done that aren't), it will not.

### Tickets as code

Tickets aren't just task descriptions — they're a software layer that operates inside CAR. You can write tickets that scope a feature and generate child tickets, spawn subagent code reviews, repay tech debt, etc. Tickets can be repo-agnostic or project-specific.

I maintain a ["blessed" set of templates](https://github.com/Git-on-my-level/car-ticket-templates) accessible from any CAR deployment. Got a generalizable ticket that works well across agents? Contribute it.

---

## 📚 Learn more

- 🗺️ [Interactive architecture explorer (Principal Forks)](https://app.principal-ade.com/Principal-Forks/codex-autorunner)
- 📜 [Codebase constitution](docs/car_constitution/10_CODEBASE_CONSTITUTION.md)
- 🏛️ [Architecture map](docs/car_constitution/20_ARCHITECTURE_MAP.md)
- 📐 [Engineering standards](docs/car_constitution/30_ENGINEERING_STANDARDS.md)
- 📝 [Run history contract](docs/RUN_HISTORY.md) · [State roots contract](docs/STATE_ROOTS.md)

### From source

```bash
./car --help
```

The shim tries `PYTHONPATH=src` first and bootstraps a local `.venv` if dependencies are missing.

---

## ⭐ Star history

[![Star History Chart](https://api.star-history.com/svg?repos=Git-on-my-level/codex-autorunner&type=Date)](https://star-history.com/#Git-on-my-level/codex-autorunner&Date)
