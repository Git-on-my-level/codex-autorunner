from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DEFAULT_REPORT_MAX_HISTORY_FILES = 20
DEFAULT_REPORT_MAX_TOTAL_BYTES = 5 * 1024 * 1024  # 5 MiB
DEFAULT_REPORT_STABLE_PREFIXES: tuple[str, ...] = ("latest-",)


@dataclass(frozen=True)
class PruneSummary:
    kept: int
    pruned: int
    bytes_before: int
    bytes_after: int


def prune_report_directory(
    report_dir: Path,
    *,
    max_history_files: int = DEFAULT_REPORT_MAX_HISTORY_FILES,
    max_total_bytes: int = DEFAULT_REPORT_MAX_TOTAL_BYTES,
    stable_prefixes: tuple[str, ...] = DEFAULT_REPORT_STABLE_PREFIXES,
) -> PruneSummary:
    """Prune report history deterministically while preserving stable outputs.

    Files with stable prefixes (for example `latest-*`) are always preserved.
    Remaining files are considered history and are pruned by:
    1) max history file count
    2) max total directory bytes
    """

    if not report_dir.exists() or not report_dir.is_dir():
        return PruneSummary(kept=0, pruned=0, bytes_before=0, bytes_after=0)

    entries: list[Path] = [p for p in report_dir.iterdir() if p.is_file()]
    sized: list[tuple[Path, int]] = []
    for path in entries:
        try:
            sized.append((path, path.stat().st_size))
        except OSError:
            continue

    bytes_before = sum(size for _, size in sized)

    stable: list[tuple[Path, int]] = []
    history: list[tuple[Path, int]] = []
    for path, size in sized:
        if path.name.startswith(stable_prefixes):
            stable.append((path, size))
        else:
            history.append((path, size))

    # Deterministic ordering: newest first by mtime, then by name.
    history.sort(
        key=lambda item: (
            item[0].stat().st_mtime_ns,
            item[0].name,
        ),
        reverse=True,
    )

    keep_history: list[tuple[Path, int]] = history[: max(0, max_history_files)]
    prune_history = history[max(0, max_history_files) :]

    kept_paths = {path for path, _ in stable + keep_history}
    bytes_after = sum(
        size for path, size in stable + keep_history if path in kept_paths
    )

    # If total bytes still exceeds limit, prune oldest keep_history first.
    if max_total_bytes >= 0 and bytes_after > max_total_bytes:
        keep_history_reversed = list(reversed(keep_history))
        for path, size in keep_history_reversed:
            if bytes_after <= max_total_bytes:
                break
            if path in kept_paths:
                kept_paths.remove(path)
                prune_history.append((path, size))
                bytes_after -= size

    pruned_count = 0
    for path, _ in prune_history:
        try:
            path.unlink(missing_ok=True)
            pruned_count += 1
        except OSError:
            continue

    final_kept = 0
    final_bytes = 0
    for path, size in sized:
        if path.exists():
            final_kept += 1
            final_bytes += size

    return PruneSummary(
        kept=final_kept,
        pruned=pruned_count,
        bytes_before=bytes_before,
        bytes_after=final_bytes,
    )
