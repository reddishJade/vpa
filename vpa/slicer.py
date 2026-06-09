import re
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class SliceLevel(Enum):
    COMMIT = "commit"
    FILE = "file"
    HUNK = "hunk"


@dataclass
class Slice:
    level: SliceLevel
    label: str
    commit_sha: str
    context: str = ""
    files: list = field(default_factory=list)
    file_path: str | None = None
    file_diff: str | None = None

    def describe(self):
        if self.level == SliceLevel.COMMIT:
            return f"commit {self.commit_sha[:8]}"
        elif self.level == SliceLevel.FILE:
            return f"file {self.file_path} (commit {self.commit_sha[:8]})"
        else:
            return f"file {self.file_path} hunk (commit {self.commit_sha[:8]})"

    def to_work_items(self):
        """Generate work item descriptors from this slice.

        Returns list of dicts with {id, kind, upstream_file, local_file}.
        """
        items = []
        sha_short = self.commit_sha[:8]

        if self.level == SliceLevel.COMMIT:
            for i, f in enumerate(self.files):
                items.append(
                    {
                        "id": f"{sha_short}:{f}:{i}",
                        "kind": "file",
                        "upstream_file": f,
                        "local_file": f,
                    }
                )
        elif self.level == SliceLevel.FILE:
            items.append(
                {
                    "id": f"{sha_short}:{self.file_path}:0",
                    "kind": "file",
                    "upstream_file": self.file_path,
                    "local_file": self.file_path,
                }
            )
        else:  # HUNK
            items.append(
                {
                    "id": f"{sha_short}:{self.file_path}:hunk",
                    "kind": "hunk",
                    "upstream_file": self.file_path,
                    "local_file": self.file_path,
                }
            )
        return items


COMMIT_THRESHOLD_LINES = 300
COMMIT_THRESHOLD_FILES = 8
FILE_THRESHOLD_LINES = 200


