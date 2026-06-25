"""Structured diff-text change analysis.

Sub-analyzers emit signals only. The aggregator combines those signals into a
single ChangeAnalysis that the orchestrator can pass to the LLM gate.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Iterable
from pathlib import Path

from vpa.orchestrator.models import (
    ChangeAnalysis,
    ChangeKind,
    ChangeSignal,
    DiffContext,
    DiffLineKind,
    GateDecisionKind,
    MappingResult,
    MappingStatus,
    SignalSource,
)

_RISK_ORDER = {
    ChangeKind.FORMAT_ONLY: 1,
    ChangeKind.COMMENT_ONLY: 1,
    ChangeKind.METADATA_ONLY: 2,
    ChangeKind.REFACTOR: 3,
    ChangeKind.API_SHAPE_CHANGE: 4,
    ChangeKind.LOGIC_CHANGE: 5,
    ChangeKind.NEW_SYMBOL: 5,
    ChangeKind.UNKNOWN: 6,
    ChangeKind.MIXED: 7,
}

_SEMANTIC_PATTERNS = (
    r"\bif\s*\(",
    r"\belse\b",
    r"\bfor\s*\(",
    r"\bwhile\s*\(",
    r"\bswitch\s*\(",
    r"\bcase\b",
    r"\breturn\b",
    r"(?<![=!<>])=(?!=)",
    r"\b#define\b",
    r"\bCALL\b",
    r"\bX\w*\(",
    r"\bflags?\b",
    r"\bopcode\b",
    r"\bstruct\b",
    r"\benum\b",
)
_SEMANTIC_RE = re.compile("|".join(_SEMANTIC_PATTERNS))
_INCLUDE_RE = re.compile(r"^\s*#\s*include\b")
_DEFINE_RE = re.compile(r"^\s*#\s*define\b")
_SIGNATURE_RE = re.compile(
    r"^\s*(?:static\s+)?(?:inline\s+)?[A-Za-z_][\w\s\*]+\s+([A-Za-z_]\w*)\s*\([^;]*\)\s*\{?\s*$"
)


class SubAnalyzer(ABC):
    @abstractmethod
    def analyze(self, diff_context: DiffContext, isa_mapping: MappingResult) -> list[ChangeSignal]:
        """Return signals, without aggregating them into a final decision."""


class DiffTextAnalyzer(SubAnalyzer):
    def analyze(self, diff_context: DiffContext, isa_mapping: MappingResult) -> list[ChangeSignal]:
        signals: list[ChangeSignal] = []
        for file_diff in diff_context.files:
            changed = _changed_lines(file_diff.hunks)
            semantic = [line for line in changed if _SEMANTIC_RE.search(line)]
            metadata = [
                line for line in changed if _INCLUDE_RE.search(line) and not _DEFINE_RE.search(line)
            ]
            if semantic:
                signals.append(
                    ChangeSignal(
                        kind=ChangeKind.LOGIC_CHANGE,
                        source=SignalSource.DIFF_TEXT,
                        confidence=0.82,
                        reason="diff contains likely runtime logic tokens",
                        file_path=file_diff.path,
                    )
                )
            elif metadata and len(metadata) == len(changed):
                signals.append(
                    ChangeSignal(
                        kind=ChangeKind.METADATA_ONLY,
                        source=SignalSource.DIFF_TEXT,
                        confidence=0.78,
                        reason="diff only changes include-style metadata",
                        file_path=file_diff.path,
                    )
                )
        return signals


class NormalizationAnalyzer(SubAnalyzer):
    def analyze(self, diff_context: DiffContext, isa_mapping: MappingResult) -> list[ChangeSignal]:
        signals: list[ChangeSignal] = []
        for file_diff in diff_context.files:
            removed = _lines_by_kind(file_diff.hunks, DiffLineKind.REMOVED)
            added = _lines_by_kind(file_diff.hunks, DiffLineKind.ADDED)
            if not removed and not added:
                continue
            if _normalize_code(removed) == _normalize_code(added):
                signals.append(
                    ChangeSignal(
                        kind=ChangeKind.FORMAT_ONLY,
                        source=SignalSource.NORMALIZED,
                        confidence=0.9,
                        reason=(
                            "changed lines are equivalent after whitespace/comment "
                            "normalization"
                        ),
                        file_path=file_diff.path,
                    )
                )
            elif _strip_blank(removed) == [] and _strip_blank(added) == []:
                signals.append(
                    ChangeSignal(
                        kind=ChangeKind.FORMAT_ONLY,
                        source=SignalSource.NORMALIZED,
                        confidence=0.95,
                        reason="diff only changes blank lines",
                        file_path=file_diff.path,
                    )
                )
            elif _normalize_comments(removed) != _normalize_comments(added) and (
                _normalize_code(removed) == _normalize_code(added)
            ):
                signals.append(
                    ChangeSignal(
                        kind=ChangeKind.COMMENT_ONLY,
                        source=SignalSource.NORMALIZED,
                        confidence=0.88,
                        reason="non-comment code is unchanged",
                        file_path=file_diff.path,
                    )
                )
        return signals


class SymbolTextAnalyzer(SubAnalyzer):
    def analyze(self, diff_context: DiffContext, isa_mapping: MappingResult) -> list[ChangeSignal]:
        signals: list[ChangeSignal] = []
        for file_diff in diff_context.files:
            removed_symbols = set(_changed_symbols(file_diff.hunks, DiffLineKind.REMOVED))
            added_symbols = set(_changed_symbols(file_diff.hunks, DiffLineKind.ADDED))
            for symbol in sorted(added_symbols - removed_symbols):
                signals.append(
                    ChangeSignal(
                        kind=ChangeKind.NEW_SYMBOL,
                        source=SignalSource.SYMBOL_TEXT,
                        confidence=0.78,
                        reason="added function-like symbol",
                        file_path=file_diff.path,
                        symbol=symbol,
                    )
                )
            if removed_symbols and added_symbols and removed_symbols != added_symbols:
                signals.append(
                    ChangeSignal(
                        kind=ChangeKind.REFACTOR,
                        source=SignalSource.SYMBOL_TEXT,
                        confidence=0.62,
                        reason="changed function-like symbols",
                        file_path=file_diff.path,
                    )
                )
        return signals


def default_analyzers() -> list[SubAnalyzer]:
    return [NormalizationAnalyzer(), DiffTextAnalyzer(), SymbolTextAnalyzer()]


def analyze(
    diff_context: DiffContext,
    isa_mapping: MappingResult,
    analyzers: Iterable[SubAnalyzer] | None = None,
) -> ChangeAnalysis:
    chain = list(analyzers or default_analyzers())
    signals = [
        signal
        for analyzer in chain
        for signal in analyzer.analyze(diff_context, isa_mapping)
    ]
    return aggregate(signals, isa_mapping)


def aggregate(signals: list[ChangeSignal], isa_mapping: MappingResult) -> ChangeAnalysis:
    if not signals:
        kind = ChangeKind.UNKNOWN
        confidence = 0.0
    else:
        meaningful = {signal.kind for signal in signals if _RISK_ORDER[signal.kind] >= 2}
        if len(meaningful) > 1:
            kind = ChangeKind.MIXED
        else:
            kind = max((signal.kind for signal in signals), key=lambda item: _RISK_ORDER[item])
        confidence = max(signal.confidence for signal in signals)

    mapped_targets = _mapped_targets(isa_mapping)
    suggested_gate = _suggest_gate(kind, isa_mapping)
    changed_symbols = sorted({signal.symbol for signal in signals if signal.symbol})
    return ChangeAnalysis(
        kind=kind,
        confidence=confidence,
        signals=signals,
        changed_symbols=changed_symbols,
        mapped_target_candidates=mapped_targets,
        suggested_gate=suggested_gate,
    )


def _suggest_gate(kind: ChangeKind, isa_mapping: MappingResult) -> GateDecisionKind:
    if kind in {ChangeKind.COMMENT_ONLY, ChangeKind.FORMAT_ONLY, ChangeKind.METADATA_ONLY}:
        return GateDecisionKind.NO_TARGET_CHANGE
    return GateDecisionKind.NEEDS_SEMANTIC_PORT


def _mapped_targets(isa_mapping: MappingResult) -> list[Path]:
    targets: list[Path] = []
    for mapping in isa_mapping.file_mappings:
        if mapping.status == MappingStatus.MAPPED:
            targets.extend(mapping.target_candidates)
    return targets


def _changed_lines(hunks) -> list[str]:
    return [
        line.text
        for hunk in hunks
        for line in hunk.lines
        if line.kind in {DiffLineKind.ADDED, DiffLineKind.REMOVED}
    ]


def _lines_by_kind(hunks, kind: DiffLineKind) -> list[str]:
    return [line.text for hunk in hunks for line in hunk.lines if line.kind == kind]


def _strip_blank(lines: list[str]) -> list[str]:
    return [line for line in lines if line.strip()]


def _normalize_comments(lines: list[str]) -> list[str]:
    return [line.strip() for line in lines if _is_comment_line(line)]


def _normalize_code(lines: list[str]) -> list[str]:
    normalized: list[str] = []
    for line in lines:
        without_comment = _remove_line_comment(line).strip()
        if not without_comment or _is_comment_line(without_comment):
            continue
        normalized.append(re.sub(r"\s+", " ", without_comment))
    return normalized


def _remove_line_comment(line: str) -> str:
    return line.split("//", 1)[0].split("/*", 1)[0]


def _is_comment_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith(("//", "/*", "*", "*/"))


def _changed_symbols(hunks, kind: DiffLineKind) -> list[str]:
    symbols: list[str] = []
    for line in _lines_by_kind(hunks, kind):
        match = _SIGNATURE_RE.match(line)
        if match:
            symbols.append(match.group(1))
    return symbols
