from pathlib import Path
from typing import Optional

from .config import Config
from .docs import DocsManager
from .prompts import DEFAULT_PROMPT_TEMPLATE, FINAL_SUMMARY_PROMPT_TEMPLATE


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
        "summary": _display_path(config.doc_path("summary")),
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
        "{{SUMMARY}}": docs.read_doc("summary"),
        "{{PREV_RUN_OUTPUT}}": prev_section,
        "{{TODO_PATH}}": doc_paths["todo"],
        "{{PROGRESS_PATH}}": doc_paths["progress"],
        "{{OPINIONS_PATH}}": doc_paths["opinions"],
        "{{SPEC_PATH}}": doc_paths["spec"],
        "{{SUMMARY_PATH}}": doc_paths["summary"],
    }
    for marker, value in replacements.items():
        template = template.replace(marker, value)
    return template


def build_final_summary_prompt(
    config: Config, docs: DocsManager, prev_run_output: Optional[str] = None
) -> str:
    """
    Build the final report prompt that produces/updates SUMMARY.md once TODO is complete.

    Note: Unlike build_prompt(), this intentionally does not use the repo's prompt.template
    override. It's a separate, purpose-built job.
    """

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
        "summary": _display_path(config.doc_path("summary")),
    }

    template = FINAL_SUMMARY_PROMPT_TEMPLATE
    # Keep a hook for future expansion (template doesn't currently include it).
    _ = prev_run_output

    replacements = {
        "{{TODO}}": docs.read_doc("todo"),
        "{{PROGRESS}}": docs.read_doc("progress"),
        "{{OPINIONS}}": docs.read_doc("opinions"),
        "{{SPEC}}": docs.read_doc("spec"),
        "{{SUMMARY}}": docs.read_doc("summary"),
        "{{TODO_PATH}}": doc_paths["todo"],
        "{{PROGRESS_PATH}}": doc_paths["progress"],
        "{{OPINIONS_PATH}}": doc_paths["opinions"],
        "{{SPEC_PATH}}": doc_paths["spec"],
        "{{SUMMARY_PATH}}": doc_paths["summary"],
    }
    for marker, value in replacements.items():
        template = template.replace(marker, value)
    return template
