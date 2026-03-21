from __future__ import annotations

import re
from pathlib import Path

from codex_autorunner.core.filebox import BOXES


def test_filebox_catalog_matches_frontend_source() -> None:
    source = Path("src/codex_autorunner/static_src/fileboxCatalog.ts").read_text(
        encoding="utf-8"
    )
    match = re.search(r"FILEBOX_BOXES\s*=\s*\[(.*?)\]", source, re.S)
    assert match is not None
    boxes = tuple(re.findall(r'"([^"]+)"', match.group(1)))
    assert boxes == BOXES
