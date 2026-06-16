"""Tests for Phase 7: HITL retry hardening + restart risk visibility.

Tests request_human, retry_with_hint, pending-after-retry, repeated
request_human, and restart context risk surfacing.

No real API calls. Uses fixture repos and mock agents/validation.
"""

import subprocess
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from vpa import ledger as L
from vpa.harness import _resolve_retry_cleanup, retry_with_hint
from vpa.prompt import build_hint_injection, build_restart_context
from vpa.verify import VerifyResult

_EVIDENCE = [{"file": "file.c", "line": 1, "snippet": "int x = 1;"}]


# ── Fixture helpers ─────────────────────────────────────────────────


def _create_fixture_repos(base_dir):
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


def _build_needs_human_ledger(output_dir, upstream, local, sha, old):
    meta = L.init_session_meta(
        "upstream", old, sha, "local", "main", "arch",
        str(upstream), str(local), "make", ["test"], [],
    )
    ledger, ledger_path = L.init_ledger(meta, output_dir)
    L.init_commit_entry(ledger, sha, "Add x", ["file.c"])
    L.init_work_items(ledger, sha, [
        {"id": _wi_id(sha), "kind": "file",
         "upstream_file": "file.c", "local_file": "file.c"},
    ])
    wi = ledger["commits"][sha]["work_items"][0]
    L.start_work_item(ledger, sha, wi["id"])
    L.complete_work_item(ledger, sha, wi["id"], "needs_human")
    L.write_ledger(ledger_path, ledger)
    return ledger, ledger_path


# ── 1. request_human creates durable manual state ───────────────────


class TestRequestHumanDurableState(TestCase):
    """request_human must produce a durable ledger/manual state."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.upstream, self.local, self.sha, self.old = _create_fixture_repos(self.tmp)
        self.output = self.tmp / "out"
        self.output.mkdir()

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def test_request_human_persists_to_ledger(self):
        _, ledger_path = _build_needs_human_ledger(
            self.output, self.upstream, self.local, self.sha, self.old,
        )
        reloaded = L.load_ledger(ledger_path)
        entry = reloaded["commits"][self.sha]
        assert entry["status"] == "needs_human"
        assert entry["work_items"][0]["status"] == "needs_human"

    def test_request_human_does_not_result_in_ported(self):
        _, ledger_path = _build_needs_human_ledger(
            self.output, self.upstream, self.local, self.sha, self.old,
        )
        reloaded = L.load_ledger(ledger_path)
        entry = reloaded["commits"][self.sha]
        assert entry["status"] != "ported"
        assert entry["status"] == "needs_human"


# ── 2. retry_with_hint context reconstruction ──────────────────────


class TestRetryContextReconstruction(TestCase):
    """retry_with_hint rebuilds context from prompt + ledger + diff + hint."""

    def test_hint_injection_format(self):
        hint = "Check the local struct definition."
        block = build_hint_injection(hint)
        assert hint in block
        assert "Human Review Note" in block

    def test_hint_append_to_system_prompt(self):
        from vpa.prompt import build_system_prompt

        hint = "Use semantic_port for this file."
        base = build_system_prompt()
        sp = base + "\n\n" + build_hint_injection(hint)
        assert hint in sp


# ── 3. retry_with_hint success path ────────────────────────────────


class TestRetryWithHintSuccess(TestCase):
    """retry_with_hint: agent ports, validation passes."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.upstream, self.local, self.sha, self.old = _create_fixture_repos(self.tmp)
        self.output = self.tmp / "out"
        self.output.mkdir()
        self.ledger, self.ledger_path = _build_needs_human_ledger(
            self.output, self.upstream, self.local, self.sha, self.old,
        )

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def test_retry_reset_then_port(self):
        entry = self.ledger["commits"][self.sha]
        reset_ids = {wi["id"] for wi in entry["work_items"]}
        L.reset_for_retry(self.ledger, self.sha)
        assert entry["work_items"][0]["status"] == "pending"

        wi = entry["work_items"][0]
        L.start_work_item(self.ledger, self.sha, wi["id"])
        L.append_decision(self.ledger, self.sha, wi["id"], "high",
                          "direct patch after hint", _EVIDENCE)
        L.complete_work_item(self.ledger, self.sha, wi["id"], "ported",
                             method="direct_patch")
        L.write_ledger(self.ledger_path, self.ledger)

        _resolve_retry_cleanup(self.ledger, self.sha, reset_ids, self.ledger_path)
        reloaded = L.load_ledger(self.ledger_path)
        assert reloaded["commits"][self.sha]["status"] == "ported"

    def test_retry_skipped_item_not_touched_by_cleanup(self):
        entry = self.ledger["commits"][self.sha]
        reset_ids = {wi["id"] for wi in entry["work_items"]}
        L.reset_for_retry(self.ledger, self.sha)

        wi = entry["work_items"][0]
        L.start_work_item(self.ledger, self.sha, wi["id"])
        L.append_decision(self.ledger, self.sha, wi["id"], "medium",
                          "skip after review", _EVIDENCE)
        L.complete_work_item(self.ledger, self.sha, wi["id"], "skipped")
        L.write_ledger(self.ledger_path, self.ledger)

        _resolve_retry_cleanup(self.ledger, self.sha, reset_ids, self.ledger_path)
        reloaded = L.load_ledger(self.ledger_path)
        assert reloaded["commits"][self.sha]["status"] == "skipped"


