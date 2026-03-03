from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from codex_autorunner.core.type_debt_ledger import (
    TypeDebtModuleSummary,
    TypeDebtOccurrence,
    TypeDebtSlice,
    build_bounded_slices,
    build_type_debt_ledger,
    collect_any_dict_occurrences,
    ledger_to_dict,
    render_markdown_report,
    summarize_occurrences,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_collect_any_dict_occurrences_detects_supported_spellings(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "src"
    source_root.mkdir()
    (source_root / "a.py").write_text(
        "from typing import Any, Dict\n"
        "x: Dict[str, Any] = {}\n"
        "y: dict[str, Any] = {}\n",
        encoding="utf-8",
    )
    (source_root / "b.py").write_text("x: dict[str, int] = {}\n", encoding="utf-8")

    occurrences = collect_any_dict_occurrences(source_root)

    assert [(occ.file, occ.line) for occ in occurrences] == [("a.py", 2), ("a.py", 3)]


def test_summarize_occurrences_orders_by_count_then_module() -> None:
    occurrences = [
        TypeDebtOccurrence(file="b.py", line=10, text="x: Dict[str, Any]"),
        TypeDebtOccurrence(file="a.py", line=2, text="x: Dict[str, Any]"),
        TypeDebtOccurrence(file="a.py", line=5, text="x: Dict[str, Any]"),
    ]

    summaries = summarize_occurrences(occurrences)

    assert summaries == [
        TypeDebtModuleSummary(module="a.py", count=2, lines=(2, 5)),
        TypeDebtModuleSummary(module="b.py", count=1, lines=(10,)),
    ]


def test_build_bounded_slices_respects_limits() -> None:
    summaries = [
        TypeDebtModuleSummary(module="config.py", count=6, lines=(1,)),
        TypeDebtModuleSummary(module="usage.py", count=5, lines=(1,)),
        TypeDebtModuleSummary(module="hub.py", count=4, lines=(1,)),
        TypeDebtModuleSummary(module="types.py", count=3, lines=(1,)),
    ]

    slices = build_bounded_slices(
        summaries,
        max_hotspots_per_slice=8,
        max_modules_per_slice=2,
    )

    assert slices == [
        TypeDebtSlice(index=1, modules=("config.py",), hotspot_count=6),
        TypeDebtSlice(index=2, modules=("usage.py",), hotspot_count=5),
        TypeDebtSlice(index=3, modules=("hub.py", "types.py"), hotspot_count=7),
    ]


def test_build_type_debt_ledger_and_markdown_report(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    source_root.mkdir()
    (source_root / "alpha.py").write_text(
        "from typing import Any, Dict\n"
        "first: Dict[str, Any] = {}\n"
        "second: Dict[str, Any] = {}\n",
        encoding="utf-8",
    )
    (source_root / "beta.py").write_text(
        "from typing import Any\n" "other: dict[str, Any] = {}\n",
        encoding="utf-8",
    )

    ledger = build_type_debt_ledger(
        source_root,
        max_hotspots_per_slice=2,
        max_modules_per_slice=1,
    )
    markdown = render_markdown_report(ledger)

    assert ledger.total_occurrences == 3
    assert "Total `Dict[str, Any]` / `dict[str, Any]` hotspots: **3**" in markdown
    assert "| 1 | `alpha.py` | 2 | 2, 3 |" in markdown
    assert "1. 2 hotspots: `alpha.py`" in markdown
    assert "2. 1 hotspots: `beta.py`" in markdown


def test_type_debt_ledger_script_emits_json(tmp_path: Path) -> None:
    source_root = tmp_path / "scan"
    source_root.mkdir()
    (source_root / "example.py").write_text(
        "from typing import Any, Dict\n" "payload: Dict[str, Any] = {}\n",
        encoding="utf-8",
    )

    script_path = _repo_root() / "scripts" / "type_debt_ledger.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script_path),
            "--root",
            str(source_root),
            "--format",
            "json",
        ],
        cwd=_repo_root(),
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload == ledger_to_dict(
        build_type_debt_ledger(
            source_root,
            max_hotspots_per_slice=45,
            max_modules_per_slice=4,
        )
    )
