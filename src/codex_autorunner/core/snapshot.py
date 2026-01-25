import asyncio
import dataclasses
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from ..integrations.app_server.client import CodexAppServerError
from ..integrations.app_server.supervisor import WorkspaceAppServerSupervisor
from .app_server_prompts import build_app_server_snapshot_prompt
from .app_server_threads import (
    AppServerThreadRegistry,
    default_app_server_threads_path,
)
from .config import RepoConfig
from .engine import Engine
from .git_utils import (
    git_available,
    git_branch,
    git_diff_name_status,
    git_head_sha,
    git_ls_files,
    git_status_porcelain,
)
from .redaction import redact_text
from .utils import atomic_write, read_json


class SnapshotError(Exception):
    """Raised when snapshot generation fails."""


def _repo_config(engine: Engine) -> RepoConfig:
    if not isinstance(engine.config, RepoConfig):
        raise SnapshotError("Snapshot generation requires a repo workspace config")
    return engine.config


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _sha256_bytes(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


_DEFAULT_IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".cache",
    "__pycache__",
    ".tox",
}


_SECRET_BASENAMES = {
    ".env",
    ".env.local",
    "id_rsa",
    "id_ed25519",
    "known_hosts",
    ".npmrc",
    ".pypirc",
}

_SECRET_EXTS = {".pem", ".key", ".p12", ".pfx", ".kdbx"}


def _looks_like_secret_path(path: Path) -> bool:
    name = path.name
    if name in _SECRET_BASENAMES:
        return True
    if name.startswith(".env."):
        return True
    if path.suffix.lower() in _SECRET_EXTS:
        return True
    return False


def _is_probably_binary(blob: bytes) -> bool:
    if b"\x00" in blob:
        return True
    # Heuristic: lots of control chars.
    sample = blob[:2048]
    if not sample:
        return False
    control = sum(1 for b in sample if b < 9 or (13 < b < 32))
    return (control / len(sample)) > 0.3


def _iter_files_fs(repo_root: Path, *, max_files: int = 5000) -> List[str]:
    out: List[str] = []
    for root, dirs, files in os.walk(repo_root):
        rel_root = os.path.relpath(root, repo_root)
        if rel_root == ".":
            rel_root = ""
        dirs[:] = [
            d
            for d in sorted(dirs)
            if d not in _DEFAULT_IGNORED_DIRS
            and not (Path(rel_root) / d).parts[:1] == (".git",)
        ]
        for f in sorted(files):
            rel = str(Path(rel_root) / f) if rel_root else f
            if _looks_like_secret_path(Path(rel)):
                continue
            out.append(rel)
            if len(out) >= max_files:
                return out
    return out


def _build_tree_outline(
    rel_paths: Iterable[str], *, max_depth: int, max_entries: int
) -> str:
    paths = [p for p in rel_paths if p and not _looks_like_secret_path(Path(p))]
    paths = sorted(set(paths))

    shown = 0
    lines: List[str] = []
    last_parts: List[str] = []
    for rel in paths:
        parts = rel.split("/")
        if len(parts) > max_depth:
            parts = parts[:max_depth]
            parts[-1] = parts[-1] + "/…"

        # Emit minimal directory structure changes based on common prefix.
        common = 0
        for a, b in zip(last_parts, parts):
            if a != b:
                break
            common += 1

        # Print remaining parts with indentation.
        for idx in range(common, len(parts)):
            name = parts[idx]
            indent = "  " * idx
            prefix = "- " if idx == 0 else "- "
            lines.append(f"{indent}{prefix}{name}")
        last_parts = parts

        shown += 1
        if shown >= max_entries:
            lines.append(f"- … (truncated after {max_entries} entries)")
            break
    return "\n".join(lines)


def _detect_key_files(repo_root: Path) -> List[Path]:
    candidates = [
        "README.md",
        "README.rst",
        "pyproject.toml",
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "requirements.txt",
        "setup.py",
        "Cargo.toml",
        "go.mod",
        "Makefile",
        "Dockerfile",
        ".github/workflows",
        "src/codex_autorunner/cli.py",
        "src/codex_autorunner/server.py",
        "src/codex_autorunner/engine.py",
    ]
    found: List[Path] = []
    for rel in candidates:
        p = repo_root / rel
        if p.exists():
            found.append(p)
    return found


