from pathlib import Path
from typing import Optional

from .config import Config
from .docs import DocsManager

DEFAULT_PROMPT_TEMPLATE = """You are Codex, an autonomous coding assistant operating on a git repository.

You are given four documents:
1) TODO: an ordered checklist of tasks.
2) PROGRESS: a running log of what has been done and how it was validated.
3) OPINIONS: design constraints, architectural preferences, and migration policies.
4) SPEC: source-of-truth requirements and scope for this project/feature.
Work docs live under the hidden .codex-autorunner directory. Edit these files directly; do not create new copies elsewhere:
- TODO: {{TODO_PATH}}
- PROGRESS: {{PROGRESS_PATH}}
- OPINIONS: {{OPINIONS_PATH}}
- SPEC: {{SPEC_PATH}}

You must:
- Work through TODO items from top to bottom. 
- Be proactive and in-context learning efficient. When you are done with one task, think about if what you learned will help you on the next task. If so, work on the next TODO item as well. Only stop if the next TODO item is very large or completely unrelated to your current context.
- Prefer fixing issues over just documenting them.
- Keep TODO, PROGRESS, OPINIONS, and SPEC in sync.
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

<SPEC>
{{SPEC}}
</SPEC>

{{PREV_RUN_OUTPUT}}

Instructions:
1) Select the highest priority unchecked TODO item and try to make concrete progress on it.
2) Make actual edits in the repo as needed.
3) Update TODO/PROGRESS/OPINIONS/SPEC before finishing.
4) Prefer small, safe, self-contained changes with tests where applicable.
5) When you are done for this run, print a concise summary of what changed and what remains.
"""


def build_prompt(
    config: Config, docs: DocsManager, prev_run_output: Optional[str]
) -> str:
    def _display_path(path: Path) -> str:
        try:
            return str(path.relative_to(config.root))
        except ValueError:
            return str(path)

    doc_paths = {
        "todo": _display_path(config.doc_path("todo")),
        "progress": _display_path(config.doc_path("progress")),
        "opinions": _display_path(config.doc_path("opinions")),
        "spec": _display_path(config.doc_path("spec")),
    }

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
        "{{SPEC}}": docs.read_doc("spec"),
        "{{PREV_RUN_OUTPUT}}": prev_section,
        "{{TODO_PATH}}": doc_paths["todo"],
        "{{PROGRESS_PATH}}": doc_paths["progress"],
        "{{OPINIONS_PATH}}": doc_paths["opinions"],
        "{{SPEC_PATH}}": doc_paths["spec"],
    }
    for marker, value in replacements.items():
        template = template.replace(marker, value)
    return template
