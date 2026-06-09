import contextlib
import os
from pathlib import Path

from . import ledger as L
from .agent import run_agent
from .prompt import (
    build_hint_injection,
    build_restart_context,
    build_system_prompt,
)
from .report import generate_json_output, generate_summary
from .slicer import SliceLevel, get_commit_subject, slice_commits
from .tools import TOOL_DEFINITIONS, ToolHandler
from .verify import (
    format_verify_results,
    run_fast_validation,
    run_slow_validation,
    validation_failed,
)

CONTEXT_LIMIT_CHARS = 100000
CONTEXT_USAGE_THRESHOLD = 0.65


def run_promotion(
    *,
    upstream_path,
    local_path,
    upstream_old,
    upstream_new,
    local_branch,
    build_cmd,
    fast_test_cmds,
    slow_test_cmds=None,
    model="gpt-4o",
    api_key=None,
    base_url=None,
    max_commits_per_restart=10,
    output_dir=None,
    upstream_name="upstream",
    local_name="local",
    arch="<arch>",
):
    api_key = api_key or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("No API key: set OPENAI_API_KEY or pass api_key=")

    output_dir = Path(output_dir or ".")
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. Init or load ledger
    ledger_path = output_dir / "ledger.json"
    if ledger_path.exists():
        ledger = L.load_ledger(ledger_path)
    else:
        session_meta = L.init_session_meta(
            upstream_name=upstream_name,
            upstream_old=upstream_old,
            upstream_new=upstream_new,
            local_name=local_name,
            local_branch=local_branch,
            arch=arch,
            upstream_path=upstream_path,
            local_path=local_path,
            build_cmd=build_cmd,
            fast_test_cmds=fast_test_cmds,
            slow_test_cmds=slow_test_cmds or [],
        )
        ledger, ledger_path = L.init_ledger(session_meta, output_dir)

    # 2. Slice commits & initialize ledger entries (harness creates structure)
    slices = list(slice_commits(upstream_path, upstream_old, upstream_new, local_path))

    # Group slices by commit for initialization
    commit_info = {}  # sha -> {"subject": ..., "files": ..., "slices": [...]}
    for sl in slices:
        sha = sl.commit_sha
        if sha not in commit_info:
            commit_info[sha] = {
                "subject": get_commit_subject(upstream_path, sha),
                "files": list(sl.files),
                "slices": [],
            }
        else:
            for f in sl.files:
                if f not in commit_info[sha]["files"]:
                    commit_info[sha]["files"].append(f)
        commit_info[sha]["slices"].append(sl)

    # Init commit entries and work items in ledger
    for sha, info in commit_info.items():
        if sha not in ledger["commits"]:
            L.init_commit_entry(
                ledger, sha, info["subject"], info["files"]
            )
            # Collect work items from all slices of this commit
            work_items = []
            for sl in info["slices"]:
                work_items.extend(sl.to_work_items())
            L.init_work_items(ledger, sha, work_items)

    L.write_ledger(ledger_path, ledger)

    # 3. Process slices
    commits_since_restart = 0
    processed_commits = set()
    fast_results = []
    slow_results = []

    for sl in slices:
        sha = sl.commit_sha
        entry = ledger["commits"].get(sha, {})

        # Skip terminal commits
        if entry.get("status") in ("ported", "skipped", "needs_human", "blocked"):
            continue

        # Compute work item IDs for this slice (always needed)
        wi_ids = [wi["id"] for wi in sl.to_work_items()]

        # Build prompt context
        if commits_since_restart >= max_commits_per_restart or _context_over(
            ledger, sl
        ):
            snapshot = L.commit_snapshot(ledger)
            base_sp = build_system_prompt(
                upstream_name=upstream_name,
                upstream_old=upstream_old,
                upstream_new=upstream_new,
                local_name=local_name,
                local_branch=local_branch,
                arch=arch,
                slice_description=sl.describe(),
                ledger_summary=L.ledger_for_prompt(ledger),
            )
            sp, _restart_msg = build_restart_context(
                snapshot, base_sp, sl.describe(),
            )
            um = (
                f"{_restart_msg}\n"
                f"Work items to process: {', '.join(wi_ids)}\n\n"
                f"{sl.context}"
            )
            commits_since_restart = 0
        else:
            sp = build_system_prompt(
                upstream_name=upstream_name,
                upstream_old=upstream_old,
                upstream_new=upstream_new,
                local_name=local_name,
                local_branch=local_branch,
                arch=arch,
                slice_description=sl.describe(),
                ledger_summary=L.ledger_for_prompt(ledger),
            )

            um = (
                f"Process {sl.describe()}.\n"
                f"Work items to process: {', '.join(wi_ids)}\n\n"
                f"{sl.context}"
            )

        # Run agent
        tool_handler = ToolHandler(local_path, upstream_path, ledger, ledger_path)

        try:
            run_agent(
                system_prompt=sp,
                user_message=um,
                tools=TOOL_DEFINITIONS,
                on_tool_call=lambda name, args, th=tool_handler: th.dispatch(name, args),
                model=model,
                api_key=api_key,
                base_url=base_url,
            )
        except RuntimeError:
            for wi_id in wi_ids:
                with contextlib.suppress(KeyError, ValueError):
                    L.complete_work_item(ledger, sha, wi_id, "blocked")
            L.write_ledger(ledger_path, ledger)
            continue

        # Reload ledger
        ledger = L.load_ledger(ledger_path)

        # Git verify
        entry = ledger["commits"].get(sha, {})
        ok, detail = L.git_verify(local_path, entry)
        if not ok:
            for wi_id in wi_ids:
                with contextlib.suppress(KeyError, ValueError):
                    L.complete_work_item(ledger, sha, wi_id, "validation_failed")
            L.record_validation(
                ledger, sha, "fast",
                {"status": "failed", "command": "git diff HEAD", "exit_code": -1,
                 "summary": f"Git verify failed: {detail}"},
            )
            L.write_ledger(ledger_path, ledger)
            continue

        # Fast validation — only if ALL work items for this commit are terminal
        all_terminal = _all_work_items_terminal(entry)
        ported_items = [
            wi for wi in entry.get("work_items", [])
            if wi["status"] == "ported"
        ]
        if ported_items and all_terminal:
            vresults = run_fast_validation(build_cmd, fast_test_cmds, local_path)
            fast_results.extend(vresults)

            passed = not validation_failed(vresults)
            L.record_validation(
                ledger, sha, "fast",
                {
                    "status": "passed" if passed else "failed",
                    "command": build_cmd,
                    "exit_code": vresults[0].exit_code if vresults else -1,
                    "summary": format_verify_results(vresults),
                },
            )
            L.write_ledger(ledger_path, ledger)

            if not passed:
                # One self-repair attempt
                fix_ok = _attempt_fix(
                    sp, vresults, tool_handler, model, api_key, base_url
                )
                if fix_ok:
                    vresults2 = run_fast_validation(
                        build_cmd, fast_test_cmds, local_path
                    )
                    fast_results.extend(vresults2)
                    if validation_failed(vresults2):
                        _mark_items_validation_failed(
                            ledger, sha, ported_items, format_verify_results(vresults2)
                        )
                        L.write_ledger(ledger_path, ledger)
                        continue
                    L.record_validation(
                        ledger, sha, "fast",
                        {
                            "status": "passed",
                            "command": build_cmd,
                            "summary": "passed after repair",
                        },
                    )
                else:
                    _mark_items_validation_failed(
                        ledger, sha, ported_items, format_verify_results(vresults)
                    )
                    L.write_ledger(ledger_path, ledger)
                    continue

        # Track restart
        terminal_statuses = ("ported", "skipped", "needs_human")
        if sl.level == SliceLevel.COMMIT or (
            sha not in processed_commits and entry.get("status") in terminal_statuses
        ):
            processed_commits.add(sha)
            commits_since_restart += 1

    # 4. Slow validation
    if slow_test_cmds:
        slow_results = run_slow_validation(slow_test_cmds, local_path)

    # 5. Generate output
    summary = generate_summary(ledger, fast_results, slow_results)
    json_output = generate_json_output(ledger, fast_results, slow_results)

    with open(output_dir / "report.md", "w") as f:
        f.write(summary)
    with open(output_dir / "report.json", "w") as f:
        f.write(json_output)

    return summary, json_output


