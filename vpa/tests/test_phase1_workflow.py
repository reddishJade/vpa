from pathlib import Path
from unittest.mock import patch

from vpa.analysis.change_analyzer import analyze
from vpa.analysis.classifier import classify_commit
from vpa.analysis.isa_mapper import map_reference_files
from vpa.engines.git import parse_diff_context
from vpa.main import main
from vpa.orchestrator.llm_gate import decide
from vpa.orchestrator.models import (
    ChangeKind,
    CommitClass,
    CommitContext,
    CommitInfo,
    GateDecisionKind,
    GatePolicy,
    MappingStatus,
)


def _diff_context(raw_patch: str):
    return parse_diff_context(CommitInfo("a" * 40, "test commit"), raw_patch)


def test_parse_raw_patch_with_hunks():
    raw_patch = """diff --git a/src/dynarec/rv64/foo.c b/src/dynarec/rv64/foo.c
index 1111111..2222222 100644
--- a/src/dynarec/rv64/foo.c
+++ b/src/dynarec/rv64/foo.c
@@ -1,3 +1,3 @@ test
 int a = 1;
-int b = 2;
+int b = 3;
 int c = 4;
"""
    context = _diff_context(raw_patch)

    assert context.raw_patch == raw_patch
    assert len(context.files) == 1
    assert context.files[0].path_after == Path("src/dynarec/rv64/foo.c")
    assert context.files[0].hunks[0].section == "test"
    assert context.files[0].hunks[0].lines[1].text == "int b = 2;"


def test_classify_reference_isa_change():
    raw_patch = """diff --git a/src/dynarec/rv64/foo.c b/src/dynarec/rv64/foo.c
--- a/src/dynarec/rv64/foo.c
+++ b/src/dynarec/rv64/foo.c
@@ -1 +1 @@
-int b = 2;
+int b = 3;
"""
    classified = classify_commit(_diff_context(raw_patch))

    assert classified.kind == CommitClass.REFERENCE_ISA_CHANGE
    assert classified.file_classes[Path("src/dynarec/rv64/foo.c")] == (
        CommitClass.REFERENCE_ISA_CHANGE
    )


def test_path_only_isa_mapping_without_repo_check():
    raw_patch = (
        "diff --git a/src/dynarec/rv64/dynarec_rv64_00.c "
        "b/src/dynarec/rv64/dynarec_rv64_00.c\n"
        """--- a/src/dynarec/rv64/dynarec_rv64_00.c
+++ b/src/dynarec/rv64/dynarec_rv64_00.c
@@ -1 +1 @@
-int b = 2;
+int b = 3;
"""
    )
    mapping = map_reference_files(_diff_context(raw_patch))

    assert mapping.file_mappings[0].status == MappingStatus.MAPPED
    assert mapping.file_mappings[0].target_candidates == [
        Path("src/dynarec/sw64_core3/dynarec_sw64_00.c")
    ]


def test_missing_target_mapping_when_repo_is_checked(tmp_path):
    raw_patch = """diff --git a/src/dynarec/rv64/rv64_helper.c b/src/dynarec/rv64/rv64_helper.c
--- a/src/dynarec/rv64/rv64_helper.c
+++ b/src/dynarec/rv64/rv64_helper.c
@@ -1 +1 @@
-int b = 2;
+int b = 3;
"""
    mapping = map_reference_files(_diff_context(raw_patch), local_repo=tmp_path)

    assert mapping.file_mappings[0].status == MappingStatus.MISSING_TARGET
    assert mapping.file_mappings[0].target_candidates == [
        Path("src/dynarec/sw64_core3/sw64_helper.c")
    ]


def test_comment_only_analysis_routes_to_no_target_change():
    raw_patch = """diff --git a/src/dynarec/rv64/foo.c b/src/dynarec/rv64/foo.c
--- a/src/dynarec/rv64/foo.c
+++ b/src/dynarec/rv64/foo.c
@@ -1 +1 @@
-// old comment
+// new comment
"""
    context = _diff_context(raw_patch)
    analysis = analyze(context, map_reference_files(context))

    assert analysis.kind in {ChangeKind.COMMENT_ONLY, ChangeKind.FORMAT_ONLY}
    assert analysis.suggested_gate == GateDecisionKind.NO_TARGET_CHANGE
    assert analysis.signals[0].source


