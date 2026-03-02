from __future__ import annotations

import ast
import dataclasses
from pathlib import Path
from typing import Iterable, Sequence, Union, cast

AstSliceExpr = Union[ast.expr, ast.slice]


@dataclasses.dataclass(frozen=True)
class TypeDebtOccurrence:
    file: str
    line: int
    text: str


@dataclasses.dataclass(frozen=True)
class TypeDebtModuleSummary:
    module: str
    count: int
    lines: tuple[int, ...]


@dataclasses.dataclass(frozen=True)
class TypeDebtSlice:
    index: int
    modules: tuple[str, ...]
    hotspot_count: int


@dataclasses.dataclass(frozen=True)
class TypeDebtLedger:
    scanned_root: str
    total_occurrences: int
    module_summaries: tuple[TypeDebtModuleSummary, ...]
    slices: tuple[TypeDebtSlice, ...]


def _iter_python_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*.py")):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts:
            continue
        yield path


def _unwrap_index_node(node: AstSliceExpr) -> AstSliceExpr:
    index_type = getattr(ast, "Index", None)
    if index_type is not None and isinstance(node, index_type):
        return cast(AstSliceExpr, node.value)
    return node


def _node_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_any_dict_subscript(node: ast.Subscript) -> bool:
    container_name = _node_name(node.value)
    if container_name not in {"Dict", "dict"}:
        return False

    slice_node = _unwrap_index_node(node.slice)
    if not isinstance(slice_node, ast.Tuple) or len(slice_node.elts) != 2:
        return False

    key_name = _node_name(slice_node.elts[0])
    value_name = _node_name(slice_node.elts[1])
    return key_name == "str" and value_name == "Any"


def collect_any_dict_occurrences(root: Path) -> list[TypeDebtOccurrence]:
    if not root.exists():
        return []
    occurrences: list[TypeDebtOccurrence] = []
    for path in _iter_python_files(root):
        relative_path = str(path.relative_to(root))
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        lines = text.splitlines()
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Subscript) or not _is_any_dict_subscript(node):
                continue
            line_number = getattr(node, "lineno", 1)
            line_text = (
                lines[line_number - 1].strip() if line_number <= len(lines) else ""
            )
            occurrences.append(
                TypeDebtOccurrence(file=relative_path, line=line_number, text=line_text)
            )
    return sorted(occurrences, key=lambda occ: (occ.file, occ.line, occ.text))


def summarize_occurrences(
    occurrences: Sequence[TypeDebtOccurrence],
) -> list[TypeDebtModuleSummary]:
    by_module: dict[str, list[int]] = {}
    for occurrence in occurrences:
        by_module.setdefault(occurrence.file, []).append(occurrence.line)
    summaries = [
        TypeDebtModuleSummary(
            module=module,
            count=len(lines),
            lines=tuple(sorted(lines)),
        )
        for module, lines in by_module.items()
    ]
    return sorted(summaries, key=lambda summary: (-summary.count, summary.module))


def build_bounded_slices(
    summaries: Sequence[TypeDebtModuleSummary],
    *,
    max_hotspots_per_slice: int = 45,
    max_modules_per_slice: int = 4,
) -> list[TypeDebtSlice]:
    if max_hotspots_per_slice <= 0:
        raise ValueError("max_hotspots_per_slice must be positive")
    if max_modules_per_slice <= 0:
        raise ValueError("max_modules_per_slice must be positive")

    ordered = sorted(summaries, key=lambda summary: (-summary.count, summary.module))
    slices: list[TypeDebtSlice] = []
    current_modules: list[str] = []
    current_hotspots = 0

    for summary in ordered:
        would_exceed_hotspots = (
            bool(current_modules)
            and current_hotspots + summary.count > max_hotspots_per_slice
        )
        would_exceed_modules = (
            bool(current_modules) and len(current_modules) >= max_modules_per_slice
        )
        if would_exceed_hotspots or would_exceed_modules:
            slices.append(
                TypeDebtSlice(
                    index=len(slices) + 1,
                    modules=tuple(current_modules),
                    hotspot_count=current_hotspots,
                )
            )
            current_modules = []
            current_hotspots = 0

        current_modules.append(summary.module)
        current_hotspots += summary.count

    if current_modules:
        slices.append(
            TypeDebtSlice(
                index=len(slices) + 1,
                modules=tuple(current_modules),
                hotspot_count=current_hotspots,
            )
        )

    return slices


def build_type_debt_ledger(
    root: Path,
    *,
    max_hotspots_per_slice: int = 45,
    max_modules_per_slice: int = 4,
) -> TypeDebtLedger:
    occurrences = collect_any_dict_occurrences(root)
    summaries = summarize_occurrences(occurrences)
    slices = build_bounded_slices(
        summaries,
        max_hotspots_per_slice=max_hotspots_per_slice,
        max_modules_per_slice=max_modules_per_slice,
    )
    return TypeDebtLedger(
        scanned_root=str(root),
        total_occurrences=len(occurrences),
        module_summaries=tuple(summaries),
        slices=tuple(slices),
    )


def render_markdown_report(
    ledger: TypeDebtLedger,
    *,
    top_modules: int = 20,
) -> str:
    lines: list[str] = [
        "# Typed Debt Ledger",
        "",
        f"Scanned root: `{ledger.scanned_root}`",
        "",
        f"Total `Dict[str, Any]` / `dict[str, Any]` hotspots: **{ledger.total_occurrences}**",
        "",
    ]
    if not ledger.module_summaries:
        lines.extend(
            [
                "No hotspots found.",
                "",
            ]
        )
        return "\n".join(lines)

    lines.extend(
        [
            "## Priority Modules",
            "",
            "| Priority | Module | Hotspots | Representative lines |",
            "| --- | --- | ---: | --- |",
        ]
    )
    for index, summary in enumerate(ledger.module_summaries[:top_modules], start=1):
        representative = ", ".join(str(line) for line in summary.lines[:5])
        if len(summary.lines) > 5:
            representative = f"{representative}, ..."
        lines.append(
            f"| {index} | `{summary.module}` | {summary.count} | {representative} |"
        )
    lines.extend(
        [
            "",
            "## Recommended Bounded Slices",
            "",
        ]
    )
    for debt_slice in ledger.slices:
        modules = ", ".join(f"`{module}`" for module in debt_slice.modules)
        lines.append(
            f"{debt_slice.index}. {debt_slice.hotspot_count} hotspots: {modules}"
        )
    lines.append("")
    return "\n".join(lines)


def ledger_to_dict(ledger: TypeDebtLedger) -> dict[str, object]:
    return {
        "scanned_root": ledger.scanned_root,
        "total_occurrences": ledger.total_occurrences,
        "module_summaries": [
            {
                "module": summary.module,
                "count": summary.count,
                "lines": list(summary.lines),
            }
            for summary in ledger.module_summaries
        ],
        "slices": [
            {
                "index": debt_slice.index,
                "hotspot_count": debt_slice.hotspot_count,
                "modules": list(debt_slice.modules),
            }
            for debt_slice in ledger.slices
        ],
    }
