import json
from datetime import UTC, datetime


def generate_summary(ledger, fast_results, slow_results):
    meta = ledger["meta"]
    commits = ledger["commits"]

    total = len(commits)
    ported = sum(1 for c in commits.values() if c["status"] == "ported")
    skipped = sum(1 for c in commits.values() if c["status"] == "skipped")
    needs_human = sum(1 for c in commits.values() if c["status"] == "needs_human")
    blocked = sum(1 for c in commits.values() if c["status"] == "blocked")
    validation_failed = sum(
        1 for c in commits.values() if c["status"] == "validation_failed"
    )
    final_manual = sum(
        1 for c in commits.values() if c["status"] == "final_manual"
    )
    partially = sum(
        1 for c in commits.values() if c["status"] == "partially_ported"
    )

    fast_status = (
        "PASS"
        if fast_results and all(r.passed for r in fast_results)
        else "FAIL" if fast_results else "NOT RUN"
    )
    slow_status = (
        "NOT RUN"
        if not slow_results
        else "PASS"
        if all(r.passed for r in slow_results)
        else "FAIL"
    )

    report_title = (
        f"# Promotion Report: {meta['upstream_old']}..{meta['upstream_new']}"
        f" → {meta['local_branch']}"
    )
    lines = [
        report_title,
        "",
        "## Overview",
        f"- Total upstream commits: {total}",
        f"- Ported: {ported}  |  Skipped: {skipped}  |  Needs Human: {needs_human}"
        f"  |  Blocked: {blocked}",
        f"- Validation Failed: {validation_failed}  |  Final Manual: {final_manual}"
        f"  |  Partially Ported: {partially}",
        f"- Fast validation: {fast_status} ({_count_failures(fast_results)} failures)",
        f"- Slow validation: {slow_status}",
        f"- Completed at: {meta.get('updated_at', datetime.now(UTC).isoformat())}",
        "",
        "## Ported Commits",
        "| SHA | Subject | Intent | Method | Files |",
        "|-----|---------|--------|--------|-------|",
    ]

    for sha, entry in sorted(commits.items()):
        if entry["status"] == "ported":
            # Aggregate methods from work items
            methods = {
                wi.get("method")
                for wi in entry.get("work_items", [])
                if wi.get("method")
            }
            method_str = ", ".join(sorted(methods)) if methods else "-"
            files = ", ".join(entry.get("local_files_modified", []))
            subject = entry.get("upstream_subject", "")[:50]
            intent = (entry.get("intent_summary") or "")[:60]
            lines.append(f"| {sha[:8]} | {subject} | {intent} | {method_str} | {files} |")

    lines += ["", "## Needs Human / Manual Required"]

    manual_entries = [
        (sha, entry)
        for sha, entry in commits.items()
        if entry["status"] in (
            "needs_human", "partially_ported", "validation_failed", "final_manual",
            "blocked",
        )
    ]
    if manual_entries:
        for sha, entry in sorted(manual_entries):
            lines.append(f"### {sha[:8]} — {entry.get('upstream_subject', '?')[:60]}")
            lines.append(f"- **Status**: {entry['status']}")
            intent = entry.get("intent_summary")
            if intent:
                lines.append(f"- **Intent**: {intent}")
            for wi in entry.get("work_items", []):
                if wi["status"] in ("needs_human", "validation_failed", "blocked"):
                    latest = wi["decisions"][-1] if wi["decisions"] else None
                    reason = latest["reason"] if latest else "no decision recorded"
                    lines.append(
                        f"- `{wi['id']}` ({wi['kind']}, {wi['status']}): {reason}"
                    )
                    if latest and latest.get("evidence"):
                        for ev in latest["evidence"]:
                            file = ev.get("file", "?")
                            line = ev.get("line", "")
                            snippet = ev.get("snippet", "")
                            loc = f"{file}:{line}" if line else file
                            ev_line = f"  - Evidence: {loc}"
                            if snippet:
                                ev_line += f" `{snippet[:60]}`"
                            lines.append(ev_line)
            if entry["status"] == "validation_failed":
                v = entry.get("validation", {})
                for vtype in ("fast", "slow"):
                    vresult = v.get(vtype, {})
                    if vresult.get("status") == "failed":
                        cmd = vresult.get("command", "?")
                        ec = vresult.get("exit_code", "?")
                        summary = vresult.get("summary", "")
                        lines.append(f"  - Validation ({vtype}): `{cmd}` (exit_code={ec})")
                        if summary:
                            lines.append(f"    Summary: {summary[:200]}")
            lines.append("")
    else:
        lines.append("None.")

    lines += ["", "## Skipped Commits", "| SHA | Subject | Reason |", "|-----|---------|--------|"]

    for sha, entry in sorted(commits.items()):
        if entry["status"] == "skipped":
            reasons = []
            for wi in entry.get("work_items", []):
                latest = wi["decisions"][-1] if wi["decisions"] else None
                if latest:
                    reasons.append(latest["reason"][:100])
            lines.append(
                f"| {sha[:8]} | {entry.get('upstream_subject', '?')[:50]} | {'; '.join(reasons)} |"
            )

    lines += ["", "## Risk Points"]

    risks = _identify_risks(ledger, fast_results, slow_results)
    if risks:
        for r in risks:
            lines.append(f"- {r}")
    else:
        lines.append("No specific risks identified.")

    lines += ["", "## Modified Files"]

    all_files = set()
    for entry in commits.values():
        for f in entry.get("local_files_modified", []):
            all_files.add(f)

    if all_files:
        for f in sorted(all_files):
            lines.append(f"- {f}")
    else:
        lines.append("(no files modified)")

    if fast_results:
        lines += ["", "## Fast Validation Results"]
        for r in fast_results:
            status = "PASS" if r.passed else "FAIL"
            lines.append(f"- [{status}] `{r.command}` ({r.duration_s:.1f}s)")

    if slow_results:
        lines += ["", "## Slow Validation Results"]
        for r in slow_results:
            status = "PASS" if r.passed else "FAIL"
            lines.append(f"- [{status}] `{r.command}` ({r.duration_s:.1f}s)")
            if not r.passed:
                lines.append(f"  ```\n  {r.stderr[:500]}\n  ```")

    return "\n".join(lines)


