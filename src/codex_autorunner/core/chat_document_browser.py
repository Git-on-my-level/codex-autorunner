from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping, Sequence, TypeVar, cast

from ..contextspace.paths import (
    contextspace_doc_catalog,
    contextspace_doc_path,
    normalize_contextspace_doc_kind,
)
from ..tickets.files import list_ticket_paths, read_ticket_frontmatter, safe_relpath
from ..tickets.lint import parse_ticket_index
from ..tickets.snapshot import list_ticket_snapshots

DocumentBrowserSource = Literal["tickets", "contextspace"]

DOCUMENT_BROWSER_SOURCES: tuple[DocumentBrowserSource, ...] = (
    "tickets",
    "contextspace",
)

T = TypeVar("T")


@dataclass(frozen=True)
class DocumentBrowserItem:
    source: DocumentBrowserSource
    document_id: str
    label: str
    description: str


@dataclass(frozen=True)
class DocumentBrowserDocument:
    source: DocumentBrowserSource
    document_id: str
    title: str
    description: str
    rel_path: str
    content: str
    exists: bool


def normalize_document_browser_source(value: str) -> DocumentBrowserSource:
    normalized = str(value or "").strip().lower()
    if normalized not in DOCUMENT_BROWSER_SOURCES:
        raise ValueError("invalid document browser source")
    return cast(DocumentBrowserSource, normalized)


def list_document_browser_items(
    repo_root: Path,
    source: str,
    *,
    query: str = "",
) -> list[DocumentBrowserItem]:
    normalized_source = normalize_document_browser_source(source)
    if normalized_source == "tickets":
        return _list_ticket_items(repo_root, query=query)
    return _list_contextspace_items(repo_root, query=query)


def read_document_browser_document(
    repo_root: Path,
    source: str,
    document_id: str,
) -> DocumentBrowserDocument | None:
    normalized_source = normalize_document_browser_source(source)
    if normalized_source == "tickets":
        return _read_ticket_document(repo_root, document_id)
    return _read_contextspace_document(repo_root, document_id)


