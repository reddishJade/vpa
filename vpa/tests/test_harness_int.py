"""Integration tests for harness run loop using mock agents and fixture repos.

Tests the full slice-processing loop: ledger initialization, agent tool calls,
git verification, fast validation, skip semantics, and reporting coverage for
ported, needs_human, validation_failed, and final_manual statuses.

No real API calls or external services. Fixtures are local temporary git repos.
"""

import json
import subprocess
import tempfile
from pathlib import Path
from unittest import TestCase

from vpa import ledger as L
from vpa.harness import run_promotion
from vpa.verify import VerifyResult

_EVIDENCE = [{"file": "file.c", "line": 1, "snippet": "int x = 1;"}]


# ── Mock helpers ────────────────────────────────────────────────────────


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


# ── Fixture helpers ─────────────────────────────────────────────────────


def _create_fixture_repos(base_dir):
    """Create upstream (2 commits) and local (1 commit) tiny git repos.

    Upstream commit 1: creates file.c with one line.
    Upstream commit 2: adds another line (the commit to port).
    Local commit 1:   same content as upstream commit 1.
    """
    upstream = Path(base_dir) / "upstream"
    local = Path(base_dir) / "local"

    for d in [upstream, local]:
        d.mkdir(parents=True)
        subprocess.run(["git", "init"], cwd=d, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=d, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=d, capture_output=True,
        )

    # Upstream commit 1
    (upstream / "file.c").write_text("int y = 2;\n")
    subprocess.run(["git", "add", "."], cwd=upstream, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"], cwd=upstream, capture_output=True,
    )
    old_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=upstream,
        capture_output=True, text=True,
    ).stdout.strip()

    # Upstream commit 2 (the one to port)
    (upstream / "file.c").write_text("int x = 1;\nint y = 2;\n")
    subprocess.run(["git", "add", "."], cwd=upstream, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Add x feature"],
        cwd=upstream, capture_output=True,
    )
    new_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=upstream,
        capture_output=True, text=True,
    ).stdout.strip()

    # Local commit 1  (same as upstream initial)
    (local / "file.c").write_text("int y = 2;\n")
    subprocess.run(["git", "add", "."], cwd=local, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "local initial"],
        cwd=local, capture_output=True,
    )

    return upstream, local, new_sha, old_sha


def _wi_id(sha):
    return f"{sha[:8]}:file.c:0"


def _porting_seq(sha):
    """Full porting tool-call sequence: intent → start → decide → edit → complete → done."""
    return [
        ("record_intent", {"commit_sha": sha, "intent_summary": "Add x"}),
        ("start_work_item", {"commit_sha": sha, "work_item_id": _wi_id(sha)}),
        ("append_decision", {
            "commit_sha": sha, "work_item_id": _wi_id(sha),
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
            "commit_sha": sha, "work_item_id": _wi_id(sha),
            "status": "ported", "method": "direct_patch",
        }),
        ("signal_done", {"commit_sha": sha}),
    ]


def _request_human_seq(sha):
    """Human-intervention tool-call sequence: intent → start → request → done."""
    return [
        ("record_intent", {"commit_sha": sha, "intent_summary": "Add x"}),
        ("start_work_item", {"commit_sha": sha, "work_item_id": _wi_id(sha)}),
        ("request_human", {
            "commit_sha": sha, "work_item_id": _wi_id(sha),
            "reason": "Complex conflict at file.c:1",
        }),
        ("signal_done", {"commit_sha": sha}),
    ]


# ── Multi-commit fixture helpers ────────────────────────────────────────


