import argparse
import sys

from .harness import retry_with_hint, run_promotion


def main(argv=None, agent_runner=None, validation_runner=None):
    """Run VPA CLI.

    Parameters
    ----------
    argv : list[str] | None
        CLI arguments (default: sys.argv[1:]).
    agent_runner : callable | None
        Inject mock agent runner for testing (default: run_agent).
    validation_runner : callable | None
        Inject mock validation runner for testing (default: run_fast_validation).
    """
    parser = argparse.ArgumentParser(
        description="vpa — version promotion agent",
    )
    sub = parser.add_subparsers(dest="command")

    # run
    run_p = sub.add_parser("run", help="Run the promotion loop")
    run_p.add_argument("--upstream-path", required=True, help="Path to upstream repo")
    run_p.add_argument("--local-path", required=True, help="Path to local repo")
    run_p.add_argument("--upstream-old", required=True, help="Upstream old revision/tag")
    run_p.add_argument("--upstream-new", required=True, help="Upstream new revision/tag")
    run_p.add_argument("--local-branch", required=True, help="Local branch name")
    run_p.add_argument("--build-cmd", required=True, help="Build command (e.g. 'make -j8')")
    run_p.add_argument(
        "--fast-test",
        action="append",
        default=[],
        help="Fast test command (repeatable)",
    )
    run_p.add_argument(
        "--slow-test",
        action="append",
        default=[],
        help="Slow test command (repeatable)",
    )
    run_p.add_argument("--model", default="gpt-4o", help="Model name")
    run_p.add_argument("--api-key", default=None, help="OpenAI-compatible API key")
    run_p.add_argument("--base-url", default=None, help="API base URL")
    run_p.add_argument("--output-dir", default="./promotion_output", help="Output directory")
    run_p.add_argument("--upstream-name", default="upstream", help="Upstream name for prompts")
    run_p.add_argument("--local-name", default="local", help="Local name for prompts")
    run_p.add_argument("--arch", default="<arch>", help="Target architecture description")
    run_p.add_argument(
        "--max-commits-per-restart", type=int, default=10, help="Restart after N commits"
    )

    # retry
    retry_p = sub.add_parser("retry", help="Retry a manual_required commit with a hint")
    retry_p.add_argument("--commit-sha", required=True, help="Commit SHA to retry")
    retry_p.add_argument("--hint", required=True, help="Human hint for the retry")
    retry_p.add_argument("--upstream-path", required=True)
    retry_p.add_argument("--local-path", required=True)
    retry_p.add_argument("--upstream-old", required=True)
    retry_p.add_argument("--upstream-new", required=True)
    retry_p.add_argument("--local-branch", required=True)
    retry_p.add_argument("--output-dir", required=True)
    retry_p.add_argument("--build-cmd", required=True)
    retry_p.add_argument("--fast-test", action="append", default=[])
    retry_p.add_argument("--model", default="gpt-4o")
    retry_p.add_argument("--api-key", default=None)
    retry_p.add_argument("--base-url", default=None)
    retry_p.add_argument("--upstream-name", default="upstream")
    retry_p.add_argument("--local-name", default="local")
    retry_p.add_argument("--arch", default="<arch>")

    args = parser.parse_args(argv)

    if args.command == "run":
        summary, _ = run_promotion(
            upstream_path=args.upstream_path,
            local_path=args.local_path,
            upstream_old=args.upstream_old,
            upstream_new=args.upstream_new,
            local_branch=args.local_branch,
            build_cmd=args.build_cmd,
            fast_test_cmds=args.fast_test,
            slow_test_cmds=args.slow_test,
            model=args.model,
            api_key=args.api_key,
            base_url=args.base_url,
            output_dir=args.output_dir,
            upstream_name=args.upstream_name,
            local_name=args.local_name,
            arch=args.arch,
            max_commits_per_restart=args.max_commits_per_restart,
            agent_runner=agent_runner,
            validation_runner=validation_runner,
        )
        print(summary)

    elif args.command == "retry":
        result = retry_with_hint(
            commit_sha=args.commit_sha,
            hint=args.hint,
            upstream_path=args.upstream_path,
            local_path=args.local_path,
            upstream_old=args.upstream_old,
            upstream_new=args.upstream_new,
            local_branch=args.local_branch,
            output_dir=args.output_dir,
            build_cmd=args.build_cmd,
            fast_test_cmds=args.fast_test,
            model=args.model,
            api_key=args.api_key,
            base_url=args.base_url,
            upstream_name=args.upstream_name,
            local_name=args.local_name,
            arch=args.arch,
            agent_runner=agent_runner,
            validation_runner=validation_runner,
        )
        if result:
            print(f"Retry succeeded: {result['status']}")
        else:
            print("Retry failed — marked as final_manual")

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
