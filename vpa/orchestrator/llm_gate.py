"""Pure LLM gate decision logic."""

from __future__ import annotations

from vpa.orchestrator.models import (
    ChangeAnalysis,
    ChangeKind,
    CommitClass,
    CommitContext,
    GateDecision,
    GateDecisionKind,
    GatePolicy,
    MappingStatus,
)


def decide(
    change_analysis: ChangeAnalysis,
    policy: GatePolicy,
    context: CommitContext,
) -> GateDecision:
    reasons = list(context.classification.reasons)
    reasons.append(f"change kind: {change_analysis.kind}")

    if context.classification.kind in {CommitClass.SHARED_CODE, CommitClass.TARGET_ISA_DIRECT}:
        return GateDecision(
            kind=GateDecisionKind.NEEDS_VALIDATION_ONLY,
            reasons=[
                *reasons,
                "shared or target-direct change should validate without semantic port",
            ],
        )

    if change_analysis.kind in {
        ChangeKind.COMMENT_ONLY,
        ChangeKind.FORMAT_ONLY,
        ChangeKind.METADATA_ONLY,
    }:
        return GateDecision(
            kind=GateDecisionKind.NO_TARGET_CHANGE,
            reasons=[*reasons, "non-semantic reference change"],
        )

    mapped = [
        mapping
        for mapping in context.isa_mapping.file_mappings
        if mapping.status == MappingStatus.MAPPED
    ]
    if mapped:
        return GateDecision(
            kind=GateDecisionKind.NEEDS_SEMANTIC_PORT,
            reasons=[*reasons, "semantic reference change has mapped target candidate"],
        )

    return GateDecision(
        kind=change_analysis.suggested_gate,
        reasons=[*reasons, "using analyzer suggested gate"],
    )