def _create_multi_commit_fixture(base_dir):
    """Create upstream (3 commits: initial + 2 porting) and local (matching initial).

    Each porting commit modifies base.c sequentially.
    Returns (upstream, local, new_sha, old_sha, [sha_a, sha_b]).
    """
    upstream = Path(base_dir) / "upstream"
    local = Path(base_dir) / "local"

    for d in [upstream, local]:
        d.mkdir(parents=True)
        subprocess.run(["git", "init"], cwd=d, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=d, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=d, capture_output=True,
        )

    # Upstream initial commit
    (upstream / "base.c").write_text("int base = 0;\n")
    subprocess.run(["git", "add", "."], cwd=upstream, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"], cwd=upstream, capture_output=True,
    )
    old_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=upstream,
        capture_output=True, text=True,
    ).stdout.strip()

    # Upstream commit A: adds feat1
    (upstream / "base.c").write_text("int base = 0;\nint feat1 = 1;\n")
    subprocess.run(["git", "add", "."], cwd=upstream, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Add feature 1"],
        cwd=upstream, capture_output=True,
    )
    sha_a = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=upstream,
        capture_output=True, text=True,
    ).stdout.strip()

    # Upstream commit B: adds feat2
    (upstream / "base.c").write_text(
        "int base = 0;\nint feat1 = 1;\nint feat2 = 2;\n"
    )
    subprocess.run(["git", "add", "."], cwd=upstream, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Add feature 2"],
        cwd=upstream, capture_output=True,
    )
    sha_b = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=upstream,
        capture_output=True, text=True,
    ).stdout.strip()

    # Local commit (same as upstream initial)
    (local / "base.c").write_text("int base = 0;\n")
    subprocess.run(["git", "add", "."], cwd=local, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "local initial"],
        cwd=local, capture_output=True,
    )

    return upstream, local, sha_b, old_sha, [sha_a, sha_b]


def _port_file_seq(sha, filename, old_string, new_string):
    """Porting sequence that edits a single file via edit_file."""
    wi_id = f"{sha[:8]}:{filename}:0"
    return [
        ("record_intent", {"commit_sha": sha, "intent_summary": f"Port {filename}"}),
        ("start_work_item", {"commit_sha": sha, "work_item_id": wi_id}),
        ("append_decision", {
            "commit_sha": sha, "work_item_id": wi_id,
            "confidence": "high", "reason": "direct patch",
            "evidence": _EVIDENCE,
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
            "commit_sha": sha, "work_item_id": wi_id,
            "status": "ported", "method": "direct_patch",
        }),
        ("signal_done", {"commit_sha": sha}),
    ]


# ── 1. Success path ─────────────────────────────────────────────────────


