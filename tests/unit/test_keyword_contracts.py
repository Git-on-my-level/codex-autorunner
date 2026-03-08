import subprocess
import sys
from pathlib import Path

import pytest

from codex_autorunner.core.utils import is_within


class TestIsWithinSemantics:
    def test_target_within_root_returns_true(self, tmp_path: Path):
        root = tmp_path / "root"
        root.mkdir()
        target = root / "subdir" / "file.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()
        assert is_within(root=root, target=target) is True

    def test_target_not_within_root_returns_false(self, tmp_path: Path):
        root = tmp_path / "root"
        other = tmp_path / "other"
        root.mkdir()
        other.mkdir()
        target = other / "file.txt"
        target.touch()
        assert is_within(root=root, target=target) is False

    def test_root_equals_target_returns_true(self, tmp_path: Path):
        root = tmp_path / "root"
        root.mkdir()
        assert is_within(root=root, target=root) is True

    def test_target_is_parent_of_root_returns_false(self, tmp_path: Path):
        parent = tmp_path
        child = parent / "child"
        child.mkdir()
        assert is_within(root=child, target=parent) is False

    def test_reversed_arguments_would_be_wrong(self, tmp_path: Path):
        root = tmp_path / "root"
        root.mkdir()
        target = root / "subdir"
        target.mkdir()
        assert is_within(root=root, target=target) is True
        assert is_within(root=target, target=root) is False


class TestIsWithinKeywordOnly:
    def test_positional_call_raises_type_error(self, tmp_path: Path):
        root = tmp_path / "root"
        root.mkdir()
        target = root / "file.txt"
        target.touch()
        with pytest.raises(TypeError, match="positional argument"):
            is_within(root, target)

    def test_missing_root_raises_type_error(self, tmp_path: Path):
        target = tmp_path / "file.txt"
        target.touch()
        with pytest.raises(TypeError):
            is_within(target=target)

    def test_missing_target_raises_type_error(self, tmp_path: Path):
        root = tmp_path
        with pytest.raises(TypeError):
            is_within(root=root)


class TestKeywordContractChecker:
    def test_checker_script_exists(self):
        script_path = (
            Path(__file__).parent.parent.parent
            / "scripts"
            / "check_keyword_contracts.py"
        )
        assert script_path.exists()

    def test_checker_finds_no_violations(self):
        script_path = (
            Path(__file__).parent.parent.parent
            / "scripts"
            / "check_keyword_contracts.py"
        )
        result = subprocess.run(
            [sys.executable, str(script_path), "--report-only"],
            capture_output=True,
            text=True,
        )
        assert (
            result.returncode == 0
        ), f"Checker found violations:\n{result.stdout}\n{result.stderr}"
