# CAR Ticket Skill

Use this with ChatGPT, Claude, Codex, or any other assistant when you want help converting a plan into CAR-compatible tickets.

Prompt pattern:

1. Share your implementation plan.
2. Attach or paste this skill doc.
3. Ask the assistant to produce `TICKET-###*.md` files that follow these rules.

---

## 1) Ticket files

- Filename: `TICKET-###*.md`
  Examples: `TICKET-001.md`, `TICKET-120-api-parity.md`
- Tickets **must be in ascending numeric order**.
- Numbers **do not need to be consecutive**.
  Leave gaps if follow-up tickets are likely.

---

## 2) Required frontmatter (minimum)

```yaml
---
agent: "codex"
done: false
---
```

Required (linted):

- `agent`: registered CAR agent id for this repo (for example `codex`, `opencode`) or the special value `user`
- `done`: boolean

Do not use assistant product names as `agent` values (for example `chatgpt` or `claude`) unless those exact ids are configured in CAR for this repo.

Common optional fields:

- `title`: short, outcome-focused summary
- `goal`: one-sentence statement of intent
- `model`: pin a specific model when necessary

---

## 3) Recommended ticket body structure

Keep tickets concise and independently verifiable.

Preferred sections:

- `## Tasks` - concrete implementation steps
- `## Acceptance criteria` (or `## Exit criteria`) - observable outcomes
- `## Tests` - commands or explicit verification steps
- `## Notes` - only if necessary

Write criteria so another agent can prove completion without guesswork.

---

## 4) Sequencing rules (critical)

CAR's `ticket_flow`:

- Executes tickets in ascending order
- Picks the first ticket where `done != true`

Implications:

- Put prerequisites in lower-numbered tickets.
- Never make a lower-numbered ticket depend on a higher-numbered one.
- If reverse dependencies appear, reorder or split tickets.
- Each ticket must be independently completable when its turn arrives.

---

## 5) Assignment defaults

- Implementation work -> repo agents (`codex`, `opencode`, etc.).
- Final human review/signoff -> user-assigned ticket near the end.
- In PMA mode, prefer delegation, not direct code edits.

---

## 6) Quality bar for good tickets

A good ticket:

- Has a single, well-scoped outcome
- References specific files/modules when useful
- Includes explicit verification (tests, checks, or observable behavior)
- Avoids vague language (`"improve"`, `"clean up"`, `"fix issues"`) without criteria

---

## 7) Copy-paste ticket template

```md
---
title: "<Outcome-focused title>"
agent: "codex"
done: false
goal: "<What will be true when this ticket is complete>."
---

## Tasks
- <Concrete implementation step>
- <Concrete implementation step>

## Acceptance criteria
- <Observable behavior or artifact>
- <Observable behavior or artifact>

## Tests
- <Commands to run or explicit checks>
```

---

## 8) Anti-patterns to reject

- Missing or invalid frontmatter
- Cross-ticket dependency deadlocks
- Tickets that rely on unstated or hidden context
- `done: true` without evidence against acceptance criteria