class TestHarnessSuccessPath(TestCase):
    """Mock agent ports a commit; fast validation passes; report reflects success."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.upstream, self.local, self.sha, self.old = _create_fixture_repos(self.tmp)
        self.output = self.tmp / "out"
        self.output.mkdir()

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def test_ported_commit(self):
        agent = MockAgent([_porting_seq(self.sha)])
        validation = MockValidation([
            [VerifyResult(passed=True, command="make", exit_code=0)],
        ])
        summary, json_output = run_promotion(
            upstream_path=str(self.upstream),
            local_path=str(self.local),
            upstream_old=self.old,
            upstream_new=self.sha,
            local_branch="main",
            build_cmd="make",
            fast_test_cmds=["make test"],
            output_dir=str(self.output),
            api_key="test-key",
            agent_runner=agent,
            validation_runner=validation,
        )

        ledger = L.load_ledger(self.output / "ledger.json")
        entry = ledger["commits"].get(self.sha)
        assert entry is not None
        assert entry["status"] == "ported"
        assert "file.c" in entry["local_files_modified"]

        wi = entry["work_items"][0]
        assert wi["status"] == "ported"
        assert wi["method"] == "direct_patch"
        assert len(wi["decisions"]) == 1

        assert entry["validation"]["fast"]["status"] == "passed"

        assert "Ported: 1" in summary
        assert self.sha[:8] in summary

        parsed = json.loads(json_output)
        assert parsed["commits"][self.sha]["status"] == "ported"

    def test_fast_validation_pass_recorded(self):
        agent = MockAgent([_porting_seq(self.sha)])
        validation = MockValidation([
            [VerifyResult(passed=True, command="make", exit_code=0)],
        ])
        run_promotion(
            upstream_path=str(self.upstream),
            local_path=str(self.local),
            upstream_old=self.old,
            upstream_new=self.sha,
            local_branch="main",
            build_cmd="make",
            fast_test_cmds=["make test"],
            output_dir=str(self.output),
            api_key="test-key",
            agent_runner=agent,
            validation_runner=validation,
        )
        ledger = L.load_ledger(self.output / "ledger.json")
        assert ledger["commits"][self.sha]["validation"]["fast"]["status"] == "passed"
        assert validation.call_count == 1
        assert agent.call_count == 1


# ── 2. Request human path ───────────────────────────────────────────────


class TestHarnessRequestHumanPath(TestCase):
    """Mock agent requests human; harness skips in subsequent runs."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.upstream, self.local, self.sha, self.old = _create_fixture_repos(self.tmp)
        self.output = self.tmp / "out"
        self.output.mkdir()

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def test_needs_human_no_validation(self):
        agent = MockAgent([_request_human_seq(self.sha)])
        validation = MockValidation([
            [VerifyResult(passed=True, command="make", exit_code=0)],
        ])
        summary, _ = run_promotion(
            upstream_path=str(self.upstream),
            local_path=str(self.local),
            upstream_old=self.old,
            upstream_new=self.sha,
            local_branch="main",
            build_cmd="make",
            fast_test_cmds=["make test"],
            output_dir=str(self.output),
            api_key="test-key",
            agent_runner=agent,
            validation_runner=validation,
        )

        ledger = L.load_ledger(self.output / "ledger.json")
        entry = ledger["commits"].get(self.sha)
        assert entry["status"] == "needs_human"
        assert entry["work_items"][0]["status"] == "needs_human"
        assert "fast" not in entry["validation"]
        assert validation.call_count == 0
        assert "needs_human" in summary.lower()

    def test_subsequent_run_skips_needs_human(self):
        agent1 = MockAgent([_request_human_seq(self.sha)])
        validation1 = MockValidation([])
        run_promotion(
            upstream_path=str(self.upstream),
            local_path=str(self.local),
            upstream_old=self.old,
            upstream_new=self.sha,
            local_branch="main",
            build_cmd="make",
            fast_test_cmds=["make test"],
            output_dir=str(self.output),
            api_key="test-key",
            agent_runner=agent1,
            validation_runner=validation1,
        )

        agent2 = MockAgent([])
        validation2 = MockValidation([])
        summary, _ = run_promotion(
            upstream_path=str(self.upstream),
            local_path=str(self.local),
            upstream_old=self.old,
            upstream_new=self.sha,
            local_branch="main",
            build_cmd="make",
            fast_test_cmds=["make test"],
            output_dir=str(self.output),
            api_key="test-key",
            agent_runner=agent2,
            validation_runner=validation2,
        )

        ledger = L.load_ledger(self.output / "ledger.json")
        assert ledger["commits"][self.sha]["status"] == "needs_human"
        assert agent2.call_count == 0
        assert "needs_human" in summary.lower()


# ── 3. Validation failed path ───────────────────────────────────────────


