"""Scenario-level integration tests for Phase 4-7 composition.

Tests multi-commit skip plus validation risk, HITL retry to final_manual,
and restart context risk line visibility.  No real API calls.  Local git repos only.
"""

import subprocess
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from vpa import ledger as L
from vpa.harness import retry_with_hint, run_promotion
from vpa.prompt import build_restart_context, build_system_prompt
from vpa.report import generate_summary
from vpa.verify import VerifyResult

# ── Mock helpers (same pattern as test_harness_int.py) ────────────────

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


# ── Fixture helpers ───────────────────────────────────────────────────

def _create_multi_commit_fixture(base_dir):
    """3 commits: initial + 2 porting. Returns (upstream, local, new, old, [sha_a, sha_b])."""
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

    # Upstream initial
    (upstream / "base.c").write_text("int base = 0;\n")
    subprocess.run(["git", "add", "."], cwd=upstream, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"], cwd=upstream, capture_output=True,
    )
    old_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=upstream,
        capture_output=True, text=True,
    ).stdout.strip()

    # Commit A
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

    # Commit B
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

    # Local matches upstream initial
    (local / "base.c").write_text("int base = 0;\n")
    subprocess.run(["git", "add", "."], cwd=local, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "local initial"],
        cwd=local, capture_output=True,
    )

    return upstream, local, sha_b, old_sha, [sha_a, sha_b]


def _create_fixture_repos(base_dir):
    """2 upstream commits (initial + porting), local matches initial."""
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

    (upstream / "file.c").write_text("int y = 2;\n")
    subprocess.run(["git", "add", "."], cwd=upstream, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"], cwd=upstream, capture_output=True,
    )
    old_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=upstream,
        capture_output=True, text=True,
    ).stdout.strip()

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

    (local / "file.c").write_text("int y = 2;\n")
    subprocess.run(["git", "add", "."], cwd=local, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "local initial"],
        cwd=local, capture_output=True,
    )

    return upstream, local, new_sha, old_sha


def _wi_id(sha):
    return f"{sha[:8]}:file.c:0"


def _base_wi_id(sha):
    return f"{sha[:8]}:base.c:0"


def _port_file_seq(sha, filename, old_string, new_string):
    """Porting sequence that edits a single file via edit_file."""
    wi_id = f"{sha[:8]}:{filename}:0"
    return [
        ("record_intent", {"commit_sha": sha, "intent_summary": f"Port {filename}"}),
        ("start_work_item", {"commit_sha": sha, "work_item_id": wi_id}),
        ("append_decision", {
            "commit_sha": sha, "work_item_id": wi_id,
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
            "commit_sha": sha, "work_item_id": wi_id,
            "status": "ported", "method": "direct_patch",
        }),
        ("signal_done", {"commit_sha": sha}),
    ]


def _request_human_seq(sha):
    """Agent requests human intervention."""
    return [
        ("record_intent", {"commit_sha": sha, "intent_summary": "Add x"}),
        ("start_work_item", {"commit_sha": sha, "work_item_id": _wi_id(sha)}),
        ("request_human", {
            "commit_sha": sha, "work_item_id": _wi_id(sha),
            "reason": "Complex conflict at file.c:1",
        }),
        ("signal_done", {"commit_sha": sha}),
    ]


_EVIDENCE = [{"file": "file.c", "line": 1, "snippet": "int x = 1;"}]


# ═══════════════════════════════════════════════════════════════════════
# Test 1 — Multi-commit skip plus validation risk
# ═══════════════════════════════════════════════════════════════════════

