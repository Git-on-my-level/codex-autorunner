"""
Shared fixtures for contract tests.

To register a new adapter, add a factory function to the appropriate
``*_FACTORIES`` list below.  The contract suites will automatically
pick it up via ``pytest_generate_tests``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Tuple

import pytest

from codex_autorunner.core.adapters import (
    FilesystemMemoryStore,
    FilesystemScopeResolver,
    FilesystemTicketStore,
)
from codex_autorunner.manifest import Manifest, ManifestRepo


def _make_hub_tree(tmp_path: Path) -> tuple[Path, Manifest]:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    repo_dir = hub_root / "my-repo"
    repo_dir.mkdir()
    wt_dir = hub_root / "my-wt"
    wt_dir.mkdir()
    for d in (repo_dir, wt_dir):
        car_dir = d / ".codex-autorunner"
        car_dir.mkdir()
        (car_dir / "tickets").mkdir()
        (car_dir / "contextspace").mkdir()
    car_hub = hub_root / ".codex-autorunner"
    car_hub.mkdir()
    (car_hub / "contextspace").mkdir()
    manifest = Manifest(
        version=3,
        repos=[
            ManifestRepo(
                id="repo-1",
                path=Path("my-repo"),
                kind="base",
                display_name="My Repo",
            ),
            ManifestRepo(
                id="wt-1",
                path=Path("my-wt"),
                kind="worktree",
                worktree_of="repo-1",
                display_name="My Worktree",
            ),
        ],
    )
    return hub_root, manifest


def _write_contextspace_doc(repo_root: Path, kind: str, content: str) -> None:
    cs_dir = repo_root / ".codex-autorunner" / "contextspace"
    cs_dir.mkdir(parents=True, exist_ok=True)
    doc_path = cs_dir / f"{kind}.md"
    doc_path.write_text(content, encoding="utf-8")


ScopeResolverFactory = Callable[[Path], FilesystemScopeResolver]

SCOPE_RESOLVER_FACTORIES: list[Tuple[str, ScopeResolverFactory]] = [
    (
        "filesystem",
        lambda tmp: FilesystemScopeResolver(*_make_hub_tree(tmp)[:2]),
    ),
]


MemoryStoreFactory = Callable[[Path], FilesystemMemoryStore]

MEMORY_STORE_FACTORIES: list[Tuple[str, MemoryStoreFactory]] = [
    (
        "filesystem",
        lambda tmp: FilesystemMemoryStore(
            FilesystemScopeResolver(*_make_hub_tree(tmp)[:2])
        ),
    ),
]


TicketStoreFactory = Callable[[Path], FilesystemTicketStore]

TICKET_STORE_FACTORIES: list[Tuple[str, TicketStoreFactory]] = [
    (
        "filesystem",
        lambda tmp: FilesystemTicketStore(
            FilesystemScopeResolver(*_make_hub_tree(tmp)[:2])
        ),
    ),
]


@pytest.fixture()
def hub_tree(tmp_path: Path) -> tuple[Path, Manifest]:
    return _make_hub_tree(tmp_path)


@pytest.fixture()
def write_contextspace_doc() -> Callable[[Path, str, str], None]:
    return _write_contextspace_doc


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "scope_resolver_factory" in metafunc.fixturenames:
        ids = [name for name, _ in SCOPE_RESOLVER_FACTORIES]
        metafunc.parametrize(
            "scope_resolver_factory",
            [fn for _, fn in SCOPE_RESOLVER_FACTORIES],
            ids=ids,
        )
    if "memory_store_factory" in metafunc.fixturenames:
        ids = [name for name, _ in MEMORY_STORE_FACTORIES]
        metafunc.parametrize(
            "memory_store_factory",
            [fn for _, fn in MEMORY_STORE_FACTORIES],
            ids=ids,
        )
    if "ticket_store_factory" in metafunc.fixturenames:
        ids = [name for name, _ in TICKET_STORE_FACTORIES]
        metafunc.parametrize(
            "ticket_store_factory",
            [fn for _, fn in TICKET_STORE_FACTORIES],
            ids=ids,
        )
