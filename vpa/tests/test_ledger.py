"""Tests for ledger — state machine, append-only decisions, commit status derivation.

These tests cover the core ledger contract. No git repositories or external
services are required.
"""

import json
import subprocess
import tempfile
from pathlib import Path
from unittest import TestCase

from vpa import ledger as L

LEDGER_FIXTURE = {
    "meta": {"updated_at": "2026-01-01T00:00:00Z"},
    "commits": {},
}


def _make_ledger():
    ld = json.loads(json.dumps(LEDGER_FIXTURE))
    return ld, None  # None = no persistent path for these tests


def _commit(ledger, sha="a" * 40, subject="test"):
    L.init_commit_entry(ledger, sha, subject, [f"src/{subject}.c"])
    L.init_work_items(
        ledger,
        sha,
        [
            {"id": f"{sha[:8]}:src/{subject}.c:0", "kind": "file",
             "upstream_file": f"src/{subject}.c", "local_file": f"src/{subject}.c"},
        ],
    )
    return sha


# ── State transitions ───────────────────────────────────────────────────


class TestStateTransitions(TestCase):
    def test_valid_pending_to_in_progress(self):
        ld, _ = _make_ledger()
        sha = _commit(ld)
        wi = ld["commits"][sha]["work_items"][0]
        self.assertEqual(wi["status"], "pending")
        L.start_work_item(ld, sha, wi["id"])
        self.assertEqual(wi["status"], "in_progress")

    def test_invalid_pending_to_ported_rejected(self):
        ld, _ = _make_ledger()
        sha = _commit(ld)
        wi = ld["commits"][sha]["work_items"][0]
        with self.assertRaises(ValueError):
            L.complete_work_item(ld, sha, wi["id"], "ported")

    def test_invalid_pending_to_skipped_rejected(self):
        ld, _ = _make_ledger()
        sha = _commit(ld)
        wi = ld["commits"][sha]["work_items"][0]
        with self.assertRaises(ValueError):
            L.complete_work_item(ld, sha, wi["id"], "skipped")

    def test_in_progress_to_ported(self):
        ld, _ = _make_ledger()
        sha = _commit(ld)
        wi = ld["commits"][sha]["work_items"][0]
        L.start_work_item(ld, sha, wi["id"])
        L.complete_work_item(ld, sha, wi["id"], "ported", method="direct_patch")
        self.assertEqual(wi["status"], "ported")

    def test_in_progress_to_skipped(self):
        ld, _ = _make_ledger()
        sha = _commit(ld)
        wi = ld["commits"][sha]["work_items"][0]
        L.start_work_item(ld, sha, wi["id"])
        L.complete_work_item(ld, sha, wi["id"], "skipped")
        self.assertEqual(wi["status"], "skipped")

    def test_in_progress_to_needs_human(self):
        ld, _ = _make_ledger()
        sha = _commit(ld)
        wi = ld["commits"][sha]["work_items"][0]
        L.start_work_item(ld, sha, wi["id"])
        L.complete_work_item(ld, sha, wi["id"], "needs_human")
        self.assertEqual(wi["status"], "needs_human")

    def test_in_progress_to_blocked(self):
        ld, _ = _make_ledger()
        sha = _commit(ld)
        wi = ld["commits"][sha]["work_items"][0]
        L.start_work_item(ld, sha, wi["id"])
        L.complete_work_item(ld, sha, wi["id"], "blocked")
        self.assertEqual(wi["status"], "blocked")

    def test_in_progress_to_validation_failed(self):
        ld, _ = _make_ledger()
        sha = _commit(ld)
        wi = ld["commits"][sha]["work_items"][0]
        L.start_work_item(ld, sha, wi["id"])
        L.complete_work_item(ld, sha, wi["id"], "validation_failed")
        self.assertEqual(wi["status"], "validation_failed")

    def test_final_manual_is_valid_completion_status(self):
        ld, _ = _make_ledger()
        sha = _commit(ld)
        wi = ld["commits"][sha]["work_items"][0]
        L.start_work_item(ld, sha, wi["id"])
        L.complete_work_item(ld, sha, wi["id"], "final_manual")
        self.assertEqual(wi["status"], "final_manual")

    def test_final_manual_is_terminal(self):
        ld, _ = _make_ledger()
        sha = _commit(ld)
        wi = ld["commits"][sha]["work_items"][0]
        L.start_work_item(ld, sha, wi["id"])
        L.complete_work_item(ld, sha, wi["id"], "final_manual")
        # Cannot transition out of final_manual
        with self.assertRaises(ValueError):
            L.start_work_item(ld, sha, wi["id"])
        with self.assertRaises(ValueError):
            L.complete_work_item(ld, sha, wi["id"], "ported")

    def test_terminal_state_rejects_backward(self):
        ld, _ = _make_ledger()
        sha = _commit(ld)
        wi = ld["commits"][sha]["work_items"][0]
        L.start_work_item(ld, sha, wi["id"])
        L.complete_work_item(ld, sha, wi["id"], "ported", method="direct_patch")
        self.assertEqual(wi["status"], "ported")
        # Cannot go back to in_progress from terminal state
        with self.assertRaises(ValueError):
            L.start_work_item(ld, sha, wi["id"])

    def test_validation_failed_to_needs_human(self):
        ld, _ = _make_ledger()
        sha = _commit(ld)
        wi = ld["commits"][sha]["work_items"][0]
        L.start_work_item(ld, sha, wi["id"])
        L.complete_work_item(ld, sha, wi["id"], "validation_failed")
        L.complete_work_item(ld, sha, wi["id"], "needs_human")
        self.assertEqual(wi["status"], "needs_human")

    def test_needs_human_to_in_progress_via_reset(self):
        ld, _ = _make_ledger()
        sha = _commit(ld)
        wi = ld["commits"][sha]["work_items"][0]
        L.start_work_item(ld, sha, wi["id"])
        L.complete_work_item(ld, sha, wi["id"], "needs_human")
        self.assertEqual(wi["status"], "needs_human")
        # reset_for_retry sets it back to pending
        L.reset_for_retry(ld, sha)
        self.assertEqual(wi["status"], "pending")
        # Now we can start again
        L.start_work_item(ld, sha, wi["id"])
        self.assertEqual(wi["status"], "in_progress")
        self.assertEqual(wi["attempt_count"], 2)

    def test_invalid_completion_status_rejected(self):
        ld, _ = _make_ledger()
        sha = _commit(ld)
        wi = ld["commits"][sha]["work_items"][0]
        L.start_work_item(ld, sha, wi["id"])
        with self.assertRaises(ValueError):
            L.complete_work_item(ld, sha, wi["id"], "in_progress")

    def test_start_from_needs_human_rejected(self):
        """needs_human → in_progress is now gated by the transition table.

        Only reset_for_retry can move items out of needs_human (to pending).
        Direct needs_human → in_progress raises ValueError.
        """
        ld, _ = _make_ledger()
        sha = _commit(ld)
        wi = ld["commits"][sha]["work_items"][0]
        L.start_work_item(ld, sha, wi["id"])
        L.complete_work_item(ld, sha, wi["id"], "needs_human")
        with self.assertRaises(ValueError):
            L.start_work_item(ld, sha, wi["id"])