# ── 4. Repeated request_human after retry → final_manual ──────────


class TestRepeatedRequestHumanAfterRetry(TestCase):
    """If retry agent calls request_human again, items become final_manual."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.upstream, self.local, self.sha, self.old = _create_fixture_repos(self.tmp)
        self.output = self.tmp / "out"
        self.output.mkdir()
        self.ledger, self.ledger_path = _build_needs_human_ledger(
            self.output, self.upstream, self.local, self.sha, self.old,
        )

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def test_repeated_request_human_becomes_final_manual(self):
        entry = self.ledger["commits"][self.sha]
        reset_ids = {wi["id"] for wi in entry["work_items"]}
        L.reset_for_retry(self.ledger, self.sha)

        wi = entry["work_items"][0]
        L.start_work_item(self.ledger, self.sha, wi["id"])
        L.append_decision(self.ledger, self.sha, wi["id"], "low",
                          "still unclear", _EVIDENCE)
        L.complete_work_item(self.ledger, self.sha, wi["id"], "needs_human")
        L.write_ledger(self.ledger_path, self.ledger)

        _resolve_retry_cleanup(self.ledger, self.sha, reset_ids, self.ledger_path)
        reloaded = L.load_ledger(self.ledger_path)
        assert reloaded["commits"][self.sha]["status"] == "final_manual"
        assert reloaded["commits"][self.sha]["work_items"][0]["status"] == "final_manual"

    def test_final_manual_terminal_no_further_retry(self):
        entry = self.ledger["commits"][self.sha]
        wi = entry["work_items"][0]
        entry["work_items"][0]["status"] = "final_manual"
        L._derive_commit_status(entry)
        L.write_ledger(self.ledger_path, self.ledger)

        with self.assertRaises(ValueError):
            L.start_work_item(self.ledger, self.sha, wi["id"])
        with self.assertRaises(ValueError):
            L.complete_work_item(self.ledger, self.sha, wi["id"], "ported")


# ── 5. Pending items after retry → final_manual ────────────────────


class TestPendingItemsAfterRetry(TestCase):
    """Items left pending after retry are cleaned up to final_manual."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.upstream, self.local, self.sha, self.old = _create_fixture_repos(self.tmp)
        self.output = self.tmp / "out"
        self.output.mkdir()
        self.ledger, self.ledger_path = _build_needs_human_ledger(
            self.output, self.upstream, self.local, self.sha, self.old,
        )

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def test_pending_items_after_retry_go_to_final_manual(self):
        entry = self.ledger["commits"][self.sha]
        reset_ids = {wi["id"] for wi in entry["work_items"]}
        L.reset_for_retry(self.ledger, self.sha)
        L.write_ledger(self.ledger_path, self.ledger)

        _resolve_retry_cleanup(self.ledger, self.sha, reset_ids, self.ledger_path)
        reloaded = L.load_ledger(self.ledger_path)
        assert reloaded["commits"][self.sha]["status"] == "final_manual"
        assert reloaded["commits"][self.sha]["work_items"][0]["status"] == "final_manual"

    def test_mixed_ported_and_pending_after_retry(self):
        entry = self.ledger["commits"][self.sha]
        # Add a second work item directly
        wi2_id = f"{self.sha[:8]}:file.c:1"
        entry["work_items"].append({
            "id": wi2_id, "kind": "file",
            "upstream_file": "file.c", "local_file": "file.c",
            "status": "pending", "method": None,
            "attempt_count": 0, "decisions": [],
        })
        wi0 = entry["work_items"][0]
        wi1 = entry["work_items"][1]
        L.start_work_item(self.ledger, self.sha, wi1["id"])
        L.complete_work_item(self.ledger, self.sha, wi1["id"], "needs_human")
        L.write_ledger(self.ledger_path, self.ledger)

        reset_ids = {wi["id"] for wi in entry["work_items"]}
        L.reset_for_retry(self.ledger, self.sha)

        L.start_work_item(self.ledger, self.sha, wi0["id"])
        L.append_decision(self.ledger, self.sha, wi0["id"], "high",
                          "done", _EVIDENCE)
        L.complete_work_item(self.ledger, self.sha, wi0["id"], "ported",
                             method="direct_patch")
        L.write_ledger(self.ledger_path, self.ledger)

        _resolve_retry_cleanup(self.ledger, self.sha, reset_ids, self.ledger_path)
        reloaded = L.load_ledger(self.ledger_path)
        entry2 = reloaded["commits"][self.sha]
        assert entry2["work_items"][0]["status"] == "ported"
        assert entry2["work_items"][1]["status"] == "final_manual"
        assert entry2["status"] == "final_manual"


