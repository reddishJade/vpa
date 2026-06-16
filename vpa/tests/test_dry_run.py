"""Phase 13 — Packaged local fixture dry run.

Tests a full VPA run using the consolidated fixtures module:
upstream repo, local repo, mock agent, mock validation runner,
and verified output files (ledger, report.md, report.json).

No real model/API calls.  Local temporary git repos only.
"""

import json
import subprocess
import tempfile
from pathlib import Path
from unittest import TestCase

from vpa import ledger as L
from vpa.harness import run_promotion
from vpa.tests.fixtures import (
    MockAgent,
    MockValidation,
    create_multi_commit_fixture,
    port_file_seq,
    skip_seq,
)
from vpa.verify import VerifyResult


class TestPackagedDryRun(TestCase):
    """Packaged local fixture dry run with one ported and one skipped commit."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.fixture_dir = self.tmp / "fixture"
        self.fixture_dir.mkdir()

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def test_packaged_dry_run(self):
        upstream, local, new_sha, old_sha, shas = \
            create_multi_commit_fixture(self.fixture_dir)
        sha_a, sha_b = shas
        output = self.tmp / "out"
        output.mkdir()

        agent = MockAgent([
            port_file_seq(sha_a, "base.c",
                          "int base = 0;\n",
                          "int base = 0;\nint feat1 = 1;\n"),
            skip_seq(sha_b, "base.c"),
        ])
        val = MockValidation([
            [VerifyResult(passed=True, command="make", exit_code=0)],
        ])

        run_promotion(
            upstream_path=str(upstream),
            local_path=str(local),
            upstream_old=old_sha,
            upstream_new=new_sha,
            local_branch="main",
            build_cmd="make",
            fast_test_cmds=["make test"],
            output_dir=str(output),
            api_key="test-key",
            agent_runner=agent,
            validation_runner=val,
        )

        # ── 1. Local working tree changed for the ported commit ──
        result = subprocess.run(
            ["git", "diff", "HEAD"],
            capture_output=True, text=True,
            cwd=str(local),
        )
        assert "+int feat1 = 1;" in result.stdout, (
            f"Expected '+int feat1 = 1;' in git diff HEAD, got:\n{result.stdout}"
        )

        # ── 2. Skipped/manual commit is represented correctly ──
        ledger = L.load_ledger(output / "ledger.json")
        assert ledger["commits"][sha_a]["status"] == "ported", \
            f"Expected ported, got {ledger['commits'][sha_a]['status']}"
        assert ledger["commits"][sha_b]["status"] == "skipped", \
            f"Expected skipped, got {ledger['commits'][sha_b]['status']}"

        # ── 3. ledger.json exists and has expected statuses ──
        assert (output / "ledger.json").exists()
        assert ledger["commits"][sha_a]["work_items"][0]["status"] == "ported"
        assert ledger["commits"][sha_b]["work_items"][0]["status"] == "skipped"

        # ── 4. report.md exists and is human-actionable ──
        report_md = output / "report.md"
        assert report_md.exists()
        text = report_md.read_text()
        assert "Ported: 1" in text, f"Expected 'Ported: 1' in report, got:\n{text}"
        assert "Skipped: 1" in text, f"Expected 'Skipped: 1' in report, got:\n{text}"
        assert sha_a[:8] in text, f"Expected {sha_a[:8]} in report"
        assert sha_b[:8] in text, f"Expected {sha_b[:8]} in report"

        # ── 5. report.json exists and preserves structured data ──
        report_json = output / "report.json"
        assert report_json.exists()
        data = json.loads(report_json.read_text())
        assert "meta" in data
        assert data["commits"][sha_a]["status"] == "ported"
        assert data["commits"][sha_b]["status"] == "skipped"
        assert data["fast_validation"][0]["passed"] is True

        # ── 6. No real model/API calls occurred ──
        assert agent.call_count == 2, \
            f"Expected 2 agent calls, got {agent.call_count}"
        assert val.call_count == 1, \
            f"Expected 1 validation call, got {val.call_count}"
