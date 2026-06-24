"""Promotion orchestrator skeleton for Phase 1 dry-run planning."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from vpa.analysis.change_analyzer import analyze
from vpa.analysis.classifier import classify_commit
from vpa.analysis.isa_mapper import map_reference_files
from vpa.engines.git import GitEngine
from vpa.orchestrator.llm_gate import decide
from vpa.orchestrator.models import (
    BaseCommitContext,
    ChangeAnalysis,
    CommitContext,
    GateDecision,
    GatePolicy,
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


class PromotionOrchestrator:
    def __init__(self, config: PromotionConfig):
        self.config = config
        self.upstream_git = GitEngine(config.upstream_repo)

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

