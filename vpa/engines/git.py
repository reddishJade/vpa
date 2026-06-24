"""Git helpers for the workflow.

This module intentionally wraps Git as a first-class engine. Phase 1 only reads
commit metadata and patch text. Phase 2 adds controlled mutation commands that
return structured results for the orchestrator and ledger.
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
    GitApplyResult,
    GitCommandResult,
    GitOperationStatus,
    PromotionMethod,
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

    def checkpoint(self) -> str:
        return self._run(["rev-parse", "HEAD"]).stdout.strip()

    def current_head(self) -> str:
        return self.checkpoint()

    def create_work_branch(self, branch_name: str) -> GitCommandResult:
        return self._run_result(["checkout", "-B", branch_name])

    def cherry_pick_from(self, upstream_repo: str | Path, sha: str) -> GitApplyResult:
        fetch = self._run_result(["fetch", "--no-tags", str(upstream_repo), sha])
        if fetch.returncode != 0:
            return GitApplyResult(
                status=GitOperationStatus.FAILED,
                method=PromotionMethod.CHERRY_PICK,
                command=fetch,
                conflicts=self.conflicted_files(),
            )

        result = self._run_result(["cherry-pick", "--no-edit", "FETCH_HEAD"])
        if result.returncode == 0:
            return GitApplyResult(
                status=GitOperationStatus.APPLIED,
                method=PromotionMethod.CHERRY_PICK,
                command=result,
                commit_sha=self.current_head(),
            )

        conflicts = self.conflicted_files()
        return GitApplyResult(
            status=GitOperationStatus.CONFLICT if conflicts else GitOperationStatus.FAILED,
            method=PromotionMethod.CHERRY_PICK,
            command=result,
            conflicts=conflicts,
        )

    def apply_patch_3way(self, patch_text: str) -> GitApplyResult:
        result = self._run_result(["apply", "--3way", "--index", "-"], input_text=patch_text)
        if result.returncode != 0:
            conflicts = self.conflicted_files()
            if not conflicts:
                direct = self._run_result(["apply", "--index", "-"], input_text=patch_text)
                if direct.returncode == 0:
                    return self._commit_applied_patch(direct)
                result = direct
            return GitApplyResult(
                status=GitOperationStatus.CONFLICT if conflicts else GitOperationStatus.FAILED,
                method=PromotionMethod.PATH_LIMITED_APPLY_3WAY,
                command=result,
                conflicts=conflicts,
            )

        return self._commit_applied_patch(result)

    def _commit_applied_patch(self, apply_result: GitCommandResult) -> GitApplyResult:
        commit = self._run_result(
            [
                "-c",
                "user.name=VPA",
                "-c",
                "user.email=vpa@example.invalid",
                "commit",
                "-m",
                "VPA path-limited apply",
            ]
        )
        if commit.returncode == 0:
            return GitApplyResult(
                status=GitOperationStatus.APPLIED,
                method=PromotionMethod.PATH_LIMITED_APPLY_3WAY,
                command=commit,
                commit_sha=self.current_head(),
            )
        return GitApplyResult(
            status=GitOperationStatus.FAILED,
            method=PromotionMethod.PATH_LIMITED_APPLY_3WAY,
            command=commit if commit.returncode != 0 else apply_result,
            conflicts=self.conflicted_files(),
        )

    def abort_cherry_pick(self) -> GitCommandResult:
        return self._run_result(["cherry-pick", "--abort"])

    def reset_to_checkpoint(self, checkpoint: str) -> GitCommandResult:
        return self._run_result(["reset", "--hard", checkpoint])

    def conflicted_files(self) -> list[Path]:
        result = self._run_result(["diff", "--name-only", "--diff-filter=U"])
        if result.returncode != 0:
            return []
        return [Path(line) for line in result.stdout.splitlines() if line]

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
        )

    def _run_result(
        self,
        args: list[str],
        *,
        input_text: str | None = None,
    ) -> GitCommandResult:
        completed = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            input=input_text,
            check=False,
            capture_output=True,
            text=True,
        )
        return GitCommandResult(
            args=["git", *args],
            cwd=self.repo,
            status=(
                GitOperationStatus.APPLIED
                if completed.returncode == 0
                else GitOperationStatus.FAILED
            ),
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


def parse_diff_context(commit: CommitInfo, raw_patch: str) -> DiffContext:
    file_patches = _split_file_patches(raw_patch)
    files = [_parse_file_patch(file_patch) for file_patch in file_patches]
    return DiffContext(commit=commit, raw_patch=raw_patch, files=files)


def render_patch(files: list[FileDiff]) -> str:
    return "".join(file.raw_patch for file in files)


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