def retry_with_hint(
    *,
    commit_sha,
    hint,
    upstream_path,
    local_path,
    upstream_old,
    upstream_new,
    local_branch,
    output_dir,
    build_cmd,
    fast_test_cmds,
    model="gpt-4o",
    api_key=None,
    base_url=None,
    upstream_name="upstream",
    local_name="local",
    arch="<arch>",
):
    """Retry a needs_human commit with a human-provided hint. One retry only."""
    api_key = api_key or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("No API key")

    ledger_path = Path(output_dir) / "ledger.json"
    if not ledger_path.exists():
        raise RuntimeError(f"Ledger not found at {ledger_path}")

    ledger = L.load_ledger(ledger_path)
    entry = ledger["commits"].get(commit_sha)
    if not entry or entry["status"] != "needs_human":
        raise RuntimeError(
            f"Commit {commit_sha[:8]} is not in needs_human state"
        )

    # Reset needs_human work items for hint-gated retry
    L.reset_for_retry(ledger, commit_sha)
    L.write_ledger(ledger_path, ledger)

    tool_handler = ToolHandler(local_path, upstream_path, ledger, ledger_path)

    from .slicer import get_commit_diff, get_commit_files

    diff = get_commit_diff(upstream_path, commit_sha)
    files = get_commit_files(upstream_path, commit_sha)

    wi_ids = [
        wi["id"] for wi in entry.get("work_items", [])
        if wi["status"] == "pending"
    ]

    sp = build_system_prompt(
        upstream_name=upstream_name,
        upstream_old=upstream_old,
        upstream_new=upstream_new,
        local_name=local_name,
        local_branch=local_branch,
        arch=arch,
        slice_description=f"commit {commit_sha[:8]} (RETRY with human hint)",
        ledger_summary=L.ledger_for_prompt(ledger),
    )
    sp += "\n\n" + build_hint_injection(hint)

    um = (
        f"Retry porting commit {commit_sha[:8]}. Previous attempt was marked needs_human.\n"
        f"Work items: {', '.join(wi_ids)}\n"
        f"Files: {', '.join(files)}\n\nDiff:\n{diff}"
    )

    try:
        run_agent(
            system_prompt=sp,
            user_message=um,
            tools=TOOL_DEFINITIONS,
            on_tool_call=lambda name, args: tool_handler.dispatch(name, args),
            model=model,
            api_key=api_key,
            base_url=base_url,
        )
    except RuntimeError:
        _mark_items_blocked(ledger, commit_sha, "Agent error during retry")
        L.write_ledger(ledger_path, ledger)
        return None

    ledger = L.load_ledger(ledger_path)
    entry = ledger["commits"].get(commit_sha, {})

    # Check if still needs_human after retry
    still_needs = [
        wi for wi in entry.get("work_items", [])
        if wi["status"] == "needs_human"
    ]
    if still_needs:
        for wi in still_needs:
            wi["status"] = "final_manual"
        L._derive_commit_status(entry)
        L.write_ledger(ledger_path, ledger)
        return None

    # Fast validation
    vresults = run_fast_validation(build_cmd, fast_test_cmds, local_path)
    if validation_failed(vresults):
        L.record_validation(
            ledger, commit_sha, "fast",
            {
                "status": "failed",
                "command": build_cmd,
                "summary": format_verify_results(vresults),
            },
        )
        _mark_items_validation_failed(
            ledger, commit_sha,
            [wi for wi in entry.get("work_items", []) if wi["status"] == "ported"],
            format_verify_results(vresults),
        )
        L.write_ledger(ledger_path, ledger)
        return None

    L.record_validation(ledger, commit_sha, "fast", {"status": "passed"})
    L.write_ledger(ledger_path, ledger)
    return entry


