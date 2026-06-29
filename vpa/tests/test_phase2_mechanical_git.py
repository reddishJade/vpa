import subprocess
import sys
from pathlib import Path

from vpa.orchestrator.models import (
    GitOperationStatus,
    PromotionMethod,
    ValidationStatus,
)
from vpa.orchestrator.promotion import (
    PromotionConfig,
    PromotionOrchestrator,
    render_run,
)


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
    _git(local, "remote", "add", "upstream", str(upstream))
    _git(local, "fetch", "upstream")
    return upstream, local, base


def test_execute_cherry_picks_shared_commit(tmp_path):
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
    assert len(run.executed) == 1
    item = run.executed[0]
    assert item.git_result is not None
    assert item.method == PromotionMethod.CHERRY_PICK
    assert item.git_result.status == GitOperationStatus.APPLIED
    assert item.validation.status == ValidationStatus.NOT_RUN

    commit_message = _git(local, "log", "-1", "--pretty=%B")
    assert "shared update" in commit_message
    assert f"(cherry picked from commit {head})" in commit_message


def test_execute_cherry_pick_conflict_rolls_back_atomic_commit(tmp_path):
    upstream, local, base = _make_repos(tmp_path)
    head = _commit_file(upstream, "src/core.c", "int value = 2;\n", "upstream update")
    _commit_file(local, "src/core.c", "int value = 3;\n", "local update")

    run = PromotionOrchestrator(
        PromotionConfig(
            upstream_repo=upstream,
            local_repo=local,
            revision_range=f"{base}..{head}",
        )
    ).execute()

    assert len(run.executed) == 1
    assert run.executed[0].git_result is not None
    assert run.executed[0].git_result.status == GitOperationStatus.ROLLED_BACK
    assert _git(local, "rev-parse", "HEAD") != head
    assert (local / "src/core.c").read_text(encoding="utf-8") == "int value = 3;\n"


def test_execute_isa_backend_conflict_records_pending_and_blocks_later_commits(tmp_path):
    upstream, local, _base = _make_repos(tmp_path)
    # Seed the rv64 file in upstream, then diverge in local.
    seed_sha = _commit_file(upstream, "src/dynarec/rv64/foo.c", "rv64 old;\n", "add rv64 foo")
    _commit_file(local, "src/dynarec/rv64/foo.c", "rv64 local;\n", "local rv64 foo")

    first = _commit_file(upstream, "src/dynarec/rv64/foo.c", "rv64 upstream v2;\n", "rv64 v2")
    second = _commit_file(upstream, "src/dynarec/rv64/foo.c", "rv64 upstream v3;\n", "rv64 v3")
    ledger = tmp_path / "ledger.jsonl"

    run = PromotionOrchestrator(
        PromotionConfig(
            upstream_repo=upstream,
            local_repo=local,
            revision_range=f"{seed_sha}..{second}",
            ledger_path=ledger,
        )
    ).execute()

    assert len(run.executed) == 2
    first_item, second_item = run.executed
    assert first_item.git_result is not None
    assert first_item.git_result.status == GitOperationStatus.ROLLED_BACK
    assert first_item.planned.context.commit.sha == first
    assert second_item.git_result is not None
    assert second_item.git_result.status == GitOperationStatus.SKIPPED
    assert (local / "src/dynarec/rv64/foo.c").read_text(encoding="utf-8") == "rv64 local;\n"

    ledger_text = ledger.read_text(encoding="utf-8")
    assert "pending_human_review" in ledger_text
    assert "src/dynarec/rv64/foo.c" in ledger_text


def test_execute_cherry_pick_rolls_back_on_validation_failure(tmp_path):
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
    assert len(run.executed) == 1
    assert run.executed[0].git_result is not None
    assert run.executed[0].validation.status == ValidationStatus.FAILED
    assert run.executed[0].git_result.status == GitOperationStatus.ROLLED_BACK


def test_shared_file_conflict_with_rv64_conditional_upgraded_to_isa_backend(tmp_path):
    upstream, local, base = _make_repos(tmp_path)
    # seed a shared file with an RV64 conditional block
    local_file = local / "src/core.c"
    local_file.write_text(
        "#if defined(RV64)\nint rv = 1;\n#endif\nint other = 2;\n", encoding="utf-8"
    )
    _git(local, "add", "src/core.c")
    _git(local, "commit", "-m", "local with rv64 block")

    # upstream modifies the file in a way that conflicts inside the conditional
    head = _commit_file(
        upstream,
        "src/core.c",
        "#if defined(RV64)\nint rv = 99;\n#endif\nint other = 2;\n",
        "upstream change inside rv64 block",
    )
    ledger = tmp_path / "ledger.jsonl"

    run = PromotionOrchestrator(
        PromotionConfig(
            upstream_repo=upstream,
            local_repo=local,
            revision_range=f"{base}..{head}",
            ledger_path=ledger,
        )
    ).execute()

    assert len(run.executed) == 1
    assert run.executed[0].git_result is not None
    assert run.executed[0].git_result.status == GitOperationStatus.ROLLED_BACK

    ledger_text = ledger.read_text(encoding="utf-8")
    assert "pending_human_review" in ledger_text
    assert "src/core.c" in ledger_text


def test_execute_refuses_dirty_tracked_local_repo(tmp_path):
    upstream, local, base = _make_repos(tmp_path)
    head = _commit_file(upstream, "src/core.c", "int value = 2;\n", "shared update")
    (local / "src/core.c").write_text("int value = 99;\n", encoding="utf-8")

    try:
        PromotionOrchestrator(
            PromotionConfig(
                upstream_repo=upstream,
                local_repo=local,
                revision_range=f"{base}..{head}",
            )
        ).execute()
    except ValueError as error:
        assert "tracked uncommitted changes" in str(error)
        assert "src/core.c" in str(error)
    else:
        raise AssertionError("execute should refuse a dirty tracked local repo")

    assert (local / "src/core.c").read_text(encoding="utf-8") == "int value = 99;\n"


def test_execute_reference_isa_change_rolls_back_without_llm(tmp_path):
    upstream, local, base = _make_repos(tmp_path)
    head = _commit_file(
        upstream,
        "src/dynarec/rv64/foo.c",
        "if(x) return 2;\n",
        "rv64 logic change",
    )

    run = PromotionOrchestrator(
        PromotionConfig(
            upstream_repo=upstream,
            local_repo=local,
            revision_range=f"{base}..{head}",
        )
    ).execute()

    assert len(run.executed) == 1
    assert run.executed[0].git_result is not None
    assert run.executed[0].method == PromotionMethod.SEMANTIC_PORT
    assert run.executed[0].git_result.status in {
        GitOperationStatus.SKIPPED,
        GitOperationStatus.ROLLED_BACK,
    }
    assert not (local / "src/dynarec/rv64/foo.c").exists()


def test_render_run_shows_cherry_pick_execution(tmp_path):
    upstream, local, base = _make_repos(tmp_path)
    head = _commit_file(upstream, "src/core.c", "int value = 2;\n", "shared update")

    run = PromotionOrchestrator(
        PromotionConfig(
            upstream_repo=upstream,
            local_repo=local,
            revision_range=f"{base}..{head}",
        )
    ).execute()

    rendered = render_run(run)
    assert "--- merge upstream ---" not in rendered
    assert "cherry_pick" in rendered
    assert "committed" in rendered
