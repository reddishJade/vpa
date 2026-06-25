import subprocess
from pathlib import Path

from vpa.engines.repair import RepairEngine, build_semantic_port_context
from vpa.orchestrator.models import (
    GitOperationStatus,
    PromotionMethod,
    SemanticPortContext,
    ValidationStatus,
)
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


def _make_semantic_repos(tmp_path: Path) -> tuple[Path, Path, str, str]:
    seed = tmp_path / "seed"
    _init_repo(seed)
    base = _commit_file(
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


class FakeSemanticClient:
    def __init__(self, patch_text: str):
        self.patch_text = patch_text
        self.context: SemanticPortContext | None = None

    def semantic_port(self, context: SemanticPortContext) -> str:
        self.context = context
        return self.patch_text


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


def test_execute_semantic_port_applies_injected_patch(tmp_path):
    upstream, local, base, head = _make_semantic_repos(tmp_path)
    patch_text = (
        "diff --git a/src/dynarec/sw64_core3/dynarec_sw64_00.c "
        "b/src/dynarec/sw64_core3/dynarec_sw64_00.c\n"
        "--- a/src/dynarec/sw64_core3/dynarec_sw64_00.c\n"
        "+++ b/src/dynarec/sw64_core3/dynarec_sw64_00.c\n"
        "@@ -1 +1 @@\n"
        "-int target(void) { return 1; }\n"
        "+int target(void) { return 2; }\n"
    )
    client = FakeSemanticClient(patch_text)

    run = PromotionOrchestrator(
        PromotionConfig(
            upstream_repo=upstream,
            local_repo=local,
            revision_range=f"{base}..{head}",
        ),
        repair_engine=RepairEngine(client),
    ).execute()

    assert (local / "src/dynarec/sw64_core3/dynarec_sw64_00.c").read_text(
        encoding="utf-8"
    ) == "int target(void) { return 2; }\n"
    assert run.executed[0].method == PromotionMethod.SEMANTIC_PORT
    assert run.executed[0].git_result
    assert run.executed[0].git_result.status == GitOperationStatus.APPLIED
    assert run.executed[0].validation.status == ValidationStatus.NOT_RUN
    assert client.context
    assert client.context.target_files[0].content == "int target(void) { return 1; }\n"


def test_execute_semantic_port_without_client_records_manual_item(tmp_path):
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
    assert run.executed[0].method == PromotionMethod.SEMANTIC_PORT_PENDING
    assert run.executed[0].git_result
    assert run.executed[0].git_result.status == GitOperationStatus.SKIPPED
    assert run.executed[0].manual_item
