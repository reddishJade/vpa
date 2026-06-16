"""Focused report unit tests for Phase 10.

Tests generate_summary and generate_json_output directly with fixture ledgers.
No git repos needed. No real API calls. No harness dependency.
"""

import json
from unittest import TestCase

from vpa.report import generate_json_output, generate_summary
from vpa.verify import VerifyResult


def _make_ledger(**commit_kwargs):
    """Build a minimal ledger with given commit entries."""
    ledger = {
        "meta": {
            "upstream_old": "a" * 40,
            "upstream_new": "b" * 40,
            "local_branch": "main",
            "started_at": "2024-01-01T00:00:00",
            "updated_at": "2024-01-01T00:00:00",
        },
        "commits": {},
    }
    for sha, kw in commit_kwargs.items():
        entry = {
            "commit_sha": sha,
            "upstream_subject": kw.get("subject", "?"),
            "intent_summary": kw.get("intent"),
            "status": kw.get("status", "pending"),
            "upstream_files": kw.get("upstream_files", []),
            "local_files_modified": kw.get("local_files_modified", []),
            "warnings": kw.get("warnings", []),
            "work_items": kw.get("work_items", []),
            "validation": kw.get("validation", {}),
        }
        ledger["commits"][sha] = entry
    return ledger


def _wi(wi_id, status="ported", kind="file", decisions=None):
    return {
        "id": wi_id,
        "kind": kind,
        "upstream_file": "file.c",
        "local_file": "file.c",
        "status": status,
        "method": "direct_patch",
        "attempt_count": 1,
        "decisions": decisions or [],
    }


def _decision(reason="ported ok", evidence=None):
    return {
        "timestamp": "2024-01-01T00:00:00",
        "attempt": 1,
        "confidence": "high",
        "reason": reason,
        "evidence": evidence or [],
    }


# ── Helpers ──────────────────────────────────────────────────────────


def _count_overview(summary, label):
    """Extract the count for a label from the Overview section."""
    for line in summary.splitlines():
        if not line.startswith("- "):
            continue
        # Parse label: count pairs in the line
        # e.g. "- Ported: 1  |  Skipped: 2"
        parts = line.split("  |  ")
        for part in parts:
            part = part.lstrip("- ")
            if part.startswith(f"{label}:"):
                count_str = part.split(":")[1].strip()
                try:
                    return int(count_str)
                except ValueError:
                    return None
    return None


def _lines_after(summary, marker):
    """Yield lines from the section after marker until next ## or end."""
    lines = summary.splitlines()
    found = False
    for line in lines:
        if found:
            if line.startswith("## "):
                return
            yield line
        elif marker in line:
            found = True


# ═══════════════════════════════════════════════════════════════════════
# Test 1 — Overview counts
# ═══════════════════════════════════════════════════════════════════════


