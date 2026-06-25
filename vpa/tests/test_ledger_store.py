from pathlib import Path

from vpa.ledger.store import LedgerStore
from vpa.orchestrator.models import CommitClass, CommitInfo, GateDecisionKind, LedgerRecord


def test_ledger_store_appends_jsonl_records(tmp_path):
    path = tmp_path / "ledger.jsonl"
    store = LedgerStore(path)
    record = LedgerRecord(
        commit=CommitInfo("a" * 40, "subject"),
        classification=CommitClass.REFERENCE_ISA_CHANGE,
        gate=GateDecisionKind.NEEDS_SEMANTIC_PORT,
        changed_files=[Path("src/dynarec/rv64/foo.c")],
        apply_status="committed",
        integrity_status="passed",
    )

    store.append(record)
    store.append({"classification": "unknown", "note": "test"})

    entries = store.read_all()
    assert len(entries) == 2
    assert entries[0]["record"]["commit"]["subject"] == "subject"
    assert entries[0]["record"]["classification"] == "reference_isa_change"
    assert entries[0]["record"]["changed_files"] == ["src/dynarec/rv64/foo.c"]
    assert entries[0]["record"]["apply_status"] == "committed"
    assert entries[1]["record"]["classification"] == "unknown"