def test_semantic_analysis_routes_mapped_reference_to_semantic_port():
    raw_patch = """diff --git a/src/dynarec/rv64/foo.c b/src/dynarec/rv64/foo.c
--- a/src/dynarec/rv64/foo.c
+++ b/src/dynarec/rv64/foo.c
@@ -1 +1 @@
-if(x) return 1;
+if(x) return 2;
"""
    diff_context = _diff_context(raw_patch)
    classification = classify_commit(diff_context)
    mapping = map_reference_files(diff_context)
    context = CommitContext(diff_context.commit, diff_context, classification, mapping)
    analysis = analyze(diff_context, mapping)
    gate = decide(analysis, GatePolicy(), context)

    assert analysis.kind == ChangeKind.LOGIC_CHANGE
    assert gate.kind == GateDecisionKind.NEEDS_SEMANTIC_PORT


def test_gate_routes_shared_change_to_validation_only():
    raw_patch = """diff --git a/src/core.c b/src/core.c
--- a/src/core.c
+++ b/src/core.c
@@ -1 +1 @@
-if(x) return 1;
+if(x) return 2;
"""
    diff_context = _diff_context(raw_patch)
    classification = classify_commit(diff_context)
    mapping = map_reference_files(diff_context)
    context = CommitContext(diff_context.commit, diff_context, classification, mapping)
    gate = decide(analyze(diff_context, mapping), GatePolicy(), context)

    assert classification.kind == CommitClass.SHARED_CODE
    assert gate.kind == GateDecisionKind.NEEDS_VALIDATION_ONLY


def test_promote_cli_builds_config_and_renders_plan(capsys, tmp_path):
    config_path = tmp_path / "vpa.toml"
    config_path.write_text(
        """
[promotion]
upstream_repo = "configured-upstream"
local_repo = "configured-local"

[validation]
smoke_commands = ["make configured-test"]
""",
        encoding="utf-8",
    )
    with patch("vpa.main.PromotionOrchestrator") as orchestrator_cls:
        orchestrator = orchestrator_cls.return_value
        orchestrator.plan.return_value.commits = []
        main(
            [
                "promote",
                "--config",
                str(config_path),
                "--rev-range",
                "old..new",
                "--dry-run",
                "--smoke-test",
                "make test",
            ]
        )

    config = orchestrator_cls.call_args.args[0]
    assert config.upstream_repo == Path("configured-upstream")
    assert config.local_repo == Path("configured-local")
    assert config.revision_range == "old..new"
    assert config.dry_run is True
    assert config.smoke_commands == ["make test"]
    assert "VPA promotion plan" in capsys.readouterr().out


def test_promote_cli_execute_runs_mechanical_workflow(capsys):
    with patch("vpa.main.PromotionOrchestrator") as orchestrator_cls:
        orchestrator = orchestrator_cls.return_value
        orchestrator.execute.return_value.plan.commits = []
        orchestrator.execute.return_value.executed = []
        main(
            [
                "promote",
                "--upstream-repo",
                "upstream",
                "--local-repo",
                "local",
                "--rev-range",
                "old..new",
                "--execute",
            ]
        )

    orchestrator.execute.assert_called_once_with()
    orchestrator.plan.assert_not_called()
    assert "VPA promotion execution" in capsys.readouterr().out


def test_promote_cli_builds_openai_compatible_repair_engine(capsys, tmp_path):
    config_path = tmp_path / "vpa.toml"
    config_path.write_text(
        """
[promotion]
upstream_repo = "upstream"
local_repo = "local"

[llm]
model = "port-model"
base_url = "https://llm.example/v1"
api_key_env = "VPA_TEST_API_KEY"
""",
        encoding="utf-8",
    )
    with (
        patch("vpa.main.PromotionOrchestrator") as orchestrator_cls,
        patch.dict("os.environ", {"VPA_TEST_API_KEY": "secret"}),
    ):
        orchestrator = orchestrator_cls.return_value
        orchestrator.execute.return_value.plan.commits = []
        orchestrator.execute.return_value.executed = []
        main(
            [
                "promote",
                "--config",
                str(config_path),
                "--rev-range",
                "old..new",
                "--execute",
            ]
        )

    repair_engine = orchestrator_cls.call_args.kwargs["repair_engine"]
    client = repair_engine.llm_client
    assert client.config.model == "port-model"
    assert client.config.base_url == "https://llm.example/v1"
    assert client.config.api_key == "secret"
    assert "VPA promotion execution" in capsys.readouterr().out