class TestOverviewCounts(TestCase):
    """All seven statuses appear in the overview with correct counts."""

    def test_all_statuses_counted_in_overview(self):
        sha_p = "p000" + "0" * 36
        sha_s = "s000" + "0" * 36
        sha_n = "n000" + "0" * 36
        sha_b = "b000" + "0" * 36
        sha_v = "v000" + "0" * 36
        sha_f = "f000" + "0" * 36
        sha_pp = "h000" + "0" * 36

        ledger = _make_ledger(**{
            sha_p: {"status": "ported", "subject": "ported commit"},
            sha_s: {"status": "skipped", "subject": "skipped commit"},
            sha_n: {"status": "needs_human", "subject": "needs human commit"},
            sha_b: {"status": "blocked", "subject": "blocked commit",
                    "work_items": [
                        _wi("b:wi0", "blocked", decisions=[_decision("blocked reason")]),
                    ]},
            sha_v: {"status": "validation_failed", "subject": "validation failed commit",
                    "work_items": [
                        _wi("v:wi0", "validation_failed", decisions=[_decision("failed reason")]),
                    ]},
            sha_f: {"status": "final_manual", "subject": "final manual commit"},
            sha_pp: {"status": "partially_ported", "subject": "partial commit"},
        })

        summary = generate_summary(ledger, [], [])

        assert _count_overview(summary, "Total upstream commits") == 7
        assert _count_overview(summary, "Ported") == 1
        assert _count_overview(summary, "Skipped") == 1
        assert _count_overview(summary, "Needs Human") == 1
        assert _count_overview(summary, "Blocked") == 1
        assert _count_overview(summary, "Validation Failed") == 1
        assert _count_overview(summary, "Final Manual") == 1
        assert _count_overview(summary, "Partially Ported") == 1

    def test_empty_ledger_overview(self):
        ledger = _make_ledger()
        summary = generate_summary(ledger, [], [])
        assert _count_overview(summary, "Total upstream commits") == 0
        assert _count_overview(summary, "Ported") == 0

    def test_final_manual_not_counted_as_ported(self):
        """final_manual increments Final Manual, not Ported."""
        sha = "f" * 40
        ledger = _make_ledger(**{
            sha: {"status": "final_manual", "subject": "manual final",
                  "work_items": [_wi("f:wi0", "final_manual")]},
        })
        summary = generate_summary(ledger, [], [])
        assert _count_overview(summary, "Ported") == 0
        assert _count_overview(summary, "Final Manual") == 1

    def test_validation_failed_not_counted_as_ported(self):
        """validation_failed increments Validation Failed, not Ported."""
        sha = "v" * 40
        ledger = _make_ledger(**{
            sha: {"status": "validation_failed", "subject": "vf",
                  "work_items": [_wi("v:wi0", "validation_failed")]},
        })
        summary = generate_summary(ledger, [], [])
        assert _count_overview(summary, "Ported") == 0
        assert _count_overview(summary, "Validation Failed") == 1


# ═══════════════════════════════════════════════════════════════════════
# Test 2 — Manual/action section
# ═══════════════════════════════════════════════════════════════════════


class TestManualActionSection(TestCase):
    """Manual section must surface needs_human, final_manual, validation_failed,
    blocked, and partially_ported with actionable detail."""

    def test_needs_human_appears_with_sha_and_reason(self):
        sha = "n" * 40
        ledger = _make_ledger(**{
            sha: {"status": "needs_human", "subject": "complex conflict",
                  "intent": "Handle the merge conflict",
                  "work_items": [
                      _wi("n:wi0", "needs_human", decisions=[
                          _decision("Complex conflict at file.c:1"),
                      ]),
                  ]},
        })
        summary = generate_summary(ledger, [], [])
        assert sha[:8] in summary
        assert "needs_human" in summary.lower()
        assert "Complex conflict at file.c:1" in summary

    def test_final_manual_appears_as_manual_not_ported(self):
        sha = "f" * 40
        ledger = _make_ledger(**{
            sha: {"status": "final_manual", "subject": "still unclear",
                  "work_items": [_wi("f:wi0", "final_manual")]},
        })
        summary = generate_summary(ledger, [], [])
        assert sha[:8] in summary
        assert "final_manual" in summary.lower()
        assert "Ported" not in [
            line for line in summary.splitlines()
            if sha[:8] in line and "Ported" in line
        ]

    def test_validation_failed_appears_with_details(self):
        sha = "v" * 40
        ledger = _make_ledger(**{
            sha: {"status": "validation_failed", "subject": "build broken",
                  "intent": "Port feature X",
                  "work_items": [
                      _wi("v:wi0", "validation_failed", decisions=[
                          _decision("validation failed after repair"),
                      ]),
                  ],
                  "validation": {
                      "fast": {
                          "status": "failed",
                          "command": "make",
                          "exit_code": 1,
                          "summary": "build error in foo.c",
                      },
                  }},
        })
        summary = generate_summary(ledger, [], [])
        assert sha[:8] in summary
        assert "validation_failed" in summary.lower()
        assert "make" in summary
        assert "build error in foo.c" in summary

    def test_blocked_appears_with_reason(self):
        sha = "b" * 40
        ledger = _make_ledger(**{
            sha: {"status": "blocked", "subject": "api mismatch",
                  "work_items": [
                      _wi("b:wi0", "blocked", decisions=[
                          _decision("Unsupported API call"),
                      ]),
                  ]},
        })
        summary = generate_summary(ledger, [], [])
        assert sha[:8] in summary
        assert "blocked" in summary.lower()
        assert "Unsupported API call" in summary

    def test_partially_ported_visible(self):
        sha = "pp" * 20
        ledger = _make_ledger(**{
            sha: {"status": "partially_ported", "subject": "half done",
                  "work_items": [
                      _wi("pp:wi0", "ported"),
                      _wi("pp:wi1", "skipped"),
                  ]},
        })
        summary = generate_summary(ledger, [], [])
        assert sha[:8] in summary
        assert "partially_ported" in summary.lower()

    def test_ported_commit_not_in_manual_section(self):
        sha = "p" * 40
        ledger = _make_ledger(**{
            sha: {"status": "ported", "subject": "good port",
                  "work_items": [_wi("p:wi0", "ported")]},
        })
        summary = generate_summary(ledger, [], [])
        lines = list(_lines_after(summary, "## Needs Human"))
        manual_text = "\n".join(lines)
        assert sha[:8] not in manual_text or manual_text.strip() == "None."

    def test_skipped_commit_not_in_manual_section(self):
        sha = "s" * 40
        ledger = _make_ledger(**{
            sha: {"status": "skipped", "subject": "skip me",
                  "work_items": [_wi("s:wi0", "skipped")]},
        })
        summary = generate_summary(ledger, [], [])
        lines = list(_lines_after(summary, "## Needs Human"))
        manual_text = "\n".join(lines)
        assert sha[:8] not in manual_text or manual_text.strip() == "None."