@dataclasses.dataclass(frozen=True)
class SeedContext:
    text: str
    bytes_read: int
    file_hashes: Dict[str, str]
    head_sha: Optional[str]
    branch: Optional[str]
    seed_hash: str


def _read_text_excerpt(
    path: Path,
    *,
    repo_root: Path,
    max_bytes: int,
    max_chars: int,
    budget_bytes: int,
    bytes_read_so_far: int,
) -> Tuple[str, int, Optional[str]]:
    rel = str(path.relative_to(repo_root))
    if _looks_like_secret_path(Path(rel)):
        return "", 0, None
    if not path.exists() or not path.is_file():
        return "", 0, None

    try:
        size = path.stat().st_size
    except OSError:
        return "", 0, None

    if size > max_bytes:
        return f"_Skipped excerpt (>{max_bytes} bytes): `{rel}`_\n", 0, None

    remaining = max(0, budget_bytes - bytes_read_so_far)
    if remaining <= 0:
        return "", 0, None

    to_read = min(max_bytes, remaining)
    try:
        blob = path.read_bytes()[:to_read]
    except OSError:
        return "", 0, None

    if _is_probably_binary(blob):
        return f"_Skipped binary file: `{rel}`_\n", 0, None

    decoded = blob.decode("utf-8", errors="replace")
    decoded = decoded.replace("\r\n", "\n")
    decoded = decoded[:max_chars]
    decoded = redact_text(decoded).strip()
    if not decoded:
        return "", 0, _sha256_bytes(blob)
    return decoded + "\n", len(blob), _sha256_bytes(blob)


def collect_seed_context(
    engine: Engine,
    *,
    per_file_read_cap_bytes: int = 200_000,
    total_read_cap_bytes: int = 1_000_000,
    tree_max_depth: int = 4,
    tree_max_entries: int = 500,
    per_doc_max_chars: int = 2000,
) -> SeedContext:
    repo_root = engine.repo_root
    config = _repo_config(engine)
    git_ok = git_available(repo_root)
    head_sha = git_head_sha(repo_root) if git_ok else None
    branch = git_branch(repo_root) if git_ok else None

    files = git_ls_files(repo_root) if git_ok else _iter_files_fs(repo_root)
    tree = _build_tree_outline(
        files, max_depth=tree_max_depth, max_entries=tree_max_entries
    )

    key_files = _detect_key_files(repo_root)

    bytes_read = 0
    file_hashes: Dict[str, str] = {}

    def _add_excerpt(title: str, path: Path, max_chars: int) -> str:
        nonlocal bytes_read
        excerpt, inc, digest = _read_text_excerpt(
            path,
            repo_root=repo_root,
            max_bytes=per_file_read_cap_bytes,
            max_chars=max_chars,
            budget_bytes=total_read_cap_bytes,
            bytes_read_so_far=bytes_read,
        )
        bytes_read += inc
        if digest:
            rel = str(path.relative_to(repo_root))
            file_hashes[rel] = digest
        if not excerpt:
            return ""
        if excerpt.startswith("_Skipped "):
            return f"- {excerpt.strip()}\n"
        return f"```text\n{excerpt}```\n"

    # Work docs are always included, but bounded and redacted.
    docs = {
        "TODO": config.doc_path("todo"),
        "PROGRESS": config.doc_path("progress"),
        "OPINIONS": config.doc_path("opinions"),
        "SPEC": config.doc_path("spec"),
    }

    parts: List[str] = []
    parts.append("# Seed context (bounded)\n")
    parts.append("## Repo identity\n")
    parts.append(f"- Root: `{repo_root}`\n")
    parts.append(f"- VCS: `git` ({'detected' if git_ok else 'not detected'})\n")
    if branch:
        parts.append(f"- Branch: `{branch}`\n")
    if head_sha:
        parts.append(f"- HEAD: `{head_sha}`\n")

    parts.append("\n## Tree outline\n")
    parts.append(f"_Max depth={tree_max_depth}, max entries={tree_max_entries}_\n\n")
    parts.append(tree + "\n")

    parts.append("\n## Key files\n")
    if key_files:
        for p in key_files:
            rel = str(p.relative_to(repo_root))
            parts.append(f"- `{rel}`\n")
    else:
        parts.append("_No key files detected._\n")

    parts.append("\n## Work docs excerpts\n")
    parts.append(
        "_Excerpts are capped and redacted; edit the real files in `.codex-autorunner/`._\n\n"
    )
    for label, path in docs.items():
        parts.append(f"### {label} (`{path.relative_to(repo_root)}`)\n")
        parts.append(_add_excerpt(label, path, per_doc_max_chars))

    # Optionally include a tiny README excerpt to ground the model.
    readme = next((p for p in key_files if p.name.lower().startswith("readme")), None)
    if readme:
        parts.append("\n## README excerpt\n")
        parts.append(_add_excerpt("README", readme, 1200))

    seed_text = "".join(parts).strip() + "\n"
    seed_hash = _sha256_text(seed_text)
    return SeedContext(
        text=seed_text,
        bytes_read=bytes_read,
        file_hashes=file_hashes,
        head_sha=head_sha,
        branch=branch,
        seed_hash=seed_hash,
    )