# ── Append-only decisions ───────────────────────────────────────────────


class TestAppendOnlyDecisions(TestCase):
    def test_append_preserves_prior_decisions(self):
        ld, _ = _make_ledger()
        sha = _commit(ld)
        wi = ld["commits"][sha]["work_items"][0]
        L.start_work_item(ld, sha, wi["id"])
        L.append_decision(ld, sha, wi["id"], "high", "first attempt")
        L.append_decision(ld, sha, wi["id"], "low", "second attempt")
        self.assertEqual(len(wi["decisions"]), 2)
        self.assertEqual(wi["decisions"][0]["reason"], "first attempt")
        self.assertEqual(wi["decisions"][1]["reason"], "second attempt")

    def test_decision_has_timestamp(self):
        ld, _ = _make_ledger()
        sha = _commit(ld)
        wi = ld["commits"][sha]["work_items"][0]
        L.start_work_item(ld, sha, wi["id"])
        L.append_decision(ld, sha, wi["id"], "medium", "test")
        self.assertIn("timestamp", wi["decisions"][0])
        self.assertRegex(wi["decisions"][0]["timestamp"], r"\d{4}-\d{2}")

    def test_decision_has_attempt_count(self):
        ld, _ = _make_ledger()
        sha = _commit(ld)
        wi = ld["commits"][sha]["work_items"][0]
        L.start_work_item(ld, sha, wi["id"])
        L.append_decision(ld, sha, wi["id"], "high", "attempt 1")
        self.assertEqual(wi["decisions"][0]["attempt"], 1)

    def test_decision_accepts_evidence(self):
        ld, _ = _make_ledger()
        sha = _commit(ld)
        wi = ld["commits"][sha]["work_items"][0]
        L.start_work_item(ld, sha, wi["id"])
        evidence = [{"file": "src/foo.c", "line": 42, "snippet": "int x = 1;"}]
        L.append_decision(ld, sha, wi["id"], "high", "with evidence", evidence)
        self.assertEqual(wi["decisions"][0]["evidence"], evidence)

    def test_decision_defaults_empty_evidence(self):
        ld, _ = _make_ledger()
        sha = _commit(ld)
        wi = ld["commits"][sha]["work_items"][0]
        L.start_work_item(ld, sha, wi["id"])
        L.append_decision(ld, sha, wi["id"], "high", "no evidence")
        self.assertEqual(wi["decisions"][0]["evidence"], [])

    def test_decision_overwrite_not_possible(self):
        ld, _ = _make_ledger()
        sha = _commit(ld)
        wi = ld["commits"][sha]["work_items"][0]
        L.start_work_item(ld, sha, wi["id"])
        L.append_decision(ld, sha, wi["id"], "high", "first")
        L.append_decision(ld, sha, wi["id"], "low", "second")
        # Both exist — first is not overwritten
        self.assertEqual(len(wi["decisions"]), 2)
        self.assertEqual(wi["decisions"][0]["reason"], "first")