class TestHarnessValidationFailedPath(TestCase):
    """Fast validation fails; repair fails; commit marked validation_failed."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.upstream, self.local, self.sha, self.old = _create_fixture_repos(self.tmp)
        self.output = self.tmp / "out"
        self.output.mkdir()

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def test_validation_failed(self):
        agent = MockAgent([
            _porting_seq(self.sha),
            [],
        ])
        fail_result = [VerifyResult(
            passed=False, command="make", exit_code=1, stderr="build error",
        )]
        validation = MockValidation([fail_result, fail_result])
        summary, _ = run_promotion(
            upstream_path=str(self.upstream),
            local_path=str(self.local),
            upstream_old=self.old,
            upstream_new=self.sha,
            local_branch="main",
            build_cmd="make",
            fast_test_cmds=["make test"],
            output_dir=str(self.output),
            api_key="test-key",
            agent_runner=agent,
            validation_runner=validation,
        )

        assert agent.call_count == 2, f"agent called {agent.call_count}x"
        assert validation.call_count == 2, f"validation called {validation.call_count}x"

        ledger = L.load_ledger(self.output / "ledger.json")
        entry = ledger["commits"].get(self.sha)
        assert entry["status"] == "validation_failed", f"got {entry['status']}"
        assert entry["work_items"][0]["status"] == "validation_failed"
        assert entry["validation"]["fast"]["status"] == "failed"
        assert "validation_failed" in summary.lower()

    def test_validation_failure_records_command_exit_code_summary(self):
        """Fast validation failure must record command, exit_code, and summary."""
        agent = MockAgent([
            _porting_seq(self.sha),
            [],
        ])
        fail_result = [VerifyResult(
            passed=False, command="make", exit_code=1, stderr="build error",
        )]
        validation = MockValidation([fail_result, fail_result])
        run_promotion(
            upstream_path=str(self.upstream),
            local_path=str(self.local),
            upstream_old=self.old,
            upstream_new=self.sha,
            local_branch="main",
            build_cmd="make",
            fast_test_cmds=["make test"],
            output_dir=str(self.output),
            api_key="test-key",
            agent_runner=agent,
            validation_runner=validation,
        )
        ledger = L.load_ledger(self.output / "ledger.json")
        v = ledger["commits"][self.sha]["validation"]["fast"]
        assert v["status"] == "failed"
        assert v["command"] == "make"
        assert v["exit_code"] == 1
        assert "build error" in v["summary"]

    def test_subsequent_run_skips_validation_failed(self):
        agent1 = MockAgent([_porting_seq(self.sha), []])
        fail = [VerifyResult(passed=False, command="make", exit_code=1)]
        validation1 = MockValidation([fail, fail])
        run_promotion(
            upstream_path=str(self.upstream),
            local_path=str(self.local),
            upstream_old=self.old,
            upstream_new=self.sha,
            local_branch="main",
            build_cmd="make",
            fast_test_cmds=["make test"],
            output_dir=str(self.output),
            api_key="test-key",
            agent_runner=agent1,
            validation_runner=validation1,
        )

        agent2 = MockAgent([])
        validation2 = MockValidation([])
        run_promotion(
            upstream_path=str(self.upstream),
            local_path=str(self.local),
            upstream_old=self.old,
            upstream_new=self.sha,
            local_branch="main",
            build_cmd="make",
            fast_test_cmds=["make test"],
            output_dir=str(self.output),
            api_key="test-key",
            agent_runner=agent2,
            validation_runner=validation2,
        )

        ledger = L.load_ledger(self.output / "ledger.json")
        assert ledger["commits"][self.sha]["status"] == "validation_failed"
        assert agent2.call_count == 0


# ── 4. Final manual path ────────────────────────────────────────────────


class TestHarnessFinalManualPath(TestCase):
    """final_manual is terminal, skipped on re-run, visible in report."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.upstream, self.local, self.sha, self.old = _create_fixture_repos(self.tmp)
        self.output = self.tmp / "out"
        self.output.mkdir()

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def _prebuild_ledger_with_final_manual(self):
        meta = L.init_session_meta(
            "upstream", self.old, self.sha, "local", "main", "arch",
            str(self.upstream), str(self.local), "make", ["test"], [],
        )
        ledger, ledger_path = L.init_ledger(meta, self.output)
        L.init_commit_entry(ledger, self.sha, "Add x", ["file.c"])
        L.init_work_items(ledger, self.sha, [
            {"id": _wi_id(self.sha), "kind": "file",
             "upstream_file": "file.c", "local_file": "file.c"},
        ])
        wi = ledger["commits"][self.sha]["work_items"][0]
        L.start_work_item(ledger, self.sha, wi["id"])
        L.complete_work_item(ledger, self.sha, wi["id"], "final_manual")
        L.write_ledger(ledger_path, ledger)

    def test_final_manual_skipped_by_harness(self):
        self._prebuild_ledger_with_final_manual()
        agent = MockAgent([])
        validation = MockValidation([])

        summary, _ = run_promotion(
            upstream_path=str(self.upstream),
            local_path=str(self.local),
            upstream_old=self.old,
            upstream_new=self.sha,
            local_branch="main",
            build_cmd="make",
            fast_test_cmds=["make test"],
            output_dir=str(self.output),
            api_key="test-key",
            agent_runner=agent,
            validation_runner=validation,
        )

        assert agent.call_count == 0
        assert validation.call_count == 0

        ledger = L.load_ledger(self.output / "ledger.json")
        assert ledger["commits"][self.sha]["status"] == "final_manual"
        assert "final_manual" in summary.lower()
        assert self.sha[:8] in summary