# ── 6. Restart context risk visibility ─────────────────────────────


class TestRestartContextRiskVisibility(TestCase):
    """Restart context must surface validation failures and git warnings."""

    def test_validation_failed_in_restart_context(self):
        snapshot = {
            "abc123def456": {
                "status": "validation_failed",
                "upstream_subject": "Add feature",
                "validation_failed": "fast:make -- build error",
                "warnings": [],
            }
        }
        sp, um = build_restart_context(snapshot, "sys", "commit x")
        assert "Risk items" in um
        assert "validation failed" in um
        assert "abc123" in um

    def test_git_warning_in_restart_context(self):
        snapshot = {
            "abc123def456": {
                "status": "ported",
                "upstream_subject": "Add feature",
                "validation_failed": None,
                "warnings": ["Git verify: partial match: found file.c, missing other.c"],
            }
        }
        sp, um = build_restart_context(snapshot, "sys", "commit x")
        assert "Risk items" in um
        assert "partial match" in um
        assert "abc123" in um

    def test_ledger_for_prompt_risk_markers(self):
        ledger = {"meta": {"updated_at": "2026-01-01T00:00:00Z"}, "commits": {}}
        sha = "a" * 40
        L.init_commit_entry(ledger, sha, "test", ["file.c"])
        L.init_work_items(ledger, sha, [
            {"id": f"{sha[:8]}:file.c:0", "kind": "file",
             "upstream_file": "file.c", "local_file": "file.c"},
        ])
        entry = ledger["commits"][sha]
        entry["warnings"].append("Git verify: partial match")
        L.record_validation(ledger, sha, "fast", {
            "status": "failed", "command": "make", "exit_code": 1,
            "summary": "build error",
        })

        summary = L.ledger_for_prompt(ledger)
        assert "!W" in summary
        assert "!V" in summary

    def test_commit_snapshot_includes_risk_data(self):
        ledger = {"meta": {"updated_at": "2026-01-01T00:00:00Z"}, "commits": {}}
        sha = "a" * 40
        L.init_commit_entry(ledger, sha, "test", ["file.c"])
        L.init_work_items(ledger, sha, [
            {"id": f"{sha[:8]}:file.c:0", "kind": "file",
             "upstream_file": "file.c", "local_file": "file.c"},
        ])
        entry = ledger["commits"][sha]
        entry["warnings"].append("Git verify: partial match")
        L.record_validation(ledger, sha, "fast", {
            "status": "failed", "command": "make", "exit_code": 1,
            "summary": "build error",
        })

        snap = L.commit_snapshot(ledger)
        snap_entry = snap[sha]
        assert "warnings" in snap_entry
        assert snap_entry["warnings"] == ["Git verify: partial match"]
        assert snap_entry.get("validation_failed") is not None
        assert "build error" in snap_entry["validation_failed"]

    def test_no_risk_lines_when_clean(self):
        snapshot = {
            "abc123def456": {
                "status": "ported",
                "upstream_subject": "Clean commit",
                "validation_failed": None,
                "warnings": [],
            }
        }
        sp, um = build_restart_context(snapshot, "sys", "commit x")
        assert "Risk items" not in um


