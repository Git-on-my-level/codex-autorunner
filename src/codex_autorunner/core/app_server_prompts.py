from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from .config import (
    AppServerAutorunnerPromptConfig,
    AppServerDocChatPromptConfig,
    AppServerSpecIngestPromptConfig,
    Config,
)

TRUNCATION_MARKER = "...[truncated]"


DOC_CHAT_APP_SERVER_TEMPLATE = """You are an autonomous coding assistant helping maintain the work docs for this repository.

Instructions:
- Use the base doc content below. Drafts (if present) are the authoritative base.
- You may inspect the repo and update the work docs listed when needed.
- If you update docs, edit the files directly. If no changes are needed, do not edit files.
- Respond with a short summary of what you did or found.

Work docs (paths):
- TODO: {todo_path}
- PROGRESS: {progress_path}
- OPINIONS: {opinions_path}
- SPEC: {spec_path}
- SUMMARY: {summary_path}

{user_viewing_block}

User request:
{message}

{docs_context_block}
{recent_summary_block}
"""


SPEC_INGEST_APP_SERVER_TEMPLATE = """You are preparing work docs (TODO/PROGRESS/OPINIONS) from the SPEC.

SPEC path: {spec_path}
TODO path: {todo_path}
PROGRESS path: {progress_path}
OPINIONS path: {opinions_path}

Instructions:
- Read the SPEC and existing docs from disk.
- Edit the TODO, PROGRESS, and OPINIONS files directly to reflect the SPEC.
- The TODO must be a Markdown checklist. Every task MUST be a checkbox line:
  - Use `- [ ] <task>` for open items and `- [x] <task>` for completed items.
  - Do NOT use plain bullets like `- task` or paragraphs for tasks.
- Do NOT output a patch block. Just edit the files.
- Output a short summary prefixed with "Agent: " explaining what you did.

User request:
{message}

{spec_excerpt_block}
"""


SNAPSHOT_APP_SERVER_TEMPLATE = """You are generating a compact Markdown repo snapshot meant to be pasted into another LLM chat.

Snapshot path: {snapshot_path}

Instructions:
- Analyze the provided context and the repository.
- Write the snapshot content directly to the snapshot path.
- Keep the file concise and high-signal.

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
{seed_context}

{changes_block}
{previous_snapshot_block}
"""


AUTORUNNER_APP_SERVER_TEMPLATE = """You are an autonomous coding assistant operating on a git repository.

Work docs (read from disk as needed):
- TODO: {todo_path}
- PROGRESS: {progress_path}
- OPINIONS: {opinions_path}
- SPEC: {spec_path}
- SUMMARY: {summary_path}

Instructions:
- Work through TODO items from top to bottom.
- Prefer fixing issues over documenting them.
- Keep TODO/PROGRESS/OPINIONS/SPEC/SUMMARY in sync.
- Make actual edits in the repo as needed.

User request:
{message}

{todo_excerpt_block}
{prev_run_block}
"""


