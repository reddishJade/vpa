"""Shared models for the architecture-port workflow."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class DiffLineKind(StrEnum):
    CONTEXT = "context"
    ADDED = "added"
    REMOVED = "removed"


class FileStatus(StrEnum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"


class FileLanguage(StrEnum):
    C = "c"
    HEADER = "header"
    ASM = "asm"
    BUILD = "build"
    TEXT = "text"
    UNKNOWN = "unknown"


class CommitClass(StrEnum):
    SHARED_CODE = "shared_code"
    REFERENCE_ISA_CHANGE = "reference_isa_change"
    TARGET_ISA_DIRECT = "target_isa_direct"
    CROSS_CUTTING = "cross_cutting"
    GENERATED_OR_VENDOR = "generated_or_vendor"
    UNKNOWN = "unknown"


class MappingStatus(StrEnum):
    MAPPED = "mapped"
    MISSING_TARGET = "missing_target"
    AMBIGUOUS = "ambiguous"
    NOT_REFERENCE_FILE = "not_reference_file"


class ChangeKind(StrEnum):
    COMMENT_ONLY = "comment_only"
    FORMAT_ONLY = "format_only"
    METADATA_ONLY = "metadata_only"
    REFACTOR = "refactor"
    API_SHAPE_CHANGE = "api_shape_change"
    LOGIC_CHANGE = "logic_change"
    NEW_SYMBOL = "new_symbol"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class SignalSource(StrEnum):
    DIFF_TEXT = "diff_text"
    NORMALIZED = "normalized"
    SYMBOL_TEXT = "symbol_text"
    AST = "ast"


class GateDecisionKind(StrEnum):
    NO_TARGET_CHANGE = "no_target_change"
    NEEDS_VALIDATION_ONLY = "needs_validation_only"
    NEEDS_SEMANTIC_PORT = "needs_semantic_port"


class RiskPreference(StrEnum):
    CONSERVATIVE = "conservative"
    BALANCED = "balanced"
    AGGRESSIVE = "aggressive"


class ValidationStatus(StrEnum):
    NOT_RUN = "not_run"
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


class FailureCode(StrEnum):
    MAX_RETRIES = "max_retries"
    INTEGRITY_FAIL = "integrity_fail"
    LLM_ERROR = "llm_error"
    NO_LLM_CONFIGURED = "no_llm_configured"


@dataclass(frozen=True)
class AgentLoopResult:
    success: bool
    failure_code: FailureCode | None = None
    status_reason: str | None = None
    patched_files: list[Path] = field(default_factory=list)


class GitOperationStatus(StrEnum):
    NOT_RUN = "not_run"
    APPLIED = "applied"
    SKIPPED = "skipped"
    CONFLICT = "conflict"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class PromotionMethod(StrEnum):
    SKIP = "skip"
    CHERRY_PICK = "cherry_pick"
    PATH_LIMITED_APPLY_3WAY = "path_limited_apply_3way"
    SEMANTIC_PORT = "semantic_port"
    MERGE = "merge"


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    subject: str
    author: str | None = None
    author_date: str | None = None


@dataclass(frozen=True)
class DiffLine:
    kind: DiffLineKind
    text: str


@dataclass(frozen=True)
class DiffHunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    section: str | None
    lines: list[DiffLine] = field(default_factory=list)


@dataclass(frozen=True)
class FileDiff:
    path_before: Path | None
    path_after: Path | None
    status: FileStatus
    language: FileLanguage
    raw_patch: str
    hunks: list[DiffHunk] = field(default_factory=list)

    @property
    def path(self) -> Path | None:
        return self.path_after or self.path_before


@dataclass(frozen=True)
class DiffContext:
    commit: CommitInfo
    raw_patch: str
    files: list[FileDiff] = field(default_factory=list)


@dataclass(frozen=True)
class BaseCommitContext:
    commit: CommitInfo
    diff_context: DiffContext


@dataclass(frozen=True)
class ClassifiedCommit:
    kind: CommitClass
    file_classes: dict[Path, CommitClass]
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FileMapping:
    reference_file: Path
    target_candidates: list[Path]
    status: MappingStatus


@dataclass(frozen=True)
class MappingResult:
    file_mappings: list[FileMapping] = field(default_factory=list)
    unmapped_reference_files: list[Path] = field(default_factory=list)

    def mapping_for(self, reference_file: Path) -> FileMapping | None:
        for mapping in self.file_mappings:
            if mapping.reference_file == reference_file:
                return mapping
        return None


@dataclass(frozen=True)
class CommitContext:
    commit: CommitInfo
    diff_context: DiffContext
    classification: ClassifiedCommit
    isa_mapping: MappingResult


@dataclass(frozen=True)
class ChangeSignal:
    kind: ChangeKind
    source: SignalSource
    confidence: float
    reason: str
    file_path: Path | None = None
    symbol: str | None = None


@dataclass(frozen=True)
class ChangeAnalysis:
    kind: ChangeKind
    confidence: float
    signals: list[ChangeSignal]
    changed_symbols: list[str]
    mapped_target_candidates: list[Path]
    suggested_gate: GateDecisionKind


@dataclass(frozen=True)
class GatePolicy:
    risk_preference: RiskPreference = RiskPreference.BALANCED
    dry_run: bool = False
    project_overrides: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class GateDecision:
    kind: GateDecisionKind
    reasons: list[str]


@dataclass(frozen=True)
class ValidationCommandResult:
    command: str
    status: ValidationStatus
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class ValidationResult:
    status: ValidationStatus
    commands: list[ValidationCommandResult] = field(default_factory=list)


@dataclass(frozen=True)
class GitCommandResult:
    args: list[str]
    cwd: Path
    status: GitOperationStatus
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class GitApplyResult:
    status: GitOperationStatus
    method: PromotionMethod
    checkpoint: str | None = None
    command: GitCommandResult | None = None
    conflicts: list[Path] = field(default_factory=list)
    commit_sha: str | None = None


@dataclass(frozen=True)
class GitMergeResult:
    status: GitOperationStatus
    conflicts: list[Path] = field(default_factory=list)
    command: GitCommandResult | None = None
    commit_sha: str | None = None


@dataclass(frozen=True)
class TargetFileContext:
    path: Path
    content: str | None


@dataclass(frozen=True)
class SemanticPortContext:
    commit: CommitInfo
    reference_patches: dict[Path, str]
    target_files: list[TargetFileContext]
    analysis: ChangeAnalysis
    gate_reasons: list[str]


@dataclass(frozen=True)
class SemanticPortResult:
    patch_text: str | None
    context: SemanticPortContext
    llm_used: bool = False


@dataclass(frozen=True)
class MergeConflictResolution:
    resolved_files: list[Path] = field(default_factory=list)
    failed_files: list[Path] = field(default_factory=list)


@dataclass(frozen=True)
class LedgerRecord:
    commit: CommitInfo
    classification: CommitClass
    gate: GateDecisionKind
    changed_files: list[Path]
    method: PromotionMethod = PromotionMethod.SKIP
    apply_status: str = "not_run"
    apply_reason: str | None = None
    integrity_status: str = "not_run"
    validation_status: str = "not_run"
    llm_used: bool = False
