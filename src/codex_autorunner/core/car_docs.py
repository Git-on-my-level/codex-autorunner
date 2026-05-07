from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Iterable, Optional

from ..bootstrap import ensure_pma_docs, pma_doc_path
from .config import ConfigError, load_hub_config


@dataclass(frozen=True)
class CarDoc:
    doc_id: str
    title: str
    scope: str
    summary: str
    path: Optional[Path]
    content: str

    def to_payload(self) -> dict[str, object]:
        return {
            "id": self.doc_id,
            "title": self.title,
            "scope": self.scope,
            "summary": self.summary,
            "path": str(self.path) if self.path is not None else None,
        }


@dataclass(frozen=True)
class _ShippedDoc:
    doc_id: str
    title: str
    filename: str
    summary: str


_SHIPPED_DOCS: tuple[_ShippedDoc, ...] = (
    _ShippedDoc(
        "car/overview",
        "CAR Overview",
        "overview.md",
        "Hub, repo, worktree, and PMA roles.",
    ),
    _ShippedDoc(
        "car/path-model",
        "CAR Path Model",
        "path-model.md",
        "Hub root, repo root, worktree root, and runtime cwd.",
    ),
    _ShippedDoc(
        "car/pma", "PMA", "pma.md", "PMA role, hub memory, and delegation defaults."
    ),
    _ShippedDoc(
        "car/managed-threads",
        "Managed Threads",
        "managed-threads.md",
        "Managed thread lifecycle and common commands.",
    ),
    _ShippedDoc(
        "car/ticket-flow",
        "Ticket Flow",
        "ticket-flow.md",
        "Ordered ticket execution and planning constraints.",
    ),
    _ShippedDoc(
        "car/contextspace",
        "Contextspace",
        "contextspace.md",
        "Repo-local durable context docs.",
    ),
    _ShippedDoc(
        "car/destinations",
        "Destinations",
        "destinations.md",
        "Local and Docker execution destinations.",
    ),
    _ShippedDoc(
        "car/worktrees",
        "Worktrees",
        "worktrees.md",
        "Hub-managed worktree expectations.",
    ),
    _ShippedDoc(
        "car/templates-apps",
        "Templates and Apps",
        "templates-apps.md",
        "Ticket templates and CAR app workflows.",
    ),
    _ShippedDoc(
        "car/troubleshooting",
        "Troubleshooting",
        "troubleshooting.md",
        "First diagnostic commands and path checks.",
    ),
    _ShippedDoc("car/glossary", "Glossary", "glossary.md", "Core CAR vocabulary."),
)

_PMA_DOCS: tuple[tuple[str, str, str, str], ...] = (
    ("pma/prompt", "PMA Prompt", "prompt.md", "Hub-local PMA base prompt."),
    (
        "pma/about",
        "PMA Operations Guide",
        "ABOUT_CAR.md",
        "Hub-local PMA operational guide.",
    ),
    ("pma/agents", "PMA AGENTS", "AGENTS.md", "Durable hub-level PMA guidance."),
    (
        "pma/active-context",
        "PMA Active Context",
        "active_context.md",
        "Short-lived PMA working context.",
    ),
    (
        "pma/context-log",
        "PMA Context Log",
        "context_log.md",
        "Append-only PMA context snapshots.",
    ),
)

_REPO_DOCS: tuple[tuple[str, str, str, str], ...] = (
    (
        "repo/about",
        "Repo ABOUT_CAR",
        ".codex-autorunner/ABOUT_CAR.md",
        "Repo-local CAR quick briefing.",
    ),
    (
        "repo/ticket-flow",
        "Repo Ticket Flow Quickstart",
        ".codex-autorunner/TICKET_FLOW_QUICKSTART.md",
        "Repo-local ticket flow commands.",
    ),
    (
        "repo/destination",
        "Repo Destination Quickstart",
        ".codex-autorunner/DESTINATION_QUICKSTART.md",
        "Repo-local destination setup.",
    ),
    (
        "repo/context-active",
        "Repo Active Context",
        ".codex-autorunner/contextspace/active_context.md",
        "Repo active working context.",
    ),
    (
        "repo/context-spec",
        "Repo Spec",
        ".codex-autorunner/contextspace/spec.md",
        "Repo durable spec context.",
    ),
    (
        "repo/context-decisions",
        "Repo Decisions",
        ".codex-autorunner/contextspace/decisions.md",
        "Repo durable decision log.",
    ),
    (
        "repo/tickets-agents",
        "Repo Ticket AGENTS",
        ".codex-autorunner/tickets/AGENTS.md",
        "Ticket folder guidance.",
    ),
)