def get_commit_list(upstream_path, old_rev, new_rev):
    result = subprocess.run(
        ["git", "log", "--reverse", "--format=%H", f"{old_rev}..{new_rev}"],
        capture_output=True,
        text=True,
        cwd=upstream_path,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git log failed: {result.stderr}")
    return [s for s in result.stdout.strip().split("\n") if s]


def get_commit_subject(upstream_path, commit_sha):
    result = subprocess.run(
        ["git", "log", "--format=%s", "-1", commit_sha],
        capture_output=True,
        text=True,
        cwd=upstream_path,
        timeout=10,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def get_commit_diff(upstream_path, commit_sha):
    result = subprocess.run(
        ["git", "diff", f"{commit_sha}~1..{commit_sha}"],
        capture_output=True,
        text=True,
        cwd=upstream_path,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git diff failed: {result.stderr}")
    return result.stdout


def get_commit_files(upstream_path, commit_sha):
    result = subprocess.run(
        ["git", "diff-tree", "--no-commit-id", "-r", "--name-only", commit_sha],
        capture_output=True,
        text=True,
        cwd=upstream_path,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git diff-tree failed: {result.stderr}")
    return [f for f in result.stdout.strip().split("\n") if f]


def count_diff_lines(diff):
    return len(diff.split("\n"))


def parse_diff_per_file(full_diff):
    per_file = {}
    current_file = None
    current_lines = []

    for line in full_diff.split("\n"):
        if line.startswith("diff --git "):
            if current_file and current_lines:
                per_file[current_file] = "\n".join(current_lines)
            current_lines = [line]
            parts = line.split()
            current_file = parts[3][2:] if len(parts) >= 4 else "<unknown>"
        elif current_file:
            current_lines.append(line)

    if current_file and current_lines:
        per_file[current_file] = "\n".join(current_lines)

    return per_file


FILE_PRIORITY_PATTERNS = [
    (r"\.(h|hpp|hxx|proto)$", 1),
    (r"(^|/)utils?/", 2),
    (r"(^|/)lib/", 2),
    (r"(^|/)internal/", 2),
    (r"(^|/)include/", 2),
    (r"\.(c|cc|cpp|cxx)$", 3),
    (r"(^|/)cmd/", 4),
    (r"(^|/)cli", 4),
    (r"(^|/)main\.", 4),
    (r"_test\.", 5),
    (r"test_.*\.", 5),
    (r"(^|/)tests?/", 5),
    (r"(^|/)spec/", 5),
]


def _priority(file_path):
    for pattern, pri in FILE_PRIORITY_PATTERNS:
        if re.search(pattern, file_path):
            return pri
    return 3


def topological_sort_files(files):
    if len(files) <= 1:
        return list(files)

    includes = re.compile(
        r'^\s*(?:#include\s+[<"](\S+)[>"]|import\s+[\w.\s,{}]*\b(\w+)\b|from\s+(\S+)\s+import)',
        re.MULTILINE,
    )

    basename_map = {}
    for f in files:
        base = f.rsplit("/", 1)[-1] if "/" in f else f
        basename_map.setdefault(base, []).append(f)

    deps = {f: set() for f in files}
    for f in files:
        try:
            with open(f) as fh:
                content = "".join(fh.readlines()[:200])
        except Exception:
            continue
        for m in includes.finditer(content):
            name = m.group(1) or m.group(2) or m.group(3)
            if not name:
                continue
            for target in basename_map.get(name.rsplit(".", 1)[0], []):
                if target != f:
                    deps[f].add(target)
            for target in basename_map.get(name, []):
                if target != f:
                    deps[f].add(target)

    sorted_files = []
    remaining = set(files)
    while remaining:
        ready = [f for f in remaining if not (deps[f] & remaining)]
        if not ready:
            ready = list(remaining)
        ready.sort(key=lambda f: (_priority(f), f))
        next_file = ready[0]
        sorted_files.append(next_file)
        remaining.remove(next_file)

    return sorted_files


def slice_commits(upstream_path, old_rev, new_rev, local_dir=None):
    """Yield Slice objects for each commit, with size-based fallback."""
    local_path = Path(local_dir) if local_dir and not isinstance(local_dir, Path) else local_dir
    shas = get_commit_list(upstream_path, old_rev, new_rev)

    for sha in shas:
        full_diff = get_commit_diff(upstream_path, sha)
        files = get_commit_files(upstream_path, sha)
        diff_lines = count_diff_lines(full_diff)

        if diff_lines > COMMIT_THRESHOLD_LINES or len(files) > COMMIT_THRESHOLD_FILES:
            per_file = parse_diff_per_file(full_diff)
            local_files = (
                [str(local_path / f) for f in files]
                if local_path
                else files
            )
            sorted_files = topological_sort_files(local_files)
            for loc_f in sorted_files:
                rel_path = (
                    str(Path(loc_f).relative_to(local_path))
                    if local_path and loc_f.startswith(str(local_path))
                    else loc_f
                )
                file_diff = per_file.get(rel_path, "")
                file_lines = count_diff_lines(file_diff)
                if file_lines > FILE_THRESHOLD_LINES:
                    yield Slice(
                        level=SliceLevel.HUNK,
                        label=f"hunk: {rel_path}",
                        commit_sha=sha,
                        context=(
                            f"Commit {sha[:8]} touches {rel_path} ({file_lines} lines in diff).\n"
                            f"Full file content is available for context."
                        ),
                        file_path=rel_path,
                        file_diff=file_diff,
                        files=[rel_path],
                    )
                else:
                    yield Slice(
                        level=SliceLevel.FILE,
                        label=f"file: {rel_path}",
                        commit_sha=sha,
                        context=(
                            f"Commit {sha[:8]} changes file {rel_path}:\n\n{file_diff}"
                        ),
                        file_path=rel_path,
                        file_diff=file_diff,
                        files=[rel_path],
                    )
        else:
            yield Slice(
                level=SliceLevel.COMMIT,
                label=f"commit: {sha[:8]}",
                commit_sha=sha,
                context=(
                    f"Commit {sha[:8]} changes {len(files)} file(s):\n"
                    + "\n".join(f"  {f}" for f in files)
                    + f"\n\nFull diff:\n{full_diff}"
                ),
                files=files,
            )