# ── 7. retry_with_hint integration (mock agent) ────────────────────


class TestRetryWithHintIntegration(TestCase):
    """Full retry_with_hint with mock agent and validation."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.upstream, self.local, self.sha, self.old = _create_fixture_repos(self.tmp)
        self.output = self.tmp / "out"
        self.output.mkdir()
        _, self.ledger_path = _build_needs_human_ledger(
            self.output, self.upstream, self.local, self.sha, self.old,
        )

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def test_retry_with_hint_includes_hint_block(self):
        captured_sp = []

        def mock_agent(**kwargs):
            captured_sp.append(kwargs.get("system_prompt", ""))
            return ("done", [])

        def mock_validation(*args, **kwargs):
            return [VerifyResult(passed=True, command="make", exit_code=0)]

        hint = "This is a test hint."
        with (
            patch("vpa.harness.run_agent", mock_agent),
            patch("vpa.harness.run_fast_validation", mock_validation),
            patch("vpa.harness.validation_failed", lambda r: False),
        ):
            retry_with_hint(
                commit_sha=self.sha,
                hint=hint,
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

        assert len(captured_sp) == 1
        assert hint in captured_sp[0]
        assert "Human Review Note" in captured_sp[0]

    def test_retry_with_hint_agent_ports_success(self):
        def mock_agent(**kwargs):
            on_tool = kwargs["on_tool_call"]
            seq = [
                ("record_intent", {"commit_sha": self.sha,
                                   "intent_summary": "Add x via hint"}),
                ("start_work_item", {"commit_sha": self.sha,
                                     "work_item_id": _wi_id(self.sha)}),
                ("append_decision", {
                    "commit_sha": self.sha,
                    "work_item_id": _wi_id(self.sha),
                    "confidence": "high", "reason": "hint-guided patch",
                    "evidence": _EVIDENCE,
                }),
                ("edit_file", {
                    "path": "file.c", "commit_sha": self.sha,
                    "old_string": "int y = 2;",
                    "new_string": "int x = 1;\nint y = 2;",
                    "dry_run": True,
                }),
                ("edit_file", {
                    "path": "file.c", "commit_sha": self.sha,
                    "old_string": "int y = 2;",
                    "new_string": "int x = 1;\nint y = 2;",
                    "dry_run": False,
                }),
                ("complete_work_item", {
                    "commit_sha": self.sha, "work_item_id": _wi_id(self.sha),
                    "status": "ported", "method": "direct_patch",
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

        def mock_validation(*args, **kwargs):
            return [VerifyResult(passed=True, command="make", exit_code=0)]

        with (
            patch("vpa.harness.run_agent", mock_agent),
            patch("vpa.harness.run_fast_validation", mock_validation),
            patch("vpa.harness.validation_failed", lambda r: False),
        ):
            result = retry_with_hint(
                commit_sha=self.sha,
                hint="Try direct patch.",
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

        assert result is not None
        ledger = L.load_ledger(self.ledger_path)
        assert ledger["commits"][self.sha]["status"] == "ported"

    def test_retry_with_hint_repeated_request_human(self):
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
                hint="Try direct patch.",
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

        assert result is None
        ledger = L.load_ledger(self.ledger_path)
        assert ledger["commits"][self.sha]["status"] == "final_manual"
        assert ledger["commits"][self.sha]["work_items"][0]["status"] == "final_manual"
