"""Phase 14 — CLI-to-harness deterministic dry run.

Tests that vpa.main() drives the real run_promotion/harness path with
injected mock agent and mock validation, producing real output files
and performing real file edits — all without real model/API calls.

Gap closed: Phase 12 tested CLI arg wiring (run_promotion fully mocked).
Phase 13 tested real run_promotion (but called directly, not through CLI).
This phase connects CLI args -> real harness with mock agent/validation.
"""

import json
import subprocess
import tempfile
from pathlib import Path
from unittest import TestCase

from vpa import ledger as L
from vpa.main import main
from vpa.tests.fixtures import (
    MockAgent,
    MockValidation,
    create_fixture_repos,
    create_multi_commit_fixture,
    create_needs_human_fixture,
    port_file_seq,
    porting_seq,
    request_human_seq,
    skip_seq,
)
from vpa.verify import VerifyResult


class TestCLIDryRun(TestCase):
    """CLI-to-harness dry run: main() -> real run_promotion -> real outputs."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.fixture_dir = self.tmp / "fixture"
        self.fixture_dir.mkdir()

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def test_cli_drives_real_harness(self):
        upstream, local, new_sha, old_sha, shas = \
            create_multi_commit_fixture(self.fixture_dir)
        sha_a, sha_b = shas
        output = self.tmp / "out"

        agent = MockAgent([
            port_file_seq(sha_a, "base.c",
                          "int base = 0;\n",
                          "int base = 0;\nint feat1 = 1;\n"),
            skip_seq(sha_b, "base.c"),
        ])
        val = MockValidation([
            [VerifyResult(passed=True, command="make", exit_code=0)],
        ])

        argv = [
            "run",
            "--upstream-path", str(upstream),
            "--local-path", str(local),
            "--upstream-old", old_sha,
            "--upstream-new", new_sha,
            "--local-branch", "main",
            "--build-cmd", "make",
            "--fast-test", "make test",
            "--output-dir", str(output),
            "--api-key", "test-key",
        ]
        main(argv=argv, agent_runner=agent, validation_runner=val)

        # ── 1. Local working tree was modified by ToolHandler.edit_file ──
        result = subprocess.run(
            ["git", "diff", "HEAD"],
            capture_output=True, text=True,
            cwd=str(local),
        )
        assert "+int feat1 = 1;" in result.stdout, (
            f"Expected '+int feat1 = 1;' in git diff HEAD, got:\n{result.stdout}"
        )

        # ── 2. No real API/model calls ──
        assert agent.call_count == 2, (
            f"Expected 2 agent calls, got {agent.call_count}"
        )
        assert val.call_count == 1, (
            f"Expected 1 validation call, got {val.call_count}"
        )

        # ── 3. Output files exist under output_dir ──
        assert (output / "ledger.json").exists()
        assert (output / "report.md").exists()
        assert (output / "report.json").exists()

        # ── 4. Ledger correctness ──
        ledger = L.load_ledger(output / "ledger.json")
        assert ledger["commits"][sha_a]["status"] == "ported", (
            f"Expected ported, got {ledger['commits'][sha_a]['status']}"
        )
        assert ledger["commits"][sha_b]["status"] == "skipped", (
            f"Expected skipped, got {ledger['commits'][sha_b]['status']}"
        )
        # local_files_modified includes the edited file
        assert "base.c" in ledger["commits"][sha_a].get("local_files_modified", []), (
            f"Expected 'base.c' in local_files_modified, "
            f"got {ledger['commits'][sha_a].get('local_files_modified', [])}"
        )
        # Validation result recorded
        assert ledger["commits"][sha_a]["validation"]["fast"]["status"] == "passed"

        # ── 5. Markdown report includes ported commit and modified file ──
        report_md = output / "report.md"
        text = report_md.read_text()
        assert sha_a[:8] in text, f"Expected {sha_a[:8]} in report"
        assert sha_b[:8] in text, f"Expected {sha_b[:8]} in report"
        assert "Ported: 1" in text, "Expected 'Ported: 1' in report"
        assert "Skipped: 1" in text, "Expected 'Skipped: 1' in report"
        assert "base.c" in text, "Expected 'base.c' in report"

        # ── 6. JSON report preserves work item structure ──
        report_json = output / "report.json"
        data = json.loads(report_json.read_text())
        assert data["commits"][sha_a]["status"] == "ported"
        assert data["commits"][sha_b]["status"] == "skipped"
        wi = data["commits"][sha_a]["work_items"][0]
        assert wi["status"] == "ported"
        assert wi["decisions"][0]["confidence"] == "high"
        assert wi["decisions"][0]["evidence"][0]["file"] == "base.c"
        assert data["fast_validation"][0]["passed"] is True


class TestCLIRetryDryRun(TestCase):
    """Phase 15 — CLI-to-harness deterministic dry run for retry."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.fixture_dir = self.tmp / "fixture"
        self.fixture_dir.mkdir()
        self.upstream, self.local, self.new_sha, self.old_sha = \
            create_fixture_repos(self.fixture_dir)
        self.output = self.tmp / "out"
        self.output.mkdir()
        create_needs_human_fixture(
            self.output, self.upstream, self.local, self.new_sha, self.old_sha,
        )

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def _retry_argv(self):
        return [
            "retry",
            "--commit-sha", self.new_sha,
            "--hint", "Check the import path",
            "--upstream-path", str(self.upstream),
            "--local-path", str(self.local),
            "--upstream-old", self.old_sha,
            "--upstream-new", self.new_sha,
            "--local-branch", "main",
            "--output-dir", str(self.output),
            "--build-cmd", "make",
            "--fast-test", "make test",
            "--model", "gpt-4o-mini",
            "--api-key", "test-key",
        ]

    def test_cli_retry_drives_real_retry_with_hint(self):
        """CLI retry reaches real retry_with_hint with hint in system prompt."""
        agent = MockAgent([])
        val = MockValidation([])

        main(argv=self._retry_argv(), agent_runner=agent, validation_runner=val)

        assert agent.call_count == 1, (
            f"Expected 1 agent call, got {agent.call_count}"
        )
        sp = agent.captured_kwargs[0]["system_prompt"]
        assert "Check the import path" in sp
        assert "Human Review Note" in sp

    def test_cli_retry_success_path(self):
        """Retry agent ports the commit; local file edited; validation recorded."""
        agent = MockAgent([porting_seq(self.new_sha)])
        val = MockValidation([
            [VerifyResult(passed=True, command="make", exit_code=0)],
        ])

        main(argv=self._retry_argv(), agent_runner=agent, validation_runner=val)

        # 1. Agent called
        assert agent.call_count == 1

        # 2. Ledger status is ported
        ledger = L.load_ledger(self.output / "ledger.json")
        assert ledger["commits"][self.new_sha]["status"] == "ported", (
            f"Expected ported, got {ledger['commits'][self.new_sha]['status']}"
        )

        # 3. Local git diff contains the edit
        result = subprocess.run(
            ["git", "diff", "HEAD"],
            capture_output=True, text=True,
            cwd=str(self.local),
        )
        assert "+int x = 1;" in result.stdout, (
            f"Expected '+int x = 1;' in git diff HEAD, got:\n{result.stdout}"
        )

        # 4. Validation recorded
        assert val.call_count == 1
        assert ledger["commits"][self.new_sha]["validation"]["fast"]["status"] == "passed"

        # 5. Report files regenerated after retry
        assert (self.output / "report.md").exists()
        assert (self.output / "report.json").exists()

        # 6. report.md shows ported status
        report_md = (self.output / "report.md").read_text()
        assert "Ported: 1" in report_md
        assert self.new_sha[:8] in report_md
        assert "final_manual" not in report_md

        # 7. report.json reflects updated status
        report_json = json.loads((self.output / "report.json").read_text())
        assert report_json["commits"][self.new_sha]["status"] == "ported"
        wi = report_json["commits"][self.new_sha]["work_items"][0]
        assert wi["status"] == "ported"
        assert wi["decisions"][0]["confidence"] == "high"
        assert report_json["fast_validation"][0]["passed"] is True

    def test_cli_retry_final_manual_path(self):
        """Retry agent requests human again → final_manual in ledger and report."""
        agent = MockAgent([request_human_seq(self.new_sha)])
        val = MockValidation([])

        main(argv=self._retry_argv(), agent_runner=agent, validation_runner=val)

        assert agent.call_count == 1

        # Ledger: final_manual
        ledger = L.load_ledger(self.output / "ledger.json")
        assert ledger["commits"][self.new_sha]["status"] == "final_manual", (
            f"Expected final_manual, got {ledger['commits'][self.new_sha]['status']}"
        )
        assert ledger["commits"][self.new_sha]["work_items"][0]["status"] == "final_manual"

        # Report files regenerated after retry
        assert (self.output / "report.md").exists()
        assert (self.output / "report.json").exists()

        # report.md shows final_manual status
        report_md = (self.output / "report.md").read_text()
        assert "final_manual" in report_md or "Manual" in report_md
        assert self.new_sha[:8] in report_md
        assert "Ported: 1" not in report_md

        # report.json reflects final_manual status
        report_json = json.loads((self.output / "report.json").read_text())
        assert report_json["commits"][self.new_sha]["status"] == "final_manual"
