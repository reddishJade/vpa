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

    def __call__(self, **kwargs):
        self.call_count += 1
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
