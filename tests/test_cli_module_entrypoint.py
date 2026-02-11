import runpy
import sys


def _run_module(argv: list[str]) -> int:
    original_argv = sys.argv[:]
    original_cli_module = sys.modules.pop("codex_autorunner.cli", None)
    try:
        sys.argv = argv[:]
        try:
            runpy.run_module("codex_autorunner.cli", run_name="__main__")
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 0
        else:
            code = 0
        return code
    finally:
        sys.argv = original_argv
        if original_cli_module is not None:
            sys.modules["codex_autorunner.cli"] = original_cli_module


def test_python_m_codex_autorunner_cli_help_prints_output(capsys):
    code = _run_module(["python -m codex_autorunner.cli", "--help"])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.strip() != ""
    assert "Usage:" in captured.out


def test_python_m_codex_autorunner_cli_version_prints_output(capsys):
    code = _run_module(["python -m codex_autorunner.cli", "--version"])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.strip() != ""
    assert captured.out.startswith("codex-autorunner ")