def summarize_changes(
    engine: Engine,
    *,
    previous_state: Optional[dict],
    current_seed: SeedContext,
    max_lines: int = 60,
) -> str:
    repo_root = engine.repo_root
    git_ok = git_available(repo_root)
    prev_sha = None
    if previous_state:
        prev_sha = previous_state.get("head_sha")

    if git_ok and prev_sha:
        diff_output = git_diff_name_status(repo_root, prev_sha, "HEAD")
        diff_lines = (diff_output or "").strip().splitlines()
        diff_lines = [ln for ln in diff_lines if ln.strip()]
        if diff_lines:
            head = "\n".join(diff_lines[:max_lines])
            tail = "\n… (truncated)\n" if len(diff_lines) > max_lines else "\n"
            return (
                "Changes since last snapshot (git diff --name-status):\n"
                f"```text\n{head}{tail}```\n"
            )

        status_output = git_status_porcelain(repo_root)
        status_lines = (status_output or "").strip().splitlines()
        status_lines = [ln for ln in status_lines if ln.strip()]
        if status_lines:
            head = "\n".join(status_lines[:max_lines])
            tail = "\n… (truncated)\n" if len(status_lines) > max_lines else "\n"
            return f"Working tree status (git status --porcelain):\n```text\n{head}{tail}```\n"

    # Fallback: compare seed input file hashes we control.
    prev_hashes: Dict[str, str] = {}
    if previous_state and isinstance(previous_state.get("seed_file_hashes"), dict):
        prev_hashes = dict(previous_state["seed_file_hashes"])
    changed = []
    for rel, digest in sorted(current_seed.file_hashes.items()):
        if prev_hashes.get(rel) != digest:
            changed.append(rel)
    if changed:
        shown = changed[:max_lines]
        suffix = "\n- … (truncated)" if len(changed) > max_lines else ""
        items = "\n".join(f"- `{p}`" for p in shown)
        return f"Changes since last snapshot (seed inputs only):\n{items}{suffix}\n"

    if prev_sha and not git_ok:
        return "No VCS change summary available (git not detected).\n"
    if not prev_sha:
        return (
            "No previous snapshot SHA recorded; treating as best-effort incremental.\n"
        )
    return "No changes detected (best-effort).\n"


def load_snapshot(engine: Engine) -> Optional[str]:
    config = _repo_config(engine)
    path = config.doc_path("snapshot")
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def load_snapshot_state(engine: Engine) -> Optional[dict]:
    config = _repo_config(engine)
    return read_json(config.doc_path("snapshot_state"))


@dataclasses.dataclass(frozen=True)
class SnapshotResult:
    content: str
    truncated: bool
    state: dict