def generate_json_output(ledger, fast_results, slow_results):
    return json.dumps(
        {
            "meta": ledger["meta"],
            "commits": ledger["commits"],
            "fast_validation": [
                {
                    "command": r.command,
                    "passed": r.passed,
                    "exit_code": r.exit_code,
                    "duration_s": r.duration_s,
                }
                for r in fast_results
            ],
            "slow_validation": [
                {
                    "command": r.command,
                    "passed": r.passed,
                    "exit_code": r.exit_code,
                    "duration_s": r.duration_s,
                }
                for r in slow_results
            ],
        },
        indent=2,
        ensure_ascii=False,
    )


def _count_failures(results):
    return sum(1 for r in results if not r.passed)


def _identify_risks(ledger, fast_results, slow_results):
    risks = []

    semantic_ports = [
        sha[:8]
        for sha, c in ledger["commits"].items()
        if any(
            wi.get("method") == "semantic_port"
            for wi in c.get("work_items", [])
        )
        and c["status"] == "ported"
    ]
    if semantic_ports and not slow_results:
        risks.append(
            f"Semantically ported commits without slow validation: {', '.join(semantic_ports)}"
        )

    partials = [
        sha[:8]
        for sha, c in ledger["commits"].items()
        if c["status"] == "partially_ported"
    ]
    if partials:
        risks.append(f"Commits with incomplete porting: {', '.join(partials)}")

    if fast_results and any(not r.passed for r in fast_results):
        failed = [r.command for r in fast_results if not r.passed]
        risks.append(f"Fast validation failures: {', '.join(failed)}")

    if slow_results and any(not r.passed for r in slow_results):
        failed = [r.command for r in slow_results if not r.passed]
        risks.append(f"Slow validation failures: {', '.join(failed)}")

    # Commits in validation_failed state
    vf_commits = [
        sha[:8] for sha, c in ledger["commits"].items()
        if c["status"] == "validation_failed"
    ]
    if vf_commits:
        risks.append(
            f"Validation failed commits requiring attention: {', '.join(vf_commits)}"
        )

    # Git verification warnings from commit entries
    git_warnings = []
    for sha, c in ledger["commits"].items():
        for w in c.get("warnings", []):
            git_warnings.append(f"{sha[:8]}: {w}")
    if git_warnings:
        risks.append("Git verification warnings:")
        for gw in git_warnings:
            risks.append(f"  - {gw}")

    # Commits with no intent_summary (agent skipped the record_intent step)
    no_intent = [
        sha[:8]
        for sha, c in ledger["commits"].items()
        if c["status"] == "ported" and not c.get("intent_summary")
    ]
    if no_intent:
        risks.append(
            "Ported commits missing intent summary"
            f" (agent bypassed record_intent): {', '.join(no_intent)}"
        )

    return risks
