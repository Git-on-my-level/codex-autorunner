import ast
from pathlib import Path


def _iter_core_modules() -> list[Path]:
    root = Path(__file__).resolve().parents[1]
    core_dir = root / "src" / "codex_autorunner" / "core"
    return [path for path in core_dir.rglob("*.py") if path.is_file()]


def _find_web_imports(path: Path) -> list[str]:
    offenders: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("codex_autorunner.web"):
                    offenders.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                continue
            if node.level == 0 and node.module.startswith("codex_autorunner.web"):
                offenders.append(node.module)
            if node.level >= 2 and (
                node.module == "web" or node.module.startswith("web.")
            ):
                offenders.append(f"{'.' * node.level}{node.module}")
    return offenders


def test_core_does_not_import_web() -> None:
    offenders = []
    for path in _iter_core_modules():
        web_imports = _find_web_imports(path)
        if web_imports:
            offenders.append((path, web_imports))
    assert not offenders, f"core modules should not import web: {offenders}"
