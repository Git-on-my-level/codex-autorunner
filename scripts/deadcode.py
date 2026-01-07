#!/usr/bin/env python3
"""
Heuristic dead-code scanner for this repo (stdlib-only).

Goal: help us prune tech debt over time by flagging *likely* unused symbols.

Design notes:
- Python: scans module-level defs/classes in src/, counts NAME-token references across src/.
  Skips decorated defs/classes (FastAPI/Typer/etc often register via decorators).
- JS: scans for named `function foo(` declarations in src/**/static/**/*.js,
  counts textual references across static JS + static HTML (for inline handlers).

This is intentionally conservative: it prefers false negatives over false positives.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import tokenize
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = REPO_ROOT / ".deadcode-baseline.json"


@dataclass(frozen=True)
class Finding:
    lang: str
    symbol: str
    file: str
    line: int
    kind: str


def _iter_files(root: Path, patterns: Sequence[str]) -> Iterable[Path]:
    for pat in patterns:
        yield from root.rglob(pat)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


# -------------------------
# Python scanning (AST + tokenize)
# -------------------------


def _python_name_token_counts(py_files: Sequence[Path]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for p in py_files:
        try:
            src = p.read_text(encoding="utf-8")
        except Exception:
            continue
        try:
            toks = tokenize.generate_tokens(iter(src.splitlines(True)).__next__)
            for tok_type, tok_str, *_ in toks:
                if tok_type == tokenize.NAME:
                    counts[tok_str] = counts.get(tok_str, 0) + 1
        except tokenize.TokenError:
            # malformed file; ignore
            continue
        try:
            tree = ast.parse(src, filename=str(p))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            for part in node.values:
                if isinstance(part, ast.FormattedValue):
                    for sub in ast.walk(part.value):
                        if isinstance(sub, ast.Name):
                            counts[sub.id] = counts.get(sub.id, 0) + 1
                        elif isinstance(sub, ast.Attribute):
                            counts[sub.attr] = counts.get(sub.attr, 0) + 1
    return counts


def _python_module_level_defs(py_path: Path) -> List[Finding]:
    src = _read_text(py_path)
    try:
        tree = ast.parse(src, filename=str(py_path))
    except SyntaxError:
        return []

    findings: List[Finding] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.decorator_list:
                continue
            findings.append(
                Finding(
                    lang="python",
                    symbol=node.name,
                    file=str(py_path.relative_to(REPO_ROOT)),
                    line=getattr(node, "lineno", 1),
                    kind="function",
                )
            )
        elif isinstance(node, ast.ClassDef):
            if node.decorator_list:
                continue
            findings.append(
                Finding(
                    lang="python",
                    symbol=node.name,
                    file=str(py_path.relative_to(REPO_ROOT)),
                    line=getattr(node, "lineno", 1),
                    kind="class",
                )
            )
    return findings


def scan_python(src_root: Path) -> List[Finding]:
    py_files = sorted(
        [
            p
            for p in _iter_files(src_root, ("*.py",))
            if p.is_file() and "__pycache__" not in p.parts
        ]
    )
    token_counts = _python_name_token_counts(py_files)

    suspects: List[Finding] = []
    for p in py_files:
        for d in _python_module_level_defs(p):
            # If the name token appears only once across src/ (the definition), it's likely unused.
            # (Recursive calls, references from other modules, __all__, etc. all bump the count.)
            if token_counts.get(d.symbol, 0) <= 1:
                suspects.append(d)
    return suspects


# -------------------------
# JS scanning (regex + string search)
# -------------------------


_JS_BLOCK_COMMENT_RE = re.compile(r"/\*[\s\S]*?\*/")
_JS_LINE_COMMENT_RE = re.compile(r"//[^\n]*")


def _strip_js_comments(text: str) -> str:
    text = _JS_BLOCK_COMMENT_RE.sub("", text)
    text = _JS_LINE_COMMENT_RE.sub("", text)
    return text


_JS_NAMED_FUNCTION_RE = re.compile(r"(?m)^\s*function\s+([A-Za-z_$][\w$]*)\s*\(")


def _js_function_defs(js_path: Path) -> List[Finding]:
    txt = _strip_js_comments(_read_text(js_path))
    out: List[Finding] = []
    for m in _JS_NAMED_FUNCTION_RE.finditer(txt):
        name = m.group(1)
        # Compute (1-based) line number from match start.
        line = txt.count("\n", 0, m.start()) + 1
        out.append(
            Finding(
                lang="javascript",
                symbol=name,
                file=str(js_path.relative_to(REPO_ROOT)),
                line=line,
                kind="function",
            )
        )
    return out


def _js_text_corpus(static_root: Path) -> str:
    parts: List[str] = []
    for p in sorted(_iter_files(static_root, ("*.js", "*.html"))):
        if not p.is_file():
            continue
        if "vendor" in p.parts:
            continue
        txt = _read_text(p)
        if p.suffix == ".js":
            txt = _strip_js_comments(txt)
        parts.append(txt)
    return "\n".join(parts)


def scan_js(static_root: Path) -> List[Finding]:
    js_files = sorted(
        [
            p
            for p in _iter_files(static_root, ("*.js",))
            if p.is_file() and "vendor" not in p.parts
        ]
    )
    corpus = _js_text_corpus(static_root)

    suspects: List[Finding] = []
    for p in js_files:
        for d in _js_function_defs(p):
            # Count word-boundary occurrences in JS+HTML corpus.
            # Definition itself counts once; >1 suggests used somewhere.
            occ = len(re.findall(rf"\b{re.escape(d.symbol)}\b", corpus))
            if occ <= 1:
                suspects.append(d)
    return suspects


# -------------------------
# Baseline + reporting
# -------------------------


def _finding_key(f: Finding) -> str:
    return f"{f.lang}:{f.kind}:{f.file}:{f.line}:{f.symbol}"


def _load_baseline(path: Path) -> Set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    if isinstance(data, dict) and isinstance(data.get("findings"), list):
        return set(str(x) for x in data["findings"])
    if isinstance(data, list):
        return set(str(x) for x in data)
    return set()


def _write_baseline(path: Path, keys: Sequence[str]) -> None:
    payload = {"findings": sorted(keys)}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _print_findings(findings: Sequence[Finding], heading: str) -> None:
    if not findings:
        print(f"{heading}: none")
        return
    print(f"{heading}: {len(findings)}")
    for f in sorted(findings, key=lambda x: (x.lang, x.file, x.line, x.symbol)):
        print(f"  - {f.lang} {f.kind} {f.symbol}  ({f.file}:{f.line})")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Heuristic dead-code scan (Python + static JS).")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE, help="Baseline JSON path.")
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="Write current findings to baseline and exit 0.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if new findings not present in baseline exist.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero if any findings exist (ignores baseline).",
    )
    args = parser.parse_args(argv)

    py_src = REPO_ROOT / "src"
    static_root = REPO_ROOT / "src" / "codex_autorunner" / "static"

    findings = scan_python(py_src) + scan_js(static_root)

    keys = [_finding_key(f) for f in findings]

    if args.update_baseline:
        _write_baseline(args.baseline, keys)
        print(f"Wrote baseline with {len(keys)} findings to {args.baseline}")
        return 0

    baseline = _load_baseline(args.baseline)
    new_keys = [k for k in keys if k not in baseline]
    new_findings = [f for f in findings if _finding_key(f) in set(new_keys)]

    _print_findings(new_findings, "New likely-dead code (not in baseline)")

    if args.strict:
        _print_findings(findings, "All likely-dead code (strict)")
        return 1 if findings else 0

    if args.check:
        return 1 if new_findings else 0

    # Default: informational run, don’t fail.
    if args.baseline.exists():
        _print_findings(findings, "All likely-dead code (including baseline)")
    else:
        print("No baseline found; run with --update-baseline to create one.")
        _print_findings(findings, "All likely-dead code (unbaselined)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
