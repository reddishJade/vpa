"""Commit classifier for routing workflow decisions."""

from __future__ import annotations

from pathlib import Path

from vpa.orchestrator.models import (
    ClassifiedCommit,
    CommitClass,
    ConflictCategory,
    DiffContext,
)

DEFAULT_REFERENCE_ISA_PATH = Path("src/dynarec/rv64")
DEFAULT_TARGET_ISA_PATH = Path("src/dynarec/sw64_core3")
DEFAULT_REFERENCE_ISA_PATHS = [
    DEFAULT_REFERENCE_ISA_PATH,
    Path("src/dynarec/arm64"),
    Path("src/dynarec/la64"),
]
GENERATED_MARKERS = ("generated", "vendor", "third_party", "external")


def classify_commit(
    diff_context: DiffContext,
    reference_isa_path: Path = DEFAULT_REFERENCE_ISA_PATH,
    target_isa_path: Path = DEFAULT_TARGET_ISA_PATH,
) -> ClassifiedCommit:
    file_classes: dict[Path, CommitClass] = {}
    for file_diff in diff_context.files:
        path = file_diff.path
        if path is None:
            continue
        file_classes[path] = classify_path(path, reference_isa_path, target_isa_path)

    unique = set(file_classes.values())
    reasons: list[str] = []
    if not unique:
        kind = CommitClass.UNKNOWN
        reasons.append("commit has no parsed file paths")
    elif unique == {CommitClass.GENERATED_OR_VENDOR}:
        kind = CommitClass.GENERATED_OR_VENDOR
        reasons.append("all files are generated/vendor paths")
    elif unique == {CommitClass.REFERENCE_ISA_CHANGE}:
        kind = CommitClass.REFERENCE_ISA_CHANGE
        reasons.append("all changed files are under the reference ISA path")
    elif unique == {CommitClass.TARGET_ISA_DIRECT}:
        kind = CommitClass.TARGET_ISA_DIRECT
        reasons.append("all changed files are under the target ISA path")
    elif unique == {CommitClass.SHARED_CODE}:
        kind = CommitClass.SHARED_CODE
        reasons.append("all changed files are shared code")
    elif CommitClass.UNKNOWN in unique:
        kind = CommitClass.UNKNOWN
        reasons.append("at least one file could not be classified")
    else:
        kind = CommitClass.CROSS_CUTTING
        reasons.append("commit spans multiple routing classes")

    return ClassifiedCommit(kind=kind, file_classes=file_classes, reasons=reasons)


def classify_path(
    path: Path,
    reference_isa_path: Path = DEFAULT_REFERENCE_ISA_PATH,
    target_isa_path: Path = DEFAULT_TARGET_ISA_PATH,
) -> CommitClass:
    normalized = _as_posix(path)
    if any(marker in normalized.split("/") for marker in GENERATED_MARKERS):
        return CommitClass.GENERATED_OR_VENDOR
    if _is_relative_to(path, reference_isa_path):
        return CommitClass.REFERENCE_ISA_CHANGE
    if _is_relative_to(path, target_isa_path):
        return CommitClass.TARGET_ISA_DIRECT
    if normalized.startswith("src/") or normalized.startswith("tests/"):
        return CommitClass.SHARED_CODE
    return CommitClass.UNKNOWN


def classify_conflict_file(
    path: Path,
    reference_isa_paths: list[Path] | None = None,
) -> ConflictCategory:
    ref_paths = reference_isa_paths or DEFAULT_REFERENCE_ISA_PATHS
    for ref in ref_paths:
        if _is_relative_to(path, ref):
            return ConflictCategory.ISA_BACKEND
    suffix = path.suffix.lower()
    name = path.name.lower()
    if (name in {"cmakelists.txt", "makefile"}
        or suffix in {".cmake", ".mk", ".md", ".yml", ".yaml", ".txt",
                      ".rst", ".json", ".toml", ".cfg", ".ini"}):
        return ConflictCategory.NON_SOURCE
    return ConflictCategory.SOURCE


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _as_posix(path: Path) -> str:
    return path.as_posix().strip("/")