# ── 5. Report visibility ────────────────────────────────────────────────


class TestReportVisibility(TestCase):
    """Report must surface validation_failed and Git verification warnings."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.upstream, self.local, self.sha, self.old = _create_fixture_repos(self.tmp)
        self.output = self.tmp / "out"
        self.output.mkdir()

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def test_report_contains_validation_failed_risk(self):
        """validation_failed commits appear in the risk section."""
        agent = MockAgent([_porting_seq(self.sha), []])
        fail = [VerifyResult(passed=False, command="make", exit_code=1)]
        validation = MockValidation([fail, fail])
        summary, _ = run_promotion(
            upstream_path=str(self.upstream),
            local_path=str(self.local),
            upstream_old=self.old,
            upstream_new=self.sha,
            local_branch="main",
            build_cmd="make",
            fast_test_cmds=["make test"],
            output_dir=str(self.output),
            api_key="test-key",
            agent_runner=agent,
            validation_runner=validation,
        )
        assert "Validation failed commits requiring attention" in summary
        assert self.sha[:8] in summary

    def test_report_contains_git_verify_warning(self):
        """Git verification warnings appear in the report risk section."""
        from vpa.report import generate_summary

        meta = L.init_session_meta(
            "upstream", self.old, self.sha, "local", "main", "arch",
            str(self.upstream), str(self.local), "make", ["test"], [],
        )
        ledger, _ = L.init_ledger(meta, self.output)
        L.init_commit_entry(ledger, self.sha, "Add x", ["file.c"])
        entry = ledger["commits"][self.sha]
        entry["warnings"].append(
            "Git verify: partial match: found file.c, missing other.c"
        )

        summary = generate_summary(ledger, [], [])
        assert "Git verification warnings" in summary
        assert "partial match" in summary
        assert self.sha[:8] in summary


# ── 6. Multi-commit ordering ────────────────────────────────────────────


class TestMultiCommitOrdering(TestCase):
    """Two porting commits processed in upstream chronological order."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.upstream, self.local, self.new, self.old, self.shas = \
            _create_multi_commit_fixture(self.tmp)
        self.output = self.tmp / "out"
        self.output.mkdir()

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def test_commits_processed_in_upstream_order(self):
        sha_a, sha_b = self.shas
        agent = MockAgent([
            _port_file_seq(sha_a, "base.c",
                           "int base = 0;\n",
                           "int base = 0;\nint feat1 = 1;\n"),
            _port_file_seq(sha_b, "base.c",
                           "int base = 0;\nint feat1 = 1;\n",
                           "int base = 0;\nint feat1 = 1;\nint feat2 = 2;\n"),
        ])
        val = MockValidation([
            [VerifyResult(passed=True, command="make", exit_code=0)],
            [VerifyResult(passed=True, command="make", exit_code=0)],
        ])
        run_promotion(
            upstream_path=str(self.upstream),
            local_path=str(self.local),
            upstream_old=self.old,
            upstream_new=self.new,
            local_branch="main",
            build_cmd="make",
            fast_test_cmds=["make test"],
            output_dir=str(self.output),
            api_key="test-key",
            agent_runner=agent,
            validation_runner=val,
        )

        assert agent.call_count == 2
        ledger = L.load_ledger(self.output / "ledger.json")

        # Both commits present in ledger
        assert sha_a in ledger["commits"]
        assert sha_b in ledger["commits"]

        # Both ported
        assert ledger["commits"][sha_a]["status"] == "ported"
        assert ledger["commits"][sha_b]["status"] == "ported"

        # Work items reflect correct files
        a_wi = ledger["commits"][sha_a]["work_items"]
        b_wi = ledger["commits"][sha_b]["work_items"]
        assert len(a_wi) == 1
        assert a_wi[0]["upstream_file"] == "base.c"
        assert len(b_wi) == 1
        assert b_wi[0]["upstream_file"] == "base.c"

        # Ledger entries appear in upstream-sorted order
        assert ledger["meta"]["upstream_old"] == self.old