# ═══════════════════════════════════════════════════════════════════════
# Test 3 — Risk section
# ═══════════════════════════════════════════════════════════════════════


class TestRiskSection(TestCase):
    """Risk section must surface concrete risks, not vague placeholders."""

    def test_git_verification_warning_appears(self):
        sha = "g" * 40
        ledger = _make_ledger(**{
            sha: {"status": "ported", "subject": "git warn",
                  "warnings": ["Git verify: partial match: found file.c, missing other.c"],
                  "work_items": [_wi("g:wi0", "ported")]},
        })
        summary = generate_summary(ledger, [], [])
        assert "Git verification warnings" in summary
        assert "partial match" in summary

    def test_validation_failed_risk(self):
        sha = "v" * 40
        ledger = _make_ledger(**{
            sha: {"status": "validation_failed", "subject": "vf risk",
                  "work_items": [_wi("v:wi0", "validation_failed")]},
        })
        summary = generate_summary(ledger, [], [])
        assert "Validation failed commits requiring attention" in summary
        assert sha[:8] in summary

    def test_partial_port_as_risk(self):
        sha = "pp" * 20
        ledger = _make_ledger(**{
            sha: {"status": "partially_ported", "subject": "partial",
                  "work_items": [
                      _wi("pp:wi0", "ported"),
                      _wi("pp:wi1", "skipped"),
                  ]},
        })
        summary = generate_summary(ledger, [], [])
        assert "incomplete porting" in summary.lower()
        assert sha[:8] in summary

    def test_no_vague_placeholder_risks(self):
        """Risk section should not contain generic/placeholder text."""
        sha = "p" * 40
        ledger = _make_ledger(**{
            sha: {"status": "ported", "subject": "clean",
                  "work_items": [_wi("p:wi0", "ported")]},
        })
        summary = generate_summary(ledger, [], [])
        risk_lines = list(_lines_after(summary, "## Risk Points"))
        risk_text = "\n".join(risk_lines).strip()
        if risk_text != "No specific risks identified.":
            for line in risk_lines:
                assert "TBD" not in line
                assert "TODO" not in line
                assert "FIXME" not in line


# ═══════════════════════════════════════════════════════════════════════
# Test 4 — Evidence visibility
# ═══════════════════════════════════════════════════════════════════════


