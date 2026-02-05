from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.filesystem import copy_path


def test_copy_path_file(tmp_path: Path) -> None:
    """Test copy_path copies a file correctly."""
    src_file = tmp_path / "source.txt"
    src_file.write_text("Hello, world!", encoding="utf-8")

    dst_file = tmp_path / "subdir" / "dest.txt"

    copy_path(src_file, dst_file)

    assert dst_file.exists()
    assert dst_file.read_text(encoding="utf-8") == "Hello, world!"


def test_copy_path_directory(tmp_path: Path) -> None:
    """Test copy_path copies a directory recursively."""
    src_dir = tmp_path / "source_dir"
    src_dir.mkdir()
    (src_dir / "file1.txt").write_text("file 1", encoding="utf-8")
    (src_dir / "file2.txt").write_text("file 2", encoding="utf-8")
    (src_dir / "nested" / "file3.txt").parent.mkdir(parents=True)
    (src_dir / "nested" / "file3.txt").write_text("file 3", encoding="utf-8")

    dst_dir = tmp_path / "dest_dir"

    copy_path(src_dir, dst_dir)

    assert dst_dir.exists()
    assert dst_dir.is_dir()
    assert (dst_dir / "file1.txt").read_text(encoding="utf-8") == "file 1"
    assert (dst_dir / "file2.txt").read_text(encoding="utf-8") == "file 2"
    assert (dst_dir / "nested" / "file3.txt").read_text(encoding="utf-8") == "file 3"
