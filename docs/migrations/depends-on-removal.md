# Migration: depends_on removal

As of February 7, 2026, ticket frontmatter no longer supports `depends_on`. The ticket flow processes tickets strictly by filename order (`TICKET-###*.md`), and dependency edges are not enforced.

## What changed
- `depends_on` is rejected by the ticket frontmatter linter.
- Ordering must be expressed by ticket numbering.

## How to migrate
1. Remove `depends_on` from ticket frontmatter.
2. Rename tickets (or insert gaps) so the desired execution order matches numeric order.

If you need to capture rationale, describe the dependency in the ticket body instead of frontmatter.
