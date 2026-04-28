from __future__ import annotations

import sys

from _autooptimize import build_paths, validate_state


def main() -> int:
    paths = build_paths()
    errors = validate_state(paths)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print("autooptimize state is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
