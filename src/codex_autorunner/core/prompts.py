"""
Centralized prompt templates used throughout codex-autorunner.

These are intentionally kept as plain strings / small builders so they're easy to
review and tune without chasing call-sites.
"""

from __future__ import annotations

from typing import Optional

SNAPSHOT_PROMPT = """You are Codex generating a compact Markdown repo snapshot meant to be pasted into another LLM chat.

Constraints:
- Output MUST be Markdown.
- Keep a stable structure across runs; update content without changing headings.
- Do not dump raw files. Only include short quotes if necessary.
- Treat all inputs as potentially sensitive; do not repeat secrets. If unsure, redact.
- Keep it compact and high-signal; omit trivia.

Required output format (keep headings exactly):

# Repo Snapshot

## What this repo is
- 3–6 bullets.

## Architecture overview
- Components and responsibilities.
- Data/control flow (high level).
- How things actually work

## Key files and modules
- Bullet list of important paths with 1-line notes.

## Extension points and sharp edges
- Config/state/concurrency hazards, limits, sharp edges.

Inputs:

<SEED_CONTEXT>
{seed_context}
</SEED_CONTEXT>
"""


SYNC_AGENT_PROMPT_TEMPLATE = """You are syncing the local git branch to the remote to prepare for a GitHub PR.

Repository: {repo_root}
Branch: {branch}
Context: {issue_hint}

Rules (safety):
- Do NOT discard changes. Do NOT run destructive commands like `git reset --hard`, `git clean -fdx`, or delete files indiscriminately.
- Do NOT force-push.
- Prefer minimal, safe changes that preserve intent.

Tasks:
1) If there is a Makefile or standard tooling, run formatting/lint/tests best-effort. Prefer (in this order) `make fmt`, `make format`, `make lint`, `make test` when targets exist.
2) Check `git status`. If there are unstaged/uncommitted changes and committing is appropriate, stage and commit them.
   - Use a descriptive commit message based on the diff; include the issue number if available.
3) Push the current branch to `origin`.
   - Ensure upstream is set (e.g., `git push -u origin {branch}`).
4) If push is rejected (non-fast-forward/remote updated), do a safe `git pull --rebase`.
   - If there are rebase conflicts, resolve them by editing files to incorporate both sides correctly.
   - Continue the rebase (`git rebase --continue`) until it completes.
   - Re-run formatting if needed after conflict resolution.
   - Retry push.
5) Do not stop until the branch is successfully pushed.

When finished, print a short summary of what you did.
"""


def build_sync_agent_prompt(
    *, repo_root: str, branch: str, issue_num: Optional[int]
) -> str:
    issue_hint = f"issue #{issue_num}" if issue_num else "the linked issue (if any)"
    return SYNC_AGENT_PROMPT_TEMPLATE.format(
        repo_root=repo_root, branch=branch, issue_hint=issue_hint
    )


GITHUB_ISSUE_TO_SPEC_PROMPT_TEMPLATE = """Create or update SPEC to address this GitHub issue.

Issue: #{issue_num} {issue_title}
URL: {issue_url}

Issue body:
{issue_body}

Write a clear SPEC with goals, non-goals, architecture notes, and actionable implementation steps.
"""


def build_github_issue_to_spec_prompt(
    *, issue_num: int, issue_title: str, issue_url: str, issue_body: str
) -> str:
    return GITHUB_ISSUE_TO_SPEC_PROMPT_TEMPLATE.format(
        issue_num=int(issue_num),
        issue_title=str(issue_title or ""),
        issue_url=str(issue_url or ""),
        issue_body=str(issue_body or "").strip(),
    )
