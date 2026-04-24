from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load_deadcode_module() -> ModuleType:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "deadcode.py"
    spec = importlib.util.spec_from_file_location("test_deadcode_script", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_python_name_token_counts_counts_top_level_all_exports(tmp_path: Path) -> None:
    module = _load_deadcode_module()
    module.REPO_ROOT = tmp_path
    source = tmp_path / "src" / "example.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def exported_symbol():\n"
        "    return None\n\n"
        "__all__ = ['exported_symbol']\n",
        encoding="utf-8",
    )

    counts = module._python_name_token_counts([source])

    assert counts["exported_symbol"] >= 2


def test_scan_python_does_not_flag_top_level_all_exports(tmp_path: Path) -> None:
    module = _load_deadcode_module()
    module.REPO_ROOT = tmp_path
    src_root = tmp_path / "src"
    src_root.mkdir()
    (src_root / "example.py").write_text(
        "def exported_symbol():\n"
        "    return None\n\n"
        "__all__ = ['exported_symbol']\n",
        encoding="utf-8",
    )

    findings = module.scan_python(src_root)

    assert all(finding.symbol != "exported_symbol" for finding in findings)