class TestMultiCommitSkipPlusValidationRisk(TestCase):
    """Two commits: first validation_failed, second ported.
    Re-run skips first, keeps second, surfaces risk in report."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.upstream, self.local, self.new, self.old, self.shas = \
            _create_multi_commit_fixture(self.tmp)
        self.output = self.tmp / "out"
        self.output.mkdir()

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def test_multi_commit_skip_and_validation_risk(self):
        sha_a, sha_b = self.shas

        # ── Run 1: commit A fails validation, commit B ports successfully ──
        fail = [VerifyResult(passed=False, command="make", exit_code=1)]
        passed = [VerifyResult(passed=True, command="make", exit_code=0)]

        agent1 = MockAgent([
            _port_file_seq(sha_a, "base.c",
                           "int base = 0;\n",
                           "int base = 0;\nint feat1 = 1;\n"),
            [],  # repair attempt — no tools needed
            _port_file_seq(sha_b, "base.c",
                           "int base = 0;\nint feat1 = 1;\n",
                           "int base = 0;\nint feat1 = 1;\nint feat2 = 2;\n"),
        ])
        val1 = MockValidation([fail, fail, passed])

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
        assert ledger["commits"][sha_a]["status"] == "validation_failed", \
            f"Expected validation_failed, got {ledger['commits'][sha_a]['status']}"
        assert ledger["commits"][sha_b]["status"] == "ported", \
            f"Expected ported, got {ledger['commits'][sha_b]['status']}"

        # ── Run 2: both terminal, should be skipped ──
        agent2 = MockAgent([])
        val2 = MockValidation([])
        summary, _ = run_promotion(
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

        # Assert no reprocessing
        assert agent2.call_count == 0, f"Expected 0 agent calls, got {agent2.call_count}"
        assert val2.call_count == 0, f"Expected 0 validation calls, got {val2.call_count}"

        # Assert states unchanged
        ledger = L.load_ledger(self.output / "ledger.json")
        assert ledger["commits"][sha_a]["status"] == "validation_failed"
        assert ledger["commits"][sha_b]["status"] == "ported"

        # Assert report surfaces validation risk
        assert "validation failed" in summary.lower()
        assert sha_a[:8] in summary


# ═══════════════════════════════════════════════════════════════════════
# Test 2 — HITL retry to final_manual
# ═══════════════════════════════════════════════════════════════════════

class TestHITLRetryToFinalManual(TestCase):
    """First run produces needs_human; retry with request_human → final_manual."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.upstream, self.local, self.sha, self.old = _create_fixture_repos(self.tmp)
        self.output = self.tmp / "out"
        self.output.mkdir()

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def test_hitl_retry_to_final_manual(self):
        # ── Run 1: agent requests human → needs_human ──
        agent1 = MockAgent([_request_human_seq(self.sha)])
        val1 = MockValidation([])
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
            validation_runner=val1,
        )

        ledger = L.load_ledger(self.output / "ledger.json")
        assert ledger["commits"][self.sha]["status"] == "needs_human"

        # ── Retry: agent requests human again → final_manual ──
        def mock_agent(**kwargs):
            on_tool = kwargs["on_tool_call"]
            seq = [
                ("record_intent", {"commit_sha": self.sha,
                                   "intent_summary": "Still unclear"}),
                ("start_work_item", {"commit_sha": self.sha,
                                     "work_item_id": _wi_id(self.sha)}),
                ("request_human", {
                    "commit_sha": self.sha,
                    "work_item_id": _wi_id(self.sha),
                    "reason": "Still complex after hint",
                }),
                ("signal_done", {"commit_sha": self.sha}),
            ]
            for name, args in seq:
                result = on_tool(name, args)
                if isinstance(result, dict) and "error" in result:
                    raise RuntimeError(
                        f"Tool '{name}' error: {result['error']}"
                    )
            return ("done", [])

        with patch("vpa.harness.run_agent", mock_agent):
            result = retry_with_hint(
                commit_sha=self.sha,
                hint="Try the direct approach.",
                upstream_path=str(self.upstream),
                local_path=str(self.local),
                upstream_old=self.old,
                upstream_new=self.sha,
                local_branch="main",
                output_dir=str(self.output),
                build_cmd="make",
                fast_test_cmds=["make test"],
                api_key="test-key",
            )

        # retry_with_hint returns None when final_manual
        assert result is None

        ledger = L.load_ledger(self.output / "ledger.json")
        entry = ledger["commits"][self.sha]
        assert entry["status"] == "final_manual"
        assert entry["work_items"][0]["status"] == "final_manual"

        # final_manual is terminal (cannot be retried)
        assert "final_manual" in L.HARNESS_SKIP_STATUSES
        with self.assertRaises(ValueError):
            L.start_work_item(ledger, self.sha, entry["work_items"][0]["id"])

        # Visible in report manual output
        summary = generate_summary(ledger, [], [])
        assert "final_manual" in summary.lower()
        assert self.sha[:8] in summary


