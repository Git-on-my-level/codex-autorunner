from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from ..bootstrap import seed_hub_files, seed_repo_files
from ..manifest import load_manifest
from .config import load_hub_config


@dataclass
class ManagedRepoRefreshSummary:
    hub_root: str
    refreshed_repo_ids: list[str] = field(default_factory=list)
    missing_repo_ids: list[str] = field(default_factory=list)

    @property
    def refreshed_count(self) -> int:
        return len(self.refreshed_repo_ids)

    @property
    def missing_count(self) -> int:
        return len(self.missing_repo_ids)

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["refreshed_count"] = self.refreshed_count
        payload["missing_count"] = self.missing_count
        return payload


def refresh_hub_managed_repos(
    hub_root: Path,
    *,
    force: bool = False,
) -> ManagedRepoRefreshSummary:
    resolved_root = hub_root.expanduser().resolve()
    hub_config = load_hub_config(resolved_root)
    manifest = load_manifest(hub_config.manifest_path, hub_config.root)

    seed_hub_files(hub_config.root, force=force)

    summary = ManagedRepoRefreshSummary(hub_root=str(hub_config.root))
    for repo in manifest.repos:
        repo_root = (hub_config.root / repo.path).resolve()
        if not repo_root.exists():
            summary.missing_repo_ids.append(repo.id)
            continue
        seed_repo_files(repo_root, force=force, git_required=False)
        summary.refreshed_repo_ids.append(repo.id)
    return summary


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Refresh generated CAR artifacts for a hub and its managed repos."
    )
    parser.add_argument(
        "--path",
        required=True,
        help="Hub root path.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force-refresh generated files even when current content matches.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a JSON summary.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    summary = refresh_hub_managed_repos(Path(args.path), force=bool(args.force))
    if args.json:
        print(json.dumps(summary.to_dict(), indent=2, sort_keys=True))
    else:
        print(
            "Refreshed %d managed repo(s)%s."
            % (
                summary.refreshed_count,
                (
                    f"; missing: {', '.join(summary.missing_repo_ids)}"
                    if summary.missing_repo_ids
                    else ""
                ),
            )
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