class TestEvidenceVisibility(TestCase):
    """Decision evidence must be visible in Markdown and preserved in JSON."""

    def test_markdown_shows_evidence_for_needs_human(self):
        sha = "n" * 40
        evidence = [{"file": "file.c", "line": 1, "snippet": "int x = 1;"}]
        ledger = _make_ledger(**{
            sha: {"status": "needs_human", "subject": "conflict",
                  "work_items": [
                      _wi("n:wi0", "needs_human", decisions=[
                          _decision("complex merge", evidence=evidence),
                      ]),
                  ]},
        })
        summary = generate_summary(ledger, [], [])
        assert "Evidence" in summary
        assert "file.c" in summary
        assert "int x = 1" in summary

    def test_json_preserves_evidence(self):
        sha = "n" * 40
        evidence = [{"file": "file.c", "line": 42, "snippet": "int y = 2;"}]
        ledger = _make_ledger(**{
            sha: {"status": "needs_human", "subject": "conflict",
                  "work_items": [
                      _wi("n:wi0", "needs_human", decisions=[
                          _decision("needs review", evidence=evidence),
                      ]),
                  ]},
        })
        raw = generate_json_output(ledger, [], [])
        parsed = json.loads(raw)
        wi = parsed["commits"][sha]["work_items"][0]
        assert wi["decisions"][0]["evidence"] == evidence


# ═══════════════════════════════════════════════════════════════════════
# Test 5 — Modified files
# ═══════════════════════════════════════════════════════════════════════


class TestModifiedFiles(TestCase):
    """local_files_modified must be deduplicated and not inflated by skipped commits."""

    def test_deduplicates_files(self):
        sha = "p" * 40
        ledger = _make_ledger(**{
            sha: {"status": "ported", "subject": "dedup",
                  "local_files_modified": ["a.c", "b.c", "a.c"],
                  "work_items": [_wi("p:wi0", "ported")]},
        })
        summary = generate_summary(ledger, [], [])
        modified_lines = list(_lines_after(summary, "## Modified Files"))
        a_count = sum(1 for line in modified_lines if line.strip() == "- a.c")
        assert a_count == 1, f"a.c appears {a_count}x, expected 1"

    def test_skipped_only_commits_do_not_inflate(self):
        sha_s = "s" * 40
        sha_p = "p" * 40
        ledger = _make_ledger(**{
            sha_s: {"status": "skipped", "subject": "skip",
                    "local_files_modified": []},
            sha_p: {"status": "ported", "subject": "port",
                    "local_files_modified": ["a.c"],
                    "work_items": [_wi("p:wi0", "ported")]},
        })
        summary = generate_summary(ledger, [], [])
        modified_lines = list(_lines_after(summary, "## Modified Files"))
        file_list = [line.strip() for line in modified_lines if line.startswith("- ")]
        assert file_list == ["- a.c"], f"Got {file_list}"

    def test_mixed_commit_output_accurate(self):
        sha_a = "a" * 40
        sha_b = "b" * 40
        sha_c = "c" * 40
        ledger = _make_ledger(**{
            sha_a: {"status": "ported", "subject": "port A",
                    "local_files_modified": ["a.c", "shared.c"],
                    "work_items": [_wi("a:wi0", "ported")]},
            sha_b: {"status": "ported", "subject": "port B",
                    "local_files_modified": ["b.c", "shared.c"],
                    "work_items": [_wi("b:wi0", "ported")]},
            sha_c: {"status": "skipped", "subject": "skip C",
                    "local_files_modified": []},
        })
        summary = generate_summary(ledger, [], [])
        modified_lines = list(_lines_after(summary, "## Modified Files"))
        file_list = sorted(line.strip() for line in modified_lines if line.startswith("- "))
        assert file_list == ["- a.c", "- b.c", "- shared.c"], f"Got {file_list}"


# ═══════════════════════════════════════════════════════════════════════
# Test 6 — JSON output structure
# ═══════════════════════════════════════════════════════════════════════


