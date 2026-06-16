"""Tests for tools — command validation, dry_run enforcement, edit limits,
intent gate, evidence gate, completion gate, signal_done.

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

_TARGET_SHA = "a" * 40
_EVIDENCE = [{"file": "src/test.c", "line": 42, "snippet": "int x = 1;"}]


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


def _setup_commit(ledger, sha=_TARGET_SHA, with_intent=True):
    """Add a single-work-item commit. Optionally record intent."""
    L.init_commit_entry(ledger, sha, "test", ["src/test.c"])
    L.init_work_items(ledger, sha, [
        {"id": f"{sha[:8]}:src/test.c:0", "kind": "file",
         "upstream_file": "src/test.c", "local_file": "src/test.c"},
    ])
    if with_intent:
        L.record_intent_summary(ledger, sha, "Fix the foo lifecycle")
    wi = ledger["commits"][sha]["work_items"][0]
    return wi


def _start_work_item(handler, ledger, sha, wi):
    """Convenience: start a work item with a decision in the ledger."""
    L.start_work_item(ledger, sha, wi["id"])
    L.append_decision(ledger, sha, wi["id"], "high", "direct patch", _EVIDENCE)


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
        ok, msg = _validate_command("git stash")
        self.assertTrue(ok)

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
    def _edit(self, handler, **kw):
        defaults = {"path": "test.c", "commit_sha": _TARGET_SHA}
        return handler.dispatch("edit_file", {**defaults, **kw})

    def test_edit_without_dry_run_rejected(self):
        handler, tmp, ledger, _ = _make_handler()
        _setup_commit(ledger)
        (Path(tmp) / "test.c").write_text("int x = 1;")
        result = self._edit(handler, old_string="int x = 1;",
                            new_string="int x = 2;", dry_run=False)
        self.assertIn("error", result)
        self.assertIn("dry_run", result["error"])

    def test_dry_run_then_execute(self):
        handler, tmp, ledger, _ = _make_handler()
        _setup_commit(ledger)
        test_file = Path(tmp) / "test.c"
        test_file.write_text("int x = 1;")
        dr = self._edit(handler, old_string="int x = 1;",
                        new_string="int x = 2;", dry_run=True)
        self.assertTrue(dr["dry_run"])
        self.assertTrue(dr["matched"])
        ex = self._edit(handler, old_string="int x = 1;",
                        new_string="int x = 2;", dry_run=False)
        self.assertFalse(ex["dry_run"])
        self.assertEqual(ex["replaced"], 1)
        self.assertEqual(test_file.read_text(), "int x = 2;")

    def test_dry_run_mismatch_reports_no_match(self):
        handler, tmp, ledger, _ = _make_handler()
        _setup_commit(ledger)
        (Path(tmp) / "test.c").write_text("int x = 1;")
        result = self._edit(handler, old_string="nonexistent",
                            new_string="x", dry_run=True)
        self.assertFalse(result["matched"])

    def test_edit_limit_enforced(self):
        handler, tmp, ledger, _ = _make_handler()
        _setup_commit(ledger)
        test_file = Path(tmp) / "test.c"
        test_file.write_text("line1\nline2\nline3\nline4\nline5\nline6\n")
        for i in range(1, 6):
            old = f"line{i}"
            self._edit(handler, old_string=old, new_string=f"changed{i}", dry_run=True)
            result = self._edit(handler, old_string=old,
                                new_string=f"changed{i}", dry_run=False)
            self.assertNotIn("error", result, msg=f"edit {i} should succeed")
        # 6th should be rejected
        self._edit(handler, old_string="line6", new_string="changed6", dry_run=True)
        result = self._edit(handler, old_string="line6",
                            new_string="changed6", dry_run=False)
        self.assertIn("error", result)
        self.assertIn("edit limit", result["error"])

    def test_read_file_outside_repo_rejected(self):
        handler, tmp, _, _ = _make_handler()
        result = handler.dispatch("read_file", {"path": "../outside.txt"})
        self.assertIn("error", result)
        self.assertIn("escapes", result["error"])


# ── Intent gate ─────────────────────────────────────────────────────────


class TestIntentGate(TestCase):
    def test_edit_without_intent_rejected(self):
        handler, tmp, ledger, _ = _make_handler()
        _setup_commit(ledger, with_intent=False)
        (Path(tmp) / "test.c").write_text("int x = 1;")
        handler.dispatch("edit_file", {
            "path": "test.c", "old_string": "int x = 1;",
            "new_string": "int x = 2;", "dry_run": True,
        })
        result = handler.dispatch("edit_file", {
            "path": "test.c", "old_string": "int x = 1;",
            "new_string": "int x = 2;", "dry_run": False,
            "commit_sha": _TARGET_SHA,
        })
        self.assertIn("error", result)
        self.assertIn("intent_summary", result["error"])

    def test_edit_with_intent_allowed(self):
        handler, tmp, ledger, _ = _make_handler()
        _setup_commit(ledger, with_intent=True)
        test_file = Path(tmp) / "test.c"
        test_file.write_text("int x = 1;")
        handler.dispatch("edit_file", {
            "path": "test.c", "old_string": "int x = 1;",
            "new_string": "int x = 2;", "dry_run": True,
            "commit_sha": _TARGET_SHA,
        })
        result = handler.dispatch("edit_file", {
            "path": "test.c", "old_string": "int x = 1;",
            "new_string": "int x = 2;", "dry_run": False,
            "commit_sha": _TARGET_SHA,
        })
        self.assertNotIn("error", result)
        self.assertEqual(result["replaced"], 1)

    def test_write_without_intent_rejected(self):
        handler, tmp, ledger, _ = _make_handler()
        _setup_commit(ledger, with_intent=False)
        result = handler.dispatch("write_file", {
            "path": "new.c", "content": "int y = 2;",
            "commit_sha": _TARGET_SHA,
        })
        self.assertIn("error", result)
        self.assertIn("intent_summary", result["error"])

    def test_write_with_intent_allowed(self):
        handler, tmp, ledger, _ = _make_handler()
        _setup_commit(ledger, with_intent=True)
        result = handler.dispatch("write_file", {
            "path": "new.c", "content": "int y = 2;",
            "commit_sha": _TARGET_SHA,
        })
        self.assertNotIn("error", result)
        self.assertTrue(result["created"])

    def test_dry_run_allowed_without_intent(self):
        """Dry-run is read-only, so intent gate does not apply."""
        handler, tmp, ledger, _ = _make_handler()
        _setup_commit(ledger, with_intent=False)
        (Path(tmp) / "test.c").write_text("int x = 1;")
        result = handler.dispatch("edit_file", {
            "path": "test.c", "old_string": "int x = 1;",
            "new_string": "int x = 2;", "dry_run": True,
            "commit_sha": _TARGET_SHA,
        })
        self.assertTrue(result["dry_run"])
        self.assertTrue(result["matched"])

    def test_edit_missing_commit_sha_rejected(self):
        handler, tmp, ledger, _ = _make_handler()
        _setup_commit(ledger)
        (Path(tmp) / "test.c").write_text("int x = 1;")
        handler.dispatch("edit_file", {
            "path": "test.c", "old_string": "int x = 1;",
            "new_string": "int x = 2;", "dry_run": True,
            "commit_sha": _TARGET_SHA,
        })
        result = handler.dispatch("edit_file", {
            "path": "test.c", "old_string": "int x = 1;",
            "new_string": "int x = 2;", "dry_run": False,
        })
        self.assertIn("error", result)
        self.assertIn("commit_sha", result["error"])


# ── Evidence gate ───────────────────────────────────────────────────────


class TestEvidenceGate(TestCase):
    def _append(self, handler, evidence=None, **overrides):
        args = {
            "commit_sha": _TARGET_SHA,
            "work_item_id": f"{_TARGET_SHA[:8]}:src/test.c:0",
            "confidence": "high",
            "reason": "direct patch",
        }
        if evidence is not None:
            args["evidence"] = evidence
        args.update(overrides)
        return handler.dispatch("append_decision", args)

    def test_empty_evidence_rejected(self):
        handler, _, ledger, _ = _make_handler()
        _setup_commit(ledger)
        L.start_work_item(ledger, _TARGET_SHA,
                          f"{_TARGET_SHA[:8]}:src/test.c:0")
        result = self._append(handler, evidence=[])
        self.assertIn("error", result)

    def test_missing_evidence_rejected(self):
        handler, _, ledger, _ = _make_handler()
        _setup_commit(ledger)
        L.start_work_item(ledger, _TARGET_SHA,
                          f"{_TARGET_SHA[:8]}:src/test.c:0")
        result = self._append(handler)
        self.assertIn("error", result)

    def test_evidence_empty_file_rejected(self):
        handler, _, ledger, _ = _make_handler()
        _setup_commit(ledger)
        L.start_work_item(ledger, _TARGET_SHA,
                          f"{_TARGET_SHA[:8]}:src/test.c:0")
        result = self._append(handler, evidence=[{"file": "", "line": 1, "snippet": "x"}])
        self.assertIn("error", result)

    def test_evidence_zero_line_rejected(self):
        handler, _, ledger, _ = _make_handler()
        _setup_commit(ledger)
        L.start_work_item(ledger, _TARGET_SHA,
                          f"{_TARGET_SHA[:8]}:src/test.c:0")
        result = self._append(handler, evidence=[{"file": "f.c", "line": 0, "snippet": "x"}])
        self.assertIn("error", result)

    def test_evidence_empty_snippet_rejected(self):
        handler, _, ledger, _ = _make_handler()
        _setup_commit(ledger)
        L.start_work_item(ledger, _TARGET_SHA,
                          f"{_TARGET_SHA[:8]}:src/test.c:0")
        result = self._append(handler, evidence=[{"file": "f.c", "line": 1, "snippet": ""}])
        self.assertIn("error", result)

    def test_valid_evidence_accepted(self):
        handler, _, ledger, _ = _make_handler()
        _setup_commit(ledger)
        L.start_work_item(ledger, _TARGET_SHA,
                          f"{_TARGET_SHA[:8]}:src/test.c:0")
        result = self._append(handler, evidence=_EVIDENCE)
        self.assertTrue(result.get("appended"))

    def test_multiple_evidence_accepted(self):
        handler, _, ledger, _ = _make_handler()
        _setup_commit(ledger)
        L.start_work_item(ledger, _TARGET_SHA,
                          f"{_TARGET_SHA[:8]}:src/test.c:0")
        ev = [{"file": "a.c", "line": 1, "snippet": "x"},
              {"file": "b.c", "line": 2, "snippet": "y"}]
        result = self._append(handler, evidence=ev)
        self.assertTrue(result.get("appended"))


# ── Completion gate ─────────────────────────────────────────────────────


class TestCompletionGate(TestCase):
    def _complete(self, handler, **overrides):
        args = {
            "commit_sha": _TARGET_SHA,
            "work_item_id": f"{_TARGET_SHA[:8]}:src/test.c:0",
            "status": "ported",
            "method": "direct_patch",
        }
        args.update(overrides)
        return handler.dispatch("complete_work_item", args)

    def test_complete_without_decision_rejected(self):
        handler, _, ledger, _ = _make_handler()
        _setup_commit(ledger)
        wi = ledger["commits"][_TARGET_SHA]["work_items"][0]
        L.start_work_item(ledger, _TARGET_SHA, wi["id"])
        result = self._complete(handler)
        self.assertIn("error", result)
        self.assertIn("no decision", result["error"])

    def test_complete_with_decision_allowed(self):
        handler, _, ledger, _ = _make_handler()
        _setup_commit(ledger)
        wi = ledger["commits"][_TARGET_SHA]["work_items"][0]
        _start_work_item(handler, ledger, _TARGET_SHA, wi)
        result = self._complete(handler)
        self.assertTrue(result.get("completed"))
        self.assertEqual(wi["status"], "ported")


# ── signal_done gate ────────────────────────────────────────────────────


class TestSignalDone(TestCase):
    def test_all_terminal_passes(self):
        handler, tmp, ledger, _ = _make_handler()
        sha = _TARGET_SHA
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
        sha = _TARGET_SHA
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
        sha = _TARGET_SHA
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
        import json
        serialized = json.dumps(TOOL_DEFINITIONS)
        self.assertIn("commit_sha", serialized)

    def test_request_human_marks_needs_human(self):
        handler, tmp, ledger, _ = _make_handler()
        sha = _TARGET_SHA
        _setup_commit(ledger, sha)
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
        handler, tmp, ledger, _ = _make_handler()
        _setup_commit(ledger, with_intent=True)
        test_file = Path(tmp) / "test.c"
        test_file.write_text("int x = 1;")
        handler.dispatch("edit_file", {
            "path": "test.c", "old_string": "int x = 1;",
            "new_string": "int x = 2;", "dry_run": True,
            "commit_sha": _TARGET_SHA,
        })
        result = handler.dispatch("edit_file", {
            "path": "test.c", "old_string": "int x = 99;",
            "new_string": "int x = 2;", "dry_run": False,
            "commit_sha": _TARGET_SHA,
        })
        self.assertFalse(result["matched"])
        self.assertEqual(result["count"], 0)
