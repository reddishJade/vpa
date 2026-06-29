"""Preprocessor conditional detection for shared-file ISA changes.

Upstream box64 keeps some RV64-specific logic inside shared source files using
``#if defined(RV64)`` and, increasingly, ``#if defined(RV64) || defined(SW64)``.
Path-only classification misses these changes, so this module parses the
preprocessor conditional stack around changed hunks and reports whether a diff
touches RV64-only, SW64-only, shared-RV64/SW64, or non-RV64 conditional blocks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from vpa.orchestrator.models import (
    ConditionalClass,
    DiffHunk,
    DiffLineKind,
    FileDiff,
)

_DEFAULT_RV64_SYMBOL = "RV64"
_DEFAULT_SW64_SYMBOL = "SW64"

_DIRECTIVE_RE = re.compile(
    r"^\s*#\s*(if|ifdef|ifndef|elif|else|endif)\b\s*(.*)$"
)


@dataclass(frozen=True)
class ConditionalDirective:
    kind: str
    expr: str
    line: int

    def covers_rv64(self, rv64: str = _DEFAULT_RV64_SYMBOL) -> bool:
        if self.kind == "ifdef":
            return self.expr == rv64
        if self.kind == "ifndef":
            return False
        if rv64 not in self.expr:
            return False
        return not self._is_negated(rv64)

    def covers_sw64(self, sw64: str = _DEFAULT_SW64_SYMBOL) -> bool:
        if self.kind == "ifdef":
            return self.expr == sw64
        if self.kind == "ifndef":
            return False
        if sw64 not in self.expr:
            return False
        return not self._is_negated(sw64)

    def is_not_rv64(self, rv64: str = _DEFAULT_RV64_SYMBOL) -> bool:
        if self.kind == "ifndef":
            return self.expr == rv64
        if rv64 not in self.expr:
            return False
        return self._is_negated(rv64)

    def _is_negated(self, symbol: str) -> bool:
        expr = self.expr
        # Simple heuristics for !defined(SYM) and #if !defined(SYM)
        return (expr.startswith("!") and symbol in expr) or (
            f"!defined({symbol})" in expr
        )

    def as_else_branch(self) -> ConditionalDirective:
        if self.kind in ("if", "ifdef", "ifndef", "elif"):
            expr = f"!({self.expr})" if self.expr else ""
            return ConditionalDirective(kind="else", expr=expr, line=self.line)
        return self


def analyze_file_conditionals(content: str) -> dict[int, list[ConditionalDirective]]:
    """Return the active preprocessor conditional stack for each 1-based line."""
    lines = content.splitlines()
    active: dict[int, list[ConditionalDirective]] = {}
    stack: list[ConditionalDirective] = []
    for idx, raw in enumerate(lines, 1):
        directive = _parse_directive(raw, idx)
        if directive is not None:
            if directive.kind in ("if", "ifdef", "ifndef"):
                stack.append(directive)
            elif directive.kind == "elif" and stack:
                stack[-1] = directive
            elif directive.kind == "else" and stack:
                stack[-1] = stack[-1].as_else_branch()
            elif directive.kind == "endif" and stack:
                stack.pop()
        active[idx] = list(stack)
    return active


def classify_file_diff_conditionals(
    file_diff: FileDiff,
    old_content: str,
    *,
    rv64_symbol: str = _DEFAULT_RV64_SYMBOL,
    sw64_symbol: str = _DEFAULT_SW64_SYMBOL,
) -> ConditionalClass:
    """Classify the conditional context of changed lines in a shared file."""
    active = analyze_file_conditionals(old_content)
    if not active:
        return ConditionalClass.NONE

    observations: list[tuple[bool, bool, bool]] = []
    for hunk in file_diff.hunks:
        for line_info in _changed_line_old_positions(hunk):
            line_no = line_info["old_line"]
            if line_no < 1 or line_no > len(active):
                continue
            stack = active[line_no]
            covers_rv64 = any(d.covers_rv64(rv64_symbol) for d in stack)
            covers_sw64 = any(d.covers_sw64(sw64_symbol) for d in stack)
            not_rv64 = any(d.is_not_rv64(rv64_symbol) for d in stack)
            if covers_rv64 or covers_sw64 or not_rv64:
                observations.append((covers_rv64, covers_sw64, not_rv64))

    if not observations:
        return ConditionalClass.NONE

    return _aggregate_observations(observations)


def _changed_line_old_positions(hunk: DiffHunk) -> list[dict[str, int]]:
    """Map changed hunk lines to approximate old-file line numbers.

    Removed lines have exact old-file positions. Added lines are inserted at the
    current old-file position, which is the same position as the preceding
    context or removed line.
    """
    positions: list[dict[str, int]] = []
    old_line = hunk.old_start
    new_line = hunk.new_start
    current_old_anchor = old_line
    for line in hunk.lines:
        if line.kind == DiffLineKind.CONTEXT:
            positions.append({"old_line": old_line, "new_line": new_line})
            current_old_anchor = old_line
            old_line += 1
            new_line += 1
        elif line.kind == DiffLineKind.REMOVED:
            positions.append({"old_line": old_line, "new_line": new_line})
            current_old_anchor = old_line
            old_line += 1
        elif line.kind == DiffLineKind.ADDED:
            positions.append({"old_line": current_old_anchor, "new_line": new_line})
            new_line += 1
    return positions


def _aggregate_observations(
    observations: list[tuple[bool, bool, bool]],
) -> ConditionalClass:
    first = observations[0]
    if all(obs == first for obs in observations):
        covers_rv64, covers_sw64, not_rv64 = first
        if covers_rv64 and covers_sw64:
            return ConditionalClass.RV64_OR_SW64
        if covers_rv64 and not covers_sw64 and not not_rv64:
            return ConditionalClass.RV64_ONLY
        if covers_sw64 and not covers_rv64 and not not_rv64:
            return ConditionalClass.SW64_ONLY
        if not_rv64 and not covers_rv64:
            return ConditionalClass.NOT_RV64
    return ConditionalClass.AMBIGUOUS


def _parse_directive(line: str, line_no: int) -> ConditionalDirective | None:
    match = _DIRECTIVE_RE.match(line)
    if not match:
        return None
    kind = match.group(1)
    expr = _strip_comments(match.group(2)).strip()
    if kind in ("else", "endif"):
        expr = ""
    return ConditionalDirective(kind=kind, expr=expr, line=line_no)


def classify_diff_context_conditionals(
    diff_context,
    content_resolver,
    *,
    rv64_symbol: str = _DEFAULT_RV64_SYMBOL,
    sw64_symbol: str = _DEFAULT_SW64_SYMBOL,
) -> dict[Path, ConditionalClass]:
    """Build a per-file conditional classification for a diff context.

    ``content_resolver`` receives a ``Path`` and the parent commit SHA string
    (taken from ``diff_context.commit.parent_sha``) and should return the file
    content before the commit, or ``None`` if unavailable.
    """
    result: dict[Path, ConditionalClass] = {}
    parent_sha = getattr(diff_context.commit, "parent_sha", None)
    if not parent_sha:
        return result
    for file_diff in diff_context.files:
        path = file_diff.path
        if path is None:
            continue
        old_content = content_resolver(path, parent_sha)
        if old_content is None:
            continue
        result[path] = classify_file_diff_conditionals(
            file_diff,
            old_content,
            rv64_symbol=rv64_symbol,
            sw64_symbol=sw64_symbol,
        )
    return result


def _strip_comments(text: str) -> str:
    # Strip C/C++ line and block comments from the tail of a directive.
    # Preprocessor directives are single-line, so only trailing comments matter.
    text = text.split("//", 1)[0]
    if "/*" in text:
        before, _, after = text.partition("/*")
        if "*/" in after:
            return before + after.split("*/", 1)[1]
        return before
    return text
