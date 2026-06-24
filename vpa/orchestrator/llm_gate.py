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
            confidence=1.0,
        )

    if change_analysis.kind in {
        ChangeKind.COMMENT_ONLY,
        ChangeKind.FORMAT_ONLY,
        ChangeKind.METADATA_ONLY,
    }:
        return GateDecision(
            kind=GateDecisionKind.NO_TARGET_CHANGE,
            reasons=[*reasons, "non-semantic reference change"],
            confidence=change_analysis.confidence,
        )

    unsafe_mapping = [
        mapping
        for mapping in context.isa_mapping.file_mappings
        if mapping.status in {MappingStatus.MISSING_TARGET, MappingStatus.AMBIGUOUS}
    ]
    if unsafe_mapping:
        return GateDecision(
            kind=GateDecisionKind.NEEDS_MANUAL_REVIEW,
            reasons=[*reasons, "reference file mapping is missing or ambiguous"],
            confidence=change_analysis.confidence,
        )

    mapped = [
        mapping
        for mapping in context.isa_mapping.file_mappings
        if mapping.status == MappingStatus.MAPPED
    ]
    if mapped and change_analysis.confidence >= policy.semantic_confidence_threshold:
        return GateDecision(
            kind=GateDecisionKind.NEEDS_SEMANTIC_PORT,
            reasons=[*reasons, "semantic reference change has mapped target candidate"],
            confidence=change_analysis.confidence,
        )

    if mapped:
        return GateDecision(
            kind=GateDecisionKind.NEEDS_MANUAL_REVIEW,
            reasons=[*reasons, "semantic confidence is below policy threshold"],
            confidence=change_analysis.confidence,
        )

    return GateDecision(
        kind=change_analysis.suggested_gate,
        reasons=[*reasons, "using analyzer suggested gate"],
        confidence=change_analysis.confidence,
    )
