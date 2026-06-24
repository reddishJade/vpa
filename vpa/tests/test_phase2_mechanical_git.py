import json
import subprocess
import sys
from pathlib import Path

from vpa.orchestrator.models import GitOperationStatus, PromotionMethod, ValidationStatus
from vpa.orchestrator.promotion import PromotionConfig, PromotionOrchestrator


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _init_repo(repo: Path) -> None:
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "core.autocrlf", "false")
    _git(repo, "config", "user.name", "VPA Test")
    _git(repo, "config", "user.email", "vpa-test@example.invalid")


def _commit_file(repo: Path, path: str, content: str, message: str) -> str:
    full_path = repo / path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(content, encoding="utf-8")
    _git(repo, "add", path)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _clone(src: Path, dst: Path) -> None:
    subprocess.run(["git", "clone", str(src), str(dst)], check=True, capture_output=True)
    _git(dst, "config", "core.autocrlf", "false")
    _git(dst, "config", "user.name", "VPA Test")
    _git(dst, "config", "user.email", "vpa-test@example.invalid")


def _make_repos(tmp_path: Path) -> tuple[Path, Path, str]:
    seed = tmp_path / "seed"
    _init_repo(seed)
    base = _commit_file(seed, "src/core.c", "int value = 1;\n", "base")
    upstream = tmp_path / "upstream"
    local = tmp_path / "local"
    _clone(seed, upstream)
    _clone(seed, local)
    return upstream, local, base


def test_execute_cherry_picks_shared_commit_and_records_ledger(tmp_path):
    upstream, local, base = _make_repos(tmp_path)
    head = _commit_file(upstream, "src/core.c", "int value = 2;\n", "shared update")
    ledger = tmp_path / "ledger.jsonl"

    run = PromotionOrchestrator(
        PromotionConfig(
            upstream_repo=upstream,
            local_repo=local,
            revision_range=f"{base}..{head}",
            ledger_path=ledger,
        )
    ).execute()

    assert (local / "src/core.c").read_text(encoding="utf-8") == "int value = 2;\n"
    assert run.executed[0].method == PromotionMethod.CHERRY_PICK
    assert run.executed[0].git_result
    assert run.executed[0].git_result.status == GitOperationStatus.APPLIED
    assert run.executed[0].validation.status == ValidationStatus.NOT_RUN
    entry = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
    assert entry["record"]["method"] == "cherry_pick"
    assert entry["record"]["git"]["checkpoint"] == base


def test_execute_rolls_back_on_cherry_pick_conflict(tmp_path):
    upstream, local, base = _make_repos(tmp_path)
    head = _commit_file(upstream, "src/core.c", "int value = 2;\n", "upstream update")
    local_head = _commit_file(local, "src/core.c", "int value = 3;\n", "local update")

    run = PromotionOrchestrator(
        PromotionConfig(
            upstream_repo=upstream,
            local_repo=local,
            revision_range=f"{base}..{head}",
        )
    ).execute()

    assert _git(local, "rev-parse", "HEAD") == local_head
    assert (local / "src/core.c").read_text(encoding="utf-8") == "int value = 3;\n"
    assert run.executed[0].git_result
    assert run.executed[0].git_result.status == GitOperationStatus.ROLLED_BACK
    assert run.executed[0].manual_item


def test_execute_rolls_back_on_validation_failure(tmp_path):
    upstream, local, base = _make_repos(tmp_path)
    head = _commit_file(upstream, "src/core.c", "int value = 2;\n", "shared update")
    fail_command = f'"{sys.executable}" -c "import sys; sys.exit(1)"'

    run = PromotionOrchestrator(
        PromotionConfig(
            upstream_repo=upstream,
            local_repo=local,
            revision_range=f"{base}..{head}",
            smoke_commands=[fail_command],
        )
    ).execute()

    assert _git(local, "rev-parse", "HEAD") == base
    assert (local / "src/core.c").read_text(encoding="utf-8") == "int value = 1;\n"
    assert run.executed[0].git_result
    assert run.executed[0].git_result.status == GitOperationStatus.ROLLED_BACK
    assert run.executed[0].validation.status == ValidationStatus.FAILED


def test_execute_uses_path_limited_apply_for_target_direct_commit(tmp_path):
    seed = tmp_path / "seed"
    _init_repo(seed)
    base = _commit_file(
        seed,
        "src/dynarec/sw64_core3/foo.c",
        "int target = 1;\n",
        "base",
    )
    upstream = tmp_path / "upstream"
    local = tmp_path / "local"
    _clone(seed, upstream)
    _clone(seed, local)
    head = _commit_file(
        upstream,
        "src/dynarec/sw64_core3/foo.c",
        "int target = 2;\n",
        "target update",
    )

    run = PromotionOrchestrator(
        PromotionConfig(
            upstream_repo=upstream,
            local_repo=local,
            revision_range=f"{base}..{head}",
        )
    ).execute()

    assert (local / "src/dynarec/sw64_core3/foo.c").read_text(encoding="utf-8") == (
        "int target = 2;\n"
    )
    assert run.executed[0].method == PromotionMethod.PATH_LIMITED_APPLY_3WAY
    assert run.executed[0].git_result
    assert run.executed[0].git_result.status == GitOperationStatus.APPLIED
