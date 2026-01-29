from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .update import _system_update_worker


def _build_logger(log_path: Path) -> logging.Logger:
    logger = logging.getLogger("codex_autorunner.system_update")
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    return logger


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run codex-autorunner update worker.")
    parser.add_argument("--repo-url", required=True)
    parser.add_argument("--repo-ref", default="main")
    parser.add_argument("--update-dir", required=True)
    parser.add_argument("--log-path", required=True)
    parser.add_argument("--target", default="both")
    parser.add_argument("--skip-checks", action="store_true")
    args = parser.parse_args(argv)

    update_dir = Path(args.update_dir).expanduser()
    log_path = Path(args.log_path).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = _build_logger(log_path)

    _system_update_worker(
        repo_url=args.repo_url,
        repo_ref=args.repo_ref,
        update_dir=update_dir,
        logger=logger,
        update_target=args.target,
        skip_checks=bool(args.skip_checks),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
