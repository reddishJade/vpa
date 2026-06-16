"""Tests for tools — command validation, dry_run enforcement, edit limits, signal_done.

These tests verify the tool safety model. No git repositories or external
services are required.
"""

import tempfile
from pathlib import Path
from unittest import TestCase

from vpa import ledger as L
from vpa.tools import (
    TOOL_DEFINITIONS,
    ToolHandler,
    _validate_command,
)


def _make_handler():
    """Create a ToolHandler with a temp directory and empty ledger."""
    tmp = tempfile.mkdtemp()
    ledger_path = Path(tmp) / "ledger.json"
    meta = L.init_session_meta("u", "o", "n", "l", "b", "a", "/u", tmp, "make", [], [])
    ledger, _ = L.init_ledger(meta, tmp)
    handler = ToolHandler(
        local_repo=tmp, upstream_repo=None, ledger=ledger, ledger_path=ledger_path,
    )
    return handler, tmp, ledger, ledger_path


# ── Command validation ──────────────────────────────────────────────────


class TestCommandValidation(TestCase):
    def test_git_reset_blocked(self):
        ok, msg = _validate_command("git reset --hard HEAD")
        self.assertFalse(ok)
        self.assertIn("blocked", msg)

    def test_git_clean_blocked(self):
        ok, msg = _validate_command("git clean -fd")
        self.assertFalse(ok)

    def test_git_checkout_blocked(self):
        ok, msg = _validate_command("git checkout -- file.c")
        self.assertFalse(ok)

    def test_git_restore_blocked(self):
        ok, msg = _validate_command("git restore file.c")
        self.assertFalse(ok)

    def test_git_stash_allowed(self):
        """git stash is in the allowlist; harness should gatekeep separately."""
        ok, msg = _validate_command("git stash")
        self.assertTrue(ok, f"Expected stash allowed, got: {msg}")

    def test_git_rebase_blocked(self):
        ok, msg = _validate_command("git rebase main")
        self.assertFalse(ok)

    def test_rm_blocked(self):
        ok, msg = _validate_command("rm -rf dir")
        self.assertFalse(ok)

    def test_mv_blocked(self):
        ok, msg = _validate_command("mv old new")
        self.assertFalse(ok)

    def test_force_blocked(self):
        ok, msg = _validate_command("somecommand --force")
        self.assertFalse(ok)

    def test_git_diff_allowed(self):
        ok, msg = _validate_command("git diff HEAD")
        self.assertTrue(ok)

    def test_git_log_allowed(self):
        ok, msg = _validate_command("git log --oneline -5")
        self.assertTrue(ok)

    def test_git_status_allowed(self):
        ok, msg = _validate_command("git status")
        self.assertTrue(ok)

    def test_git_show_allowed(self):
        ok, msg = _validate_command("git show HEAD")
        self.assertTrue(ok)

    def test_git_blame_allowed(self):
        ok, msg = _validate_command("git blame file.c")
        self.assertTrue(ok)

    def test_git_branch_allowed(self):
        ok, msg = _validate_command("git branch")
        self.assertTrue(ok)

    def test_non_git_command_allowed(self):
        ok, msg = _validate_command("grep -rn foo .")
        self.assertTrue(ok)

    def test_git_subcommand_not_in_allowlist(self):
        ok, msg = _validate_command("git push origin main")
        self.assertFalse(ok)
        self.assertIn("not in allowlist", msg)


# ── Dry run enforcement ────────────────────────────────────────────────


class TestDryRunEnforcement(TestCase):
    def test_edit_without_dry_run_rejected(self):
        handler, tmp, _, _ = _make_handler()
        test_file = Path(tmp) / "test.c"
        test_file.write_text("int x = 1;")
        result = handler.dispatch("edit_file", {
            "path": "test.c",
            "old_string": "int x = 1;",
            "new_string": "int x = 2;",
            "dry_run": False,
        })
        self.assertIn("error", result)
        self.assertIn("dry_run", result["error"])

    def test_dry_run_then_execute(self):
        handler, tmp, _, _ = _make_handler()
        test_file = Path(tmp) / "test.c"
        test_file.write_text("int x = 1;")
        # dry run first
        dr = handler.dispatch("edit_file", {
            "path": "test.c",
            "old_string": "int x = 1;",
            "new_string": "int x = 2;",
            "dry_run": True,
        })
        self.assertTrue(dr["dry_run"])
        self.assertTrue(dr["matched"])
        # then execute
        ex = handler.dispatch("edit_file", {
            "path": "test.c",
            "old_string": "int x = 1;",
            "new_string": "int x = 2;",
            "dry_run": False,
        })
        self.assertFalse(ex["dry_run"])
        self.assertEqual(ex["replaced"], 1)
        self.assertEqual(test_file.read_text(), "int x = 2;")

    def test_dry_run_mismatch_reports_no_match(self):
        handler, tmp, _, _ = _make_handler()
        test_file = Path(tmp) / "test.c"
        test_file.write_text("int x = 1;")
        result = handler.dispatch("edit_file", {
            "path": "test.c",
            "old_string": "nonexistent",
            "new_string": "x",
            "dry_run": True,
        })
        self.assertFalse(result["matched"])

    def test_edit_limit_enforced(self):
        handler, tmp, _, _ = _make_handler()
        test_file = Path(tmp) / "test.c"
        test_file.write_text("line1\nline2\nline3\nline4\nline5\nline6\n")
        replacements = [
            ("line1", "changed1"),
            ("line2", "changed2"),
            ("line3", "changed3"),
            ("line4", "changed4"),
            ("line5", "changed5"),
        ]
        # First 5 should succeed (5 limit)
        for i, (old, new) in enumerate(replacements):
            handler.dispatch("edit_file", {
                "path": "test.c", "old_string": old, "new_string": new,
                "dry_run": True,
            })
            result = handler.dispatch("edit_file", {
                "path": "test.c", "old_string": old, "new_string": new,
                "dry_run": False,
            })
            self.assertNotIn("error", result, msg=f"edit {i} should succeed")
        # 6th should be rejected
        handler.dispatch("edit_file", {
            "path": "test.c", "old_string": "line6", "new_string": "changed6",
            "dry_run": True,
        })
        result = handler.dispatch("edit_file", {
            "path": "test.c", "old_string": "line6", "new_string": "changed6",
            "dry_run": False,
        })
        self.assertIn("error", result)
        self.assertIn("edit limit", result["error"])

    def test_read_file_outside_repo_rejected(self):
        handler, tmp, _, _ = _make_handler()
        result = handler.dispatch("read_file", {"path": "../outside.txt"})
        self.assertIn("error", result)
        self.assertIn("escapes", result["error"])


