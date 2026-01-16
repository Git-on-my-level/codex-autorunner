from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple


class PatchError(Exception):
    """Raised when a patch cannot be normalized or applied."""


_APPLY_PATCH_BEGIN = "*** begin patch"
_APPLY_PATCH_UPDATE = "*** update file:"
_APPLY_PATCH_ADD = "*** add file:"
_APPLY_PATCH_DELETE = "*** delete file:"
_PATCH_EMPTY_HUNK = "@@"
_PATCH_TIMEOUT_SECONDS = 10
_PATCH_STDOUT_LIMIT = 4000


def _normalize_target_path(raw: str) -> str:
    path = raw.strip()
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def _extract_patch_targets(patch_text: str) -> List[str]:
    targets: List[str] = []
    for line in patch_text.splitlines():
        if line.startswith("--- ") or line.startswith("+++ "):
            parts = line.split()
            if len(parts) >= 2:
                target = parts[1]
                if target != "/dev/null":
                    targets.append(target)
    return targets


def _apply_patch_section(
    target_path: str, lines: List[str], *, targets: List[str], output: List[str]
) -> None:
    normalized_target = _normalize_target_path(target_path)
    if not normalized_target:
        return
    header = f"--- a/{normalized_target}\n+++ b/{normalized_target}\n"
    if not lines:
        lines = [_PATCH_EMPTY_HUNK]
    body = "\n".join(lines)
    output.append(header + body + ("\n" if body else ""))
    targets.append(f"a/{normalized_target}")


def _convert_apply_patch_format(patch_text: str) -> Tuple[str, List[str]]:
    lines = patch_text.splitlines()
    output: List[str] = []
    targets: List[str] = []
    current_target = ""
    current_lines: List[str] = []

    def flush() -> None:
        nonlocal current_target, current_lines
        _apply_patch_section(
            current_target, current_lines, targets=targets, output=output
        )
        current_target = ""
        current_lines = []

    for line in lines:
        lower = line.lower()
        if lower.startswith(_APPLY_PATCH_UPDATE):
            flush()
            current_target = line.split(":", 1)[1].strip()
            continue
        if lower.startswith(_APPLY_PATCH_ADD) or lower.startswith(_APPLY_PATCH_DELETE):
            raise PatchError(
                "Unsupported apply_patch directive; only Update File is allowed"
            )
        if lower.startswith("***"):
            continue
        if line.startswith("@@"):
            current_lines.append(line)
            continue
        if line.startswith(("+", "-", " ")):
            payload = line[1:]
            if payload.startswith(line[0]):
                payload = payload[1:]
            current_lines.append(line[0] + payload)
            continue
    flush()
    if not targets:
        raise PatchError("Patch missing target path")
    return "".join(output), targets


def normalize_patch_text(
    patch_text: str, *, default_target: Optional[str] = None
) -> Tuple[str, List[str]]:
    if not isinstance(patch_text, str) or not patch_text.strip():
        raise PatchError("Patch text is empty")
    normalized = patch_text.strip("\n")
    targets: List[str] = []
    if _APPLY_PATCH_BEGIN in normalized.lower():
        normalized, targets = _convert_apply_patch_format(normalized)
    else:
        targets = _extract_patch_targets(normalized)
    if not targets and default_target:
        normalized_target = _normalize_target_path(default_target)
        header = f"--- a/{normalized_target}\n+++ b/{normalized_target}\n"
        if normalized and not normalized.startswith("@@"):
            normalized = header + "\n" + normalized
        else:
            normalized = header + normalized
        targets = [f"a/{normalized_target}"]
    if not targets:
        raise PatchError("Patch file missing file headers")
    if not normalized.endswith("\n"):
        normalized += "\n"
    return normalized, targets


def normalize_targets(targets: Iterable[str]) -> List[str]:
    return [_normalize_target_path(t) for t in targets if t and t != "/dev/null"]


def ensure_patch_targets_allowed(
    targets: Iterable[str], allowed: Iterable[str]
) -> List[str]:
    normalized = normalize_targets(targets)
    allowed_set = {path for path in allowed}
    unexpected = [path for path in normalized if path not in allowed_set]
    if unexpected:
        raise PatchError(f"Patch referenced unexpected files: {', '.join(unexpected)}")
    return normalized


def infer_patch_strip(targets: Iterable[str]) -> int:
    raw = list(targets)
    if raw and all(t.startswith(("a/", "b/")) for t in raw):
        return 1
    return 0


def _truncate_output(output: str) -> str:
    if len(output) <= _PATCH_STDOUT_LIMIT:
        return output
    return f"{output[:_PATCH_STDOUT_LIMIT]}...(truncated)"


def _run_patch(cmd: Sequence[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(cmd),
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=_PATCH_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise PatchError("patch command not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise PatchError("patch command timed out") from exc


def apply_patch_file(repo_root: Path, patch_path: Path, targets: Sequence[str]) -> None:
    strip = infer_patch_strip(targets)
    cmd = ["patch", f"-p{strip}", "--batch", "--quiet", "-i", str(patch_path)]
    proc = _run_patch(cmd, cwd=repo_root)
    if proc.returncode != 0:
        detail = _truncate_output((proc.stdout or "").strip())
        raise PatchError(detail or f"patch exited with {proc.returncode}")


def preview_patch(
    repo_root: Path,
    patch_text: str,
    targets: Sequence[str],
    *,
    base_content: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    strip = infer_patch_strip(targets)
    normalized_targets = normalize_targets(targets)
    with tempfile.TemporaryDirectory(prefix="car-patch-") as tmp_dir:
        root = Path(tmp_dir)
        for target in normalized_targets:
            source = repo_root / target
            dest = root / target
            dest.parent.mkdir(parents=True, exist_ok=True)
            if base_content and target in base_content:
                dest.write_text(base_content[target], encoding="utf-8")
                continue
            if source.exists():
                dest.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            else:
                dest.write_text("", encoding="utf-8")
        patch_file = root / "preview.patch"
        patch_payload = patch_text if patch_text.endswith("\n") else patch_text + "\n"
        patch_file.write_text(patch_payload, encoding="utf-8")
        cmd = ["patch", f"-p{strip}", "--batch", "--quiet", "-i", str(patch_file)]
        proc = _run_patch(cmd, cwd=root)
        if proc.returncode != 0:
            detail = _truncate_output((proc.stdout or "").strip())
            raise PatchError(detail or f"patch exited with {proc.returncode}")
        results: dict[str, str] = {}
        for target in normalized_targets:
            dest = root / target
            if dest.exists():
                results[target] = dest.read_text(encoding="utf-8")
            else:
                results[target] = ""
        return results


__all__ = [
    "PatchError",
    "apply_patch_file",
    "ensure_patch_targets_allowed",
    "infer_patch_strip",
    "normalize_patch_text",
    "normalize_targets",
    "preview_patch",
]
