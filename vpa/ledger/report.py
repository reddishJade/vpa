"""Human-readable reporting from ledger and planned commits."""

from __future__ import annotations

import json
from dataclasses import asdict
from enum import Enum
from pathlib import Path
from typing import Any

from vpa.orchestrator.promotion import PromotionPlan, render_plan


def write_reports(plan: PromotionPlan, markdown_path: Path, json_path: Path) -> None:
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(render_plan(plan) + "\n", encoding="utf-8")
    json_path.write_text(
        json.dumps(_to_jsonable(asdict(plan)), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def render_ledger(ledger_path: str | Path) -> str:
    """Read a ledger JSONL file and return a human-readable summary."""
    records: list[dict[str, Any]] = []
    with open(ledger_path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                records.append(json.loads(line))

    commit_records: list[dict] = []
    pending: list[dict] = []
    for entry in records:
        r = entry.get("record", {})
        if "commit" in r:
            commit_records.append(r)
        elif r.get("status") == "pending_human_review" and "commit_sha" in r:
            pending.append(r)

    total = len(commit_records)
    committed = [r for r in commit_records if r.get("apply_status") == "committed"]
    rolled_back = [r for r in commit_records if r.get("apply_status") == "rolled_back"]

    pending_isa = [p for p in pending if p.get("category") == "isa_backend"]
    pending_source = [p for p in pending if p.get("category") == "source"]

    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("VPA Ledger Report")
    lines.append("=" * 72)
    lines.append("")
    lines.append(f"Total commits in ledger: {total}")
    lines.append(f"  committed:   {len(committed)}")
    lines.append(f"  rolled_back: {len(rolled_back)}")
    lines.append(f"  skipped:     {total - len(committed) - len(rolled_back)}")
    lines.append("")
    lines.append(f"Pending human-review files: {len(pending)}")
    lines.append(f"  ISA_BACKEND: {len(pending_isa)}")
    lines.append(f"  SOURCE:      {len(pending_source)}")
    lines.append("")
    lines.append("-" * 72)

    if pending:
        lines.append("")
        lines.append("=" * 72)
        lines.append("FILES REQUIRING HUMAN REVIEW")
        lines.append("=" * 72)
        lines.append("")
        for p in pending:
            cat = p.get("category", "?").upper()
            fpath = p.get("file_path", "?")
            csha = (p.get("commit_sha") or "?")[:12]
            csub = (p.get("commit_subject") or "?")[:65]
            lines.append(f"  [{cat}] {fpath}")
            lines.append(f"          commit {csha}  {csub}")
            lines.append("")

    if committed:
        lines.append("=" * 72)
        lines.append(f"COMMITTED ({len(committed)}/{total})")
        lines.append("=" * 72)
        lines.append("")
        for r in committed[-5:]:
            c = r.get("commit", {})
            sha = c.get("sha", "?")[:12]
            subj = c.get("subject", "?")[:65]
            cl = r.get("classification", "?")
            lines.append(f"  {sha}  [{cl}] {subj}")
        if len(committed) > 5:
            lines.append(f"  ... ({len(committed) - 5} more)")
        lines.append("")

    if rolled_back:
        lines.append("=" * 72)
        lines.append(f"ROLLED BACK ({len(rolled_back)}/{total})")
        lines.append("=" * 72)
        lines.append("")
        for r in rolled_back[-5:]:
            c = r.get("commit", {})
            sha = c.get("sha", "?")[:12]
            subj = c.get("subject", "?")[:65]
            cl = r.get("classification", "?")
            lines.append(f"  {sha}  [{cl}] {subj}")
        if len(rolled_back) > 5:
            lines.append(f"  ... ({len(rolled_back) - 5} more)")
        lines.append("")

    return "\n".join(lines)


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_to_jsonable(item) for item in value]
    return value

