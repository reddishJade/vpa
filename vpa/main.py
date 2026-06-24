import argparse
import sys
from pathlib import Path

from .orchestrator.models import GatePolicy, RiskPreference
from .orchestrator.promotion import PromotionConfig, PromotionOrchestrator, render_plan


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="vpa - architecture-port promotion workflow")
    sub = parser.add_subparsers(dest="command")

    promote_p = sub.add_parser("promote", help="Plan the architecture-port workflow")
    promote_p.add_argument("--upstream-repo", required=True, help="Path to upstream Git repo")
    promote_p.add_argument("--local-repo", required=True, help="Path to local target repo")
    promote_p.add_argument("--rev-range", required=True, help="Git revision range to promote")
    promote_p.add_argument(
        "--target-isa-path",
        default="src/dynarec/sw64_core3",
        help="Target ISA path in the local repo",
    )
    promote_p.add_argument(
        "--reference-isa-path",
        default="src/dynarec/rv64",
        help="Primary reference ISA path in upstream",
    )
    promote_p.add_argument(
        "--fallback-reference-isa-path",
        action="append",
        default=[],
        help="Fallback reference ISA path (repeatable)",
    )
    promote_p.add_argument("--build-cmd", default=None, help="Configured build command")
    promote_p.add_argument(
        "--smoke-test",
        action="append",
        default=[],
        help="Configured smoke/test command (repeatable)",
    )
    promote_p.add_argument("--ledger-path", default=None, help="Ledger artifact path")
    promote_p.add_argument("--report-path", default=None, help="Report artifact path")
    promote_p.add_argument("--dry-run", action="store_true", help="Plan without mutating repos")
    promote_p.add_argument(
        "--semantic-confidence-threshold",
        type=float,
        default=0.65,
        help="Minimum analyzer confidence before semantic porting",
    )
    promote_p.add_argument(
        "--manual-confidence-threshold",
        type=float,
        default=0.4,
        help="Confidence below which manual review is preferred",
    )
    promote_p.add_argument(
        "--risk-preference",
        choices=[item.value for item in RiskPreference],
        default=RiskPreference.BALANCED.value,
        help="Gate risk preference",
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "promote":
        policy = GatePolicy(
            semantic_confidence_threshold=args.semantic_confidence_threshold,
            manual_confidence_threshold=args.manual_confidence_threshold,
            risk_preference=RiskPreference(args.risk_preference),
            dry_run=args.dry_run,
        )
        config = PromotionConfig(
            upstream_repo=Path(args.upstream_repo),
            local_repo=Path(args.local_repo),
            revision_range=args.rev_range,
            target_isa_path=Path(args.target_isa_path),
            primary_reference_isa_path=Path(args.reference_isa_path),
            fallback_reference_isa_paths=[
                Path(path) for path in args.fallback_reference_isa_path
            ],
            build_command=args.build_cmd,
            smoke_commands=args.smoke_test,
            dry_run=args.dry_run,
            ledger_path=Path(args.ledger_path) if args.ledger_path else None,
            report_path=Path(args.report_path) if args.report_path else None,
            gate_policy=policy,
        )
        plan = PromotionOrchestrator(config).plan()
        print(render_plan(plan))
        return

    parser.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()
