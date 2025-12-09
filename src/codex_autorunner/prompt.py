from pathlib import Path
from typing import Optional

from .config import Config
from .docs import DocsManager

DEFAULT_PROMPT_TEMPLATE = """You are Codex, an autonomous coding assistant operating on a git repository.

You are given three documents:
1) TODO: an ordered checklist of tasks.
2) PROGRESS: a running log of what has been done and how it was validated.
3) OPINIONS: design constraints, architectural preferences, and migration policies.

You must:
- Work through TODO items from top to bottom.
- Prefer fixing issues over just documenting them.
- Keep TODO, PROGRESS, and OPINIONS in sync.
- If you find a single TODO to be too large, you can split it, but clearly delineate each TODO item.
- The TODO is for high-level tasks and goals, it should not be used for small tasks, you should use your built-in todo list for that.
- Leave clear handoff notes (tests run, files touched, expected diffs).

<TODO>
{{TODO}}
</TODO>

<PROGRESS>
{{PROGRESS}}
</PROGRESS>

<OPINIONS>
{{OPINIONS}}
</OPINIONS>

{{PREV_RUN_OUTPUT}}

Instructions:
1) Select the highest priority unchecked TODO item and try to make concrete progress on it.
2) Make actual edits in the repo as needed.
3) Update TODO/PROGRESS/OPINIONS before finishing.
4) Prefer small, safe, self-contained changes with tests where applicable.
5) When you are done for this run, print a concise summary of what changed and what remains.
"""

DEFAULT_CHAT_TEMPLATE = """You are running in a project that uses codex-autorunner.

The user runs long-horizon tasks using a series of Codex agents that reference TODO/PROGRESS/OPINIONS as context stored under .codex-autorunner/. You can make edits to those docs when the user wants to change or modify the current trajectory.

Here is the contents of those documents:
{{DOCS_SECTION}}

<USER_MESSAGE>
{{USER_MESSAGE}}
</USER_MESSAGE>
"""


def build_prompt(
    config: Config, docs: DocsManager, prev_run_output: Optional[str]
) -> str:
    template_path: Path = config.prompt_template if config.prompt_template else None
    if template_path and template_path.exists():
        template = template_path.read_text(encoding="utf-8")
    else:
        template = DEFAULT_PROMPT_TEMPLATE

    prev_section = ""
    if prev_run_output:
        prev_section = "<PREV_RUN_OUTPUT>\n" + prev_run_output + "\n</PREV_RUN_OUTPUT>"

    replacements = {
        "{{TODO}}": docs.read_doc("todo"),
        "{{PROGRESS}}": docs.read_doc("progress"),
        "{{OPINIONS}}": docs.read_doc("opinions"),
        "{{PREV_RUN_OUTPUT}}": prev_section,
    }
    for marker, value in replacements.items():
        template = template.replace(marker, value)
    return template


def build_chat_prompt(
    docs: DocsManager,
    message: str,
    include_todo: bool = True,
    include_progress: bool = True,
    include_opinions: bool = True,
) -> str:
    sections = []
    if include_todo:
        sections.append("<TODO>\\n" + docs.read_doc("todo") + "\\n</TODO>")
    if include_progress:
        sections.append("<PROGRESS>\\n" + docs.read_doc("progress") + "\\n</PROGRESS>")
    if include_opinions:
        sections.append("<OPINIONS>\\n" + docs.read_doc("opinions") + "\\n</OPINIONS>")

    docs_block = "\\n\\n".join(sections) if sections else ""
    prompt = DEFAULT_CHAT_TEMPLATE.replace("{{DOCS_SECTION}}", docs_block)
    prompt = prompt.replace("{{USER_MESSAGE}}", message)
    return prompt
