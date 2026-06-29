from pathlib import Path

from vpa.engines.repair import build_semantic_port_context
from vpa.orchestrator.models import GitOperationStatus, PromotionMethod
from vpa.orchestrator.promotion import PromotionConfig, PromotionOrchestrator


def _git(repo, *args):
    import subprocess

    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _init_repo(repo):
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "core.autocrlf", "false")
    _git(repo, "config", "user.name", "VPA Test")
    _git(repo, "config", "user.email", "vpa-test@example.invalid")


def _commit_file(repo, path, content, message):
    full_path = repo / path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(content, encoding="utf-8")
    _git(repo, "add", path)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _clone(src, dst):
    import subprocess

    subprocess.run(["git", "clone", str(src), str(dst)], check=True, capture_output=True)
    _git(dst, "config", "core.autocrlf", "false")
    _git(dst, "config", "user.name", "VPA Test")
    _git(dst, "config", "user.email", "vpa-test@example.invalid")


def _make_semantic_repos(tmp_path):
    seed = tmp_path / "seed"
    _init_repo(seed)
    _commit_file(
        seed,
        "src/dynarec/rv64/dynarec_rv64_00.c",
        "int ref(void) { return 1; }\n",
        "reference base",
    )
    _commit_file(
        seed,
        "src/dynarec/sw64_core3/dynarec_sw64_00.c",
        "int target(void) { return 1; }\n",
        "target base",
    )
    base = _git(seed, "rev-parse", "HEAD")
    upstream = tmp_path / "upstream"
    local = tmp_path / "local"
    _clone(seed, upstream)
    _clone(seed, local)
    head = _commit_file(
        upstream,
        "src/dynarec/rv64/dynarec_rv64_00.c",
        "int ref(void) { return 2; }\n",
        "reference semantic update",
    )
    return upstream, local, base, head


def test_builds_compact_semantic_port_context(tmp_path):
    upstream, local, base, head = _make_semantic_repos(tmp_path)
    planned = PromotionOrchestrator(
        PromotionConfig(
            upstream_repo=upstream,
            local_repo=local,
            revision_range=f"{base}..{head}",
        )
    ).plan().commits[0]

    context = build_semantic_port_context(
        planned.context,
        planned.analysis,
        planned.gate_decision,
        local,
    )

    assert list(context.reference_patches) == [
        Path("src/dynarec/rv64/dynarec_rv64_00.c")
    ]
    assert context.target_files[0].path == Path(
        "src/dynarec/sw64_core3/dynarec_sw64_00.c"
    )
    assert context.target_files[0].content == "int target(void) { return 1; }\n"
    assert context.gate_reasons


def test_execute_isa_translate_without_client_skips(tmp_path):
    upstream, local, base, head = _make_semantic_repos(tmp_path)

    run = PromotionOrchestrator(
        PromotionConfig(
            upstream_repo=upstream,
            local_repo=local,
            revision_range=f"{base}..{head}",
        )
    ).execute()

    assert (local / "src/dynarec/sw64_core3/dynarec_sw64_00.c").read_text(
        encoding="utf-8"
    ) == "int target(void) { return 1; }\n"
    assert run.executed[0].method == PromotionMethod.CHERRY_PICK
    assert run.executed[0].git_result
    assert run.executed[0].git_result.status == GitOperationStatus.APPLIED