def _read_shipped_doc(spec: _ShippedDoc) -> CarDoc:
    resource = resources.files("codex_autorunner.docs").joinpath(spec.filename)
    content = resource.read_text(encoding="utf-8")
    path: Optional[Path]
    try:
        path = Path(str(resource))
    except TypeError:
        path = None
    return CarDoc(
        doc_id=spec.doc_id,
        title=spec.title,
        scope="car",
        summary=spec.summary,
        path=path,
        content=content,
    )


def _read_file_doc(
    *,
    doc_id: str,
    title: str,
    scope: str,
    summary: str,
    path: Path,
) -> Optional[CarDoc]:
    if not path.exists() or not path.is_file():
        return None
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return CarDoc(
        doc_id=doc_id,
        title=title,
        scope=scope,
        summary=summary,
        path=path.resolve(),
        content=content,
    )


def _resolve_hub_root(hub_root: Optional[Path]) -> Optional[Path]:
    if hub_root is None:
        return None
    try:
        return load_hub_config(hub_root).root
    except ConfigError:
        return hub_root.expanduser().resolve()


def collect_car_docs(
    *,
    hub_root: Optional[Path] = None,
    repo_root: Optional[Path] = None,
) -> list[CarDoc]:
    docs: list[CarDoc] = [_read_shipped_doc(spec) for spec in _SHIPPED_DOCS]

    resolved_hub = _resolve_hub_root(hub_root)
    if resolved_hub is not None:
        try:
            ensure_pma_docs(resolved_hub)
        except OSError:
            pass
        for doc_id, title, filename, summary in _PMA_DOCS:
            doc = _read_file_doc(
                doc_id=doc_id,
                title=title,
                scope="pma",
                summary=summary,
                path=pma_doc_path(resolved_hub, filename),
            )
            if doc is not None:
                docs.append(doc)

    if repo_root is not None:
        resolved_repo = repo_root.expanduser().resolve()
        for doc_id, title, rel_path, summary in _REPO_DOCS:
            doc = _read_file_doc(
                doc_id=doc_id,
                title=title,
                scope="repo",
                summary=summary,
                path=resolved_repo / rel_path,
            )
            if doc is not None:
                docs.append(doc)

    return docs


def find_doc(
    doc_id: str,
    *,
    hub_root: Optional[Path] = None,
    repo_root: Optional[Path] = None,
) -> Optional[CarDoc]:
    normalized = doc_id.strip().lower()
    for doc in collect_car_docs(hub_root=hub_root, repo_root=repo_root):
        if doc.doc_id.lower() == normalized:
            return doc
    return None


def search_car_docs(
    query: str,
    *,
    hub_root: Optional[Path] = None,
    repo_root: Optional[Path] = None,
) -> list[CarDoc]:
    terms = [term.lower() for term in query.split() if term.strip()]
    if not terms:
        return []
    matches: list[CarDoc] = []
    for doc in collect_car_docs(hub_root=hub_root, repo_root=repo_root):
        haystack = "\n".join(
            [doc.doc_id, doc.title, doc.scope, doc.summary, doc.content]
        ).lower()
        if all(term in haystack for term in terms):
            matches.append(doc)
    return matches


def format_docs_json(docs: Iterable[CarDoc]) -> str:
    return json.dumps([doc.to_payload() for doc in docs], indent=2)
