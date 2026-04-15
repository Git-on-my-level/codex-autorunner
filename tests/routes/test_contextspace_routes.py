from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from codex_autorunner.bootstrap import seed_hub_files, seed_repo_files
from codex_autorunner.contextspace.paths import CONTEXTSPACE_DOC_KINDS
from codex_autorunner.surfaces.web.app import create_repo_app


def _client_for_repo(repo_root: Path) -> TestClient:
    seed_hub_files(repo_root.parent, force=True)
    seed_repo_files(repo_root, git_required=False)
    (repo_root / ".git").mkdir(exist_ok=True)
    app = create_repo_app(repo_root)
    return TestClient(app)


def test_contextspace_rejects_invalid_doc_kind(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    client = _client_for_repo(repo_root)

    res = client.put("/api/contextspace/binary", json={"content": "nope"})
    assert res.status_code == 400
    assert res.json()["detail"] == "invalid contextspace doc kind"


def test_contextspace_get_includes_catalog(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    client = _client_for_repo(repo_root)

    res = client.get("/api/contextspace")
    assert res.status_code == 200
    payload = res.json()
    assert [entry["kind"] for entry in payload["kinds"]] == list(CONTEXTSPACE_DOC_KINDS)


def test_contextspace_tree_lists_seeded_docs(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    client = _client_for_repo(repo_root)

    res = client.get("/api/contextspace/tree")

    assert res.status_code == 200
    payload = res.json()
    assert payload["defaultPath"] == "active_context.md"
    nodes = {entry["path"]: entry for entry in payload["tree"]}
    assert set(nodes) >= {"active_context.md", "decisions.md", "spec.md"}
    assert nodes["active_context.md"]["type"] == "file"
    assert nodes["active_context.md"]["is_pinned"] is True
    assert isinstance(nodes["active_context.md"]["size"], int)


def test_contextspace_put_rejects_unknown_keys(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    client = _client_for_repo(repo_root)

    res = client.put(
        "/api/contextspace/active_context",
        json={"contnet": "updated"},
    )

    assert res.status_code == 422
    detail = res.json()["detail"]
    assert any(item["loc"][-1] == "contnet" for item in detail)