# ── Commit status derivation ────────────────────────────────────────────


class TestCommitStatusDerivation(TestCase):
    def test_no_work_items_pending(self):
        ld, _ = _make_ledger()
        sha = _commit(ld)
        self.assertEqual(ld["commits"][sha]["status"], "pending")

    def test_all_ported(self):
        ld, _ = _make_ledger()
        sha = _commit(ld)
        wi = ld["commits"][sha]["work_items"][0]
        L.start_work_item(ld, sha, wi["id"])
        L.complete_work_item(ld, sha, wi["id"], "ported", method="direct_patch")
        self.assertEqual(ld["commits"][sha]["status"], "ported")

    def test_all_skipped(self):
        ld, _ = _make_ledger()
        sha = _commit(ld)
        wi = ld["commits"][sha]["work_items"][0]
        L.start_work_item(ld, sha, wi["id"])
        L.complete_work_item(ld, sha, wi["id"], "skipped")
        self.assertEqual(ld["commits"][sha]["status"], "skipped")

    def test_mixed_ported_skipped_derives_partially_ported(self):
        ld, _ = _make_ledger()
        sha = "a" * 40
        L.init_commit_entry(ld, sha, "test", ["src/foo.c", "src/bar.c"])
        L.init_work_items(ld, sha, [
            {"id": f"{sha[:8]}:src/foo.c:0", "kind": "file",
             "upstream_file": "src/foo.c", "local_file": "src/foo.c"},
            {"id": f"{sha[:8]}:src/bar.c:1", "kind": "file",
             "upstream_file": "src/bar.c", "local_file": "src/bar.c"},
        ])
        items = ld["commits"][sha]["work_items"]
        L.start_work_item(ld, sha, items[0]["id"])
        L.complete_work_item(ld, sha, items[0]["id"], "ported", method="direct_patch")
        L.start_work_item(ld, sha, items[1]["id"])
        L.complete_work_item(ld, sha, items[1]["id"], "skipped")
        # DESIGN.md: partially_ported = some hunks done, others pending/skipped
        self.assertEqual(ld["commits"][sha]["status"], "partially_ported")

    def test_any_needs_human(self):
        ld, _ = _make_ledger()
        sha = _commit(ld)
        wi = ld["commits"][sha]["work_items"][0]
        L.start_work_item(ld, sha, wi["id"])
        L.complete_work_item(ld, sha, wi["id"], "needs_human")
        self.assertEqual(ld["commits"][sha]["status"], "needs_human")

    def test_needs_human_overrides_ported(self):
        ld, _ = _make_ledger()
        sha = "a" * 40
        L.init_commit_entry(ld, sha, "test", ["src/foo.c", "src/bar.c"])
        L.init_work_items(ld, sha, [
            {"id": f"{sha[:8]}:src/foo.c:0", "kind": "file",
             "upstream_file": "src/foo.c", "local_file": "src/foo.c"},
            {"id": f"{sha[:8]}:src/bar.c:1", "kind": "file",
             "upstream_file": "src/bar.c", "local_file": "src/bar.c"},
        ])
        items = ld["commits"][sha]["work_items"]
        L.start_work_item(ld, sha, items[0]["id"])
        L.complete_work_item(ld, sha, items[0]["id"], "ported", method="direct_patch")
        L.start_work_item(ld, sha, items[1]["id"])
        L.complete_work_item(ld, sha, items[1]["id"], "needs_human")
        self.assertEqual(ld["commits"][sha]["status"], "needs_human")

    def test_any_validation_failed(self):
        ld, _ = _make_ledger()
        sha = _commit(ld)
        wi = ld["commits"][sha]["work_items"][0]
        L.start_work_item(ld, sha, wi["id"])
        L.complete_work_item(ld, sha, wi["id"], "validation_failed")
        self.assertEqual(ld["commits"][sha]["status"], "validation_failed")

    def test_any_blocked(self):
        ld, _ = _make_ledger()
        sha = _commit(ld)
        wi = ld["commits"][sha]["work_items"][0]
        L.start_work_item(ld, sha, wi["id"])
        L.complete_work_item(ld, sha, wi["id"], "blocked")
        self.assertEqual(ld["commits"][sha]["status"], "blocked")

    def test_all_final_manual(self):
        ld, _ = _make_ledger()
        sha = _commit(ld)
        wi = ld["commits"][sha]["work_items"][0]
        L.start_work_item(ld, sha, wi["id"])
        L.complete_work_item(ld, sha, wi["id"], "final_manual")
        self.assertEqual(ld["commits"][sha]["status"], "final_manual")

    def test_final_manual_overrides_validation_failed(self):
        ld, _ = _make_ledger()
        sha = "a" * 40
        L.init_commit_entry(ld, sha, "test", ["src/foo.c", "src/bar.c"])
        L.init_work_items(ld, sha, [
            {"id": f"{sha[:8]}:src/foo.c:0", "kind": "file",
             "upstream_file": "src/foo.c", "local_file": "src/foo.c"},
            {"id": f"{sha[:8]}:src/bar.c:1", "kind": "file",
             "upstream_file": "src/bar.c", "local_file": "src/bar.c"},
        ])
        items = ld["commits"][sha]["work_items"]
        L.start_work_item(ld, sha, items[0]["id"])
        L.complete_work_item(ld, sha, items[0]["id"], "final_manual")
        L.start_work_item(ld, sha, items[1]["id"])
        L.complete_work_item(ld, sha, items[1]["id"], "validation_failed")
        # final_manual should take priority over validation_failed
        self.assertEqual(ld["commits"][sha]["status"], "final_manual")

    def test_final_manual_overrides_needs_human(self):
        ld, _ = _make_ledger()
        sha = "a" * 40
        L.init_commit_entry(ld, sha, "test", ["src/foo.c", "src/bar.c"])
        L.init_work_items(ld, sha, [
            {"id": f"{sha[:8]}:src/foo.c:0", "kind": "file",
             "upstream_file": "src/foo.c", "local_file": "src/foo.c"},
            {"id": f"{sha[:8]}:src/bar.c:1", "kind": "file",
             "upstream_file": "src/bar.c", "local_file": "src/bar.c"},
        ])
        items = ld["commits"][sha]["work_items"]
        L.start_work_item(ld, sha, items[0]["id"])
        L.complete_work_item(ld, sha, items[0]["id"], "final_manual")
        L.start_work_item(ld, sha, items[1]["id"])
        L.complete_work_item(ld, sha, items[1]["id"], "needs_human")
        # final_manual should take priority over needs_human
        self.assertEqual(ld["commits"][sha]["status"], "final_manual")

    def test_in_progress_higher_priority_than_partial(self):
        ld, _ = _make_ledger()
        sha = "a" * 40
        L.init_commit_entry(ld, sha, "test", ["src/foo.c", "src/bar.c"])
        L.init_work_items(ld, sha, [
            {"id": f"{sha[:8]}:src/foo.c:0", "kind": "file",
             "upstream_file": "src/foo.c", "local_file": "src/foo.c"},
            {"id": f"{sha[:8]}:src/bar.c:1", "kind": "file",
             "upstream_file": "src/bar.c", "local_file": "src/bar.c"},
        ])
        items = ld["commits"][sha]["work_items"]
        L.start_work_item(ld, sha, items[0]["id"])
        L.complete_work_item(ld, sha, items[0]["id"], "ported", method="direct_patch")
        # Second item still pending → one ported, one pending
        # Falls through to the else clause → partially_ported
        self.assertEqual(ld["commits"][sha]["status"], "partially_ported")

    def test_porting_ported_local_files_modified(self):
        ld, _ = _make_ledger()
        sha = _commit(ld)
        wi = ld["commits"][sha]["work_items"][0]
        L.start_work_item(ld, sha, wi["id"])
        L.complete_work_item(ld, sha, wi["id"], "ported", method="direct_patch")
        self.assertIn("src/test.c", ld["commits"][sha]["local_files_modified"])

    def test_skipped_does_not_modify_local_files(self):
        ld, _ = _make_ledger()
        sha = _commit(ld)
        wi = ld["commits"][sha]["work_items"][0]
        L.start_work_item(ld, sha, wi["id"])
        L.complete_work_item(ld, sha, wi["id"], "skipped")
        self.assertEqual(ld["commits"][sha]["local_files_modified"], [])


