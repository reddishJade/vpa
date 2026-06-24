"""Configured validation command runner."""

from __future__ import annotations

import subprocess
from pathlib import Path

from vpa.orchestrator.models import (
    ValidationCommandResult,
    ValidationResult,
    ValidationStatus,
)


def run_validation(repo: str | Path, commands: list[str]) -> ValidationResult:
    results: list[ValidationCommandResult] = []
    for command in commands:
        completed = subprocess.run(
            command,
            cwd=repo,
            shell=True,
            capture_output=True,
            text=True,
        )
        status = ValidationStatus.PASSED if completed.returncode == 0 else ValidationStatus.FAILED
        results.append(
            ValidationCommandResult(
                command=command,
                status=status,
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
        )
        if status == ValidationStatus.FAILED:
            return ValidationResult(status=ValidationStatus.FAILED, commands=results)
    if not results:
        return ValidationResult(status=ValidationStatus.NOT_RUN, commands=[])
    return ValidationResult(status=ValidationStatus.PASSED, commands=results)