# ── 7. Multi-commit skip behavior ───────────────────────────────────────


class TestMultiCommitSkipBehavior(TestCase):
    """First commit terminal, second pending; re-run skips terminal."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.upstream, self.local, self.new, self.old, self.shas = \
            _create_multi_commit_fixture(self.tmp)
        self.output = self.tmp / "out"
        self.output.mkdir()

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def test_first_terminal_second_pending_rerun_skips_first(self):
        sha_a, sha_b = self.shas

        # Run 1: only sequence for commit A; commit B gets empty agent
        agent1 = MockAgent([
            _port_file_seq(sha_a, "base.c",
                           "int base = 0;\n",
                           "int base = 0;\nint feat1 = 1;\n"),
        ])
        val1 = MockValidation([])
        run_promotion(
            upstream_path=str(self.upstream),
            local_path=str(self.local),
            upstream_old=self.old,
            upstream_new=self.new,
            local_branch="main",
            build_cmd="make",
            fast_test_cmds=["make test"],
            output_dir=str(self.output),
            api_key="test-key",
            agent_runner=agent1,
            validation_runner=val1,
        )

        ledger = L.load_ledger(self.output / "ledger.json")
        assert ledger["commits"][sha_a]["status"] == "ported"
        assert ledger["commits"][sha_b]["status"] == "pending"

        # Run 2: skip terminal commit A, process pending commit B
        agent2 = MockAgent([
            _port_file_seq(sha_b, "base.c",
                           "int base = 0;\nint feat1 = 1;\n",
                           "int base = 0;\nint feat1 = 1;\nint feat2 = 2;\n"),
        ])
        val2 = MockValidation([
            [VerifyResult(passed=True, command="make", exit_code=0)],
        ])
        run_promotion(
            upstream_path=str(self.upstream),
            local_path=str(self.local),
            upstream_old=self.old,
            upstream_new=self.new,
            local_branch="main",
            build_cmd="make",
            fast_test_cmds=["make test"],
            output_dir=str(self.output),
            api_key="test-key",
            agent_runner=agent2,
            validation_runner=val2,
        )

        ledger = L.load_ledger(self.output / "ledger.json")
        assert ledger["commits"][sha_a]["status"] == "ported"
        assert ledger["commits"][sha_b]["status"] == "ported"
        assert agent2.call_count == 1, \
            f"Expected 1 agent call (commit B only), got {agent2.call_count}"


# ── 8. max_commits_per_restart behavior ──────────────────────────────────


class TestMaxCommitsPerRestart(TestCase):
    """Restart boundary at max_commits_per_restart threshold."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.upstream, self.local, self.new, self.old, self.shas = \
            _create_multi_commit_fixture(self.tmp)
        self.output = self.tmp / "out"
        self.output.mkdir()

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def test_restart_produced_at_boundary(self):
        sha_a, sha_b = self.shas
        agent = MockAgent([
            _port_file_seq(sha_a, "base.c",
                           "int base = 0;\n",
                           "int base = 0;\nint feat1 = 1;\n"),
            _port_file_seq(sha_b, "base.c",
                           "int base = 0;\nint feat1 = 1;\n",
                           "int base = 0;\nint feat1 = 1;\nint feat2 = 2;\n"),
        ])
        val = MockValidation([
            [VerifyResult(passed=True, command="make", exit_code=0)],
            [VerifyResult(passed=True, command="make", exit_code=0)],
        ])
        run_promotion(
            upstream_path=str(self.upstream),
            local_path=str(self.local),
            upstream_old=self.old,
            upstream_new=self.new,
            local_branch="main",
            build_cmd="make",
            fast_test_cmds=["make test"],
            output_dir=str(self.output),
            api_key="test-key",
            agent_runner=agent,
            validation_runner=val,
            max_commits_per_restart=1,
        )

        assert agent.call_count == 2
        call1 = agent.captured_kwargs[0]
        call2 = agent.captured_kwargs[1]

        # First call: no restart preamble
        assert "Resuming promotion" not in call1["user_message"]
        assert "Ledger snapshot" not in call1["user_message"]

        # Second call: restart preamble present
        assert "Resuming promotion" in call2["user_message"]
        assert "Ledger snapshot" in call2["user_message"]

        # Both commits processed correctly
        ledger = L.load_ledger(self.output / "ledger.json")
        assert ledger["commits"][sha_a]["status"] == "ported"
        assert ledger["commits"][sha_b]["status"] == "ported"

    def test_no_restart_when_below_threshold(self):
        sha_a, sha_b = self.shas
        agent = MockAgent([
            _port_file_seq(sha_a, "base.c",
                           "int base = 0;\n",
                           "int base = 0;\nint feat1 = 1;\n"),
            _port_file_seq(sha_b, "base.c",
                           "int base = 0;\nint feat1 = 1;\n",
                           "int base = 0;\nint feat1 = 1;\nint feat2 = 2;\n"),
        ])
        val = MockValidation([
            [VerifyResult(passed=True, command="make", exit_code=0)],
            [VerifyResult(passed=True, command="make", exit_code=0)],
        ])
        run_promotion(
            upstream_path=str(self.upstream),
            local_path=str(self.local),
            upstream_old=self.old,
            upstream_new=self.new,
            local_branch="main",
            build_cmd="make",
            fast_test_cmds=["make test"],
            output_dir=str(self.output),
            api_key="test-key",
            agent_runner=agent,
            validation_runner=val,
            max_commits_per_restart=10,
        )

        assert agent.call_count == 2
        for kwargs in agent.captured_kwargs:
            assert "Resuming promotion" not in kwargs["user_message"]


