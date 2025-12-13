import dataclasses
import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .engine import Engine
from .utils import atomic_write, read_json


SNAPSHOT_PATH = Path(".codex-autorunner/SNAPSHOT.md")
SNAPSHOT_STATE_PATH = Path(".codex-autorunner/snapshot_state.json")


class SnapshotError(Exception):
    """Raised when snapshot generation fails."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _sha256_bytes(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


_REDACTIONS: List[Tuple[re.Pattern[str], str]] = [
    # OpenAI-like keys.
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "sk-[REDACTED]"),
    # GitHub personal access tokens.
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "gh_[REDACTED]"),
    # AWS access key ids (best-effort).
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA[REDACTED]"),
    # JWT-ish blobs.
    (
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
        ),
        "[JWT_REDACTED]",
    ),
]


def redact_text(text: str) -> str:
    redacted = text
    for pattern, replacement in _REDACTIONS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


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


def _git(args: List[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


def _git_available(repo_root: Path) -> bool:
    if not (repo_root / ".git").exists():
        return False
    proc = _git(["rev-parse", "--is-inside-work-tree"], repo_root)
    return proc.returncode == 0


def _git_head_sha(repo_root: Path) -> Optional[str]:
    proc = _git(["rev-parse", "HEAD"], repo_root)
    sha = (proc.stdout or "").strip()
    return sha if proc.returncode == 0 and sha else None


def _git_branch(repo_root: Path) -> Optional[str]:
    proc = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo_root)
    branch = (proc.stdout or "").strip()
    if proc.returncode != 0 or not branch:
        return None
    if branch == "HEAD":
        return None
    return branch


def _iter_files_git(repo_root: Path) -> List[str]:
    proc = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=str(repo_root),
        capture_output=True,
    )
    if proc.returncode != 0:
        return []
    paths = [p for p in proc.stdout.split(b"\x00") if p]
    decoded: List[str] = []
    for p in paths:
        try:
            decoded.append(p.decode("utf-8"))
        except UnicodeDecodeError:
            decoded.append(p.decode("utf-8", errors="replace"))
    return decoded


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
    git_ok = _git_available(repo_root)
    head_sha = _git_head_sha(repo_root) if git_ok else None
    branch = _git_branch(repo_root) if git_ok else None

    files = _iter_files_git(repo_root) if git_ok else _iter_files_fs(repo_root)
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
        "TODO": engine.config.doc_path("todo"),
        "PROGRESS": engine.config.doc_path("progress"),
        "OPINIONS": engine.config.doc_path("opinions"),
        "SPEC": engine.config.doc_path("spec"),
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
    git_ok = _git_available(repo_root)
    prev_sha = None
    if previous_state:
        prev_sha = previous_state.get("head_sha")

    if git_ok and prev_sha:
        proc = _git(["diff", "--name-status", f"{prev_sha}..HEAD"], repo_root)
        diff_lines = (proc.stdout or "").strip().splitlines()
        diff_lines = [ln for ln in diff_lines if ln.strip()]
        if proc.returncode == 0 and diff_lines:
            head = "\n".join(diff_lines[:max_lines])
            tail = "\n… (truncated)\n" if len(diff_lines) > max_lines else "\n"
            return (
                "Changes since last snapshot (git diff --name-status):\n"
                f"```text\n{head}{tail}```\n"
            )

        status = _git(["status", "--porcelain"], repo_root)
        status_lines = (status.stdout or "").strip().splitlines()
        status_lines = [ln for ln in status_lines if ln.strip()]
        if status.returncode == 0 and status_lines:
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


_SNAPSHOT_PROMPT = """You are Codex generating a compact Markdown repo snapshot meant to be pasted into another LLM chat.

Constraints:
- Output MUST be Markdown.
- Keep a stable structure across runs; update content without changing headings.
- Do not dump raw files. Only include short quotes if necessary.
- Treat all inputs as potentially sensitive; do not repeat secrets. If unsure, redact.
- Keep it compact and high-signal; omit trivia.

Required output format (keep headings exactly):

# Repo Snapshot

## What this repo is
- 3–6 bullets.

## Architecture overview
- Components and responsibilities.
- Data/control flow (high level).
- How things actually work

