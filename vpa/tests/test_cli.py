"""CLI smoke tests for Phase 12: argument wiring, output paths, failure behavior.

Verifies vpa.main.main() argument parsing and delegation. No real API/model
calls. Uses monkeypatching and temporary fixture repos.
"""

import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from vpa.main import main


class TestCLIRunWiring(TestCase):
    """Verify CLI run command arguments are correctly passed to run_promotion."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.upstream = self.tmp / "upstream"
        self.local = self.tmp / "local"
        self.upstream.mkdir()
        self.local.mkdir()

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def _run_main(self, args, mock_impl=None):
        """Call main() with patched sys.argv and run_promotion."""
        argv = ["vpa", "run"] + args
        with patch.object(sys, "argv", argv), patch("vpa.main.run_promotion") as mr:
            if mock_impl:
                mr.side_effect = mock_impl
            else:
                mr.return_value = ("summary text", "{}")
            main()
            return mr

    def test_minimal_required_args(self):
        """Minimal required args reach run_promotion with correct values."""
        args = [
            "--upstream-path", str(self.upstream),
            "--local-path", str(self.local),
            "--upstream-old", "abc1234",
            "--upstream-new", "def5678",
            "--local-branch", "main",
            "--build-cmd", "make",
        ]
        mock = self._run_main(args)
        mock.assert_called_once()
        kw = mock.call_args.kwargs
        assert kw["upstream_path"] == str(self.upstream)
        assert kw["local_path"] == str(self.local)
        assert kw["upstream_old"] == "abc1234"
        assert kw["upstream_new"] == "def5678"
        assert kw["local_branch"] == "main"
        assert kw["build_cmd"] == "make"
        assert kw["fast_test_cmds"] == []
        assert kw["slow_test_cmds"] == []
        assert kw["output_dir"] == "./promotion_output"

    def test_all_args_passed(self):
        """All explicit args are forwarded with correct types."""
        out = self.tmp / "out"
        args = [
            "--upstream-path", str(self.upstream),
            "--local-path", str(self.local),
            "--upstream-old", "abc1234",
            "--upstream-new", "def5678",
            "--local-branch", "develop",
            "--build-cmd", "make -j4",
            "--fast-test", "make test",
            "--fast-test", "make lint",
            "--slow-test", "make bench",
            "--model", "gpt-4o-mini",
            "--api-key", "sk-test",
            "--base-url", "https://api.test.com",
            "--output-dir", str(out),
            "--upstream-name", "origin",
            "--local-name", "fork",
            "--arch", "x86_64",
            "--max-commits-per-restart", "3",
        ]
        mock = self._run_main(args)
        mock.assert_called_once()
        kw = mock.call_args.kwargs
        assert kw["upstream_path"] == str(self.upstream)
        assert kw["local_path"] == str(self.local)
        assert kw["upstream_old"] == "abc1234"
        assert kw["upstream_new"] == "def5678"
        assert kw["local_branch"] == "develop"
        assert kw["build_cmd"] == "make -j4"
        assert kw["fast_test_cmds"] == ["make test", "make lint"]
        assert kw["slow_test_cmds"] == ["make bench"]
        assert kw["model"] == "gpt-4o-mini"
        assert kw["api_key"] == "sk-test"
        assert kw["base_url"] == "https://api.test.com"
        assert kw["output_dir"] == str(out)
        assert kw["upstream_name"] == "origin"
        assert kw["local_name"] == "fork"
        assert kw["arch"] == "x86_64"
        assert kw["max_commits_per_restart"] == 3

    def test_output_dir_honored(self):
        """Output files appear in the directory given by --output-dir."""
        output_dir = self.tmp / "my_output"

        def mock_run(**kwargs):
            out = Path(kwargs["output_dir"])
            out.mkdir(parents=True, exist_ok=True)
            (out / "ledger.json").write_text('{"meta": {}}')
            (out / "report.md").write_text("# summary")
            (out / "report.json").write_text('{"meta": {}}')
            return ("# summary", '{"meta": {}}')

        args = [
            "--upstream-path", str(self.upstream),
            "--local-path", str(self.local),
            "--upstream-old", "abc1234",
            "--upstream-new", "def5678",
            "--local-branch", "main",
            "--build-cmd", "make",
            "--output-dir", str(output_dir),
        ]
        mock = self._run_main(args, mock_impl=mock_run)
        mock.assert_called_once()
        assert (output_dir / "ledger.json").exists()
        assert (output_dir / "report.md").exists()
        assert (output_dir / "report.json").exists()

    def test_existing_output_dir_reused(self):
        """Existing output dir is passed through (run_promotion handles reuse)."""
        output_dir = self.tmp / "existing_out"
        output_dir.mkdir()
        (output_dir / "ledger.json").write_text('{"existing": true}')

        def mock_run(**kwargs):
            out = Path(kwargs["output_dir"])
            assert out == output_dir
            # Simulate existing-ledger load behavior
            existing = out / "ledger.json"
            assert existing.exists()
            return ("summary", "{}")

        args = [
            "--upstream-path", str(self.upstream),
            "--local-path", str(self.local),
            "--upstream-old", "abc",
            "--upstream-new", "def",
            "--local-branch", "main",
            "--build-cmd", "make",
            "--output-dir", str(output_dir),
        ]
        mock = self._run_main(args, mock_impl=mock_run)
        mock.assert_called_once()

    def test_missing_required_arg_exits(self):
        """Missing --upstream-path causes SystemExit from argparse."""
        argv = [
            "vpa", "run",
            "--local-path", str(self.local),
            "--upstream-old", "abc",
            "--upstream-new", "def",
            "--local-branch", "main",
            "--build-cmd", "make",
        ]
        with patch.object(sys, "argv", argv):
            with self.assertRaises(SystemExit) as cm:
                main()
            assert cm.exception.code == 2

    def test_unknown_command_exits(self):
        """Unknown subcommand causes SystemExit (exit code 2 from argparse)."""
        argv = ["vpa", "nonexistent"]
        with patch.object(sys, "argv", argv):
            with self.assertRaises(SystemExit) as cm:
                main()
            assert cm.exception.code == 2


class TestCLIRetryWiring(TestCase):
    """Verify CLI retry command arguments are correctly passed."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.upstream = self.tmp / "upstream"
        self.local = self.tmp / "local"
        self.upstream.mkdir()
        self.local.mkdir()

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def test_retry_args_passed(self):
        """All retry args are forwarded to retry_with_hint."""
        argv = [
            "vpa", "retry",
            "--commit-sha", "abc1234",
            "--hint", "Check the import path",
            "--upstream-path", str(self.upstream),
            "--local-path", str(self.local),
            "--upstream-old", "oldtag",
            "--upstream-new", "newtag",
            "--local-branch", "main",
            "--output-dir", str(self.tmp / "out"),
            "--build-cmd", "make",
            "--fast-test", "make test",
            "--model", "gpt-4o-mini",
            "--api-key", "sk-key",
            "--base-url", "https://api.test.com",
            "--upstream-name", "origin",
            "--local-name", "fork",
            "--arch", "arm64",
        ]
        with patch.object(sys, "argv", argv), patch("vpa.main.retry_with_hint") as mr:
            mr.return_value = None
            main()
        mr.assert_called_once()
        kw = mr.call_args.kwargs
        assert kw["commit_sha"] == "abc1234"
        assert kw["hint"] == "Check the import path"
        assert kw["upstream_path"] == str(self.upstream)
        assert kw["local_path"] == str(self.local)
        assert kw["upstream_old"] == "oldtag"
        assert kw["upstream_new"] == "newtag"
        assert kw["local_branch"] == "main"
        assert kw["build_cmd"] == "make"
        assert kw["fast_test_cmds"] == ["make test"]
        assert kw["model"] == "gpt-4o-mini"
        assert kw["arch"] == "arm64"

    def test_retry_missing_required_arg_exits(self):
        """Missing --commit-sha causes SystemExit."""
        argv = [
            "vpa", "retry",
            "--hint", "fix it",
            "--upstream-path", "/tmp/u",
            "--local-path", "/tmp/l",
            "--upstream-old", "o",
            "--upstream-new", "n",
            "--local-branch", "main",
            "--output-dir", "/tmp/o",
            "--build-cmd", "make",
        ]
        with patch.object(sys, "argv", argv):
            with self.assertRaises(SystemExit) as cm:
                main()
            assert cm.exception.code == 2
