"""Promotion orchestrator for planning and Git execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from vpa.analysis.change_analyzer import analyze
from vpa.analysis.classifier import (
    classify_commit,
    classify_conflict_file,
    upgrade_conflict_by_content,
    without_generated_files,
)
from vpa.analysis.isa_mapper import map_reference_files
from vpa.analysis.preprocessor import classify_diff_context_conditionals
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
    ConflictCategory,
    FailureCode,
    GateDecision,
    GateDecisionKind,
    GatePolicy,
    GitApplyResult,
    GitCommandResult,
    GitOperationStatus,
    LedgerRecord,
    MergeConflictResolution,
    PendingConflictRecord,
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
    max_source_conflicts: int = 0
    ledger_path: Path | None = None
    report_path: Path | None = None
    gate_policy: GatePolicy = field(default_factory=GatePolicy)


@dataclass(frozen=True)
class PlannedCommit:
    context: CommitContext
    analysis: ChangeAnalysis
    gate_decision: GateDecision


@dataclass(frozen=True)
class CommitGroup:
    kind: CommitClass
    commits: list[PlannedCommit]


@dataclass(frozen=True)
class PromotionPlan:
    commits: list[PlannedCommit]


@dataclass(frozen=True)
class ExecutedCommit:
    planned: PlannedCommit
    method: PromotionMethod
    git_result: GitApplyResult | None
    validation: ValidationResult


@dataclass(frozen=True)
class PromotionRun:
    plan: PromotionPlan
    executed: list[ExecutedCommit] = field(default_factory=list)


class PromotionOrchestrator:
    def __init__(self, config: PromotionConfig, repair_engine: RepairEngine | None = None):
        self.config = config
        self.upstream_git = GitEngine(config.upstream_repo)
        self.local_git = GitEngine(config.local_repo)
        self.repair_engine = repair_engine or RepairEngine()
        self.ledger: LedgerStore | None = None

    def plan(self) -> PromotionPlan:
        planned: list[PlannedCommit] = []
        for sha in self.upstream_git.list_commits(self.config.revision_range):
            diff_context = without_generated_files(self.upstream_git.read_diff_context(sha))
            if not diff_context.files:
                continue
            base_context = BaseCommitContext(
                commit=diff_context.commit,
                diff_context=diff_context,
            )
            file_conditionals = classify_diff_context_conditionals(
                base_context.diff_context,
                self._resolve_parent_file_content,
            )
            classification = classify_commit(
                base_context.diff_context,
                reference_isa_path=self.config.primary_reference_isa_path,
                target_isa_path=self.config.target_isa_path,
                file_conditionals=file_conditionals,
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

    def _resolve_parent_file_content(self, path: Path, parent_sha: str) -> str | None:
        return self.upstream_git.show_file(path, parent_sha)

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
        self.ledger = LedgerStore(self.config.ledger_path) if self.config.ledger_path else None

        executed: list[ExecutedCommit] = []
        processed_shas: set[str] = set()
        if self.ledger:
            processed_shas = self.ledger.processed_commits()
        for planned in plan.commits:
            sha = planned.context.commit.sha
            if sha in processed_shas:
                executed.append(self._skip_commit(planned, "already processed in ledger"))
                continue
            if planned.context.classification.kind == CommitClass.GENERATED_OR_VENDOR:
                executed.append(self._skip_commit(planned, "generated/vendor path"))
                continue
            result = self._execute_commit(planned)
            executed.append(result)
            if self.ledger:
                self.ledger.append(_ledger_record(result))

        return PromotionRun(plan=plan, executed=executed)

    def _skip_commit(self, planned: PlannedCommit, reason: str) -> ExecutedCommit:
        return ExecutedCommit(
            planned=planned,
            method=PromotionMethod.SKIP,
            git_result=GitApplyResult(
                status=GitOperationStatus.SKIPPED,
                method=PromotionMethod.SKIP,
            ),
            validation=ValidationResult(ValidationStatus.NOT_RUN),
        )

    def _resolve_merge_conflicts(
        self,
        conflict_files: list[Path],
        planned: PlannedCommit,
    ) -> MergeConflictResolution:
        ref_paths = [self.config.primary_reference_isa_path]
        ref_paths += self.config.fallback_reference_isa_paths
        resolved: list[Path] = []
        failed: list[Path] = []
        source_count = 0
        max_source = self.config.max_source_conflicts
        for rel_path in conflict_files:
            full_path = self.config.local_repo / rel_path
            category = classify_conflict_file(rel_path, reference_isa_paths=ref_paths)
            category = upgrade_conflict_by_content(full_path, category)
            if category == ConflictCategory.ISA_BACKEND:
                ok = self._resolve_isa_conflict(rel_path, planned)
            elif category == ConflictCategory.NON_SOURCE:
                ok = self._resolve_non_source_conflict(rel_path)
            else:
                if max_source and source_count >= max_source:
                    failed.append(full_path)
                    continue
                source_count += 1
                ok = self._resolve_source_conflict(rel_path, planned)
            if ok:
                resolved.append(full_path)
            else:
                failed.append(full_path)
        return MergeConflictResolution(resolved_files=resolved, failed_files=failed)

    def _resolve_isa_conflict(self, rel_path: Path, planned: PlannedCommit) -> bool:
        if self.ledger is not None:
            self.ledger.append(
                PendingConflictRecord(
                    commit_sha=planned.context.commit.sha,
                    commit_subject=planned.context.commit.subject,
                    file_path=rel_path,
                    category=ConflictCategory.ISA_BACKEND,
                )
            )
        return False

    def _resolve_non_source_conflict(self, rel_path: Path) -> bool:
        self.local_git._run_result(["checkout", "--theirs", str(rel_path)])
        self.local_git._run_result(["add", str(rel_path)])
        return True

    def _resolve_source_conflict(self, rel_path: Path, planned: PlannedCommit) -> bool:
        if self.ledger is not None:
            self.ledger.append(
                PendingConflictRecord(
                    commit_sha=planned.context.commit.sha,
                    commit_subject=planned.context.commit.subject,
                    file_path=rel_path,
                    category=ConflictCategory.SOURCE,
                )
            )
        return False

    def _execute_commit(self, planned: PlannedCommit) -> ExecutedCommit:
        checkpoint = self.local_git.checkpoint()
        method = _mechanical_method(planned)
        git_result = self._apply_mechanical_commit(planned, method)
        git_result = _with_checkpoint(git_result, checkpoint)

        if git_result.status == GitOperationStatus.CONFLICT:
            repair_result = self._resolve_merge_conflicts(git_result.conflicts, planned)
            if repair_result.failed_files:
                self._rollback(checkpoint, git_result)
                return ExecutedCommit(
                    planned=planned,
                    method=method,
                    git_result=_rolled_back(git_result),
                    validation=ValidationResult(ValidationStatus.NOT_RUN),
                )
            git_result = self._continue_mechanical_commit(planned, method, git_result)
            if git_result.status != GitOperationStatus.APPLIED:
                self._rollback(checkpoint, git_result)
                return ExecutedCommit(
                    planned=planned,
                    method=method,
                    git_result=_rolled_back(git_result),
                    validation=ValidationResult(ValidationStatus.NOT_RUN),
                )
        elif git_result.status != GitOperationStatus.APPLIED:
            self._rollback(checkpoint, git_result)
            return ExecutedCommit(
                planned=planned,
                method=method,
                git_result=_rolled_back(git_result),
                validation=ValidationResult(ValidationStatus.NOT_RUN),
            )

        if planned.gate_decision.kind == GateDecisionKind.NEEDS_SEMANTIC_PORT:
            if self.ledger is not None:
                for file_diff in planned.context.diff_context.files:
                    if file_diff.path is not None:
                        self.ledger.append(
                            PendingConflictRecord(
                                commit_sha=planned.context.commit.sha,
                                commit_subject=planned.context.commit.subject,
                                file_path=file_diff.path,
                                category=ConflictCategory.ISA_BACKEND,
                                status="pending_semantic_port",
                            )
                        )

        validation = run_validation(self.config.local_repo, _validation_commands(self.config))
        if validation.status == ValidationStatus.FAILED:
            assert git_result is not None
            self.local_git.reset_to_checkpoint(checkpoint)
            return ExecutedCommit(
                planned=planned,
                method=method,
                git_result=_rolled_back(git_result),
                validation=validation,
            )

        assert git_result is not None
        return ExecutedCommit(
            planned=planned,
            method=method,
            git_result=git_result,
            validation=validation,
        )

    def _continue_mechanical_commit(
        self,
        planned: PlannedCommit,
        method: PromotionMethod,
        git_result: GitApplyResult,
    ) -> GitApplyResult:
        commit = planned.context.commit
        if method == PromotionMethod.PATH_LIMITED_APPLY_3WAY:
            short = commit.sha[:12]
            message = f"VPA path-limited apply {short}\n\n(applied from commit {commit.sha})"
            author = None
        else:
            message = f"{commit.subject}\n\n(cherry picked from commit {commit.sha})"
            author = commit.author
        commit_result = self.local_git.commit_cherry_pick(message, author=author)
        if commit_result.returncode != 0:
            return GitApplyResult(
                status=GitOperationStatus.FAILED,
                method=method,
                checkpoint=git_result.checkpoint,
                command=commit_result,
                conflicts=git_result.conflicts,
            )
        return GitApplyResult(
            status=GitOperationStatus.APPLIED,
            method=method,
            checkpoint=git_result.checkpoint,
            commit_sha=self.local_git.current_head(),
        )

    def _execute_isa_translate(self, planned: PlannedCommit) -> ExecutedCommit:
        checkpoint = self.local_git.checkpoint()
        result = self.repair_engine.isa_translate(
            planned.context,
            planned.analysis,
            planned.gate_decision,
            self.config.local_repo,
        )
        if not result.success:
            self.local_git.reset_to_checkpoint(checkpoint)
            return ExecutedCommit(
                planned=planned,
                method=PromotionMethod.SEMANTIC_PORT,
                git_result=GitApplyResult(
                    status=(
                        GitOperationStatus.SKIPPED
                        if result.failure_code == FailureCode.NO_LLM_CONFIGURED
                        else GitOperationStatus.ROLLED_BACK
                    ),
                    method=PromotionMethod.SEMANTIC_PORT,
                    checkpoint=checkpoint,
                    command=(
                        GitCommandResult(
                            args=[],
                            cwd=self.config.local_repo,
                            status=GitOperationStatus.FAILED,
                            returncode=1,
                            stderr=result.status_reason or "",
                        )
                        if result.failure_code == FailureCode.NO_LLM_CONFIGURED
                        else None
                    ),
                ),
                validation=ValidationResult(ValidationStatus.NOT_RUN),
            )

        stage = self.local_git._run_result(["add", "-A"])
        if stage.returncode != 0:
            self.local_git.reset_to_checkpoint(checkpoint)
            return ExecutedCommit(
                planned=planned,
                method=PromotionMethod.SEMANTIC_PORT,
                git_result=GitApplyResult(
                    status=GitOperationStatus.FAILED,
                    method=PromotionMethod.SEMANTIC_PORT,
                    checkpoint=checkpoint,
                ),
                validation=ValidationResult(ValidationStatus.NOT_RUN),
            )

        staged = self.local_git._run_result(["diff", "--cached", "--name-only"])
        if not staged.stdout.strip():
            return ExecutedCommit(
                planned=planned,
                method=PromotionMethod.SEMANTIC_PORT,
                git_result=GitApplyResult(
                    status=GitOperationStatus.SKIPPED,
                    method=PromotionMethod.SEMANTIC_PORT,
                    checkpoint=checkpoint,
                ),
                validation=ValidationResult(ValidationStatus.NOT_RUN),
            )

        commit_result = self.local_git._run_result(
            [
                "-c", "user.name=VPA",
                "-c", "user.email=vpa@example.invalid",
                "commit", "-m",
                f"VPA semantic port {planned.context.commit.sha[:12]}",
            ]
        )
        if commit_result.returncode != 0:
            self.local_git.reset_to_checkpoint(checkpoint)
            return ExecutedCommit(
                planned=planned,
                method=PromotionMethod.SEMANTIC_PORT,
                git_result=GitApplyResult(
                    status=GitOperationStatus.FAILED,
                    method=PromotionMethod.SEMANTIC_PORT,
                    checkpoint=checkpoint,
                ),
                validation=ValidationResult(ValidationStatus.NOT_RUN),
            )

        validation = run_validation(self.config.local_repo, _validation_commands(self.config))
        if validation.status == ValidationStatus.FAILED:
            self.local_git.reset_to_checkpoint(checkpoint)
            return ExecutedCommit(
                planned=planned,
                method=PromotionMethod.SEMANTIC_PORT,
                git_result=GitApplyResult(
                    status=GitOperationStatus.ROLLED_BACK,
                    method=PromotionMethod.SEMANTIC_PORT,
                    checkpoint=checkpoint,
                ),
                validation=validation,
            )

        return ExecutedCommit(
            planned=planned,
            method=PromotionMethod.SEMANTIC_PORT,
            git_result=GitApplyResult(
                status=GitOperationStatus.APPLIED,
                method=PromotionMethod.SEMANTIC_PORT,
                checkpoint=checkpoint,
                commit_sha=self.local_git.current_head(),
            ),
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
        if git_result.method == PromotionMethod.CHERRY_PICK:
            self.local_git.abort_cherry_pick()
        self.local_git.reset_to_checkpoint(checkpoint)


def render_plan(plan: PromotionPlan) -> str:
    lines = ["VPA promotion plan", ""]
    for item in plan.commits:
        commit = item.context.commit
        lines.append(f"- {commit.sha[:12]} {commit.subject}")
        lines.append(f"  classification: {item.context.classification.kind}")
        lines.append(f"  change: {item.analysis.kind}")
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
        lines.append(f"  apply: {_apply_status_label(git_status)}")
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
    if not run.executed:
        lines.append("(no commits executed)")
    return "\n".join(lines)


def _group_commits(plan: PromotionPlan) -> list[CommitGroup]:
    groups: list[CommitGroup] = []
    for planned in plan.commits:
        kind = planned.context.classification.kind
        if groups and groups[-1].kind == kind:
            groups[-1].commits.append(planned)
        else:
            groups.append(CommitGroup(kind=kind, commits=[planned]))
    return groups


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


def _touches_pending_files(planned: PlannedCommit, pending_files: set[str]) -> list[str]:
    touched: list[str] = []
    for file_diff in planned.context.diff_context.files:
        path = file_diff.path
        if path is None:
            continue
        posix = path.as_posix()
        if posix in pending_files:
            touched.append(posix)
    return touched


def _first_nonempty_lines(text: str, limit: int = 6) -> list[str]:
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[:limit]


def _apply_status_label(status: GitOperationStatus) -> str:
    if status == GitOperationStatus.APPLIED:
        return "committed"
    if status == GitOperationStatus.SKIPPED:
        return "skipped"
    if status == GitOperationStatus.FAILED:
        return "failed"
    return "rolled_back"


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
    status = executed.git_result.status if executed.git_result else None
    apply_status = (
        "committed" if status == GitOperationStatus.APPLIED
        else "skipped" if status == GitOperationStatus.SKIPPED
        else "rolled_back"
    )
    apply_reason = None
    if executed.git_result and executed.git_result.command:
        apply_reason = executed.git_result.command.stderr or None
    elif executed.git_result and status == GitOperationStatus.SKIPPED:
        apply_reason = "No LLM configured or no changes produced"
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
        apply_status=apply_status,
        apply_reason=apply_reason,
        integrity_status="passed",
        validation_status=executed.validation.status.value if executed.validation else "not_run",
        llm_used=executed.method == PromotionMethod.SEMANTIC_PORT,
    )