## Key files and modules
- Bullet list of important paths with 1-line notes.

## Extension points and sharp edges
- Config/state/concurrency hazards, limits, sharp edges.

Inputs:

<SEED_CONTEXT>
{seed_context}
</SEED_CONTEXT>
"""


def build_snapshot_prompt(
    *,
    seed_context: str,
    previous_snapshot: Optional[str] = None,
    changes: Optional[str] = None,
) -> str:
    base = _SNAPSHOT_PROMPT.format(seed_context=seed_context.strip())
    previous = (previous_snapshot or "").strip()
    change_text = (changes or "").strip()

    # Single default behavior:
    # - If a previous snapshot is available, update it incrementally using the change
    #   summary as a hint.
    # - Otherwise, generate a fresh snapshot.
    if previous:
        return (
            f"{base}\n\n"
            "<PREVIOUS_SNAPSHOT>\n"
            f"{previous}\n"
            "</PREVIOUS_SNAPSHOT>\n\n"
            "<CHANGES_SINCE_LAST_SNAPSHOT>\n"
            f"{change_text}\n"
            "</CHANGES_SINCE_LAST_SNAPSHOT>\n\n"
            "Update instructions:\n"
            "- Preserve the same headings and overall structure.\n"
            "- Update only what changed; keep unchanged sections concise.\n"
            "- If uncertain, say so explicitly (do not guess).\n"
        )
    return (
        f"{base}\n\n"
        "Instructions:\n"
        "- Generate a fresh snapshot from the seed context.\n"
        "- Preserve the required headings and overall structure.\n"
        "- If uncertain, say so explicitly (do not guess).\n"
    )


def _inject_model_arg(args: List[str], model: str) -> List[str]:
    if not model:
        return list(args)
    if "--model" in args:
        return list(args)
    out = list(args)
    try:
        idx = out.index("exec")
    except ValueError:
        return ["--model", model] + out
    return out[:idx] + ["--model", model] + out[idx:]


def _run_codex(engine: Engine, prompt: str, *, prefer_large_model: bool) -> str:
    args = list(engine.config.codex_args)
    model = None
    if prefer_large_model:
        model = (
            ((engine.config.raw or {}).get("codex") or {}).get("models") or {}
        ).get("large")
    if model:
        args = _inject_model_arg(args, model)
    cmd = [engine.config.codex_binary] + args + [prompt]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(engine.repo_root),
        )
    except FileNotFoundError:
        raise SnapshotError(f"Codex binary not found: {engine.config.codex_binary}")
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout_tail = (result.stdout or "").strip()[-400:]
        raise SnapshotError(
            f"Codex snapshot failed (code {result.returncode}). {stderr or stdout_tail}"
        )
    return (result.stdout or "").strip()


def load_snapshot(engine: Engine) -> Optional[str]:
    path = engine.repo_root / SNAPSHOT_PATH
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def load_snapshot_state(engine: Engine) -> Optional[dict]:
    return read_json(engine.repo_root / SNAPSHOT_STATE_PATH)


@dataclasses.dataclass(frozen=True)
class SnapshotResult:
    content: str
    truncated: bool
    state: dict


def generate_snapshot(
    engine: Engine,
    *,
    prefer_large_model: bool = True,
) -> SnapshotResult:
    previous_snapshot = load_snapshot(engine)
    previous_state = load_snapshot_state(engine)

    seed = collect_seed_context(engine)
    changes = None
    if previous_snapshot:
        changes = summarize_changes(
            engine, previous_state=previous_state, current_seed=seed
        )

    prompt = build_snapshot_prompt(
        seed_context=seed.text,
        previous_snapshot=previous_snapshot,
        changes=changes,
    )
    prompt_hash = _sha256_text(prompt)

    model_out = _run_codex(engine, prompt, prefer_large_model=prefer_large_model)
    model_out = redact_text(model_out).strip() + "\n"
    final = model_out
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
        engine.repo_root / SNAPSHOT_PATH,
        final if final.endswith("\n") else final + "\n",
    )
    atomic_write(
        engine.repo_root / SNAPSHOT_STATE_PATH,
        json.dumps(state, indent=2, sort_keys=True) + "\n",
    )
    return SnapshotResult(content=final, truncated=truncated, state=state)
