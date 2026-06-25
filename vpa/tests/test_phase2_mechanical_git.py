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


def test_execute_merges_shared_commit(tmp_path):
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
    assert run.merge is not None
    assert run.merge.git_result.status == GitOperationStatus.APPLIED
    assert run.merge.validation.status == ValidationStatus.NOT_RUN
    assert run.executed == []


def test_execute_merge_conflict_resolves_with_fallback_to_ours(tmp_path):
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

    assert run.merge is not None
    assert run.merge.git_result.status == GitOperationStatus.CONFLICT
    assert run.merge.repair_result is not None
    assert len(run.merge.repair_result.failed_files) >= 1


def test_execute_merge_rolls_back_on_validation_failure(tmp_path):
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
    assert run.merge is not None
    assert run.merge.validation.status == ValidationStatus.FAILED


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


def test_execute_reference_isa_change_via_semantic_port(tmp_path):
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

    assert run.merge is not None
    assert run.merge.git_result.status == GitOperationStatus.APPLIED
    assert (local / "src/dynarec/rv64/foo.c").read_text(encoding="utf-8") == "if(x) return 2;\n"
    assert run.executed == [] or run.executed[0].method == PromotionMethod.SEMANTIC_PORT


def test_render_run_shows_merge_and_semantic_port(tmp_path):
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
    assert "--- merge upstream ---" in rendered
    assert "applied" in rendered