# ── signal_done gate ────────────────────────────────────────────────────


class TestSignalDone(TestCase):
    def test_all_terminal_passes(self):
        handler, tmp, ledger, _ = _make_handler()
        sha = "a" * 40
        L.init_commit_entry(ledger, sha, "test", ["src/test.c"])
        L.init_work_items(ledger, sha, [
            {"id": f"{sha[:8]}:src/test.c:0", "kind": "file",
             "upstream_file": "src/test.c", "local_file": "src/test.c"},
        ])
        wi = ledger["commits"][sha]["work_items"][0]
        L.start_work_item(ledger, sha, wi["id"])
        L.complete_work_item(ledger, sha, wi["id"], "ported", method="direct_patch")
        result = handler.dispatch("signal_done", {"commit_sha": sha})
        self.assertTrue(result["done"])

    def test_pending_work_item_fails(self):
        handler, tmp, ledger, _ = _make_handler()
        sha = "a" * 40
        L.init_commit_entry(ledger, sha, "test", ["src/test.c"])
        L.init_work_items(ledger, sha, [
            {"id": f"{sha[:8]}:src/test.c:0", "kind": "file",
             "upstream_file": "src/test.c", "local_file": "src/test.c"},
        ])
        result = handler.dispatch("signal_done", {"commit_sha": sha})
        self.assertFalse(result["done"])
        self.assertIn("not in terminal state", result.get("error", ""))

    def test_unknown_commit_still_succeeds(self):
        handler, _, _, _ = _make_handler()
        result = handler.dispatch("signal_done", {"commit_sha": "unknown"})
        self.assertTrue(result["done"])

    def test_mixed_terminal_and_pending_fails(self):
        handler, tmp, ledger, _ = _make_handler()
        sha = "a" * 40
        L.init_commit_entry(ledger, sha, "test", ["src/a.c", "src/b.c"])
        L.init_work_items(ledger, sha, [
            {"id": f"{sha[:8]}:src/a.c:0", "kind": "file",
             "upstream_file": "src/a.c", "local_file": "src/a.c"},
            {"id": f"{sha[:8]}:src/b.c:1", "kind": "file",
             "upstream_file": "src/b.c", "local_file": "src/b.c"},
        ])
        wi0 = ledger["commits"][sha]["work_items"][0]
        L.start_work_item(ledger, sha, wi0["id"])
        L.complete_work_item(ledger, sha, wi0["id"], "ported", method="direct_patch")
        # wi1 remains pending
        result = handler.dispatch("signal_done", {"commit_sha": sha})
        self.assertFalse(result["done"])
        self.assertIn("not in terminal state", result.get("error", ""))

    def test_signal_done_in_tool_definitions(self):
        names = [t["function"]["name"] for t in TOOL_DEFINITIONS]
        self.assertIn("signal_done", names)
        # Check that commit_sha is a parameter
        import json
        serialized = json.dumps(TOOL_DEFINITIONS)
        self.assertIn("commit_sha", serialized)

    def test_request_human_marks_needs_human(self):
        handler, tmp, ledger, _ = _make_handler()
        sha = "a" * 40
        L.init_commit_entry(ledger, sha, "test", ["src/test.c"])
        L.init_work_items(ledger, sha, [
            {"id": f"{sha[:8]}:src/test.c:0", "kind": "file",
             "upstream_file": "src/test.c", "local_file": "src/test.c"},
        ])
        wi = ledger["commits"][sha]["work_items"][0]
        L.start_work_item(ledger, sha, wi["id"])
        result = handler.dispatch("request_human", {
            "commit_sha": sha,
            "work_item_id": wi["id"],
            "reason": "Conflict at src/test.c:42",
        })
        self.assertTrue(result["manual_required"])
        self.assertEqual(wi["status"], "needs_human")

    def test_edit_file_dry_run_verified_set_isolation(self):
        """Unmatched old_string returns matched=False before dry_run check."""
        handler, tmp, _, _ = _make_handler()
        test_file = Path(tmp) / "test.c"
        test_file.write_text("int x = 1;")
        # dry_run for (path, old) works
        handler.dispatch("edit_file", {
            "path": "test.c", "old_string": "int x = 1;",
            "new_string": "int x = 2;", "dry_run": True,
        })
        # a mismatched old_string for same path returns no-match, not dry_run error
        result = handler.dispatch("edit_file", {
            "path": "test.c", "old_string": "int x = 99;",
            "new_string": "int x = 2;", "dry_run": False,
        })
        self.assertFalse(result["matched"])
        self.assertEqual(result["count"], 0)
