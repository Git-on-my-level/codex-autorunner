import re
from pathlib import Path
from typing import Mapping, Optional

from .config import Config
from .docs import DocsManager
from .prompts import FINAL_SUMMARY_PROMPT_TEMPLATE


def _display_path(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def build_doc_paths(config: Config) -> Mapping[str, str]:
    def _safe_path(*keys: str) -> str:
        for key in keys:
            try:
                return _display_path(config.root, config.doc_path(key))
            except KeyError:
                continue
        return ""

    return {
        "todo": _safe_path("todo", "active_context"),
        "progress": _safe_path("progress", "decisions"),
        "opinions": _safe_path("opinions"),
        "spec": _safe_path("spec"),
        "summary": _safe_path("summary"),
    }


def build_prompt_text(
    *,
    template: str,
    docs: Mapping[str, str],
    doc_paths: Mapping[str, str],
    prev_run_output: Optional[str],
) -> str:
    prev_section = ""
    if prev_run_output:
        prev_section = "<PREV_RUN_OUTPUT>\n" + prev_run_output + "\n</PREV_RUN_OUTPUT>"

    replacements = {
        "{{TODO}}": docs.get("todo", ""),
        "{{PROGRESS}}": docs.get("progress", ""),
        "{{OPINIONS}}": docs.get("opinions", ""),
        "{{SPEC}}": docs.get("spec", ""),
        "{{SUMMARY}}": docs.get("summary", ""),
        "{{PREV_RUN_OUTPUT}}": prev_section,
        "{{TODO_PATH}}": doc_paths.get("todo", ""),
        "{{PROGRESS_PATH}}": doc_paths.get("progress", ""),
        "{{OPINIONS_PATH}}": doc_paths.get("opinions", ""),
        "{{SPEC_PATH}}": doc_paths.get("spec", ""),
        "{{SUMMARY_PATH}}": doc_paths.get("summary", ""),
    }
    pattern = re.compile("|".join(re.escape(key) for key in replacements))
    return pattern.sub(lambda match: replacements[match.group(0)], template)


def build_final_summary_prompt(
    config: Config, docs: DocsManager, prev_run_output: Optional[str] = None
) -> str:
    """
    Build the final report prompt that produces/updates SUMMARY.md once TODO is complete.

    Note: Unlike build_prompt(), this intentionally does not use the repo's prompt.template
    override. It's a separate, purpose-built job.
    """

    doc_paths = build_doc_paths(config)
    doc_contents = {
        "todo": docs.read_doc("todo") or docs.read_doc("active_context"),
        "progress": docs.read_doc("progress") or docs.read_doc("decisions"),
        "opinions": docs.read_doc("opinions"),
        "spec": docs.read_doc("spec"),
        "summary": docs.read_doc("summary"),
    }
    # Keep a hook for future expansion (template doesn't currently include it).
    _ = prev_run_output
    return build_prompt_text(
        template=FINAL_SUMMARY_PROMPT_TEMPLATE,
        docs=doc_contents,
        doc_paths=doc_paths,
        prev_run_output=None,
    )