def _display_path(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def truncate_text(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    normalized = text or ""
    if len(normalized) <= max_chars:
        return normalized
    if max_chars <= len(TRUNCATION_MARKER):
        return TRUNCATION_MARKER[:max_chars]
    return normalized[: max_chars - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


def _optional_block(tag: str, content: str) -> str:
    if not content:
        return ""
    return f"<{tag}>\n{content}\n</{tag}>"


def _shrink_prompt(
    *,
    max_chars: int,
    render: Callable[[], str],
    sections: dict[str, str],
    order: list[str],
) -> str:
    prompt = render()
    if len(prompt) <= max_chars:
        return prompt
    for key in order:
        if len(prompt) <= max_chars:
            break
        value = sections.get(key, "")
        if not value:
            continue
        overflow = len(prompt) - max_chars
        new_limit = max(len(value) - overflow, 0)
        sections[key] = truncate_text(value, new_limit)
        prompt = render()
    if len(prompt) > max_chars:
        prompt = truncate_text(prompt, max_chars)
    return prompt


def build_doc_chat_prompt(
    config: Config,
    *,
    message: str,
    recent_summary: Optional[str],
    docs: dict[str, dict[str, str]],
    context_doc: Optional[str] = None,
) -> str:
    prompt_cfg: AppServerDocChatPromptConfig = config.app_server.prompts.doc_chat
    doc_paths = {
        "todo": _display_path(config.root, config.doc_path("todo")),
        "progress": _display_path(config.root, config.doc_path("progress")),
        "opinions": _display_path(config.root, config.doc_path("opinions")),
        "spec": _display_path(config.root, config.doc_path("spec")),
        "summary": _display_path(config.root, config.doc_path("summary")),
    }
    message_text = truncate_text(message, prompt_cfg.message_max_chars)
    doc_blocks = []
    for key, path in doc_paths.items():
        payload = docs.get(key, {})
        source = payload.get("source") or "disk"
        content = truncate_text(
            str(payload.get("content") or ""), prompt_cfg.target_excerpt_max_chars
        )
        if not content.strip():
            content = "(empty)"
        label = f"{key.upper()} [{path}] ({source.upper()})"
        doc_blocks.append(f"{label}\n{content}")
    docs_context = "\n\n".join(doc_blocks)
    recent_text = truncate_text(
        recent_summary or "", prompt_cfg.recent_summary_max_chars
    )
    user_viewing = ""
    if context_doc:
        user_viewing = f"The user is currently looking at {context_doc.upper()}."

    sections = {
        "message": message_text,
        "docs_context": docs_context,
        "recent_summary": recent_text,
        "user_viewing": user_viewing,
    }

    def render() -> str:
        return DOC_CHAT_APP_SERVER_TEMPLATE.format(
            todo_path=doc_paths["todo"],
            progress_path=doc_paths["progress"],
            opinions_path=doc_paths["opinions"],
            spec_path=doc_paths["spec"],
            summary_path=doc_paths["summary"],
            message=sections["message"],
            user_viewing_block=_optional_block(
                "USER_VIEWING", sections["user_viewing"]
            ),
            docs_context_block=_optional_block("DOC_BASES", sections["docs_context"]),
            recent_summary_block=_optional_block(
                "RECENT_RUN_SUMMARY", sections["recent_summary"]
            ),
        )

    return _shrink_prompt(
        max_chars=prompt_cfg.max_chars,
        render=render,
        sections=sections,
        order=["recent_summary", "docs_context", "message"],
    )


def build_spec_ingest_prompt(
    config: Config,
    *,
    message: str,
    spec_path: Optional[Path] = None,
) -> str:
    prompt_cfg: AppServerSpecIngestPromptConfig = config.app_server.prompts.spec_ingest
    doc_paths = {
        "todo": _display_path(config.root, config.doc_path("todo")),
        "progress": _display_path(config.root, config.doc_path("progress")),
        "opinions": _display_path(config.root, config.doc_path("opinions")),
    }
    spec_target = spec_path or config.doc_path("spec")
    spec_path_str = _display_path(config.root, spec_target)
    message_text = truncate_text(message, prompt_cfg.message_max_chars)
    spec_excerpt = truncate_text(
        spec_target.read_text(encoding="utf-8"),
        prompt_cfg.spec_excerpt_max_chars,
    )

    sections = {
        "message": message_text,
        "spec_excerpt": spec_excerpt,
    }

    def render() -> str:
        return SPEC_INGEST_APP_SERVER_TEMPLATE.format(
            spec_path=spec_path_str,
            todo_path=doc_paths["todo"],
            progress_path=doc_paths["progress"],
            opinions_path=doc_paths["opinions"],
            message=sections["message"],
            spec_excerpt_block=_optional_block(
                "SPEC_EXCERPT", sections["spec_excerpt"]
            ),
        )

    return _shrink_prompt(
        max_chars=prompt_cfg.max_chars,
        render=render,
        sections=sections,
        order=["spec_excerpt", "message"],
    )


def build_app_server_snapshot_prompt(
    config: Config,
    *,
    seed_context: str,
    previous_snapshot: Optional[str] = None,
    changes: Optional[str] = None,
) -> str:
    snapshot_path = config.doc_path("snapshot")
    previous_block = ""
    if previous_snapshot:
        previous_block = (
            f"<PREVIOUS_SNAPSHOT>\n{previous_snapshot.strip()}\n</PREVIOUS_SNAPSHOT>"
        )
    changes_block = ""
    if changes:
        changes_block = f"<CHANGES_SINCE_LAST_SNAPSHOT>\n{changes.strip()}\n</CHANGES_SINCE_LAST_SNAPSHOT>"

    return SNAPSHOT_APP_SERVER_TEMPLATE.format(
        snapshot_path=snapshot_path,
        seed_context=seed_context,
        changes_block=changes_block,
        previous_snapshot_block=previous_block,
    )


def build_autorunner_prompt(
    config: Config,
    *,
    message: str,
    prev_run_summary: Optional[str] = None,
) -> str:
    prompt_cfg: AppServerAutorunnerPromptConfig = config.app_server.prompts.autorunner
    doc_paths = {
        "todo": _display_path(config.root, config.doc_path("todo")),
        "progress": _display_path(config.root, config.doc_path("progress")),
        "opinions": _display_path(config.root, config.doc_path("opinions")),
        "spec": _display_path(config.root, config.doc_path("spec")),
        "summary": _display_path(config.root, config.doc_path("summary")),
    }
    message_text = truncate_text(message, prompt_cfg.message_max_chars)
    todo_excerpt = truncate_text(
        config.doc_path("todo").read_text(encoding="utf-8"),
        prompt_cfg.todo_excerpt_max_chars,
    )
    prev_run_text = truncate_text(prev_run_summary or "", prompt_cfg.prev_run_max_chars)

    sections = {
        "message": message_text,
        "todo_excerpt": todo_excerpt,
        "prev_run": prev_run_text,
    }

    def render() -> str:
        return AUTORUNNER_APP_SERVER_TEMPLATE.format(
            todo_path=doc_paths["todo"],
            progress_path=doc_paths["progress"],
            opinions_path=doc_paths["opinions"],
            spec_path=doc_paths["spec"],
            summary_path=doc_paths["summary"],
            message=sections["message"],
            todo_excerpt_block=_optional_block(
                "TODO_EXCERPT", sections["todo_excerpt"]
            ),
            prev_run_block=_optional_block("PREV_RUN_SUMMARY", sections["prev_run"]),
        )

    return _shrink_prompt(
        max_chars=prompt_cfg.max_chars,
        render=render,
        sections=sections,
        order=["prev_run", "todo_excerpt", "message"],
    )


APP_SERVER_PROMPT_BUILDERS = {
    "doc_chat": build_doc_chat_prompt,
    "spec_ingest": build_spec_ingest_prompt,
    "autorunner": build_autorunner_prompt,
}


__all__ = [
    "AUTORUNNER_APP_SERVER_TEMPLATE",
    "APP_SERVER_PROMPT_BUILDERS",
    "DOC_CHAT_APP_SERVER_TEMPLATE",
    "SPEC_INGEST_APP_SERVER_TEMPLATE",
    "SNAPSHOT_APP_SERVER_TEMPLATE",
    "TRUNCATION_MARKER",
    "build_autorunner_prompt",
    "build_doc_chat_prompt",
    "build_spec_ingest_prompt",
    "build_app_server_snapshot_prompt",
    "truncate_text",
]