class TestJsonOutput(TestCase):
    """JSON output must preserve structured data for automation."""

    def test_json_contains_meta(self):
        ledger = _make_ledger()
        raw = generate_json_output(ledger, [], [])
        parsed = json.loads(raw)
        assert "meta" in parsed
        assert parsed["meta"]["local_branch"] == "main"

    def test_json_contains_commits(self):
        sha = "c" * 40
        ledger = _make_ledger(**{
            sha: {"status": "ported", "subject": "my commit",
                  "work_items": [_wi("c:wi0", "ported")]},
        })
        raw = generate_json_output(ledger, [], [])
        parsed = json.loads(raw)
        assert sha in parsed["commits"]
        assert parsed["commits"][sha]["status"] == "ported"

    def test_json_contains_all_statuses(self):
        ledger = _make_ledger(**{
            "a" * 40: {"status": "ported", "subject": "a"},
            "b" * 40: {"status": "skipped", "subject": "b"},
            "c" * 40: {"status": "needs_human", "subject": "c"},
            "d" * 40: {"status": "blocked", "subject": "d"},
            "e" * 40: {"status": "validation_failed", "subject": "e"},
            "f" * 40: {"status": "final_manual", "subject": "f"},
            "g" * 40: {"status": "partially_ported", "subject": "g"},
        })
        raw = generate_json_output(ledger, [], [])
        parsed = json.loads(raw)
        statuses = {parsed["commits"][k]["status"] for k in parsed["commits"]}
        assert "ported" in statuses
        assert "skipped" in statuses
        assert "needs_human" in statuses
        assert "blocked" in statuses
        assert "validation_failed" in statuses
        assert "final_manual" in statuses
        assert "partially_ported" in statuses

    def test_json_contains_validation(self):
        sha = "v" * 40
        ledger = _make_ledger(**{
            sha: {"status": "validation_failed", "subject": "vf",
                  "work_items": [_wi("v:wi0", "validation_failed")]},
        })
        raw = generate_json_output(ledger, [VerifyResult(
            passed=False, command="make", exit_code=1, stderr="error",
        )], [])
        parsed = json.loads(raw)
        assert "fast_validation" in parsed
        assert len(parsed["fast_validation"]) > 0
        assert parsed["fast_validation"][0]["passed"] is False

    def test_json_contains_warnings(self):
        sha = "w" * 40
        ledger = _make_ledger(**{
            sha: {"status": "ported", "subject": "warn me",
                  "warnings": ["Git verify: partial match"],
                  "work_items": [_wi("w:wi0", "ported")]},
        })
        raw = generate_json_output(ledger, [], [])
        parsed = json.loads(raw)
        assert parsed["commits"][sha]["warnings"] == ["Git verify: partial match"]

    def test_json_contains_work_item_decisions(self):
        sha = "d" * 40
        evidence = [{"file": "f.c", "line": 5, "snippet": "int z = 3;"}]
        ledger = _make_ledger(**{
            sha: {"status": "ported", "subject": "decide",
                  "work_items": [
                      _wi("d:wi0", "ported", decisions=[
                          {"timestamp": "2024-01-01T00:00:00", "attempt": 1,
                           "confidence": "high", "reason": "clean patch",
                           "evidence": evidence},
                      ]),
                  ]},
        })
        raw = generate_json_output(ledger, [], [])
        parsed = json.loads(raw)
        wi = parsed["commits"][sha]["work_items"][0]
        assert len(wi["decisions"]) == 1
        assert wi["decisions"][0]["reason"] == "clean patch"
        assert wi["decisions"][0]["evidence"] == evidence

    def test_json_is_stable_machine_readable(self):
        sha = "s" * 40
        ledger = _make_ledger(**{
            sha: {"status": "ported", "subject": "stable",
                  "work_items": [_wi("s:wi0", "ported")]},
        })
        raw1 = generate_json_output(ledger, [], [])
        raw2 = generate_json_output(ledger, [], [])
        parsed1 = json.loads(raw1)
        parsed2 = json.loads(raw2)
        assert parsed1 == parsed2


# ═══════════════════════════════════════════════════════════════════════
# Test 7 — Blocked commit with no decision
# ═══════════════════════════════════════════════════════════════════════


class TestBlockedNoDecision(TestCase):
    """Blocked work items without decisions still appear with status."""

    def test_blocked_without_decision_still_visible(self):
        sha = "b" * 40
        ledger = _make_ledger(**{
            sha: {"status": "blocked", "subject": "crashed",
                  "work_items": [
                      # no decisions — agent crashed before appending
                      _wi("b:wi0", "blocked", decisions=[]),
                  ]},
        })
        summary = generate_summary(ledger, [], [])
        assert sha[:8] in summary
        assert "blocked" in summary.lower()
        assert "no decision recorded" in summary.lower()