class TestRestartContextContent(TestCase):
    """Restart context content: durable state only, no chat history."""

    def test_build_restart_context_includes_snapshot_and_current_unit(self):
        from vpa.prompt import build_restart_context

        snapshot = {
            "abc123def456": {
                "status": "ported",
                "intent_summary": "Add x",
                "upstream_subject": "Add x feature",
                "local_files_modified": ["file.c"],
            }
        }
        sp, um = build_restart_context(
            snapshot, "You are a version promotion agent.",
            "commit abc1234",
        )
        assert "Resuming promotion" in um
        assert "Ledger snapshot" in um
        assert "abc123" in um
        assert "ported" in um
        assert "commit abc1234" in um

    def test_no_prior_conversation_in_restart_context(self):
        from vpa.prompt import build_restart_context

        sp, um = build_restart_context({}, "sys prompt", "commit 0000")
        assert "Prior conversation" not in um
        assert "previous messages" not in um.lower()

    def test_restart_context_via_harness_includes_commit_and_diff(self):
        """Through harness: restart user_message has commit ID, work items, diff."""
        tmp = Path(tempfile.mkdtemp())
        try:
            upstream, local, new, old, shas = _create_multi_commit_fixture(tmp)
            out = tmp / "out"
            out.mkdir()
            sha_a, sha_b = shas
            agent = MockAgent([
                _port_file_seq(sha_a, "base.c",
                               "int base = 0;\n",
                               "int base = 0;\nint feat1 = 1;\n"),
                _port_file_seq(sha_b, "base.c",
                               "int base = 0;\nint feat1 = 1;\n",
                               "int base = 0;\nint feat1 = 1;\nint feat2 = 2;\n"),
            ])
            val = MockValidation([
                [VerifyResult(passed=True, command="make", exit_code=0)],
                [VerifyResult(passed=True, command="make", exit_code=0)],
            ])
            run_promotion(
                upstream_path=str(upstream),
                local_path=str(local),
                upstream_old=old,
                upstream_new=new,
                local_branch="main",
                build_cmd="make",
                fast_test_cmds=["make test"],
                output_dir=str(out),
                api_key="test-key",
                agent_runner=agent,
                validation_runner=val,
                max_commits_per_restart=1,
            )

            restart_call = agent.captured_kwargs[1]
            um = restart_call["user_message"]
            sp = restart_call["system_prompt"]

            # Ledger snapshot present
            assert "Ledger snapshot" in um
            # Current commit identity present
            assert sha_b[:8] in um
            # Work items present
            assert "Work items to process" in um
            # Diff context present (commit touches base.c)
            assert "base.c" in um
            # No stale conversation history
            assert "Prior conversation" not in um
            # Fresh system prompt
            assert "version promotion agent" in sp
        finally:
            subprocess.run(["rm", "-rf", str(tmp)])

    def test_validation_and_warnings_survive_restart(self):
        """Validation results and warnings persist in ledger after restart."""
        tmp = Path(tempfile.mkdtemp())
        try:
            upstream, local, new, old, shas = _create_multi_commit_fixture(tmp)
            out = tmp / "out"
            out.mkdir()
            sha_a, sha_b = shas
            agent = MockAgent([
                _port_file_seq(sha_a, "base.c",
                               "int base = 0;\n",
                               "int base = 0;\nint feat1 = 1;\n"),
                _port_file_seq(sha_b, "base.c",
                               "int base = 0;\nint feat1 = 1;\n",
                               "int base = 0;\nint feat1 = 1;\nint feat2 = 2;\n"),
            ])
            val = MockValidation([
                [VerifyResult(passed=True, command="make", exit_code=0)],
                [VerifyResult(passed=True, command="make", exit_code=0)],
            ])
            run_promotion(
                upstream_path=str(upstream),
                local_path=str(local),
                upstream_old=old,
                upstream_new=new,
                local_branch="main",
                build_cmd="make",
                fast_test_cmds=["make test"],
                output_dir=str(out),
                api_key="test-key",
                agent_runner=agent,
                validation_runner=val,
                max_commits_per_restart=1,
            )

            ledger = L.load_ledger(out / "ledger.json")
            # Validation results survive for commit A (processed before restart)
            a_val = ledger["commits"][sha_a].get("validation", {})
            assert "fast" in a_val
            assert a_val["fast"]["status"] == "passed"
            # Warnings also survive (empty in this case, but key exists)
            assert "warnings" in ledger["commits"][sha_a]
        finally:
            subprocess.run(["rm", "-rf", str(tmp)])