# ── Edge cases ──────────────────────────────────────────────────────────


class TestLedgerEdgeCases(TestCase):
    def test_init_commit_then_record_intent(self):
        ld, _ = _make_ledger()
        sha = _commit(ld)
        self.assertIsNone(ld["commits"][sha]["intent_summary"])
        L.record_intent_summary(ld, sha, "Fix the foo lifecycle")
        self.assertEqual(
            ld["commits"][sha]["intent_summary"], "Fix the foo lifecycle"
        )

    def test_unknown_commit_raises_key_error(self):
        ld, _ = _make_ledger()
        with self.assertRaises(KeyError):
            L.start_work_item(ld, "badsha", "work_item_id")

    def test_unknown_work_item_raises_key_error(self):
        ld, _ = _make_ledger()
        sha = _commit(ld)
        with self.assertRaises(KeyError):
            L.start_work_item(ld, sha, "nonexistent_id")

    def test_canonical_sets_include_all_terminal_statuses(self):
        """TERMINAL_WORK_ITEM_STATUSES and HARNESS_SKIP_STATUSES
        must include all statuses that represent done/non-reprocessable states.
        """
        self.assertIn("final_manual", L.TERMINAL_WORK_ITEM_STATUSES)
        self.assertIn("validation_failed", L.HARNESS_SKIP_STATUSES)
        self.assertIn("final_manual", L.HARNESS_SKIP_STATUSES)
        # HARNESS_SKIP_STATUSES must be a superset of TERMINAL in practice
        for s in L.TERMINAL_WORK_ITEM_STATUSES:
            self.assertIn(s, L.HARNESS_SKIP_STATUSES,
                          f"{s} in TERMINAL_WORK_ITEM_STATUSES but not HARNESS_SKIP_STATUSES")

    def test_validation_failed_in_skip_set(self):
        """validation_failed must be in HARNESS_SKIP_STATUSES so the
        main loop does not re-process failed commits."""
        self.assertIn("validation_failed", L.HARNESS_SKIP_STATUSES)

    def test_session_meta_has_required_fields(self):
        meta = L.init_session_meta(
            "upstream", "v1", "v2", "local", "main", "arm64",
            "/upstream", "/local", "make", ["test"], ["slow_test"],
        )
        self.assertEqual(meta["upstream_name"], "upstream")
        self.assertEqual(meta["local_branch"], "main")
        self.assertEqual(meta["fast_test_cmds"], ["test"])
        self.assertIn("started_at", meta)
        self.assertIn("updated_at", meta)

    def test_persist_and_reload_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "ledger.json"
            meta = L.init_session_meta(
                "up", "o", "n", "lo", "br", "arch", "/u", "/l", "make", [], [],
            )
            ld, _ = L.init_ledger(meta, tmp)
            sha = _commit(ld)
            L.write_ledger(ledger_path, ld)
            reloaded = L.load_ledger(ledger_path)
            self.assertIn(sha, reloaded["commits"])
            self.assertEqual(reloaded["meta"]["upstream_name"], "up")

    def test_iter_manual_required(self):
        ld, _ = _make_ledger()
        sha1 = _commit(ld, "a" * 40, "one")
        L.start_work_item(ld, sha1, ld["commits"][sha1]["work_items"][0]["id"])
        L.complete_work_item(
            ld, sha1, ld["commits"][sha1]["work_items"][0]["id"], "needs_human"
        )
        sha2 = _commit(ld, "b" * 40, "two")
        L.start_work_item(ld, sha2, ld["commits"][sha2]["work_items"][0]["id"])
        wi2 = ld["commits"][sha2]["work_items"][0]
        L.complete_work_item(ld, sha2, wi2["id"], "ported", method="direct_patch")
        results = list(L.iter_manual_required(ld))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0], sha1)


