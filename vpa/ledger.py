"""Ledger — agent external working memory.

Three layers:
  0. Session Record (root `meta`)     — task parameters
  1. Commit Entry                     — per-upstream-commit tracking
  2. Work Item (+ Decision Records)   — per-hunk/file scheduling unit

Rules:
  - Harness creates structure; agent advances state.
  - Decision records are append-only (never overwritten).
  - State transitions follow a strict forward-only machine.
  - No generic mark_commit_progress(status=...) that bypasses the machine.
"""

import json
import subprocess
from datetime import UTC, datetime

# ── State machine ──────────────────────────────────────────────────────────

# Canonical status sets — single source of truth
# Work item statuses that are considered "done" (no further agent action needed)
TERMINAL_WORK_ITEM_STATUSES = frozenset({
    "ported", "skipped", "blocked", "needs_human",
    "validation_failed", "final_manual",
})

# Commit statuses that the harness main loop should skip (never re-process)
HARNESS_SKIP_STATUSES = frozenset({
    "ported", "skipped", "needs_human", "blocked",
    "validation_failed", "final_manual",
})

VALID_TRANSITIONS = {
    "pending": {
        "in_progress",
    },
    "in_progress": {
        "ported",
        "partially_ported",
        "skipped",
        "needs_human",
        "blocked",
        "validation_failed",
        "final_manual",
    },
    "partially_ported": {
        "ported",
        "needs_human",
    },
    "validation_failed": {
        "needs_human",
    },
    # needs_human must go through reset_for_retry → pending → in_progress
    # Direct needs_human → in_progress is NOT allowed
    # (harness gatekeeps with reset_for_retry)
    "final_manual": set(),
    # Terminal states: ported, skipped, blocked, final_manual — no outgoing
}

ALLOWED_COMPLETION_STATUSES = {
    "ported", "skipped", "needs_human", "blocked",
    "validation_failed", "final_manual",
}


def _transition_allowed(from_status, to_status):
    allowed = VALID_TRANSITIONS.get(from_status, set())
    return to_status in allowed


# ── Session-level ──────────────────────────────────────────────────────────

def init_session_meta(
    upstream_name,
    upstream_old,
    upstream_new,
    local_name,
    local_branch,
    arch,
    upstream_path,
    local_path,
    build_cmd,
    fast_test_cmds,
    slow_test_cmds,
):
    return {
        "upstream_name": upstream_name,
        "upstream_old": upstream_old,
        "upstream_new": upstream_new,
        "local_name": local_name,
        "local_branch": local_branch,
        "arch": arch,
        "upstream_path": str(upstream_path),
        "local_path": str(local_path),
        "build_cmd": build_cmd,
        "fast_test_cmds": fast_test_cmds,
        "slow_test_cmds": slow_test_cmds,
        "started_at": datetime.now(UTC).isoformat(),
        "updated_at": datetime.now(UTC).isoformat(),
    }


def init_ledger(session_meta, output_dir):
    from pathlib import Path

    ledger = {"meta": session_meta, "commits": {}}
    path = Path(output_dir) / "ledger.json"
    write_ledger(path, ledger)
    return ledger, path


def load_ledger(path):
    with open(path) as f:
        return json.load(f)


def write_ledger(path, ledger):
    ledger["meta"]["updated_at"] = datetime.now(UTC).isoformat()
    with open(path, "w") as f:
        json.dump(ledger, f, indent=2, ensure_ascii=False)


# ── Commit Entry ───────────────────────────────────────────────────────────

def init_commit_entry(ledger, commit_sha, upstream_subject, upstream_files):
    """Harness: create a commit skeleton with empty work items.

    Work items are NOT created here — call init_work_items separately after
    slicing determines granularity.
    """
    ledger["commits"][commit_sha] = {
        "commit_sha": commit_sha,
        "upstream_subject": upstream_subject,
        "intent_summary": None,
        "status": "pending",
        "upstream_files": upstream_files,
        "local_files_modified": [],
        "work_items": [],
        "validation": {},
    }
    return ledger["commits"][commit_sha]


def record_intent_summary(ledger, commit_sha, intent_summary):
    """Agent: record upstream intent BEFORE any edits."""
    entry = ledger["commits"].get(commit_sha)
    if entry is None:
        raise KeyError(f"commit {commit_sha[:8]} not initialized")
    entry["intent_summary"] = intent_summary