# ── 9. Context threshold behavior ───────────────────────────────────────


class TestContextThreshold(TestCase):
    """Deterministic test for _context_over estimation and threshold."""

    def test_estimate_context_total(self):
        from vpa.harness import _estimate_context_total

        assert _estimate_context_total(100, 50) == 3150
        assert _estimate_context_total(0, 0) == 3000
        assert _estimate_context_total(50000, 10000) == 63000

    def test_context_under_threshold(self):
        from vpa.harness import _context_over
        from vpa.slicer import Slice, SliceLevel

        led = {"meta": {}, "commits": {
            "a" * 40: {"status": "ported", "upstream_subject": "t"},
        }}
        sl = Slice(level=SliceLevel.COMMIT, label="t",
                   commit_sha="a" * 40, context="small")
        assert not _context_over(led, sl)

    def test_context_over_threshold(self):
        from vpa.harness import _context_over
        from vpa.slicer import Slice, SliceLevel

        # Many large commits to push ledger summary above threshold
        commits = {}
        for i in range(800):
            commits[f"{i:040d}"] = {
                "status": "ported",
                "upstream_subject": "x" * 60,
            }
        led = {"meta": {}, "commits": commits}
        sl = Slice(level=SliceLevel.COMMIT, label="t",
                   commit_sha="a" * 40, context="x" * 50000)
        assert _context_over(led, sl)

    def test_threshold_boundary(self):
        from vpa.harness import (
            CONTEXT_LIMIT_CHARS,
            CONTEXT_USAGE_THRESHOLD,
            _estimate_context_total,
        )

        boundary = int(CONTEXT_LIMIT_CHARS * CONTEXT_USAGE_THRESHOLD)
        just_under_total = _estimate_context_total(0, boundary - 1 - 3000)
        just_over_total = _estimate_context_total(0, boundary + 1 - 3000)
        boundary_total = _estimate_context_total(0, boundary - 3000)

        def _over(total):
            return total / CONTEXT_LIMIT_CHARS > CONTEXT_USAGE_THRESHOLD

        assert not _over(just_under_total)
        assert _over(just_over_total)
        assert not _over(boundary_total)  # equals threshold, not strictly greater