def browser_page_count(total_items: int, page_size: int) -> int:
    if page_size <= 0:
        raise ValueError("page_size must be positive")
    if total_items <= 0:
        return 1
    return ((total_items - 1) // page_size) + 1


def browser_page_slice(
    items: Sequence[T],
    *,
    page: int,
    page_size: int,
) -> tuple[int, int, list[T]]:
    total_pages = browser_page_count(len(items), page_size)
    normalized_page = max(0, min(page, total_pages - 1))
    start = normalized_page * page_size
    end = start + page_size
    return normalized_page, total_pages, list(items[start:end])


def _list_ticket_items(repo_root: Path, *, query: str) -> list[DocumentBrowserItem]:
    items: list[DocumentBrowserItem] = []
    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    paths = list_ticket_paths(ticket_dir)
    index_counts = _ticket_index_counts(paths)
    path_by_name = {path.name: path for path in paths}
    for snap in list_ticket_snapshots(repo_root):
        path = path_by_name.get(snap.filename)
        if path is None:
            continue
        description = f"{'done' if snap.is_done else 'open'} - {snap.rel_path}"
        item = DocumentBrowserItem(
            source="tickets",
            document_id=_ticket_document_id(path, repo_root, index_counts=index_counts),
            label=snap.label,
            description=description,
        )
        if _matches_query(
            query,
            (
                item.document_id,
                item.label,
                item.description,
                snap.rel_path,
                snap.title,
            ),
        ):
            items.append(item)
    return items


def _list_contextspace_items(
    repo_root: Path, *, query: str
) -> list[DocumentBrowserItem]:
    items: list[DocumentBrowserItem] = []
    for entry in contextspace_doc_catalog():
        path = contextspace_doc_path(repo_root, entry.kind)
        description = entry.description
        if not path.exists():
            description = f"{description} Missing file."
        item = DocumentBrowserItem(
            source="contextspace",
            document_id=entry.kind,
            label=entry.label,
            description=description,
        )
        if _matches_query(
            query,
            (
                entry.kind,
                entry.label,
                entry.description,
                path.relative_to(repo_root).as_posix(),
            ),
        ):
            items.append(item)
    return items


def _read_ticket_document(
    repo_root: Path,
    document_id: str,
) -> DocumentBrowserDocument | None:
    target_id = str(document_id or "").strip()
    if not target_id:
        return None
    ticket_dir = repo_root / ".codex-autorunner" / "tickets"
    paths = list_ticket_paths(ticket_dir)
    index_counts = _ticket_index_counts(paths)
    for path in paths:
        if _ticket_document_id(path, repo_root, index_counts=index_counts) != target_id:
            continue
        rel_path = safe_relpath(path, repo_root)
        frontmatter, errors = read_ticket_frontmatter(path)
        title = frontmatter.title.strip() if frontmatter and frontmatter.title else ""
        is_done = bool(frontmatter and not errors and frontmatter.done)
        content = path.read_text(encoding="utf-8") if path.exists() else ""
        display_title = f"{path.name}{' - ' + title if title else ''}"
        description = f"{'done' if is_done else 'open'} - {rel_path}"
        return DocumentBrowserDocument(
            source="tickets",
            document_id=target_id,
            title=display_title,
            description=description,
            rel_path=rel_path,
            content=content,
            exists=path.exists(),
        )
    return None


def _read_contextspace_document(
    repo_root: Path,
    document_id: str,
) -> DocumentBrowserDocument | None:
    try:
        kind = normalize_contextspace_doc_kind(document_id)
    except ValueError:
        return None
    entry = next(item for item in contextspace_doc_catalog() if item.kind == kind)
    path = contextspace_doc_path(repo_root, kind)
    rel_path = path.relative_to(repo_root).as_posix()
    exists = path.exists()
    content = path.read_text(encoding="utf-8") if exists else ""
    description = entry.description if exists else f"{entry.description} Missing file."
    return DocumentBrowserDocument(
        source="contextspace",
        document_id=kind,
        title=entry.label,
        description=description,
        rel_path=rel_path,
        content=content,
        exists=exists,
    )


def _ticket_index_counts(paths: Sequence[Path]) -> Mapping[int, int]:
    counts: Counter[int] = Counter()
    for path in paths:
        idx = parse_ticket_index(path.name)
        if idx is not None:
            counts[idx] += 1
    return counts


def _ticket_document_id(
    path: Path,
    repo_root: Path,
    *,
    index_counts: Mapping[int, int],
) -> str:
    """Stable id for ticket browser selection.

    Uses the numeric index when it uniquely identifies one file under the ticket
    directory; when multiple files share the same parsed index (for example
    ``TICKET-001.md`` and ``TICKET-001-draft.md``), appends a short path hash so
    each file maps to a distinct id.
    """

    index = parse_ticket_index(path.name)
    if index is None:
        return path.name
    if index_counts.get(index, 0) <= 1:
        return str(index)
    rel = safe_relpath(path, repo_root)
    digest = hashlib.sha256(rel.encode("utf-8")).hexdigest()[:10]
    return f"{index}-{digest}"


def _matches_query(query: str, fields: Sequence[str]) -> bool:
    normalized_query = " ".join(str(query or "").lower().split())
    if not normalized_query:
        return True
    tokens = [token for token in normalized_query.split(" ") if token]
    if not tokens:
        return True
    normalized_fields = [" ".join(str(field or "").lower().split()) for field in fields]
    combined = " ".join(field for field in normalized_fields if field)
    return all(token in combined for token in tokens)


__all__ = [
    "DOCUMENT_BROWSER_SOURCES",
    "DocumentBrowserDocument",
    "DocumentBrowserItem",
    "DocumentBrowserSource",
    "browser_page_count",
    "browser_page_slice",
    "list_document_browser_items",
    "normalize_document_browser_source",
    "read_document_browser_document",
]