class SnapshotService:
    def __init__(
        self,
        engine: Engine,
        *,
        app_server_supervisor: Optional[WorkspaceAppServerSupervisor] = None,
        app_server_threads: Optional[AppServerThreadRegistry] = None,
    ) -> None:
        self.engine = engine
        self._app_server_supervisor = app_server_supervisor
        self._app_server_threads = app_server_threads or AppServerThreadRegistry(
            default_app_server_threads_path(self.engine.repo_root)
        )
        self._lock: Optional[asyncio.Lock] = None

    def _ensure_lock(self) -> asyncio.Lock:
        if self._lock is None:
            try:
                self._lock = asyncio.Lock()
            except RuntimeError:
                asyncio.set_event_loop(asyncio.new_event_loop())
                self._lock = asyncio.Lock()
        return self._lock

    def _ensure_app_server(self) -> WorkspaceAppServerSupervisor:
        if self._app_server_supervisor is None:
            raise SnapshotError("App-server backend is not configured")
        return self._app_server_supervisor

    async def generate_snapshot(
        self,
    ) -> SnapshotResult:
        lock = self._ensure_lock()
        if lock.locked():
            raise SnapshotError("Snapshot generation already running")
        async with lock:
            config = _repo_config(self.engine)
            previous_snapshot = await asyncio.to_thread(load_snapshot, self.engine)
            previous_state = await asyncio.to_thread(load_snapshot_state, self.engine)

            seed = await asyncio.to_thread(collect_seed_context, self.engine)
            changes = None
            if previous_snapshot:
                changes = await asyncio.to_thread(
                    summarize_changes,
                    self.engine,
                    previous_state=previous_state,
                    current_seed=seed,
                )

            prompt = build_app_server_snapshot_prompt(
                config,
                seed_context=seed.text,
                previous_snapshot=previous_snapshot,
                changes=changes,
            )
            prompt_hash = _sha256_text(prompt)

            supervisor = self._ensure_app_server()
            client = await supervisor.get_client(self.engine.repo_root)
            key = "snapshot"
            thread_id = self._app_server_threads.get_thread_id(key)
            if thread_id:
                try:
                    result = await client.thread_resume(thread_id)
                    resumed = result.get("id")
                    if isinstance(resumed, str) and resumed:
                        thread_id = resumed
                        self._app_server_threads.set_thread_id(key, thread_id)
                except CodexAppServerError:
                    self._app_server_threads.reset_thread(key)
                    thread_id = None
            if not thread_id:
                thread = await client.thread_start(str(self.engine.repo_root))
                thread_id = thread.get("id")
                if not isinstance(thread_id, str) or not thread_id:
                    raise SnapshotError("App-server did not return a thread id")
                self._app_server_threads.set_thread_id(key, thread_id)

            handle = await client.turn_start(
                thread_id,
                prompt,
                approval_policy="never",
                sandbox_policy="dangerFullAccess",
            )

            # Wait for completion (no streaming/interrupts for now)
            try:
                await handle.wait(timeout=300)  # 5 mins timeout
            except asyncio.TimeoutError as err:
                raise SnapshotError("Snapshot generation timed out") from err

            # Read the result from disk
            path = config.doc_path("snapshot")
            if not path.exists():
                raise SnapshotError("Agent failed to write snapshot file")

            final = path.read_text(encoding="utf-8")
            final = redact_text(final).strip() + "\n"
            truncated = False

            state = {
                "generated_at": _now_iso(),
                "truncated": truncated,
                "head_sha": seed.head_sha,
                "branch": seed.branch,
                "seed_hash": seed.seed_hash,
                "prompt_hash": prompt_hash,
                "seed_bytes_read": seed.bytes_read,
                "seed_file_hashes": seed.file_hashes,
            }

            atomic_write(
                config.doc_path("snapshot"),
                final if final.endswith("\n") else final + "\n",
            )
            atomic_write(
                config.doc_path("snapshot_state"),
                json.dumps(state, indent=2, sort_keys=True) + "\n",
            )
            return SnapshotResult(content=final, truncated=truncated, state=state)


# Keep the original function signature for backward compatibility if needed,
# but it will likely break if used in sync context without loop.
# We will update callers to use SnapshotService.
def generate_snapshot(
    engine: Engine,
    *,
    prefer_large_model: bool = True,
) -> SnapshotResult:
    raise NotImplementedError("Use SnapshotService.generate_snapshot() instead")
