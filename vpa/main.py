import argparse
import os
import sys
from pathlib import Path

from .config import DEFAULT_CONFIG_PATH, load_settings
from .engines.repair import (
    OpenAICompatibleConfig,
    OpenAICompatibleSemanticPortClient,
    RepairEngine,
)
from .orchestrator.models import GatePolicy, RiskPreference
from .orchestrator.promotion import (
    PromotionConfig,
    PromotionOrchestrator,
    render_plan,
    render_run,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="vpa - architecture-port promotion workflow")
    sub = parser.add_subparsers(dest="command")

    promote_p = sub.add_parser("promote", help="Plan the architecture-port workflow")
    promote_p.add_argument(
        "--config",
        default=None,
        help=f"Path to TOML config file; defaults to .\\{DEFAULT_CONFIG_PATH}",
    )
    promote_p.add_argument("--upstream-repo", default=None, help="Override upstream Git repo")
    promote_p.add_argument("--local-repo", default=None, help="Override local target repo")
    promote_p.add_argument("--rev-range", default=None, help="Git revision range to promote")
    promote_p.add_argument(
        "--target-isa-path",
        default=None,
        help="Override target ISA path in the local repo",
    )
    promote_p.add_argument(
        "--reference-isa-path",
        default=None,
        help="Override primary reference ISA path in upstream",
    )
    promote_p.add_argument(
        "--fallback-reference-isa-path",
        action="append",
        default=None,
        help="Override fallback reference ISA path (repeatable)",
    )
    promote_p.add_argument("--build-cmd", default=None, help="Override configured build command")
    promote_p.add_argument(
        "--smoke-test",
        action="append",
        default=None,
        help="Override configured smoke/test command (repeatable)",
    )
    promote_p.add_argument("--ledger-path", default=None, help="Override ledger artifact path")
    promote_p.add_argument("--report-path", default=None, help="Override report artifact path")
    promote_p.add_argument("--dry-run", action="store_true", help="Plan without mutating repos")
    promote_p.add_argument(
        "--execute",
        action="store_true",
        help="Run the mechanical Git path for eligible commits",
    )
    promote_p.add_argument(
        "--semantic-confidence-threshold",
        type=float,
        default=None,
        help="Override minimum analyzer confidence before semantic porting",
    )
    promote_p.add_argument(
        "--manual-confidence-threshold",
        type=float,
        default=None,
        help="Override confidence below which manual review is preferred",
    )
    promote_p.add_argument(
        "--risk-preference",
        choices=[item.value for item in RiskPreference],
        default=None,
        help="Override gate risk preference",
    )
    promote_p.add_argument(
        "--llm-temperature",
        type=float,
        default=None,
        help="Override LLM temperature for semantic porting",
    )
    promote_p.add_argument(
        "--llm-max-context-chars",
        type=int,
        default=None,
        help="Override maximum semantic-port prompt size before truncation",
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "promote":
        try:
            settings = load_settings(Path(args.config) if args.config else None)
        except ValueError as error:
            parser.error(str(error))

        upstream_repo = _optional_path(args.upstream_repo, settings.upstream_repo)
        if upstream_repo is None:
            parser.error("upstream_repo is required in vpa.toml or --upstream-repo")
        local_repo = _optional_path(args.local_repo, settings.local_repo)
        if local_repo is None:
            parser.error("local_repo is required in vpa.toml or --local-repo")
        revision_range = args.rev_range
        if not revision_range:
            parser.error("revision range is required: pass --rev-range")

        risk_preference = args.risk_preference or settings.risk_preference
        policy = GatePolicy(
            semantic_confidence_threshold=(
                args.semantic_confidence_threshold
                if args.semantic_confidence_threshold is not None
                else settings.semantic_confidence_threshold
            ),
            manual_confidence_threshold=(
                args.manual_confidence_threshold
                if args.manual_confidence_threshold is not None
                else settings.manual_confidence_threshold
            ),
            risk_preference=RiskPreference(risk_preference),
            dry_run=args.dry_run,
        )
        config = PromotionConfig(
            upstream_repo=upstream_repo,
            local_repo=local_repo,
            revision_range=revision_range,
            target_isa_path=(
                Path(args.target_isa_path) if args.target_isa_path else settings.target_isa_path
            ),
            primary_reference_isa_path=(
                Path(args.reference_isa_path)
                if args.reference_isa_path
                else settings.reference_isa_path
            ),
            fallback_reference_isa_paths=[Path(path) for path in args.fallback_reference_isa_path]
            if args.fallback_reference_isa_path is not None
            else settings.fallback_reference_isa_paths,
            build_command=args.build_cmd if args.build_cmd is not None else settings.build_command,
            smoke_commands=(
                args.smoke_test if args.smoke_test is not None else settings.smoke_commands
            ),
            dry_run=args.dry_run,
            ledger_path=Path(args.ledger_path) if args.ledger_path else settings.ledger_path,
            report_path=Path(args.report_path) if args.report_path else settings.report_path,
            gate_policy=policy,
        )
        repair_engine = _build_repair_engine(args, settings)
        try:
            orchestrator = PromotionOrchestrator(config, repair_engine=repair_engine)
        except ValueError as error:
            parser.error(str(error))
        if args.execute and not args.dry_run:
            try:
                run = orchestrator.execute()
            except ValueError as error:
                parser.error(str(error))
            print(render_run(run))
        else:
            try:
                plan = orchestrator.plan()
            except ValueError as error:
                parser.error(str(error))
            print(render_plan(plan))
        return

    parser.print_help()
    sys.exit(1)


def _optional_path(cli_value: str | None, config_value: Path | None) -> Path | None:
    value = Path(cli_value) if cli_value else config_value
    return value


def _build_repair_engine(args, settings) -> RepairEngine:
    if not settings.llm.model:
        return RepairEngine()
    api_key = os.getenv(settings.llm.api_key_env)
    client = OpenAICompatibleSemanticPortClient(
        OpenAICompatibleConfig(
            model=settings.llm.model,
            api_key=api_key,
            base_url=settings.llm.base_url,
            temperature=(
                args.llm_temperature
                if args.llm_temperature is not None
                else settings.llm.temperature
            ),
            max_context_chars=(
                args.llm_max_context_chars
                if args.llm_max_context_chars is not None
                else settings.llm.max_context_chars
            ),
        )
    )
    return RepairEngine(client)


if __name__ == "__main__":
    main()