# ═══════════════════════════════════════════════════════════════════════
# Test 3 — Restart context carries risk lines
# ═══════════════════════════════════════════════════════════════════════

class TestRestartContextCarriesRiskLines(TestCase):
    """Restarted agent context carries compact risk info (!V, !W, risk lines).
    No prior conversation transcript included.

    Primary path: real run_promotion with max_commits_per_restart.
    Supplementary: seeded ledger + build_restart_context."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.upstream, self.local, self.new, self.old, self.shas = \
            _create_multi_commit_fixture(self.tmp)
        self.output = self.tmp / "out"
        self.output.mkdir()

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def test_restart_context_risk_lines_via_harness(self):
        """Primary: commit A fails validation; commit B gets restart with risk info."""
        sha_a, sha_b = self.shas

        fail = [VerifyResult(passed=False, command="make", exit_code=1)]
        passed = [VerifyResult(passed=True, command="make", exit_code=0)]

        agent = MockAgent([
            _port_file_seq(sha_a, "base.c",
                           "int base = 0;\n",
                           "int base = 0;\nint feat1 = 1;\n"),
            [],  # repair — empty sequence
            _port_file_seq(sha_b, "base.c",
                           "int base = 0;\nint feat1 = 1;\n",
                           "int base = 0;\nint feat1 = 1;\nint feat2 = 2;\n"),
        ])
        val = MockValidation([fail, fail, passed])

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

        # Three agent calls: port A, repair A, port B (with restart)
        assert agent.call_count == 3, f"Expected 3 calls, got {agent.call_count}"

        restart_call = agent.captured_kwargs[2]
        restart_um = restart_call["user_message"]
        restart_sp = restart_call["system_prompt"]

        # Risk lines in user message (from build_restart_context _risk_lines)
        assert "Risk items" in restart_um
        assert "validation failed" in restart_um.lower()
        assert sha_a[:8] in restart_um

        # !V marker in system prompt ledger summary (from ledger_for_prompt)
        assert "!V" in restart_sp

        # No prior conversation transcript
        assert "Prior conversation" not in restart_um
        assert "previous messages" not in restart_um.lower()

    def test_restart_context_risk_lines_direct(self):
        """Supplementary: seeded ledger + commit_snapshot + build_restart_context."""
        sha_a, sha_b = self.shas

        meta = L.init_session_meta(
            "upstream", self.old, self.new, "local", "main", "arch",
            str(self.upstream), str(self.local), "make", ["test"], [],
        )
        ledger, _ = L.init_ledger(meta, self.output)

        L.init_commit_entry(ledger, sha_a, "Add feature 1", ["base.c", "other.c"])
        L.init_work_items(ledger, sha_a, [
            {"id": f"{sha_a[:8]}:base.c:0", "kind": "file",
             "upstream_file": "base.c", "local_file": "base.c"},
        ])
        wi = ledger["commits"][sha_a]["work_items"][0]
        L.start_work_item(ledger, sha_a, wi["id"])
        L.complete_work_item(ledger, sha_a, wi["id"], "validation_failed")
        L.record_validation(ledger, sha_a, "fast", {
            "status": "failed", "command": "make", "exit_code": 1,
            "summary": "build error in feat1",
        })
        ledger["commits"][sha_a]["warnings"].append(
            "Git verify: partial match: found base.c, missing other.c"
        )

        L.init_commit_entry(ledger, sha_b, "Add feature 2", ["base.c"])
        L.init_work_items(ledger, sha_b, [
            {"id": f"{sha_b[:8]}:base.c:0", "kind": "file",
             "upstream_file": "base.c", "local_file": "base.c"},
        ])

        snapshot = L.commit_snapshot(ledger)
        sp = build_system_prompt(
            upstream_name="upstream",
            upstream_old=self.old,
            upstream_new=self.new,
            local_name="local",
            local_branch="main",
            arch="arch",
            slice_description=f"commit {sha_b[:8]}",
            ledger_summary=L.ledger_for_prompt(ledger),
        )
        sp2, um = build_restart_context(snapshot, sp, f"commit {sha_b[:8]}")

        assert "!V" in sp2
        assert "!W" in sp2
        assert "Risk items" in um
        assert "validation failed" in um.lower()
        assert sha_a[:8] in um
        assert "Prior conversation" not in um
        assert "previous messages" not in um.lower()
