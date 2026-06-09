import subprocess
from dataclasses import dataclass


@dataclass
class VerifyResult:
    passed: bool
    command: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int = -1
    duration_s: float = 0.0


def run_fast_validation(build_cmd, test_cmds, local_repo, timeout=120):
    """Run build + targeted tests. Returns list of VerifyResult."""
    results = []

    # Build
    results.append(_run_cmd(build_cmd, local_repo, timeout))

    # Related tests
    for cmd in test_cmds:
        if not results[-1].passed:
            # Skip remaining tests if build failed
            break
        results.append(_run_cmd(cmd, local_repo, timeout))

    return results


def run_slow_validation(test_cmds, local_repo, timeout=600):
    """Run full test suite. Returns list of VerifyResult."""
    results = []
    for cmd in test_cmds:
        result = _run_cmd(cmd, local_repo, timeout)
        results.append(result)
        if not result.passed:
            break
    return results


def _run_cmd(cmd, cwd, timeout):
    import time

    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout,
        )
        stdout = proc.stdout[-5000:] if len(proc.stdout) > 5000 else proc.stdout
        stderr = proc.stderr[-3000:] if len(proc.stderr) > 3000 else proc.stderr
        return VerifyResult(
            passed=proc.returncode == 0,
            command=cmd,
            stdout=stdout,
            stderr=stderr,
            exit_code=proc.returncode,
            duration_s=time.monotonic() - start,
        )
    except subprocess.TimeoutExpired:
        return VerifyResult(
            passed=False,
            command=cmd,
            stderr=f"timeout after {timeout}s",
            duration_s=time.monotonic() - start,
        )
    except Exception as e:
        return VerifyResult(
            passed=False,
            command=cmd,
            stderr=str(e),
            duration_s=time.monotonic() - start,
        )


def validation_failed(results):
    """Check if any verification step failed."""
    return any(not r.passed for r in results)


def format_verify_results(results):
    """Format verification results for agent consumption."""
    lines = []
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        lines.append(
            f"[{status}] {r.command} (exit={r.exit_code}, {r.duration_s:.1f}s)"
        )
        if not r.passed and r.stderr:
            lines.append(f"  stderr: {r.stderr[:500]}")
    return "\n".join(lines)
