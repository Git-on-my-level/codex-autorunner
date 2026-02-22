"""Archive browsing routes for repo-mode servers."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse

from ..schemas import (
    ArchiveSnapshotDetailResponse,
    ArchiveSnapshotsResponse,
    ArchiveSnapshotSummary,
    ArchiveTreeNode,
    ArchiveTreeResponse,
    LocalRunArchivesResponse,
    LocalRunArchiveSummary,
)

logger = logging.getLogger("codex_autorunner.routes.archive")

_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")


def _archive_worktrees_root(repo_root: Path) -> Path:
    return repo_root / ".codex-autorunner" / "archive" / "worktrees"


def _local_flows_root(repo_root: Path) -> Path:
    return repo_root / ".codex-autorunner" / "flows"


def _normalize_component(value: str, label: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise ValueError(f"missing {label}")
    if "\\" in cleaned:
        raise ValueError(f"invalid {label}")
    if _DRIVE_PREFIX_RE.match(cleaned):
        raise ValueError(f"invalid {label}")
    path = PurePosixPath(cleaned)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"invalid {label}")
    if len(path.parts) != 1:
        raise ValueError(f"invalid {label}")
    if path.name in {".", ".."}:
        raise ValueError(f"invalid {label}")
    return path.name


def _normalize_archive_rel_path(base: Path, rel_path: str) -> tuple[Path, str]:
    cleaned = (rel_path or "").strip()
    if not cleaned:
        return base, ""
    if "\\" in cleaned:
        raise ValueError("invalid archive path")
    if _DRIVE_PREFIX_RE.match(cleaned):
        raise ValueError("invalid archive path")
    relative = PurePosixPath(cleaned)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("invalid archive path")
    base_real = base.resolve(strict=False)
    candidate = (base / relative).resolve(
        strict=False
    )  # codeql[py/path-injection] base is validated snapshot root
    try:
        rel_posix = candidate.relative_to(base_real).as_posix()
    except ValueError:
        raise ValueError("invalid archive path") from None
    return candidate, rel_posix


_LOCAL_ARCHIVE_DIRS = {"archived_tickets", "archived_runs"}


def _normalize_local_archive_rel_path(base: Path, rel_path: str) -> tuple[Path, str]:
    target, rel_posix = _normalize_archive_rel_path(base, rel_path)
    if rel_posix:
        head = rel_posix.split("/", 1)[0]
        if head not in _LOCAL_ARCHIVE_DIRS:
            raise ValueError("invalid archive path")
    return target, rel_posix


def _resolve_snapshot_root(
    repo_root: Path,
    snapshot_id: str,
    worktree_repo_id: Optional[str] = None,
) -> tuple[Path, str]:
    snapshot_id = _normalize_component(snapshot_id, "snapshot_id")
    worktrees_root = _archive_worktrees_root(repo_root)
    if not worktrees_root.exists():
        raise FileNotFoundError("archive root missing")

    matches: list[tuple[str, Path]] = []
    if worktree_repo_id:
        worktree_repo_id = _normalize_component(worktree_repo_id, "worktree_repo_id")
        candidate = worktrees_root / worktree_repo_id / snapshot_id
        if candidate.exists() and candidate.is_dir():
            matches.append((worktree_repo_id, candidate))
    else:
        for worktree_dir in sorted(worktrees_root.iterdir(), key=lambda p: p.name):
            if not worktree_dir.is_dir():
                continue
            worktree_id = worktree_dir.name
            candidate = worktree_dir / snapshot_id
            if candidate.exists() and candidate.is_dir():
                matches.append((worktree_id, candidate))

    if not matches:
        raise FileNotFoundError("snapshot not found")
    if len(matches) > 1:
        raise RuntimeError("snapshot id ambiguous")

    worktree_id, snapshot_root = matches[0]
    resolved_root = snapshot_root.resolve(strict=False)
    archive_root = worktrees_root.resolve(strict=False)
    try:
        resolved_root.relative_to(archive_root)
    except ValueError:
        raise ValueError("invalid snapshot path") from None
    return resolved_root, worktree_id


def _resolve_local_run_root(repo_root: Path, run_id: str) -> Path:
    run_id = _normalize_component(run_id, "run_id")
    flows_root = _local_flows_root(repo_root)
    run_root = flows_root / run_id
    if not run_root.exists() or not run_root.is_dir():
        raise FileNotFoundError("run archive not found")
    return run_root


def _safe_mtime(path: Path) -> Optional[float]:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _format_created_at(path: Path) -> Optional[str]:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        return None


def _load_meta(meta_path: Path) -> Optional[dict[str, Any]]:
    try:
        if not meta_path.exists():
            return None
        raw = meta_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except Exception as exc:
        logger.debug("Failed to read META.json at %s: %s", meta_path, exc)
    return None


def _snapshot_summary(
    snapshot_root: Path,
    worktree_repo_id: str,
    meta: Optional[dict[str, Any]],
) -> ArchiveSnapshotSummary:
    snapshot_id = snapshot_root.name
    created_at: Optional[str] = None
    status: Optional[str] = None
    branch: Optional[str] = None
    head_sha: Optional[str] = None
    note: Optional[str] = None
    summary: Optional[dict[str, Any]] = None

    if meta:
        created_at = str(meta.get("created_at")) if meta.get("created_at") else None
        status = str(meta.get("status")) if meta.get("status") else None
        branch = str(meta.get("branch")) if meta.get("branch") else None
        head_sha = str(meta.get("head_sha")) if meta.get("head_sha") else None
        note = str(meta.get("note")) if meta.get("note") else None
        if isinstance(meta.get("summary"), dict):
            summary = meta.get("summary")

    if not created_at:
        created_at = _format_created_at(snapshot_root)
    if not status:
        status = "partial" if meta is None else "unknown"

    return ArchiveSnapshotSummary(
        snapshot_id=snapshot_id,
        worktree_repo_id=worktree_repo_id,
        created_at=created_at,
        status=status,
        branch=branch,
        head_sha=head_sha,
        note=note,
        summary=summary,
    )


def _iter_snapshots(repo_root: Path) -> list[ArchiveSnapshotSummary]:
    worktrees_root = _archive_worktrees_root(repo_root)
    if not worktrees_root.exists() or not worktrees_root.is_dir():
        return []
    snapshots: list[ArchiveSnapshotSummary] = []
    for worktree_dir in sorted(worktrees_root.iterdir(), key=lambda p: p.name):
        if not worktree_dir.is_dir():
            continue
        worktree_id = worktree_dir.name
        for snapshot_dir in sorted(worktree_dir.iterdir(), key=lambda p: p.name):
            if not snapshot_dir.is_dir():
                continue
            meta = _load_meta(snapshot_dir / "META.json")
            snapshots.append(_snapshot_summary(snapshot_dir, worktree_id, meta))
    snapshots.sort(
        key=lambda item: (item.created_at or "", item.snapshot_id), reverse=True
    )
    return snapshots


def _format_mtime(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _iter_local_run_archives(repo_root: Path) -> list[LocalRunArchiveSummary]:
    flows_root = _local_flows_root(repo_root)
    if not flows_root.exists() or not flows_root.is_dir():
        return []
    entries: list[tuple[float, LocalRunArchiveSummary]] = []
    for run_dir in sorted(flows_root.iterdir(), key=lambda p: p.name):
        if not run_dir.is_dir():
            continue
        tickets_dir = run_dir / "archived_tickets"
        runs_dir = run_dir / "archived_runs"
        has_tickets = tickets_dir.exists() and tickets_dir.is_dir()
        has_runs = runs_dir.exists() and runs_dir.is_dir()
        if not has_tickets and not has_runs:
            continue
        mtime_candidates = [
            _safe_mtime(tickets_dir) if has_tickets else None,
            _safe_mtime(runs_dir) if has_runs else None,
        ]
        mtime = max([ts for ts in mtime_candidates if ts is not None], default=0.0)
        summary = LocalRunArchiveSummary(
            run_id=run_dir.name,
            archived_at=_format_mtime(mtime) if mtime else None,
            has_tickets=has_tickets,
            has_runs=has_runs,
        )
        entries.append((mtime, summary))
    entries.sort(key=lambda item: (item[0], item[1].run_id), reverse=True)
    return [entry[1] for entry in entries]


def _list_tree(snapshot_root: Path, rel_path: str) -> ArchiveTreeResponse:
    target, rel_posix = _normalize_archive_rel_path(snapshot_root, rel_path)
    if (
        not target.exists()
    ):  # codeql[py/path-injection] target normalized to snapshot root
        raise FileNotFoundError("path not found")
    if (
        not target.is_dir()
    ):  # codeql[py/path-injection] target normalized to snapshot root
        raise ValueError("path is not a directory")

    root_real = snapshot_root.resolve(strict=False)
    nodes: list[ArchiveTreeNode] = []
    for child in sorted(
        target.iterdir(), key=lambda p: p.name
    ):  # codeql[py/path-injection] target validated by normalize helper
        try:
            resolved = child.resolve(strict=False)
            resolved.relative_to(root_real)
        except Exception:
            continue

        if child.is_dir():
            node_type = "folder"
            size_bytes: Optional[int] = None
        else:
            node_type = "file"
            try:
                size_bytes = child.stat().st_size
            except OSError:
                size_bytes = None

        try:
            node_path = resolved.relative_to(root_real).as_posix()
        except ValueError:
            continue

        node_type_literal: Literal["file", "folder"] = node_type  # type: ignore[assignment]
        nodes.append(
            ArchiveTreeNode(
                path=node_path,
                name=child.name,
                type=node_type_literal,
                size_bytes=size_bytes,
                mtime=_safe_mtime(child),
            )
        )

    return ArchiveTreeResponse(path=rel_posix, nodes=nodes)


def _list_local_tree(run_root: Path, rel_path: str) -> ArchiveTreeResponse:
    target, rel_posix = _normalize_local_archive_rel_path(run_root, rel_path)
    if not rel_posix:
        nodes: list[ArchiveTreeNode] = []
        for name in sorted(_LOCAL_ARCHIVE_DIRS):
            candidate = run_root / name
            if not candidate.exists() or not candidate.is_dir():
                continue
            nodes.append(
                ArchiveTreeNode(
                    path=name,
                    name=name,
                    type="folder",
                    size_bytes=None,
                    mtime=_safe_mtime(candidate),
                )
            )
        return ArchiveTreeResponse(path="", nodes=nodes)

    if not target.exists():
        raise FileNotFoundError("path not found")
    if not target.is_dir():
        raise ValueError("path is not a directory")

    root_real = run_root.resolve(strict=False)
    local_nodes: list[ArchiveTreeNode] = []
    for child in sorted(target.iterdir(), key=lambda p: p.name):
        try:
            resolved = child.resolve(strict=False)
            resolved.relative_to(root_real)
        except Exception:
            continue

        if child.is_dir():
            node_type: Literal["file", "folder"] = "folder"
            size_bytes: Optional[int] = None
        else:
            node_type = "file"
            try:
                size_bytes = child.stat().st_size
            except OSError:
                size_bytes = None

        try:
            node_path = resolved.relative_to(root_real).as_posix()
        except ValueError:
            continue

        local_nodes.append(
            ArchiveTreeNode(
                path=node_path,
                name=child.name,
                type=node_type,
                size_bytes=size_bytes,
                mtime=_safe_mtime(child),
            )
        )

    return ArchiveTreeResponse(path=rel_posix, nodes=local_nodes)


def build_archive_routes() -> APIRouter:
    router = APIRouter(prefix="/api/archive", tags=["archive"])

    @router.get("/snapshots", response_model=ArchiveSnapshotsResponse)
    def list_snapshots(request: Request):
        repo_root = request.app.state.engine.repo_root
        snapshots = _iter_snapshots(repo_root)
        return {"snapshots": snapshots}

    @router.get("/local-runs", response_model=LocalRunArchivesResponse)
    def list_local_runs(request: Request):
        repo_root = request.app.state.engine.repo_root
        archives = _iter_local_run_archives(repo_root)
        return {"archives": archives}

    @router.get(
        "/snapshots/{snapshot_id}", response_model=ArchiveSnapshotDetailResponse
    )
    def get_snapshot(
        request: Request, snapshot_id: str, worktree_repo_id: Optional[str] = None
    ):
        repo_root = request.app.state.engine.repo_root
        try:
            snapshot_root, worktree_id = _resolve_snapshot_root(
                repo_root, snapshot_id, worktree_repo_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        meta = _load_meta(snapshot_root / "META.json")
        summary = _snapshot_summary(snapshot_root, worktree_id, meta)
        return {"snapshot": summary, "meta": meta}

    @router.get("/tree", response_model=ArchiveTreeResponse)
    def list_tree(
        request: Request,
        snapshot_id: str,
        path: str = "",
        worktree_repo_id: Optional[str] = None,
    ):
        repo_root = request.app.state.engine.repo_root
        try:
            snapshot_root, _ = _resolve_snapshot_root(
                repo_root, snapshot_id, worktree_repo_id
            )
            response = _list_tree(snapshot_root, path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return response

    @router.get("/local/tree", response_model=ArchiveTreeResponse)
    def list_local_tree(
        request: Request,
        run_id: str,
        path: str = "",
    ):
        repo_root = request.app.state.engine.repo_root
        try:
            run_root = _resolve_local_run_root(repo_root, run_id)
            response = _list_local_tree(run_root, path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return response

    @router.get("/file", response_class=PlainTextResponse)
    def read_file(
        request: Request,
        snapshot_id: str,
        path: str,
        worktree_repo_id: Optional[str] = None,
    ):
        repo_root = request.app.state.engine.repo_root
        try:
            snapshot_root, _ = _resolve_snapshot_root(
                repo_root, snapshot_id, worktree_repo_id
            )
            target, _ = _normalize_archive_rel_path(snapshot_root, path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        if not target.exists() or target.is_dir():
            raise HTTPException(status_code=404, detail="file not found")

        try:
            content = target.read_text(
                encoding="utf-8", errors="replace"
            )  # codeql[py/path-injection] target validated by normalize helper
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return PlainTextResponse(content)

    @router.get("/local/file", response_class=PlainTextResponse)
    def read_local_file(
        request: Request,
        run_id: str,
        path: str,
    ):
        repo_root = request.app.state.engine.repo_root
        try:
            run_root = _resolve_local_run_root(repo_root, run_id)
            target, rel_posix = _normalize_local_archive_rel_path(run_root, path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        if not rel_posix or not target.exists() or target.is_dir():
            raise HTTPException(status_code=404, detail="file not found")

        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return PlainTextResponse(content)

    @router.get("/download")
    def download_file(
        request: Request,
        snapshot_id: str,
        path: str,
        worktree_repo_id: Optional[str] = None,
    ):
        repo_root = request.app.state.engine.repo_root
        try:
            snapshot_root, _ = _resolve_snapshot_root(
                repo_root, snapshot_id, worktree_repo_id
            )
            target, _ = _normalize_archive_rel_path(snapshot_root, path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        if not target.exists() or target.is_dir():
            raise HTTPException(status_code=404, detail="file not found")

        return FileResponse(
            path=target,  # codeql[py/path-injection] target validated by normalize helper
            filename=target.name,
        )

    @router.get("/local/download")
    def download_local_file(
        request: Request,
        run_id: str,
        path: str,
    ):
        repo_root = request.app.state.engine.repo_root
        try:
            run_root = _resolve_local_run_root(repo_root, run_id)
            target, rel_posix = _normalize_local_archive_rel_path(run_root, path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        if not rel_posix or not target.exists() or target.is_dir():
            raise HTTPException(status_code=404, detail="file not found")

        return FileResponse(
            path=target,  # codeql[py/path-injection] target validated by normalize helper
            filename=target.name,
        )

    return router


__all__ = ["build_archive_routes"]
