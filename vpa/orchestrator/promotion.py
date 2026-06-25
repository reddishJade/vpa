"""Promotion orchestrator for planning and mechanical Git execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from vpa.analysis.change_analyzer import analyze
from vpa.analysis.classifier import classify_commit
from vpa.analysis.isa_mapper import map_reference_files
from vpa.engines.git import GitEngine, render_patch
from vpa.engines.repair import RepairEngine
from vpa.engines.validation import run_validation
from vpa.ledger.store import LedgerStore
from vpa.orchestrator.llm_gate import decide
from vpa.orchestrator.models import (
    BaseCommitContext,
    ChangeAnalysis,
    CommitClass,
    CommitContext,
    GateDecision,
    GateDecisionKind,
    GatePolicy,
    GitApplyResult,
    GitOperationStatus,
    LedgerRecord,
    PromotionMethod,
    ValidationResult,
    ValidationStatus,
)


@dataclass(frozen=True)
class PromotionConfig:
    upstream_repo: Path
    local_repo: Path
    revision_range: str
    target_isa_path: Path = Path("src/dynarec/sw64_core3")
    primary_reference_isa_path: Path = Path("src/dynarec/rv64")
    fallback_reference_isa_paths: list[Path] = field(default_factory=list)
    build_command: str | None = None
    smoke_commands: list[str] = field(default_factory=list)
    dry_run: bool = False
    ledger_path: Path | None = None
    report_path: Path | None = None
    gate_policy: GatePolicy = field(default_factory=GatePolicy)


@dataclass(frozen=True)
class PlannedCommit:
    context: CommitContext
    analysis: ChangeAnalysis
    gate_decision: GateDecision


@dataclass(frozen=True)
class PromotionPlan:
    commits: list[PlannedCommit]


@dataclass(frozen=True)
class ExecutedCommit:
    planned: PlannedCommit
    method: PromotionMethod
    git_result: GitApplyResult | None
    validation: ValidationResult
    manual_item: str | None = None


@dataclass(frozen=True)
class PromotionRun:
    plan: PromotionPlan
    executed: list[ExecutedCommit]


class PromotionOrchestrator:
    def __init__(self, config: PromotionConfig, repair_engine: RepairEngine | None = None):
        self.config = config
        self.upstream_git = GitEngine(config.upstream_repo)
        self.local_git = GitEngine(config.local_repo)
        self.repair_engine = repair_engine or RepairEngine()

    def plan(self) -> PromotionPlan:
        planned: list[PlannedCommit] = []
        for sha in self.upstream_git.list_commits(self.config.revision_range):
            diff_context = self.upstream_git.read_diff_context(sha)
            base_context = BaseCommitContext(
                commit=diff_context.commit,
                diff_context=diff_context,
            )
            classification = classify_commit(
                base_context.diff_context,
                reference_isa_path=self.config.primary_reference_isa_path,
                target_isa_path=self.config.target_isa_path,
            )
            isa_mapping = map_reference_files(
                base_context.diff_context,
                local_repo=self.config.local_repo,
                reference_isa_path=self.config.primary_reference_isa_path,
                target_isa_path=self.config.target_isa_path,
            )
            context = CommitContext(
                commit=base_context.commit,
                diff_context=base_context.diff_context,
                classification=classification,
                isa_mapping=isa_mapping,
            )
            analysis = analyze(context.diff_context, context.isa_mapping)
            gate_decision = decide(analysis, self.config.gate_policy, context)
            planned.append(PlannedCommit(context, analysis, gate_decision))
        return PromotionPlan(commits=planned)

    def execute(self) -> PromotionRun:
        dirty_paths = self.local_git.tracked_changes()
        if dirty_paths:
            preview = ", ".join(path.as_posix() for path in dirty_paths[:8])
            suffix = "" if len(dirty_paths) <= 8 else f", ... ({len(dirty_paths)} total)"
            raise ValueError(
                "Local repo has tracked uncommitted changes; refusing to execute "
                f"because rollback uses git reset --hard: {preview}{suffix}"
            )
        plan = self.plan()
        ledger = LedgerStore(self.config.ledger_path) if self.config.ledger_path else None
        executed: list[ExecutedCommit] = []
        for planned in plan.commits:
            result = self._execute_commit(planned)
            executed.append(result)
            if ledger:
                ledger.append(_ledger_record(result))
        return PromotionRun(plan=plan, executed=executed)

    def _execute_commit(self, planned: PlannedCommit) -> ExecutedCommit:
        gate = planned.gate_decision.kind
        if gate == GateDecisionKind.NO_TARGET_CHANGE:
            return ExecutedCommit(
                planned=planned,
                method=PromotionMethod.SKIP,
                git_result=GitApplyResult(
                    status=GitOperationStatus.SKIPPED,
                    method=PromotionMethod.SKIP,
                ),
                validation=ValidationResult(ValidationStatus.NOT_RUN),
            )
        if gate == GateDecisionKind.NEEDS_MANUAL_REVIEW:
            return ExecutedCommit(
                planned=planned,
                method=PromotionMethod.MANUAL,
                git_result=GitApplyResult(
                    status=GitOperationStatus.SKIPPED,
                    method=PromotionMethod.MANUAL,
                ),
                validation=ValidationResult(ValidationStatus.NOT_RUN),
                manual_item="Manual review required by gate decision.",
            )
        if gate == GateDecisionKind.NEEDS_SEMANTIC_PORT:
            return self._execute_semantic_port(planned)

        checkpoint = self.local_git.checkpoint()
        method = _mechanical_method(planned)
        git_result = self._apply_mechanical_commit(planned, method)
        git_result = _with_checkpoint(git_result, checkpoint)
        if git_result.status != GitOperationStatus.APPLIED:
            self._rollback(checkpoint, git_result)
            return ExecutedCommit(
                planned=planned,
                method=method,
                git_result=_rolled_back(git_result),
                validation=ValidationResult(ValidationStatus.NOT_RUN),
                manual_item="Mechanical Git application failed; rolled back to checkpoint.",
            )

        validation = run_validation(self.config.local_repo, _validation_commands(self.config))
        if validation.status == ValidationStatus.FAILED:
            self._rollback(checkpoint, git_result)
            return ExecutedCommit(
                planned=planned,
                method=method,
                git_result=_rolled_back(git_result),
                validation=validation,
                manual_item=(
                    "Validation failed after mechanical application; "
                    "rolled back to checkpoint."
                ),
            )

        return ExecutedCommit(
            planned=planned,
            method=method,
            git_result=git_result,
            validation=validation,
        )

    def _execute_semantic_port(self, planned: PlannedCommit) -> ExecutedCommit:
        checkpoint = self.local_git.checkpoint()
        repair = self.repair_engine.semantic_port(
            planned.context,
            planned.analysis,
            planned.gate_decision,
            self.config.local_repo,
        )
        if repair.patch_text is None:
            return ExecutedCommit(
                planned=planned,
                method=PromotionMethod.SEMANTIC_PORT_PENDING,
                git_result=GitApplyResult(
                    status=GitOperationStatus.SKIPPED,
                    method=PromotionMethod.SEMANTIC_PORT_PENDING,
                    checkpoint=checkpoint,
                ),
                validation=ValidationResult(ValidationStatus.NOT_RUN),
                manual_item=repair.manual_item,
            )

        git_result = self.local_git.apply_patch_3way(
            repair.patch_text,
            method=PromotionMethod.SEMANTIC_PORT,
            commit_message=f"VPA semantic port {planned.context.commit.sha[:12]}",
        )
        git_result = _with_checkpoint(git_result, checkpoint)
        if git_result.status != GitOperationStatus.APPLIED:
            self._rollback(checkpoint, git_result)
            return ExecutedCommit(
                planned=planned,
                method=PromotionMethod.SEMANTIC_PORT,
                git_result=_rolled_back(git_result),
                validation=ValidationResult(ValidationStatus.NOT_RUN),
                manual_item="Semantic port patch failed to apply; rolled back to checkpoint.",
            )

        validation = run_validation(self.config.local_repo, _validation_commands(self.config))
        if validation.status == ValidationStatus.FAILED:
            self._rollback(checkpoint, git_result)
            return ExecutedCommit(
                planned=planned,
                method=PromotionMethod.SEMANTIC_PORT,
                git_result=_rolled_back(git_result),
                validation=validation,
                manual_item=(
                    "Validation failed after semantic port patch; "
                    "rolled back to checkpoint."
                ),
            )

        return ExecutedCommit(
            planned=planned,
            method=PromotionMethod.SEMANTIC_PORT,
            git_result=git_result,
            validation=validation,
        )

    def _apply_mechanical_commit(
        self,
        planned: PlannedCommit,
        method: PromotionMethod,
    ) -> GitApplyResult:
        if method == PromotionMethod.PATH_LIMITED_APPLY_3WAY:
            files = _path_limited_files(planned, self.config.target_isa_path)
            return self.local_git.apply_patch_3way(render_patch(files))
        return self.local_git.cherry_pick_from(
            self.config.upstream_repo,
            planned.context.commit.sha,
        )

    def _rollback(self, checkpoint: str, git_result: GitApplyResult) -> None:
        if (
            git_result.status == GitOperationStatus.CONFLICT
            and git_result.method == PromotionMethod.CHERRY_PICK
        ):
            self.local_git.abort_cherry_pick()
        self.local_git.reset_to_checkpoint(checkpoint)


def render_plan(plan: PromotionPlan) -> str:
    lines = ["VPA promotion plan", ""]
    for item in plan.commits:
        commit = item.context.commit
        lines.append(f"- {commit.sha[:12]} {commit.subject}")
        lines.append(f"  classification: {item.context.classification.kind}")
        lines.append(f"  change: {item.analysis.kind} confidence={item.analysis.confidence:.2f}")
        lines.append(f"  gate: {item.gate_decision.kind}")
    if not plan.commits:
        lines.append("(no commits)")
    return "\n".join(lines)


def render_run(run: PromotionRun) -> str:
    lines = [render_plan(run.plan), "", "VPA promotion execution", ""]
    for item in run.executed:
        commit = item.planned.context.commit
        git_status = item.git_result.status if item.git_result else GitOperationStatus.NOT_RUN
        lines.append(f"- {commit.sha[:12]} {commit.subject}")
        lines.append(f"  method: {item.method}")
        lines.append(f"  git: {git_status}")
        if item.git_result:
            if item.git_result.conflicts:
                conflicts = ", ".join(path.as_posix() for path in item.git_result.conflicts[:5])
                suffix = (
                    ""
                    if len(item.git_result.conflicts) <= 5
                    else f", ... ({len(item.git_result.conflicts)} total)"
                )
                lines.append(f"  conflicts: {conflicts}{suffix}")
            if item.git_result.command and item.git_result.command.returncode != 0:
                lines.append(f"  git_returncode: {item.git_result.command.returncode}")
                stderr = _first_nonempty_lines(item.git_result.command.stderr)
                if stderr:
                    lines.append("  git_stderr:")
                    lines.extend(f"    {line}" for line in stderr)
        lines.append(f"  validation: {item.validation.status}")
        if item.validation.status == ValidationStatus.FAILED:
            failed = next(
                (
                    command
                    for command in item.validation.commands
                    if command.status == ValidationStatus.FAILED
                ),
                None,
            )
            if failed:
                lines.append(f"  failed_command: {failed.command}")
                stderr = _first_nonempty_lines(failed.stderr)
                if stderr:
                    lines.append("  validation_stderr:")
                    lines.extend(f"    {line}" for line in stderr)
        if item.manual_item:
            lines.append(f"  manual: {item.manual_item}")
    if not run.executed:
        lines.append("(no commits executed)")
    return "\n".join(lines)


def _mechanical_method(planned: PlannedCommit) -> PromotionMethod:
    if planned.context.classification.kind == CommitClass.TARGET_ISA_DIRECT:
        return PromotionMethod.PATH_LIMITED_APPLY_3WAY
    return PromotionMethod.CHERRY_PICK


def _path_limited_files(planned: PlannedCommit, target_isa_path: Path):
    target_files = []
    for file_diff in planned.context.diff_context.files:
        path = file_diff.path
        if path and _is_under(path, target_isa_path):
            target_files.append(file_diff)
    return target_files or planned.context.diff_context.files


def _is_under(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _first_nonempty_lines(text: str, limit: int = 6) -> list[str]:
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[:limit]


def _validation_commands(config: PromotionConfig) -> list[str]:
    commands = []
    if config.build_command:
        commands.append(config.build_command)
    commands.extend(config.smoke_commands)
    return commands


def _with_checkpoint(result: GitApplyResult, checkpoint: str) -> GitApplyResult:
    return GitApplyResult(
        status=result.status,
        method=result.method,
        checkpoint=checkpoint,
        command=result.command,
        conflicts=result.conflicts,
        commit_sha=result.commit_sha,
    )


def _rolled_back(result: GitApplyResult) -> GitApplyResult:
    return GitApplyResult(
        status=GitOperationStatus.ROLLED_BACK,
        method=result.method,
        checkpoint=result.checkpoint,
        command=result.command,
        conflicts=result.conflicts,
        commit_sha=result.commit_sha,
    )


def _ledger_record(executed: ExecutedCommit) -> LedgerRecord:
    planned = executed.planned
    return LedgerRecord(
        commit=planned.context.commit,
        classification=planned.context.classification.kind,
        gate=planned.gate_decision.kind,
        changed_files=[
            file_diff.path
            for file_diff in planned.context.diff_context.files
            if file_diff.path is not None
        ],
        method=executed.method,
        git=executed.git_result,
        validation=executed.validation,
        llm_used=executed.method == PromotionMethod.SEMANTIC_PORT,
        manual_item=executed.manual_item,
    )