def _commit_has_work_items(entry):
    """Check if any work items are not in terminal state."""
    for wi in entry.get("work_items", []):
        if wi["status"] not in TERMINAL_WORK_ITEM_STATUSES:
            return False
    return bool(entry.get("work_items"))


# ── Work Items ─────────────────────────────────────────────────────────────

def init_work_items(ledger, commit_sha, items):
    """Harness: populate work items for a commit.

    items: list of dicts with {id, kind, upstream_file, local_file}
    All created as status='pending'.
    """
    entry = ledger["commits"].get(commit_sha)
    if entry is None:
        raise KeyError(f"commit {commit_sha[:8]} not initialized")
    entry["work_items"] = [
        {
            "id": item["id"],
            "kind": item["kind"],
            "upstream_file": item["upstream_file"],
            "local_file": item.get("local_file"),
            "status": "pending",
            "method": None,
            "attempt_count": 0,
            "decisions": [],
        }
        for item in items
    ]


def start_work_item(ledger, commit_sha, work_item_id):
    """Agent/harness: mark a work item as in_progress."""
    wi = _get_work_item(ledger, commit_sha, work_item_id)
    if not _transition_allowed(wi["status"], "in_progress"):
        raise ValueError(
            f"cannot transition {wi['id']} from {wi['status']} to in_progress"
        )
    wi["status"] = "in_progress"
    wi["attempt_count"] += 1
    _derive_commit_status(ledger["commits"][commit_sha])
    return wi


def append_decision(ledger, commit_sha, work_item_id, confidence, reason, evidence=None):
    """Agent: append a decision record (append-only, never overwrite)."""
    wi = _get_work_item(ledger, commit_sha, work_item_id)
    decision = {
        "timestamp": datetime.now(UTC).isoformat(),
        "attempt": wi["attempt_count"],
        "confidence": confidence,
        "reason": reason,
        "evidence": evidence or [],
    }
    wi["decisions"].append(decision)
    return decision


def complete_work_item(
    ledger, commit_sha, work_item_id, status, method=None, local_file=None
):
    """Agent: complete a work item with a terminal or semi-terminal status.

    Valid statuses: ported, skipped, needs_human, blocked, validation_failed
    """
    if status not in ALLOWED_COMPLETION_STATUSES:
        raise ValueError(
            f"invalid completion status '{status}'; allowed: {ALLOWED_COMPLETION_STATUSES}"
        )

    wi = _get_work_item(ledger, commit_sha, work_item_id)
    if not _transition_allowed(wi["status"], status):
        raise ValueError(
            f"cannot transition {wi['id']} from {wi['status']} to {status}"
        )

    wi["status"] = status
    if method:
        wi["method"] = method
    if local_file and local_file != wi.get("local_file"):
        wi["local_file"] = local_file

    # Track modified files
    if status == "ported":
        entry = ledger["commits"][commit_sha]
        lf = wi.get("local_file") or wi.get("upstream_file")
        if lf and lf not in entry["local_files_modified"]:
            entry["local_files_modified"].append(lf)

    _derive_commit_status(ledger["commits"][commit_sha])
    return wi


def create_work_item(ledger, commit_sha, kind, upstream_file, description, local_file=None):
    """Agent: create a synthetic work item (the ONLY kind agent can create).

    Synthetic items represent local-only adaptations that have no corresponding
    upstream hunk (e.g. compatibility shims when upstream removes an API).
    """
    entry = ledger["commits"].get(commit_sha)
    if entry is None:
        raise KeyError(f"commit {commit_sha[:8]} not initialized")

    idx = len(entry["work_items"])
    wi_id = f"{commit_sha[:8]}:synthetic:{idx}"
    wi = {
        "id": wi_id,
        "kind": "synthetic",
        "upstream_file": upstream_file,
        "local_file": local_file,
        "status": "pending",
        "method": None,
        "attempt_count": 0,
        "decisions": [],
        "description": description,
    }
    entry["work_items"].append(wi)
    return wi


# ── Validation ─────────────────────────────────────────────────────────────

def record_validation(ledger, commit_sha, validation_type, result):
    """Harness: record fast or slow validation results for a commit."""
    entry = ledger["commits"].get(commit_sha)
    if entry is None:
        raise KeyError(f"commit {commit_sha[:8]} not initialized")
    entry["validation"][validation_type] = result


# ── Derived status ─────────────────────────────────────────────────────────

