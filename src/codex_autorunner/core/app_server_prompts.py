from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from .config import AppServerAutorunnerPromptConfig, Config

TRUNCATION_MARKER = "...[truncated]"


AUTORUNNER_APP_SERVER_TEMPLATE = """You are an autonomous coding assistant operating on a git repository.

Contextspace docs (optional; read from disk when useful):
- Active context: {active_context_path}
- Decisions: {decisions_path}
- Spec: {spec_path}

Tickets:
- The authoritative work items are ticket files under `.codex-autorunner/tickets/`.
- Pick the next not-done ticket, implement it, and update the ticket file (`done: true`) when complete.

Instructions:
- This run is non-interactive. Do not ask the user questions. If unsure, make reasonable assumptions and proceed.
- Prefer small, safe diffs and keep work focused on the current ticket.
- You may create new tickets only when needed to break down the current work.

User request:
{message}

{workspace_spec_block}
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


def build_autorunner_prompt(
    config: Config,
    *,
    message: str,
    prev_run_summary: Optional[str] = None,
) -> str:
    prompt_cfg: AppServerAutorunnerPromptConfig = config.app_server.prompts.autorunner
    doc_paths = {
        "active_context": _display_path(config.root, config.doc_path("active_context")),
        "decisions": _display_path(config.root, config.doc_path("decisions")),
        "spec": _display_path(config.root, config.doc_path("spec")),
    }

    message_text = truncate_text(message, prompt_cfg.message_max_chars)
    spec_excerpt = truncate_text(
        (
            config.doc_path("spec").read_text(encoding="utf-8")
            if config.doc_path("spec").exists()
            else ""
        ),
        prompt_cfg.todo_excerpt_max_chars,
    )
    prev_run_text = truncate_text(prev_run_summary or "", prompt_cfg.prev_run_max_chars)

    sections = {
        "message": message_text,
        "workspace_spec": spec_excerpt,
        "prev_run": prev_run_text,
    }

    def render() -> str:
        return AUTORUNNER_APP_SERVER_TEMPLATE.format(
            active_context_path=doc_paths["active_context"],
            decisions_path=doc_paths["decisions"],
            spec_path=doc_paths["spec"],
            message=sections["message"],
            workspace_spec_block=_optional_block(
                "WORKSPACE_SPEC", sections["workspace_spec"]
            ),
            prev_run_block=_optional_block("PREV_RUN_SUMMARY", sections["prev_run"]),
        )

    return _shrink_prompt(
        max_chars=prompt_cfg.max_chars,
        render=render,
        sections=sections,
        order=["prev_run", "workspace_spec", "message"],
    )


APP_SERVER_PROMPT_BUILDERS = {
    "autorunner": build_autorunner_prompt,
}


__all__ = [
    "AUTORUNNER_APP_SERVER_TEMPLATE",
    "APP_SERVER_PROMPT_BUILDERS",
    "TRUNCATION_MARKER",
    "build_autorunner_prompt",
    "truncate_text",
]