def _context_over(ledger, current_slice):
    summary = L.ledger_for_prompt(ledger)
    slice_text = current_slice.context or ""
    total = 3000 + len(summary) + len(slice_text)
    return total / CONTEXT_LIMIT_CHARS > CONTEXT_USAGE_THRESHOLD


def _attempt_fix(system_prompt, vresults, tool_handler, model, api_key, base_url):
    failure_report = format_verify_results(vresults)
    fix_prompt = (
        f"{system_prompt}\n\n"
        "## Fix Previous Attempt\n"
        "The build or tests failed after your last porting attempt. "
        "Analyze the failure output below and fix the issue.\n\n"
        f"Failure output:\n{failure_report}"
    )
    try:
        run_agent(
            system_prompt=fix_prompt,
            user_message="Fix the build/test failures shown above.",
            tools=TOOL_DEFINITIONS,
            on_tool_call=lambda name, args: tool_handler.dispatch(name, args),
            model=model,
            api_key=api_key,
            base_url=base_url,
        )
        return True
    except RuntimeError:
        return False


def _mark_items_validation_failed(ledger, commit_sha, work_items, summary):
    """Mark work items as validation_failed and record the validation result."""
    for wi in work_items:
        with contextlib.suppress(KeyError, ValueError):
            L.complete_work_item(
                ledger, commit_sha, wi["id"], "validation_failed"
            )
    L.record_validation(
        ledger, commit_sha, "fast",
        {"status": "failed", "summary": summary},
    )


def _all_work_items_terminal(entry):
    """True when every work item in the commit has reached a terminal status."""
    terminal = {"ported", "skipped", "blocked", "needs_human", "validation_failed",
                "final_manual"}
    for wi in entry.get("work_items", []):
        if wi["status"] not in terminal:
            return False
    return bool(entry.get("work_items"))


def _mark_items_blocked(ledger, commit_sha, reason):
    """Mark all pending/in_progress items as blocked."""
    entry = ledger["commits"].get(commit_sha, {})
    for wi in entry.get("work_items", []):
        if wi["status"] in ("pending", "in_progress"):
            try:
                L.complete_work_item(ledger, commit_sha, wi["id"], "blocked")
            except (KeyError, ValueError):
                wi["status"] = "blocked"
