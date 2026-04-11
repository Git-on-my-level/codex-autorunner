from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_CORPUS_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "acp_lifecycle_corpus.json"
)


@lru_cache(maxsize=1)
def load_acp_lifecycle_corpus() -> list[dict[str, Any]]:
    loaded = json.loads(_CORPUS_PATH.read_text(encoding="utf-8"))
    if not isinstance(loaded, list):
        raise TypeError("ACP lifecycle corpus must be a JSON array")
    return [dict(item) for item in loaded]


def acp_lifecycle_case_ids() -> list[str]:
    return [str(case["name"]) for case in load_acp_lifecycle_corpus()]
