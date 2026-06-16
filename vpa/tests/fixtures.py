"""Consolidated dry-run fixture helpers for VPA testing.

Provides MockAgent, MockValidation, temporary git repo builders,
and tool-call sequence helpers.  All tests should import from here
to avoid duplication across test files.
"""

import subprocess
from pathlib import Path

from vpa.verify import VerifyResult

_EVIDENCE = [{"file": "file.c", "line": 1, "snippet": "int x = 1;"}]


class MockAgent:
    """Predetermined tool-call sequences. One sequence consumed per call."""

    def __init__(self, sequences):
        self.sequences = list(sequences)
        self.call_count = 0
        self.captured_kwargs = []

    def __call__(self, **kwargs):
        self.call_count += 1
        self.captured_kwargs.append(kwargs)
        if not self.sequences:
            return ("No more sequences.", [])
        on_tool_call = kwargs["on_tool_call"]
        seq = self.sequences.pop(0)
        for name, args in seq:
            result = on_tool_call(name, args)
            if isinstance(result, dict) and "error" in result:
                raise RuntimeError(
                    f"Tool '{name}' error: {result['error']}"
                )
        return ("Mock agent done.", [])


class MockValidation:
    """Predetermined verification results. One list consumed per call."""

    def __init__(self, result_lists):
        self.result_lists = list(result_lists)
        self.call_count = 0

    def __call__(self, build_cmd, test_cmds, local_repo, timeout=120):
        self.call_count += 1
        if self.result_lists:
            return self.result_lists.pop(0)
        return [VerifyResult(passed=True, command="mock", exit_code=0)]


def _init_repo(path):
    """Initialize a git repo with test user config."""
    subprocess.run(["git", "init"], cwd=path, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=path, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=path, capture_output=True,
    )


def _git_commit(path, message):
    """Stage all changes and commit, returning HEAD sha."""
    subprocess.run(["git", "add", "."], cwd=path, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", message], cwd=path, capture_output=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path,
        capture_output=True, text=True,
    ).stdout.strip()


def create_fixture_repos(base_dir):
    """Create upstream (2 commits) and local (1 commit) tiny git repos.

    Upstream commit 1: creates file.c with one line.
    Upstream commit 2: adds another line (the commit to port).
    Local commit 1:   same content as upstream commit 1.
    """
    upstream = Path(base_dir) / "upstream"
    local = Path(base_dir) / "local"

    for d in [upstream, local]:
        d.mkdir(parents=True)
        _init_repo(d)

    (upstream / "file.c").write_text("int y = 2;\n")
    old_sha = _git_commit(upstream, "initial")

    (upstream / "file.c").write_text("int x = 1;\nint y = 2;\n")
    new_sha = _git_commit(upstream, "Add x feature")

    (local / "file.c").write_text("int y = 2;\n")
    _git_commit(local, "local initial")

    return upstream, local, new_sha, old_sha


def create_multi_commit_fixture(base_dir):
    """Create upstream (3 commits: initial + 2 porting) and local (matching initial).

    Each porting commit modifies base.c sequentially.
    Returns (upstream, local, new_sha, old_sha, [sha_a, sha_b]).
    """
    upstream = Path(base_dir) / "upstream"
    local = Path(base_dir) / "local"

    for d in [upstream, local]:
        d.mkdir(parents=True)
        _init_repo(d)

    (upstream / "base.c").write_text("int base = 0;\n")
    old_sha = _git_commit(upstream, "initial")

    (upstream / "base.c").write_text("int base = 0;\nint feat1 = 1;\n")
    sha_a = _git_commit(upstream, "Add feature 1")

    (upstream / "base.c").write_text(
        "int base = 0;\nint feat1 = 1;\nint feat2 = 2;\n"
    )
    sha_b = _git_commit(upstream, "Add feature 2")

    (local / "base.c").write_text("int base = 0;\n")
    _git_commit(local, "local initial")

    return upstream, local, sha_b, old_sha, [sha_a, sha_b]


def wi_id(sha):
    return f"{sha[:8]}:file.c:0"


def base_wi_id(sha):
    return f"{sha[:8]}:base.c:0"


def porting_seq(sha):
    """Full porting tool-call sequence: intent -> start -> decide -> edit -> complete -> done."""
    return [
        ("record_intent", {"commit_sha": sha, "intent_summary": "Add x"}),
        ("start_work_item", {"commit_sha": sha, "work_item_id": wi_id(sha)}),
        ("append_decision", {
            "commit_sha": sha, "work_item_id": wi_id(sha),
            "confidence": "high", "reason": "direct patch",
            "evidence": _EVIDENCE,
        }),
        ("edit_file", {
            "path": "file.c", "commit_sha": sha,
            "old_string": "int y = 2;",
            "new_string": "int x = 1;\nint y = 2;",
            "dry_run": True,
        }),
        ("edit_file", {
            "path": "file.c", "commit_sha": sha,
            "old_string": "int y = 2;",
            "new_string": "int x = 1;\nint y = 2;",
            "dry_run": False,
        }),
        ("complete_work_item", {
            "commit_sha": sha, "work_item_id": wi_id(sha),
            "status": "ported", "method": "direct_patch",
        }),
        ("signal_done", {"commit_sha": sha}),
    ]


def request_human_seq(sha):
    """Human-intervention tool-call sequence: intent -> start -> request -> done."""
    return [
        ("record_intent", {"commit_sha": sha, "intent_summary": "Add x"}),
        ("start_work_item", {"commit_sha": sha, "work_item_id": wi_id(sha)}),
        ("request_human", {
            "commit_sha": sha, "work_item_id": wi_id(sha),
            "reason": "Complex conflict at file.c:1",
        }),
        ("signal_done", {"commit_sha": sha}),
    ]


def port_file_seq(sha, filename, old_string, new_string):
    """Porting sequence that edits a single file via edit_file."""
    wid = f"{sha[:8]}:{filename}:0"
    return [
        ("record_intent", {"commit_sha": sha, "intent_summary": f"Port {filename}"}),
        ("start_work_item", {"commit_sha": sha, "work_item_id": wid}),
        ("append_decision", {
            "commit_sha": sha, "work_item_id": wid,
            "confidence": "high", "reason": "direct patch",
            "evidence": [{"file": filename, "line": 1, "snippet": old_string[:20]}],
        }),
        ("edit_file", {
            "path": filename, "commit_sha": sha,
            "old_string": old_string,
            "new_string": new_string,
            "dry_run": True,
        }),
        ("edit_file", {
            "path": filename, "commit_sha": sha,
            "old_string": old_string,
            "new_string": new_string,
            "dry_run": False,
        }),
        ("complete_work_item", {
            "commit_sha": sha, "work_item_id": wid,
            "status": "ported", "method": "direct_patch",
        }),
        ("signal_done", {"commit_sha": sha}),
    ]


def skip_seq(sha, filename):
    """Tool-call sequence for a skipped work item."""
    wid = f"{sha[:8]}:{filename}:0"
    return [
        ("record_intent", {"commit_sha": sha, "intent_summary": "Not applicable"}),
        ("start_work_item", {"commit_sha": sha, "work_item_id": wid}),
        ("append_decision", {
            "commit_sha": sha, "work_item_id": wid,
            "confidence": "low", "reason": "Not applicable to local repo",
            "evidence": _EVIDENCE,
        }),
        ("complete_work_item", {
            "commit_sha": sha, "work_item_id": wid,
            "status": "skipped",
        }),
        ("signal_done", {"commit_sha": sha}),
    ]
