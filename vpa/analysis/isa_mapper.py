"""Path-only ISA mapper for reference-to-target dynarec files."""

from __future__ import annotations

from pathlib import Path

from vpa.orchestrator.models import (
    DiffContext,
    FileMapping,
    MappingResult,
    MappingStatus,
)

DEFAULT_REFERENCE_ISA_PATH = Path("src/dynarec/rv64")
DEFAULT_TARGET_ISA_PATH = Path("src/dynarec/sw64_core3")


def map_reference_files(
    diff_context: DiffContext,
    local_repo: str | Path | None = None,
    reference_isa_path: Path = DEFAULT_REFERENCE_ISA_PATH,
    target_isa_path: Path = DEFAULT_TARGET_ISA_PATH,
) -> MappingResult:
    root = Path(local_repo) if local_repo is not None else None
    mappings: list[FileMapping] = []
    unmapped: list[Path] = []

    for file_diff in diff_context.files:
        path = file_diff.path
        if path is None:
            continue
        mapping = map_reference_path(path, root, reference_isa_path, target_isa_path)
        mappings.append(mapping)
        if mapping.status != MappingStatus.MAPPED:
            unmapped.append(path)

    return MappingResult(file_mappings=mappings, unmapped_reference_files=unmapped)


def map_reference_path(
    path: Path,
    local_repo: Path | None = None,
    reference_isa_path: Path = DEFAULT_REFERENCE_ISA_PATH,
    target_isa_path: Path = DEFAULT_TARGET_ISA_PATH,
) -> FileMapping:
    try:
        relative = path.relative_to(reference_isa_path)
    except ValueError:
        return FileMapping(path, [], MappingStatus.NOT_REFERENCE_FILE)

    mapped_name = _map_filename(relative.name)
    target = target_isa_path / relative.with_name(mapped_name)
    if local_repo is None:
        return FileMapping(path, [target], MappingStatus.MAPPED)

    full_target = local_repo / target
    if full_target.exists():
        return FileMapping(path, [target], MappingStatus.MAPPED)
    return FileMapping(path, [target], MappingStatus.MISSING_TARGET)


def _map_filename(filename: str) -> str:
    if filename.startswith("dynarec_rv64_"):
        return filename.replace("dynarec_rv64_", "dynarec_sw64_", 1)
    if filename.startswith("rv64_"):
        return filename.replace("rv64_", "sw64_", 1)
    return filename

