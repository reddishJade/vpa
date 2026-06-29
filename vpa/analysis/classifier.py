"""Commit classifier for routing workflow decisions."""

from __future__ import annotations

import re
from pathlib import Path

from vpa.orchestrator.models import (
    ClassifiedCommit,
    CommitClass,
    ConditionalClass,
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
    file_conditionals: dict[Path, ConditionalClass] | None = None,
) -> ClassifiedCommit:
    file_classes: dict[Path, CommitClass] = {}
    conditionals = dict(file_conditionals or {})
    for file_diff in diff_context.files:
        path = file_diff.path
        if path is None:
            continue
        file_class = classify_path(path, reference_isa_path, target_isa_path)
        conditional = conditionals.get(path, ConditionalClass.NONE)
        upgraded = _upgrade_shared_by_conditional(file_class, conditional)
        if upgraded != file_class:
            conditionals[path] = conditional
        file_classes[path] = upgraded

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

    if any(c == ConditionalClass.RV64_ONLY for c in conditionals.values()):
        reasons.append("at least one shared file change is inside an RV64-only block")

    return ClassifiedCommit(
        kind=kind,
        file_classes=file_classes,
        reasons=reasons,
        file_conditionals=conditionals,
    )


def _upgrade_shared_by_conditional(
    file_class: CommitClass,
    conditional: ConditionalClass,
) -> CommitClass:
    if file_class != CommitClass.SHARED_CODE:
        return file_class
    if conditional in {
        ConditionalClass.RV64_ONLY,
        ConditionalClass.SW64_ONLY,
        ConditionalClass.NOT_RV64,
    }:
        return CommitClass.CROSS_CUTTING
    return file_class


def classify_path(
    path: Path,
    reference_isa_path: Path = DEFAULT_REFERENCE_ISA_PATH,
    target_isa_path: Path = DEFAULT_TARGET_ISA_PATH,
) -> CommitClass:
    if is_generated_or_vendor_path(path):
        return CommitClass.GENERATED_OR_VENDOR
    if _is_relative_to(path, reference_isa_path):
        return CommitClass.REFERENCE_ISA_CHANGE
    if _is_relative_to(path, target_isa_path):
        return CommitClass.TARGET_ISA_DIRECT
    normalized = _as_posix(path)
    if normalized.startswith("src/") or normalized.startswith("tests/"):
        return CommitClass.SHARED_CODE
    return CommitClass.UNKNOWN


def is_generated_or_vendor_path(path: Path) -> bool:
    normalized = _as_posix(path)
    return any(marker in normalized.split("/") for marker in GENERATED_MARKERS)


def without_generated_files(diff_context: DiffContext) -> DiffContext:
    """Return a new DiffContext with generated/vendor files removed."""
    kept = [
        file_diff
        for file_diff in diff_context.files
        if file_diff.path is None or not is_generated_or_vendor_path(file_diff.path)
    ]
    return DiffContext(
        commit=diff_context.commit,
        raw_patch=diff_context.raw_patch,
        files=kept,
    )


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


def upgrade_conflict_by_content(
    path: Path,
    category: ConflictCategory,
    content: str | None = None,
) -> ConflictCategory:
    """If the file is a SOURCE conflict but contains RV64/SW64
    preprocessor conditionals, upgrade to ISA_BACKEND so it is
    recorded and deferred to human review instead of auto-resolved."""
    if category != ConflictCategory.SOURCE:
        return category
    if content is None:
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return category
    if _has_isa_conditional(content):
        return ConflictCategory.ISA_BACKEND
    return category


_ISA_CONDITIONAL_RE = re.compile(
    r'#\s*(?:if|ifdef|elif)\b.*\b(RV64|SW64)\b'
)


def _has_isa_conditional(content: str) -> bool:
    return bool(_ISA_CONDITIONAL_RE.search(content))


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _as_posix(path: Path) -> str:
    return path.as_posix().strip("/")

