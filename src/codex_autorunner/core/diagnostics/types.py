from __future__ import annotations

from typing import Optional


class DoctorCheck:
    """Health check result."""

    def __init__(
        self,
        name: str,
        passed: bool,
        message: str,
        severity: str = "error",
        check_id: Optional[str] = None,
        fix: Optional[str] = None,
    ):
        self.name = name
        self.passed = passed
        self.message = message
        self.severity = severity
        self.check_id = check_id
        self.status = "ok" if passed else "error"
        self.fix = fix

    def __repr__(self) -> str:
        status = "✓" if self.passed else "✗"
        return f"{status} {self.name}: {self.message}"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "passed": self.passed,
            "message": self.message,
            "severity": self.severity,
            "check_id": self.check_id,
            "status": self.status,
            "fix": self.fix,
        }


class DoctorReport:
    """Report from running health checks."""

    def __init__(self, checks: list[DoctorCheck]):
        self.checks = checks

    @property
    def all_passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def has_errors(self) -> bool:
        return any(check.status == "error" for check in self.checks)

    def to_dict(self) -> dict:
        return {
            "ok": sum(1 for check in self.checks if check.status == "ok"),
            "warnings": sum(1 for check in self.checks if check.status == "warning"),
            "errors": sum(1 for check in self.checks if check.status == "error"),
            "checks": [check.to_dict() for check in self.checks],
        }

    def print_report(self) -> None:
        for check in self.checks:
            if check.severity == "error":
                print(check)
        for check in self.checks:
            if check.severity == "warning":
                print(check)
        for check in self.checks:
            if check.passed and check.severity != "info":
                print(check)


__all__ = ["DoctorCheck", "DoctorReport"]
