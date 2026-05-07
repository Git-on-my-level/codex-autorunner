from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_CORPUS_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "runtime_lifecycle_sequences.json"
)


@lru_cache(maxsize=1)
def load_runtime_lifecycle_sequences() -> list[dict[str, Any]]:
    loaded = json.loads(_CORPUS_PATH.read_text(encoding="utf-8"))
    if not isinstance(loaded, list):
        raise TypeError("Runtime lifecycle sequence corpus must be a JSON array")
    return [dict(item) for item in loaded]


def runtime_lifecycle_sequence_ids() -> list[str]:
    return [str(case["name"]) for case in load_runtime_lifecycle_sequences()]
