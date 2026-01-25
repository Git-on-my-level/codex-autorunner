from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Optional

from .utils import is_within

if TYPE_CHECKING:
    from .engine import Engine


TRUNCATION_SUFFIX = "... (truncated)\n"


def _truncate_text(text: str, limit: Optional[int]) -> str:
    if limit is None or limit <= 0 or len(text) <= limit:
        return text
    head = text[: max(0, limit - len(TRUNCATION_SUFFIX))]
    return head.rstrip() + TRUNCATION_SUFFIX


def _safe_read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception as exc:
        return f"(failed to read {path.name}: {exc})"


def _artifact_entries(
    engine: "Engine", run_id: Optional[int], max_doc_chars: Optional[int]
) -> list[tuple[str, str]]:
    if run_id is None:
        return []
    index = engine._load_run_index()
    entry = index.get(str(run_id))
    if not isinstance(entry, dict):
        return []
    artifacts = entry.get("artifacts")
    if not isinstance(artifacts, dict):
        return []
    repo_root = engine.repo_root
    limit = (
        max_doc_chars if isinstance(max_doc_chars, int) and max_doc_chars > 0 else 4000
    )
    limit = max(2000, min(limit, 8000))
    pairs: list[tuple[str, str]] = []
    for label, key in (
        ("Output", "output_path"),
        ("Diff", "diff_path"),
        ("Plan", "plan_path"),
    ):
        raw = artifacts.get(key)
        if not isinstance(raw, str) or not raw:
            continue
        path = Path(raw).expanduser()
        if not is_within(repo_root, path) or not path.exists():
            continue
        content = _truncate_text(_safe_read(path), limit)
        pairs.append((label, content))
    return pairs


def build_spec_progress_review_context(
    engine: "Engine",
    *,
    exit_reason: str,
    last_run_id: Optional[int],
    last_exit_code: Optional[int],
    max_doc_chars: int,
    primary_docs: Iterable[str],
    include_docs: Iterable[str],
    include_last_run_artifacts: bool,
) -> str:
    remaining = (
        max_doc_chars if isinstance(max_doc_chars, int) and max_doc_chars > 0 else None
    )
    parts: list[str] = []

    def add(text: str, *, annotate: bool = False) -> None:
        nonlocal remaining
        if text is None:
            return
        if remaining is None:
            parts.append(text)
            return
        if remaining <= 0:
            return
        if len(text) <= remaining:
            parts.append(text)
            remaining -= len(text)
            return
        if annotate and remaining > len(TRUNCATION_SUFFIX):
            snippet = text[: remaining - len(TRUNCATION_SUFFIX)]
            parts.append(snippet.rstrip() + TRUNCATION_SUFFIX)
        else:
            parts.append(text[:remaining])
        remaining = 0

    def doc_label(name: str) -> str:
        try:
            return engine.config.doc_path(name).relative_to(engine.repo_root).as_posix()
        except Exception:
            return name

    def read_doc(name: str) -> str:
        try:
            return engine.docs.read_doc(name)
        except Exception as exc:
            return f"(failed to read {name}: {exc})"

    add("# Autorunner Review Context\n\n")
    add("## Exit reason\n")
    add(f"- reason: {exit_reason or 'unknown'}\n")
    if last_run_id is not None:
        add(f"- last_run_id: {last_run_id}\n")
    if last_exit_code is not None:
        add(f"- last_exit_code: {last_exit_code}\n")
    add("\n")

    primary_list = [doc for doc in primary_docs if isinstance(doc, str)] or [
        "spec",
        "active_context",
    ]
    primary_set = {doc.lower() for doc in primary_list}

    add("## Primary docs\n")
    for key in primary_list:
        add(f"### {doc_label(key)}\n")
        content = read_doc(key).strip()
        add(f"{content}\n\n" if content else "_No content_\n\n", annotate=True)

    extras_seen = set()
    extra_docs = [doc for doc in include_docs if isinstance(doc, str)]
    if extra_docs:
        add("## Optional docs\n")
        for key in extra_docs:
            normalized = key.lower()
            if normalized in extras_seen or normalized in primary_set:
                continue
            extras_seen.add(normalized)
            add(f"### {doc_label(normalized)}\n")
            content = read_doc(normalized).strip()
            add(f"{content}\n\n" if content else "_No content_\n\n", annotate=True)

    if include_last_run_artifacts:
        if remaining is not None and remaining <= 0:
            return "".join(parts)
        add("## Last run artifacts\n")
        artifacts = _artifact_entries(
            engine,
            last_run_id,
            remaining if remaining is not None else max_doc_chars,
        )
        if not artifacts:
            add("_No artifacts found_\n\n")
        else:
            for label, content in artifacts:
                add(f"### {label}\n")
                add(f"{content}\n\n" if content else "_No content_\n\n", annotate=True)

    return "".join(parts)
