import re
import subprocess
from pathlib import Path
from typing import Dict, Optional

from .utils import atomic_write
from .engine import Engine


class SpecIngestError(Exception):
    """Raised when ingesting a SPEC fails."""


SPEC_INGEST_PROMPT = """You are Codex preparing work docs from a SPEC for an autonomous agent.

Inputs:
<SPEC>
{spec}
</SPEC>

<EXISTING_TODO>
{todo}
</EXISTING_TODO>

<EXISTING_PROGRESS>
{progress}
</EXISTING_PROGRESS>

<EXISTING_OPINIONS>
{opinions}
</EXISTING_OPINIONS>

Tasks:
1) Generate an ordered TODO checklist of high-level tasks derived from the SPEC (use - [ ] bullets). Each TODO item should be a multi-hour long task. You should also think about how to leverage in-context learning that the agents will have. Meaning that related items should be in one TODO so that the agent only has to learn about them once, instead of potentially multiple agents needing to relearn the same problem space.
2) Generate PROGRESS that preserves meaningful existing history and notes any inferred status from the SPEC.
3) Generate OPINIONS by merging existing constraints with SPEC requirements/preferences; keep concise and non-duplicative.

Output strictly in these sections:
<TODO>...</TODO>
<PROGRESS>...</PROGRESS>
<OPINIONS>...</OPINIONS>
"""


def _extract_section(text: str, tag: str) -> Optional[str]:
    pattern = re.compile(rf"<{tag}>\s*(.*?)\s*</{tag}>", re.DOTALL | re.IGNORECASE)
    match = pattern.search(text)
    if not match:
        return None
    return match.group(1).strip()


def build_spec_ingest_prompt(spec: str, todo: str, progress: str, opinions: str) -> str:
    return SPEC_INGEST_PROMPT.format(
        spec=spec.strip(),
        todo=todo.strip(),
        progress=progress.strip(),
        opinions=opinions.strip(),
    )


def parse_spec_ingest_output(text: str) -> Dict[str, str]:
    todo = _extract_section(text, "TODO")
    progress = _extract_section(text, "PROGRESS")
    opinions = _extract_section(text, "OPINIONS")
    if not todo or not progress or not opinions:
        raise SpecIngestError(
            "Failed to parse ingest output; missing TODO/PROGRESS/OPINIONS sections"
        )
    return {"todo": todo, "progress": progress, "opinions": opinions}


def generate_docs_from_spec(
    engine: Engine, spec_path: Optional[Path] = None
) -> Dict[str, str]:
    path = spec_path or engine.config.doc_path("spec")
    if not path.exists():
        raise SpecIngestError(f"SPEC not found at {path}")
    spec_text = path.read_text(encoding="utf-8")
    if not spec_text.strip():
        raise SpecIngestError(f"SPEC at {path} is empty")

    prompt = build_spec_ingest_prompt(
        spec_text,
        engine.docs.read_doc("todo"),
        engine.docs.read_doc("progress"),
        engine.docs.read_doc("opinions"),
    )
    cmd = [engine.config.codex_binary] + engine.config.codex_args + [prompt]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(engine.repo_root),
        )
    except FileNotFoundError:
        raise SpecIngestError(f"Codex binary not found: {engine.config.codex_binary}")

    if result.returncode != 0:
        stderr = result.stderr.strip() if result.stderr else ""
        stdout_tail = (result.stdout or "").strip()[-400:]
        raise SpecIngestError(
            f"Codex ingest failed (code {result.returncode}). {stderr or stdout_tail}"
        )

    return parse_spec_ingest_output(result.stdout or "")


def ensure_can_overwrite(engine: Engine, force: bool) -> None:
    if force:
        return
    for key in ("todo", "progress", "opinions"):
        existing = engine.docs.read_doc(key).strip()
        if existing:
            raise SpecIngestError(
                "TODO/PROGRESS/OPINIONS already contain content; rerun with --force to overwrite"
            )


def write_ingested_docs(engine: Engine, docs: Dict[str, str], force: bool = False) -> None:
    ensure_can_overwrite(engine, force)
    for key, content in docs.items():
        target = engine.config.doc_path(key)
        text = content if content.endswith("\n") else content + "\n"
        atomic_write(target, text)


def clear_work_docs(engine: Engine) -> Dict[str, str]:
    defaults = {
        "todo": "# TODO\n\n",
        "progress": "# Progress\n\n",
        "opinions": "# Opinions\n\n",
    }
    for key, content in defaults.items():
        atomic_write(engine.config.doc_path(key), content)
    # Read back to reflect actual on-disk content.
    return {k: engine.docs.read_doc(k) for k in defaults.keys()}
