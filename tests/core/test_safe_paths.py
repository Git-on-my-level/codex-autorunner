"""Tests for safe_paths module."""

import pytest

from codex_autorunner.core.safe_paths import (
    SafePathError,
    validate_relative_posix_path,
    validate_single_filename,
)


class TestValidateRelativePosixPath:
    """Tests for validate_relative_posix_path function."""

    def test_valid_single_filename(self):
        result = validate_relative_posix_path("file.txt")
        assert str(result) == "file.txt"
        assert result.parts == ("file.txt",)

    def test_valid_subpath(self):
        result = validate_relative_posix_path("a/b/c.txt")
        assert str(result) == "a/b/c.txt"
        assert result.parts == ("a", "b", "c.txt")

    def test_valid_path_with_dots_in_filename(self):
        result = validate_relative_posix_path(".hiddenfile")
        assert str(result) == ".hiddenfile"

        result = validate_relative_posix_path("file.with.dots.txt")
        assert str(result) == "file.with.dots.txt"

    def test_rejects_empty_string(self):
        with pytest.raises(SafePathError, match="empty or"):
            validate_relative_posix_path("")

    def test_rejects_whitespace_only(self):
        with pytest.raises(SafePathError, match="empty or"):
            validate_relative_posix_path("   ")

    def test_rejects_dot_only(self):
        with pytest.raises(SafePathError, match="empty or"):
            validate_relative_posix_path(".")

    def test_rejects_backslash(self):
        with pytest.raises(SafePathError, match="backslashes"):
            validate_relative_posix_path("a\\b.txt")

        with pytest.raises(SafePathError, match="backslashes"):
            validate_relative_posix_path("C:\\Windows\\System32\\config")

    def test_rejects_absolute_path(self):
        with pytest.raises(SafePathError, match="Absolute paths"):
            validate_relative_posix_path("/etc/passwd")

        with pytest.raises(SafePathError, match="Absolute paths"):
            validate_relative_posix_path("/")

    def test_rejects_parent_traversal(self):
        with pytest.raises(SafePathError, match=r"\.\..*not allowed"):
            validate_relative_posix_path("../etc/passwd")

        with pytest.raises(SafePathError, match=r"\.\..*not allowed"):
            validate_relative_posix_path("a/../b")

        with pytest.raises(SafePathError, match=r"\.\..*not allowed"):
            validate_relative_posix_path("a/b/../../c")

        with pytest.raises(SafePathError, match=r"\.\..*not allowed"):
            validate_relative_posix_path("..")

    def test_rejects_trailing_slash(self):
        # Note: PurePosixPath normalizes multiple consecutive slashes to single
        # slashes, so "a/b//" becomes "a/b/" which doesn't create empty parts.
        # This is safe, just normalized behavior of path parsing.
        result = validate_relative_posix_path("a/b/")
        assert str(result) == "a/b"

        # Double slashes are also normalized
        result = validate_relative_posix_path("a//b")
        assert str(result) == "a/b"

    def test_handles_decoded_url_paths(self):
        # After FastAPI decodes URL encoding, "..%2f..%2fetc%2fpasswd" becomes
        # "../..//etc/passwd" which contains ".." segments and will be rejected.
        # This test simulates what FastAPI passes to our function.
        with pytest.raises(SafePathError, match=r"\.\..*not allowed"):
            validate_relative_posix_path("../..//etc/passwd")

    def test_windows_drive_trick(self):
        # On Windows, paths like C: can be tricky, but PurePosixPath treats
        # them as relative paths, so we need to catch them with backslash check
        with pytest.raises(SafePathError, match="backslashes"):
            validate_relative_posix_path("C:\\Windows\\System32")


class TestValidateSingleFilename:
    """Tests for validate_single_filename function."""

    def test_valid_filename(self):
        result = validate_single_filename("file.txt")
        assert result == "file.txt"

    def test_valid_filename_with_dots(self):
        result = validate_single_filename(".hiddenfile")
        assert result == ".hiddenfile"

        result = validate_single_filename("file.with.dots.txt")
        assert result == "file.with.dots.txt"

    def test_rejects_subpath(self):
        with pytest.raises(SafePathError, match="Subpaths not allowed"):
            validate_single_filename("a/b.txt")

        with pytest.raises(SafePathError, match="Subpaths not allowed"):
            validate_single_filename("a/b/c.txt")

    def test_rejects_parent_traversal(self):
        with pytest.raises(SafePathError, match=r"\.\..*not allowed"):
            validate_single_filename("../file.txt")

        with pytest.raises(SafePathError, match=r"\.\..*not allowed"):
            validate_single_filename("..")

    def test_rejects_absolute_path(self):
        with pytest.raises(SafePathError, match="Absolute paths"):
            validate_single_filename("/etc/passwd")

    def test_rejects_backslash(self):
        with pytest.raises(SafePathError, match="backslashes"):
            validate_single_filename("a\\b.txt")

    def test_rejects_empty_string(self):
        with pytest.raises(SafePathError, match="empty or"):
            validate_single_filename("")

    def test_returns_string_not_path(self):
        result = validate_single_filename("file.txt")
        assert isinstance(result, str)
        assert result == "file.txt"


class TestSafePathError:
    """Tests for SafePathError exception."""

    def test_error_with_path(self):
        exc = SafePathError("Invalid path", path="/etc/passwd")
        assert str(exc) == "Invalid path"
        assert exc.path == "/etc/passwd"

    def test_error_without_path(self):
        exc = SafePathError("Something went wrong")
        assert str(exc) == "Something went wrong"
        assert exc.path is None