def _derive_commit_status(entry):
    """Compute commit status from work item states."""
    wi_statuses = {wi["status"] for wi in entry.get("work_items", [])}

    if not wi_statuses or wi_statuses == {"pending"}:
        entry["status"] = "pending"
    elif all(s in ("ported", "skipped", "blocked") for s in wi_statuses):
        if all(s == "ported" for s in wi_statuses):
            entry["status"] = "ported"
        elif all(s == "skipped" for s in wi_statuses):
            entry["status"] = "skipped"
        elif any(s == "blocked" for s in wi_statuses):
            entry["status"] = "blocked"
        else:
            entry["status"] = "partially_ported"  # some ported, rest skipped
    elif any(s == "final_manual" for s in wi_statuses):
        entry["status"] = "final_manual"
    elif any(s == "validation_failed" for s in wi_statuses):
        entry["status"] = "validation_failed"
    elif any(s == "needs_human" for s in wi_statuses):
        entry["status"] = "needs_human"
    elif any(s == "blocked" for s in wi_statuses):
        entry["status"] = "blocked"
    elif any(s == "in_progress" for s in wi_statuses):
        entry["status"] = "in_progress"
    else:
        entry["status"] = "partially_ported"

    return entry["status"]


def reset_for_retry(ledger, commit_sha):
    """Harness: reset needs_human work items for hint-gated retry.

    This is the ONLY code path that moves items out of needs_human.
    Sets them back to pending so the agent can call start_work_item()
    and increment attempt_count.
    """
    entry = ledger["commits"].get(commit_sha)
    if entry is None:
        raise KeyError(f"commit {commit_sha[:8]} not found")
    for wi in entry.get("work_items", []):
        if wi["status"] == "needs_human":
            wi["status"] = "pending"
    entry["status"] = "pending"


# ── Git verification ───────────────────────────────────────────────────────

def git_verify(local_repo, commit_entry):
    """Check that git HEAD working tree has changes for claimed modified files."""
    modified = commit_entry.get("local_files_modified", [])
    if not modified:
        return True, "no files claimed"

    try:
        result = subprocess.run(
            ["git", "diff", "HEAD", "--name-only"],
            capture_output=True,
            text=True,
            cwd=local_repo,
            timeout=10,
        )
        changed = set(result.stdout.strip().split("\n"))
        changed.discard("")
        claimed = set(modified)
        overlap = claimed & changed
        if not overlap:
            return False, f"none of {claimed} appear in git diff HEAD"
        missing = claimed - changed
        if missing:
            return (
                True,
                f"partial match: {overlap} found, {missing} missing from git diff",
            )
        return True, f"all {len(overlap)} files confirmed in git diff"
    except Exception as e:
        return False, f"git diff failed: {e}"


# ── Snapshot for restart ───────────────────────────────────────────────────

def commit_snapshot(ledger):
    """Commit-level summary for restart context — compact enough for prompt."""
    result = {}
    for sha, entry in ledger["commits"].items():
        result[sha] = {
            "status": entry["status"],
            "intent_summary": entry.get("intent_summary"),
            "upstream_subject": entry.get("upstream_subject"),
            "local_files_modified": entry.get("local_files_modified", []),
        }
    return result


def ledger_for_prompt(ledger):
    """Compact ledger summary for system prompt injection."""
    lines = []
    for sha, entry in sorted(ledger["commits"].items()):
        short = sha[:8]
        subject = entry.get("upstream_subject", "")[:60]
        lines.append(
            f"  {short}: {entry['status']} | {subject}"
        )
    return "\n".join(lines) if lines else "  (no entries yet)"


# ── Helpers ────────────────────────────────────────────────────────────────

def _get_work_item(ledger, commit_sha, work_item_id):
    entry = ledger["commits"].get(commit_sha)
    if entry is None:
        raise KeyError(f"commit {commit_sha[:8]} not found in ledger")
    for wi in entry.get("work_items", []):
        if wi["id"] == work_item_id:
            return wi
    raise KeyError(f"work item {work_item_id} not found in commit {commit_sha[:8]}")


def iter_pending_work_items(ledger, commit_sha):
    """Yield work items that are pending or in_progress."""
    entry = ledger["commits"].get(commit_sha, {})
    for wi in entry.get("work_items", []):
        if wi["status"] in ("pending", "in_progress", "partially_ported"):
            yield wi


def iter_manual_required(ledger):
    """Yield (commit_sha, work_item) for items needing human attention."""
    for sha, entry in ledger["commits"].items():
        for wi in entry.get("work_items", []):
            if wi["status"] == "needs_human":
                yield sha, wi
