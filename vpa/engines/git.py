"""Git read helpers for the workflow.

This module intentionally wraps Git as a first-class engine. Phase 1 only reads
commit metadata and patch text; mutation commands belong to later phases.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from vpa.orchestrator.models import (
    CommitInfo,
    DiffContext,
    DiffHunk,
    DiffLine,
    DiffLineKind,
    FileDiff,
    FileLanguage,
    FileStatus,
)

_HUNK_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@(?: (?P<section>.*))?$"
)


class GitEngine:
    def __init__(self, repo: str | Path):
        self.repo = Path(repo)

    def list_commits(self, revision_range: str) -> list[str]:
        result = self._run(["rev-list", "--reverse", revision_range])
        return [line for line in result.stdout.splitlines() if line]

    def read_commit(self, sha: str) -> CommitInfo:
        fmt = "%H%x00%s%x00%an <%ae>%x00%aI"
        result = self._run(["show", "-s", f"--format={fmt}", sha])
        commit_sha, subject, author, author_date = result.stdout.rstrip("\n").split("\x00", 3)
        return CommitInfo(
            sha=commit_sha,
            subject=subject,
            author=author,
            author_date=author_date,
        )

    def read_raw_patch(self, sha: str) -> str:
        return self._run(["show", "--format=", "--find-renames", sha]).stdout

    def read_diff_context(self, sha: str) -> DiffContext:
        commit = self.read_commit(sha)
        raw_patch = self.read_raw_patch(sha)
        return parse_diff_context(commit, raw_patch)

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
        )


def parse_diff_context(commit: CommitInfo, raw_patch: str) -> DiffContext:
    file_patches = _split_file_patches(raw_patch)
    files = [_parse_file_patch(file_patch) for file_patch in file_patches]
    return DiffContext(commit=commit, raw_patch=raw_patch, files=files)


def _split_file_patches(raw_patch: str) -> list[str]:
    chunks: list[list[str]] = []
    current: list[str] = []
    for line in raw_patch.splitlines():
        if line.startswith("diff --git "):
            if current:
                chunks.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        chunks.append(current)
    return ["\n".join(chunk) + "\n" for chunk in chunks]


def _parse_file_patch(raw_patch: str) -> FileDiff:
    lines = raw_patch.splitlines()
    path_before: Path | None = None
    path_after: Path | None = None
    status = FileStatus.MODIFIED
    hunks: list[DiffHunk] = []

    if lines and lines[0].startswith("diff --git "):
        parts = lines[0].split()
        if len(parts) >= 4:
            path_before = _strip_git_prefix(parts[2])
            path_after = _strip_git_prefix(parts[3])

    for line in lines:
        if line.startswith("new file mode"):
            status = FileStatus.ADDED
        elif line.startswith("deleted file mode"):
            status = FileStatus.DELETED
        elif line.startswith("similarity index"):
            status = FileStatus.RENAMED
        elif line.startswith("rename from "):
            path_before = Path(line.removeprefix("rename from "))
        elif line.startswith("rename to "):
            path_after = Path(line.removeprefix("rename to "))
        elif line.startswith("--- "):
            old_path = line.removeprefix("--- ")
            if old_path != "/dev/null":
                path_before = _strip_git_prefix(old_path)
        elif line.startswith("+++ "):
            new_path = line.removeprefix("+++ ")
            if new_path != "/dev/null":
                path_after = _strip_git_prefix(new_path)

    current_header: tuple[int, int, int, int, str | None] | None = None
    current_lines: list[DiffLine] = []
    for line in lines:
        match = _HUNK_RE.match(line)
        if match:
            if current_header is not None:
                hunks.append(_build_hunk(current_header, current_lines))
            current_header = (
                int(match.group("old_start")),
                int(match.group("old_count") or "1"),
                int(match.group("new_start")),
                int(match.group("new_count") or "1"),
                match.group("section"),
            )
            current_lines = []
            continue
        if current_header is None:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            current_lines.append(DiffLine(DiffLineKind.ADDED, line[1:]))
        elif line.startswith("-") and not line.startswith("---"):
            current_lines.append(DiffLine(DiffLineKind.REMOVED, line[1:]))
        elif line.startswith(" "):
            current_lines.append(DiffLine(DiffLineKind.CONTEXT, line[1:]))
    if current_header is not None:
        hunks.append(_build_hunk(current_header, current_lines))

    path = path_after or path_before
    return FileDiff(
        path_before=path_before,
        path_after=path_after,
        status=status,
        language=detect_language(path),
        raw_patch=raw_patch,
        hunks=hunks,
    )


def _build_hunk(
    header: tuple[int, int, int, int, str | None],
    lines: list[DiffLine],
) -> DiffHunk:
    old_start, old_count, new_start, new_count, section = header
    return DiffHunk(
        old_start=old_start,
        old_count=old_count,
        new_start=new_start,
        new_count=new_count,
        section=section,
        lines=lines,
    )


def _strip_git_prefix(path: str) -> Path:
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return Path(path)


def detect_language(path: Path | None) -> FileLanguage:
    if path is None:
        return FileLanguage.UNKNOWN
    suffix = path.suffix.lower()
    name = path.name.lower()
    if suffix == ".c":
        return FileLanguage.C
    if suffix == ".h":
        return FileLanguage.HEADER
    if suffix in {".s", ".asm"}:
        return FileLanguage.ASM
    if name in {"cmakelists.txt", "makefile"} or suffix in {".cmake", ".mk"}:
        return FileLanguage.BUILD
    if suffix in {".md", ".txt", ".rst"}:
        return FileLanguage.TEXT
    return FileLanguage.UNKNOWN

