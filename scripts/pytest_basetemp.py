#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (_REPO_ROOT / "src", _REPO_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from tests.support.hermetic_roots import HermeticTestRoots


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Print the repo-scoped pytest --basetemp path."
    )
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args()

    roots = HermeticTestRoots.from_repo_root(Path(args.repo_root))
    roots.pytest_basetemp_root.parent.mkdir(parents=True, exist_ok=True)
    print(roots.pytest_basetemp_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