# ── Git verification ────────────────────────────────────────────────────


class TestGitVerify(TestCase):
    """git_verify checks that claimed modified files appear in git diff HEAD."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        subprocess.run(["git", "init"], cwd=self.tmp, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=self.tmp, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=self.tmp, capture_output=True,
        )
        (self.tmp / "file.c").write_text("int y = 2;\n")
        subprocess.run(["git", "add", "."], cwd=self.tmp, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=self.tmp, capture_output=True,
        )

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def test_no_files_claimed_returns_ok(self):
        entry = {"local_files_modified": []}
        ok, detail = L.git_verify(str(self.tmp), entry)
        self.assertTrue(ok)
        self.assertIn("no files claimed", detail)

    def test_all_files_match_returns_ok(self):
        (self.tmp / "file.c").write_text("int x = 1;\n")
        ok, detail = L.git_verify(str(self.tmp), {"local_files_modified": ["file.c"]})
        self.assertTrue(ok)
        self.assertIn("all 1 files confirmed", detail)

    def test_no_overlap_returns_false(self):
        (self.tmp / "file.c").write_text("int x = 1;\n")
        entry = {"local_files_modified": ["other.c"]}
        ok, detail = L.git_verify(str(self.tmp), entry)
        self.assertFalse(ok)
        self.assertIn("none of", detail)

    def test_partial_match_returns_true_with_warning(self):
        (self.tmp / "file.c").write_text("int x = 1;\n")
        entry = {"local_files_modified": ["file.c", "missing.c"]}
        ok, detail = L.git_verify(str(self.tmp), entry)
        self.assertTrue(ok)
        self.assertIn("partial match", detail)
        self.assertIn("file.c", detail)
        self.assertIn("missing.c", detail)

    def test_git_diff_failure_returns_false(self):
        entry = {"local_files_modified": ["file.c"]}
        ok, detail = L.git_verify("/nonexistent/path", entry)
        self.assertFalse(ok)
        self.assertIn("git diff failed", detail)
